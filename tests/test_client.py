import json
import unittest

from oa_agent_connector.client import OAClient, PermissionGateError


class FakeOAClient(OAClient):
    def __init__(self, todo_text):
        super().__init__("https://example.invalid/oa/")
        self.todo_text = todo_text

    def _request(self, path, method="GET", params=None, data=None):
        if params and params.get("method") == "list":
            return {"url": "https://example.invalid/oa/list", "text": self.todo_text}
        return {"url": "https://example.invalid/oa/ok", "text": "ok"}


class OAClientTest(unittest.TestCase):
    def test_parse_json_todos(self):
        client = FakeOAClient(json.dumps({"rows": [{"fdId": "1234567890abcdef1234567890abcdef", "docSubject": "采购审批"}]}))
        todos = client.list_todos()
        self.assertEqual(todos[0].fd_id, "1234567890abcdef1234567890abcdef")
        self.assertEqual(todos[0].subject, "采购审批")

    def test_parse_landray_column_datas(self):
        payload = {
            "columns": [{"property": "fdId"}, {"property": "docSubject"}],
            "datas": [[
                {"col": "fdId", "value": "1234567890abcdef1234567890abcdef"},
                {"col": "docSubject", "value": "<span class=\"com_subject\">采购审批</span>"},
            ]],
        }
        client = FakeOAClient(json.dumps(payload))
        todos = client.list_todos()
        self.assertEqual(todos[0].fd_id, "1234567890abcdef1234567890abcdef")
        self.assertEqual(todos[0].subject, "采购审批")

    def test_approval_dry_run_requires_current_todo(self):
        client = FakeOAClient(json.dumps({"rows": [{"fdId": "1234567890abcdef1234567890abcdef"}]}))
        result = client.approve("1234567890abcdef1234567890abcdef", "同意")
        self.assertTrue(result["dryRun"])
        flow_param = json.loads(result["payload"]["flowParam"])
        self.assertNotIn("handler", flow_param)

        with self.assertRaises(PermissionGateError):
            client.approve("ffffffffffffffffffffffffffffffff", "同意")


if __name__ == "__main__":
    unittest.main()
