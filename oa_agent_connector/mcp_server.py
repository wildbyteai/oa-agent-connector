from __future__ import annotations

import hashlib
import re
import json
import os
import posixpath
import secrets
import sys
import time
import urllib.parse
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional

from .client import ApprovalResultUnknownError, ApprovalStateChangedError, OAClient, OAConnectorError
from .credential_store import CredentialStoreError, SystemCredentialStore
from .local_auth import begin_local_auth, read_local_auth_status, transport_security_issue
from .security import sanitize_error_message


SERVER_NAME = "oa-agent-connector"
SERVER_VERSION = "0.2.11"
MAX_BATCH_APPROVAL_ITEMS = 20
BATCH_APPROVAL_CONFIRM_PHRASE = "确认批量审批"
BATCH_TERMINAL_RESULT_TTL_SECONDS = 3600


def _state_dir() -> Path:
    configured = os.getenv("OA_AGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".oa-agent-connector"


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _password_login_enabled() -> bool:
    return _env_flag("OA_AGENT_ENABLE_PASSWORD_LOGIN")


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


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _saved_session_meta(session: str) -> Dict[str, Any]:
    meta = _session_paths(session)["meta"]
    if not meta.exists():
        return {}
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _saved_base_url(session: str) -> Optional[str]:
    value = _saved_session_meta(session).get("baseUrl")
    return str(value) if value else None


def _pending_dir() -> Path:
    return _state_dir() / "pending-approvals"


def _pending_path(token: str) -> Path:
    safe = "".join(ch for ch in token if ch.isalnum() or ch in ("-", "_"))
    return _pending_dir() / f"{safe}.json"


def _pending_claim_path(token: str) -> Path:
    safe = "".join(ch for ch in token if ch.isalnum() or ch in ("-", "_"))
    return _pending_dir() / f"{safe}.processing"


def _approval_workitem_lock_dir() -> Path:
    return _state_dir() / "approval-workitem-locks"


def _transport_confirmation_dir() -> Path:
    return _state_dir() / "transport-confirmations"


def _transport_confirmation_path(token: str) -> Path:
    safe = "".join(ch for ch in token if ch.isalnum() or ch in ("-", "_"))
    return _transport_confirmation_dir() / f"{safe}.json"


def _auto_login_lock_path(session: str) -> Path:
    return _state_dir() / "auto-login-locks" / f"{_safe_session_name(session)}.lock"


def _write_session_meta(session: str, data: Dict[str, Any]) -> None:
    paths = _session_paths(session)
    _ensure_private_dir(paths["root"])
    paths["meta"].write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        paths["meta"].chmod(0o600)
    except OSError:
        pass


def _save_session(
    session: str,
    base_url: str,
    login_account: str = "",
    auto_login_enabled: Optional[bool] = None,
) -> None:
    data = _saved_session_meta(session)
    data["baseUrl"] = base_url
    if login_account:
        data["loginAccount"] = login_account
    if auto_login_enabled is not None:
        data["autoLoginEnabled"] = bool(auto_login_enabled)
    _write_session_meta(session, data)


def _load_base_url(session: str, base_url: Optional[str]) -> str:
    if base_url:
        return base_url
    env_base = os.getenv("OA_BASE_URL")
    if env_base:
        return env_base
    saved_base = _saved_base_url(session)
    if saved_base:
        return saved_base
    raise OAConnectorError("缺少 baseUrl：请先配置 OA 地址，或传入 baseUrl/OA_BASE_URL")


def _reject_https_tls_skip_without_admin(base_url: str, insecure: bool) -> None:
    scheme = urllib.parse.urlparse(str(base_url)).scheme.lower()
    if scheme == "https" and insecure and not _env_flag("OA_AGENT_ALLOW_INSECURE_AUTH"):
        raise OAConnectorError("HTTPS 证书校验不能跳过；如确需跳过，请由管理员显式设置 OA_AGENT_ALLOW_INSECURE_AUTH=1")


def _client(session: str, base_url: Optional[str] = None, insecure: bool = False) -> OAClient:
    resolved_base_url = _load_base_url(session, base_url)
    _reject_https_tls_skip_without_admin(resolved_base_url, insecure)
    paths = _session_paths(session)
    return OAClient(
        resolved_base_url,
        cookie_file=str(paths["cookie"]),
        verify_tls=not insecure,
    )


def _auto_login_available(session: str) -> bool:
    meta = _saved_session_meta(session)
    return bool(meta.get("autoLoginEnabled") and meta.get("loginAccount"))


def _block_auto_login(session: str, now: Optional[int] = None) -> None:
    meta = _saved_session_meta(session)
    failed_at = int(now if now is not None else time.time())
    failure_count = int(meta.get("autoLoginFailureCount") or 0) + 1
    meta["autoLoginLastFailedAt"] = failed_at
    meta["autoLoginBlockedUntil"] = failed_at + 900
    meta["autoLoginFailureCount"] = failure_count
    if failure_count >= 3:
        meta["autoLoginEnabled"] = False
        meta["autoLoginRequiresManualAuth"] = True
    meta.pop("autoLoginLastSucceededAt", None)
    _write_session_meta(session, meta)


def _try_auto_login(session: str) -> bool:
    meta = _saved_session_meta(session)
    if not meta.get("autoLoginEnabled"):
        return False
    username = str(meta.get("loginAccount") or "").strip()
    base_url = str(meta.get("baseUrl") or "").strip()
    if not username or not base_url:
        return False
    started_at = time.time()
    now = int(started_at)
    if int(meta.get("autoLoginBlockedUntil") or 0) > now:
        return False
    lock_path = _auto_login_lock_path(session)
    _ensure_private_dir(lock_path.parent)
    lock_token = secrets.token_urlsafe(16)
    lock_acquired = False
    saw_existing_lock = False
    deadline = time.monotonic() + 65
    while not lock_acquired:
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            saw_existing_lock = True
            current = _saved_session_meta(session)
            if float(current.get("autoLoginLastSucceededAt") or 0) > started_at:
                return True
            if int(current.get("autoLoginBlockedUntil") or 0) > int(time.time()):
                return False
            try:
                if time.time() - lock_path.stat().st_mtime > 90:
                    lock_path.unlink()
                    saw_existing_lock = False
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
            continue
        try:
            os.write(fd, lock_token.encode("ascii"))
        finally:
            os.close(fd)
        lock_acquired = True

    try:
        if saw_existing_lock:
            current = _saved_session_meta(session)
            if float(current.get("autoLoginLastSucceededAt") or 0) > started_at:
                return True
            if int(current.get("autoLoginBlockedUntil") or 0) > int(time.time()):
                return False
        meta = _saved_session_meta(session)
        username = str(meta.get("loginAccount") or "").strip()
        base_url = str(meta.get("baseUrl") or "").strip()
        if not meta.get("autoLoginEnabled") or not username or not base_url:
            return False
        password = SystemCredentialStore(namespace=str(_state_dir().resolve())).load(
            base_url,
            session,
            username,
        )
        if not password:
            raise CredentialStoreError("系统密码保险箱中没有可用登录信息")
        insecure = bool(meta.get("autoLoginInsecure"))
        _reject_https_tls_skip_without_admin(base_url, insecure)
        client = OAClient(
            base_url,
            cookie_file=str(_session_paths(session)["cookie"]),
            verify_tls=not insecure,
        )
        client.login(username, password)
        client.assert_logged_in()
    except Exception:
        _block_auto_login(session, now=now)
        return False
    else:
        meta.pop("autoLoginLastFailedAt", None)
        meta.pop("autoLoginBlockedUntil", None)
        meta.pop("autoLoginFailureCount", None)
        meta.pop("autoLoginRequiresManualAuth", None)
        meta["autoLoginLastSucceededAt"] = time.time()
        _write_session_meta(session, meta)
        return True
    finally:
        try:
            if lock_path.read_text(encoding="ascii") == lock_token:
                lock_path.unlink()
        except OSError:
            pass


def _can_retry_after_auto_login(tool_name: str) -> bool:
    return tool_name not in {
        "oa_setup_guide",
        "oa_begin_auth",
        "oa_local_auth_status",
        "oa_login",
        "oa_confirm_approval",
        "oa_confirm_batch_approval",
    }


def _absolute_url(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(str(base_url).rstrip("/") + "/", str(path or "").lstrip("/"))


def _canonical_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(str(base_url or "").strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        raise OAConnectorError("OA 地址格式不正确")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OAConnectorError("OA 地址格式不正确") from exc
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    decoded_path = urllib.parse.unquote(parsed.path or "/")
    normalized_path = posixpath.normpath("/" + decoded_path.lstrip("/"))
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, host, normalized_path, "", ""))


def _ok(data: Any) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}]}


def _mcp_error(data: Any) -> Dict[str, Any]:
    return {
        "isError": True,
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
    }


def _auth_required_reason(reason: str) -> bool:
    text = str(reason or "")
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "当前会话未登录",
            "cookie 未登录",
            "cookie 已失效",
            "未登录",
            "未授权",
            "授权失效",
            "授权不可用",
            "登录状态不可用",
            "登录失败",
            "登录页",
            "仍停留在登录页",
            "请先 login",
            "http 401",
            "unauthorized",
        )
    )


def _setup_guide(reason: str = "", session: str = "default") -> Dict[str, Any]:
    example_base_url = os.getenv("OA_BASE_URL") or _saved_base_url(session) or "<OA_BASE_URL>"
    example_state_dir = os.getenv("OA_AGENT_STATE_DIR") or str(_state_dir())
    auth_required = _auth_required_reason(reason)
    base_url_configured = example_base_url != "<OA_BASE_URL>"
    auth_action = {
        "tool": "oa_begin_auth",
        "arguments": {
            "baseUrl": example_base_url,
            "session": session,
        },
    }
    return {
        "ok": False,
        "reason": reason,
        "session": session,
        "configurationRequired": not base_url_configured,
        "reauthRequired": auth_required,
        "nextAction": auth_action if auth_required and base_url_configured else None,
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
                "description": "优先调用 oa_begin_auth 生成本机授权页面。用户可选择把登录信息安全保存在电脑自带的密码保险箱中，登录过期后自动恢复。",
                **auth_action,
            },
            {
                "step": 3,
                "title": "查询待办",
                "description": "授权成功后调用 oa_list_todos，只会返回当前登录账号有权限看到的待审批清单。",
                "tool": "oa_list_todos",
                "arguments": {"session": session, "page": 1, "pageSize": 20},
            },
        ],
    }


def _redact_tool_message(message: str) -> str:
    if _auth_required_reason(message):
        return "OA 登录状态不可用"
    return sanitize_error_message(message)


def _tool_error(message: str, session: str = "default") -> Dict[str, Any]:
    return _mcp_error(_setup_guide(_redact_tool_message(message), session=session))


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


def _approval_login_binding(session: str, base_url: str) -> str:
    meta = _saved_session_meta(session)
    login_account = str(meta.get("loginAccount") or "").strip()
    if not login_account:
        raise OAConnectorError("无法确认当前 OA 登录账号，请重新授权后再准备审批")
    normalized_base_url = _canonical_base_url(str(base_url or meta.get("baseUrl") or ""))
    return hashlib.sha256(
        f"{normalized_base_url}\n{session}\n{login_account}".encode("utf-8")
    ).hexdigest()


def _find_current_todo(client: OAClient, fd_id: str) -> Dict[str, Any]:
    for todo in client.list_todos(page=1, page_size=200):
        if todo.fd_id == fd_id:
            return todo.to_dict()
    raise OAConnectorError(f"拒绝操作：{fd_id} 不在当前登录账号的待审批列表中")


def _approval_binding_complete(binding: Any) -> bool:
    return isinstance(binding, dict) and all(
        str(binding.get(key) or "").strip()
        for key in ("processId", "taskId", "nodeId", "activityType", "operationType")
    )


def _approval_client_for_pending(pending: Dict[str, Any], fd_id: str) -> OAClient:
    pending_session = str(pending["session"])
    base_url = str(pending["baseUrl"])
    current_binding = _approval_login_binding(pending_session, base_url)
    if not pending.get("loginBinding") or pending["loginBinding"] != current_binding:
        raise OAConnectorError("OA 登录账号已变化，请重新准备审批并再次确认")

    client = _client(
        session=pending_session,
        base_url=base_url,
        insecure=bool(pending.get("insecure")),
    )
    try:
        _find_current_todo(client, fd_id)
    except Exception as exc:
        if not (
            _auth_required_reason(str(exc))
            and _auto_login_available(pending_session)
            and _try_auto_login(pending_session)
        ):
            raise
        client = _client(
            session=pending_session,
            base_url=base_url,
            insecure=bool(pending.get("insecure")),
        )
        try:
            _find_current_todo(client, fd_id)
        except Exception as retry_exc:
            if _auth_required_reason(str(retry_exc)):
                _block_auto_login(pending_session)
            raise

    current_binding = _approval_login_binding(pending_session, base_url)
    if not pending.get("loginBinding") or pending["loginBinding"] != current_binding:
        raise OAConnectorError("OA 登录账号已变化，请重新准备审批并再次确认")
    return client


def _execute_bound_approval(client: OAClient, item: Dict[str, Any]) -> Dict[str, Any]:
    binding = item.get("approvalBinding")
    if not _approval_binding_complete(binding):
        raise OAConnectorError("审批确认状态不完整，请重新准备审批并再次确认")
    if item["action"] == "approve":
        return client.approve(
            str(item["fdId"]),
            str(item["note"]),
            execute=True,
            future_node_id=item.get("futureNodeId"),
            expected_binding=binding,
        )
    if item["action"] == "reject":
        return client.reject(
            str(item["fdId"]),
            str(item["note"]),
            execute=True,
            expected_binding=binding,
        )
    raise OAConnectorError("审批动作只允许 approve 或 reject")


def _normalize_batch_approval_items(raw_items: Any) -> list[Dict[str, Any]]:
    if not isinstance(raw_items, list) or not raw_items:
        raise OAConnectorError("批量审批至少需要 1 条单据")
    if len(raw_items) > MAX_BATCH_APPROVAL_ITEMS:
        raise OAConnectorError(f"单次批量审批最多 {MAX_BATCH_APPROVAL_ITEMS} 条")

    allowed_keys = {"fdId", "action", "note"}
    normalized: list[Dict[str, Any]] = []
    seen_fd_ids = set()
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise OAConnectorError(f"批量审批第 {index} 条格式不正确")
        if "futureNodeId" in raw:
            raise OAConnectorError("MCP 正式审批不支持手工指定下一节点，请在 OA 页面处理")
        unknown_keys = set(raw) - allowed_keys
        if unknown_keys:
            raise OAConnectorError(f"批量审批第 {index} 条包含不支持的参数")
        if not isinstance(raw.get("fdId"), str):
            raise OAConnectorError(f"批量审批第 {index} 条单据格式不正确")
        if not isinstance(raw.get("action"), str):
            raise OAConnectorError(f"批量审批第 {index} 条动作格式不正确")
        if not isinstance(raw.get("note"), str):
            raise OAConnectorError(f"批量审批第 {index} 条备注格式不正确")
        fd_id = raw["fdId"].strip()
        action = raw["action"].strip()
        note = raw["note"].strip()
        if not fd_id:
            raise OAConnectorError(f"批量审批第 {index} 条缺少单据")
        if fd_id in seen_fd_ids:
            raise OAConnectorError("同一条单据不能在一次批量审批中重复出现")
        _approval_action_label(action)
        if not note:
            raise OAConnectorError(f"批量审批第 {index} 条备注不能为空")
        seen_fd_ids.add(fd_id)
        normalized.append(
            {
                "fdId": fd_id,
                "action": action,
                "note": note,
            }
        )
    return normalized


def _batch_item_public(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    result = {
        "index": index,
        "fdId": item["fdId"],
        "subject": item.get("subject") or item.get("detailTitle") or "",
        "action": item["action"],
        "actionLabel": _approval_action_label(str(item["action"])),
        "note": item["note"],
        "currentNode": item.get("currentNode") or "",
        "currentHandler": item.get("currentHandler") or "",
    }
    if item.get("detailUrl"):
        result["detailUrl"] = item["detailUrl"]
    return result


def _batch_item_with_status(
    item: Dict[str, Any],
    index: int,
    status: str,
    reason: str = "",
) -> Dict[str, Any]:
    result = _batch_item_public(item, index)
    result["status"] = status
    if reason:
        result["reason"] = _redact_tool_message(reason)
    return result


def _pending_approval_kind(pending: Dict[str, Any]) -> str:
    kind = str(pending.get("kind") or "single")
    if kind not in {"single", "batch"}:
        raise OAConnectorError("审批确认状态格式不正确，请重新准备审批")
    return kind


def _save_pending_approval(data: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(24)
    data = dict(data)
    data["confirmationToken"] = token
    data["createdAt"] = int(time.time())
    data["expiresAt"] = data["createdAt"] + 900
    pending_dir = _pending_dir()
    _ensure_private_dir(pending_dir)
    path = _pending_path(token)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


def _save_transport_confirmation(session: str, base_url: str, expires_in: int) -> Dict[str, Any]:
    token = secrets.token_urlsafe(24)
    data = {
        "token": token,
        "session": session,
        "baseUrl": base_url,
        "expiresAt": int(time.time()) + max(60, min(int(expires_in), 1800)),
    }
    path = _transport_confirmation_path(token)
    _ensure_private_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def _consume_transport_confirmation(token: str, session: str, base_url: str) -> bool:
    if not token:
        return False
    path = _transport_confirmation_path(token)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        path.unlink()
    except OSError:
        pass
    if int(data.get("expiresAt") or 0) < int(time.time()):
        return False
    return data.get("session") == session and data.get("baseUrl") == base_url


def _load_pending_approval(token: str) -> Dict[str, Any]:
    path = _pending_path(token)
    if _pending_claim_path(token).exists():
        raise OAConnectorError("这次审批确认正在处理或已经使用，请重新准备审批")
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


def _claim_pending_approval(token: str) -> Dict[str, Any]:
    source = _pending_path(token)
    claimed = _pending_claim_path(token)
    _ensure_private_dir(claimed.parent)
    try:
        fd = os.open(str(claimed), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise OAConnectorError("这次审批确认正在处理或已经使用，请重新准备审批") from exc
    try:
        try:
            raw = source.read_bytes()
        except FileNotFoundError as exc:
            os.close(fd)
            fd = -1
            try:
                claimed.unlink()
            except OSError:
                pass
            raise OAConnectorError("确认 token 不存在或已使用，请重新准备审批") from exc
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            source.unlink()
        except OSError:
            pass
        data = json.loads(raw.decode("utf-8"))
        if int(time.time()) > int(data.get("expiresAt") or 0):
            try:
                claimed.unlink()
            except OSError:
                pass
            raise OAConnectorError("确认 token 已过期，请重新准备审批")
        return data
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise


def _atomic_write_private_json(path: Path, data: Dict[str, Any], *, require_exists: bool = False) -> None:
    if require_exists and not path.exists():
        raise OAConnectorError("审批确认状态已经不可用，请重新准备审批")
    _ensure_private_dir(path.parent)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass


def _write_claimed_approval(token: str, data: Dict[str, Any]) -> None:
    _atomic_write_private_json(_pending_claim_path(token), data, require_exists=True)


def _approval_workitem_lock_path(pending: Dict[str, Any], item: Dict[str, Any]) -> Path:
    binding = item.get("approvalBinding")
    if not _approval_binding_complete(binding):
        raise OAConnectorError("审批确认状态不完整，请重新准备审批并再次确认")
    normalized_base_url = _canonical_base_url(str(pending.get("baseUrl") or ""))
    lock_key = hashlib.sha256(
        (
            f"{normalized_base_url}\n"
            f"{str(item.get('fdId') or '').strip()}\n"
            f"{str(binding['processId']).strip()}\n"
            f"{str(binding['taskId']).strip()}"
        ).encode("utf-8")
    ).hexdigest()
    return _approval_workitem_lock_dir() / f"{lock_key}.lock"


def _approval_workitem_lock_owner(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _acquire_approval_workitem_lock(
    pending: Dict[str, Any],
    item: Dict[str, Any],
    token: str,
) -> Path:
    path = _approval_workitem_lock_path(pending, item)
    _ensure_private_dir(path.parent)
    owner = _approval_workitem_lock_owner(token)
    now = int(time.time())
    data = {
        "owner": owner,
        "state": "claimed",
        "createdAt": now,
    }
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise OAConnectorError(
            "这条审批正在由另一个确认流程处理或刚刚提交，请重新查询待办后再操作"
        ) from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def _mark_approval_workitem_submitted(path: Path, token: str) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise OAConnectorError("无法保存审批防重复状态，请勿重复提交") from exc
    if data.get("owner") != _approval_workitem_lock_owner(token):
        raise OAConnectorError("审批防重复状态已变化，请勿重复提交")
    now = int(time.time())
    data["state"] = "submitted"
    data["submittedAt"] = now
    _atomic_write_private_json(path, data, require_exists=True)


def _release_approval_workitem_lock(path: Optional[Path], token: str) -> None:
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    if data.get("owner") != _approval_workitem_lock_owner(token) or data.get("state") == "submitted":
        return
    try:
        path.unlink()
    except OSError:
        pass


def _save_batch_progress(
    token: str,
    pending: Dict[str, Any],
    *,
    status: str,
    completed_items: list[Dict[str, Any]],
    current_item: Optional[Dict[str, Any]] = None,
) -> None:
    state = dict(pending)
    state["batchProgress"] = {
        "status": status,
        "completedItems": completed_items,
        "currentItem": current_item,
        "updatedAt": int(time.time()),
    }
    _write_claimed_approval(token, state)


def _save_batch_terminal_result(
    token: str,
    pending: Dict[str, Any],
    *,
    status: str,
    completed_items: list[Dict[str, Any]],
    current_item: Optional[Dict[str, Any]],
    payload: Dict[str, Any],
    is_error: bool,
) -> None:
    now = int(time.time())
    state = dict(pending)
    state["batchProgress"] = {
        "status": status,
        "completedItems": completed_items,
        "currentItem": current_item,
        "updatedAt": now,
    }
    state["terminalResult"] = {
        "isError": is_error,
        "payload": payload,
        "completedAt": now,
    }
    state["terminalExpiresAt"] = now + BATCH_TERMINAL_RESULT_TTL_SECONDS
    _write_claimed_approval(token, state)


def _load_batch_terminal_result(token: str) -> Optional[Dict[str, Any]]:
    path = _pending_claim_path(token)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if _pending_approval_kind(state) != "batch":
        raise OAConnectorError("这是单条审批确认，请使用 oa_confirm_approval")
    terminal = state.get("terminalResult")
    if not isinstance(terminal, dict) or not isinstance(terminal.get("payload"), dict):
        return None
    if int(state.get("terminalExpiresAt") or 0) < int(time.time()):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    current_binding = _approval_login_binding(
        str(state.get("session") or "default"),
        str(state.get("baseUrl") or ""),
    )
    if not state.get("loginBinding") or state["loginBinding"] != current_binding:
        raise OAConnectorError("OA 登录账号已变化，不能读取上一账号的批量审批结果")
    if terminal.get("isError"):
        return _mcp_error(terminal["payload"])
    return _ok(terminal["payload"])


def _cleanup_expired_batch_terminal_results() -> None:
    pending_dir = _pending_dir()
    if not pending_dir.exists():
        return
    now = int(time.time())
    for path in pending_dir.glob("*.processing"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(state.get("terminalResult"), dict):
            continue
        if int(state.get("terminalExpiresAt") or 0) >= now:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def _batch_stopped_payload(
    pending: Dict[str, Any],
    items: list[Dict[str, Any]],
    completed_items: list[Dict[str, Any]],
    current_index: int,
    reason: str,
    *,
    result_unknown: bool,
    submitted_once: bool,
) -> Dict[str, Any]:
    public_reason = _redact_tool_message(reason)
    current_status = "resultUnknown" if result_unknown else "failed"
    current_item = _batch_item_with_status(
        items[current_index],
        current_index + 1,
        current_status,
        reason,
    )
    not_attempted_items = [
        _batch_item_with_status(item, index + 1, "notAttempted")
        for index, item in enumerate(items[current_index + 1 :], start=current_index + 1)
    ]
    completed_count = len(completed_items)
    completed_prefix = f"前 {completed_count} 条已完成，" if completed_count else ""
    if result_unknown:
        user_message = f"批量审批已停止：{completed_prefix}第 {current_index + 1} 条结果不明确，后续未执行。"
        next_step = "请先在 OA 页面核对当前这条单据的流程状态，不要重复提交；核对后重新查询待办，只为仍需处理的项目准备新批次。"
    else:
        user_message = f"批量审批已停止：{completed_prefix}第 {current_index + 1} 条未完成，后续未执行。"
        next_step = "请处理当前失败原因后重新查询待办，只为仍需处理的项目准备新批次。"
    payload: Dict[str, Any] = {
        "ok": False,
        "batch": True,
        "anyCompleted": completed_count > 0,
        "stopped": True,
        "batchNonTransactional": True,
        "partialCompletion": completed_count > 0,
        "totalCount": len(items),
        "completedCount": completed_count,
        "completedItems": completed_items,
        "currentItem": current_item,
        "notAttemptedCount": len(not_attempted_items),
        "notAttemptedItems": not_attempted_items,
        "resultUnknown": result_unknown,
        "submittedOnce": submitted_once,
        "retryAllowed": False,
        "reason": public_reason,
        "userMessage": user_message,
        "nextStep": next_step,
    }
    if _auth_required_reason(reason):
        guide = _setup_guide("OA 登录状态不可用", session=str(pending["session"]))
        payload["reauthRequired"] = True
        payload["nextAction"] = guide.get("nextAction")
        payload["nextStep"] = (
            "请先按 nextAction 重新授权，再重新查询待办；只为仍需处理的项目准备新批次，不能复用本次确认 token。"
        )
    return payload


def _batch_persistence_stopped_payload(
    items: list[Dict[str, Any]],
    completed_items: list[Dict[str, Any]],
    next_index: int,
) -> Dict[str, Any]:
    not_attempted_items = [
        _batch_item_with_status(item, index + 1, "notAttempted")
        for index, item in enumerate(items[next_index:], start=next_index)
    ]
    all_oa_items_completed = not not_attempted_items and len(completed_items) == len(items)
    if all_oa_items_completed:
        user_message = (
            f"OA 已明确完成全部 {len(completed_items)} 条审批，但本机无法安全保存最终记录。"
            "请勿重复提交，请先刷新待办核对。"
        )
        next_step = "请重新查询 OA 待办并核对全部项目；不要复用本次确认 token。"
    else:
        user_message = (
            f"批量审批已停止：前 {len(completed_items)} 条已完成，但本机无法安全保存后续进度，剩余项目未执行。"
        )
        next_step = "请先重新查询 OA 待办并核对已完成项目，再为仍需处理的项目准备新批次；不要复用本次确认 token。"
    return {
        "ok": False,
        "batch": True,
        "stopped": not all_oa_items_completed,
        "batchNonTransactional": True,
        "partialCompletion": bool(completed_items) and bool(not_attempted_items),
        "anyCompleted": bool(completed_items),
        "oaCompletedAllItems": all_oa_items_completed,
        "totalCount": len(items),
        "completedCount": len(completed_items),
        "completedItems": completed_items,
        "currentItem": None,
        "notAttemptedCount": len(not_attempted_items),
        "notAttemptedItems": not_attempted_items,
        "resultUnknown": False,
        "retryAllowed": False,
        "statePersistenceWarning": True,
        "reason": "本机无法安全保存批量审批进度",
        "userMessage": user_message,
        "nextStep": next_step,
    }


def _delete_pending_approval(token: str) -> None:
    source = _pending_path(token)
    claimed = _pending_claim_path(token)
    try:
        source.unlink()
    except OSError:
        pass
    if source.exists():
        return
    try:
        claimed.unlink()
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


PASSWORD_LOGIN_TOOL = _tool_schema(
    "oa_login",
    "兼容/调试登录工具。默认不向普通 Agent 暴露；普通用户必须优先使用 oa_begin_auth 本机授权页，避免在聊天里输入密码。",
    {
        "baseUrl": {"type": "string", "description": "OA 根地址，例如 https://example.com/oa/"},
        "username": {"type": "string", "description": "OA 登录账号"},
        "password": {"type": "string", "description": "OA 登录密码"},
        "session": {"type": "string", "description": "本地会话名，默认 default"},
        "insecure": {"type": "boolean", "description": "HTTPS 证书不校验，默认 false；仅管理员批准后使用"},
    },
    ["baseUrl", "username", "password"],
)


TOOLS = [
    _tool_schema(
        "oa_setup_guide",
        "当用户想查看 OA 待办但 MCP 未配置、未授权或授权过期时，返回分步配置和授权指引。",
        {
            "reason": {"type": "string", "description": "触发指引的原因，可选"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_begin_auth",
        "启动只监听 127.0.0.1 的本机 OA 授权页面，返回可点击 authUrl。密码不进入聊天或连接器文件；用户可选择存入系统密码保险箱，用于登录过期后自动恢复。",
        {
            "baseUrl": {"type": "string", "description": "OA 根地址；不传则使用已配置 OA_BASE_URL 或当前 session 保存的地址"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "expiresInSeconds": {"type": "integer", "minimum": 60, "maximum": 1800, "default": 600},
            "insecure": {"type": "boolean", "description": "用户确认后允许通过 HTTP 内网 OA 授权；HTTPS 跳过证书校验仍需管理员批准"},
            "transportConfirmationToken": {"type": "string", "description": "MCP 返回的 HTTP 安全确认令牌；只有用户确认后才能按 nextAction 传入"},
        },
    ),
    _tool_schema(
        "oa_local_auth_status",
        "查询 oa_begin_auth 返回的本机授权页面状态，不触碰 OA 密码。",
        {
            "authToken": {"type": "string"},
        },
        ["authToken"],
    ),
    _tool_schema(
        "oa_disable_auto_login",
        "关闭当前 OA 会话的自动登录。只删除系统密码保险箱中的登录信息，保留当前 cookie。",
        {
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_auth_status",
        "检查指定会话是否仍可访问 OA 待办数据源，并尽量返回当前登录身份 loginAs。",
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
        "准备审批动作：校验当前账号待办权限和当前节点可用动作，整理单据、动作和备注，生成待用户确认的摘要和 confirmationToken。不提交审批。",
        {
            "fdId": {"type": "string"},
            "action": {"type": "string", "enum": ["approve", "reject"], "description": "approve=同意，reject=驳回"},
            "note": {"type": "string", "description": "审批备注/意见"},
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
        "oa_prepare_batch_approval",
        "准备一批审批：一次最多 20 条，可混合同意和驳回，不支持手工指定下一节点。会逐条校验当前账号待办权限、当前节点动作和审批任务绑定；全部通过后才生成确认摘要和 confirmationToken，不提交审批。",
        {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_BATCH_APPROVAL_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "fdId": {"type": "string"},
                        "action": {
                            "type": "string",
                            "enum": ["approve", "reject"],
                            "description": "approve=同意，reject=驳回",
                        },
                        "note": {"type": "string", "description": "本条单据的审批备注/意见"},
                    },
                    "required": ["fdId", "action", "note"],
                    "additionalProperties": False,
                },
            },
            "baseUrl": {"type": "string"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
            "insecure": {"type": "boolean"},
        },
        ["items"],
    ),
    _tool_schema(
        "oa_confirm_batch_approval",
        "用户确认后按摘要顺序逐条执行批量审批。必须传 oa_prepare_batch_approval 返回的 confirmationToken，并传固定确认文本“确认批量审批”。批量审批不是事务；遇到第一条失败或结果不明确会立即停止，后续项目不会执行。",
        {
            "confirmationToken": {"type": "string"},
            "confirmationText": {"type": "string", "description": "固定填写：确认批量审批"},
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
        "执行 OA 通用只读搜索，返回结构化结果和受控 recordRef。常用：scope 可选 all/knowledge/news；searchFields 可选 title/content/fdDescription/creator/attachment，其中 title 由 MCP 本地过滤，不下发 OA 标题字段；matchMode 可选 keyword/contains/exact，contains/exact 会自动忽略标题里的空白；默认 requireDetail=true，只返回可继续查看详情的结果；默认 dedupByDocument=true，按文档去重。",
        {
            "query": {"type": "string"},
            "scope": {"type": "string", "enum": ["all", "knowledge", "news"]},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "matchMode": {"type": "string", "enum": ["keyword", "contains", "exact"]},
            "requireDetail": {"type": "boolean", "description": "默认 true，只返回可用 oa_get_object_detail 查看详情的结果"},
            "dedupByDocument": {"type": "boolean", "description": "默认 true，按 fdId 聚合搜索结果，减少附件级重复条目"},
            "searchFields": {"type": "array", "description": "title 会由 MCP 本地过滤，不下发 OA 标题字段", "items": {"type": "string", "enum": ["title", "content", "fdDescription", "creator", "attachment"]}},
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
        "批量执行通用 OA 搜索，输入为 queries 数组，可选列附件或受限下载。支持 matchMode keyword/contains/exact；searchFields=title 由 MCP 本地过滤；默认 requireDetail=true；默认 dedupByDocument=true。",
        {
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "scope": {"type": "string", "enum": ["all", "knowledge", "news"]},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "matchMode": {"type": "string", "enum": ["keyword", "contains", "exact"]},
            "requireDetail": {"type": "boolean", "description": "默认 true，只返回可继续查看详情的结果"},
            "dedupByDocument": {"type": "boolean", "description": "默认 true，按 fdId 聚合搜索结果"},
            "searchFields": {"type": "array", "description": "title 会由 MCP 本地过滤，不下发 OA 标题字段", "items": {"type": "string", "enum": ["title", "content", "fdDescription", "creator", "attachment"]}},
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


def _listed_tools() -> list[Dict[str, Any]]:
    if _password_login_enabled():
        return TOOLS + [PASSWORD_LOGIN_TOOL]
    return TOOLS


def _session(args: Dict[str, Any]) -> str:
    return str(args.get("session") or "default")


def _bool(args: Dict[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if isinstance(value, bool):
        return value
    raise OAConnectorError(f"参数 {key} 必须是布尔值 true 或 false")



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


def _http_transport_confirmation_error(session: str, base_url: str, expires_in: int) -> Dict[str, Any]:
    confirmation = _save_transport_confirmation(session, base_url, expires_in)
    return _mcp_error(
        {
            "ok": False,
            "transportSecurityRequired": True,
            "reason": "当前 OA 地址使用 HTTP。继续授权时，OA 账号和密码会通过非 HTTPS 连接发送。",
            "code": "httpBaseUrl",
            "session": session,
            "baseUrl": base_url,
            "confirmationText": "确认继续登录",
            "transportConfirmationToken": confirmation["token"],
            "confirmationExpiresAt": confirmation["expiresAt"],
            "nextAction": {
                "tool": "oa_begin_auth",
                "arguments": {
                    "baseUrl": base_url,
                    "session": session,
                    "expiresInSeconds": expires_in,
                    "insecure": True,
                    "transportConfirmationToken": confirmation["token"],
                },
            },
            "userMessage": "请确认你正在登录公司 OA。确认后我会打开本机授权页，你只需要在页面里输入 OA 账号和密码。",
        }
    )



def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    _cleanup_expired_batch_terminal_results()
    session = _session(args)
    insecure = _bool(args, "insecure")

    if name == "oa_setup_guide":
        return _ok(_setup_guide(str(args.get("reason") or ""), session=session))

    if name == "oa_begin_auth":
        base_url = _load_base_url(session, args.get("baseUrl"))
        expires_in = int(args.get("expiresInSeconds") or 600)
        scheme = urllib.parse.urlparse(str(base_url)).scheme.lower()
        if scheme == "http" and not _env_flag("OA_AGENT_ALLOW_INSECURE_AUTH"):
            if not insecure:
                return _http_transport_confirmation_error(session, base_url, expires_in)
            confirmation_token = str(args.get("transportConfirmationToken") or "")
            if not _consume_transport_confirmation(confirmation_token, session, base_url):
                return _http_transport_confirmation_error(session, base_url, expires_in)
        security_issue = transport_security_issue(base_url, insecure)
        if security_issue:
            payload: Dict[str, Any] = {
                "ok": False,
                "transportSecurityRequired": False,
                "reason": security_issue["message"],
                "code": security_issue["code"],
                "session": session,
                "baseUrl": base_url,
            }
            if security_issue["code"] == "tlsVerificationDisabled":
                payload["adminApprovalRequired"] = True
                payload["userMessage"] = "当前 HTTPS 连接请求跳过证书校验。请使用可信证书，或由管理员显式批准后再继续。"
            else:
                payload["configurationRequired"] = True
                payload["userMessage"] = "OA 地址格式不正确。请提供以 https:// 或 http:// 开头的 OA 地址。"
            return _mcp_error(payload)
        result = begin_local_auth(
            base_url=base_url,
            session=session,
            state_dir=str(_state_dir()),
            insecure=insecure,
            expires_in=expires_in,
        )
        return _ok(result)

    if name == "oa_local_auth_status":
        return _ok(read_local_auth_status(str(_state_dir()), str(args["authToken"])))

    if name == "oa_disable_auto_login":
        meta = _saved_session_meta(session)
        base_url = str(meta.get("baseUrl") or "")
        username = str(meta.get("loginAccount") or "")
        if meta.get("autoLoginEnabled") or meta.get("credentialCleanupFailed"):
            SystemCredentialStore(namespace=str(_state_dir().resolve())).delete(
                base_url,
                session,
                username,
            )
        meta["autoLoginEnabled"] = False
        meta["autoLoginInsecure"] = False
        meta["credentialCleanupFailed"] = False
        meta.pop("autoLoginLastFailedAt", None)
        meta.pop("autoLoginBlockedUntil", None)
        meta.pop("autoLoginLastSucceededAt", None)
        meta.pop("autoLoginFailureCount", None)
        meta.pop("autoLoginRequiresManualAuth", None)
        _write_session_meta(session, meta)
        return _ok({"ok": True, "session": session, "autoLoginEnabled": False, "cookiePreserved": True})

    if name == "oa_login":
        if not _password_login_enabled():
            raise OAConnectorError("oa_login 默认禁用。请使用 oa_begin_auth 生成本机授权页面，避免在聊天里输入 OA 密码")
        base_url = str(args["baseUrl"])
        _reject_https_tls_skip_without_admin(base_url, insecure)
        client = OAClient(base_url, cookie_file=str(_session_paths(session)["cookie"]), verify_tls=not insecure)
        client.login(str(args["username"]), str(args["password"]))
        _save_session(session, base_url, login_account=str(args["username"]))
        return _ok({"ok": True, "session": session, "baseUrl": base_url})

    if name == "oa_confirm_approval":
        token = str(args["confirmationToken"])
        pending = _load_pending_approval(token)
        if _pending_approval_kind(pending) != "single":
            raise OAConnectorError("这是批量审批确认，请使用 oa_confirm_batch_approval")
        expected = _approval_confirm_phrase(str(pending["action"]))
        confirmation_text = str(args["confirmationText"]).strip()
        if confirmation_text != expected:
            raise OAConnectorError(f"确认文本不匹配：需要用户明确发送“{expected}”")
        pending = _claim_pending_approval(token)
        if _pending_approval_kind(pending) != "single":
            raise OAConnectorError("这是批量审批确认，请使用 oa_confirm_batch_approval")
        expected = _approval_confirm_phrase(str(pending["action"]))
        if confirmation_text != expected:
            raise OAConnectorError(f"确认文本不匹配：需要用户明确发送“{expected}”")
        if not _approval_binding_complete(pending.get("approvalBinding")):
            _delete_pending_approval(token)
            raise OAConnectorError("审批确认状态不完整，请重新准备审批并再次确认")
        if pending.get("futureNodeId"):
            _delete_pending_approval(token)
            raise OAConnectorError("MCP 正式审批不支持手工指定下一节点，请在 OA 页面处理")
        workitem_lock: Optional[Path] = None
        try:
            workitem_lock = _acquire_approval_workitem_lock(pending, pending, token)
            confirm_client = _approval_client_for_pending(pending, str(pending["fdId"]))
        except Exception:
            _release_approval_workitem_lock(workitem_lock, token)
            _delete_pending_approval(token)
            raise
        try:
            result = _execute_bound_approval(confirm_client, pending)
        except ApprovalStateChangedError:
            _release_approval_workitem_lock(workitem_lock, token)
            _delete_pending_approval(token)
            raise
        except ApprovalResultUnknownError as exc:
            state_persistence_warning = False
            try:
                _mark_approval_workitem_submitted(workitem_lock, token)
            except Exception:
                state_persistence_warning = True
            _delete_pending_approval(token)
            payload = {
                "ok": False,
                "resultUnknown": True,
                "submittedOnce": True,
                "retryAllowed": False,
                "action": pending["action"],
                "fdId": pending["fdId"],
                "reason": str(exc),
                "userMessage": str(exc),
                "nextStep": "请用户先打开 OA 页面查看这条单据的流程状态，不要再次提交本次审批。",
            }
            if state_persistence_warning:
                payload["statePersistenceWarning"] = True
            return _mcp_error(payload)
        except OAConnectorError:
            _release_approval_workitem_lock(workitem_lock, token)
            _delete_pending_approval(token)
            raise
        except Exception as exc:
            state_persistence_warning = False
            try:
                _mark_approval_workitem_submitted(workitem_lock, token)
            except Exception:
                state_persistence_warning = True
            _delete_pending_approval(token)
            payload = {
                "ok": False,
                "resultUnknown": True,
                "submittedOnce": True,
                "retryAllowed": False,
                "action": pending["action"],
                "fdId": pending["fdId"],
                "reason": _redact_tool_message(str(exc)),
                "userMessage": "审批请求可能已经发出，但结果无法确认。请勿重复提交，请先在 OA 页面核对。",
                "nextStep": "请用户先打开 OA 页面查看这条单据的流程状态，不要再次提交本次审批。",
            }
            if state_persistence_warning:
                payload["statePersistenceWarning"] = True
            return _mcp_error(payload)
        state_persistence_warning = False
        try:
            _mark_approval_workitem_submitted(workitem_lock, token)
        except Exception:
            state_persistence_warning = True
        _delete_pending_approval(token)
        user_message = "已提交审批同意。" if pending["action"] == "approve" else "已提交驳回。"
        payload = {
            "ok": True,
            "executed": True,
            "action": pending["action"],
            "fdId": pending["fdId"],
            "userMessage": user_message,
            "result": result,
        }
        if state_persistence_warning:
            payload["statePersistenceWarning"] = True
            payload["retryAllowed"] = False
        return _ok(payload)

    if name == "oa_confirm_batch_approval":
        token = str(args["confirmationToken"])
        confirmation_text = str(args["confirmationText"]).strip()
        terminal_result = _load_batch_terminal_result(token)
        if terminal_result is not None:
            if confirmation_text != BATCH_APPROVAL_CONFIRM_PHRASE:
                raise OAConnectorError(
                    f"确认文本不匹配：需要用户明确发送“{BATCH_APPROVAL_CONFIRM_PHRASE}”"
                )
            return terminal_result
        pending = _load_pending_approval(token)
        if _pending_approval_kind(pending) != "batch":
            raise OAConnectorError("这是单条审批确认，请使用 oa_confirm_approval")
        if confirmation_text != BATCH_APPROVAL_CONFIRM_PHRASE:
            raise OAConnectorError(
                f"确认文本不匹配：需要用户明确发送“{BATCH_APPROVAL_CONFIRM_PHRASE}”"
            )
        pending = _claim_pending_approval(token)
        if _pending_approval_kind(pending) != "batch":
            raise OAConnectorError("这是单条审批确认，请使用 oa_confirm_approval")
        if confirmation_text != BATCH_APPROVAL_CONFIRM_PHRASE:
            raise OAConnectorError(
                f"确认文本不匹配：需要用户明确发送“{BATCH_APPROVAL_CONFIRM_PHRASE}”"
            )
        items = pending.get("items")
        if not isinstance(items, list) or not items or len(items) > MAX_BATCH_APPROVAL_ITEMS:
            _delete_pending_approval(token)
            raise OAConnectorError("批量审批确认状态不完整，请重新准备批量审批")
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("fdId"), str)
            or not isinstance(item.get("action"), str)
            or item.get("action") not in {"approve", "reject"}
            or not isinstance(item.get("note"), str)
            or bool(item.get("futureNodeId"))
            or not _approval_binding_complete(item.get("approvalBinding"))
            for item in items
        ):
            _delete_pending_approval(token)
            raise OAConnectorError("批量审批确认状态不完整，请重新准备批量审批")

        completed_items: list[Dict[str, Any]] = []
        for current_index, item in enumerate(items):
            current_item = _batch_item_with_status(item, current_index + 1, "executing")
            try:
                _save_batch_progress(
                    token,
                    pending,
                    status="executing",
                    completed_items=completed_items,
                    current_item=current_item,
                )
            except Exception as exc:
                payload = _batch_stopped_payload(
                    pending,
                    items,
                    completed_items,
                    current_index,
                    str(exc),
                    result_unknown=False,
                    submitted_once=False,
                )
                payload["statePersistenceWarning"] = True
                payload["reason"] = "本机无法安全保存批量审批进度"
                payload["nextStep"] = "请重新查询 OA 待办后再准备新批次；不要复用本次确认 token。"
                try:
                    _save_batch_terminal_result(
                        token,
                        pending,
                        status="failed",
                        completed_items=completed_items,
                        current_item=payload["currentItem"],
                        payload=payload,
                        is_error=True,
                    )
                except Exception:
                    pass
                return _mcp_error(payload)

            workitem_lock: Optional[Path] = None
            try:
                workitem_lock = _acquire_approval_workitem_lock(pending, item, token)
                confirm_client = _approval_client_for_pending(pending, str(item["fdId"]))
            except Exception as exc:
                _release_approval_workitem_lock(workitem_lock, token)
                payload = _batch_stopped_payload(
                    pending,
                    items,
                    completed_items,
                    current_index,
                    str(exc),
                    result_unknown=False,
                    submitted_once=False,
                )
                try:
                    _save_batch_terminal_result(
                        token,
                        pending,
                        status="failed",
                        completed_items=completed_items,
                        current_item=payload["currentItem"],
                        payload=payload,
                        is_error=True,
                    )
                except Exception:
                    payload["statePersistenceWarning"] = True
                    payload["retryAllowed"] = False
                return _mcp_error(payload)
            try:
                _execute_bound_approval(confirm_client, item)
            except ApprovalResultUnknownError as exc:
                state_persistence_warning = False
                try:
                    _mark_approval_workitem_submitted(workitem_lock, token)
                except Exception:
                    state_persistence_warning = True
                payload = _batch_stopped_payload(
                    pending,
                    items,
                    completed_items,
                    current_index,
                    str(exc),
                    result_unknown=True,
                    submitted_once=True,
                )
                if state_persistence_warning:
                    payload["statePersistenceWarning"] = True
                try:
                    _save_batch_terminal_result(
                        token,
                        pending,
                        status="resultUnknown",
                        completed_items=completed_items,
                        current_item=payload["currentItem"],
                        payload=payload,
                        is_error=True,
                    )
                except Exception:
                    payload["statePersistenceWarning"] = True
                    payload["retryAllowed"] = False
                return _mcp_error(payload)
            except Exception as exc:
                result_unknown = not isinstance(exc, OAConnectorError)
                state_persistence_warning = False
                if result_unknown:
                    try:
                        _mark_approval_workitem_submitted(workitem_lock, token)
                    except Exception:
                        state_persistence_warning = True
                else:
                    _release_approval_workitem_lock(workitem_lock, token)
                payload = _batch_stopped_payload(
                    pending,
                    items,
                    completed_items,
                    current_index,
                    str(exc),
                    result_unknown=result_unknown,
                    submitted_once=result_unknown,
                )
                if state_persistence_warning:
                    payload["statePersistenceWarning"] = True
                try:
                    _save_batch_terminal_result(
                        token,
                        pending,
                        status="resultUnknown" if result_unknown else "failed",
                        completed_items=completed_items,
                        current_item=payload["currentItem"],
                        payload=payload,
                        is_error=True,
                    )
                except Exception:
                    payload["statePersistenceWarning"] = True
                    payload["retryAllowed"] = False
                return _mcp_error(payload)

            state_persistence_warning = False
            try:
                _mark_approval_workitem_submitted(workitem_lock, token)
            except Exception:
                state_persistence_warning = True
            completed_item = _batch_item_with_status(item, current_index + 1, "completed")
            completed_items.append(completed_item)
            try:
                _save_batch_progress(
                    token,
                    pending,
                    status="executing" if current_index + 1 < len(items) else "completed",
                    completed_items=completed_items,
                )
            except Exception:
                state_persistence_warning = True
            if state_persistence_warning:
                payload = _batch_persistence_stopped_payload(
                    items,
                    completed_items,
                    current_index + 1,
                )
                try:
                    _save_batch_terminal_result(
                        token,
                        pending,
                        status="statePersistenceWarning",
                        completed_items=completed_items,
                        current_item=None,
                        payload=payload,
                        is_error=True,
                    )
                except Exception:
                    pass
                return _mcp_error(payload)

        payload = {
            "ok": True,
            "batch": True,
            "executed": True,
            "batchNonTransactional": True,
            "totalCount": len(items),
            "completedCount": len(completed_items),
            "completedItems": completed_items,
            "currentItem": None,
            "notAttemptedCount": 0,
            "notAttemptedItems": [],
            "userMessage": f"批量审批已完成，共 {len(completed_items)} 条。",
        }
        try:
            _save_batch_terminal_result(
                token,
                pending,
                status="completed",
                completed_items=completed_items,
                current_item=None,
                payload=payload,
                is_error=False,
            )
        except Exception:
            payload["statePersistenceWarning"] = True
            payload["retryAllowed"] = False
        return _ok(payload)

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
            client.assert_logged_in()
            batch_args = dict(args)
            queries = list(batch_args.pop("queries"))
            batch_args.pop("session", None)
            return _ok(client.batch_search_objects(queries=queries, **batch_args))

    # Legacy tools: allow baseUrl/insecure
    client = _client(session=session, base_url=args.get("baseUrl"), insecure=insecure)

    if name == "oa_auth_status":
        if hasattr(client, "auth_status"):
            status = client.auth_status()
        else:
            client.assert_logged_in()
            status = {"ok": True}
        status["session"] = session
        status["baseUrl"] = client.base_url
        meta = _saved_session_meta(session)
        status["autoLoginEnabled"] = bool(meta.get("autoLoginEnabled"))
        status["autoLoginCleanupRequired"] = bool(meta.get("credentialCleanupFailed"))
        status["autoLoginRequiresManualAuth"] = bool(meta.get("autoLoginRequiresManualAuth"))
        if not status.get("loginAs") and meta.get("loginAccount"):
            status["loginAs"] = str(meta["loginAccount"])
            status["identityAvailable"] = True
            status["identitySource"] = "savedLoginAccount"
        return _ok(status)
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
    if name == "oa_prepare_batch_approval":
        requested_items = _normalize_batch_approval_items(args.get("items"))
        base_url = str(args.get("baseUrl") or client.base_url)
        login_binding = _approval_login_binding(session, base_url)
        todo_items = client.list_todos(page=1, page_size=200)
        todos_by_id = {todo.fd_id: todo.to_dict() for todo in todo_items}
        prepared_items: list[Dict[str, Any]] = []

        for index, requested in enumerate(requested_items, start=1):
            todo = todos_by_id.get(requested["fdId"])
            if todo is None:
                raise OAConnectorError(f"批量审批第 {index} 条不在当前登录账号的待审批列表中")
            capability = client.validate_approval_action(
                requested["fdId"],
                requested["action"],
                require_in_todo=False,
            )
            approval_binding = capability.get("approvalBinding")
            if not _approval_binding_complete(approval_binding):
                raise OAConnectorError(f"批量审批第 {index} 条无法确认当前审批任务，请重新查看待办")
            raw = todo.get("raw") or {}
            detail_path = str(todo.get("detailPath") or "")
            prepared_items.append(
                {
                    **requested,
                    "subject": todo.get("subject") or capability.get("title") or "",
                    "currentNode": _plain_text(raw.get("nodeName")),
                    "currentHandler": _plain_text(raw.get("handlerName")),
                    "detailTitle": capability.get("title", ""),
                    "detailUrl": _absolute_url(base_url, detail_path) if detail_path else "",
                    "approvalBinding": approval_binding,
                }
            )

        if _approval_login_binding(session, base_url) != login_binding:
            raise OAConnectorError("OA 登录账号已变化，请重新准备批量审批")

        pending = {
            "kind": "batch",
            "session": session,
            "baseUrl": base_url,
            "insecure": insecure,
            "loginBinding": login_binding,
            "items": prepared_items,
        }
        token = _save_pending_approval(pending)
        public_items = [
            _batch_item_public(item, index)
            for index, item in enumerate(prepared_items, start=1)
        ]
        approve_count = sum(item["action"] == "approve" for item in prepared_items)
        reject_count = len(prepared_items) - approve_count
        return _ok(
            {
                "ok": True,
                "batch": True,
                "requiresUserConfirmation": True,
                "confirmationToken": token,
                "confirmationPhrase": BATCH_APPROVAL_CONFIRM_PHRASE,
                "summary": {
                    "totalCount": len(public_items),
                    "approveCount": approve_count,
                    "rejectCount": reject_count,
                    "items": public_items,
                },
                "permissionCheck": {
                    "ok": True,
                    "checkedCount": len(public_items),
                    "actionAvailable": True,
                    "evidence": "全部单据都属于当前登录账号的 OA 待审批清单，且详情页确认当前节点支持所选动作。执行每一条前还会再次校验。",
                },
                "batchNonTransactional": True,
                "warning": "批量审批会按清单顺序逐条提交，不是一次性事务；如果中途失败，前面已完成的项目不会回滚，后续项目不会执行。",
                "nextStep": f"请把 summary 和 warning 整理给用户确认。用户明确回复“{BATCH_APPROVAL_CONFIRM_PHRASE}”后，调用 oa_confirm_batch_approval。",
            }
        )
    if name == "oa_prepare_approval":
        if "futureNodeId" in args:
            raise OAConnectorError("MCP 正式审批不支持手工指定下一节点，请在 OA 页面处理")
        fd_id = str(args["fdId"])
        action = str(args["action"])
        note = str(args["note"]).strip()
        if not note:
            raise OAConnectorError("审批备注不能为空")
        action_label = _approval_action_label(action)
        todo = _find_current_todo(client, fd_id)
        capability = client.validate_approval_action(fd_id, action, require_in_todo=False)
        approval_binding = capability.get("approvalBinding")
        if not _approval_binding_complete(approval_binding):
            raise OAConnectorError("无法确认当前审批任务，请重新查看这条待办")
        raw = todo.get("raw") or {}
        base_url = str(args.get("baseUrl") or client.base_url)
        pending = {
            "kind": "single",
            "session": session,
            "baseUrl": base_url,
            "insecure": insecure,
            "fdId": fd_id,
            "action": action,
            "note": note,
            "loginBinding": _approval_login_binding(session, base_url),
            "approvalBinding": approval_binding,
        }
        token = _save_pending_approval(pending)
        summary = {
            "ok": True,
            "requiresUserConfirmation": True,
            "confirmationToken": token,
            "confirmationPhrase": _approval_confirm_phrase(action),
            "summary": {
                "fdId": fd_id,
                "subject": todo.get("subject") or capability.get("title") or "",
                "action": action,
                "actionLabel": action_label,
                "note": note,
                "currentNode": _plain_text(raw.get("nodeName")),
                "currentHandler": _plain_text(raw.get("handlerName")),
                "detailTitle": capability.get("title", ""),
                "detailUrl": _absolute_url(base_url, str(todo.get("detailPath") or "")),
            },
            "permissionCheck": {
                "ok": True,
                "actionAvailable": True,
                "evidence": "该单据属于当前登录账号的 OA 待审批清单，且详情页确认当前节点支持本次审批动作；执行前会再次校验。",
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
            return _response(message_id, {"tools": _listed_tools()})
        if method == "tools/call":
            tool_args = dict(params.get("arguments") or {})
            tool_name = str(params["name"])
            try:
                result = call_tool(tool_name, tool_args)
            except Exception as exc:
                session = _session(tool_args)
                if (
                    _can_retry_after_auto_login(tool_name)
                    and _auth_required_reason(str(exc))
                    and _auto_login_available(session)
                    and _try_auto_login(session)
                ):
                    try:
                        result = call_tool(tool_name, tool_args)
                    except Exception as retry_exc:
                        if _auth_required_reason(str(retry_exc)):
                            _block_auto_login(session)
                        result = _tool_error(str(retry_exc), session=session)
                else:
                    result = _tool_error(str(exc), session=session)
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
