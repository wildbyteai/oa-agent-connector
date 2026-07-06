import json
import tempfile
import unittest
from pathlib import Path

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

    def test_validate_search_params_rejects_bad_match_mode(self):
        client = FakeOAClient("{}")
        with self.assertRaises(OAConnectorError) as ctx:
            client._validate_search_params({"query": "hello", "matchMode": "fuzzy"})
        self.assertIn("标题匹配模式", str(ctx.exception))


class FakeSearchClient(OAClient):
    def __init__(self, payload):
        super().__init__("https://example.invalid/oa/")
        self.payload = payload
        self.last_request = None

    def _request(self, path, method="GET", params=None, data=None):
        self.last_request = {"path": path, "method": method, "params": params or {}, "data": data}
        return {"url": "https://example.invalid/oa/search", "text": json.dumps(self.payload, ensure_ascii=False)}


class SearchObjectsTest(unittest.TestCase):
    def test_search_objects_parses_record_ref_and_exact_title(self):
        payload = {
            "queryPage": {
                "totalrows": 2,
                "list": [
                    {
                        "lksFieldsMap": {"subject": "<em>出厂报告-产品A</em>"},
                        "content": "摘要内容",
                        "creator": "示例用户A",
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
        self.assertEqual(item["normalizedTitle"], "出厂报告-产品A")
        self.assertEqual(item["type"], "document")
        self.assertEqual(item["attachmentCount"], 0)
        self.assertEqual(client.last_request["params"]["resultType"], "json")
        self.assertEqual(client.last_request["params"]["bond"], "like")
        self.assertEqual(client.last_request["params"]["docFileType"], "pdf")
        self.assertEqual(client.last_request["params"]["sortType"], "time")
        self.assertEqual(client.last_request["params"]["sortOrder"], "desc")
        self.assertEqual(client.last_request["params"]["searchFields"], "subject,attachment")

    def test_search_objects_normalizes_cjk_spaces_for_contains_and_exact_modes(self):
        payload = {
            "queryPage": {
                "totalrows": 2,
                "list": [
                    {
                        "lksFieldsMap": {
                            "subject": {"value": "三 草 两 木 白 管 星 钻 唇膏"},
                            "modelName": {"value": "KmsMultidocKnowledge"},
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6"
                            },
                        },
                    },
                    {
                        "lksFieldsMap": {
                            "subject": {"value": "无关文档"},
                            "modelName": {"value": "KmsMultidocKnowledge"},
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=28256d188087f3669a0808d440da67a6"
                            },
                        },
                    },
                ],
            }
        }
        client = FakeSearchClient(payload)

        contains = client.search_objects(query="三草两木白管星钻唇膏", scope="knowledge", matchMode="contains")
        exact = client.search_objects(query="三草两木白管星钻唇膏", scope="knowledge", matchMode="exact")
        legacy = client.search_objects(query="三草两木白管星钻唇膏", scope="knowledge", onlyExactTitle=True)

        self.assertEqual(len(contains["items"]), 1)
        self.assertEqual(contains["items"][0]["title"], "三 草 两 木 白 管 星 钻 唇膏")
        self.assertEqual(contains["items"][0]["normalizedTitle"], "三草两木白管星钻唇膏")
        self.assertTrue(contains["items"][0]["matchedContainsTitle"])
        self.assertEqual(contains["matchMode"], "contains")
        self.assertEqual(len(exact["items"]), 1)
        self.assertTrue(exact["items"][0]["matchedExactTitle"])
        self.assertEqual(len(legacy["items"]), 1)
        self.assertEqual(legacy["matchMode"], "exact")

    def test_search_objects_dedups_attachment_rows_by_document_by_default(self):
        payload = {
            "queryPage": {
                "totalrows": 3,
                "list": [
                    {
                        "lksFieldsMap": {
                            "subject": {"value": "产品资料.pdf"},
                            "modelName": {"value": "KmsMultidocKnowledge"},
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6"
                            },
                        },
                    },
                    {
                        "lksFieldsMap": {
                            "subject": {"value": "产品资料.jpg"},
                            "modelName": {"value": "KmsMultidocKnowledge"},
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6"
                            },
                        },
                    },
                    {
                        "lksFieldsMap": {
                            "subject": {"value": "产品资料"},
                            "modelName": {"value": "KmsMultidocKnowledge"},
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6"
                            },
                        },
                    },
                ],
            }
        }
        client = FakeSearchClient(payload)

        deduped = client.search_objects(query="产品资料", scope="knowledge", matchMode="contains")
        raw = client.search_objects(query="产品资料", scope="knowledge", matchMode="contains", dedupByDocument=False)

        self.assertTrue(deduped["dedupByDocument"])
        self.assertEqual(deduped["filteredCount"], 3)
        self.assertEqual(deduped["returnedCount"], 1)
        self.assertEqual(len(deduped["items"]), 1)
        self.assertEqual(deduped["items"][0]["title"], "产品资料")
        self.assertEqual(deduped["items"][0]["type"], "document")
        self.assertEqual(deduped["items"][0]["attachmentCount"], 2)
        self.assertEqual(deduped["items"][0]["attachmentTitles"], ["产品资料.pdf", "产品资料.jpg"])
        self.assertFalse(raw["dedupByDocument"])
        self.assertEqual(len(raw["items"]), 3)
        self.assertEqual([item["type"] for item in raw["items"]], ["attachment", "attachment", "document"])

    def test_search_objects_parses_landray_lks_field_values(self):
        payload = {
            "queryPage": {
                "totalrows": 1,
                "list": [
                    {
                        "docId": "ignored",
                        "lksFieldsMap": {
                            "docKey": {
                                "value": "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge_18721823f0e840b54ca85da4cfeb56c6_18721823db8fad6093486154f38bc47e"
                            },
                            "linkStr": {
                                "value": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18721823f0e840b54ca85da4cfeb56c6"
                            },
                            "modelName": {"value": "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge"},
                            "modelName2": "cn-文档知识库",
                            "fileName": {"value": "仓库考勤制度 &#40;2&#41;.doc"},
                            "fullText": {"value": "任何补偿开除。<font>请假</font>流程"},
                            "creator": {"value": "示例用户"},
                            "createTime": {"value": "2023-03-27"},
                        },
                    }
                ],
            }
        }
        client = FakeSearchClient(payload)

        result = client.search_objects(query="请假", scope="knowledge", pageSize=5)

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["fdId"], "18721823f0e840b54ca85da4cfeb56c6")
        self.assertEqual(item["title"], "仓库考勤制度 (2).doc")
        self.assertIn("请假", item["summary"])
        self.assertIn("流程", item["summary"])
        self.assertEqual(item["creator"], "示例用户")
        self.assertEqual(item["createTime"], "2023-03-27")
        self.assertTrue(item["supportsDetail"])
        self.assertTrue(item["supportsAttachments"])


class FakeDetailClient(OAClient):
    def __init__(self, html_text):
        super().__init__("https://example.invalid/oa/")
        self.html_text = html_text
        self.last_request = None

    def _request(self, path, method="GET", params=None, data=None):
        self.last_request = {"path": path, "method": method, "params": params or {}, "data": data}
        return {"url": "https://example.invalid/oa/detail", "text": self.html_text}


class ObjectDetailTest(unittest.TestCase):
    def test_get_object_detail_extracts_text_and_attachments(self):
        html_text = """
        <html><head><title>出厂报告-产品A</title><script>var token='secret';</script></head>
        <body>
          <nav>首页 导航</nav>
          <input type="hidden" name="csrf" value="hidden-token">
          <div id="docContent">正文第一段 <b>正文第二段</b></div>
          <script>
            attachmentObject_attachment.addDoc('att-1','file-1','附件一.pdf','application/pdf','200261');
            attachmentObject_attachment.addDoc("att-2","file-2","\\u9644\\u4ef6\\u4e8c.docx","application/vnd.openxmlformats-officedocument.wordprocessingml.document","1024");
            attachmentObject_attachment.addDoc("\\u793A\\u4F8B\\u9644\\u4EF6.pdf","1710b67825225c4d93e765b4429afb93",true,"application/pdf","51567.0","1710b67451705fddd2624874bb1b55a4","0");
          </script>
        </body></html>
        """
        client = FakeDetailClient(html_text)
        ref = {
            "scope": "knowledge",
            "modelName": "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge",
            "recordId": "18256d188087f3669a0808d440da67a6",
            "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
        }

        detail = client.get_object_detail(record_ref=ref, include_text=True, text_limit=12000)

        self.assertEqual(detail["title"], "出厂报告-产品A")
        self.assertIn("正文第一段", detail["text"])
        self.assertNotIn("hidden-token", detail["text"])
        self.assertNotIn("secret", detail["text"])
        self.assertEqual(len(detail["attachments"]), 3)
        self.assertEqual(detail["attachments"][0]["index"], 1)
        self.assertEqual(detail["attachments"][0]["name"], "附件一.pdf")
        self.assertEqual(detail["attachments"][0]["attachmentId"], "att-1")
        self.assertEqual(detail["attachments"][0]["fileId"], "file-1")
        self.assertEqual(detail["attachments"][0]["size"], 200261)
        self.assertNotIn("url", detail["attachments"][0])
        self.assertEqual(detail["attachments"][2]["name"], "示例附件.pdf")
        self.assertEqual(detail["attachments"][2]["attachmentId"], "1710b67825225c4d93e765b4429afb93")
        self.assertEqual(detail["attachments"][2]["fileId"], "1710b67451705fddd2624874bb1b55a4")
        self.assertEqual(detail["attachments"][2]["mimeType"], "application/pdf")
        self.assertEqual(detail["attachments"][2]["size"], 51567)

    def test_validate_record_ref_rejects_unsafe_paths(self):
        client = FakeDetailClient("ok")
        bad_refs = [
            {"scope": "knowledge", "modelName": "KmsMultidocKnowledge", "recordId": "1", "path": "https://evil.test/a"},
            {"scope": "knowledge", "modelName": "KmsMultidocKnowledge", "recordId": "1", "path": "//evil.test/a"},
            {"scope": "knowledge", "modelName": "KmsMultidocKnowledge", "recordId": "1", "path": "/kms/../secret?fdId=1"},
            {"scope": "unknown", "modelName": "KmsMultidocKnowledge", "recordId": "1", "path": "/kms/a?fdId=1"},
            {"scope": "knowledge", "modelName": "BadModel", "recordId": "1", "path": "/kms/a?fdId=1"},
        ]
        for ref in bad_refs:
            with self.subTest(ref=ref):
                with self.assertRaises(OAConnectorError):
                    client._validate_record_ref(ref)


class FakeDownloadClient(OAClient):
    def __init__(self, detail_html, download_bytes):
        super().__init__("https://example.invalid/oa/")
        self.detail_html = detail_html
        # download_bytes 可以是 str（向后兼容）或 bytes
        if isinstance(download_bytes, bytes):
            self.download_bytes = download_bytes
        else:
            self.download_bytes = download_bytes.encode("utf-8")
        self.requests = []

    def _request(self, path, method="GET", params=None, data=None):
        self.requests.append({"path": path, "method": method, "params": params or {}, "data": data})
        return {"url": "https://example.invalid/oa/detail", "text": self.detail_html}

    def _request_bytes(self, path, method="GET", params=None, data=None):
        self.requests.append({"path": path, "method": method, "params": params or {}, "data": data})
        if "sys_att_main" in path:
            return {"url": "https://example.invalid/oa/sys/attachment/sys_att_main/sysAttMain.do", "bytes": self.download_bytes}
        return {"url": "https://example.invalid/oa/detail", "bytes": self.detail_html.encode("utf-8")}


class AttachmentDownloadTest(unittest.TestCase):
    def test_safe_filename_removes_path_tricks(self):
        client = FakeDownloadClient("", "")
        self.assertEqual(client._safe_filename("../报告/产品A.pdf"), "产品A.pdf")
        self.assertEqual(client._safe_filename("C:\\tmp\\产品A.pdf"), "产品A.pdf")
        self.assertEqual(client._safe_filename("bad\x00:name?.pdf"), "bad_name_.pdf")
        self.assertEqual(client._safe_filename(""), "attachment")

    def test_download_attachment_saves_file_and_avoids_duplicates(self):
        detail_html = """
        <html><title>出厂报告-产品A</title><body>
        <script>attachmentObject_attachment.addDoc('att-1','file-1','../报告/产品A.pdf','application/pdf','3');</script>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            client = FakeDownloadClient(detail_html, "PDF")
            ref = {
                "scope": "knowledge",
                "modelName": "KmsMultidocKnowledge",
                "recordId": "18256d188087f3669a0808d440da67a6",
                "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
            }
            first = client.download_attachment(ref, attachment_index=1, output_dir=tmpdir, overwrite=False, max_bytes=10)
            second = client.download_attachment(ref, attachment_index=1, output_dir=tmpdir, overwrite=False, max_bytes=10)

            self.assertTrue(Path(first["savedPath"]).exists())
            self.assertTrue(Path(second["savedPath"]).exists())
            self.assertTrue(first["savedPath"].endswith("产品A.pdf"))
            self.assertTrue(second["savedPath"].endswith("产品A (1).pdf"))

    def test_download_attachment_supports_landray_filename_first_signature(self):
        detail_html = """
        <html><title>真实附件格式</title><body>
        <script>attachmentObject_attachment.addDoc("真实附件.pdf","1710b67825225c4d93e765b4429afb93",true,"application/pdf","3.0","1710b67451705fddd2624874bb1b55a4","0");</script>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            client = FakeDownloadClient(detail_html, b"PDF")
            ref = {
                "scope": "knowledge",
                "modelName": "KmsMultidocKnowledge",
                "recordId": "18256d188087f3669a0808d440da67a6",
                "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
            }
            result = client.download_attachment(ref, attachment_index=1, output_dir=tmpdir, overwrite=False, max_bytes=10)

            self.assertTrue(result["savedPath"].endswith("真实附件.pdf"))
            download_request = client.requests[-1]["path"]
            self.assertIn("fdId=1710b67825225c4d93e765b4429afb93", download_request)
            self.assertNotIn("fdId=%E7%9C%9F%E5%AE%9E%E9%99%84%E4%BB%B6.pdf", download_request)
            self.assertEqual(result["bytes"], 3)

    def test_download_rejects_html_response_and_large_file(self):
        detail_html = """
        <script>attachmentObject_attachment.addDoc('att-1','file-1','产品A.pdf','application/pdf','100');</script>
        """
        ref = {
            "scope": "knowledge",
            "modelName": "KmsMultidocKnowledge",
            "recordId": "18256d188087f3669a0808d440da67a6",
            "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            html_client = FakeDownloadClient(detail_html, b"<html>login</html>")
            with self.assertRaises(OAConnectorError):
                html_client.download_attachment(ref, 1, tmpdir, max_bytes=1000)

            large_client = FakeDownloadClient(detail_html, b"x" * 11)
            with self.assertRaises(OAConnectorError):
                large_client.download_attachment(ref, 1, tmpdir, max_bytes=10)

    def test_download_binary_bytes_preserved_exactly(self):
        """验证二进制附件（含非 UTF-8 字节）下载后文件内容完全一致。"""
        detail_html = """
        <script>attachmentObject_attachment.addDoc('att-1','file-1','report.bin','application/octet-stream','8');</script>
        """
        # 构造含非 UTF-8 字节的二进制数据
        binary_data = bytes(range(256))[:64]  # 0x00..0x3F，包含 0x00 等控制字符
        ref = {
            "scope": "knowledge",
            "modelName": "KmsMultidocKnowledge",
            "recordId": "18256d188087f3669a0808d440da67a6",
            "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            client = FakeDownloadClient(detail_html, binary_data)
            result = client.download_attachment(ref, 1, tmpdir, max_bytes=1024)
            saved = Path(result["savedPath"])
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), binary_data)
            self.assertEqual(result["bytes"], len(binary_data))

    def test_download_pdf_bytes_preserved(self):
        """验证模拟 PDF 二进制数据下载后完全一致。"""
        detail_html = """
        <script>attachmentObject_attachment.addDoc('att-1','file-1','doc.pdf','application/pdf','8');</script>
        """
        # PDF 魔数 + 非 UTF-8 数据
        pdf_header = b"%PDF-1.4\n"
        pdf_body = b"\x80\x81\x82\x83\xff\xfe\xfd"
        pdf_data = pdf_header + pdf_body
        ref = {
            "scope": "knowledge",
            "modelName": "KmsMultidocKnowledge",
            "recordId": "18256d188087f3669a0808d440da67a6",
            "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            client = FakeDownloadClient(detail_html, pdf_data)
            result = client.download_attachment(ref, 1, tmpdir, max_bytes=1024)
            saved = Path(result["savedPath"])
            self.assertEqual(saved.read_bytes(), pdf_data)
            self.assertEqual(result["bytes"], len(pdf_data))


class FakeBatchClient(FakeSearchClient):
    def __init__(self):
        super().__init__({})
        self.downloads = []

    def search_objects(self, **kwargs):
        query = kwargs["query"]
        if query == "bad":
            raise OAConnectorError("搜索出现错误：模拟失败 Cookie=secret")
        matched = query == "出厂报告-产品A"
        return {
            "query": query,
            "items": [
                {
                    "recordRef": {
                        "scope": "knowledge",
                        "modelName": "KmsMultidocKnowledge",
                        "recordId": "18256d188087f3669a0808d440da67a6",
                        "path": "/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId=18256d188087f3669a0808d440da67a6",
                    },
                    "title": query,
                    "matchedExactTitle": matched,
                    "attachments": [],
                }
            ] if matched else [],
            "page": 1,
            "pageSize": 5,
            "total": 1 if matched else 0,
        }

    def get_object_detail(self, record_ref=None, include_text=True, text_limit=12000, fields=None, fd_id=None):
        return {
            "recordRef": record_ref,
            "title": "出厂报告-产品A",
            "text": "" if not include_text else "正文",
            "textExtractionWarning": "",
            "attachments": [{"index": 1, "name": "报告.pdf", "mimeType": "application/pdf", "size": 3, "downloadable": True}],
        }

    def download_attachment(self, record_ref, attachment_index, output_dir, overwrite=False, max_bytes=52428800, fd_id=None):
        self.downloads.append(record_ref["recordId"])
        return {"ok": True, "savedPath": str(Path(output_dir).expanduser() / "报告.pdf"), "bytes": 3}


class BatchSearchObjectsTest(unittest.TestCase):
    def test_batch_search_continues_after_single_error_and_sanitizes_error(self):
        client = FakeBatchClient()
        result = client.batch_search_objects(
            queries=["出厂报告-产品A", "bad", "无结果"],
            scope="knowledge",
            modelName="KmsMultidocKnowledge",
            bond="like",
            exactTitle=True,
            onlyExactTitle=True,
            pageSize=5,
            includeAttachments=True,
            maxDetailsPerQuery=1,
        )

        self.assertEqual(result["summary"]["totalQueries"], 3)
        self.assertEqual(result["summary"]["matchedQueries"], 1)
        self.assertEqual(result["summary"]["errors"], 1)
        self.assertEqual(result["items"][0]["results"][0]["attachments"][0]["name"], "报告.pdf")
        self.assertNotIn("Cookie", result["items"][1]["error"])

    def test_batch_download_requires_exact_title_and_positive_max_downloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            client = FakeBatchClient()
            result = client.batch_search_objects(
                queries=["出厂报告-产品A"],
                scope="knowledge",
                modelName="KmsMultidocKnowledge",
                bond="like",
                exactTitle=True,
                onlyExactTitle=True,
                includeAttachments=True,
                downloadFirstAttachment=True,
                maxDownloads=1,
                outputDir=tmpdir,
            )
            self.assertEqual(result["summary"]["downloads"], 1)
            self.assertEqual(len(client.downloads), 1)

    def test_batch_search_rejects_page_size_above_20(self):
        """batch=True 时 pageSize=21 应被拒绝（batchPageSizeMax=20）。"""
        client = FakeBatchClient()
        with self.assertRaises(OAConnectorError) as ctx:
            client.batch_search_objects(
                queries=["test"],
                pageSize=21,
            )
        self.assertIn("pageSize", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
