# OA MCP Search Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 `oa-agent-connector` MCP 增加通用只读 OA 搜索、详情读取、附件元数据解析、受控附件下载和受限批量搜索能力。

**Architecture:** 保留现有 `OAClient` 与 `mcp_server.py` 风格，在 `OAClient` 内新增配置化 schema、搜索参数校验、recordRef 校验、详情/附件解析和下载安全 helper；在 MCP 层追加 5 个只读工具并复用 `_ok` / `_mcp_error` 返回格式。实现先用标准库完成 MVP，不新增第三方依赖；未知模块只返回搜索元数据，不进入详情解析或下载。

**Tech Stack:** Python 3.9+、标准库 `urllib` / `html.parser` / `json` / `re` / `pathlib` / `tempfile`、`unittest`、现有 MCP JSON-RPC 手写入口。

## Global Constraints

- 不新增第三方依赖，`pyproject.toml` 的 `dependencies = []` 保持不变。
- 新增工具必须只复用当前登录 session/cookie，不开放任意 `baseUrl`、`insecure`、任意 URL 下载或任意附件 ID 下载。
- 搜索、详情、列附件属于 OA 读取；附件下载属于 OA 读取 + 本地文件写入；不得宣称“无痕”或“完全无副作用”。
- MVP 不提供新增、更新、编辑、删除、审批、流程推进、代办处理、状态变更或数据库写入。
- `scope`、`modelName`、`searchFields`、`docFileType`、`sortType`、`sortOrder`、`bond`、`timeRange` 必须走白名单。
- 搜索 `query` 必填，最大 200 字符，拒绝控制字符；搜索 `pageSize` 默认 20、最大 50；批量 `queries` 最大 100，批量 `pageSize` 默认 5、最大 20。
- 详情正文 `textLimit` 默认 12000、最大 20000；批量默认不返回正文。
- 附件下载 `maxBytes` 默认 52428800；批量 `maxDownloads` 默认 0、最大 50，必须显式大于 0 才允许批量下载。
- 输出不得包含 cookie、`Set-Cookie`、`JSESSIONID`、`Authorization`、密码、完整请求头、完整错误 HTML、附件内容或下载 URL。
- 现有审批安全流必须保持：`oa_approve` / `oa_reject` 禁止直接 `execute=true`，正式执行仍需 `oa_prepare_approval -> 用户确认 -> oa_confirm_approval`。
- 当前目录不是 git 仓库；执行完成并需要发布时，必须同步更新 `github_publish/` 副本中的同名文件和测试。

---

## File Structure

- Modify: `oa_agent_connector/client.py`
  - 新增常量：搜索 schema、scope/model 白名单、限制值、字段映射。
  - 新增公开方法：`get_search_schema()`、`search_objects()`、`get_object_detail()`、`download_attachment()`、`batch_search_objects()`。
  - 新增私有 helper：参数校验、recordRef 校验、搜索结果解析、详情正文提取、知识文档附件解析、安全文件名、受控下载。
- Modify: `oa_agent_connector/mcp_server.py`
  - 追加 5 个 `_tool_schema(...)`：`oa_get_search_schema`、`oa_search_objects`、`oa_get_object_detail`、`oa_download_attachment`、`oa_batch_search_objects`。
  - 在 `call_tool(...)` 中追加 5 个分支；新工具不接受 `baseUrl` / `insecure`。
- Modify: `tests/test_client.py`
  - 增加搜索 schema、参数校验、搜索解析、精确标题匹配、recordRef 校验、附件解析、安全文件名、下载安全、批量行为单元测试。
- Modify: `tests/test_mcp_server.py`
  - 增加新增工具 schema 和 `call_tool` 分支测试，确保 `additionalProperties=false` 且未声明参数不进入 client。
- Modify after implementation if publishing: `github_publish/oa_agent_connector/client.py`
  - 与主实现保持一致。
- Modify after implementation if publishing: `github_publish/oa_agent_connector/mcp_server.py`
  - 与主实现保持一致。
- Modify after implementation if publishing: `github_publish/tests/test_client.py`
  - 与主测试保持一致。
- Modify after implementation if publishing: `github_publish/tests/test_mcp_server.py`
  - 与主测试保持一致。

---

### Task 1: Add Search Schema and Parameter Validation

**Files:**
- Modify: `oa_agent_connector/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: existing `OAClient`, `OAConnectorError`.
- Produces:
  - `OAClient.get_search_schema(scope: str = "all") -> Dict[str, Any]`
  - `OAClient._validate_search_params(params: Dict[str, Any], *, batch: bool = False) -> Dict[str, Any]`
  - `OAClient._normalize_model_name(scope: str, model_name: Optional[str]) -> Optional[str]`
  - `OAClient._scope_config(scope: str) -> Dict[str, Any]`

- [ ] **Step 1: Write failing tests for schema and allowed values**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run the new schema tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.SearchSchemaTest -v
```

Expected: FAIL with `AttributeError: 'FakeOAClient' object has no attribute 'get_search_schema'`.

- [ ] **Step 3: Add schema constants and `get_search_schema`**

Add near the existing regex constants in `oa_agent_connector/client.py`:

```python
SEARCH_LIMITS = {
    "queryMaxLength": 200,
    "pageSizeDefault": 20,
    "pageSizeMax": 50,
    "batchQueriesMax": 100,
    "batchPageSizeDefault": 5,
    "batchPageSizeMax": 20,
    "maxDetailsPerQueryDefault": 1,
    "maxDetailsPerQueryMax": 3,
    "detailTextLimitDefault": 12000,
    "detailTextLimitMax": 20000,
    "downloadMaxBytesDefault": 52428800,
    "batchMaxDownloadsDefault": 0,
    "batchMaxDownloadsMax": 50,
}

SEARCH_FIELD_MAP = {
    "title": "subject",
    "content": "content",
    "fdDescription": "fdDescription",
    "creator": "creator",
    "attachment": "attachment",
}

SEARCH_SCOPES = {
    "all": {
        "description": "OA 全系统搜索，默认只返回搜索结果元数据，不进入未知模块详情解析",
        "allowedModelNames": ["*"],
        "models": [
            {"modelName": "*", "title": "全部", "supportsDetail": False, "supportsAttachments": False},
        ],
    },
    "knowledge": {
        "description": "文档知识库",
        "allowedModelNames": [
            "KmsMultidocKnowledge",
            "com.landray.kmss.kms.multidoc.model.KmsMultidocKnowledge",
        ],
        "detailParser": "kms_multidoc_knowledge",
        "models": [
            {
                "modelName": "KmsMultidocKnowledge",
                "title": "文档知识库",
                "supportsDetail": True,
                "supportsAttachments": True,
            },
        ],
    },
    "news": {
        "description": "新闻文档",
        "allowedModelNames": ["SysNewsMain", "com.landray.kmss.sys.news.model.SysNewsMain"],
        "models": [
            {"modelName": "SysNewsMain", "title": "新闻文档", "supportsDetail": False, "supportsAttachments": False},
        ],
    },
}

ALLOWED_BONDS = ("or", "and", "like")
ALLOWED_SORT_TYPES = ("relevance", "readCount", "time")
ALLOWED_SORT_ORDERS = ("asc", "desc")
ALLOWED_TIME_RANGES = ("", "day", "week", "month", "year")
ALLOWED_DOC_FILE_TYPES = ("", "pdf", "doc;docx", "xls;xlsx", "ppt;pptx", "txt")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
```

Add methods inside `OAClient` before `login(...)`:

```python
    def get_search_schema(self, scope: str = "all") -> Dict[str, Any]:
        scope = scope or "all"
        config = self._scope_config(scope)
        return {
            "scope": scope,
            "models": list(config.get("models", [])),
            "searchFields": list(SEARCH_FIELD_MAP.keys()),
            "bond": list(ALLOWED_BONDS),
            "sortTypes": list(ALLOWED_SORT_TYPES),
            "sortOrders": list(ALLOWED_SORT_ORDERS),
            "timeRanges": [value for value in ALLOWED_TIME_RANGES if value],
            "docFileTypes": [value for value in ALLOWED_DOC_FILE_TYPES if value],
            "limits": dict(SEARCH_LIMITS),
        }

    def _scope_config(self, scope: str) -> Dict[str, Any]:
        if scope not in SEARCH_SCOPES:
            raise OAConnectorError("不支持的搜索范围或模块")
        return SEARCH_SCOPES[scope]

    def _normalize_model_name(self, scope: str, model_name: Optional[str]) -> Optional[str]:
        if not model_name:
            return None
        allowed = self._scope_config(scope).get("allowedModelNames", [])
        if "*" not in allowed and model_name not in allowed:
            raise OAConnectorError("不支持的搜索范围或模块")
        return str(model_name)
```

- [ ] **Step 4: Run schema tests and verify they pass**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.SearchSchemaTest -v
```

Expected: PASS.

- [ ] **Step 5: Write failing validation tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 6: Run validation tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.SearchValidationTest -v
```

Expected: FAIL with missing `_validate_search_params`.

- [ ] **Step 7: Implement `_validate_search_params`**

Add inside `OAClient` after `_normalize_model_name(...)`:

```python
    def _validate_search_params(self, params: Dict[str, Any], *, batch: bool = False) -> Dict[str, Any]:
        query = str(params.get("query") or "").strip()
        if not query:
            raise OAConnectorError("搜索关键词不能为空")
        if len(query) > SEARCH_LIMITS["queryMaxLength"] or CONTROL_CHAR_RE.search(query):
            raise OAConnectorError("搜索关键词不合法")

        scope = str(params.get("scope") or "all")
        self._scope_config(scope)
        model_name = self._normalize_model_name(scope, params.get("modelName"))

        bond = str(params.get("bond") or "or")
        if bond not in ALLOWED_BONDS:
            raise OAConnectorError("不支持的关键词关系")

        raw_fields = params.get("searchFields") or []
        if isinstance(raw_fields, str):
            raw_fields = [raw_fields]
        search_fields: List[str] = []
        for field in raw_fields:
            field = str(field)
            if field not in SEARCH_FIELD_MAP:
                raise OAConnectorError("不支持的搜索字段")
            search_fields.append(SEARCH_FIELD_MAP[field])

        doc_file_type = str(params.get("docFileType") or "")
        if doc_file_type not in ALLOWED_DOC_FILE_TYPES:
            raise OAConnectorError("不支持的附件类型")

        sort_type = str(params.get("sortType") or "relevance")
        if sort_type not in ALLOWED_SORT_TYPES:
            raise OAConnectorError("不支持的排序字段")
        sort_order = str(params.get("sortOrder") or "desc")
        if sort_order not in ALLOWED_SORT_ORDERS:
            raise OAConnectorError("不支持的排序方向")

        time_range = str(params.get("timeRange") or "")
        if time_range not in ALLOWED_TIME_RANGES:
            raise OAConnectorError("不支持的时间范围")

        from_create_time = str(params.get("fromCreateTime") or "")
        to_create_time = str(params.get("toCreateTime") or "")
        for value in (from_create_time, to_create_time):
            if value and not DATE_RE.match(value):
                raise OAConnectorError("日期格式必须为 YYYY-MM-DD")
        if from_create_time and to_create_time and from_create_time > to_create_time:
            raise OAConnectorError("开始日期不得晚于结束日期")

        default_page_size = SEARCH_LIMITS["batchPageSizeDefault"] if batch else SEARCH_LIMITS["pageSizeDefault"]
        max_page_size = SEARCH_LIMITS["batchPageSizeMax"] if batch else SEARCH_LIMITS["pageSizeMax"]
        page = int(params.get("page") or 1)
        page_size = int(params.get("pageSize") or default_page_size)
        if page < 1:
            raise OAConnectorError("页码必须大于 0")
        if page_size < 1 or page_size > max_page_size:
            raise OAConnectorError("pageSize 超过允许范围")

        return {
            "query": query,
            "scope": scope,
            "modelName": model_name,
            "bond": bond,
            "outKeyword": str(params.get("outKeyword") or ""),
            "searchFields": search_fields,
            "docFileType": doc_file_type,
            "timeRange": time_range,
            "fromCreateTime": from_create_time,
            "toCreateTime": to_create_time,
            "category": str(params.get("category") or ""),
            "docStatus": str(params.get("docStatus") or ""),
            "sortType": sort_type,
            "sortOrder": sort_order,
            "exactTitle": bool(params.get("exactTitle", False)),
            "onlyExactTitle": bool(params.get("onlyExactTitle", False)),
            "page": page,
            "pageSize": page_size,
        }
```

- [ ] **Step 8: Run validation tests and all existing client tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_client.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py tests/test_client.py
git commit -m "feat: add OA search schema validation"
```

Expected: commit succeeds if this directory is a git repository. If `fatal: not a git repository` appears, record that no commit was made and continue.

---

### Task 2: Implement OA Search Objects and recordRef Parsing

**Files:**
- Modify: `oa_agent_connector/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: Task 1 `get_search_schema`, `_validate_search_params`, `_scope_config`.
- Produces:
  - `OAClient.search_objects(**kwargs: Any) -> Dict[str, Any]`
  - `OAClient._parse_search_results(payload: Any, validated: Dict[str, Any]) -> Dict[str, Any]`
  - `OAClient._record_ref_from_search_row(row: Dict[str, Any], scope: str) -> Optional[Dict[str, str]]`
  - `OAClient._clean_search_title(value: Any) -> str`

- [ ] **Step 1: Write failing search parsing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run the search test and verify it fails**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.SearchObjectsTest -v
```

Expected: FAIL with missing `search_objects`.

- [ ] **Step 3: Implement `search_objects` and search result parsing**

Add inside `OAClient` after existing `list_todos(...)`:

```python
    def search_objects(self, **kwargs: Any) -> Dict[str, Any]:
        validated = self._validate_search_params(kwargs)
        params: Dict[str, str] = {
            "method": "search",
            "resultType": "json",
            "newLUI": "true",
            "searchAll": "true",
            "queryString": validated["query"],
            "pageno": str(validated["page"]),
            "rowsize": str(validated["pageSize"]),
            "bond": validated["bond"],
        }
        if validated["outKeyword"]:
            params["outKeyword"] = validated["outKeyword"]
        if validated["searchFields"]:
            params["searchFields"] = ",".join(validated["searchFields"])
        if validated["docFileType"]:
            params["docFileType"] = validated["docFileType"]
        if validated["timeRange"]:
            params["timeRange"] = validated["timeRange"]
        if validated["fromCreateTime"]:
            params["fromCreateTime"] = validated["fromCreateTime"]
        if validated["toCreateTime"]:
            params["toCreateTime"] = validated["toCreateTime"]
        if validated["category"]:
            params["category"] = validated["category"]
        if validated["docStatus"]:
            params["docStatus"] = validated["docStatus"]
        if validated["modelName"]:
            params["modelName"] = validated["modelName"]
        if validated["sortType"] != "relevance":
            params["sortType"] = validated["sortType"]
        if validated["sortType"] == "time":
            params["sortOrder"] = validated["sortOrder"]

        response = self._request("sys/ftsearch/searchBuilder.do", params=params)
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能搜索 OA 内容")
        parsed = self._try_json(response["text"].lstrip("﻿\r\n\t "))
        if parsed is None:
            raise OAConnectorError("搜索出现错误：OA 返回非 JSON 响应")
        return self._parse_search_results(parsed, validated)

    def _parse_search_results(self, payload: Any, validated: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(payload, dict) and payload.get("EsError"):
            raise OAConnectorError(f"搜索出现错误：{str(payload.get('EsError'))[:120]}")
        query_page = payload.get("queryPage", payload) if isinstance(payload, dict) else {}
        rows = []
        if isinstance(query_page, dict):
            rows = query_page.get("list") or query_page.get("rows") or query_page.get("data") or []
        if not isinstance(rows, list):
            rows = []

        items: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record_ref = self._record_ref_from_search_row(row, validated["scope"])
            if not record_ref:
                continue
            fields = row.get("lksFieldsMap") if isinstance(row.get("lksFieldsMap"), dict) else {}
            title = self._clean_search_title(fields.get("subject") or row.get("subject") or row.get("docSubject") or row.get("title") or "")
            matched_exact = title == validated["query"] if validated["exactTitle"] else False
            if validated["onlyExactTitle"] and not matched_exact:
                continue
            model_name = str(record_ref.get("modelName") or row.get("modelName") or "")
            supports_detail, supports_attachments = self._model_capabilities(validated["scope"], model_name)
            summary = self._strip_html(str(row.get("content") or row.get("fdDescription") or ""))[:300]
            read_count = self._to_int(row.get("docReadCount") or row.get("readCount"))
            items.append(
                {
                    "recordRef": record_ref,
                    "fdId": record_ref["recordId"],
                    "title": title,
                    "summary": summary,
                    "creator": self._strip_html(str(row.get("creator") or "")),
                    "createTime": str(row.get("createTime") or ""),
                    "readCount": read_count,
                    "modelTitle": str(row.get("modelTitle") or ""),
                    "matchedExactTitle": matched_exact,
                    "supportsDetail": supports_detail,
                    "supportsAttachments": supports_attachments,
                }
            )
        total = self._to_int(query_page.get("totalrows") if isinstance(query_page, dict) else None)
        return {
            "query": validated["query"],
            "items": items,
            "page": validated["page"],
            "pageSize": validated["pageSize"],
            "total": total if total is not None else len(items),
            "totalNote": "以 OA 搜索接口返回为准",
        }

    def _record_ref_from_search_row(self, row: Dict[str, Any], scope: str) -> Optional[Dict[str, str]]:
        model_name = str(row.get("modelName") or "")
        path = str(row.get("linkStr") or "")
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("?")[0].split("/"):
            return None
        record_id = str(row.get("docKey") or "").strip()
        if not record_id:
            match = re.search(r"(?:fdId=|fdId/)([0-9a-fA-F]{24,40})", path)
            record_id = match.group(1) if match else ""
        if not record_id:
            return None
        return {"scope": scope, "modelName": model_name, "recordId": record_id, "path": path}

    def _model_capabilities(self, scope: str, model_name: str) -> tuple[bool, bool]:
        config = self._scope_config(scope)
        if config.get("detailParser") == "kms_multidoc_knowledge":
            allowed = config.get("allowedModelNames", [])
            if model_name in allowed or model_name.endswith("KmsMultidocKnowledge"):
                return True, True
        return False, False

    def _clean_search_title(self, value: Any) -> str:
        return self._strip_html(str(value or ""))

    def _to_int(self, value: Any) -> Optional[int]:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None
```

- [ ] **Step 4: Run search tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.SearchObjectsTest -v
```

Expected: PASS.

- [ ] **Step 5: Run all client tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_client.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py tests/test_client.py
git commit -m "feat: add OA object search"
```

Expected: commit succeeds if this directory is a git repository; otherwise record that no commit was made.

---

### Task 3: Implement Object Detail and Attachment Metadata Parsing

**Files:**
- Modify: `oa_agent_connector/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: Task 2 `recordRef`, `_model_capabilities`, existing `_request`, `_strip_html`, `_extract_title`.
- Produces:
  - `OAClient.get_object_detail(record_ref: Optional[Dict[str, Any]] = None, include_text: bool = True, text_limit: int = 12000, fields: Optional[List[str]] = None, fd_id: Optional[str] = None) -> Dict[str, Any]`
  - `OAClient._validate_record_ref(record_ref: Dict[str, Any]) -> Dict[str, str]`
  - `OAClient._extract_detail_text(html_text: str, text_limit: int) -> tuple[str, str]`
  - `OAClient._parse_knowledge_attachments(html_text: str) -> List[Dict[str, Any]]`

- [ ] **Step 1: Write failing detail and attachment tests**

Append to `tests/test_client.py`:

```python
class FakeDetailClient(OAClient):
    def __init__(self, html_text):
        super().__init__("https://oa.example.test/")
        self.html_text = html_text
        self.last_request = None

    def _request(self, path, method="GET", params=None, data=None):
        self.last_request = {"path": path, "method": method, "params": params or {}, "data": data}
        return {"url": "https://oa.example.test/detail", "text": self.html_text}


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
        self.assertEqual(len(detail["attachments"]), 2)
        self.assertEqual(detail["attachments"][0]["index"], 1)
        self.assertEqual(detail["attachments"][0]["name"], "附件一.pdf")
        self.assertEqual(detail["attachments"][0]["attachmentId"], "att-1")
        self.assertEqual(detail["attachments"][0]["fileId"], "file-1")
        self.assertEqual(detail["attachments"][0]["size"], 200261)
        self.assertNotIn("url", detail["attachments"][0])

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
```

- [ ] **Step 2: Run detail tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.ObjectDetailTest -v
```

Expected: FAIL with missing `get_object_detail` / `_validate_record_ref`.

- [ ] **Step 3: Implement recordRef validation and detail extraction**

Add inside `OAClient` after search helper methods:

```python
    def get_object_detail(
        self,
        record_ref: Optional[Dict[str, Any]] = None,
        include_text: bool = True,
        text_limit: int = SEARCH_LIMITS["detailTextLimitDefault"],
        fields: Optional[List[str]] = None,
        fd_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        del fields
        if record_ref is None and fd_id:
            record_ref = {
                "scope": "knowledge",
                "modelName": "KmsMultidocKnowledge",
                "recordId": fd_id,
                "path": f"/kms/multidoc/kms_multidoc_knowledge/kmsMultidocKnowledge.do?method=view&fdId={urllib.parse.quote(fd_id)}",
            }
        if record_ref is None:
            raise OAConnectorError("缺少 recordRef")
        ref = self._validate_record_ref(record_ref)
        if text_limit < 0 or text_limit > SEARCH_LIMITS["detailTextLimitMax"]:
            raise OAConnectorError("textLimit 超过允许范围")
        supports_detail, supports_attachments = self._model_capabilities(ref["scope"], ref["modelName"])
        if not supports_detail:
            raise OAConnectorError("该模块不支持详情解析")

        response = self._request(ref["path"])
        if self._looks_like_login_page(response["url"], response["text"]):
            raise OAConnectorError("当前会话未登录，不能查看 OA 内容")
        title = self._extract_title(response["text"])
        text = ""
        warning = ""
        if include_text:
            text, warning = self._extract_detail_text(response["text"], text_limit)
        attachments = self._parse_knowledge_attachments(response["text"]) if supports_attachments else []
        return {
            "recordRef": ref,
            "title": title,
            "text": text,
            "textExtractionWarning": warning,
            "attachments": attachments,
        }

    def _validate_record_ref(self, record_ref: Dict[str, Any]) -> Dict[str, str]:
        scope = str(record_ref.get("scope") or "")
        model_name = str(record_ref.get("modelName") or "")
        record_id = str(record_ref.get("recordId") or "")
        path = str(record_ref.get("path") or "")
        self._scope_config(scope)
        self._normalize_model_name(scope, model_name)
        if not record_id:
            raise OAConnectorError("recordRef 无效")
        if not path.startswith("/") or path.startswith("//"):
            raise OAConnectorError("recordRef 无效")
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc:
            raise OAConnectorError("recordRef 无效")
        if ".." in [part for part in parsed.path.split("/") if part]:
            raise OAConnectorError("recordRef 无效")
        if record_id not in path:
            raise OAConnectorError("recordRef 无效")
        return {"scope": scope, "modelName": model_name, "recordId": record_id, "path": path}

    def _extract_detail_text(self, html_text: str, text_limit: int) -> tuple[str, str]:
        cleaned = re.sub(r"<script\b.*?</script>", " ", html_text, flags=re.I | re.S)
        cleaned = re.sub(r"<style\b.*?</style>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<input\b[^>]*type=[\"']?hidden[\"']?[^>]*>", " ", cleaned, flags=re.I | re.S)
        warning = ""
        match = re.search(r"<div[^>]+(?:id|class)=[\"'][^\"']*(?:docContent|fdContent|content|mainContent)[^\"']*[\"'][^>]*>(.*?)</div>", cleaned, flags=re.I | re.S)
        if match:
            source = match.group(1)
        else:
            source = cleaned
            warning = "未识别到模块正文容器，已使用严格截断的页面文本"
        text = self._strip_html(source)
        return text[:text_limit], warning
```

- [ ] **Step 4: Implement `addDoc(...)` attachment parser**

Add inside `OAClient` after `_extract_detail_text(...)`:

```python
    def _parse_knowledge_attachments(self, html_text: str) -> List[Dict[str, Any]]:
        attachments: List[Dict[str, Any]] = []
        pattern = re.compile(r"attachmentObject_attachment\.addDoc\((.*?)\)", re.I | re.S)
        for match in pattern.finditer(html_text):
            args = self._parse_js_string_args(match.group(1))
            if len(args) < 3:
                continue
            attachment_id = args[0]
            file_id = args[1] if len(args) > 1 else ""
            name = args[2] if len(args) > 2 else ""
            mime_type = args[3] if len(args) > 3 else ""
            size = self._to_int(args[4]) if len(args) > 4 else None
            attachments.append(
                {
                    "index": len(attachments) + 1,
                    "name": name,
                    "attachmentId": attachment_id,
                    "fileId": file_id,
                    "mimeType": mime_type,
                    "size": size,
                    "downloadable": bool(attachment_id or file_id),
                }
            )
        return attachments

    def _parse_js_string_args(self, text: str) -> List[str]:
        values: List[str] = []
        current: List[str] = []
        quote: Optional[str] = None
        escaped = False
        for ch in text:
            if quote:
                if escaped:
                    current.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    raw = "".join(current)
                    values.append(bytes(raw, "utf-8").decode("unicode_escape"))
                    current = []
                    quote = None
                else:
                    current.append(ch)
            elif ch in ("'", '"'):
                quote = ch
        return values
```

- [ ] **Step 5: Run detail tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.ObjectDetailTest -v
```

Expected: PASS.

- [ ] **Step 6: Run all client tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_client.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py tests/test_client.py
git commit -m "feat: add OA object detail parsing"
```

Expected: commit succeeds if this directory is a git repository; otherwise record that no commit was made.

---

### Task 4: Implement Safe Attachment Download

**Files:**
- Modify: `oa_agent_connector/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: Task 3 `get_object_detail`, `_parse_knowledge_attachments`, `_validate_record_ref`.
- Produces:
  - `OAClient.download_attachment(record_ref: Optional[Dict[str, Any]], attachment_index: int, output_dir: str, overwrite: bool = False, max_bytes: int = 52428800, fd_id: Optional[str] = None) -> Dict[str, Any]`
  - `OAClient._safe_filename(name: str) -> str`
  - `OAClient._unique_output_path(output_dir: Path, filename: str, overwrite: bool) -> Path`
  - `OAClient._attachment_download_path(record_ref: Dict[str, str], attachment: Dict[str, Any]) -> str`

- [ ] **Step 1: Write failing filename and download tests**

Append to `tests/test_client.py`:

```python
class FakeDownloadClient(OAClient):
    def __init__(self, detail_html, download_text):
        super().__init__("https://oa.example.test/")
        self.detail_html = detail_html
        self.download_text = download_text
        self.requests = []

    def _request(self, path, method="GET", params=None, data=None):
        self.requests.append({"path": path, "method": method, "params": params or {}, "data": data})
        if "sys_att_main" in path:
            return {"url": "https://oa.example.test/sys/attachment/sys_att_main/sysAttMain.do", "text": self.download_text}
        return {"url": "https://oa.example.test/detail", "text": self.detail_html}


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
            self.assertEqual(first["bytes"], 3)

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
            html_client = FakeDownloadClient(detail_html, "<html>login</html>")
            with self.assertRaises(OAConnectorError):
                html_client.download_attachment(ref, 1, tmpdir, max_bytes=1000)

            large_client = FakeDownloadClient(detail_html, "x" * 11)
            with self.assertRaises(OAConnectorError):
                large_client.download_attachment(ref, 1, tmpdir, max_bytes=10)
```

- [ ] **Step 2: Run download tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.AttachmentDownloadTest -v
```

Expected: FAIL with missing `_safe_filename` / `download_attachment`.

- [ ] **Step 3: Implement safe filename and unique output path**

Add `import tempfile` near imports in `client.py`.

Add inside `OAClient`:

```python
    def _safe_filename(self, name: str) -> str:
        name = str(name or "").replace("\\", "/")
        name = Path(name).name
        name = re.sub(r"[\x00-\x1f\x7f/:*?\"<>|]+", "_", name)
        name = name.replace("..", "_").strip(" .")
        if not name:
            name = "attachment"
        if len(name) > 180:
            stem = Path(name).stem[:120]
            suffix = Path(name).suffix[:20]
            name = f"{stem}{suffix}" if suffix else stem
        return name

    def _unique_output_path(self, output_dir: Path, filename: str, overwrite: bool) -> Path:
        output_dir = output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        candidate = (output_dir / filename).resolve()
        if output_dir not in candidate.parents and candidate != output_dir:
            raise OAConnectorError("保存附件失败：文件路径不安全")
        if overwrite or not candidate.exists():
            return candidate
        stem = candidate.stem
        suffix = candidate.suffix
        counter = 1
        while True:
            next_candidate = (output_dir / f"{stem} ({counter}){suffix}").resolve()
            if not next_candidate.exists():
                return next_candidate
            counter += 1
```

- [ ] **Step 4: Implement download attachment method**

Add inside `OAClient`:

```python
    def download_attachment(
        self,
        record_ref: Optional[Dict[str, Any]],
        attachment_index: int,
        output_dir: str,
        overwrite: bool = False,
        max_bytes: int = SEARCH_LIMITS["downloadMaxBytesDefault"],
        fd_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if max_bytes < 1 or max_bytes > SEARCH_LIMITS["downloadMaxBytesDefault"]:
            raise OAConnectorError("附件超过下载大小上限")
        detail = self.get_object_detail(record_ref=record_ref, include_text=False, fd_id=fd_id)
        attachments = detail.get("attachments") or []
        selected = None
        for attachment in attachments:
            if int(attachment.get("index") or 0) == int(attachment_index):
                selected = attachment
                break
        if selected is None:
            raise OAConnectorError("附件序号不存在")
        if selected.get("size") is not None and int(selected["size"]) > max_bytes:
            raise OAConnectorError("附件超过下载大小上限")

        filename = self._safe_filename(str(selected.get("name") or "attachment"))
        output_path = self._unique_output_path(Path(output_dir), filename, overwrite)
        download_path = self._attachment_download_path(detail["recordRef"], selected)
        response = self._request(download_path)
        text = response["text"]
        if self._looks_like_login_page(response["url"], text) or "<html" in text[:200].lower():
            raise OAConnectorError("下载附件失败，当前会话可能失效或无权限")
        raw = text.encode("utf-8")
        if len(raw) > max_bytes:
            raise OAConnectorError("附件超过下载大小上限")

        temp_path = output_path.with_name(f".{output_path.name}.tmp")
        try:
            temp_path.write_bytes(raw)
            temp_path.replace(output_path)
        except OSError as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise OAConnectorError(f"保存附件失败: {str(exc)[:120]}") from exc

        return {
            "ok": True,
            "recordRef": detail["recordRef"],
            "attachment": {
                "index": selected.get("index"),
                "name": selected.get("name"),
                "attachmentId": selected.get("attachmentId"),
                "mimeType": selected.get("mimeType"),
                "size": selected.get("size"),
            },
            "savedPath": str(output_path),
            "bytes": len(raw),
        }

    def _attachment_download_path(self, record_ref: Dict[str, str], attachment: Dict[str, Any]) -> str:
        attachment_id = urllib.parse.quote(str(attachment.get("attachmentId") or ""))
        file_id = urllib.parse.quote(str(attachment.get("fileId") or ""))
        if not attachment_id and not file_id:
            raise OAConnectorError("附件序号不存在")
        query = urllib.parse.urlencode({"method": "download", "fdId": attachment_id, "fileId": file_id, "modelId": record_ref["recordId"]})
        return f"/sys/attachment/sys_att_main/sysAttMain.do?{query}"
```

- [ ] **Step 5: Run download tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.AttachmentDownloadTest -v
```

Expected: PASS.

- [ ] **Step 6: Run all client tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_client.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py tests/test_client.py
git commit -m "feat: add safe OA attachment download"
```

Expected: commit succeeds if this directory is a git repository; otherwise record that no commit was made.

---

### Task 5: Implement Batch Search Objects

**Files:**
- Modify: `oa_agent_connector/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: Task 2 `search_objects`, Task 3 `get_object_detail`, Task 4 `download_attachment`.
- Produces:
  - `OAClient.batch_search_objects(queries: List[str], **kwargs: Any) -> Dict[str, Any]`

- [ ] **Step 1: Write failing batch tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run batch tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.BatchSearchObjectsTest -v
```

Expected: FAIL with missing `batch_search_objects`.

- [ ] **Step 3: Implement `batch_search_objects`**

Add inside `OAClient`:

```python
    def batch_search_objects(self, queries: List[str], **kwargs: Any) -> Dict[str, Any]:
        if not isinstance(queries, list) or not queries:
            raise OAConnectorError("queries 不能为空")
        if len(queries) > SEARCH_LIMITS["batchQueriesMax"]:
            raise OAConnectorError("queries 数量超过允许范围")
        include_details = bool(kwargs.get("includeDetails", False))
        include_attachments = bool(kwargs.get("includeAttachments", False))
        download_first = bool(kwargs.get("downloadFirstAttachment", False))
        max_details = int(kwargs.get("maxDetailsPerQuery") or SEARCH_LIMITS["maxDetailsPerQueryDefault"])
        if max_details < 1 or max_details > SEARCH_LIMITS["maxDetailsPerQueryMax"]:
            raise OAConnectorError("maxDetailsPerQuery 超过允许范围")
        max_downloads = int(kwargs.get("maxDownloads") or SEARCH_LIMITS["batchMaxDownloadsDefault"])
        if max_downloads < 0 or max_downloads > SEARCH_LIMITS["batchMaxDownloadsMax"]:
            raise OAConnectorError("maxDownloads 超过允许范围")

        items: List[Dict[str, Any]] = []
        matched_queries = 0
        errors = 0
        downloads = 0
        for query in queries:
            item = {"query": query, "matched": False, "resultCount": 0, "results": [], "error": None}
            try:
                search_kwargs = dict(kwargs)
                search_kwargs.pop("includeDetails", None)
                search_kwargs.pop("includeAttachments", None)
                search_kwargs.pop("downloadFirstAttachment", None)
                search_kwargs.pop("maxDetailsPerQuery", None)
                search_kwargs.pop("maxDownloads", None)
                search_kwargs.pop("outputDir", None)
                search_kwargs["query"] = query
                search_kwargs["pageSize"] = int(kwargs.get("pageSize") or SEARCH_LIMITS["batchPageSizeDefault"])
                result = self.search_objects(**search_kwargs)
                results = result.get("items", [])
                item["matched"] = bool(results)
                item["resultCount"] = len(results)
                if results:
                    matched_queries += 1
                for result_item in results[:max_details if (include_details or include_attachments or download_first) else len(results)]:
                    compact = {
                        "recordRef": result_item.get("recordRef"),
                        "title": result_item.get("title"),
                        "matchedExactTitle": result_item.get("matchedExactTitle", False),
                        "attachments": [],
                        "downloaded": [],
                    }
                    if include_details or include_attachments or download_first:
                        detail = self.get_object_detail(record_ref=result_item["recordRef"], include_text=include_details)
                        if include_details:
                            compact["text"] = detail.get("text", "")
                            compact["textExtractionWarning"] = detail.get("textExtractionWarning", "")
                        if include_attachments or download_first:
                            compact["attachments"] = detail.get("attachments", [])
                    if download_first and compact["attachments"]:
                        if not kwargs.get("exactTitle") or not kwargs.get("onlyExactTitle") or not compact.get("matchedExactTitle"):
                            pass
                        elif max_downloads <= 0 or downloads >= max_downloads:
                            pass
                        else:
                            downloaded = self.download_attachment(
                                result_item["recordRef"],
                                attachment_index=int(compact["attachments"][0]["index"]),
                                output_dir=str(kwargs.get("outputDir") or "~/Downloads/oa-attachments"),
                                overwrite=bool(kwargs.get("overwrite", False)),
                                max_bytes=int(kwargs.get("maxBytes") or SEARCH_LIMITS["downloadMaxBytesDefault"]),
                            )
                            compact["downloaded"].append(downloaded)
                            downloads += 1
                    item["results"].append(compact)
            except Exception as exc:
                errors += 1
                item["error"] = self._sanitize_error(exc)
            items.append(item)
        return {
            "items": items,
            "summary": {
                "totalQueries": len(queries),
                "matchedQueries": matched_queries,
                "errors": errors,
                "downloads": downloads,
            },
        }

    def _sanitize_error(self, exc: Exception) -> str:
        text = str(exc)
        text = re.sub(r"(?i)(cookie|set-cookie|jsessionid|authorization|password|j_password)\s*[:=][^\s,;]+", r"\1=[redacted]", text)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:200]
```

- [ ] **Step 4: Run batch tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_client.BatchSearchObjectsTest -v
```

Expected: PASS.

- [ ] **Step 5: Run all client tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_client.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py tests/test_client.py
git commit -m "feat: add OA batch object search"
```

Expected: commit succeeds if this directory is a git repository; otherwise record that no commit was made.

---

### Task 6: Expose New MCP Tools

**Files:**
- Modify: `oa_agent_connector/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 1-5 `OAClient` methods.
- Produces MCP tools:
  - `oa_get_search_schema`
  - `oa_search_objects`
  - `oa_get_object_detail`
  - `oa_download_attachment`
  - `oa_batch_search_objects`

- [ ] **Step 1: Write failing MCP tool schema tests**

Append to `tests/test_mcp_server.py`:

```python
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
```

- [ ] **Step 2: Run MCP search tests and verify they fail**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_mcp_server.SearchMCPServerTest -v
```

Expected: FAIL because tools are missing.

- [ ] **Step 3: Add five tool schemas**

In `oa_agent_connector/mcp_server.py`, append these entries to `TOOLS` before `oa_prepare_approval` or after `oa_get_detail`:

```python
    _tool_schema(
        "oa_get_search_schema",
        "返回当前 MCP 支持的 OA 搜索范围、字段枚举、排序、文件类型和限制规则。",
        {
            "scope": {"type": "string", "description": "搜索范围，默认 all，可选 knowledge"},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_search_objects",
        "执行 OA 通用只读搜索，返回结构化结果和受控 recordRef。",
        {
            "query": {"type": "string"},
            "scope": {"type": "string"},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "searchFields": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string"},
            "docStatus": {"type": "string"},
            "docFileType": {"type": "string"},
            "outKeyword": {"type": "string"},
            "timeRange": {"type": "string"},
            "fromCreateTime": {"type": "string"},
            "toCreateTime": {"type": "string"},
            "sortType": {"type": "string"},
            "sortOrder": {"type": "string"},
            "exactTitle": {"type": "boolean"},
            "onlyExactTitle": {"type": "boolean"},
            "page": {"type": "integer", "minimum": 1},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 50},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["query"],
    ),
    _tool_schema(
        "oa_get_object_detail",
        "按搜索结果返回的 recordRef 读取 OA 对象详情和附件元数据。",
        {
            "recordRef": {"type": "object"},
            "fdId": {"type": "string", "description": "兼容便捷参数，仅默认知识文档解析器使用"},
            "includeText": {"type": "boolean"},
            "textLimit": {"type": "integer", "minimum": 0, "maximum": 20000},
            "fields": {"type": "array", "items": {"type": "string"}},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
    ),
    _tool_schema(
        "oa_download_attachment",
        "按 recordRef + attachmentIndex 下载当前详情页中可见附件到本地安全目录。",
        {
            "recordRef": {"type": "object"},
            "fdId": {"type": "string", "description": "兼容便捷参数，仅默认知识文档解析器使用"},
            "attachmentIndex": {"type": "integer", "minimum": 1},
            "outputDir": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "maxBytes": {"type": "integer", "minimum": 1, "maximum": 52428800},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["attachmentIndex", "outputDir"],
    ),
    _tool_schema(
        "oa_batch_search_objects",
        "批量执行通用 OA 搜索，输入为 queries 数组，可选列附件或受限下载。",
        {
            "queries": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "scope": {"type": "string"},
            "modelName": {"type": "string"},
            "bond": {"type": "string", "enum": ["or", "and", "like"]},
            "searchFields": {"type": "array", "items": {"type": "string"}},
            "sortType": {"type": "string"},
            "sortOrder": {"type": "string"},
            "docFileType": {"type": "string"},
            "exactTitle": {"type": "boolean"},
            "onlyExactTitle": {"type": "boolean"},
            "pageSize": {"type": "integer", "minimum": 1, "maximum": 20},
            "includeDetails": {"type": "boolean"},
            "includeAttachments": {"type": "boolean"},
            "maxDetailsPerQuery": {"type": "integer", "minimum": 1, "maximum": 3},
            "downloadFirstAttachment": {"type": "boolean"},
            "maxDownloads": {"type": "integer", "minimum": 0, "maximum": 50},
            "outputDir": {"type": "string"},
            "overwrite": {"type": "boolean"},
            "maxBytes": {"type": "integer", "minimum": 1, "maximum": 52428800},
            "session": {"type": "string", "description": "本地会话名，默认 default"},
        },
        ["queries"],
    ),
```

- [ ] **Step 4: Add `call_tool` branches**

In `call_tool(...)`, after client creation and before existing `oa_auth_status` branch, add:

```python
    if name == "oa_get_search_schema":
        return _ok(client.get_search_schema(str(args.get("scope") or "all")))
    if name == "oa_search_objects":
        return _ok(
            client.search_objects(
                query=str(args["query"]),
                scope=args.get("scope") or "all",
                modelName=args.get("modelName"),
                bond=args.get("bond") or "or",
                searchFields=args.get("searchFields") or [],
                category=args.get("category") or "",
                docStatus=args.get("docStatus") or "",
                docFileType=args.get("docFileType") or "",
                outKeyword=args.get("outKeyword") or "",
                timeRange=args.get("timeRange") or "",
                fromCreateTime=args.get("fromCreateTime") or "",
                toCreateTime=args.get("toCreateTime") or "",
                sortType=args.get("sortType") or "relevance",
                sortOrder=args.get("sortOrder") or "desc",
                exactTitle=_bool(args, "exactTitle"),
                onlyExactTitle=_bool(args, "onlyExactTitle"),
                page=int(args.get("page") or 1),
                pageSize=int(args.get("pageSize") or 20),
            )
        )
    if name == "oa_get_object_detail":
        return _ok(
            client.get_object_detail(
                record_ref=args.get("recordRef"),
                include_text=_bool(args, "includeText", True),
                text_limit=int(args.get("textLimit") or 12000),
                fields=args.get("fields") or [],
                fd_id=args.get("fdId"),
            )
        )
    if name == "oa_download_attachment":
        return _ok(
            client.download_attachment(
                record_ref=args.get("recordRef"),
                attachment_index=int(args["attachmentIndex"]),
                output_dir=str(args["outputDir"]),
                overwrite=_bool(args, "overwrite"),
                max_bytes=int(args.get("maxBytes") or 52428800),
                fd_id=args.get("fdId"),
            )
        )
    if name == "oa_batch_search_objects":
        batch_args = dict(args)
        queries = list(batch_args.pop("queries"))
        batch_args.pop("session", None)
        return _ok(client.batch_search_objects(queries=queries, **batch_args))
```

- [ ] **Step 5: Run MCP tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests.test_mcp_server.SearchMCPServerTest -v
```

Expected: PASS.

- [ ] **Step 6: Run all MCP tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest tests/test_mcp_server.py -v
```

Expected: PASS.

- [ ] **Step 7: Run full unit suite**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 8: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: expose OA search MCP tools"
```

Expected: commit succeeds if this directory is a git repository; otherwise record that no commit was made.

---

### Task 7: Security Regression, Publish Copy Sync, and Verification

**Files:**
- Modify if publishing: `github_publish/oa_agent_connector/client.py`
- Modify if publishing: `github_publish/oa_agent_connector/mcp_server.py`
- Modify if publishing: `github_publish/tests/test_client.py`
- Modify if publishing: `github_publish/tests/test_mcp_server.py`
- Test: `tests/test_client.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: all previous tasks.
- Produces: verified implementation and optional synchronized publish copy.

- [ ] **Step 1: Add sensitive output regression tests**

Append to `tests/test_mcp_server.py`:

```python
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
        for forbidden in ["abc", "def", "ghi", "Bearer token", "<html>"]:
            self.assertNotIn(forbidden, text)

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
```

- [ ] **Step 2: If the sensitive output test fails, sanitize MCP tool errors**

If `test_new_tool_error_output_does_not_leak_sensitive_patterns` fails, modify `_tool_error` in `oa_agent_connector/mcp_server.py`:

```python
def _redact_tool_message(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"(?i)(cookie|set-cookie|jsessionid|authorization|password|j_password)\s*[:=][^\s,;]+", r"\1=[redacted]", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:200]


def _tool_error(message: str) -> Dict[str, Any]:
    return _mcp_error(_setup_guide(_redact_tool_message(message)))
```

- [ ] **Step 3: Run the full unit suite**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 4: Run a no-network MCP schema smoke test**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa && python - <<'PY'
from oa_agent_connector import mcp_server
resp = mcp_server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
names = {tool['name'] for tool in resp['result']['tools']}
expected = {'oa_get_search_schema', 'oa_search_objects', 'oa_get_object_detail', 'oa_download_attachment', 'oa_batch_search_objects'}
missing = expected - names
if missing:
    raise SystemExit(f'missing tools: {sorted(missing)}')
print('ok', sorted(expected))
PY
```

Expected: prints `ok [...]` and exits 0.

- [ ] **Step 5: If publish copy is required, synchronize files**

Run these copy commands only after the main files and tests pass:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa
cp oa_agent_connector/client.py github_publish/oa_agent_connector/client.py
cp oa_agent_connector/mcp_server.py github_publish/oa_agent_connector/mcp_server.py
cp tests/test_client.py github_publish/tests/test_client.py
cp tests/test_mcp_server.py github_publish/tests/test_mcp_server.py
```

Expected: files are copied with no output.

- [ ] **Step 6: If publish copy was synchronized, run publish-copy tests**

Run:

```bash
cd /Users/kyle/Documents/saselomo/ai-space/oa/github_publish && python -m unittest discover -s tests -v
```

Expected: PASS.

- [ ] **Step 7: Optional online read-only verification with explicit authorization**

Only run this step after the user confirms which OA session and low-sensitivity test record/attachment may be used. Do not run it automatically.

Verification flow:

```text
1. 调用 oa_get_search_schema(scope="knowledge")，确认返回 schema。
2. 调用 oa_search_objects(query="用户授权的关键词", scope="knowledge", modelName="KmsMultidocKnowledge", pageSize=5)，确认返回非登录页和结构化 items。
3. 选一个搜索结果 recordRef 调用 oa_get_object_detail(includeText=false)，确认 title 和 attachments 元数据。
4. 如用户明确授权下载，调用 oa_download_attachment(recordRef=..., attachmentIndex=1, outputDir="临时目录")，确认 savedPath 位于临时目录且文件大小合理。
5. 不执行 OA 编辑、删除、审批、保存表单或数据库写入。
```

Expected: 搜索、详情、列附件、可选下载均成功；验证摘要不包含 cookie、正文敏感内容或下载 URL。

- [ ] **Step 8: Commit if inside a git repository**

Run:

```bash
git status --short
git add oa_agent_connector/client.py oa_agent_connector/mcp_server.py tests/test_client.py tests/test_mcp_server.py github_publish/oa_agent_connector/client.py github_publish/oa_agent_connector/mcp_server.py github_publish/tests/test_client.py github_publish/tests/test_mcp_server.py
git commit -m "test: verify OA search security boundaries"
```

Expected: commit succeeds if this directory is a git repository. If publish copy was not synchronized, omit the `github_publish/...` paths. If `fatal: not a git repository` appears, record that no commit was made.

---

## Self-Review

**Spec coverage:** This plan maps every included capability from `docs/superpowers/specs/2026-07-06-oa-mcp-search-attachments-design.md` to tasks: schema and whitelist validation in Task 1, search and exact title matching in Task 2, detail and `addDoc(...)` attachment metadata in Task 3, safe attachment download in Task 4, bounded batch behavior in Task 5, MCP exposure in Task 6, security regression and optional online verification in Task 7. The out-of-scope requirements are preserved in Global Constraints and negative tests.

**Placeholder scan:** The plan contains no prohibited placeholder markers. Each code-changing step includes concrete code snippets and exact commands with expected outcomes.

**Type consistency:** Public methods produced by earlier tasks are consumed with matching names and parameter shapes in later tasks: `search_objects(**kwargs)`, `get_object_detail(record_ref=..., include_text=..., text_limit=..., fd_id=...)`, `download_attachment(record_ref, attachment_index, output_dir, overwrite=False, max_bytes=..., fd_id=None)`, and `batch_search_objects(queries, **kwargs)`. MCP argument names match the existing camelCase style where applicable: `recordRef`, `attachmentIndex`, `outputDir`, `pageSize`, `textLimit`, `maxBytes`.
