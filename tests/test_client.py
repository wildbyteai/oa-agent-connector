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


class SearchSchemaTest(unittest.TestCase):
    def test_get_search_schema_for_knowledge(self):
        client = FakeOAClient("{}")
        schema = client.get_search_schema("knowledge")

        self.assertEqual(schema["scope"], "knowledge")
        self.assertIn("KmsMultidocKnowledge", [m["modelName"] for m in schema["models"]])
        self.assertIn("title", schema["searchFields"])
        self.assertIn("attachment", schema["searchFields"])
        self.assertEqual(schema["limits"]["queryMaxLength"], 200)
        self.assertEqual(schema["limits"]["pageSizeMax"], 50)
        self.assertEqual(schema["limits"]["batchQueriesMax"], 100)
        self.assertEqual(schema["limits"]["detailTextLimitMax"], 20000)
        self.assertEqual(schema["limits"]["downloadMaxBytesDefault"], 52428800)

    def test_get_search_schema_rejects_unknown_scope(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client.get_search_schema("finance-secret")
        self.assertIn("不支持的搜索范围或模块", str(ctx.exception))


class SearchValidationTest(unittest.TestCase):
    def test_validate_search_params_maps_and_defaults(self):
        client = FakeOAClient("{}")
        params = client._validate_search_params({
            "query": "出厂报告-产品A",
            "scope": "knowledge",
            "modelName": "KmsMultidocKnowledge",
            "bond": "like",
            "searchFields": ["title", "attachment"],
            "docFileType": "pdf",
            "sortType": "time",
            "sortOrder": "desc",
            "page": 1,
            "pageSize": 20,
        })

        self.assertEqual(params["query"], "出厂报告-产品A")
        self.assertEqual(params["scope"], "knowledge")
        self.assertEqual(params["modelName"], "KmsMultidocKnowledge")
        self.assertEqual(params["searchFields"], ["subject", "attachment"])
        self.assertEqual(params["pageSize"], 20)

    def test_validate_search_params_rejects_bad_values(self):
        client = FakeOAClient("{}")
        bad_cases = [
            {"query": ""},
            {"query": "x" * 201},
            {"query": "abc\x00def"},
            {"query": "x", "scope": "unknown"},
            {"query": "x", "scope": "knowledge", "modelName": "BadModel"},
            {"query": "x", "bond": "near"},
            {"query": "x", "searchFields": ["rawSql"]},
            {"query": "x", "docFileType": "exe"},
            {"query": "x", "sortType": "fd_secret"},
            {"query": "x", "sortOrder": "sideways"},
            {"query": "x", "timeRange": "decade"},
            {"query": "x", "fromCreateTime": "2026/07/06"},
            {"query": "x", "fromCreateTime": "2026-07-07", "toCreateTime": "2026-07-06"},
            {"query": "x", "pageSize": 51},
        ]
        for case in bad_cases:
            with self.subTest(case=case):
                with self.assertRaises(OAConnectorError):
                    client._validate_search_params(case)

    def test_validate_search_params_rejects_non_numeric_page(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "test", "page": "abc"})
        self.assertIn("page/pageSize", str(ctx.exception))

    def test_validate_search_params_rejects_non_numeric_page_size(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "test", "pageSize": "not-a-number"})
        self.assertIn("page/pageSize", str(ctx.exception))

    def test_validate_search_params_rejects_newline_in_query(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "hello\nworld"})
        self.assertIn("搜索关键词不合法", str(ctx.exception))

    def test_validate_search_params_rejects_tab_in_query(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "hello\tworld"})
        self.assertIn("搜索关键词不合法", str(ctx.exception))

    def test_validate_search_params_rejects_carriage_return_in_query(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "hello\rworld"})
        self.assertIn("搜索关键词不合法", str(ctx.exception))


class FakeSearchClient(OAClient):
    def __init__(self, payload):
        super().__init__("https://oa.example.test/")
        self.payload = payload
        self.last_request = None

    def _request(self, path, method="GET", params=None, data=None):
        self.last_request = {"path": path, "method": method, "params": params or {}, "data": data}
        return {"url": "https://oa.example.test/search", "text": json.dumps(self.payload, ensure_ascii=False)}


class SearchObjectsTest(unittest.TestCase):
    def test_search_objects_parses_record_ref_and_exact_title(self):
        payload = {
            "queryPage": {
                "totalrows": 2,
                "list": [
                    {
                        "lksFieldsMap": {"subject": "<em>出厂报告-产品A</em>"},
                        "content": "摘要内容",
                        "creator": "张三",
                        "createTime": "2026-07-01",
                        "docReadCount": "4",
                        "modelName": "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge",
                        "modelTitle": "文档知识库",
                        "linkStr": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
                    },
                    {
                        "lksFieldsMap": {"subject": "出厂报告-产品B"},
                        "content": "另一个摘要",
                        "modelName": "KmsMultidocKnowledge",
                        "linkStr": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=28256d188087f3669a0808d440da67a6",
                    },
                ],
            }
        }
        client = FakeSearchClient(payload)

        result = client.search_objects(
            query="出厂报告-产品A",
            scope="knowledge",
            modelName="KmsMultidocKnowledge",
            bond="like",
            searchFields=["title", "attachment"],
            docFileType="pdf",
            sortType="time",
            sortOrder="desc",
            exactTitle=True,
            onlyExactTitle=True,
        )

        self.assertEqual(result["query"], "出厂报告-产品A")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["fdId"], "18256d188087f3669a0808d440da67a6")
        self.assertEqual(item["title"], "出厂报告-产品A")
        self.assertTrue(item["matchedExactTitle"])
        self.assertTrue(item["supportsDetail"])
        self.assertTrue(item["supportsAttachments"])
        self.assertEqual(item["recordRef"]["path"], "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6")
        self.assertEqual(client.last_request["params"]["resultType"], "json")
        self.assertEqual(client.last_request["params"]["bond"], "like")
        self.assertEqual(client.last_request["params"]["docFileType"], "pdf")
        self.assertEqual(client.last_request["params"]["sortType"], "time")
        self.assertEqual(client.last_request["params"]["sortOrder"], "desc")
        self.assertEqual(client.last_request["params"]["searchFields"], "subject,attachment")


if __name__ == "__main__":
    unittest.main()
