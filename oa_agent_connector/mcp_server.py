from __future__ import annotations

import hashlib
import re
import json
import os
import secrets
import sys
import time
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional

from .client import OAClient, OAConnectorError


SERVER_NAME = "oa-agent-connector"
SERVER_VERSION = "0.2.3"


def _state_dir() -> Path:
    configured = os.getenv("OA_AGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".oa-agent-connector"


def _safe_session_name(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))[:32]
    return f"{cleaned or 'session'}-{digest}"


def _session_paths(session: str) -> Dict[str, Path]:
    root = _state_dir()
    safe = _safe_session_name(session)
    return {
        "root": root,
        "cookie": root / f"{safe}.cookies",
        "meta": root / f"{safe}.json",
    }


def _pending_dir() -> Path:
    return _state_dir() / "pending-approvals"


def _pending_path(token: str) -> Path:
    safe = "".join(ch for ch in token if ch.isalnum() or ch in ("-", "_"))
    return _pending_dir() / f"{safe}.json"


def _save_session(session: str, base_url: str) -> None:
    paths = _session_paths(session)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["meta"].write_text(json.dumps({"baseUrl": base_url}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        paths["meta"].chmod(0o600)
    except OSError:
        pass


def _load_base_url(session: str, base_url: Optional[str]) -> str:
    if base_url:
        return base_url
    env_base = os.getenv("OA_BASE_URL")
    if env_base:
        return env_base
    paths = _session_paths(session)
    if paths["meta"].exists():
        return json.loads(paths["meta"].read_text(encoding="utf-8"))["baseUrl"]
    raise OAConnectorError("缺少 baseUrl：请先调用 oa_login，或传入 baseUrl/OA_BASE_URL")


def _client(session: str, base_url: Optional[str] = None, insecure: bool = False) -> OAClient:
    resolved_base_url = _load_base_url(session, base_url)
    paths = _session_paths(session)
    return OAClient(
        resolved_base_url,
        cookie_file=str(paths["cookie"]),
        verify_tls=not insecure,
    )


def _absolute_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(str(base_url).rstrip("/") + "/", str(path or "").lstrip("/"))


def _ok(data: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def _mcp_error(data: Any) -> Dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
    }


def _setup_guide(reason: str = "") -> Dict[str, Any]:
    example_base_url = os.getenv("OA_BASE_URL") or "<OA_BASE_URL>"
    example_state_dir = os.getenv("OA_AGENT_STATE_DIR") or str(_state_dir())
    return {
        "ok": False,
        "reason": reason,
        "guide": [
            {
                "step": 1,
                "title": "确认 MCP 已配置",
                "description": "如果客户端根本没有 oa / oa-agent-mcp 这个 MCP，先在 MCP 客户端配置里添加它。OA_AGENT_STATE_DIR 必须是这台电脑上的真实绝对路径，用来保存登录状态和审批确认状态。",
                "exampleConfig": {
                    "mcpServers": {
                        "oa": {
                            "command": "oa-agent-mcp",
                            "env": {
                                "OA_BASE_URL": example_base_url,
                                "OA_AGENT_STATE_DIR": example_state_dir,
                            },
                        }
                    }
                },
            },
            {
                "step": 2,
                "title": "授权登录",
                "description": "调用 oa_login，使用用户自己的 OA 账号密码登录。密码不会保存，只保存登录 cookie。",
                "tool": "oa_login",
                "arguments": {
                    "baseUrl": example_base_url,
                    "username": "用户自己的 OA 账号",
                    "password": "用户自己的 OA 密码",
                    "session": "default",
                },
            },
            {
                "step": 3,
                "title": "查询待办",
                "description": "授权成功后调用 oa_list_todos，只会返回当前登录账号有权限看到的待审批清单。",
                "tool": "oa_list_todos",
                "arguments": {"session": "default", "page": 1, "pageSize": 20},
            },
        ],
    }


def _redact_tool_message(message: str) -> str:
    text = str(message or "")
    # Split on semicolons to handle multi-value headers like "Cookie: a=1; b=2"
    parts = re.split(r";", text)
    for i, part in enumerate(parts):
        part = re.sub(
            r"(?i)(cookie|set-cookie|jsessionid|authorization|password|j_password)\s*[:=]\s*\S.*",
            r"\1=[redacted]",
            part,
        )
        parts[i] = part
    text = ";".join(parts)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _tool_error(message: str) -> Dict[str, Any]:
    return _mcp_error(_setup_guide(_redact_tool_message(message)))


def _plain_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _approval_action_label(action: str) -> str:
    if action == "approve":
        return "同意"
    if action == "reject":
        return "驳回"
    raise OAConnectorError("审批动作只允许 approve 或 reject")


def _approval_confirm_phrase(action: str) -> str:
    return "确认审批" if action == "approve" else "确认驳回"


def _find_current_todo(client: OAClient, fd_id: str) -> Dict[str, Any]:
    for todo in client.list_todos(page=1, page_size=200):
        if todo.fd_id == fd_id:
            return todo.to_dict()
    raise OAConnectorError(f"拒绝操作：{fd_id} 不在当前登录账号的待审批列表中")


def _save_pending_approval(data: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    data = dict(data)
    data["confirmationToken"] = token
    data["createdAt"] = int(time.time())
    data["expiresAt"] = data["createdAt"] + 900
    pending_dir = _pending_dir()
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = _pending_path(token)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _load_pending_approval(token: str) -> Dict[str, Any]:
    path = _pending_path(token)
    if not path.exists():
        raise OAConnectorError("确认 token 不存在或已使用，请重新准备审批")
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(time.time()) > int(data.get("expiresAt") or 0):
        try:
            path.unlink()
        except OSError:
            pass
        raise OAConnectorError("确认 token 已过期，请重新准备审批")
    return data


def _delete_pending_approval(token: str) -> None:
    try:
        _pending_path(token).unlink()
    except OSError:
        pass


def _tool_schema(name: str, description: str, properties: Dict[str, Any], required: Optional[list[str]] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool_schema(
        "oa_setup_guide",
        "当用户想查看 OA 待办但 MCP 未配置、未授权或授权过期时，返回分步配置和授权指引。",
        {
            "reason": {"type": "string", "description": "触发指引的原因，可选"},
        },
    ),
    _tool_schema(
        "oa_login",
        "登录 OA 并保存当前用户会话 cookie。不会保存密码。",
        {
            "baseUrl": {"type": "string", "description": "OA 根地址，例如 https://example.com/oa/"},
            "username": {"type": "string", "description": "OA 登录账号"},
            "password": {"type": "string", "description": "OA 登录密码"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean", "description": "HTTPS 证书不校验，默认 false"},
        },
        ["baseUrl", "username", "password"],
    ),
    _tool_schema(
        "oa_auth_status",
        "检查指定会话是否仍可访问 OA 待办数据源。",
        {
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
    ),
    _tool_schema(
        "oa_list_todos",
        "查询当前登录账号有权限看到的待审批清单。",
        {
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "page": {"type": "integer", "minimum": 1, "default": 1},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
            "insecure": {"type": "boolean"},
        },
    ),
    _tool_schema(
        "oa_get_detail",
        "查看待审批单据详情。默认要求 fdId 必须在当前登录账号待办清单中。",
        {
            "fdId": {"type": "string"},
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "allowNonTodo": {"type": "boolean", "description": "是否允许查看非当前待办，默认 false"},
            "insecure": {"type": "boolean"},
        },
        ["fdId"],
    ),
    _tool_schema(
        "oa_prepare_approval",
        "准备审批动作：校验当前账号待办权限，整理单据、动作和备注，生成待用户确认的摘要和 confirmationToken。不提交审批。",
        {
            "fdId": {"type": "string"},
            "action": {"type": "string", "enum": ["approve", "reject"], "description": "approve=同意，reject=驳回"},
            "note": {"type": "string", "description": "审批备注/意见"},
            "futureNodeId": {"type": "string", "description": "可选，人工决策下一节点，仅同意时使用"},
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
        ["fdId", "action", "note"],
    ),
    _tool_schema(
        "oa_confirm_approval",
        "用户确认后执行审批。必须传 oa_prepare_approval 返回的 confirmationToken，并传确认文本：同意用“确认审批”，驳回用“确认驳回”。执行前会再次校验当前账号待办权限。",
        {
            "confirmationToken": {"type": "string"},
            "confirmationText": {"type": "string", "description": "同意填确认审批，驳回填确认驳回"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
        ["confirmationToken", "confirmationText"],
    ),
    _tool_schema(
        "oa_approve",
        "同意审批 dry-run 兼容工具。MCP 禁止通过本工具直接 execute=true；正式执行请使用 oa_prepare_approval -> 用户确认 -> oa_confirm_approval。",
        {
            "fdId": {"type": "string"},
            "note": {"type": "string"},
            "execute": {"type": "boolean", "default": False},
            "futureNodeId": {"type": "string"},
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
        ["fdId", "note"],
    ),
    _tool_schema(
        "oa_reject",
        "驳回审批 dry-run 兼容工具。MCP 禁止通过本工具直接 execute=true；正式执行请使用 oa_prepare_approval -> 用户确认 -> oa_confirm_approval。",
        {
            "fdId": {"type": "string"},
            "note": {"type": "string"},
            "execute": {"type": "boolean", "default": False},
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
        ["fdId", "note"],
    ),
    _tool_schema(
        "oa_get_search_schema",
        "返回当前 MCP 支持的 OA 搜索范围、字段枚举、排序、文件类型和限制规则。",
        {
            "scope": {"type": "string", "description": "搜索范围，默认 all，可选 knowledge"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_search_objects",
        "执行 OA 通用只读搜索，返回结构化结果和受控 recordRef。常用：scope 可选 all/knowledge/news；searchFields 可选 title/content/fdDescription/creator/attachment；matchMode 可选 keyword/contains/exact，contains/exact 会自动忽略标题里的空白；默认 requireDetail=true，只返回可继续查看详情的结果；默认 dedupByDocument=true，按文档去重。",
        {
            "query": {"type": "string"},
            "scope": {"type": "string", "enum": ["all", "knowledge", "news"]},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "matchMode": {"type": "string", "enum": ["keyword", "contains", "exact"]},
            "requireDetail": {"type": "boolean", "description": "默认 true，只返回可用 oa_get_object_detail 查看详情的结果"},
            "dedupByDocument": {"type": "boolean", "description": "默认 true，按 fdId 聚合搜索结果，减少附件级重复条目"},
            "searchFields": {"type": "array", "items": {"type": "string", "enum": ["title", "content", "fdDescription", "creator", "attachment"]}},
            "category": {"type": "string"},
            "docStatus": {"type": "string"},
            "docFileType": {"type": "string", "enum": ["", "pdf", "doc;docx", "xls;xlsx", "ppt;pptx", "txt"]},
            "outKeyword": {"type": "string"},
            "timeRange": {"type": "string", "enum": ["", "day", "week", "month", "year"]},
            "fromCreateTime": {"type": "string"},
            "toCreateTime": {"type": "string"},
            "sortType": {"type": "string", "enum": ["relevance", "readCount", "time"]},
            "sortOrder": {"type": "string", "enum": ["asc", "desc"]},
            "exactTitle": {"type": "boolean", "description": "兼容旧参数；建议改用 matchMode"},
            "onlyExactTitle": {"type": "boolean", "description": "兼容旧参数；true 等价于 matchMode=exact"},
            "page": {"type": "integer", "minimum": 1},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 50},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["query"],
    ),
    _tool_schema(
        "oa_get_object_detail",
        "按搜索结果返回的 recordRef 读取 OA 对象详情和附件元数据。",
        {
            "recordRef": {"type": "object"},
            "fdId": {"type": "string", "description": "兼容便捷参数，仅默认知识文档解析器使用"},
            "includeText": {"type": "boolean"},
            "textLimit": {"type": "integer", "minimum": 0, "maximum": 20000},
            "fields": {"type": "array", "items": {"type": "string"}},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_download_attachment",
        "按 recordRef + attachmentIndex 下载当前详情页中可见附件到本地安全目录。",
        {
            "recordRef": {"type": "object"},
            "fdId": {"type": "string", "description": "兼容便捷参数，仅默认知识文档解析器使用"},
            "attachmentIndex": {"type": "integer", "minimum": 1},
            "outputDir": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "maxBytes": {"type": "integer", "minimum": 1, "maximum": 52428800},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["attachmentIndex", "outputDir"],
    ),
    _tool_schema(
        "oa_batch_search_objects",
        "批量执行通用 OA 搜索，输入为 queries 数组，可选列附件或受限下载。支持 matchMode keyword/contains/exact；默认 requireDetail=true；默认 dedupByDocument=true。",
        {
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "scope": {"type": "string", "enum": ["all", "knowledge", "news"]},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "matchMode": {"type": "string", "enum": ["keyword", "contains", "exact"]},
            "requireDetail": {"type": "boolean", "description": "默认 true，只返回可继续查看详情的结果"},
            "dedupByDocument": {"type": "boolean", "description": "默认 true，按 fdId 聚合搜索结果"},
            "searchFields": {"type": "array", "items": {"type": "string", "enum": ["title", "content", "fdDescription", "creator", "attachment"]}},
            "sortType": {"type": "string", "enum": ["relevance", "readCount", "time"]},
            "sortOrder": {"type": "string", "enum": ["asc", "desc"]},
            "docFileType": {"type": "string", "enum": ["", "pdf", "doc;docx", "xls;xlsx", "ppt;pptx", "txt"]},
            "exactTitle": {"type": "boolean", "description": "兼容旧参数；建议改用 matchMode"},
            "onlyExactTitle": {"type": "boolean", "description": "兼容旧参数；true 等价于 matchMode=exact"},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "includeDetails": {"type": "boolean"},
            "includeAttachments": {"type": "boolean"},
            "maxDetailsPerQuery": {"type": "integer", "minimum": 1, "maximum": 3},
            "downloadFirstAttachment": {"type": "boolean"},
            "maxDownloads": {"type": "integer", "minimum": 0, "maximum": 50},
            "outputDir": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "maxBytes": {"type": "integer", "minimum": 1, "maximum": 52428800},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["queries"],
    ),
]


def _session(args: Dict[str, Any]) -> str:
    return str(args.get("session") or "default")


def _bool(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    return bool(value)



_NEW_SEARCH_TOOL_NAMES = frozenset({
    "oa_get_search_schema",
    "oa_search_objects",
    "oa_get_object_detail",
    "oa_download_attachment",
    "oa_batch_search_objects",
})

_BYPASS_PARAM_KEYS = frozenset({
    "baseUrl",
    "insecure",
    "extraParams",
    "attachmentUrl",
    "fileId",
    "attachmentId",
})


def _reject_bypass_params(tool_name: str, args: Dict[str, Any]) -> None:
    """Reject bypass parameters for new search tools."""
    injected = _BYPASS_PARAM_KEYS & set(args.keys())
    if injected:
        raise OAConnectorError(
            "工具 %s 不接受参数: %s" % (tool_name, ", ".join(sorted(injected)))
        )



def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    session = _session(args)
    insecure = _bool(args, "insecure")

    if name == "oa_setup_guide":
        return _ok(_setup_guide(str(args.get("reason") or "")))

    if name == "oa_login":
        base_url = str(args["baseUrl"])
        client = OAClient(base_url, cookie_file=str(_session_paths(session)["cookie"]), verify_tls=not insecure)
        client.login(str(args["username"]), str(args["password"]))
        _save_session(session, base_url)
        return _ok({"ok": True, "session": session, "baseUrl": base_url})

    if name == "oa_confirm_approval":
        token = str(args["confirmationToken"])
        pending = _load_pending_approval(token)
        expected = _approval_confirm_phrase(str(pending["action"]))
        confirmation_text = str(args["confirmationText"]).strip()
        if confirmation_text != expected:
            raise OAConnectorError(f"确认文本不匹配：需要用户明确发送“{expected}”")
        confirm_client = _client(
            session=str(pending["session"]),
            base_url=str(pending["baseUrl"]),
            insecure=bool(pending.get("insecure")),
        )
        _find_current_todo(confirm_client, str(pending["fdId"]))
        if pending["action"] == "approve":
            result = confirm_client.approve(
                str(pending["fdId"]),
                str(pending["note"]),
                execute=True,
                future_node_id=pending.get("futureNodeId"),
            )
        else:
            result = confirm_client.reject(str(pending["fdId"]), str(pending["note"]), execute=True)
        _delete_pending_approval(token)
        return _ok({"ok": True, "executed": True, "action": pending["action"], "fdId": pending["fdId"], "result": result})

    # New search tools: create client with session only, reject bypass params
    if name in _NEW_SEARCH_TOOL_NAMES:
        _reject_bypass_params(name, args)
        client = _client(session=session)

        if name == "oa_get_search_schema":
            return _ok(client.get_search_schema(str(args.get("scope") or "all")))
        if name == "oa_search_objects":
            return _ok(
                client.search_objects(
                    query=str(args["query"]),
                    scope=args.get("scope") or "all",
                    modelName=args.get("modelName"),
                    bond=args.get("bond") or "or",
                    matchMode=args.get("matchMode") or "",
                    requireDetail=_bool(args, "requireDetail", True),
                    dedupByDocument=_bool(args, "dedupByDocument", True),
                    searchFields=args.get("searchFields") or [],
                    category=args.get("category") or "",
                    docStatus=args.get("docStatus") or "",
                    docFileType=args.get("docFileType") or "",
                    outKeyword=args.get("outKeyword") or "",
                    timeRange=args.get("timeRange") or "",
                    fromCreateTime=args.get("fromCreateTime") or "",
                    toCreateTime=args.get("toCreateTime") or "",
                    sortType=args.get("sortType") or "relevance",
                    sortOrder=args.get("sortOrder") or "desc",
                    exactTitle=_bool(args, "exactTitle"),
                    onlyExactTitle=_bool(args, "onlyExactTitle"),
                    page=int(args.get("page") or 1),
                    pageSize=int(args.get("pageSize") or 20),
                )
            )
        if name == "oa_get_object_detail":
            return _ok(
                client.get_object_detail(
                    record_ref=args.get("recordRef"),
                    include_text=_bool(args, "includeText", True),
                    text_limit=int(args.get("textLimit") or 12000),
                    fields=args.get("fields") or [],
                    fd_id=args.get("fdId"),
                )
            )
        if name == "oa_download_attachment":
            return _ok(
                client.download_attachment(
                    record_ref=args.get("recordRef"),
                    attachment_index=int(args["attachmentIndex"]),
                    output_dir=str(args["outputDir"]),
                    overwrite=_bool(args, "overwrite"),
                    max_bytes=int(args.get("maxBytes") or 52428800),
                    fd_id=args.get("fdId"),
                )
            )
        if name == "oa_batch_search_objects":
            batch_args = dict(args)
            queries = list(batch_args.pop("queries"))
            batch_args.pop("session", None)
            return _ok(client.batch_search_objects(queries=queries, **batch_args))

    # Legacy tools: allow baseUrl/insecure
    client = _client(session=session, base_url=args.get("baseUrl"), insecure=insecure)

    if name == "oa_auth_status":
        client.assert_logged_in()
        return _ok({"ok": True, "session": session, "baseUrl": client.base_url})
    if name == "oa_list_todos":
        page = int(args.get("page") or 1)
        page_size = int(args.get("pageSize") or 20)
        todos = []
        for todo in client.list_todos(page=page, page_size=page_size):
            data = todo.to_dict()
            if data.get("detailPath"):
                data["detailUrl"] = _absolute_url(client.base_url, str(data["detailPath"]))
            todos.append(data)
        return _ok({"items": todos, "page": page, "pageSize": page_size, "session": session})
    if name == "oa_get_detail":
        detail = client.get_detail(str(args["fdId"]), require_in_todo=not _bool(args, "allowNonTodo"))
        return _ok(detail)
    if name == "oa_prepare_approval":
        fd_id = str(args["fdId"])
        action = str(args["action"])
        note = str(args["note"]).strip()
        if not note:
            raise OAConnectorError("审批备注不能为空")
        action_label = _approval_action_label(action)
        todo = _find_current_todo(client, fd_id)
        detail = client.get_detail(fd_id, require_in_todo=True)
        raw = todo.get("raw") or {}
        base_url = str(args.get("baseUrl") or client.base_url)
        pending = {
            "session": session,
            "baseUrl": base_url,
            "insecure": insecure,
            "fdId": fd_id,
            "action": action,
            "note": note,
            "futureNodeId": args.get("futureNodeId"),
        }
        token = _save_pending_approval(pending)
        summary = {
            "ok": True,
            "requiresUserConfirmation": True,
            "confirmationToken": token,
            "confirmationPhrase": _approval_confirm_phrase(action),
            "summary": {
                "fdId": fd_id,
                "subject": todo.get("subject") or detail.get("title") or "",
                "action": action,
                "actionLabel": action_label,
                "note": note,
                "currentNode": _plain_text(raw.get("nodeName")),
                "currentHandler": _plain_text(raw.get("handlerName")),
                "detailTitle": detail.get("title", ""),
            },
            "permissionCheck": {
                "ok": True,
                "evidence": "该 fdId 存在于当前登录账号的 OA 待审批清单中；执行前会再次校验。",
            },
            "nextStep": f"请把 summary 整理给用户确认。用户明确回复“{_approval_confirm_phrase(action)}”后，调用 oa_confirm_approval。",
        }
        return _ok(summary)
    if name == "oa_approve":
        if _bool(args, "execute"):
            return _mcp_error(
                {
                    "ok": False,
                    "message": "MCP 禁止直接执行审批。请先调用 oa_prepare_approval 生成确认摘要，用户明确确认后再调用 oa_confirm_approval。",
                    "requiredFlow": ["oa_prepare_approval", "用户确认审批信息", "oa_confirm_approval"],
                }
            )
        result = client.approve(
            str(args["fdId"]),
            str(args["note"]),
            execute=_bool(args, "execute"),
            future_node_id=args.get("futureNodeId"),
        )
        return _ok(result)
    if name == "oa_reject":
        if _bool(args, "execute"):
            return _mcp_error(
                {
                    "ok": False,
                    "message": "MCP 禁止直接执行驳回。请先调用 oa_prepare_approval 生成确认摘要，用户明确确认后再调用 oa_confirm_approval。",
                    "requiredFlow": ["oa_prepare_approval", "用户确认审批信息", "oa_confirm_approval"],
                }
            )
        result = client.reject(str(args["fdId"]), str(args["note"]), execute=_bool(args, "execute"))
        return _ok(result)
    raise OAConnectorError(f"未知工具: {name}")


def _response(message_id: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:
        return None
    try:
        if method == "initialize":
            return _response(
                message_id,
                {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "tools/list":
            return _response(message_id, {"tools": TOOLS})
        if method == "tools/call":
            try:
                result = call_tool(str(params["name"]), dict(params.get("arguments") or {}))
            except Exception as exc:
                result = _tool_error(str(exc))
            return _response(message_id, result)
        return _response(message_id, error={"code": -32601, "message": f"Method not found: {method}"})
    except Exception as exc:
        return _response(message_id, error={"code": -32000, "message": str(exc)})


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle(message)
        except Exception as exc:
            response = _response(None, error={"code": -32700, "message": str(exc)})
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
