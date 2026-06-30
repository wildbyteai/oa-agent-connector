import json
import unittest

from oa_agent_connector.client import OAClient, OAConnectorError, PermissionGateError


class FakeOAClient(OAClient):
    def __init__(self, todo_text):
        super().__init__("https://example.invalid/oa/")
        self.todo_text = todo_text

    def _request(self, path, method="GET", params=None, data=None):
        if params and params.get("method") == "list":
            return {"url": "https://example.invalid/oa/list", "text": self.todo_text}
        return {"url": "https://example.invalid/oa/ok", "text": "ok"}


class TimeoutApprovalClient(FakeOAClient):
    def __init__(self):
        super().__init__(json.dumps({"rows": [{"fdId": "1234567890abcdef1234567890abcdef"}]}))
        self.used_ui_fallback = False

    def _request(self, path, method="GET", params=None, data=None):
        if params and params.get("method") == "list":
            return {"url": "https://example.invalid/oa/list", "text": self.todo_text}
        if path == "api/km-review/kmReviewRestService/approveProcess":
            raise TimeoutError("timed out")
        return {"url": "https://example.invalid/oa/ok", "text": "ok"}

    def _approval_action_via_ui(self, fd_id, operation_type, audit_note, future_node_id=None):
        self.used_ui_fallback = True
        return {"dryRun": False, "fdId": fd_id, "transport": "ui-form", "operationType": operation_type}


class WrappedTimeoutApprovalClient(TimeoutApprovalClient):
    def _request(self, path, method="GET", params=None, data=None):
        if params and params.get("method") == "list":
            return {"url": "https://example.invalid/oa/list", "text": self.todo_text}
        if path == "api/km-review/kmReviewRestService/approveProcess":
            raise OAConnectorError("请求 OA 失败: <urlopen error timed out>")
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

    def test_approval_timeout_falls_back_to_ui_form(self):
        client = TimeoutApprovalClient()
        result = client.approve("1234567890abcdef1234567890abcdef", "同意", execute=True)
        self.assertTrue(client.used_ui_fallback)
        self.assertEqual(result["transport"], "ui-form")

    def test_wrapped_approval_timeout_falls_back_to_ui_form(self):
        client = WrappedTimeoutApprovalClient()
        result = client.approve("1234567890abcdef1234567890abcdef", "同意", execute=True)
        self.assertTrue(client.used_ui_fallback)
        self.assertEqual(result["transport"], "ui-form")

    def test_find_review_workitem_handles_malformed_xml_attrs(self):
        client = FakeOAClient("{}")
        malformed_xml = (
            '<root><task type="reviewWorkitem" id="task-1" data="{"key":"value"}">'
            '<operations><operation id="handler_refuse" /></operations>'
            "</task></root>"
        )
        task = client._find_review_workitem(malformed_xml, "handler_refuse")
        self.assertEqual(task, {"id": "task-1", "type": "reviewWorkitem"})

    def test_find_review_workitem_requires_requested_operation(self):
        client = FakeOAClient("{}")
        current_node_xml = (
            "<root>"
            '<task type="reviewWorkitem" id="pass-task"><operation id="handler_pass" /></task>'
            '<task type="reviewWorkitem" id="refuse-task"><operation id="handler_refuse" /></task>'
            "</root>"
        )
        task = client._find_review_workitem(current_node_xml, "handler_refuse")
        self.assertEqual(task, {"id": "refuse-task", "type": "reviewWorkitem"})


if __name__ == "__main__":
    unittest.main()
