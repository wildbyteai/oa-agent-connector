from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .client import OAClient


ClientFactory = Callable[..., OAClient]


def _safe_session_name(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_"))[:32]
    return f"{cleaned or 'session'}-{digest}"


def _env_flag(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_insecure_auth() -> bool:
    return _env_flag("OA_AGENT_ALLOW_INSECURE_AUTH")


def _validate_auth_transport(base_url: str, insecure: bool) -> None:
    scheme = urllib.parse.urlparse(str(base_url)).scheme.lower()
    if scheme != "https" or insecure:
        if not _allow_insecure_auth():
            raise ValueError("本机授权默认要求 HTTPS OA 地址且不允许跳过证书校验；如确需 HTTP/跳过校验，请由管理员显式设置 OA_AGENT_ALLOW_INSECURE_AUTH=1")


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _expected_auth_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{int(port)}/authorize?state={urllib.parse.quote(token)}"


def _is_expected_auth_url(auth_url: str, token: str, port: int) -> bool:
    try:
        parsed = urllib.parse.urlparse(str(auth_url))
        query = urllib.parse.parse_qs(parsed.query)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.path == "/authorize"
        and parsed.port == int(port)
        and (query.get("state") or [""])[0] == token
    )


def _session_paths(state_dir: str, session: str) -> Dict[str, Path]:
    root = Path(state_dir).expanduser()
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


def _save_session(state_dir: str, session: str, base_url: str) -> None:
    paths = _session_paths(state_dir, session)
    _ensure_private_dir(paths["root"])
    paths["meta"].write_text(json.dumps({"baseUrl": base_url}, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        paths["meta"].chmod(0o600)
    except OSError:
        pass


def _safe_token(token: str) -> str:
    safe = "".join(ch for ch in token if ch.isalnum() or ch in ("-", "_"))
    if not safe:
        raise ValueError("authToken is invalid")
    return safe


def _auth_dir(state_dir: str) -> Path:
    return Path(state_dir).expanduser() / "local-auth"


def local_auth_status_path(state_dir: str, token: str) -> Path:
    return _auth_dir(state_dir) / f"{_safe_token(token)}.json"


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _redact_error(message: str) -> str:
    text = str(message or "")
    text = re.sub(
        r"(?i)(cookie|set-cookie|jsessionid|authorization|password|j_password)\s*[:=]\s*\S.*",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def read_local_auth_status(state_dir: str, token: str) -> Dict[str, Any]:
    path = local_auth_status_path(state_dir, token)
    if not path.exists():
        return {"ok": False, "status": "notFound", "authToken": token}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "unreadable", "authToken": token}
    if data.get("status") == "pending" and int(data.get("expiresAt") or 0) < int(time.time()):
        data = dict(data)
        data["status"] = "expired"
        data["ok"] = False
    return data


def _read_status_file(path: Path, token: str) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "status": "notFound", "authToken": token}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "unreadable", "authToken": token}


def _html_page(title: str, body: str) -> bytes:
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-store">
  <title>{html.escape(title)}</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f6f7f9;
      color: #1f2328;
    }}
    main {{
      max-width: 420px;
      margin: 8vh auto;
      padding: 24px;
      background: #fff;
      border: 1px solid #d8dee4;
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(140, 149, 159, 0.16);
    }}
    h1 {{ font-size: 22px; margin: 0 0 16px; }}
    p {{ line-height: 1.55; }}
    label {{ display: block; margin: 14px 0 6px; font-weight: 600; }}
    input {{
      box-sizing: border-box;
      width: 100%;
      padding: 10px 12px;
      border: 1px solid #d0d7de;
      border-radius: 6px;
      font-size: 15px;
    }}
    button {{
      width: 100%;
      margin-top: 20px;
      padding: 11px 14px;
      border: 0;
      border-radius: 6px;
      background: #0969da;
      color: #fff;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }}
    .muted {{ color: #57606a; font-size: 14px; }}
    .error {{ color: #b42318; }}
    .success {{ color: #1a7f37; }}
    code {{ word-break: break-all; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
    return page.encode("utf-8")


def _form_html(base_url: str, session: str, token: str, error: str = "") -> bytes:
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    body = f"""
<h1>OA 授权登录</h1>
<p class="muted">请在本机页面输入 OA 账号和密码。密码只用于本次登录，不会保存，也不会写入聊天记录。若浏览器询问是否保存密码，请选择不保存。</p>
{error_html}
<form method="post" action="/authorize" autocomplete="off">
  <input type="hidden" name="state" value="{html.escape(token)}">
  <label>OA 地址</label>
  <input value="{html.escape(base_url)}" readonly>
  <label>会话</label>
  <input value="{html.escape(session)}" readonly>
  <label for="username">OA 账号</label>
  <input id="username" name="username" autocomplete="off" required autofocus>
  <label for="password">OA 密码</label>
  <input id="password" name="password" type="password" autocomplete="off" required>
  <button type="submit">授权登录</button>
</form>"""
    return _html_page("OA 授权登录", body)


def _message_html(title: str, message: str, success: bool = False) -> bytes:
    cls = "success" if success else "error"
    body = f"""
<h1>{html.escape(title)}</h1>
<p class="{cls}">{html.escape(message)}</p>
<p class="muted">可以关闭这个页面，回到 Agent 继续使用。</p>"""
    return _html_page(title, body)


def serve_local_auth(
    *,
    base_url: str,
    session: str,
    state_dir: str,
    token: str,
    port: int = 0,
    expires_in: int = 600,
    status_file: Optional[str] = None,
    insecure: bool = False,
    client_factory: ClientFactory = OAClient,
) -> int:
    _validate_auth_transport(base_url, insecure)
    expires_in = max(60, min(int(expires_in), 1800))
    expires_at = int(time.time()) + expires_in
    status_path = Path(status_file).expanduser() if status_file else local_auth_status_path(state_dir, token)
    paths = _session_paths(state_dir, session)

    class Handler(BaseHTTPRequestHandler):
        server_version = "OAAgentLocalAuth/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, content: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/authorize" or (query.get("state") or [""])[0] != token:
                self._send(404, _message_html("链接不可用", "这个授权链接无效或已经过期。"))
                return
            if int(time.time()) > expires_at:
                self._send(410, _message_html("授权已过期", "请回到 Agent 重新发起 OA 授权。"))
                return
            self._send(200, _form_html(base_url, session, token))

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/authorize":
                self._send(404, _message_html("链接不可用", "这个授权链接无效或已经过期。"))
                return
            if int(time.time()) > expires_at:
                self._send(410, _message_html("授权已过期", "请回到 Agent 重新发起 OA 授权。"))
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > 20000:
                self._send(400, _message_html("提交失败", "提交内容不完整，请重新打开授权链接。"))
                return
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            form = urllib.parse.parse_qs(raw, keep_blank_values=True)
            if (form.get("state") or [""])[0] != token:
                self._send(403, _message_html("提交失败", "授权校验失败，请重新打开授权链接。"))
                return
            username = (form.get("username") or [""])[0].strip()
            password = (form.get("password") or [""])[0]
            if not username or not password:
                self._send(400, _form_html(base_url, session, token, "请填写 OA 账号和密码。"))
                return
            try:
                client = client_factory(base_url, cookie_file=str(paths["cookie"]), verify_tls=not insecure)
                client.login(username, password)
                _save_session(state_dir, session, base_url)
                _write_json(
                    status_path,
                    {
                        "ok": True,
                        "status": "success",
                        "authToken": token,
                        "session": session,
                        "baseUrl": base_url,
                        "completedAt": int(time.time()),
                    },
                )
                setattr(self.server, "finished", True)
                self._send(200, _message_html("授权成功", "OA 授权已完成。", success=True))
            except Exception as exc:
                _write_json(
                    status_path,
                    {
                        "ok": False,
                        "status": "pending",
                        "authToken": token,
                        "authUrl": auth_url,
                        "session": session,
                        "baseUrl": base_url,
                        "expiresAt": expires_at,
                        "lastError": _redact_error(str(exc)),
                        "updatedAt": int(time.time()),
                    },
                )
                self._send(400, _form_html(base_url, session, token, "登录失败，请检查账号或密码后重试。"))

    httpd = ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    httpd.timeout = 1
    actual_port = int(httpd.server_address[1])
    auth_url = f"http://127.0.0.1:{actual_port}/authorize?state={urllib.parse.quote(token)}"
    _write_json(
        status_path,
        {
            "ok": False,
            "status": "pending",
            "authToken": token,
            "authUrl": auth_url,
            "session": session,
            "baseUrl": base_url,
            "expiresAt": expires_at,
            "pid": os.getpid(),
        },
    )
    try:
        setattr(httpd, "finished", False)
        while not bool(getattr(httpd, "finished", False)):
            if int(time.time()) > expires_at:
                current = _read_status_file(status_path, token)
                if current.get("status") == "pending":
                    _write_json(
                        status_path,
                        {
                            "ok": False,
                            "status": "expired",
                            "authToken": token,
                            "session": session,
                            "baseUrl": base_url,
                            "expiresAt": expires_at,
                        },
                    )
                return 0
            httpd.handle_request()
    finally:
        httpd.server_close()
    return 0


def begin_local_auth(
    *,
    base_url: str,
    session: str,
    state_dir: str,
    insecure: bool = False,
    expires_in: int = 600,
) -> Dict[str, Any]:
    _validate_auth_transport(base_url, insecure)
    token = secrets.token_urlsafe(24)
    status_path = local_auth_status_path(state_dir, token)
    port = _reserve_loopback_port()
    auth_url = _expected_auth_url(port, token)
    cmd = [
        sys.executable,
        "-m",
        "oa_agent_connector.local_auth",
        "serve",
        "--base-url",
        base_url,
        "--session",
        session,
        "--state-dir",
        str(Path(state_dir).expanduser()),
        "--token",
        token,
        "--port",
        str(port),
        "--expires-in",
        str(max(60, min(int(expires_in), 1800))),
        "--status-file",
        str(status_path),
    ]
    if insecure:
        cmd.append("--insecure")

    env = os.environ.copy()
    env["OA_AGENT_STATE_DIR"] = str(Path(state_dir).expanduser())
    popen_kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **popen_kwargs)

    deadline = time.time() + 5
    last_status: Dict[str, Any] = {"status": "starting"}
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("本机授权页面启动失败")
        last_status = read_local_auth_status(state_dir, token)
        if last_status.get("status") == "pending" and last_status.get("authUrl"):
            if not _is_expected_auth_url(str(last_status["authUrl"]), token, port):
                raise RuntimeError("本机授权页面返回了无效授权链接")
            return {
                "ok": True,
                "authRequired": True,
                "authUrl": auth_url,
                "authToken": token,
                "session": session,
                "baseUrl": base_url,
                "expiresAt": last_status.get("expiresAt"),
                "expiresInSeconds": max(60, min(int(expires_in), 1800)),
                "nextStep": "请让用户点击 authUrl，在本机页面输入 OA 账号和密码；授权成功后调用 oa_auth_status 或重试原操作。",
            }
        time.sleep(0.05)
    raise RuntimeError(f"本机授权页面启动超时: {last_status.get('status')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa-agent-local-auth")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--base-url", required=True)
    serve.add_argument("--session", required=True)
    serve.add_argument("--state-dir", required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--expires-in", type=int, default=600)
    serve.add_argument("--status-file")
    serve.add_argument("--insecure", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return serve_local_auth(
            base_url=args.base_url,
            session=args.session,
            state_dir=args.state_dir,
            token=args.token,
            port=args.port,
            expires_in=args.expires_in,
            status_file=args.status_file,
            insecure=args.insecure,
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
