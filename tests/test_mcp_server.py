import json
import re
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
                raw={"nodeName": "<div>部门负责人</div>", "handlerName": "<div>审批人A</div>"},
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


class SearchMCPServerTest(unittest.TestCase):
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
        self.assertNotIn("baseUrl", search_props)
        self.assertNotIn("insecure", search_props)

    def test_search_tool_calls_delegate_to_client(self):
        class SearchFakeClient(FakeClient):
            def get_search_schema(self, scope="all"):
                return {"scope": scope, "models": [], "searchFields": ["title"], "limits": {}}

            def search_objects(self, **kwargs):
                return {"query": kwargs["query"], "items": [], "page": 1, "pageSize": 20, "total": 0}

            def get_object_detail(self, record_ref=None, include_text=True, text_limit=12000, fields=None, fd_id=None):
                return {"recordRef": record_ref, "title": "详情", "text": "", "attachments": []}

            def download_attachment(self, record_ref, attachment_index, output_dir, overwrite=False, max_bytes=52428800, fd_id=None):
                return {"ok": True, "savedPath": str(Path(output_dir) / "a.pdf"), "bytes": 1}

            def batch_search_objects(self, queries, **kwargs):
                return {"items": [], "summary": {"totalQueries": len(queries), "matchedQueries": 0, "errors": 0, "downloads": 0}}

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://oa.example.test/"}, clear=False):
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
                        "params": {"name": "oa_search_objects", "arguments": {"query": "abc", "scope": "knowledge"}},
                    })
                    self.assertEqual(json.loads(searched["result"]["content"][0]["text"])["query"], "abc")

                    batched = mcp_server.handle({
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "oa_batch_search_objects", "arguments": {"queries": ["a", "b"], "scope": "knowledge"}},
                    })
                    self.assertEqual(json.loads(batched["result"]["content"][0]["text"])["summary"]["totalQueries"], 2)


class SensitiveOutputRegressionTest(unittest.TestCase):
    def test_new_tool_error_output_does_not_leak_sensitive_patterns(self):
        class LeakyClient(FakeClient):
            def search_objects(self, **kwargs):
                raise RuntimeError("Cookie: abc; Set-Cookie: def; JSESSIONID=ghi; Authorization: Bearer token; <html>full</html>")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://oa.example.test/"}, clear=False):
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
            with patch.dict("os.environ", {"OA_AGENT_STATE_DIR": tmpdir, "OA_BASE_URL": "http://oa.example.test/"}, clear=False):
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
