from __future__ import annotations

import hashlib
import json
import urllib.parse
from typing import Any, Optional


class CredentialStoreError(RuntimeError):
    pass


class SystemCredentialStore:
    """Store OA passwords in the operating system credential vault."""

    _KEYRING_USERNAME = "oa-login"

    def __init__(self, backend: Optional[Any] = None, namespace: str = "default"):
        self._provided_backend = backend
        self._namespace = str(namespace or "default")

    def save(self, base_url: str, session: str, username: str, password: str) -> None:
        if not username or not password:
            raise CredentialStoreError("账号或密码为空，无法启用自动登录")
        backend = self._backend()
        payload = json.dumps(
            {
                "version": 1,
                "baseUrl": self._normalized_base_url(base_url),
                "username": username,
                "password": password,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            backend.set_password(self._service_name(session), self._KEYRING_USERNAME, payload)
        except Exception as exc:
            raise CredentialStoreError("系统密码保险箱保存失败") from exc

    def load(self, base_url: str, session: str, username: str) -> Optional[str]:
        if not username:
            return None
        backend = self._backend()
        try:
            value = backend.get_password(self._service_name(session), self._KEYRING_USERNAME)
        except Exception as exc:
            raise CredentialStoreError("系统密码保险箱读取失败") from exc
        if not value:
            return None
        try:
            payload = json.loads(str(value))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CredentialStoreError("系统密码保险箱中的登录信息无法读取") from exc
        if not isinstance(payload, dict):
            return None
        if payload.get("baseUrl") != self._normalized_base_url(base_url):
            return None
        if str(payload.get("username") or "") != username:
            return None
        password = payload.get("password")
        return str(password) if password else None

    def delete(self, base_url: str, session: str, username: str) -> None:
        backend = self._backend()
        try:
            existing = backend.get_password(self._service_name(session), self._KEYRING_USERNAME)
            if existing is None:
                return
            backend.delete_password(self._service_name(session), self._KEYRING_USERNAME)
        except Exception as exc:
            raise CredentialStoreError("系统密码保险箱清理失败") from exc

    def _backend(self) -> Any:
        if self._provided_backend is not None:
            return self._provided_backend
        try:
            import keyring

            backend = keyring.get_keyring()
        except Exception as exc:
            raise CredentialStoreError("系统密码保险箱不可用") from exc
        try:
            priority = float(getattr(backend, "priority", 0) or 0)
        except Exception as exc:
            raise CredentialStoreError("系统密码保险箱不可用") from exc
        if priority <= 0:
            raise CredentialStoreError("系统密码保险箱不可用")
        backend_module = type(backend).__module__
        secure_backend_prefixes = (
            "keyring.backends.macOS",
            "keyring.backends.Windows",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        )
        if not backend_module.startswith(secure_backend_prefixes):
            raise CredentialStoreError("当前密码存储方式不符合安全要求")
        return backend

    @staticmethod
    def _normalized_base_url(base_url: str) -> str:
        parsed = urllib.parse.urlparse(str(base_url).strip())
        return urllib.parse.urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                "/" + parsed.path.strip("/") if parsed.path.strip("/") else "",
                "",
                "",
                "",
            )
        )

    def _service_name(self, session: str) -> str:
        digest = hashlib.sha256(
            f"{self._namespace}\n{session}".encode("utf-8")
        ).hexdigest()[:24]
        return f"oa-agent-connector:{digest}"
