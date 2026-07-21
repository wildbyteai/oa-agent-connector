# OA MCP 安装与授权流程

目标流程：

1. 新用户下载安装 MCP。
2. 在 MCP 客户端中配置 `oa-agent-mcp`。
3. Agent 调用 `oa_begin_auth` 生成本机授权链接，用户点击后在本机页面完成 OA 授权。
4. 调用 `oa_list_todos` 查询当前登录账号有权限看到的待审批清单。
5. 需要搜索 OA 文档时，调用只读搜索、详情和附件下载工具，仍然只基于当前登录账号权限。

对普通用户的标准回复话术见 [oa_mcp_user_output_standard.md](oa_mcp_user_output_standard.md)。Agent 面向用户输出时应使用自然语言，不直接展示 MCP 参数、JSON、token、fdId、HTTP 错误等技术内容。

## 安装

从源码目录安装：

```bash
cd /path/to/oa-agent-connector
python3 -m pip install .
```

开发调试可用 editable 安装：

```bash
cd /path/to/oa-agent-connector
python3 -m pip install -e .
```

安装后应能运行：

```bash
oa-agent-mcp
```

该命令是 stdio MCP server，直接运行时会等待 MCP 客户端输入 JSON-RPC 消息。

安装后还应能运行配置生成命令：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

这个命令会按当前电脑生成带真实 `OA_AGENT_STATE_DIR` 的 MCP 配置。macOS/Linux 和 Windows 都使用当前用户目录下的 `.oa-agent-connector`。

## MCP 客户端配置

安装 Agent 应优先运行：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

然后把命令输出的 JSON 写入 MCP 客户端配置。

如果需要手动写配置，必须在目标电脑上生成真实绝对路径，并写入 `OA_AGENT_STATE_DIR`。

路径规则：

- macOS/Linux：当前用户主目录下的 `.oa-agent-connector`，例如 `/Users/yourname/.oa-agent-connector`
- Windows：当前用户目录下的 `.oa-agent-connector`，例如 `C:\\Users\\yourname\\.oa-agent-connector`

`<ABSOLUTE_STATE_DIR>` 是文档占位符，实际配置时不能原样保留。

示例配置：

```json
{
  "mcpServers": {
    "oa": {
      "command": "oa-agent-mcp",
      "env": {
        "OA_BASE_URL": "<OA_BASE_URL>",
        "OA_AGENT_STATE_DIR": "<ABSOLUTE_STATE_DIR>"
      }
    }
  }
}
```

`OA_BASE_URL` 由用户提供，`OA_AGENT_STATE_DIR` 由安装 Agent 按当前电脑系统和用户名生成。

## 第一次授权连接

优先调用 MCP 工具 `oa_begin_auth`：

```json
{
  "baseUrl": "<OA_BASE_URL>",
  "session": "default",
  "expiresInSeconds": 600
}
```

返回：

```json
{
  "ok": true,
  "authRequired": true,
  "authUrl": "http://127.0.0.1:<port>/authorize?state=<token>",
  "authToken": "<token>",
  "session": "default",
  "baseUrl": "<OA_BASE_URL>"
}
```

Agent 把 `authUrl` 展示给用户点击。用户在本机页面输入 OA 账号和密码；密码不会进入聊天记录或连接器文件。用户勾选“在这台电脑上安全记住”后，密码只保存到系统密码保险箱。授权页只监听 `127.0.0.1`，链接过期后需要重新发起。

授权成功后，可调用 `oa_auth_status` 验证当前 session，也可以调用 `oa_local_auth_status` 查询本机授权页状态。

`oa_login` 仍保留为兼容工具，但不作为普通用户默认授权方式。

默认安全策略：

- `oa_login` 默认不出现在 MCP 工具列表里；只有管理员显式设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才开放。
- `oa_begin_auth` 默认优先要求 OA 地址为 HTTPS。
- 如果企业内网 OA 只能使用 HTTP，MCP 会先返回 `transportSecurityRequired=true`、普通确认文案、一次性确认令牌和 `nextAction`。Agent 对普通用户只提示“请确认你正在登录公司 OA”，让用户回复“确认继续登录”；确认后再按 `nextAction` 调用 `oa_begin_auth(insecure=true)`，不需要用户手动改 MCP 配置或重启。确认令牌只给 Agent 内部传递，不展示给用户。
- HTTPS 跳过证书校验不走普通用户确认流程，需要管理员显式设置 `OA_AGENT_ALLOW_INSECURE_AUTH=1`。
- `OA_AGENT_ALLOW_INSECURE_AUTH=1` 只作为管理员预先批准可信内网 HTTP 或 HTTPS 跳过证书校验的全局例外；普通安装流程不需要设置。公司统一部署时可由管理员预置，普通用户就不会看到确认步骤。

MCP 状态目录只保存登录后的 Cookie、baseUrl、登录账号身份提示和待确认审批状态：

- 推荐目录：MCP 配置里的 `OA_AGENT_STATE_DIR`
- cookie 文件权限会尽量设置为 `0600`
- 不建议省略 `OA_AGENT_STATE_DIR`；不同 MCP 调用可能处在不同沙箱，固定绝对路径可以保证“准备审批”和“确认审批”读写同一份确认状态。
- MCP 不会因为一次查询失败或鉴权提示就删除 cookie；会保留原文件并引导用户重新登录。
- 只有用户明确清理会话，或后续实现能确认服务端会话已彻底失效并需要重置时，才应删除 cookie。

用户在本机授权页勾选“在这台电脑上安全记住”后：

- 密码只保存到 macOS 钥匙串或 Windows 凭据管理器，不写入 `OA_AGENT_STATE_DIR`。
- Cookie 明确失效时，MCP 会先自动登录并重试原操作一次。
- 自动登录失败后会进入 15 分钟冷却；连续失败 3 次后自动登录会停止，必须重新授权，避免旧密码导致账号锁定。
- 用户说“关闭 OA 自动登录”时，调用 `oa_disable_auto_login`。它只删除系统密码保险箱中的登录信息，不删除当前 Cookie。
- 如果返回 `autoLoginCleanupRequired=true`，说明连接器已经停止使用旧凭据，但系统密码保险箱清理未完成；再次调用 `oa_disable_auto_login` 重试，不要向用户声称已经删除。

## 查询待办

调用 MCP 工具 `oa_list_todos`：

```json
{
  "session": "default",
  "page": 1,
  "pageSize": 20
}
```

返回格式：

```json
{
  "items": [
    {
      "fdId": "00000000000000000000000000000001",
      "subject": "请假申请_示例员工(带薪年假：1天)",
      "raw": {}
    }
  ],
  "page": 1,
  "pageSize": 20,
  "session": "default"
}
```

待办查询使用 OA 现有前端数据源：

```text
GET /km/review/km_review_index/kmReviewIndex.do?method=list&j_path=/listApproval&mydoc=approval
```

## 用户直接说“查看我的 OA 待办”

推荐 agent 行为：

1. 先调用 `oa_list_todos`。
2. 如果成功，直接展示待办清单。
3. 如果返回 `isError=true` 且内容里有 `guide`，把 guide 展示给用户。
4. Cookie 失效时，MCP 会先尝试自动恢复。只有未保存登录信息或自动恢复失败时，guide 才会提示重新授权；此时调用 `oa_begin_auth`，把本机授权链接展示给用户点击。
5. 用户完成本机授权后，调用 `oa_auth_status` 或直接重试 `oa_list_todos`。

注意边界：

- 如果 MCP 客户端根本没有配置 `oa-agent-mcp`，这个 MCP 无法被调用，因此不能由 MCP 自己弹出引导；需要客户端安装页、插件市场说明或人工文档先完成 MCP 配置。
- 一旦 MCP 已配置但缺少 `OA_BASE_URL` 或没有可用授权，`oa_list_todos` 会返回分步引导，不会只抛出裸错误，也不会擅自删除已有 Cookie。Cookie 过期且已安全保存登录信息时，会先自动恢复。
- 如果返回里有 `reauthRequired=true` 和 `nextAction`，Agent 不需要询问“是否重新授权”，应直接按 `nextAction` 发起本机授权，把返回的授权链接给用户点击。
- 如果返回里有 `transportSecurityRequired=true` 和 `nextAction`，Agent 必须先提示用户“请确认你正在登录公司 OA”。用户回复“确认继续登录”后，再按 `nextAction` 继续发起本机授权；不要把确认令牌展示给用户。
- 如果返回里有 `configurationRequired=true`，说明连接器还不知道 OA 地址。Agent 应先让用户提供 OA 地址，重新生成 MCP 配置，再继续授权。

可主动调用 `oa_setup_guide` 获取同一套引导：

```json
{
  "reason": "用户想查看 OA 待办，但尚未完成授权"
}
```

## 用户直接说“在 OA 里搜索”

推荐 agent 行为：

1. 识别用户要在 OA 内部搜索，而不是网页搜索或本地文件搜索。
2. 调用 `oa_search_objects`。普通 OA 文档搜索优先使用 `scope=knowledge`；只有用户明确要搜全部 OA 时才使用 `scope=all`。
3. 保持默认 `requireDetail=true`，只返回可继续查看详情的结果。
4. 如果用户表达“完整标题”“完全匹配某个产品名”“标题里有这句话”，优先使用 `matchMode=contains`。它会自动忽略 OA 标题中的空白，并默认按文档去重。
5. 如果用户明确要求“标题必须完全等于这句话”，使用 `matchMode=exact`。它同样会忽略标题中的空白，但要求归一化后完全相等。
6. 不要为了标题搜索额外依赖 OA 远端标题字段过滤。`searchFields=["title"]` 会由 MCP 转成本地标题过滤，不会下发 OA 的标题字段，避免 OA 搜索接口返回通用错误。
6. 展示 OA 返回的前几条结果，普通用户只看标题、创建人、时间、附件数量和序号。
7. 用户说“看第 1 条详情”时，使用搜索结果里的 `detailAction` 或 `recordRef` 调用 `oa_get_object_detail`；如果 Agent 客户端支持链接，也可以展示 `detailUrl` 让用户点击打开 OA 原生详情页。
8. 用户说“下载第 1 条附件”时，先确认该详情页里有附件，再用 `oa_download_attachment` 下载。

推荐触发话术：

```text
在 OA 里搜索：示例产品
```

```text
帮我查一下 OA 里有没有示例产品的出厂报告
```

```text
在 OA 里搜索示例产品，打开第一条并下载附件
```

注意边界：

- 搜索结果来自 OA 当前登录账号可见的数据。
- 查询结果列表必须能继续查看详情；不可查看详情的数据不能放进用户可选择列表。
- `detailUrl` 只用于打开 OA 原生详情页。用户浏览器没有登录 OA 时，点击后可能先看到 OA 登录页。
- `oa_search_objects` 已内置标题去空白匹配、`matchMode=contains/exact`、标题字段本地过滤和默认按文档去重。Agent 如果再做额外过滤或重新排序，必须明确告诉用户这是 Agent 的二次处理。
- 附件下载只能基于 `oa_get_object_detail` 返回的附件序号，不能接受任意下载 URL。
- 下载目录应使用本机安全目录；不要覆盖用户已有文件，除非用户明确要求。

## 权限边界

- 必须先完成 OA 授权，后续查询使用该登录 cookie。
- `oa_list_todos` 只返回 OA 现有接口对当前登录账号可见的数据。
- `oa_get_detail` 默认要求 `fdId` 必须在当前登录账号待办清单中。
- `oa_search_objects`、`oa_get_object_detail`、`oa_download_attachment` 只做当前账号权限内的只读搜索、详情查看和附件下载。
- `oa_download_attachment` 只允许下载详情页里枚举出来的附件，不暴露任意 URL 下载能力。
- MCP 正式审批必须走 `oa_prepare_approval` -> 用户确认 -> `oa_confirm_approval`。
- MCP 批量审批必须走 `oa_prepare_batch_approval` -> 用户确认 -> `oa_confirm_batch_approval`，固定确认词为 `确认批量审批`。
- `oa_prepare_approval` 会先确认 `fdId` 在当前登录账号待办清单中，并从详情页确认用户选择的同意或驳回动作确实可用，再整理单据、动作、备注、当前节点、当前处理人。
- `oa_confirm_approval` 执行前会再次查询当前登录账号待办清单，`fdId` 不在清单中则拒绝。
- `oa_prepare_batch_approval` 单次最多接受 20 条，可混合同意和驳回；所有项目都通过待办权限、动作和 workitem 绑定校验后才生成一个批量确认 token。
- MCP 正式审批工具不接受 `futureNodeId`。需要人工选择下一节点时，必须让用户在 OA 原生页面处理。
- `oa_confirm_batch_approval` 按确认摘要顺序严格串行处理。遇到第一条失败或结果不明确立即停止，返回已完成、当前项目和未执行项目；不自动重试整批。
- 批量审批不是事务。前面已完成的项目不会因后续失败而回滚，未执行项目需要重新查询待办后另建新批次。
- 确认阶段会先规范化 OA 地址，再为 `baseUrl + fdId + processId + taskId` 建立跨 token 的本地原子锁；同一个 workitem 即使出现在两个批次，或同时出现在单条和批量 token 中，也只能提交一次。不确定锁不自动接管，避免并发清理旧锁时出现重复提交。
- 批量完成、失败或结果不明确后，`.processing` 会保留只读终态。相同 token 再次调用只返回已保存结果，不会重新提交；过期终态可安全清理。
- 审批执行从 OA 原生详情页读取当前处理人和可用动作，不依赖申请正文的编辑权限。即使节点禁止修改正文，只要 OA 详情页确认当前账号可审批，连接器仍可按原生流程处理。
- 当前审批任务和默认驳回节点都遵循 OA 页面自身规则，不允许 Agent 自行指定审批人或猜测驳回节点。
- 准备审批时会绑定当前登录账号、流程、任务、节点和动作；自动登录恢复后会再次核对账号，确认时任一项变化都会拒绝执行，并要求重新准备。
- 审批请求只发送一次。OA 没有返回明确结果时，连接器只刷新一次待办获取核对线索；即使单据已离开待办也不能据此宣称成功，必须提示用户去 OA 页面查看，不能自动重试。
- `oa_approve` / `oa_reject` 仅保留 dry-run 兼容；MCP 禁止通过它们直接 `execute=true`。
- MCP 不暴露原始 URL 请求、原始 `flowParam` 或任意 `handler` 参数。

## 审批操作流程

用户选择一条待办后，推荐 agent 行为如下：

1. 调用 `oa_get_detail` 查看详情。
2. 和用户确认想做的动作：同意或驳回。
3. 向用户确认审批备注/意见。
4. 调用 `oa_prepare_approval`，只准备不提交。
5. 把返回的 `summary` 整理给用户确认，至少包含：
   - 单据 `fdId`
   - 主题
   - 当前节点
   - 当前处理人
   - 动作：同意/驳回
   - 审批备注
   - 权限校验结果
6. 用户明确回复 `确认审批` 或 `确认驳回` 后，调用 `oa_confirm_approval`。

准备同意审批：

```json
{
  "fdId": "00000000000000000000000000000001",
  "action": "approve",
  "note": "同意",
  "session": "default"
}
```

准备驳回：

```json
{
  "fdId": "00000000000000000000000000000001",
  "action": "reject",
  "note": "资料不完整，请补充后再提交",
  "session": "default"
}
```

`oa_prepare_approval` 返回 `confirmationToken` 和 `confirmationPhrase`。用户确认后执行：

```json
{
  "confirmationToken": "prepare 返回的 token",
  "confirmationText": "确认审批"
}
```

驳回时 `confirmationText` 必须是：

```text
确认驳回
```

确认 token 默认 15 分钟有效，过期后需要重新准备审批。执行时会重新校验当前账号是否仍有这条待办的审批权限。提交请求一旦发出，同一个 token 不会再次使用；如果结果不明确，先到 OA 页面核对，不能直接重复确认。

### 批量审批

用户一次选择多条待办时，Agent 应先把每条单据的动作和备注确认清楚，再调用 `oa_prepare_batch_approval`。示例：

```json
{
  "items": [
    {
      "fdId": "00000000000000000000000000000001",
      "action": "approve",
      "note": "同意"
    },
    {
      "fdId": "00000000000000000000000000000002",
      "action": "reject",
      "note": "请补充资料后重新提交"
    }
  ],
  "session": "default"
}
```

准备成功后，Agent 必须向用户逐条展示主题、当前节点、当前处理人、动作和备注，并明确说明：批量审批会按顺序逐条处理，不是事务，中途失败时前面已完成的项目不会回滚，后续项目不会执行。

用户只有明确回复下面的固定确认词后，Agent 才能执行：

```text
确认批量审批
```

然后调用 `oa_confirm_batch_approval`：

```json
{
  "confirmationToken": "prepare 返回的批量 token",
  "confirmationText": "确认批量审批"
}
```

执行结果必须按三组向用户说明：`completedItems` 为已完成，`currentItem` 为当前失败或结果不明确的项目，`notAttemptedItems` 为尚未执行。不得复用原 token，也不得自动重发整批。

如果连接器明确收到 OA 成功结果，但本机进度记录无法安全落盘，必须立即停止后续项目，返回 `statePersistenceWarning=true`，并把当前已成功项目放进 `completedItems`。Agent 应先刷新待办核对，再重新准备剩余项目。

## 可用工具

- `oa_setup_guide`：返回配置、授权、查询的分步引导。
- `oa_begin_auth`：启动本机授权页面，返回可点击授权链接。
- `oa_local_auth_status`：查询本机授权页面状态。
- `oa_disable_auto_login`：删除当前会话在系统密码保险箱中的登录信息，保留 Cookie。
- `oa_login`：兼容登录工具，保存 cookie，不保存密码；默认不暴露，只有设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才会出现在工具列表里。
- `oa_auth_status`：检查当前 session 是否仍有效，并尽量返回当前登录身份 `loginAs`。
- `oa_list_todos`：查询当前登录账号待办清单。
- `oa_get_detail`：查看待审批单据详情。
- `oa_get_search_schema`：查看当前 MCP 支持的 OA 搜索范围、字段、排序和限制。
- `oa_search_objects`：执行 OA 通用只读搜索，支持 `matchMode=keyword/contains/exact`，默认 `requireDetail=true` 且按文档去重，返回可继续查看详情的结果。
- `oa_get_object_detail`：按搜索结果查看 OA 文档详情和附件列表。
- `oa_download_attachment`：按详情页附件序号下载当前账号可见附件。
- `oa_batch_search_objects`：批量执行 OA 搜索，可用于多关键词查找。
- `oa_prepare_approval`：准备审批动作，生成待用户确认的摘要和 token。
- `oa_confirm_approval`：用户确认后执行审批，执行前再次校验权限。
- `oa_prepare_batch_approval`：准备最多 20 条批量审批，全部校验通过后生成一个确认摘要和 token。
- `oa_confirm_batch_approval`：用户回复 `确认批量审批` 后串行执行，首条失败或结果不明确时停止。
- `oa_approve`：同意审批 dry-run 兼容工具，MCP 禁止直接执行。
- `oa_reject`：驳回审批 dry-run 兼容工具，MCP 禁止直接执行。
