import json
import os
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

from oa_agent_connector import local_auth


class LocalAuthTest(unittest.TestCase):
    def test_error_redaction_hides_json_and_url_encoded_passwords(self):
        messages = [
            '{"password":"json-secret"}',
            "j_password%3Dencoded-secret%26username%3Du001",
            "%22password%22%3A%22encoded-json-secret%22",
        ]

        for message in messages:
            with self.subTest(message=message):
                redacted = local_auth._redact_error(message)
                self.assertIn("敏感内容已隐藏", redacted)
                self.assertNotIn("secret", redacted)

    def test_local_auth_can_store_credential_for_automatic_relogin(self):
        seen = {}

        class FakeAuthClient:
            def __init__(self, base_url, cookie_file=None, verify_tls=True):
                self.cookie_file = cookie_file

            def login(self, username, password):
                Path(self.cookie_file).write_text("cookie", encoding="utf-8")
                return True

        class FakeCredentialStore:
            def save(self, base_url, session, username, password):
                seen.update(
                    {
                        "base_url": base_url,
                        "session": session,
                        "username": username,
                        "password": password,
                    }
                )

            def delete(self, base_url, session, username):
                raise AssertionError("remembered login should overwrite the session credential")

        with tempfile.TemporaryDirectory() as tmpdir:
            token = "remember-token"
            status_file = Path(tmpdir) / "local-auth" / "remember-token.json"
            thread = threading.Thread(
                target=local_auth.serve_local_auth,
                kwargs={
                    "base_url": "https://example.invalid/oa/",
                    "session": "work",
                    "state_dir": tmpdir,
                    "token": token,
                    "port": 0,
                    "expires_in": 120,
                    "status_file": str(status_file),
                    "client_factory": FakeAuthClient,
                    "credential_store": FakeCredentialStore(),
                },
                daemon=True,
            )
            thread.start()

            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                if status_file.exists():
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                    if status.get("authUrl"):
                        break
                time.sleep(0.05)

            page = urllib.request.urlopen(status["authUrl"], timeout=3).read().decode("utf-8")
            self.assertIn("登录过期后自动登录", page)
            self.assertIn('name="rememberCredential"', page)

            form = urllib.parse.urlencode(
                {
                    "state": token,
                    "username": "u001",
                    "password": "secret-password",
                    "rememberCredential": "1",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                status["authUrl"],
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
            self.assertIn("授权成功", response)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            self.assertEqual(seen["base_url"], "https://example.invalid/oa/")
            self.assertEqual(seen["session"], "work")
            self.assertEqual(seen["username"], "u001")
            self.assertEqual(seen["password"], "secret-password")

            paths = local_auth._session_paths(tmpdir, "work")
            metadata = json.loads(paths["meta"].read_text(encoding="utf-8"))
            final_status = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertTrue(metadata["autoLoginEnabled"])
            self.assertTrue(final_status["autoLoginEnabled"])
            persisted = (
                status_file.read_text(encoding="utf-8")
                + paths["cookie"].read_text(encoding="utf-8")
                + paths["meta"].read_text(encoding="utf-8")
            )
            self.assertNotIn("secret-password", persisted)

    def test_unchecking_remember_reports_system_credential_cleanup_failure(self):
        class FakeAuthClient:
            def __init__(self, base_url, cookie_file=None, verify_tls=True):
                self.cookie_file = cookie_file

            def login(self, username, password):
                Path(self.cookie_file).write_text("cookie", encoding="utf-8")
                return True

        class FailingCredentialStore:
            def delete(self, base_url, session, username):
                raise local_auth.CredentialStoreError("vault locked")

        with tempfile.TemporaryDirectory() as tmpdir:
            local_auth._save_session(
                tmpdir,
                "work",
                "https://example.invalid/oa/",
                login_account="u001",
                auto_login_enabled=True,
            )
            token = "cleanup-token"
            status_file = Path(tmpdir) / "local-auth" / "cleanup-token.json"
            thread = threading.Thread(
                target=local_auth.serve_local_auth,
                kwargs={
                    "base_url": "https://example.invalid/oa/",
                    "session": "work",
                    "state_dir": tmpdir,
                    "token": token,
                    "port": 0,
                    "expires_in": 120,
                    "status_file": str(status_file),
                    "client_factory": FakeAuthClient,
                    "credential_store": FailingCredentialStore(),
                },
                daemon=True,
            )
            thread.start()

            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                if status_file.exists():
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                    if status.get("authUrl"):
                        break
                time.sleep(0.05)

            form = urllib.parse.urlencode(
                {"state": token, "username": "u001", "password": "secret-password"}
            ).encode("utf-8")
            request = urllib.request.Request(
                status["authUrl"],
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
            thread.join(timeout=5)

            metadata = json.loads(local_auth._session_paths(tmpdir, "work")["meta"].read_text(encoding="utf-8"))
            final_status = json.loads(status_file.read_text(encoding="utf-8"))

        self.assertIn("未能清理", response)
        self.assertFalse(metadata["autoLoginEnabled"])
        self.assertTrue(metadata["credentialCleanupFailed"])
        self.assertTrue(final_status["credentialCleanupFailed"])

    def test_local_auth_page_logs_in_without_persisting_password(self):
        seen = {}

        class FakeAuthClient:
            def __init__(self, base_url, cookie_file=None, verify_tls=True):
                self.base_url = base_url
                self.cookie_file = cookie_file
                self.verify_tls = verify_tls

            def login(self, username, password):
                seen["username"] = username
                seen["password"] = password
                Path(self.cookie_file).write_text(f"cookie-for-{username}", encoding="utf-8")
                return True

        def factory(base_url, cookie_file=None, verify_tls=True):
            seen["base_url"] = base_url
            seen["cookie_file"] = cookie_file
            seen["verify_tls"] = verify_tls
            return FakeAuthClient(base_url, cookie_file=cookie_file, verify_tls=verify_tls)

        with tempfile.TemporaryDirectory() as tmpdir:
            token = "test-token"
            status_file = Path(tmpdir) / "local-auth" / "test-token.json"
            thread = threading.Thread(
                target=local_auth.serve_local_auth,
                kwargs={
                    "base_url": "https://example.invalid/oa/",
                    "session": "work",
                    "state_dir": tmpdir,
                    "token": token,
                    "port": 0,
                    "expires_in": 120,
                    "status_file": str(status_file),
                    "client_factory": factory,
                },
                daemon=True,
            )
            thread.start()

            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                if status_file.exists():
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                    if status.get("authUrl"):
                        break
                time.sleep(0.05)
            self.assertEqual(status.get("status"), "pending")
            self.assertTrue(str(status["authUrl"]).startswith("http://127.0.0.1:"))

            page = urllib.request.urlopen(status["authUrl"], timeout=3).read().decode("utf-8")
            self.assertIn("OA 授权登录", page)
            self.assertIn("https://example.invalid/oa/", page)
            self.assertIn('autocomplete="off"', page)

            form = urllib.parse.urlencode(
                {
                    "state": token,
                    "username": "u001",
                    "password": "secret-password",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                status["authUrl"],
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
            self.assertIn("授权成功", response)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            final_status = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(final_status["status"], "success")
            self.assertEqual(final_status["session"], "work")
            self.assertNotIn("loginAccount", final_status)
            self.assertEqual(seen["username"], "u001")
            self.assertEqual(seen["password"], "secret-password")
            self.assertEqual(seen["base_url"], "https://example.invalid/oa/")

            paths = local_auth._session_paths(tmpdir, "work")
            self.assertTrue(paths["cookie"].exists())
            self.assertTrue(paths["meta"].exists())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(paths["root"].stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(status_file.parent.stat().st_mode), 0o700)
            persisted = (
                status_file.read_text(encoding="utf-8")
                + paths["cookie"].read_text(encoding="utf-8")
                + paths["meta"].read_text(encoding="utf-8")
            )
            self.assertNotIn("secret-password", persisted)

    def test_failed_login_keeps_pending_status_and_redacts_error(self):
        attempts = {"count": 0}

        class RetryAuthClient:
            def __init__(self, base_url, cookie_file=None, verify_tls=True):
                self.cookie_file = cookie_file

            def login(self, username, password):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise RuntimeError("Password: secret-value; Cookie: abc; <html>bad</html>")
                Path(self.cookie_file).write_text("cookie", encoding="utf-8")
                return True

        with tempfile.TemporaryDirectory() as tmpdir:
            token = "retry-token"
            status_file = Path(tmpdir) / "local-auth" / "retry-token.json"
            thread = threading.Thread(
                target=local_auth.serve_local_auth,
                kwargs={
                    "base_url": "https://example.invalid/oa/",
                    "session": "work",
                    "state_dir": tmpdir,
                    "token": token,
                    "port": 0,
                    "expires_in": 120,
                    "status_file": str(status_file),
                    "client_factory": RetryAuthClient,
                },
                daemon=True,
            )
            thread.start()

            deadline = time.time() + 5
            status = {}
            while time.time() < deadline:
                if status_file.exists():
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                    if status.get("authUrl"):
                        break
                time.sleep(0.05)
            auth_url = status["authUrl"]

            form = urllib.parse.urlencode({"state": token, "username": "u001", "password": "bad"}).encode("utf-8")
            request = urllib.request.Request(auth_url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as http_error:
                urllib.request.urlopen(request, timeout=5).read()
            http_error.exception.close()

            failed_status = json.loads(status_file.read_text(encoding="utf-8"))
            self.assertEqual(failed_status["status"], "pending")
            self.assertIn("expiresAt", failed_status)
            redacted = json.dumps(failed_status, ensure_ascii=False)
            self.assertNotIn("secret-value", redacted)
            self.assertNotIn("abc", redacted)
            self.assertNotIn("<html>", redacted)

            form = urllib.parse.urlencode({"state": token, "username": "u001", "password": "good"}).encode("utf-8")
            request = urllib.request.Request(auth_url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
            response = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
            self.assertIn("授权成功", response)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

    def test_begin_auth_rejects_tampered_status_auth_url(self):
        class FakeProcess:
            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(local_auth, "_reserve_loopback_port", return_value=12345):
                with patch.object(local_auth.subprocess, "Popen", return_value=FakeProcess()):
                    with patch.object(
                        local_auth,
                        "read_local_auth_status",
                        return_value={
                            "ok": False,
                            "status": "pending",
                            "authUrl": "http://127.0.0.1:54321/authorize?state=wrong",
                        },
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            local_auth.begin_local_auth(
                                base_url="https://example.invalid/oa/",
                                session="work",
                                state_dir=tmpdir,
                            )
        self.assertIn("无效授权链接", str(ctx.exception))

    def test_begin_auth_rejects_http_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_ALLOW_INSECURE_AUTH": ""}, clear=False):
                with self.assertRaises(ValueError) as ctx:
                    local_auth.begin_local_auth(
                        base_url="http://example.invalid/oa/",
                        session="work",
                        state_dir=tmpdir,
                    )
        self.assertIn("HTTPS", str(ctx.exception))

    def test_begin_auth_rejects_https_insecure_without_admin_opt_in(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_ALLOW_INSECURE_AUTH": ""}, clear=False):
                with self.assertRaises(ValueError) as ctx:
                    local_auth.begin_local_auth(
                        base_url="https://example.invalid/oa/",
                        session="work",
                        state_dir=tmpdir,
                        insecure=True,
                    )
        self.assertIn("证书校验", str(ctx.exception))

    def test_http_local_auth_form_shows_transport_warning(self):
        html = local_auth._form_html("http://example.invalid/oa/", "work", "token", insecure=True).decode("utf-8")
        self.assertIn("请确认这是公司 OA 登录页面", html)

    def test_begin_auth_allows_http_with_dynamic_insecure_override(self):
        class FakeProcess:
            def poll(self):
                return None

        captured = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProcess()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(local_auth.secrets, "token_urlsafe", return_value="token-123"):
                with patch.object(local_auth, "_reserve_loopback_port", return_value=12345):
                    with patch.object(local_auth.subprocess, "Popen", side_effect=fake_popen):
                        with patch.object(
                            local_auth,
                            "read_local_auth_status",
                            return_value={
                                "ok": False,
                                "status": "pending",
                                "authUrl": "http://127.0.0.1:12345/authorize?state=token-123",
                                "expiresAt": 123,
                            },
                        ):
                            result = local_auth.begin_local_auth(
                                base_url="http://example.invalid/oa/",
                                session="work",
                                state_dir=tmpdir,
                                insecure=True,
                            )
        self.assertEqual(result["authUrl"], "http://127.0.0.1:12345/authorize?state=token-123")
        self.assertIn("--insecure", captured["cmd"])

    def test_begin_auth_rejects_unsupported_scheme_even_with_insecure_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ValueError) as ctx:
                local_auth.begin_local_auth(
                    base_url="ftp://example.invalid/oa/",
                    session="work",
                    state_dir=tmpdir,
                    insecure=True,
                )
        self.assertIn("HTTPS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
