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


if __name__ == "__main__":
    unittest.main()
