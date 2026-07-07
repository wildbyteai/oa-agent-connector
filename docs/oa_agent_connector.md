# OA Agent Connector

这个连接器不改 OA 服务端源码，只使用源码里已经存在的接口：

- 登录：`POST /j_acegi_security_check`
- 待我审列表：`GET /km/review/km_review_index/kmReviewIndex.do?method=list&j_path=/listApproval&mydoc=approval`
- 审批处理：`POST /api/km-review/kmReviewRestService/approveProcess`
- 详情查看：`GET /km/review/km_review_main/kmReviewMain.do?method=view&fdId=...`
- 通用搜索：`GET /sys/ftsearch/searchBuilder.do?method=search&resultType=json`
- 文档详情：基于搜索结果返回的受控 `recordRef.path`
- 附件下载：只基于详情页解析出的附件序号，不接受任意下载 URL

## 权限控制

审批执行前有强制 gate：

1. 必须先登录，连接器使用登录后 cookie。
2. 每次审批前重新查询当前登录账号的 `type=unExecuted` 待审列表。
3. 只有 `fdId` 在当前待审列表中才允许提交审批接口。
4. 连接器不接受也不传入任意 `handler`，避免绕过当前登录账号权限。
5. `approve` / `reject` 默认 dry-run，只有显式 `--execute` 才会真正提交。

## 用法

```bash
export OA_BASE_URL="https://example.com/oa/"
export OA_AGENT_STATE_DIR="$HOME/.oa-agent-connector"
python3 -m oa_agent_connector.cli login --username "zhangsan"
python3 -m oa_agent_connector.cli todos
python3 -m oa_agent_connector.cli detail "fd_id_here"
python3 -m oa_agent_connector.cli approve "fd_id_here" --note "同意"
python3 -m oa_agent_connector.cli approve "fd_id_here" --note "同意" --execute
python3 -m oa_agent_connector.cli reject "fd_id_here" --note "不同意，原因..."
```

登录 cookie 默认保存在当前目录 `.oa-session.cookies`，文件权限会尽量设置为 `0600`。也可以用 `OA_COOKIE_FILE` 或 `--cookie-file` 指定。连接器不会因为一次请求失败或鉴权提示就删除 cookie；失效时先提示重新登录，只有用户明确清理会话或实现能确认服务端会话必须重置时才删除。

MCP 模式建议始终配置 `OA_AGENT_STATE_DIR` 为目标电脑上的真实绝对路径：

- macOS/Linux：当前用户主目录下的 `.oa-agent-connector`
- Windows：当前用户目录下的 `.oa-agent-connector`

这个目录用于保存 MCP 登录 cookie、OA 地址和审批确认状态，确保不同 MCP 调用能读写同一份状态。

可用下面的命令在目标电脑上生成 MCP 配置：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

MCP 普通授权默认使用 `oa_begin_auth` 本机授权页。`oa_login` 默认不暴露给普通 Agent；只有管理员显式设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才开放。`oa_begin_auth` 默认优先要求 OA 地址为 HTTPS；如果企业内网 OA 只能 HTTP，MCP 会先返回安全确认提示、一次性确认令牌和 `nextAction`，用户确认后 Agent 再调用 `oa_begin_auth(insecure=true)` 继续授权，不需要用户手动改配置或重启。HTTPS 跳过证书校验不走普通用户确认流程，仍需管理员显式设置 `OA_AGENT_ALLOW_INSECURE_AUTH=1`。该环境变量仅作为管理员预先批准可信内网 HTTP 或 HTTPS 跳过证书校验的全局例外。

## Agent 工具封装建议

对外暴露工具时建议只暴露以下动作：

- `begin_auth(base_url)`：默认授权入口，返回本机授权链接
- `local_auth_status(auth_token)`
- `login(base_url, username, password)`：兼容或调试入口，默认不暴露；不建议普通用户在聊天里输入密码
- `list_todos(page, page_size)`
- `get_detail(fd_id)`
- `search_objects(query, scope, page, page_size)`
- `get_object_detail(record_ref)`
- `download_attachment(record_ref, attachment_index, output_dir)`
- `batch_search_objects(queries, scope)`
- `approve(fd_id, note, execute=false)`
- `reject(fd_id, note, execute=false)`

不要暴露原始 `flowParam`、`handler`、任意 URL 请求能力。搜索详情必须通过 MCP 返回的 `recordRef` 继续访问；附件下载必须通过详情页中的附件序号，不能接受用户手写下载 URL。需要扩展会签、转办、人工决策节点时，也应增加白名单参数，并继续保留“当前登录账号待审列表包含 fdId”的 gate。

## 搜索与附件边界

- `oa_search_objects` 只做 OA 当前账号权限内的只读搜索；本地支持 `matchMode=keyword/contains/exact`，其中 `contains/exact` 会忽略标题空白。
- `searchFields=["title"]` 只作为 MCP 本地标题过滤，不下发 OA 的标题字段，避免 OA 搜索接口返回通用错误。
- `oa_search_objects` 默认 `requireDetail=true`，只返回可继续通过 `oa_get_object_detail` 查看详情的结果。
- `oa_search_objects` 默认 `dedupByDocument=true`，按 `fdId` 聚合同一文档下的附件级命中，并返回 `normalizedTitle`、`type`、`attachmentCount`、`attachmentTitles`、`detailUrl`。
- `detailUrl` 由当前 `baseUrl` 和受控站内 `recordRef.path` 拼接生成，仅用于浏览器打开 OA 原生详情页；下载附件仍必须通过详情页附件序号。
- `oa_get_object_detail` 只接受搜索结果返回的 `recordRef`，并校验路径必须是站内相对路径。
- `oa_download_attachment` 会重新读取详情页附件列表，只下载其中可见附件。
- 附件保存时会清理文件名里的路径穿越字符，并默认避免覆盖已有文件。
- 如果 Agent 在 MCP 结果之外又做了额外业务过滤、改排序或隐藏结果，应明确告诉用户这是 Agent 二次处理，不是 OA 原始结果。

## MCP Server

本项目已提供 stdio MCP server：

```bash
oa-agent-mcp
```

面向实施和调试的完整安装、授权、查询流程见 [mcp_install_flow.md](mcp_install_flow.md)。

面向同事发布的标准使用流程见 [oa_mcp_colleague_guide.md](oa_mcp_colleague_guide.md)。

面向普通用户的标准回复话术见 [oa_mcp_user_output_standard.md](oa_mcp_user_output_standard.md)。
