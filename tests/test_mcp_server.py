import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from oa_agent_connector import mcp_server
from oa_agent_connector.client import (
    ApprovalResultUnknownError,
    ApprovalStateChangedError,
    OAConnectorError,
    OATodo,
)


def fake_approval_binding(action):
    return {
        "processId": "process-1",
        "taskId": "task-1",
        "nodeId": "node-1",
        "activityType": "reviewWorkitem",
        "operationType": "handler_pass" if action == "approve" else "handler_refuse",
    }


class FakeClient:
    def __init__(self, base_url, cookie_file=None, verify_tls=True):
        self.base_url = base_url.rstrip("/") + "/"
        self.cookie_file = cookie_file
        self.verify_tls = verify_tls

    def login(self, username, password):
        Path(self.cookie_file).write_text("cookie", encoding="utf-8")
        return True

    def assert_logged_in(self):
        return None

    def list_todos(self, page=1, page_size=20):
        return [
            OATodo(
                "1234567890abcdef1234567890abcdef",
                "采购审批",
                raw={"nodeName": "<div>部门负责人</div>", "handlerName": "<div>审批人A</div>"},
            )
        ]

    def get_detail(self, fd_id, require_in_todo=True):
        return {"fdId": fd_id, "title": "采购审批", "text": "采购审批详情"}

    def validate_approval_action(self, fd_id, action, require_in_todo=True):
        return {
            "fdId": fd_id,
            "action": action,
            "operationAvailable": True,
            "title": "采购审批",
            "url": "https://example.invalid/oa/view",
            "formSource": "view",
            "approvalBinding": fake_approval_binding(action),
        }

    def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
        if execute and expected_binding != fake_approval_binding("approve"):
            raise AssertionError("approval binding was not preserved")
        return {"dryRun": not execute, "fdId": fd_id, "result": fd_id}

    def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
        if execute and expected_binding != fake_approval_binding("reject"):
            raise AssertionError("approval binding was not preserved")
        return {"dryRun": not execute, "fdId": fd_id, "result": fd_id}


class ExpiredAuthClient(FakeClient):
    def list_todos(self, page=1, page_size=20):
        raise RuntimeError("当前 cookie 未登录或已失效，请先 login")


class MCPServerTest(unittest.TestCase):
    def test_expired_cookie_automatically_relogs_in_and_retries_read(self):
        calls = {"login": 0, "load": 0}

        class AutoReloginClient(FakeClient):
            def login(self, username, password):
                calls["login"] += 1
                if username != "u001" or password != "stored-secret":
                    raise AssertionError("unexpected stored credential")
                Path(self.cookie_file).write_text("refreshed-cookie", encoding="utf-8")
                return True

            def list_todos(self, page=1, page_size=20):
                cookie_path = Path(self.cookie_file)
                if not cookie_path.exists() or cookie_path.read_text(encoding="utf-8") != "refreshed-cookie":
                    raise RuntimeError("当前 cookie 未登录或已失效，请先 login")
                return super().list_todos(page=page, page_size=page_size)

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                calls["load"] += 1
                if base_url != "https://example.invalid/oa/" or session != "work" or username != "u001":
                    raise AssertionError("unexpected credential lookup")
                return "stored-secret"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                meta_path = mcp_server._session_paths("work")["meta"]
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                metadata["autoLoginEnabled"] = True
                meta_path.write_text(json.dumps(metadata), encoding="utf-8")

                with patch.object(mcp_server, "OAClient", AutoReloginClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        response = mcp_server.handle(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": "oa_list_todos",
                                    "arguments": {"session": "work"},
                                },
                            }
                        )

        self.assertFalse(response["result"].get("isError", False))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["items"][0]["subject"], "采购审批")
        self.assertEqual(calls, {"login": 1, "load": 1})
        self.assertNotIn("stored-secret", json.dumps(response, ensure_ascii=False))

    def test_confirm_approval_relogs_in_before_permission_check_and_executes_once(self):
        calls = {"login": 0, "reject": 0}

        class ConfirmReloginClient(FakeClient):
            def login(self, username, password):
                calls["login"] += 1
                if username != "u001" or password != "stored-secret":
                    raise AssertionError("unexpected stored credential")
                Path(self.cookie_file).write_text("refreshed-cookie", encoding="utf-8")
                return True

            def list_todos(self, page=1, page_size=20):
                cookie_path = Path(self.cookie_file)
                if not cookie_path.exists() or cookie_path.read_text(encoding="utf-8") != "refreshed-cookie":
                    raise RuntimeError("当前 cookie 未登录或已失效，请先 login")
                return super().list_todos(page=page, page_size=page_size)

            def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
                calls["reject"] += 1
                return {"dryRun": not execute, "fdId": fd_id, "result": "rejected"}

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                return "stored-secret"

        fd_id = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                token = mcp_server._save_pending_approval(
                    {
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": fd_id,
                        "action": "reject",
                        "note": "资料不完整",
                        "futureNodeId": None,
                        "loginBinding": mcp_server._approval_login_binding(
                            "work",
                            "https://example.invalid/oa/",
                        ),
                        "approvalBinding": fake_approval_binding("reject"),
                    }
                )
                with patch.object(mcp_server, "OAClient", ConfirmReloginClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        response = mcp_server.handle(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": "oa_confirm_approval",
                                    "arguments": {
                                        "confirmationToken": token,
                                        "confirmationText": "确认驳回",
                                    },
                                },
                            }
                        )

        self.assertFalse(response["result"].get("isError", False))
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["executed"])
        self.assertEqual(calls, {"login": 1, "reject": 1})

    def test_confirm_approval_rechecks_login_binding_after_auto_login(self):
        calls = {"approve": 0}
        auth_state = {"ready": False}

        class ReloginClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                if not auth_state["ready"]:
                    raise RuntimeError("当前 cookie 未登录或已失效，请先 login")
                return super().list_todos(page=page, page_size=page_size)

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                return super().approve(
                    fd_id,
                    audit_note,
                    execute=execute,
                    future_node_id=future_node_id,
                    expected_binding=expected_binding,
                )

        def relogin(_session):
            auth_state["ready"] = True
            return True

        fd_id = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                token = mcp_server._save_pending_approval(
                    {
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": fd_id,
                        "action": "approve",
                        "note": "同意",
                        "futureNodeId": None,
                        "loginBinding": "binding-before-login",
                        "approvalBinding": fake_approval_binding("approve"),
                    }
                )
                with patch.object(mcp_server, "OAClient", ReloginClient):
                    with patch.object(mcp_server, "_auto_login_available", return_value=True):
                        with patch.object(mcp_server, "_try_auto_login", side_effect=relogin):
                            with patch.object(
                                mcp_server,
                                "_approval_login_binding",
                                side_effect=["binding-before-login", "binding-after-login"],
                            ):
                                response = mcp_server.handle(
                                    {
                                        "jsonrpc": "2.0",
                                        "id": 1,
                                        "method": "tools/call",
                                        "params": {
                                            "name": "oa_confirm_approval",
                                            "arguments": {
                                                "confirmationToken": token,
                                                "confirmationText": "确认审批",
                                            },
                                        },
                                    }
                                )

        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("登录账号已变化", payload["reason"])
        self.assertEqual(calls["approve"], 0)

    def test_delete_pending_keeps_claim_when_source_delete_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                token = mcp_server._save_pending_approval({"session": "work"})
                source = mcp_server._pending_path(token)
                claimed = mcp_server._pending_claim_path(token)
                claimed.write_bytes(source.read_bytes())
                original_unlink = Path.unlink

                def fail_source_unlink(path, *args, **kwargs):
                    if path == source:
                        raise PermissionError("source is locked")
                    return original_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", new=fail_source_unlink):
                    mcp_server._delete_pending_approval(token)

                source_exists = source.exists()
                claimed_exists = claimed.exists()
                with self.assertRaisesRegex(OAConnectorError, "正在处理或已经使用"):
                    mcp_server._load_pending_approval(token)
                mcp_server._delete_pending_approval(token)

        self.assertTrue(source_exists)
        self.assertTrue(claimed_exists)

    def test_confirm_approval_returns_non_retryable_unknown_result(self):
        calls = {"approve": 0}

        class UnknownResultClient(FakeClient):
            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                raise ApprovalResultUnknownError(
                    "审批请求已经发出，但 OA 没有返回明确结果。请勿重复提交，请先在 OA 页面核对。"
                )

        fd_id = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                token = mcp_server._save_pending_approval(
                    {
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": fd_id,
                        "action": "approve",
                        "note": "同意",
                        "futureNodeId": None,
                        "loginBinding": mcp_server._approval_login_binding(
                            "work",
                            "https://example.invalid/oa/",
                        ),
                        "approvalBinding": fake_approval_binding("approve"),
                    }
                )
                with patch.object(mcp_server, "OAClient", UnknownResultClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_confirm_approval",
                                "arguments": {
                                    "confirmationToken": token,
                                    "confirmationText": "确认审批",
                                },
                            },
                        }
                    )
                pending_exists = mcp_server._pending_path(token).exists()
                claimed_exists = mcp_server._pending_claim_path(token).exists()

        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["resultUnknown"])
        self.assertTrue(payload["submittedOnce"])
        self.assertFalse(payload["retryAllowed"])
        self.assertIn("请勿重复提交", payload["userMessage"])
        self.assertEqual(calls["approve"], 1)
        self.assertFalse(pending_exists)
        self.assertFalse(claimed_exists)

    def test_confirm_approval_rejects_missing_workitem_binding(self):
        calls = {"approve": 0}

        class CountingClient(FakeClient):
            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                return super().approve(
                    fd_id,
                    audit_note,
                    execute=execute,
                    future_node_id=future_node_id,
                    expected_binding=expected_binding,
                )

        fd_id = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                token = mcp_server._save_pending_approval(
                    {
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": fd_id,
                        "action": "approve",
                        "note": "同意",
                        "futureNodeId": None,
                        "loginBinding": mcp_server._approval_login_binding(
                            "work",
                            "https://example.invalid/oa/",
                        ),
                    }
                )
                with patch.object(mcp_server, "OAClient", CountingClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_confirm_approval",
                                "arguments": {
                                    "confirmationToken": token,
                                    "confirmationText": "确认审批",
                                },
                            },
                        }
                    )
                pending_exists = mcp_server._pending_path(token).exists()
                claimed_exists = mcp_server._pending_claim_path(token).exists()

        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("确认状态不完整", payload["reason"])
        self.assertEqual(calls["approve"], 0)
        self.assertFalse(pending_exists)
        self.assertFalse(claimed_exists)

    def test_confirmation_token_can_only_be_claimed_by_one_process(self):
        calls = {"reject": 0}
        calls_lock = threading.Lock()

        class CountingClient(FakeClient):
            def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
                with calls_lock:
                    calls["reject"] += 1
                return {"dryRun": not execute, "fdId": fd_id, "result": "rejected"}

        fd_id = "1234567890abcdef1234567890abcdef"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                token = mcp_server._save_pending_approval(
                    {
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": fd_id,
                        "action": "reject",
                        "note": "资料不完整",
                        "futureNodeId": None,
                        "loginBinding": mcp_server._approval_login_binding(
                            "work",
                            "https://example.invalid/oa/",
                        ),
                        "approvalBinding": fake_approval_binding("reject"),
                    }
                )
                original_load = mcp_server._load_pending_approval
                load_barrier = threading.Barrier(2)

                def synchronized_load(value):
                    pending = original_load(value)
                    load_barrier.wait(timeout=5)
                    return pending

                responses = []

                def confirm(message_id):
                    responses.append(
                        mcp_server.handle(
                            {
                                "jsonrpc": "2.0",
                                "id": message_id,
                                "method": "tools/call",
                                "params": {
                                    "name": "oa_confirm_approval",
                                    "arguments": {
                                        "confirmationToken": token,
                                        "confirmationText": "确认驳回",
                                    },
                                },
                            }
                        )
                    )

                with patch.object(mcp_server, "OAClient", CountingClient):
                    with patch.object(mcp_server, "_load_pending_approval", side_effect=synchronized_load):
                        threads = [threading.Thread(target=confirm, args=(message_id,)) for message_id in (1, 2)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=5)
                            self.assertFalse(thread.is_alive())

        self.assertEqual(calls["reject"], 1)
        self.assertEqual(sum(not response["result"].get("isError", False) for response in responses), 1)

    def test_confirmation_is_rejected_after_switching_login_account(self):
        calls = {"reject": 0}

        class CountingClient(FakeClient):
            def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
                calls["reject"] += 1
                return {"dryRun": not execute, "fdId": fd_id}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", CountingClient):
                    prepared = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_prepare_approval",
                                "arguments": {
                                    "fdId": "1234567890abcdef1234567890abcdef",
                                    "action": "reject",
                                    "note": "资料不完整",
                                    "session": "work",
                                },
                            },
                        }
                    )
                    prepared_payload = json.loads(prepared["result"]["content"][0]["text"])
                    mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u002")
                    confirmed = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_confirm_approval",
                                "arguments": {
                                    "confirmationToken": prepared_payload["confirmationToken"],
                                    "confirmationText": "确认驳回",
                                },
                            },
                        }
                    )

        self.assertTrue(confirmed["result"]["isError"])
        payload = json.loads(confirmed["result"]["content"][0]["text"])
        self.assertIn("登录账号已变化", payload["reason"])
        self.assertEqual(calls["reject"], 0)

    def test_failed_automatic_relogin_uses_cooldown_to_avoid_account_lockout(self):
        calls = {"login": 0, "load": 0}

        class FailedReloginClient(ExpiredAuthClient):
            def login(self, username, password):
                calls["login"] += 1
                raise RuntimeError("登录失败或仍停留在登录页")

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                calls["load"] += 1
                return "stale-secret"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                with patch.object(mcp_server, "OAClient", FailedReloginClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        responses = [
                            mcp_server.handle(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message_id,
                                    "method": "tools/call",
                                    "params": {
                                        "name": "oa_list_todos",
                                        "arguments": {"session": "work"},
                                    },
                                }
                            )
                            for message_id in (1, 2)
                        ]
                metadata = json.loads(mcp_server._session_paths("work")["meta"].read_text(encoding="utf-8"))

        self.assertEqual(calls, {"login": 1, "load": 1})
        self.assertGreater(metadata["autoLoginBlockedUntil"], metadata["autoLoginLastFailedAt"])
        for response in responses:
            self.assertTrue(response["result"]["isError"])
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertTrue(payload["reauthRequired"])
            self.assertNotIn("stale-secret", json.dumps(response, ensure_ascii=False))

    def test_automatic_relogin_is_disabled_after_three_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                meta_path = mcp_server._session_paths("work")["meta"]
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                metadata["autoLoginFailureCount"] = 2
                meta_path.write_text(json.dumps(metadata), encoding="utf-8")

                mcp_server._block_auto_login("work", now=1000)
                final = json.loads(meta_path.read_text(encoding="utf-8"))

        self.assertEqual(final["autoLoginFailureCount"], 3)
        self.assertFalse(final["autoLoginEnabled"])
        self.assertTrue(final["autoLoginRequiresManualAuth"])

    def test_apparently_successful_relogin_that_stays_unauthorized_is_cooled_down(self):
        calls = {"login": 0}

        class StillUnauthorizedClient(ExpiredAuthClient):
            def login(self, username, password):
                calls["login"] += 1
                Path(self.cookie_file).write_text("new-cookie", encoding="utf-8")
                return True

            def assert_logged_in(self):
                raise RuntimeError("当前 cookie 未登录或已失效，请先 login")

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                return "stored-secret"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                with patch.object(mcp_server, "OAClient", StillUnauthorizedClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        for message_id in (1, 2):
                            mcp_server.handle(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message_id,
                                    "method": "tools/call",
                                    "params": {
                                        "name": "oa_list_todos",
                                        "arguments": {"session": "work"},
                                    },
                                }
                            )

                metadata = json.loads(mcp_server._session_paths("work")["meta"].read_text(encoding="utf-8"))

        self.assertEqual(calls["login"], 1)
        self.assertNotIn("autoLoginLastSucceededAt", metadata)

    def test_concurrent_automatic_relogin_uses_one_password_attempt(self):
        calls = {"login": 0}
        calls_lock = threading.Lock()
        start_barrier = threading.Barrier(2)

        class ConcurrentReloginClient(FakeClient):
            def login(self, username, password):
                with calls_lock:
                    calls["login"] += 1
                time.sleep(0.2)
                Path(self.cookie_file).write_text("refreshed-cookie", encoding="utf-8")
                return True

            def assert_logged_in(self):
                cookie_path = Path(self.cookie_file)
                if not cookie_path.exists() or cookie_path.read_text(encoding="utf-8") != "refreshed-cookie":
                    raise RuntimeError("当前 cookie 未登录或已失效，请先 login")

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                return "stored-secret"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                results = []

                def relogin():
                    start_barrier.wait(timeout=5)
                    results.append(mcp_server._try_auto_login("work"))

                with patch.object(mcp_server, "OAClient", ConcurrentReloginClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        threads = [threading.Thread(target=relogin) for _ in range(2)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=5)
                            self.assertFalse(thread.is_alive())

        self.assertEqual(results, [True, True])
        self.assertEqual(calls["login"], 1)

    def test_initialize_and_tools_list(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "oa-agent-connector")
        self.assertEqual(init["result"]["serverInfo"]["version"], "0.2.13")

        with patch.dict("os.environ", {"OA_AGENT_ENABLE_PASSWORD_LOGIN": ""}, clear=False):
            tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("oa_version_status", names)
        self.assertIn("oa_setup_guide", names)
        self.assertIn("oa_begin_auth", names)
        self.assertIn("oa_local_auth_status", names)
        self.assertIn("oa_disable_auto_login", names)
        self.assertNotIn("oa_login", names)
        self.assertIn("oa_list_todos", names)
        self.assertNotIn("password", json.dumps(tools["result"]["tools"], ensure_ascii=False).lower())

    def test_disable_auto_login_deletes_system_credential_but_keeps_cookie(self):
        calls = []

        class FakeCredentialStore:
            def delete(self, base_url, session, username):
                calls.append((base_url, session, username))

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                cookie_path = mcp_server._session_paths("work")["cookie"]
                cookie_path.write_text("active-cookie", encoding="utf-8")
                with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_disable_auto_login",
                                "arguments": {"session": "work"},
                            },
                        }
                    )
                metadata = json.loads(mcp_server._session_paths("work")["meta"].read_text(encoding="utf-8"))
                cookie_text = cookie_path.read_text(encoding="utf-8")

        self.assertFalse(response["result"].get("isError", False))
        self.assertEqual(calls, [("https://example.invalid/oa/", "work", "u001")])
        self.assertFalse(metadata["autoLoginEnabled"])
        self.assertEqual(cookie_text, "active-cookie")

    def test_login_then_list_todos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_AGENT_ENABLE_PASSWORD_LOGIN": "1"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    login = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "https://example.invalid/oa/",
                                    "username": "u",
                                    "password": "p",
                                },
                            },
                        }
                    )
                    self.assertNotIn("error", login)

                    listed = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_list_todos", "arguments": {}},
                        }
                    )
                    text = listed["result"]["content"][0]["text"]
                    payload = json.loads(text)
                    self.assertEqual(payload["items"][0]["subject"], "采购审批")
                    self.assertEqual(payload["items"][0]["detailUrl"], "https://example.invalid/oa/km/review/km_review_main/kmReviewMain.do?method=view&fdId=1234567890abcdef1234567890abcdef")
                    self.assertEqual(
                        payload["approvalHandling"],
                        {
                            "scopeVersion": "standard-approval-v1",
                            "level": 2,
                            "code": "detail_check_required",
                            "label": "需要先核对",
                            "canContinueInChat": False,
                            "message": "请先查看详情，确认是否需要填写或修改业务表单、选择下一流向，或执行转办、沟通、废弃、加签、补签等特殊操作。未确认前不要准备审批。",
                        },
                    )
                    self.assertEqual(
                        payload["versionCheck"],
                        {
                            "recommended": True,
                            "tool": "oa_version_status",
                            "arguments": {},
                            "message": "本次操作完成后检查一次 OA 助手版本。检查结果会在本机缓存；只有发现新版本时才提示用户。",
                        },
                    )

    def test_version_status_reports_update_and_uses_success_cache(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, limit):
                self.limit = limit
                return b'[project]\nname = "oa-agent-connector"\nversion = "0.2.14"\n'

        response = FakeResponse()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch("oa_agent_connector.mcp_server.urllib.request.urlopen", return_value=response) as opened:
                    first = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_version_status", "arguments": {}},
                        }
                    )
                    second = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_version_status", "arguments": {}},
                        }
                    )

        first_payload = json.loads(first["result"]["content"][0]["text"])
        second_payload = json.loads(second["result"]["content"][0]["text"])
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(first_payload["installedVersion"], "0.2.13")
        self.assertEqual(first_payload["latestVersion"], "0.2.14")
        self.assertTrue(first_payload["updateAvailable"])
        self.assertEqual(first_payload["status"], "update_available")
        self.assertTrue(first_payload["agentAction"]["notifyUser"])
        self.assertTrue(first_payload["agentAction"]["requiresUserConfirmation"])
        self.assertNotIn("versionCheck", first_payload)
        self.assertTrue(second_payload["cached"])

    def test_version_status_failure_is_non_blocking_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch(
                    "oa_agent_connector.mcp_server.urllib.request.urlopen",
                    side_effect=OSError("Authorization: Bearer should-not-leak"),
                ):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_version_status", "arguments": {"force": True}},
                        }
                    )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "unknown")
        self.assertIsNone(payload["updateAvailable"])
        self.assertFalse(payload["agentAction"]["notifyUser"])
        self.assertNotIn("should-not-leak", json.dumps(payload, ensure_ascii=False))

    def test_version_status_failure_uses_failure_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    side_effect=OSError("offline"),
                ) as fetch:
                    first = mcp_server._version_status()
                    second = mcp_server._version_status()

        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["status"], "unknown")
        self.assertFalse(second["agentAction"]["notifyUser"])

    def test_version_status_force_bypasses_success_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                mcp_server._write_version_check_cache(
                    {
                        "status": "success",
                        "checkedAt": int(time.time()),
                        "latestVersion": "0.2.14",
                    }
                )
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.15",
                ) as fetch:
                    cached = mcp_server._version_status()
                    forced = mcp_server._version_status(force=True)

        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["latestVersion"], "0.2.14")
        self.assertFalse(forced["cached"])
        self.assertEqual(forced["latestVersion"], "0.2.15")

    def test_version_status_refreshes_expired_success_and_failure_cache(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                mcp_server._write_version_check_cache(
                    {
                        "status": "success",
                        "checkedAt": now - mcp_server.VERSION_CHECK_SUCCESS_TTL_SECONDS,
                        "latestVersion": "0.2.14",
                    }
                )
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.13",
                ) as success_fetch:
                    success_result = mcp_server._version_status()

                mcp_server._write_version_check_cache(
                    {
                        "status": "failure",
                        "checkedAt": now - mcp_server.VERSION_CHECK_FAILURE_TTL_SECONDS,
                    }
                )
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.13",
                ) as failure_fetch:
                    failure_result = mcp_server._version_status()

        success_fetch.assert_called_once_with()
        failure_fetch.assert_called_once_with()
        self.assertFalse(success_result["cached"])
        self.assertFalse(failure_result["cached"])

    def test_version_status_ignores_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                cache_path = mcp_server._version_check_cache_path()
                cache_path.write_text("{not-json", encoding="utf-8")
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.13",
                ) as fetch:
                    result = mcp_server._version_status()

        fetch.assert_called_once_with()
        self.assertEqual(result["status"], "current")

    def test_version_status_concurrent_calls_share_one_fetch(self):
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        fetch_count = 0
        fetch_count_lock = threading.Lock()
        results = []

        def fetch():
            nonlocal fetch_count
            with fetch_count_lock:
                fetch_count += 1
            fetch_started.set()
            release_fetch.wait(2)
            return "0.2.14"

        def check_version():
            results.append(mcp_server._version_status())

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    side_effect=fetch,
                ):
                    first = threading.Thread(target=check_version)
                    second = threading.Thread(target=check_version)
                    first.start()
                    self.assertTrue(fetch_started.wait(1))
                    second.start()
                    time.sleep(0.1)
                    release_fetch.set()
                    first.join(2)
                    second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(fetch_count, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result["cached"] for result in results), [False, True])

    def test_version_status_does_not_notify_when_public_version_is_older(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.12",
                ):
                    result = mcp_server._version_status()

        self.assertEqual(result["status"], "ahead")
        self.assertFalse(result["updateAvailable"])
        self.assertFalse(result["agentAction"]["notifyUser"])

    def test_version_status_current_version_does_not_notify_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "",
                },
                clear=False,
            ):
                with patch(
                    "oa_agent_connector.mcp_server._fetch_latest_version",
                    return_value="0.2.13",
                ):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_version_status", "arguments": {}},
                        }
                    )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "current")
        self.assertFalse(payload["updateAvailable"])
        self.assertFalse(payload["agentAction"]["notifyUser"])
        self.assertNotIn("versionCheck", payload)

    def test_version_check_can_be_disabled_without_network_or_recommendation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {
                    "OA_AGENT_STATE_DIR": tmpdir,
                    "OA_AGENT_DISABLE_UPDATE_CHECK": "1",
                },
                clear=False,
            ):
                with patch("oa_agent_connector.mcp_server._fetch_latest_version") as fetch:
                    version_response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_version_status", "arguments": {}},
                        }
                    )
                    guide_response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_setup_guide", "arguments": {}},
                        }
                    )

        version_payload = json.loads(version_response["result"]["content"][0]["text"])
        guide_payload = json.loads(guide_response["result"]["content"][0]["text"])
        fetch.assert_not_called()
        self.assertEqual(version_payload["status"], "disabled")
        self.assertFalse(version_payload["agentAction"]["notifyUser"])
        self.assertNotIn("versionCheck", guide_payload)

    def test_version_status_rejects_oa_connection_parameters(self):
        response = mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "oa_version_status",
                    "arguments": {"baseUrl": "https://example.invalid/oa/"},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("不接受参数", payload["reason"])
        self.assertNotIn("versionCheck", payload)

    def test_get_detail_marks_approval_as_needing_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_get_detail",
                                "arguments": {"fdId": "1234567890abcdef1234567890abcdef"},
                            },
                        }
                    )

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["approvalHandling"]["level"], 2)
        self.assertEqual(payload["approvalHandling"]["code"], "detail_check_required")
        self.assertFalse(payload["approvalHandling"]["canContinueInChat"])

    def test_password_login_hidden_and_disabled_by_default(self):
        with patch.dict("os.environ", {"OA_AGENT_ENABLE_PASSWORD_LOGIN": ""}, clear=False):
            response = mcp_server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "oa_login",
                        "arguments": {
                            "baseUrl": "https://example.invalid/oa/",
                            "username": "u",
                            "password": "p",
                        },
                    },
                }
            )
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("oa_begin_auth", payload["reason"])
        self.assertNotIn("password", json.dumps(payload, ensure_ascii=False).lower())

    def test_password_login_can_be_explicitly_enabled_for_compatibility(self):
        with patch.dict("os.environ", {"OA_AGENT_ENABLE_PASSWORD_LOGIN": "1"}, clear=False):
            tools = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("oa_login", names)

    def test_list_todos_without_config_returns_guide(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=True):
                listed = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_list_todos", "arguments": {}},
                    }
                )
                result = listed["result"]
                self.assertTrue(result["isError"])
                payload = json.loads(result["content"][0]["text"])
                self.assertIn("guide", payload)
                self.assertEqual(payload["guide"][1]["tool"], "oa_begin_auth")
                self.assertTrue(payload["configurationRequired"])
                self.assertFalse(payload["reauthRequired"])
                self.assertIsNone(payload["nextAction"])
                self.assertEqual(payload["versionCheck"]["tool"], "oa_version_status")

    def test_auth_error_does_not_delete_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_AGENT_ENABLE_PASSWORD_LOGIN": "1"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "https://example.invalid/oa/",
                                    "username": "u",
                                    "password": "p",
                                },
                            },
                        }
                    )
                cookie_path = mcp_server._session_paths("default")["cookie"]
                self.assertTrue(cookie_path.exists())

                with patch.object(mcp_server, "OAClient", ExpiredAuthClient):
                    listed = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_list_todos", "arguments": {}},
                        }
                    )
                self.assertTrue(listed["result"]["isError"])
                payload = json.loads(listed["result"]["content"][0]["text"])
                self.assertTrue(payload["reauthRequired"])
                self.assertEqual(payload["nextAction"]["tool"], "oa_begin_auth")
                self.assertEqual(payload["nextAction"]["arguments"]["baseUrl"], "https://example.invalid/oa/")
                self.assertNotIn("fallbackAction", payload)
                self.assertNotIn("password", json.dumps(payload, ensure_ascii=False).lower())
                self.assertTrue(cookie_path.exists())

    def test_begin_auth_returns_local_url_without_password(self):
        calls = {}

        def fake_begin_local_auth(**kwargs):
            calls.update(kwargs)
            return {
                "ok": True,
                "authRequired": True,
                "authUrl": "http://127.0.0.1:12345/authorize?state=abc",
                "authToken": "abc",
                "session": kwargs["session"],
                "baseUrl": kwargs["base_url"],
                "expiresAt": 123,
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "begin_local_auth", fake_begin_local_auth):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_begin_auth",
                                "arguments": {"session": "work", "expiresInSeconds": 120},
                            },
                        }
                    )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["authUrl"], "http://127.0.0.1:12345/authorize?state=abc")
        self.assertNotIn("password", json.dumps(payload, ensure_ascii=False).lower())
        self.assertEqual(calls["base_url"], "https://example.invalid/oa/")
        self.assertEqual(calls["session"], "work")
        self.assertEqual(calls["state_dir"], tmpdir)
        self.assertEqual(calls["expires_in"], 120)

    def test_begin_auth_http_returns_dynamic_insecure_next_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://example.invalid/oa/", "OA_AGENT_ALLOW_INSECURE_AUTH": ""},
                clear=False,
            ):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_begin_auth", "arguments": {}},
                    }
                )
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["transportSecurityRequired"])
        self.assertEqual(payload["nextAction"]["tool"], "oa_begin_auth")
        self.assertTrue(payload["nextAction"]["arguments"]["insecure"])
        self.assertEqual(payload["nextAction"]["arguments"]["baseUrl"], "http://example.invalid/oa/")
        self.assertIn("transportConfirmationToken", payload)
        self.assertEqual(payload["nextAction"]["arguments"]["transportConfirmationToken"], payload["transportConfirmationToken"])
        self.assertEqual(payload["confirmationText"], "确认继续登录")
        self.assertNotIn("authUrl", payload)

    def test_begin_auth_http_dynamic_insecure_override_requires_confirmation_token(self):
        calls = {}

        def fake_begin_local_auth(**kwargs):
            calls.update(kwargs)
            return {
                "ok": True,
                "authRequired": True,
                "authUrl": "http://127.0.0.1:12345/authorize?state=abc",
                "authToken": "abc",
                "session": kwargs["session"],
                "baseUrl": kwargs["base_url"],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "begin_local_auth", fake_begin_local_auth):
                    first = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_begin_auth", "arguments": {"session": "work"}},
                        }
                    )
                    first_payload = json.loads(first["result"]["content"][0]["text"])
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_begin_auth", "arguments": first_payload["nextAction"]["arguments"]},
                        }
                    )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["authUrl"], "http://127.0.0.1:12345/authorize?state=abc")
        self.assertEqual(calls["base_url"], "http://example.invalid/oa/")
        self.assertTrue(calls["insecure"])
        self.assertEqual(calls["session"], "work")

    def test_begin_auth_http_insecure_without_confirmation_token_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://example.invalid/oa/"}, clear=False):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_begin_auth", "arguments": {"session": "work", "insecure": True}},
                    }
                )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertTrue(payload["transportSecurityRequired"])
        self.assertNotIn("authUrl", payload)
        self.assertIn("transportConfirmationToken", payload)

    def test_begin_auth_rejects_insecure_string_value(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://example.invalid/oa/"}, clear=False):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_begin_auth", "arguments": {"session": "work", "insecure": "false"}},
                    }
                )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertIn("必须是布尔值", payload["reason"])

    def test_begin_auth_https_insecure_requires_admin_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/", "OA_AGENT_ALLOW_INSECURE_AUTH": ""},
                clear=False,
            ):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_begin_auth", "arguments": {"session": "work", "insecure": True}},
                    }
                )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertTrue(payload["adminApprovalRequired"])
        self.assertEqual(payload["code"], "tlsVerificationDisabled")
        self.assertNotIn("nextAction", payload)

    def test_begin_auth_rejects_unsupported_scheme_without_next_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "ftp://example.invalid/oa/"}, clear=False):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_begin_auth", "arguments": {"session": "work", "insecure": True}},
                    }
                )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertTrue(payload["configurationRequired"])
        self.assertFalse(payload["transportSecurityRequired"])
        self.assertNotIn("nextAction", payload)

    def test_local_auth_status_reads_status_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status_dir = Path(tmpdir) / "local-auth"
            status_dir.mkdir()
            (status_dir / "abc.json").write_text(
                json.dumps({"ok": True, "status": "success", "authToken": "abc", "loginAccount": "u001"}, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                response = mcp_server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_local_auth_status", "arguments": {"authToken": "abc"}},
                    }
                )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "success")
        self.assertNotIn("loginAccount", payload)

    def test_auth_status_returns_login_identity_from_client(self):
        class IdentityClient(FakeClient):
            def auth_status(self):
                return {
                    "ok": True,
                    "loginAs": "示例用户/技术经理",
                    "identityAvailable": True,
                    "identity": {"userName": "示例用户", "position": "技术经理"},
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", IdentityClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_auth_status", "arguments": {"session": "work"}},
                        }
                    )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["loginAs"], "示例用户/技术经理")
        self.assertEqual(payload["session"], "work")

    def test_auth_status_falls_back_to_saved_login_account(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_AGENT_ENABLE_PASSWORD_LOGIN": "1"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "https://example.invalid/oa/",
                                    "username": "u001",
                                    "password": "p",
                                    "session": "work",
                                },
                            },
                        }
                    )
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {"name": "oa_auth_status", "arguments": {"session": "work"}},
                        }
                    )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["loginAs"], "u001")
        self.assertEqual(payload["identitySource"], "savedLoginAccount")

    def test_prepare_then_confirm_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_AGENT_ENABLE_PASSWORD_LOGIN": "1"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "https://example.invalid/oa/",
                                    "username": "u",
                                    "password": "p",
                                },
                            },
                        }
                    )
                    prepared = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_prepare_approval",
                                "arguments": {
                                    "fdId": "1234567890abcdef1234567890abcdef",
                                    "action": "approve",
                                    "note": "同意",
                                },
                            },
                        }
                    )
                    payload = json.loads(prepared["result"]["content"][0]["text"])
                    self.assertTrue(payload["requiresUserConfirmation"])
                    self.assertEqual(payload["confirmationPhrase"], "确认审批")
                    self.assertTrue(payload["permissionCheck"]["actionAvailable"])
                    self.assertEqual(payload["approvalHandling"]["scopeVersion"], "standard-approval-v1")
                    self.assertEqual(payload["approvalHandling"]["level"], 1)
                    self.assertEqual(payload["approvalHandling"]["code"], "standard_approval")
                    self.assertTrue(payload["approvalHandling"]["canContinueInChat"])
                    self.assertIn("不会填写或修改业务表单", payload["approvalHandling"]["message"])

                    confirmed = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_confirm_approval",
                                "arguments": {
                                    "confirmationToken": payload["confirmationToken"],
                                    "confirmationText": "确认审批",
                                },
                            },
                        }
                    )
                    result = json.loads(confirmed["result"]["content"][0]["text"])
                    self.assertTrue(result["executed"])
                    self.assertEqual(result["fdId"], "1234567890abcdef1234567890abcdef")
                    self.assertEqual(result["userMessage"], "已提交审批同意。")

    def test_prepare_approval_rejects_action_missing_from_view_page(self):
        class UnsupportedActionClient(FakeClient):
            def validate_approval_action(self, fd_id, action, require_in_todo=True):
                raise OAConnectorError("当前节点不支持本次审批动作")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                with patch.object(mcp_server, "OAClient", UnsupportedActionClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_prepare_approval",
                                "arguments": {
                                    "fdId": "1234567890abcdef1234567890abcdef",
                                    "action": "reject",
                                    "note": "资料不完整",
                                },
                            },
                        }
                    )

                pending_dir = Path(tmpdir) / "pending-approvals"
                pending_files = list(pending_dir.glob("*.json")) if pending_dir.exists() else []

        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertIn("当前节点不支持", payload["reason"])
        self.assertEqual(pending_files, [])

    def test_direct_execute_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_approve",
                                "arguments": {
                                    "fdId": "1234567890abcdef1234567890abcdef",
                                    "note": "同意",
                                    "execute": True,
                                },
                            },
                        }
                    )
                    self.assertTrue(response["result"]["isError"])
                    payload = json.loads(response["result"]["content"][0]["text"])
                    self.assertIn("requiredFlow", payload)


class BatchApprovalMCPServerTest(unittest.TestCase):
    FD_IDS = [str(index) * 32 for index in range(1, 5)]

    def _tool_call(self, name, arguments, message_id=1):
        return mcp_server.handle(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )

    def _payload(self, response):
        return json.loads(response["result"]["content"][0]["text"])

    def _todos(self):
        return [
            OATodo(
                fd_id,
                f"审批单 {index}",
                raw={"nodeName": f"节点 {index}", "handlerName": f"审批人 {index}"},
            )
            for index, fd_id in enumerate(self.FD_IDS, start=1)
        ]

    def test_batch_tools_publish_strict_nested_schema(self):
        response = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        self.assertIn("oa_prepare_batch_approval", tools)
        self.assertIn("oa_confirm_batch_approval", tools)
        self.assertNotIn(
            "futureNodeId",
            tools["oa_prepare_approval"]["inputSchema"]["properties"],
        )
        items_schema = tools["oa_prepare_batch_approval"]["inputSchema"]["properties"]["items"]
        self.assertEqual(items_schema["minItems"], 1)
        self.assertEqual(items_schema["maxItems"], 20)
        self.assertFalse(items_schema["items"]["additionalProperties"])
        self.assertNotIn("futureNodeId", items_schema["items"]["properties"])
        self.assertEqual(
            items_schema["items"]["required"],
            ["fdId", "action", "note"],
        )

    def test_single_prepare_rejects_manual_future_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    response = self._tool_call(
                        "oa_prepare_approval",
                        {
                            "fdId": self.FD_IDS[0],
                            "action": "approve",
                            "note": "同意",
                            "futureNodeId": "node-2",
                        },
                    )
                pending_dir = Path(tmpdir) / "pending-approvals"
                pending_files = list(pending_dir.glob("*.json")) if pending_dir.exists() else []

        self.assertTrue(response["result"]["isError"])
        payload = self._payload(response)
        self.assertIn("不支持手工指定下一节点", payload["reason"])
        self.assertEqual(payload["approvalHandling"]["level"], 3)
        self.assertEqual(payload["approvalHandling"]["code"], "native_oa_required")
        self.assertFalse(payload["approvalHandling"]["canContinueInChat"])
        self.assertNotIn("guide", payload)
        self.assertEqual(pending_files, [])

    def test_single_confirm_rejects_legacy_token_with_future_node(self):
        calls = {"approve": 0}

        class CountingClient(FakeClient):
            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                token = mcp_server._save_pending_approval(
                    {
                        "kind": "single",
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "fdId": self.FD_IDS[0],
                        "action": "approve",
                        "note": "同意",
                        "futureNodeId": "node-2",
                        "loginBinding": mcp_server._approval_login_binding(
                            "work",
                            "https://example.invalid/oa/",
                        ),
                        "approvalBinding": fake_approval_binding("approve"),
                    }
                )
                with patch.object(mcp_server, "OAClient", CountingClient):
                    response = self._tool_call(
                        "oa_confirm_approval",
                        {"confirmationToken": token, "confirmationText": "确认审批"},
                    )

        self.assertTrue(response["result"]["isError"])
        self.assertIn("不支持手工指定下一节点", self._payload(response)["reason"])
        self.assertEqual(calls["approve"], 0)

    def test_batch_item_validation_rejects_empty_oversized_duplicate_and_unknown_fields(self):
        with self.assertRaisesRegex(OAConnectorError, "至少需要 1 条"):
            mcp_server._normalize_batch_approval_items([])
        with self.assertRaisesRegex(OAConnectorError, "最多 20 条"):
            mcp_server._normalize_batch_approval_items(
                [
                    {"fdId": f"{index:032x}", "action": "approve", "note": "同意"}
                    for index in range(21)
                ]
            )
        with self.assertRaisesRegex(OAConnectorError, "不能.*重复"):
            mcp_server._normalize_batch_approval_items(
                [
                    {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"},
                    {"fdId": self.FD_IDS[0], "action": "reject", "note": "退回"},
                ]
            )
        with self.assertRaisesRegex(OAConnectorError, "不支持的参数"):
            mcp_server._normalize_batch_approval_items(
                [
                    {
                        "fdId": self.FD_IDS[0],
                        "action": "approve",
                        "note": "同意",
                        "execute": True,
                    }
                ]
            )
        with self.assertRaisesRegex(OAConnectorError, "不支持手工指定下一节点"):
            mcp_server._normalize_batch_approval_items(
                [
                    {
                        "fdId": self.FD_IDS[0],
                        "action": "reject",
                        "note": "退回",
                        "futureNodeId": "node-2",
                    }
                ]
            )

    def test_batch_prepare_is_all_or_nothing_and_reads_todos_once(self):
        test_case = self
        calls = {"list": 0, "validate": []}

        class PrepareFailureClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                calls["list"] += 1
                return test_case._todos()[:2]

            def validate_approval_action(self, fd_id, action, require_in_todo=True):
                calls["validate"].append(fd_id)
                if fd_id == test_case.FD_IDS[1]:
                    raise OAConnectorError("当前节点不支持本次审批动作")
                return super().validate_approval_action(fd_id, action, require_in_todo)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                )
                with patch.object(mcp_server, "OAClient", PrepareFailureClient):
                    response = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"},
                                {"fdId": self.FD_IDS[1], "action": "reject", "note": "退回"},
                            ],
                        },
                    )
                pending_dir = Path(tmpdir) / "pending-approvals"
                pending_files = list(pending_dir.glob("*.json")) if pending_dir.exists() else []

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(calls["list"], 1)
        self.assertEqual(calls["validate"], self.FD_IDS[:2])
        self.assertEqual(pending_files, [])

    def test_mixed_batch_executes_in_order_after_one_confirmation(self):
        test_case = self
        events = []
        calls = {"list": 0}

        class MixedBatchClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                calls["list"] += 1
                return test_case._todos()[:3]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                self.assert_binding(expected_binding, "approve")
                events.append((fd_id, "approve", audit_note))
                return {"result": "success"}

            def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
                self.assert_binding(expected_binding, "reject")
                events.append((fd_id, "reject", audit_note))
                return {"result": "success"}

            @staticmethod
            def assert_binding(binding, action):
                if binding != fake_approval_binding(action):
                    raise AssertionError("approval binding was not preserved")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", MixedBatchClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意 1"},
                                {"fdId": self.FD_IDS[1], "action": "reject", "note": "退回 2"},
                                {"fdId": self.FD_IDS[2], "action": "approve", "note": "同意 3"},
                            ],
                        },
                    )
                    prepared_payload = self._payload(prepared)
                    self.assertEqual(calls["list"], 1)
                    self.assertEqual(prepared_payload["confirmationPhrase"], "确认批量审批")
                    self.assertTrue(prepared_payload["batchNonTransactional"])
                    self.assertEqual(prepared_payload["approvalHandling"]["level"], 1)
                    self.assertEqual(prepared_payload["approvalHandling"]["code"], "standard_approval")
                    self.assertTrue(prepared_payload["approvalHandling"]["canContinueInChat"])
                    confirmed = self._tool_call(
                        "oa_confirm_batch_approval",
                        {
                            "confirmationToken": prepared_payload["confirmationToken"],
                            "confirmationText": "确认批量审批",
                        },
                        message_id=2,
                    )

        payload = self._payload(confirmed)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["completedCount"], 3)
        self.assertEqual([item["status"] for item in payload["completedItems"]], ["completed"] * 3)
        self.assertEqual(
            events,
            [
                (self.FD_IDS[0], "approve", "同意 1"),
                (self.FD_IDS[1], "reject", "退回 2"),
                (self.FD_IDS[2], "approve", "同意 3"),
            ],
        )

    def test_batch_stops_after_first_failure_and_reports_partial_completion(self):
        test_case = self
        events = []

        class StateChangedClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:3]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                events.append((fd_id, "approve"))
                return {"result": "success"}

            def reject(self, fd_id, audit_note, execute=False, expected_binding=None):
                events.append((fd_id, "reject"))
                raise ApprovalStateChangedError("审批流程状态已变化，请重新准备审批并再次确认")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", StateChangedClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"},
                                {"fdId": self.FD_IDS[1], "action": "reject", "note": "退回"},
                                {"fdId": self.FD_IDS[2], "action": "approve", "note": "同意"},
                            ],
                        },
                    )
                    token = self._payload(prepared)["confirmationToken"]
                    confirmed = self._tool_call(
                        "oa_confirm_batch_approval",
                        {"confirmationToken": token, "confirmationText": "确认批量审批"},
                        message_id=2,
                    )

        self.assertTrue(confirmed["result"]["isError"])
        payload = self._payload(confirmed)
        self.assertTrue(payload["partialCompletion"])
        self.assertEqual(payload["completedCount"], 1)
        self.assertEqual(payload["completedItems"][0]["fdId"], self.FD_IDS[0])
        self.assertEqual(payload["currentItem"]["fdId"], self.FD_IDS[1])
        self.assertEqual(payload["currentItem"]["status"], "failed")
        self.assertEqual([item["fdId"] for item in payload["notAttemptedItems"]], [self.FD_IDS[2]])
        self.assertFalse(payload["retryAllowed"])
        self.assertEqual(events, [(self.FD_IDS[0], "approve"), (self.FD_IDS[1], "reject")])

    def test_batch_unknown_result_stops_without_retrying_or_running_later_items(self):
        test_case = self
        events = []

        class UnknownBatchClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:3]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                events.append(fd_id)
                if fd_id == test_case.FD_IDS[1]:
                    raise ApprovalResultUnknownError("审批请求已经发出，但 OA 没有返回明确结果。请勿重复提交。")
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", UnknownBatchClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"},
                                {"fdId": self.FD_IDS[1], "action": "approve", "note": "同意"},
                                {"fdId": self.FD_IDS[2], "action": "approve", "note": "同意"},
                            ],
                        },
                    )
                    confirmed = self._tool_call(
                        "oa_confirm_batch_approval",
                        {
                            "confirmationToken": self._payload(prepared)["confirmationToken"],
                            "confirmationText": "确认批量审批",
                        },
                        message_id=2,
                    )

        payload = self._payload(confirmed)
        self.assertTrue(payload["resultUnknown"])
        self.assertTrue(payload["submittedOnce"])
        self.assertFalse(payload["retryAllowed"])
        self.assertEqual(payload["completedCount"], 1)
        self.assertEqual(payload["currentItem"]["status"], "resultUnknown")
        self.assertEqual(events, self.FD_IDS[:2])
        self.assertFalse(mcp_server._can_retry_after_auto_login("oa_confirm_batch_approval"))

    def test_batch_confirmation_token_can_only_be_claimed_once(self):
        test_case = self
        calls = {"approve": 0}
        calls_lock = threading.Lock()

        class ConcurrentBatchClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                with calls_lock:
                    calls["approve"] += 1
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", ConcurrentBatchClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                            ],
                        },
                    )
                    token = self._payload(prepared)["confirmationToken"]
                    original_load = mcp_server._load_pending_approval
                    load_barrier = threading.Barrier(2)

                    def synchronized_load(value):
                        pending = original_load(value)
                        load_barrier.wait(timeout=5)
                        return pending

                    responses = []

                    def confirm(message_id):
                        responses.append(
                            self._tool_call(
                                "oa_confirm_batch_approval",
                                {
                                    "confirmationToken": token,
                                    "confirmationText": "确认批量审批",
                                },
                                message_id=message_id,
                            )
                        )

                    with patch.object(mcp_server, "_load_pending_approval", side_effect=synchronized_load):
                        threads = [threading.Thread(target=confirm, args=(message_id,)) for message_id in (2, 3)]
                        for thread in threads:
                            thread.start()
                        for thread in threads:
                            thread.join(timeout=5)
                            self.assertFalse(thread.is_alive())

        self.assertEqual(calls["approve"], 1)
        self.assertEqual(sum(not response["result"].get("isError", False) for response in responses), 1)

    def test_distinct_batch_tokens_cannot_submit_same_workitem_concurrently(self):
        test_case = self
        calls = {"approve": 0}
        calls_lock = threading.Lock()

        class OverlappingBatchClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                with calls_lock:
                    calls["approve"] += 1
                time.sleep(0.05)
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", OverlappingBatchClient):
                    tokens = []
                    for message_id in (1, 2):
                        prepared = self._tool_call(
                            "oa_prepare_batch_approval",
                            {
                                "session": "work",
                                "items": [
                                    {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                                ],
                            },
                            message_id=message_id,
                        )
                        tokens.append(self._payload(prepared)["confirmationToken"])

                    start_barrier = threading.Barrier(2)
                    responses = []

                    def confirm(message_id, token):
                        start_barrier.wait(timeout=5)
                        responses.append(
                            self._tool_call(
                                "oa_confirm_batch_approval",
                                {
                                    "confirmationToken": token,
                                    "confirmationText": "确认批量审批",
                                },
                                message_id=message_id,
                            )
                        )

                    threads = [
                        threading.Thread(target=confirm, args=(message_id, token))
                        for message_id, token in zip((3, 4), tokens)
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())

        payloads = [self._payload(response) for response in responses]
        self.assertEqual(calls["approve"], 1)
        self.assertEqual(sum(bool(payload.get("ok")) for payload in payloads), 1)
        blocked = next(payload for payload in payloads if not payload.get("ok"))
        self.assertIn("另一个确认流程", blocked["reason"])
        self.assertFalse(blocked["retryAllowed"])

    def test_equivalent_base_urls_share_the_same_workitem_lock(self):
        item = {
            "fdId": self.FD_IDS[0],
            "action": "approve",
            "note": "同意",
            "approvalBinding": fake_approval_binding("approve"),
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                first = mcp_server._approval_workitem_lock_path(
                    {"baseUrl": "https://EXAMPLE.invalid:443/x/../oa/"},
                    item,
                )
                second = mcp_server._approval_workitem_lock_path(
                    {"baseUrl": "https://example.invalid/oa"},
                    item,
                )

        self.assertEqual(first, second)

    def test_existing_workitem_lock_is_never_auto_replaced(self):
        item = {
            "fdId": self.FD_IDS[0],
            "action": "approve",
            "note": "同意",
            "approvalBinding": fake_approval_binding("approve"),
        }
        pending = {"baseUrl": "https://example.invalid/oa/"}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                path = mcp_server._acquire_approval_workitem_lock(pending, item, "token-one")
                lock_data = json.loads(path.read_text(encoding="utf-8"))
                lock_data["expiresAt"] = 0
                path.write_text(json.dumps(lock_data), encoding="utf-8")
                with self.assertRaisesRegex(OAConnectorError, "另一个确认流程"):
                    mcp_server._acquire_approval_workitem_lock(pending, item, "token-two")
                mcp_server._release_approval_workitem_lock(path, "token-one")

    def test_batch_terminal_result_can_be_read_again_without_resubmitting(self):
        test_case = self
        calls = {"approve": 0}

        class TerminalReadbackClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", TerminalReadbackClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                            ],
                        },
                    )
                    token = self._payload(prepared)["confirmationToken"]
                    first = self._tool_call(
                        "oa_confirm_batch_approval",
                        {"confirmationToken": token, "confirmationText": "确认批量审批"},
                        message_id=2,
                    )
                    processing_exists = mcp_server._pending_claim_path(token).exists()
                    second = self._tool_call(
                        "oa_confirm_batch_approval",
                        {"confirmationToken": token, "confirmationText": "确认批量审批"},
                        message_id=3,
                    )
                    mcp_server._save_session(
                        "work",
                        "https://example.invalid/oa/",
                        login_account="u002",
                    )
                    switched_account = self._tool_call(
                        "oa_confirm_batch_approval",
                        {"confirmationToken": token, "confirmationText": "确认批量审批"},
                        message_id=4,
                    )

        self.assertTrue(self._payload(first)["ok"])
        self.assertEqual(self._payload(first), self._payload(second))
        self.assertTrue(processing_exists)
        self.assertEqual(calls["approve"], 1)
        self.assertTrue(switched_account["result"]["isError"])
        switched_text = switched_account["result"]["content"][0]["text"]
        self.assertIn("登录账号已变化", switched_text)
        self.assertNotIn("审批单 1", switched_text)

    def test_expired_batch_terminal_is_cleaned_on_next_tool_call(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                path = mcp_server._pending_claim_path("expired-terminal")
                mcp_server._ensure_private_dir(path.parent)
                path.write_text(
                    json.dumps(
                        {
                            "kind": "batch",
                            "terminalResult": {"isError": False, "payload": {"ok": True}},
                            "terminalExpiresAt": int(time.time()) - 1,
                        }
                    ),
                    encoding="utf-8",
                )
                mcp_server.call_tool("oa_setup_guide", {})
                exists_after_cleanup = path.exists()

        self.assertFalse(exists_after_cleanup)

    def test_single_and_batch_tokens_share_workitem_duplicate_lock(self):
        test_case = self
        calls = {"approve": 0}

        class CrossFlowClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                calls["approve"] += 1
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", CrossFlowClient):
                    single = self._tool_call(
                        "oa_prepare_approval",
                        {
                            "session": "work",
                            "fdId": self.FD_IDS[0],
                            "action": "approve",
                            "note": "同意",
                        },
                    )
                    batch = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                            ],
                        },
                        message_id=2,
                    )
                    single_result = self._tool_call(
                        "oa_confirm_approval",
                        {
                            "confirmationToken": self._payload(single)["confirmationToken"],
                            "confirmationText": "确认审批",
                        },
                        message_id=3,
                    )
                    batch_result = self._tool_call(
                        "oa_confirm_batch_approval",
                        {
                            "confirmationToken": self._payload(batch)["confirmationToken"],
                            "confirmationText": "确认批量审批",
                        },
                        message_id=4,
                    )

        self.assertTrue(self._payload(single_result)["ok"])
        self.assertTrue(batch_result["result"]["isError"])
        self.assertIn("刚刚提交", self._payload(batch_result)["reason"])
        self.assertEqual(calls["approve"], 1)

    def test_progress_write_failure_after_success_stops_before_next_item(self):
        test_case = self
        events = []

        class PersistenceFailureClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:2]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                events.append(fd_id)
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", PersistenceFailureClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"},
                                {"fdId": self.FD_IDS[1], "action": "approve", "note": "同意"},
                            ],
                        },
                    )
                    token = self._payload(prepared)["confirmationToken"]
                    original_save = mcp_server._save_batch_progress
                    save_calls = {"count": 0}

                    def fail_after_first_write(*args, **kwargs):
                        save_calls["count"] += 1
                        if save_calls["count"] >= 2:
                            raise OSError("disk full")
                        return original_save(*args, **kwargs)

                    with patch.object(
                        mcp_server,
                        "_save_batch_progress",
                        side_effect=fail_after_first_write,
                    ):
                        first = self._tool_call(
                            "oa_confirm_batch_approval",
                            {"confirmationToken": token, "confirmationText": "确认批量审批"},
                            message_id=2,
                        )
                    second = self._tool_call(
                        "oa_confirm_batch_approval",
                        {"confirmationToken": token, "confirmationText": "确认批量审批"},
                        message_id=3,
                    )

        payload = self._payload(first)
        self.assertTrue(first["result"]["isError"])
        self.assertTrue(payload["statePersistenceWarning"])
        self.assertEqual(payload["completedCount"], 1)
        self.assertEqual([item["fdId"] for item in payload["notAttemptedItems"]], [self.FD_IDS[1]])
        self.assertEqual(self._payload(second), payload)
        self.assertEqual(events, [self.FD_IDS[0]])

    def test_final_progress_write_failure_does_not_report_partial_completion(self):
        test_case = self
        events = []

        class FinalPersistenceFailureClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                events.append(fd_id)
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", FinalPersistenceFailureClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                            ],
                        },
                    )
                    token = self._payload(prepared)["confirmationToken"]
                    original_save = mcp_server._save_batch_progress
                    save_calls = {"count": 0}

                    def fail_final_write(*args, **kwargs):
                        save_calls["count"] += 1
                        if save_calls["count"] >= 2:
                            raise OSError("disk full")
                        return original_save(*args, **kwargs)

                    with patch.object(
                        mcp_server,
                        "_save_batch_progress",
                        side_effect=fail_final_write,
                    ):
                        response = self._tool_call(
                            "oa_confirm_batch_approval",
                            {"confirmationToken": token, "confirmationText": "确认批量审批"},
                            message_id=2,
                        )

        payload = self._payload(response)
        self.assertTrue(response["result"]["isError"])
        self.assertTrue(payload["statePersistenceWarning"])
        self.assertTrue(payload["oaCompletedAllItems"])
        self.assertFalse(payload["partialCompletion"])
        self.assertFalse(payload["stopped"])
        self.assertEqual(payload["completedCount"], payload["totalCount"])
        self.assertEqual(payload["notAttemptedCount"], 0)
        self.assertNotIn("剩余项目未执行", payload["userMessage"])
        self.assertEqual(events, [self.FD_IDS[0]])

    def test_batch_confirmation_stops_if_login_account_changes(self):
        test_case = self
        events = []

        class AccountBoundClient(FakeClient):
            def list_todos(self, page=1, page_size=20):
                return test_case._todos()[:1]

            def approve(self, fd_id, audit_note, execute=False, future_node_id=None, expected_binding=None):
                events.append(fd_id)
                return {"result": "success"}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u001")
                with patch.object(mcp_server, "OAClient", AccountBoundClient):
                    prepared = self._tool_call(
                        "oa_prepare_batch_approval",
                        {
                            "session": "work",
                            "items": [
                                {"fdId": self.FD_IDS[0], "action": "approve", "note": "同意"}
                            ],
                        },
                    )
                    mcp_server._save_session("work", "https://example.invalid/oa/", login_account="u002")
                    confirmed = self._tool_call(
                        "oa_confirm_batch_approval",
                        {
                            "confirmationToken": self._payload(prepared)["confirmationToken"],
                            "confirmationText": "确认批量审批",
                        },
                        message_id=2,
                    )

        payload = self._payload(confirmed)
        self.assertTrue(confirmed["result"]["isError"])
        self.assertIn("登录账号已变化", payload["reason"])
        self.assertEqual(payload["completedCount"], 0)
        self.assertEqual(events, [])

    def test_single_and_batch_confirmation_tokens_cannot_be_cross_used(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                single_token = mcp_server._save_pending_approval(
                    {"kind": "single", "action": "approve", "session": "work"}
                )
                batch_token = mcp_server._save_pending_approval(
                    {"kind": "batch", "session": "work", "items": []}
                )
                batch_response = self._tool_call(
                    "oa_confirm_batch_approval",
                    {"confirmationToken": single_token, "confirmationText": "确认批量审批"},
                )
                single_response = self._tool_call(
                    "oa_confirm_approval",
                    {"confirmationToken": batch_token, "confirmationText": "确认审批"},
                    message_id=2,
                )
                single_exists = mcp_server._pending_path(single_token).exists()
                batch_exists = mcp_server._pending_path(batch_token).exists()

        self.assertTrue(batch_response["result"]["isError"])
        self.assertTrue(single_response["result"]["isError"])
        self.assertTrue(single_exists)
        self.assertTrue(batch_exists)

    def test_batch_wrong_confirmation_phrase_does_not_consume_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                token = mcp_server._save_pending_approval(
                    {"kind": "batch", "session": "work", "items": []}
                )
                response = self._tool_call(
                    "oa_confirm_batch_approval",
                    {"confirmationToken": token, "confirmationText": "确认"},
                )
                source_exists = mcp_server._pending_path(token).exists()
                claim_exists = mcp_server._pending_claim_path(token).exists()

        self.assertTrue(response["result"]["isError"])
        payload = self._payload(response)
        self.assertIn("确认批量审批", payload["reason"])
        self.assertTrue(source_exists)
        self.assertFalse(claim_exists)

    def test_batch_progress_survives_process_interruption(self):
        items = [
            {
                "fdId": self.FD_IDS[0],
                "action": "approve",
                "note": "同意 1",
                "approvalBinding": fake_approval_binding("approve"),
            },
            {
                "fdId": self.FD_IDS[1],
                "action": "approve",
                "note": "同意 2",
                "approvalBinding": fake_approval_binding("approve"),
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                token = mcp_server._save_pending_approval(
                    {
                        "kind": "batch",
                        "session": "work",
                        "baseUrl": "https://example.invalid/oa/",
                        "insecure": False,
                        "loginBinding": "binding",
                        "items": items,
                    }
                )
                executions = {"count": 0}

                def execute(_client, _item):
                    executions["count"] += 1
                    if executions["count"] == 2:
                        raise KeyboardInterrupt()
                    return {"result": "success"}

                with patch.object(mcp_server, "_approval_client_for_pending", return_value=object()):
                    with patch.object(mcp_server, "_execute_bound_approval", side_effect=execute):
                        with self.assertRaises(KeyboardInterrupt):
                            mcp_server.call_tool(
                                "oa_confirm_batch_approval",
                                {
                                    "confirmationToken": token,
                                    "confirmationText": "确认批量审批",
                                },
                            )
                progress = json.loads(
                    mcp_server._pending_claim_path(token).read_text(encoding="utf-8")
                )["batchProgress"]

        self.assertEqual(len(progress["completedItems"]), 1)
        self.assertEqual(progress["completedItems"][0]["fdId"], self.FD_IDS[0])
        self.assertEqual(progress["currentItem"]["fdId"], self.FD_IDS[1])
        self.assertEqual(progress["currentItem"]["status"], "executing")


class SearchMCPServerTest(unittest.TestCase):
    def test_batch_search_relogs_in_before_starting_the_batch(self):
        calls = {"login": 0, "batch": 0}

        class BatchReloginClient(FakeClient):
            def assert_logged_in(self):
                cookie_path = Path(self.cookie_file)
                if not cookie_path.exists() or cookie_path.read_text(encoding="utf-8") != "refreshed-cookie":
                    raise RuntimeError("当前 cookie 未登录或已失效，请先 login")

            def login(self, username, password):
                calls["login"] += 1
                Path(self.cookie_file).write_text("refreshed-cookie", encoding="utf-8")
                return True

            def batch_search_objects(self, queries, **kwargs):
                calls["batch"] += 1
                return {
                    "items": [],
                    "summary": {
                        "totalQueries": len(queries),
                        "matchedQueries": 0,
                        "errors": 0,
                        "downloads": 0,
                    },
                }

        class FakeCredentialStore:
            def load(self, base_url, session, username):
                return "stored-secret"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                mcp_server._save_session(
                    "work",
                    "https://example.invalid/oa/",
                    login_account="u001",
                    auto_login_enabled=True,
                )
                with patch.object(mcp_server, "OAClient", BatchReloginClient):
                    with patch("oa_agent_connector.mcp_server.SystemCredentialStore", return_value=FakeCredentialStore()):
                        response = mcp_server.handle(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {
                                    "name": "oa_batch_search_objects",
                                    "arguments": {"queries": ["a", "b"], "session": "work"},
                                },
                            }
                        )

        self.assertFalse(response["result"].get("isError", False))
        self.assertEqual(calls, {"login": 1, "batch": 1})

    def test_search_tools_are_listed_with_strict_schema(self):
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})["result"]["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        for name in [
            "oa_get_search_schema",
            "oa_search_objects",
            "oa_get_object_detail",
            "oa_download_attachment",
            "oa_batch_search_objects",
        ]:
            self.assertIn(name, by_name)
            self.assertFalse(by_name[name]["inputSchema"].get("additionalProperties", True))

        search_props = by_name["oa_search_objects"]["inputSchema"]["properties"]
        self.assertIn("query", search_props)
        self.assertEqual(search_props["scope"]["enum"], ["all", "knowledge", "news"])
        self.assertEqual(search_props["matchMode"]["enum"], ["keyword", "contains", "exact"])
        self.assertIn("requireDetail", search_props)
        self.assertIn("dedupByDocument", search_props)
        self.assertIn("title", search_props["searchFields"]["items"]["enum"])
        self.assertNotIn("baseUrl", search_props)
        self.assertNotIn("insecure", search_props)

    def test_search_tool_calls_delegate_to_client(self):
        seen = {}

        class SearchFakeClient(FakeClient):
            def get_search_schema(self, scope="all"):
                return {"scope": scope, "models": [], "searchFields": ["title"], "limits": {}}

            def search_objects(self, **kwargs):
                seen["search"] = kwargs
                return {"query": kwargs["query"], "items": [], "page": 1, "pageSize": 20, "total": 0}

            def get_object_detail(self, record_ref=None, include_text=True, text_limit=12000, fields=None, fd_id=None):
                return {"recordRef": record_ref, "title": "详情", "text": "", "attachments": []}

            def download_attachment(self, record_ref, attachment_index, output_dir, overwrite=False, max_bytes=52428800, fd_id=None):
                return {"ok": True, "savedPath": str(Path(output_dir) / "a.pdf"), "bytes": 1}

            def batch_search_objects(self, queries, **kwargs):
                return {"items": [], "summary": {"totalQueries": len(queries), "matchedQueries": 0, "errors": 0, "downloads": 0}}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", SearchFakeClient):
                    schema = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_get_search_schema", "arguments": {"scope": "knowledge"}},
                    })
                    self.assertEqual(json.loads(schema["result"]["content"][0]["text"])["scope"], "knowledge")

                    searched = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "oa_search_objects",
                            "arguments": {
                                "query": "abc",
                                "scope": "knowledge",
                                "matchMode": "contains",
                                "requireDetail": False,
                                "dedupByDocument": False,
                            },
                        },
                    })
                    self.assertEqual(json.loads(searched["result"]["content"][0]["text"])["query"], "abc")
                    self.assertEqual(seen["search"]["matchMode"], "contains")
                    self.assertFalse(seen["search"]["requireDetail"])
                    self.assertFalse(seen["search"]["dedupByDocument"])

                    batched = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "oa_batch_search_objects", "arguments": {"queries": ["a", "b"], "scope": "knowledge"}},
                    })
                    self.assertEqual(json.loads(batched["result"]["content"][0]["text"])["summary"]["totalQueries"], 2)

    def test_new_search_tools_reject_bypass_params(self):
        """新增 5 个工具注入 baseUrl/insecure 等参数应返回 isError，不调用 FakeClient。"""
        call_count = {"n": 0}

        class TrackingClient(FakeClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                call_count["n"] += 1

            def get_search_schema(self, scope="all"):
                return {"scope": scope}

            def search_objects(self, **kwargs):
                return {"query": kwargs["query"], "items": []}

            def get_object_detail(self, record_ref=None, include_text=True, text_limit=12000, fields=None, fd_id=None):
                return {"recordRef": record_ref, "title": "t", "text": "", "attachments": []}

            def download_attachment(self, record_ref, attachment_index, output_dir, overwrite=False, max_bytes=52428800, fd_id=None):
                return {"ok": True}

            def batch_search_objects(self, queries, **kwargs):
                return {"items": [], "summary": {"totalQueries": len(queries), "matchedQueries": 0, "errors": 0, "downloads": 0}}

        bypass_payloads = [
            {"name": "oa_get_search_schema", "arguments": {"scope": "all", "baseUrl": "https://evil.test/oa/"}},
            {"name": "oa_search_objects", "arguments": {"query": "test", "insecure": True}},
            {"name": "oa_get_object_detail", "arguments": {"recordRef": {"scope": "knowledge", "modelName": "KmsMultidocKnowledge", "recordId": "x", "path": "/x?fdId=x"}, "extraParams": "evil"}},
            {"name": "oa_download_attachment", "arguments": {"recordRef": {"scope": "knowledge", "modelName": "KmsMultidocKnowledge", "recordId": "x", "path": "/x?fdId=x"}, "attachmentIndex": 1, "outputDir": "/tmp", "attachmentUrl": "https://evil.test/"}},
            {"name": "oa_batch_search_objects", "arguments": {"queries": ["a"], "fileId": "evil"}},
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", TrackingClient):
                    call_count["n"] = 0
                    for payload in bypass_payloads:
                        with self.subTest(tool=payload["name"]):
                            response = mcp_server.handle({
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": payload,
                            })
                            result = response["result"]
                            self.assertTrue(result["isError"], f"{payload['name']} 应返回 isError")
                            text = result["content"][0]["text"]
                            self.assertIn("不接受参数", text)
                    # FakeClient 不应被实例化（OAClient 不应被调用）
                    self.assertEqual(call_count["n"], 0)

    def test_search_auth_error_returns_reauth_next_action(self):
        class SearchAuthExpiredClient(FakeClient):
            def search_objects(self, **kwargs):
                raise RuntimeError("当前会话未登录，不能搜索 OA 内容")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", SearchAuthExpiredClient):
                    response = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_search_objects", "arguments": {"query": "abc", "session": "work"}},
                    })

        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertTrue(response["result"]["isError"])
        self.assertTrue(payload["reauthRequired"])
        self.assertEqual(payload["nextAction"]["tool"], "oa_begin_auth")
        self.assertEqual(payload["nextAction"]["arguments"]["baseUrl"], "https://example.invalid/oa/")
        self.assertEqual(payload["nextAction"]["arguments"]["session"], "work")
        self.assertNotIn("password", json.dumps(payload, ensure_ascii=False).lower())


class SensitiveOutputRegressionTest(unittest.TestCase):
    def test_encoded_and_json_passwords_are_never_returned(self):
        class LeakyClient(FakeClient):
            def search_objects(self, **kwargs):
                raise RuntimeError(
                    '{"password":"json-secret","next":"j_password%3Dencoded-secret%26x%3D1"}'
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                "os.environ",
                {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"},
                clear=False,
            ):
                with patch.object(mcp_server, "OAClient", LeakyClient):
                    response = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": "oa_search_objects", "arguments": {"query": "abc"}},
                        }
                    )

        text = json.dumps(response, ensure_ascii=False)
        self.assertIn("敏感内容已隐藏", text)
        self.assertNotIn("json-secret", text)
        self.assertNotIn("encoded-secret", text)

    def test_new_tool_error_output_does_not_leak_sensitive_patterns(self):
        class LeakyClient(FakeClient):
            def search_objects(self, **kwargs):
                raise RuntimeError("Cookie: abc; Set-Cookie: def; JSESSIONID=ghi; Authorization: Bearer token; <html>full</html>")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", LeakyClient):
                    response = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "oa_search_objects", "arguments": {"query": "abc"}},
                    })
        text = response["result"]["content"][0]["text"]
        # Verify sensitive patterns are not leaked as standalone tokens
        for forbidden in ["abc", "ghi", "Bearer token", "<html>"]:
            self.assertNotRegex(text, rf"\b{re.escape(forbidden)}\b")
        # "def" is a common substring in words like "description"; check the
        # redacted reason field directly instead of a whole-text substring match.
        payload = json.loads(text)
        self.assertNotRegex(payload["reason"], r"Set-Cookie:\s*\S+")

    def test_existing_direct_execute_is_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "https://example.invalid/oa/"}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    response = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "oa_reject",
                            "arguments": {"fdId": "1234567890abcdef1234567890abcdef", "note": "不同意", "execute": True},
                        },
                    })
        self.assertTrue(response["result"]["isError"])
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["requiredFlow"], ["oa_prepare_approval", "用户确认审批信息", "oa_confirm_approval"])


if __name__ == "__main__":
    unittest.main()
