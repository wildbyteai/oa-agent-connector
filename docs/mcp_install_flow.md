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

Agent 把 `authUrl` 展示给用户点击。用户在本机页面输入 OA 账号和密码；密码不会进入聊天记录，也不会保存。授权页只监听 `127.0.0.1`，过期后需要重新发起。

授权成功后，可调用 `oa_auth_status` 验证当前 session，也可以调用 `oa_local_auth_status` 查询本机授权页状态。

`oa_login` 仍保留为兼容工具，但不作为普通用户默认授权方式。

默认安全策略：

- `oa_login` 默认不出现在 MCP 工具列表里；只有管理员显式设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才开放。
- `oa_begin_auth` 默认要求 OA 地址为 HTTPS，且不允许跳过证书校验。
- 如果企业内网 OA 只能使用 HTTP，或必须跳过证书校验，需要管理员在 MCP 配置中显式设置 `OA_AGENT_ALLOW_INSECURE_AUTH=1`。

MCP 只保存登录后的 cookie、baseUrl 和待确认审批状态：

- 推荐目录：MCP 配置里的 `OA_AGENT_STATE_DIR`
- cookie 文件权限会尽量设置为 `0600`
- 不建议省略 `OA_AGENT_STATE_DIR`；不同 MCP 调用可能处在不同沙箱，固定绝对路径可以保证“准备审批”和“确认审批”读写同一份确认状态。
- MCP 不会因为一次查询失败或鉴权提示就删除 cookie；会保留原文件并引导用户重新登录。
- 只有用户明确清理会话，或后续实现能确认服务端会话已彻底失效并需要重置时，才应删除 cookie。

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
4. 如果 guide 提示未授权，调用 `oa_begin_auth`，把返回的本机授权链接展示给用户点击。
5. 用户完成本机授权后，调用 `oa_auth_status` 或直接重试 `oa_list_todos`。

注意边界：

- 如果 MCP 客户端根本没有配置 `oa-agent-mcp`，这个 MCP 无法被调用，因此不能由 MCP 自己弹出引导；需要客户端安装页、插件市场说明或人工文档先完成 MCP 配置。
- 一旦 MCP 已配置但缺少 `OA_BASE_URL`、没有登录 cookie、cookie 过期，`oa_list_todos` 会返回分步引导，不会只抛出裸错误，也不会擅自删除已有 cookie。
- 如果返回里有 `reauthRequired=true` 和 `nextAction`，Agent 不需要询问“是否重新授权”，应直接按 `nextAction` 发起本机授权，把返回的授权链接给用户点击。
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
- `oa_search_objects` 已内置标题去空白匹配、`matchMode=contains/exact` 和默认按文档去重。Agent 如果再做额外过滤或重新排序，必须明确告诉用户这是 Agent 的二次处理。
- 附件下载只能基于 `oa_get_object_detail` 返回的附件序号，不能接受任意下载 URL。
- 下载目录应使用本机安全目录；不要覆盖用户已有文件，除非用户明确要求。

## 权限边界

- 必须先完成 OA 授权，后续查询使用该登录 cookie。
- `oa_list_todos` 只返回 OA 现有接口对当前登录账号可见的数据。
- `oa_get_detail` 默认要求 `fdId` 必须在当前登录账号待办清单中。
- `oa_search_objects`、`oa_get_object_detail`、`oa_download_attachment` 只做当前账号权限内的只读搜索、详情查看和附件下载。
- `oa_download_attachment` 只允许下载详情页里枚举出来的附件，不暴露任意 URL 下载能力。
- MCP 正式审批必须走 `oa_prepare_approval` -> 用户确认 -> `oa_confirm_approval`。
- `oa_prepare_approval` 会先确认 `fdId` 在当前登录账号待办清单中，并整理单据、动作、备注、当前节点、当前处理人。
- `oa_confirm_approval` 执行前会再次查询当前登录账号待办清单，`fdId` 不在清单中则拒绝。
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

确认 token 默认 15 分钟有效，过期后需要重新准备审批。执行时会重新校验当前账号是否仍有这条待办的审批权限。

## 可用工具

- `oa_setup_guide`：返回配置、授权、查询的分步引导。
- `oa_begin_auth`：启动本机授权页面，返回可点击授权链接。
- `oa_local_auth_status`：查询本机授权页面状态。
- `oa_login`：兼容登录工具，保存 cookie，不保存密码；默认不暴露，只有设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时才会出现在工具列表里。
- `oa_auth_status`：检查当前 session 是否仍有效。
- `oa_list_todos`：查询当前登录账号待办清单。
- `oa_get_detail`：查看待审批单据详情。
- `oa_get_search_schema`：查看当前 MCP 支持的 OA 搜索范围、字段、排序和限制。
- `oa_search_objects`：执行 OA 通用只读搜索，支持 `matchMode=keyword/contains/exact`，默认 `requireDetail=true` 且按文档去重，返回可继续查看详情的结果。
- `oa_get_object_detail`：按搜索结果查看 OA 文档详情和附件列表。
- `oa_download_attachment`：按详情页附件序号下载当前账号可见附件。
- `oa_batch_search_objects`：批量执行 OA 搜索，可用于多关键词查找。
- `oa_prepare_approval`：准备审批动作，生成待用户确认的摘要和 token。
- `oa_confirm_approval`：用户确认后执行审批，执行前再次校验权限。
- `oa_approve`：同意审批 dry-run 兼容工具，MCP 禁止直接执行。
- `oa_reject`：驳回审批 dry-run 兼容工具，MCP 禁止直接执行。
