import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oa_agent_connector import mcp_server
from oa_agent_connector.client import OATodo


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
                raw={"nodeName": "<div>部门负责人</div>", "handlerName": "<div>张三</div>"},
            )
        ]

    def get_detail(self, fd_id, require_in_todo=True):
        return {"fdId": fd_id, "title": "采购审批", "text": "采购审批详情"}

    def approve(self, fd_id, audit_note, execute=False, future_node_id=None):
        return {"dryRun": not execute, "fdId": fd_id, "result": fd_id}

    def reject(self, fd_id, audit_note, execute=False):
        return {"dryRun": not execute, "fdId": fd_id, "result": fd_id}


class ExpiredAuthClient(FakeClient):
    def list_todos(self, page=1, page_size=20):
        raise RuntimeError("当前 cookie 未登录或已失效，请先 login")


class MCPServerTest(unittest.TestCase):
    def test_initialize_and_tools_list(self):
        init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "oa-agent-connector")

        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertIn("oa_setup_guide", names)
        self.assertIn("oa_login", names)
        self.assertIn("oa_list_todos", names)

    def test_login_then_list_todos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    login = mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "http://oa.example.test/",
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
                self.assertEqual(payload["guide"][1]["tool"], "oa_login")

    def test_auth_error_does_not_delete_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "http://oa.example.test/",
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
                self.assertTrue(cookie_path.exists())

    def test_prepare_then_confirm_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir}, clear=False):
                with patch.object(mcp_server, "OAClient", FakeClient):
                    mcp_server.handle(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "oa_login",
                                "arguments": {
                                    "baseUrl": "http://oa.example.test/",
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

    def test_direct_execute_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://oa.example.test/"}, clear=False):
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


if __name__ == "__main__":
    unittest.main()
