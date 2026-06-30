from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET


FD_ID_RE = re.compile(r"\bfdId[=/]([0-9a-fA-F]{24,40})")
FD_ID_VALUE_RE = re.compile(r"""(?:["']?fdId["']?\s*[:=]\s*["']|value=["'])([0-9a-fA-F]{24,40})""")
SUBJECT_RE = re.compile(r"""class=["'][^"']*com_subject[^"']*["'][^>]*>(.*?)</""", re.I | re.S)


class OAConnectorError(RuntimeError):
    pass


class PermissionGateError(OAConnectorError):
    pass


@dataclass(frozen=True)
class OATodo:
    fd_id: str
    subject: str = ""
    raw: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = {"fdId": self.fd_id, "subject": self.subject}
        if self.raw:
            data["raw"] = self.raw
        return data


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: List[tuple[str, str]] = []
        self._textarea_name: Optional[str] = None
        self._textarea_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "input" and "name" in data:
            input_type = data.get("type", "text").lower()
            if input_type in ("checkbox", "radio") and "checked" not in data:
                return
            self.fields.append((data["name"], data.get("value", "")))
        elif tag == "textarea" and "name" in data:
            self._textarea_name = data["name"]
            self._textarea_parts = []

    def handle_data(self, data: str) -> None:
        if self._textarea_name:
            self._textarea_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._textarea_name:
            self.fields.append((self._textarea_name, "".join(self._textarea_parts)))
            self._textarea_name = None
            self._textarea_parts = []


class OAClient:
    """Small connector for OA approval operations.

    This is intentionally session-bound. Approval execution first reloads the
    logged-in user's "待我审" list and refuses to touch documents absent from it.
    """

    def __init__(
        self,
        base_url: str,
        cookie_file: Optional[str] = None,
        timeout: int = 30,
        verify_tls: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.cookie_file = Path(cookie_file).expanduser() if cookie_file else None
        self.cookie_jar = MozillaCookieJar(str(self.cookie_file)) if self.cookie_file else MozillaCookieJar()
        if self.cookie_file and self.cookie_file.exists():
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if not verify_tls:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def login(self, username: str, password: str) -> bool:
        response = self._request(
            "j_acegi_security_check",
            method="POST",
            data={"j_username": username, "j_password": password},
        )
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("登录失败或仍停留在登录页")
        self._save_cookies()
        return True

    def assert_logged_in(self) -> None:
        response = self._request(
            "km/review/km_review_index/kmReviewIndex.do",
            params={"method": "list", "j_path": "/listApproval", "mydoc": "approval", "q.mydoc": "approval"},
        )
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前 cookie 未登录或已失效，请先 login")

    def list_todos(self, page: int = 1, page_size: int = 20) -> List[OATodo]:
        response = self._request(
            "km/review/km_review_index/kmReviewIndex.do",
            params={
                "method": "list",
                "j_path": "/listApproval",
                "mydoc": "approval",
                "q.mydoc": "approval",
                "cri.q": "docStatus:20",
                "pageno": str(page),
                "rowsize": str(page_size),
            },
        )
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能查询待办")
        return self._parse_todos(response["text"])

    def get_detail(self, fd_id: str, require_in_todo: bool = True) -> Dict[str, Any]:
        if require_in_todo:
            self._assert_fd_id_in_current_todos(fd_id)
        response = self._request(
            "km/review/km_review_main/kmReviewMain.do",
            params={"method": "view", "fdId": fd_id},
        )
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能查看审批详情")
        return {
            "fdId": fd_id,
            "url": response["url"],
            "title": self._extract_title(response["text"]),
            "text": self._strip_html(response["text"])[:8000],
        }

    def approve(
        self,
        fd_id: str,
        audit_note: str,
        execute: bool = False,
        future_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._approval_action(
            fd_id=fd_id,
            operation_type="handler_pass",
            audit_note=audit_note,
            execute=execute,
            future_node_id=future_node_id,
        )

    def reject(self, fd_id: str, audit_note: str, execute: bool = False) -> Dict[str, Any]:
        return self._approval_action(
            fd_id=fd_id,
            operation_type="handler_refuse",
            audit_note=audit_note,
            execute=execute,
        )

    def _approval_action(
        self,
        fd_id: str,
        operation_type: str,
        audit_note: str,
        execute: bool,
        future_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._assert_fd_id_in_current_todos(fd_id)
        flow_param: Dict[str, Any] = {
            "operationType": operation_type,
            "auditNote": audit_note,
            "operParam": {},
        }
        if future_node_id:
            flow_param["futureNodeId"] = future_node_id

        payload = {"fdId": fd_id, "flowParam": json.dumps(flow_param, ensure_ascii=False)}
        endpoint = "api/km-review/kmReviewRestService/approveProcess"
        if not execute:
            return {
                "dryRun": True,
                "method": "POST",
                "endpoint": urllib.parse.urljoin(self.base_url, endpoint),
                "payload": payload,
                "permissionGate": "fdId was present in current session's type=unExecuted list",
            }

        try:
            response = self._request(endpoint, method="POST", data=payload)
        except OAConnectorError as exc:
            if "HTTP 401" in str(exc) or "Unauthorized" in str(exc):
                return self._approval_action_via_ui(fd_id, operation_type, audit_note, future_node_id)
            raise
        text = response["text"].strip()
        if text == fd_id:
            self._save_cookies()
            return {"dryRun": False, "fdId": fd_id, "result": text, "transport": "rest"}

        if "Unauthorized" in text or "HTTP 401" in text:
            return self._approval_action_via_ui(fd_id, operation_type, audit_note, future_node_id)
        raise OAConnectorError(f"审批接口返回异常: {text[:500]}")

    def _approval_action_via_ui(
        self,
        fd_id: str,
        operation_type: str,
        audit_note: str,
        future_node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        edit = self._request(
            "km/review/km_review_main/kmReviewMain.do",
            params={"method": "edit", "fdId": fd_id},
        )
        if self._looks_like_login_page(edit["url"], edit["text"]):
            raise OAConnectorError("当前会话未登录，不能处理审批")

        form_data = self._parse_form_fields(edit["text"])
        process_id = form_data.get("sysWfBusinessForm.fdProcessId") or fd_id
        audit_note_id = form_data.get("sysWfBusinessForm.fdAuditNoteFdId") or ""
        task = self._find_review_workitem(form_data.get("sysWfBusinessForm.fdCurNodeXML", ""))
        param: Dict[str, Any] = {
            "operationName": "驳回" if operation_type == "handler_refuse" else "通过",
            "notifyType": "{}",
            "notifyLevel": "3",
            "notifyOnFinish": False,
            "notifyForFollow": False,
            "auditNote": audit_note,
            "auditNoteFdId": audit_note_id,
        }

        if operation_type == "handler_refuse":
            node_id = self._current_node_id(form_data.get("sysWfBusinessForm.fdTranProcessXML", ""))
            jump_node_id = self._default_refuse_node(process_id, node_id)
            param.update(
                {
                    "jumpToNodeId": jump_node_id,
                    "jumpToNodeInstanceId": "",
                    "refusePassedToThisNode": False,
                    "refusePassedToThisNodeOnNode": False,
                    "refusePassedToTheNode": False,
                    "lbpmHandlerTriage": "",
                    "isRecoverPassedSubprocess": False,
                }
            )
        elif future_node_id:
            param["futureNodeId"] = future_node_id

        fd_parameter = {
            "taskId": task["id"],
            "processId": process_id,
            "activityType": task["type"],
            "operationType": operation_type,
            "param": param,
        }
        form_data["sysWfBusinessForm.fdParameterJson"] = json.dumps(fd_parameter, ensure_ascii=False)
        form_data["sysWfBusinessForm.fdSystemNotifyType"] = "{}"
        form_data["fdUsageContent"] = audit_note

        response = self._request(
            "km/review/km_review_main/kmReviewMain.do",
            method="POST",
            params={"method": "update"},
            data=form_data,
        )
        plain = self._strip_html(response["text"])
        if '"status":true' not in response["text"] and "您的操作已成功" not in plain:
            raise OAConnectorError(f"审批表单提交失败: {plain[:800]}")

        self._save_cookies()
        return {
            "dryRun": False,
            "fdId": fd_id,
            "result": "success",
            "transport": "ui-form",
            "operationType": operation_type,
        }

    def _parse_form_fields(self, html_text: str) -> Dict[str, str]:
        parser = _FormParser()
        parser.feed(html_text)
        data: Dict[str, str] = {}
        for key, value in parser.fields:
            data.setdefault(key, unescape(value))
        return data

    def _find_review_workitem(self, current_node_xml: str) -> Dict[str, str]:
        root = ET.fromstring(current_node_xml)
        for task in root.findall(".//task"):
            task_type = task.attrib.get("type", "")
            operations = {op.attrib.get("id") for op in task.findall(".//operation")}
            if task_type == "reviewWorkitem" and ("handler_pass" in operations or "handler_refuse" in operations):
                return {"id": task.attrib["id"], "type": task_type}
        raise OAConnectorError("未找到当前登录账号可处理的流程 workitem")

    def _current_node_id(self, tran_process_xml: str) -> str:
        root = ET.fromstring(tran_process_xml)
        node = root.find(".//runningNodes/node")
        if node is None or not node.attrib.get("id"):
            raise OAConnectorError("未找到当前运行节点")
        return node.attrib["id"]

    def _default_refuse_node(self, process_id: str, node_id: str) -> str:
        response = self._request(
            "sys/lbpm/engine/jsonp.jsp",
            method="POST",
            data={
                "s_bean": "lbpmRefuseRuleDataBean",
                "processId": process_id,
                "nodeId": node_id,
            },
        )
        try:
            nodes = json.loads(response["text"].strip())
        except json.JSONDecodeError as exc:
            raise OAConnectorError(f"读取可驳回节点失败: {response['text'][:500]}") from exc
        if not nodes:
            raise OAConnectorError("当前节点没有可驳回节点")
        return str(nodes[0]).split("#", 1)[0]

    def _assert_fd_id_in_current_todos(self, fd_id: str) -> None:
        todo_ids = {todo.fd_id for todo in self.list_todos(page=1, page_size=200)}
        if fd_id not in todo_ids:
            raise PermissionGateError(f"拒绝操作：{fd_id} 不在当前登录账号的待审批列表中")

    def _request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        url = urllib.parse.urljoin(self.base_url, path.lstrip("/"))
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "User-Agent": "oa-agent-connector/0.1",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Accept": "application/json,text/json,text/html,*/*",
            },
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return {"url": resp.geturl(), "text": raw.decode(charset, errors="replace")}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise OAConnectorError(f"HTTP {exc.code}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise OAConnectorError(f"请求 OA 失败: {exc}") from exc

    def _parse_todos(self, text: str) -> List[OATodo]:
        stripped = text.lstrip("\ufeff\r\n\t ")
        parsed = self._try_json(stripped)
        if parsed is not None:
            table_rows = self._parse_column_datas(parsed)
            if table_rows:
                return [todo for todo in (self._row_to_todo(row) for row in table_rows) if todo is not None]
            rows = self._find_rows(parsed)
            todos = [self._row_to_todo(row) for row in rows]
            return [todo for todo in todos if todo is not None]

        fd_ids = []
        for match in list(FD_ID_RE.finditer(text)) + list(FD_ID_VALUE_RE.finditer(text)):
            fd_id = match.group(1)
            if fd_id not in fd_ids:
                fd_ids.append(fd_id)
        subjects = [self._strip_html(match.group(1)).strip() for match in SUBJECT_RE.finditer(text)]
        return [OATodo(fd_id=fd_id, subject=subjects[i] if i < len(subjects) else "") for i, fd_id in enumerate(fd_ids)]

    def _try_json(self, text: str) -> Optional[Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def _find_rows(self, node: Any) -> List[Dict[str, Any]]:
        if isinstance(node, list):
            return [row for row in node if isinstance(row, dict)]
        if isinstance(node, dict):
            for key in ("rows", "list", "data", "items"):
                value = node.get(key)
                if isinstance(value, list):
                    return [row for row in value if isinstance(row, dict)]
            for value in node.values():
                rows = self._find_rows(value)
                if rows:
                    return rows
        return []

    def _parse_column_datas(self, node: Any) -> List[Dict[str, Any]]:
        if not isinstance(node, dict) or not isinstance(node.get("datas"), list):
            return []
        rows: List[Dict[str, Any]] = []
        for row in node["datas"]:
            if not isinstance(row, list):
                continue
            mapped: Dict[str, Any] = {}
            for cell in row:
                if not isinstance(cell, dict):
                    continue
                key = str(cell.get("col") or cell.get("property") or "").strip()
                if key:
                    mapped[key] = cell.get("value", "")
            if mapped:
                rows.append(mapped)
        return rows

    def _row_to_todo(self, row: Dict[str, Any]) -> Optional[OATodo]:
        fd_id = str(row.get("fdId") or row.get("fd_id") or "").strip()
        if not fd_id:
            return None
        subject = str(row.get("docSubject") or row.get("subject") or row.get("title") or "").strip()
        subject = self._strip_html(subject)
        return OATodo(fd_id=fd_id, subject=subject, raw=row)

    def _looks_like_login_page(self, url: str, text: str) -> bool:
        lowered = (url + "\n" + text[:3000]).lower()
        return "j_acegi_security_check" in lowered or "j_username" in lowered and "j_password" in lowered

    def _save_cookies(self) -> None:
        if self.cookie_file:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
            try:
                self.cookie_file.chmod(0o600)
            except OSError:
                pass

    def _extract_title(self, text: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        return self._strip_html(match.group(1)).strip() if match else ""

    def _strip_html(self, text: str) -> str:
        text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()
