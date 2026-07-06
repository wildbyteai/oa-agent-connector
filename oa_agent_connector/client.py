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

SEARCH_LIMITS = {
    "queryMaxLength": 200,
    "pageSizeDefault": 20,
    "pageSizeMax": 50,
    "batchQueriesMax": 100,
    "batchPageSizeDefault": 5,
    "batchPageSizeMax": 20,
    "maxDetailsPerQueryDefault": 1,
    "maxDetailsPerQueryMax": 3,
    "detailTextLimitDefault": 12000,
    "detailTextLimitMax": 20000,
    "downloadMaxBytesDefault": 52428800,
    "batchMaxDownloadsDefault": 0,
    "batchMaxDownloadsMax": 50,
}

SEARCH_FIELD_MAP = {
    "title": "subject",
    "content": "content",
    "fdDescription": "fdDescription",
    "creator": "creator",
    "attachment": "attachment",
}

SEARCH_SCOPES = {
    "all": {
        "description": "OA 全系统搜索，默认只返回搜索结果元数据，不进入未知模块详情解析",
        "allowedModelNames": ["*"],
        "models": [
            {"modelName": "*", "title": "全部", "supportsDetail": False, "supportsAttachments": False},
        ],
    },
    "knowledge": {
        "description": "文档知识库",
        "allowedModelNames": [
            "KmsMultidocKnowledge",
            "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge",
        ],
        "detailParser": "kms_multidoc_knowledge",
        "models": [
            {
                "modelName": "KmsMultidocKnowledge",
                "title": "文档知识库",
                "supportsDetail": True,
                "supportsAttachments": True,
            },
        ],
    },
    "news": {
        "description": "新闻文档",
        "allowedModelNames": ["SysNewsMain", "com.landray.kmss.sys.news.model.SysNewsMain"],
        "models": [
            {"modelName": "SysNewsMain", "title": "新闻文档", "supportsDetail": False, "supportsAttachments": False},
        ],
    },
}

ALLOWED_BONDS = ("or", "and", "like")
ALLOWED_SORT_TYPES = ("relevance", "readCount", "time")
ALLOWED_SORT_ORDERS = ("asc", "desc")
ALLOWED_TIME_RANGES = ("", "day", "week", "month", "year")
ALLOWED_DOC_FILE_TYPES = ("", "pdf", "doc;docx", "xls;xlsx", "ppt;pptx", "txt")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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

    def get_search_schema(self, scope: str = "all") -> Dict[str, Any]:
        scope = scope or "all"
        config = self._scope_config(scope)
        return {
            "scope": scope,
            "models": list(config.get("models", [])),
            "searchFields": list(SEARCH_FIELD_MAP.keys()),
            "bond": list(ALLOWED_BONDS),
            "sortTypes": list(ALLOWED_SORT_TYPES),
            "sortOrders": list(ALLOWED_SORT_ORDERS),
            "timeRanges": [value for value in ALLOWED_TIME_RANGES if value],
            "docFileTypes": [value for value in ALLOWED_DOC_FILE_TYPES if value],
            "limits": dict(SEARCH_LIMITS),
        }

    def _scope_config(self, scope: str) -> Dict[str, Any]:
        if scope not in SEARCH_SCOPES:
            raise OAConnectorError("不支持的搜索范围或模块")
        return SEARCH_SCOPES[scope]

    def _normalize_model_name(self, scope: str, model_name: Optional[str]) -> Optional[str]:
        if not model_name:
            return None
        allowed = self._scope_config(scope).get("allowedModelNames", [])
        if "*" not in allowed and model_name not in allowed:
            raise OAConnectorError("不支持的搜索范围或模块")
        return str(model_name)

    def _validate_search_params(self, params: Dict[str, Any], *, batch: bool = False) -> Dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise OAConnectorError("搜索关键词不能为空")
        if len(query) > SEARCH_LIMITS["queryMaxLength"] or CONTROL_CHAR_RE.search(query):
            raise OAConnectorError("搜索关键词不合法")

        scope = str(params.get("scope") or "all")
        self._scope_config(scope)
        model_name = self._normalize_model_name(scope, params.get("modelName"))

        bond = str(params.get("bond") or "or")
        if bond not in ALLOWED_BONDS:
            raise OAConnectorError("不支持的关键词关系")

        raw_fields = params.get("searchFields") or []
        if isinstance(raw_fields, str):
            raw_fields = [raw_fields]
        search_fields: List[str] = []
        for field in raw_fields:
            field = str(field)
            if field not in SEARCH_FIELD_MAP:
                raise OAConnectorError("不支持的搜索字段")
            search_fields.append(SEARCH_FIELD_MAP[field])

        doc_file_type = str(params.get("docFileType") or "")
        if doc_file_type not in ALLOWED_DOC_FILE_TYPES:
            raise OAConnectorError("不支持的附件类型")

        sort_type = str(params.get("sortType") or "relevance")
        if sort_type not in ALLOWED_SORT_TYPES:
            raise OAConnectorError("不支持的排序字段")
        sort_order = str(params.get("sortOrder") or "desc")
        if sort_order not in ALLOWED_SORT_ORDERS:
            raise OAConnectorError("不支持的排序方向")

        time_range = str(params.get("timeRange") or "")
        if time_range not in ALLOWED_TIME_RANGES:
            raise OAConnectorError("不支持的时间范围")

        from_create_time = str(params.get("fromCreateTime") or "")
        to_create_time = str(params.get("toCreateTime") or "")
        for value in (from_create_time, to_create_time):
            if value and not DATE_RE.match(value):
                raise OAConnectorError("日期格式必须为 YYYY-MM-DD")
        if from_create_time and to_create_time and from_create_time > to_create_time:
            raise OAConnectorError("开始日期不得晚于结束日期")

        default_page_size = SEARCH_LIMITS["batchPageSizeDefault"] if batch else SEARCH_LIMITS["pageSizeDefault"]
        max_page_size = SEARCH_LIMITS["batchPageSizeMax"] if batch else SEARCH_LIMITS["pageSizeMax"]
        try:
            page = int(params.get("page") or 1)
            page_size = int(params.get("pageSize") or default_page_size)
        except (ValueError, TypeError):
            raise OAConnectorError("page/pageSize 必须为正整数")
        if page < 1:
            raise OAConnectorError("页码必须大于 0")
        if page_size < 1 or page_size > max_page_size:
            raise OAConnectorError("pageSize 超过允许范围")

        return {
            "query": query,
            "scope": scope,
            "modelName": model_name,
            "bond": bond,
            "outKeyword": str(params.get("outKeyword") or ""),
            "searchFields": search_fields,
            "docFileType": doc_file_type,
            "timeRange": time_range,
            "fromCreateTime": from_create_time,
            "toCreateTime": to_create_time,
            "category": str(params.get("category") or ""),
            "docStatus": str(params.get("docStatus") or ""),
            "sortType": sort_type,
            "sortOrder": sort_order,
            "exactTitle": bool(params.get("exactTitle", False)),
            "onlyExactTitle": bool(params.get("onlyExactTitle", False)),
            "page": page,
            "pageSize": page_size,
        }

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

    def search_objects(self, **kwargs: Any) -> Dict[str, Any]:
        validated = self._validate_search_params(kwargs)
        params: Dict[str, str] = {
            "method": "search",
            "resultType": "json",
            "newLUI": "true",
            "searchAll": "true",
            "queryString": validated["query"],
            "pageno": str(validated["page"]),
            "rowsize": str(validated["pageSize"]),
            "bond": validated["bond"],
        }
        if validated["outKeyword"]:
            params["outKeyword"] = validated["outKeyword"]
        if validated["searchFields"]:
            params["searchFields"] = ",".join(validated["searchFields"])
        if validated["docFileType"]:
            params["docFileType"] = validated["docFileType"]
        if validated["timeRange"]:
            params["timeRange"] = validated["timeRange"]
        if validated["fromCreateTime"]:
            params["fromCreateTime"] = validated["fromCreateTime"]
        if validated["toCreateTime"]:
            params["toCreateTime"] = validated["toCreateTime"]
        if validated["category"]:
            params["category"] = validated["category"]
        if validated["docStatus"]:
            params["docStatus"] = validated["docStatus"]
        if validated["modelName"]:
            params["modelName"] = validated["modelName"]
        if validated["sortType"] != "relevance":
            params["sortType"] = validated["sortType"]
        if validated["sortType"] == "time":
            params["sortOrder"] = validated["sortOrder"]

        response = self._request("sys/ftsearch/searchBuilder.do", params=params)
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能搜索 OA 内容")
        parsed = self._try_json(response["text"].lstrip("﻿\r\n\t "))
        if parsed is None:
            raise OAConnectorError("搜索出现错误：OA 返回非 JSON 响应")
        return self._parse_search_results(parsed, validated)

    def _parse_search_results(self, payload: Any, validated: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(payload, dict) and payload.get("EsError"):
            raise OAConnectorError(f"搜索出现错误：{str(payload.get('EsError'))[:120]}")
        query_page = payload.get("queryPage", payload) if isinstance(payload, dict) else {}
        rows = []
        if isinstance(query_page, dict):
            rows = query_page.get("list") or query_page.get("rows") or query_page.get("data") or []
        if not isinstance(rows, list):
            rows = []

        items: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record_ref = self._record_ref_from_search_row(row, validated["scope"])
            if not record_ref:
                continue
            fields = row.get("lksFieldsMap") if isinstance(row.get("lksFieldsMap"), dict) else {}
            title = self._clean_search_title(
                fields.get("subject") or row.get("subject") or row.get("docSubject") or row.get("title") or ""
            )
            matched_exact = title == validated["query"] if validated["exactTitle"] else False
            if validated["onlyExactTitle"] and not matched_exact:
                continue
            model_name = str(record_ref.get("modelName") or row.get("modelName") or "")
            supports_detail, supports_attachments = self._model_capabilities(validated["scope"], model_name)
            summary = self._strip_html(str(row.get("content") or row.get("fdDescription") or ""))[:300]
            read_count = self._to_int(row.get("docReadCount") or row.get("readCount"))
            items.append(
                {
                    "recordRef": record_ref,
                    "fdId": record_ref["recordId"],
                    "title": title,
                    "summary": summary,
                    "creator": self._strip_html(str(row.get("creator") or "")),
                    "createTime": str(row.get("createTime") or ""),
                    "readCount": read_count,
                    "modelTitle": str(row.get("modelTitle") or ""),
                    "matchedExactTitle": matched_exact,
                    "supportsDetail": supports_detail,
                    "supportsAttachments": supports_attachments,
                }
            )
        total = self._to_int(query_page.get("totalrows") if isinstance(query_page, dict) else None)
        return {
            "query": validated["query"],
            "items": items,
            "page": validated["page"],
            "pageSize": validated["pageSize"],
            "total": total if total is not None else len(items),
            "totalNote": "以 OA 搜索接口返回为准",
        }

    def _record_ref_from_search_row(self, row: Dict[str, Any], scope: str) -> Optional[Dict[str, str]]:
        model_name = str(row.get("modelName") or "")
        path = str(row.get("linkStr") or "")
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0].split("/"):
            return None
        record_id = str(row.get("docKey") or "").strip()
        if not record_id:
            match = re.search(r"(?:fdId=|fdId/)([0-9a-fA-F]{24,40})", path)
            record_id = match.group(1) if match else ""
        if not record_id:
            return None
        return {"scope": scope, "modelName": model_name, "recordId": record_id, "path": path}

    def _model_capabilities(self, scope: str, model_name: str) -> tuple[bool, bool]:
        config = self._scope_config(scope)
        if config.get("detailParser") == "kms_multidoc_knowledge":
            allowed = config.get("allowedModelNames", [])
            if model_name in allowed or model_name.endswith("KmsMultidocKnowledge"):
                return True, True
        return False, False

    def _clean_search_title(self, value: Any) -> str:
        return self._strip_html(str(value or ""))

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

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
            if self._should_fallback_to_ui_form(exc):
                return self._approval_action_via_ui(fd_id, operation_type, audit_note, future_node_id)
            raise
        except TimeoutError:
            return self._approval_action_via_ui(fd_id, operation_type, audit_note, future_node_id)
        text = response["text"].strip()
        if text == fd_id:
            self._save_cookies()
            return {"dryRun": False, "fdId": fd_id, "result": text, "transport": "rest"}

        if "Unauthorized" in text or "HTTP 401" in text:
            return self._approval_action_via_ui(fd_id, operation_type, audit_note, future_node_id)
        raise OAConnectorError(f"审批接口返回异常: {text[:500]}")

    def _should_fallback_to_ui_form(self, exc: OAConnectorError) -> bool:
        message = str(exc)
        lowered = message.lower()
        return (
            "HTTP 401" in message
            or "Unauthorized" in message
            or "timeout" in lowered
            or "timed out" in lowered
        )

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
        task = self._find_review_workitem(form_data.get("sysWfBusinessForm.fdCurNodeXML", ""), operation_type)
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

    def _find_review_workitem(self, current_node_xml: str, required_operation: Optional[str] = None) -> Dict[str, str]:
        regex_task = self._find_review_workitem_by_regex(current_node_xml, required_operation)
        if regex_task:
            return regex_task

        try:
            root = ET.fromstring(current_node_xml)
        except ET.ParseError as exc:
            raise OAConnectorError("未找到当前登录账号可处理的流程 workitem") from exc
        for task in root.findall(".//task"):
            task_type = task.attrib.get("type", "")
            operations = {op.attrib.get("id") for op in task.findall(".//operation")}
            if self._is_review_workitem(task_type, operations, required_operation):
                return {"id": task.attrib["id"], "type": task_type}
        raise OAConnectorError("未找到当前登录账号可处理的流程 workitem")

    def _find_review_workitem_by_regex(
        self, current_node_xml: str, required_operation: Optional[str] = None
    ) -> Optional[Dict[str, str]]:
        task_pattern = re.compile(r"<task\b(?P<attrs>[^>]*)>(?P<body>.*?)</task>", re.I | re.S)
        operation_pattern = re.compile(r"<operation\b(?P<attrs>[^>]*)/?>", re.I | re.S)
        for task_match in task_pattern.finditer(current_node_xml):
            task_attrs = self._attrs_from_text(task_match.group("attrs"))
            task_type = task_attrs.get("type", "")
            task_id = task_attrs.get("id", "")
            if not task_id:
                continue
            operations = {
                self._attrs_from_text(operation_match.group("attrs")).get("id")
                for operation_match in operation_pattern.finditer(task_match.group("body"))
            }
            if self._is_review_workitem(task_type, operations, required_operation):
                return {"id": task_id, "type": task_type}
        return None

    def _attrs_from_text(self, text: str) -> Dict[str, str]:
        attrs: Dict[str, str] = {}
        for match in re.finditer(r"""\b([:\w.-]+)\s*=\s*(["'])(.*?)\2""", text, re.S):
            attrs[match.group(1)] = unescape(match.group(3))
        return attrs

    def _is_review_workitem(
        self, task_type: str, operations: set[Optional[str]], required_operation: Optional[str] = None
    ) -> bool:
        if task_type != "reviewWorkitem":
            return False
        available_operations = {operation for operation in operations if operation}
        if required_operation:
            return required_operation in available_operations
        return bool({"handler_pass", "handler_refuse"} & available_operations)

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
