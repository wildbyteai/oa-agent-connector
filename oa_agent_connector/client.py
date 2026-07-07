from __future__ import annotations

import json
import re
import ssl
import tempfile
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
ALLOWED_MATCH_MODES = ("keyword", "contains", "exact")
ALLOWED_SORT_TYPES = ("relevance", "readCount", "time")
ALLOWED_SORT_ORDERS = ("asc", "desc")
ALLOWED_TIME_RANGES = ("", "day", "week", "month", "year")
ALLOWED_DOC_FILE_TYPES = ("", "pdf", "doc;docx", "xls;xlsx", "ppt;pptx", "txt")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
ATTACHMENT_TITLE_RE = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?|txt|jpe?g|png|gif|bmp|zip|rar|7z)(?:$|[\s)）\]}】])", re.I)


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
        data = {
            "fdId": self.fd_id,
            "subject": self.subject,
            "detailAvailable": True,
            "detailAction": {
                "tool": "oa_get_detail",
                "arguments": {"fdId": self.fd_id},
            },
        }
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
            "matchMode": list(ALLOWED_MATCH_MODES),
            "sortTypes": list(ALLOWED_SORT_TYPES),
            "sortOrders": list(ALLOWED_SORT_ORDERS),
            "timeRanges": [value for value in ALLOWED_TIME_RANGES if value],
            "docFileTypes": [value for value in ALLOWED_DOC_FILE_TYPES if value],
            "resultDefaults": {"requireDetail": True, "dedupByDocument": True},
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

        exact_title = self._bool_param(params.get("exactTitle"), False)
        only_exact_title = self._bool_param(params.get("onlyExactTitle"), False)
        match_mode = str(params.get("matchMode") or "").strip()
        if not match_mode:
            match_mode = "exact" if only_exact_title else "keyword"
        if match_mode not in ALLOWED_MATCH_MODES:
            raise OAConnectorError("不支持的标题匹配模式")

        return {
            "query": query,
            "normalizedQuery": self._normalize_search_text(query),
            "scope": scope,
            "modelName": model_name,
            "bond": bond,
            "matchMode": match_mode,
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
            "exactTitle": exact_title,
            "onlyExactTitle": only_exact_title,
            "dedupByDocument": self._bool_param(params.get("dedupByDocument"), True),
            "requireDetail": self._bool_param(params.get("requireDetail"), True),
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
        candidate_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            record_ref = self._record_ref_from_search_row(row, validated["scope"])
            if not record_ref:
                continue
            title = self._clean_search_title(
                self._search_row_value(row, "subject")
                or self._search_row_value(row, "docSubject")
                or self._search_row_value(row, "title")
                or self._search_row_value(row, "fileName")
            )
            normalized_title = self._normalize_search_text(title)
            matched_exact = normalized_title == validated["normalizedQuery"]
            matched_contains = bool(validated["normalizedQuery"] and validated["normalizedQuery"] in normalized_title)
            if validated["matchMode"] == "exact" and not matched_exact:
                continue
            if validated["matchMode"] == "contains" and not matched_contains:
                continue
            if validated["onlyExactTitle"] and not matched_exact:
                continue
            model_name = str(record_ref.get("modelName") or row.get("modelName") or "")
            supports_detail, supports_attachments = self._model_capabilities(validated["scope"], model_name)
            candidate_count += 1
            if validated["requireDetail"] and not supports_detail:
                continue
            summary = self._strip_html(
                self._search_row_value(row, "content")
                or self._search_row_value(row, "fdDescription")
                or self._search_row_value(row, "fullText")
            )[:300]
            read_count = self._to_int(self._search_row_value(row, "docReadCount") or self._search_row_value(row, "readCount"))
            items.append(
                {
                    "recordRef": record_ref,
                    "fdId": record_ref["recordId"],
                    "type": self._search_result_type(row, title),
                    "title": title,
                    "normalizedTitle": normalized_title,
                    "summary": summary,
                    "creator": self._strip_html(self._search_row_value(row, "creator")),
                    "createTime": self._search_row_value(row, "createTime"),
                    "readCount": read_count,
                    "modelTitle": self._search_row_value(row, "modelTitle") or self._search_row_value(row, "modelName2"),
                    "matchedExactTitle": matched_exact,
                    "matchedContainsTitle": matched_contains,
                    "attachmentCount": 0,
                    "attachmentTitles": [],
                    "supportsDetail": supports_detail,
                    "supportsAttachments": supports_attachments,
                    "detailAvailable": supports_detail,
                    "detailAction": self._detail_action(record_ref) if supports_detail else None,
                }
            )
        original_item_count = len(items)
        if validated["dedupByDocument"]:
            items = self._dedup_search_items(items)
        total = self._to_int(query_page.get("totalrows") if isinstance(query_page, dict) else None)
        return {
            "query": validated["query"],
            "normalizedQuery": validated["normalizedQuery"],
            "matchMode": validated["matchMode"],
            "dedupByDocument": validated["dedupByDocument"],
            "requireDetail": validated["requireDetail"],
            "items": items,
            "page": validated["page"],
            "pageSize": validated["pageSize"],
            "total": total if total is not None else len(items),
            "candidateCount": candidate_count,
            "detailFilteredCount": candidate_count - original_item_count,
            "filteredCount": original_item_count,
            "returnedCount": len(items),
            "totalNote": "total 以 OA 搜索接口返回为准；items 已按本地 matchMode 和 dedupByDocument 处理",
        }

    def _bool_param(self, value: Any, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off", ""):
            return False
        raise OAConnectorError("布尔参数格式不正确")

    def _normalize_search_text(self, value: Any) -> str:
        return re.sub(r"\s+", "", self._strip_html(str(value or "")))

    def _detail_action(self, record_ref: Dict[str, str]) -> Dict[str, Any]:
        return {
            "tool": "oa_get_object_detail",
            "arguments": {"recordRef": record_ref},
        }

    def _search_result_type(self, row: Dict[str, Any], title: str) -> str:
        if self._search_row_value(row, "fileName"):
            return "attachment"
        if ATTACHMENT_TITLE_RE.search(title or ""):
            return "attachment"
        return "document"

    def _dedup_search_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for item in items:
            fd_id = str(item.get("fdId") or "")
            if not fd_id:
                continue
            if fd_id not in grouped:
                grouped[fd_id] = {"best": item, "attachments": [], "seenAttachments": set()}
                order.append(fd_id)
            group = grouped[fd_id]
            if self._search_item_better(item, group["best"]):
                group["best"] = item
            if item.get("type") == "attachment":
                title = str(item.get("title") or "").strip()
                if title and title not in group["seenAttachments"]:
                    group["seenAttachments"].add(title)
                    group["attachments"].append(title)

        deduped: List[Dict[str, Any]] = []
        for fd_id in order:
            group = grouped[fd_id]
            item = dict(group["best"])
            item["type"] = "document"
            item["attachmentCount"] = len(group["attachments"])
            item["attachmentTitles"] = list(group["attachments"])
            deduped.append(item)
        return deduped

    def _search_item_better(self, candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
        def score(item: Dict[str, Any]) -> tuple[int, int, int, int]:
            return (
                1 if item.get("type") == "document" else 0,
                1 if item.get("matchedExactTitle") else 0,
                1 if item.get("matchedContainsTitle") else 0,
                -len(str(item.get("normalizedTitle") or "")),
            )

        return score(candidate) > score(current)

    def _record_ref_from_search_row(self, row: Dict[str, Any], scope: str) -> Optional[Dict[str, str]]:
        model_name = self._search_row_value(row, "modelName")
        path = self._search_row_value(row, "linkStr")
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0].split("/"):
            return None
        match = re.search(r"(?:fdId=|fdId/)([0-9a-fA-F]{24,40})", path)
        record_id = match.group(1) if match else ""
        if not record_id:
            doc_key = self._search_row_value(row, "docKey")
            ids = re.findall(r"[0-9a-fA-F]{24,40}", doc_key)
            record_id = ids[0] if ids else ""
        if not record_id:
            return None
        return {"scope": scope, "modelName": model_name, "recordId": record_id, "path": path}

    def _search_row_value(self, row: Dict[str, Any], key: str) -> str:
        for source in (row, row.get("lksFieldsMap") if isinstance(row.get("lksFieldsMap"), dict) else {}):
            if not isinstance(source, dict) or key not in source:
                continue
            value = source.get(key)
            if isinstance(value, dict):
                value = value.get("value")
            if value is not None:
                return unescape(str(value))
        return ""

    def _model_capabilities(self, scope: str, model_name: str) -> tuple[bool, bool]:
        config = self._scope_config(scope)
        if config.get("detailParser") == "kms_multidoc_knowledge":
            allowed = config.get("allowedModelNames", [])
            if model_name in allowed or model_name.endswith("KmsMultidocKnowledge"):
                return True, True
        if model_name.endswith("KmsMultidocKnowledge"):
            return True, True
        return False, False

    def _clean_search_title(self, value: Any) -> str:
        return self._strip_html(str(value or ""))

    def get_object_detail(
        self,
        record_ref: Optional[Dict[str, Any]] = None,
        include_text: bool = True,
        text_limit: int = SEARCH_LIMITS["detailTextLimitDefault"],
        fields: Optional[List[str]] = None,
        fd_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        del fields
        if record_ref is None and fd_id:
            record_ref = {
                "scope": "knowledge",
                "modelName": "KmsMultidocKnowledge",
                "recordId": fd_id,
                "path": f"/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId={urllib.parse.quote(fd_id)}",
            }
        if record_ref is None:
            raise OAConnectorError("缺少 recordRef")
        ref = self._validate_record_ref(record_ref)
        if text_limit < 0 or text_limit > SEARCH_LIMITS["detailTextLimitMax"]:
            raise OAConnectorError("textLimit 超过允许范围")
        supports_detail, supports_attachments = self._model_capabilities(ref["scope"], ref["modelName"])
        if not supports_detail:
            raise OAConnectorError("该模块不支持详情解析")

        response = self._request(ref["path"])
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能查看 OA 内容")
        title = self._extract_title(response["text"])
        text = ""
        warning = ""
        if include_text:
            text, warning = self._extract_detail_text(response["text"], text_limit)
        attachments = self._parse_knowledge_attachments(response["text"]) if supports_attachments else []
        return {
            "recordRef": ref,
            "title": title,
            "text": text,
            "textExtractionWarning": warning,
            "attachments": attachments,
        }

    def _validate_record_ref(self, record_ref: Dict[str, Any]) -> Dict[str, str]:
        scope = str(record_ref.get("scope") or "")
        model_name = str(record_ref.get("modelName") or "")
        record_id = str(record_ref.get("recordId") or "")
        path = str(record_ref.get("path") or "")
        self._scope_config(scope)
        self._normalize_model_name(scope, model_name)
        if not record_id:
            raise OAConnectorError("recordRef 无效")
        if not path.startswith("/") or path.startswith("//"):
            raise OAConnectorError("recordRef 无效")
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc:
            raise OAConnectorError("recordRef 无效")
        if ".." in [part for part in parsed.path.split("/") if part]:
            raise OAConnectorError("recordRef 无效")
        if record_id not in path:
            raise OAConnectorError("recordRef 无效")
        return {"scope": scope, "modelName": model_name, "recordId": record_id, "path": path}

    def _extract_detail_text(self, html_text: str, text_limit: int) -> tuple[str, str]:
        cleaned = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
        cleaned = re.sub(r"<style\b.*?</style>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<input\b[^>]*type=[\"']?hidden[\"']?[^>]*>", " ", cleaned, flags=re.I | re.S)
        warning = ""
        match = re.search(
            r"<div[^>]+(?:id|class)=[\"'][^\"']*(?:docContent|fdContent|content|mainContent)[^\"']*[\"'][^>]*>(.*?)</div>",
            cleaned,
            flags=re.I | re.S,
        )
        if match:
            source = match.group(1)
        else:
            source = cleaned
            warning = "未识别到模块正文容器，已使用严格截断的页面文本"
        text = self._strip_html(source)
        return text[:text_limit], warning

    def _parse_knowledge_attachments(self, html_text: str) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        pattern = re.compile(r"attachmentObject_attachment\.addDoc\((.*?)\)", re.I | re.S)
        for match in pattern.finditer(html_text):
            args = self._parse_js_string_args(match.group(1))
            if len(args) < 3:
                continue
            if len(args) >= 4 and self._looks_like_filename(args[0]) and self._looks_like_mime_type(args[2]):
                name = args[0]
                attachment_id = args[1]
                file_id = args[4] if len(args) > 4 and re.fullmatch(r"[0-9a-fA-F]{24,40}", args[4]) else ""
                mime_type = args[2]
                size = self._to_int(args[3])
            else:
                attachment_id = args[0]
                file_id = args[1] if len(args) > 1 else ""
                name = args[2] if len(args) > 2 else ""
                mime_type = args[3] if len(args) > 3 else ""
                size = self._to_int(args[4]) if len(args) > 4 else None
            attachments.append(
                {
                    "index": len(attachments) + 1,
                    "name": name,
                    "attachmentId": attachment_id,
                    "fileId": file_id,
                    "mimeType": mime_type,
                    "size": size,
                    "downloadable": bool(attachment_id or file_id),
                }
            )
        return attachments

    def _looks_like_filename(self, value: str) -> bool:
        return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", value or ""))

    def _looks_like_mime_type(self, value: str) -> bool:
        return bool(re.match(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$", value or ""))

    def _parse_js_string_args(self, text: str) -> List[str]:
        values: List[str] = []
        current: List[str] = []
        quote: Optional[str] = None
        escaped = False
        for ch in text:
            if quote:
                if escaped:
                    current.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    raw = "".join(current)
                    try:
                        values.append(raw.encode("raw_unicode_escape").decode("unicode_escape"))
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        values.append(raw)
                    current = []
                    quote = None
                else:
                    current.append(ch)
            elif ch in ("'", '"'):
                quote = ch
        return values

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            try:
                return int(float(str(value).strip()))
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

    def _request_bytes(
        self,
        path: str,
        method: str = "GET",
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """返回原始 bytes，不做 decode；用于二进制附件下载。"""
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
                return {"url": resp.geturl(), "bytes": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise OAConnectorError(f"HTTP {exc.code}: {raw[:500]}") from exc
        except urllib.error.URLError as exc:
            raise OAConnectorError(f"请求 OA 失败: {exc}") from exc

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
        return ("j_acegi_security_check" in lowered) or ("j_username" in lowered and "j_password" in lowered)

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

    def _safe_filename(self, name: str) -> str:
        name = str(name or "").replace("\\", "/")
        name = Path(name).name
        name = re.sub(r"[\x00-\x1f\x7f/:*?\"<>|]+", "_", name)
        name = name.replace("..", "_").strip(" .")
        if not name:
            name = "attachment"
        if len(name) > 180:
            stem = Path(name).stem[:120]
            suffix = Path(name).suffix[:20]
            name = f"{stem}{suffix}" if suffix else stem
        return name

    def _unique_output_path(self, output_dir: Path, filename: str, overwrite: bool) -> Path:
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        candidate = (output_dir / filename).resolve()
        if output_dir not in candidate.parents and candidate != output_dir:
            raise OAConnectorError("保存附件失败：文件路径不安全")
        if overwrite or not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        counter = 1
        while True:
            next_candidate = (output_dir / f"{stem} ({counter}){suffix}").resolve()
            if not next_candidate.exists():
                return next_candidate
            counter += 1

    def batch_search_objects(self, queries: List[str], **kwargs: Any) -> Dict[str, Any]:
        if not isinstance(queries, list) or not queries:
            raise OAConnectorError("queries 不能为空")
        if len(queries) > SEARCH_LIMITS["batchQueriesMax"]:
            raise OAConnectorError("queries 数量超过允许范围")
        include_details = bool(kwargs.get("includeDetails", False))
        include_attachments = bool(kwargs.get("includeAttachments", False))
        download_first = bool(kwargs.get("downloadFirstAttachment", False))
        max_details = int(kwargs.get("maxDetailsPerQuery") or SEARCH_LIMITS["maxDetailsPerQueryDefault"])
        if max_details < 1 or max_details > SEARCH_LIMITS["maxDetailsPerQueryMax"]:
            raise OAConnectorError("maxDetailsPerQuery 超过允许范围")
        max_downloads = int(kwargs.get("maxDownloads") or SEARCH_LIMITS["batchMaxDownloadsDefault"])
        if max_downloads < 0 or max_downloads > SEARCH_LIMITS["batchMaxDownloadsMax"]:
            raise OAConnectorError("maxDownloads 超过允许范围")

        # 显式校验批量模式下的 pageSize
        raw_page_size = kwargs.get("pageSize")
        if raw_page_size is not None:
            ps = int(raw_page_size)
            if ps < 1 or ps > SEARCH_LIMITS["batchPageSizeMax"]:
                raise OAConnectorError("pageSize 超过允许范围")

        items: List[Dict[str, Any]] = []
        matched_queries = 0
        errors = 0
        downloads = 0
        for query in queries:
            item: Dict[str, Any] = {"query": query, "matched": False, "resultCount": 0, "results": [], "error": None}
            try:
                search_kwargs = dict(kwargs)
                search_kwargs.pop("includeDetails", None)
                search_kwargs.pop("includeAttachments", None)
                search_kwargs.pop("downloadFirstAttachment", None)
                search_kwargs.pop("maxDetailsPerQuery", None)
                search_kwargs.pop("maxDownloads", None)
                search_kwargs.pop("outputDir", None)
                search_kwargs["query"] = query
                search_kwargs["pageSize"] = int(kwargs.get("pageSize") or SEARCH_LIMITS["batchPageSizeDefault"])
                result = self.search_objects(**search_kwargs)
                results = result.get("items", [])
                item["matched"] = bool(results)
                item["resultCount"] = len(results)
                if results:
                    matched_queries += 1
                for result_item in results[: max_details if (include_details or include_attachments or download_first) else len(results)]:
                    compact: Dict[str, Any] = {
                        "recordRef": result_item.get("recordRef"),
                        "type": result_item.get("type"),
                        "title": result_item.get("title"),
                        "normalizedTitle": result_item.get("normalizedTitle"),
                        "matchedExactTitle": result_item.get("matchedExactTitle", False),
                        "matchedContainsTitle": result_item.get("matchedContainsTitle", False),
                        "attachmentCount": result_item.get("attachmentCount", 0),
                        "attachmentTitles": result_item.get("attachmentTitles", []),
                        "detailAvailable": result_item.get("detailAvailable", False),
                        "detailAction": result_item.get("detailAction"),
                        "attachments": [],
                        "downloaded": [],
                    }
                    if include_details or include_attachments or download_first:
                        detail = self.get_object_detail(record_ref=result_item["recordRef"], include_text=include_details)
                        if include_details:
                            compact["text"] = detail.get("text", "")
                            compact["textExtractionWarning"] = detail.get("textExtractionWarning", "")
                        if include_attachments or download_first:
                            compact["attachments"] = detail.get("attachments", [])
                    if download_first and compact["attachments"]:
                        exact_download_allowed = (
                            str(kwargs.get("matchMode") or "") == "exact"
                            or self._bool_param(kwargs.get("onlyExactTitle"), False)
                        )
                        if (
                            not exact_download_allowed
                            or not compact.get("matchedExactTitle")
                        ):
                            pass
                        elif max_downloads <= 0 or downloads >= max_downloads:
                            pass
                        else:
                            downloaded = self.download_attachment(
                                result_item["recordRef"],
                                attachment_index=int(compact["attachments"][0]["index"]),
                                output_dir=str(kwargs.get("outputDir") or "~/Downloads/oa-attachments"),
                                overwrite=bool(kwargs.get("overwrite", False)),
                                max_bytes=int(kwargs.get("maxBytes") or SEARCH_LIMITS["downloadMaxBytesDefault"]),
                            )
                            compact["downloaded"].append(downloaded)
                            downloads += 1
                    item["results"].append(compact)
            except Exception as exc:
                errors += 1
                item["error"] = self._sanitize_error(exc)
            items.append(item)
        return {
            "items": items,
            "summary": {
                "totalQueries": len(queries),
                "matchedQueries": matched_queries,
                "errors": errors,
                "downloads": downloads,
            },
        }

    def _sanitize_error(self, exc: Exception) -> str:
        text = str(exc)
        text = re.sub(
            r"(?i)(?:cookie|set-cookie|jsessionid|authorization|password|j_password)\s*[:=][^\s,;]+",
            "[redacted]",
            text,
        )
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:200]

    def download_attachment(
        self,
        record_ref: Optional[Dict[str, Any]],
        attachment_index: int,
        output_dir: str,
        overwrite: bool = False,
        max_bytes: int = SEARCH_LIMITS["downloadMaxBytesDefault"],
        fd_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if max_bytes < 1 or max_bytes > SEARCH_LIMITS["downloadMaxBytesDefault"]:
            raise OAConnectorError("附件超过下载大小上限")
        detail = self.get_object_detail(record_ref=record_ref, include_text=False, fd_id=fd_id)
        attachments = detail.get("attachments") or []
        selected = None
        for attachment in attachments:
            if int(attachment.get("index") or 0) == int(attachment_index):
                selected = attachment
                break
        if selected is None:
            raise OAConnectorError("附件序号不存在")
        if selected.get("size") is not None and int(selected["size"]) > max_bytes:
            raise OAConnectorError("附件超过下载大小上限")

        filename = self._safe_filename(str(selected.get("name") or "attachment"))
        output_path = self._unique_output_path(Path(output_dir), filename, overwrite)
        download_path = self._attachment_download_path(detail["recordRef"], selected)
        response = self._request_bytes(download_path)
        raw = response["bytes"]
        # 登录页/HTML 检测：检查前 512 字节
        head = raw[:512]
        if self._looks_like_login_page(response["url"], head.decode("utf-8", errors="replace")):
            raise OAConnectorError("下载附件失败，当前会话可能失效或无权限")
        if b"<html" in head.lower():
            raise OAConnectorError("下载附件失败，当前会话可能失效或无权限")
        if len(raw) > max_bytes:
            raise OAConnectorError("附件超过下载大小上限")

        temp_path = output_path.with_name(f".{output_path.name}.tmp")
        try:
            with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False) as temp_file:
                temp_file.write(raw)
                temp_path = Path(temp_file.name)
            temp_path.replace(output_path)
        except OSError as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise OAConnectorError(f"保存附件失败: {str(exc)[:120]}") from exc

        return {
            "ok": True,
            "recordRef": detail["recordRef"],
            "attachment": {
                "index": selected.get("index"),
                "name": selected.get("name"),
                "attachmentId": selected.get("attachmentId"),
                "mimeType": selected.get("mimeType"),
                "size": selected.get("size"),
            },
            "savedPath": str(output_path),
            "bytes": len(raw),
        }

    def _attachment_download_path(self, record_ref: Dict[str, str], attachment: Dict[str, Any]) -> str:
        attachment_id = urllib.parse.quote(str(attachment.get("attachmentId") or ""))
        file_id = urllib.parse.quote(str(attachment.get("fileId") or ""))
        if not attachment_id and not file_id:
            raise OAConnectorError("附件序号不存在")
        query = urllib.parse.urlencode({"method": "download", "fdId": attachment_id, "fileId": file_id, "modelId": record_ref["recordId"]})
        return f"/sys/attachment/sys_att_main/sysAttMain.do?{query}"
