# OA Agent Connector

这个连接器不改 OA 服务端源码，只使用源码里已经存在的接口：

- 登录：`POST /j_acegi_security_check`
- 待我审列表：`GET /km/review/km_review_index/kmReviewIndex.do?method=list&j_path=/listApproval&mydoc=approval`
- 审批处理：读取现有详情页中的原生审批表单，再通过 `POST /km/review/km_review_main/kmReviewMain.do?method=publishDraft` 提交
- 详情查看：`GET /km/review/km_review_main/kmReviewMain.do?method=view&fdId=...`
- 通用搜索：`GET /sys/ftsearch/searchBuilder.do?method=search&resultType=json`
- 文档详情：基于搜索结果返回的受控 `recordRef.path`
- 附件下载：只基于详情页解析出的附件序号，不接受任意下载 URL

## 当前版本范围

`0.2.13` 将审批能力固定为 `standard-approval-v1`：支持审批意见、同意、驳回，以及相同范围内的批量审批；不填写或修改业务表单，不选择下一流向，也不执行转办、沟通、废弃、加签、补签等特殊动作。

MCP 返回统一的 `approvalHandling`：

- 第 1 级 `standard_approval`：可在对话中审批，但确认摘要必须说明本次不会填写表单或选择流向。
- 第 2 级 `detail_check_required`：需要先查看详情，不能仅凭待办标题准备审批。
- 第 3 级 `native_oa_required`：必须打开 OA 原生页面处理。无法判断时按此级别处理。

## 权限控制

审批执行前有强制 gate：

1. 必须先登录，连接器使用登录后 cookie。
2. 每次审批前重新查询当前登录账号的 `type=unExecuted` 待审列表。
3. 只有 `fdId` 在当前待审列表中才允许提交审批接口。
4. 从 `method=view` 详情页读取当前 workitem，只接受 OA 原生标记为 `taskFrom=workitem` 且操作身份为 `handler` 的任务，并确认用户要求的同意或驳回动作确实可用。
5. 不使用 `method=edit` 判断审批权限；流程节点可以允许审批但禁止修改申请正文。当前版本不会替用户修改申请正文或其他业务表单字段。
6. 连接器不接受也不传入任意 `handler`，避免绕过当前登录账号权限。
7. `approve` / `reject` 默认 dry-run，只有显式 `--execute` 才会真正提交。

有多个当前 workitem 时，连接器遵循页面中的 `lbpm.defaultTaskId`，没有指定时与 OA 页面一致取第一个。驳回节点读取 OA 的 `isRefuseToPrevNodeDefault` 设置：开启时取可驳回列表最后一项，否则取第一项。

MCP 正式审批还增加两层保护：准备确认摘要时就验证所选动作存在于当前 workitem；确认令牌会在网络请求前被单个进程原子占用，并绑定准备审批时的 OA 登录账号以及 `processId/taskId/nodeId/activityType/operationType`。自动登录恢复后会再次核对账号绑定；确认时任一流程绑定值变化都要求重新准备。即使状态文件删除失败，占用标记也会保留，避免旧 token 再次可用。审批只使用页面表单这一条提交路径，不会在请求超时后自动改用另一条路径再次提交。如果提交响应不明确，只查询一次当前待办获取线索；无论单据仍在还是已经离开待办，都返回“结果不明确，请勿重复提交”，不能仅凭待办消失宣称成功。

批量审批使用 `oa_prepare_batch_approval` 和 `oa_confirm_batch_approval`，单次最多 20 条，可混合同意和驳回。批次中的每条都必须先确认属于标准审批。MCP 的单条和批量正式审批都不接受手工 `futureNodeId`；需要填写表单、选择下一节点或执行特殊动作时由用户在 OA 原生页面处理。准备阶段一次读取当前账号待办，逐条验证动作和完整 workitem 绑定，全部通过后才生成批量 token。确认阶段严格串行，每条执行前再次校验账号和待办权限；首条失败或结果不明确时停止，不执行后续项目，也不自动重试整批。批量审批不是事务，前面已经完成的项目不会回滚。

防重复控制同时覆盖 token 和 workitem：token 在网络请求前原子 claim；规范化后的 `baseUrl + fdId + processId + taskId` 还会生成跨 token 的原子锁，因此两个批次或单条/批量重叠也不能同时提交同一任务。成功、结果不明确或进程中断留下的不确定锁都不会被另一个进程自动接管；必须先人工核对 OA 状态，避免待办延迟或锁接管竞态造成重复提交。每完成一条都会把进度写入 `.processing`；批量终态也保留在该文件中，相同 token 后续只能读取原结果，不能重发，过期终态会在后续 MCP 调用时清理。若 OA 已明确成功但本地进度落盘失败，连接器立即停止后续项目并返回不可重试的状态告警。

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

这个目录用于保存 MCP 登录 Cookie、OA 地址和审批确认状态，确保不同 MCP 调用能读写同一份状态。自动登录密码不写入这个目录。

可用下面的命令在目标电脑上生成 MCP 配置：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

MCP 普通授权默认使用 `oa_begin_auth` 本机授权页。`oa_login` 默认不暴露给普通 Agent；只有管理员显式设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才开放。`oa_begin_auth` 默认优先要求 OA 地址为 HTTPS；如果企业内网 OA 只能 HTTP，MCP 会先返回安全确认提示、一次性确认令牌和 `nextAction`，用户确认后 Agent 再调用 `oa_begin_auth(insecure=true)` 继续授权，不需要用户手动改配置或重启。HTTPS 跳过证书校验不走普通用户确认流程，仍需管理员显式设置 `OA_AGENT_ALLOW_INSECURE_AUTH=1`。该环境变量仅作为管理员预先批准可信内网 HTTP 或 HTTPS 跳过证书校验的全局例外。

用户在本机授权页勾选“在这台电脑上安全记住”后，密码通过 `keyring` 保存到 macOS 钥匙串或 Windows 凭据管理器。Keyring 服务标识同时绑定 `OA_AGENT_STATE_DIR` 和 session，避免同一电脑上的多套连接器互相覆盖。MCP 检测到明确的登录失效时，会自动登录并把只读操作重试一次；审批确认只在执行前的待办权限复核阶段自动恢复，不会自动重放审批提交。失败后默认冷却 15 分钟，连续失败 3 次后停止自动登录并要求重新授权。`oa_disable_auto_login` 可删除系统凭据并保留当前 Cookie。

## 版本识别

MCP 工具 `oa_version_status` 会读取公开 GitHub 仓库 `main` 分支的 `pyproject.toml`，与本机 `SERVER_VERSION` 比较。业务工具响应会附带 `versionCheck`，建议 Agent 在业务回复后调用该工具。

- 成功结果缓存 24 小时，失败结果缓存 1 小时，缓存文件为 `OA_AGENT_STATE_DIR/version-check.json`。
- 只有远端版本更高时返回 `updateAvailable=true` 和需要用户确认的升级提示。
- 检查失败返回非阻断结果，不泄露底层异常，也不影响 OA 业务工具。
- 版本工具不接受 OA 地址、账号、Cookie 等连接参数。
- 设置 `OA_AGENT_DISABLE_UPDATE_CHECK=1` 可关闭版本检查。
- 连接器不自动升级；用户确认后由 Agent 执行安装，并刷新或重启 MCP 进程。

## Agent 工具封装建议

对外暴露工具时建议只暴露以下动作：

- `version_status(force=False)`：检查公开版本，默认使用本机缓存
- `begin_auth(base_url)`：默认授权入口，返回本机授权链接
- `local_auth_status(auth_token)`
- `disable_auto_login()`：删除系统密码保险箱中的登录信息，保留当前 Cookie
- `login(base_url, username, password)`：兼容或调试入口，默认不暴露；不建议普通用户在聊天里输入密码
- `list_todos(page, page_size)`
- `get_detail(fd_id)`
- `search_objects(query, scope, page, page_size)`
- `get_object_detail(record_ref)`
- `download_attachment(record_ref, attachment_index, output_dir)`
- `batch_search_objects(queries, scope)`
- `prepare_approval(fd_id, action, note)` / `confirm_approval(token, confirmation_text)`
- `prepare_batch_approval(items)` / `confirm_batch_approval(token, "确认批量审批")`
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
