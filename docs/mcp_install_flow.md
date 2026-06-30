# OA MCP 安装与授权流程

目标流程：

1. 新用户下载安装 MCP。
2. 在 MCP 客户端中配置 `oa-agent-mcp`。
3. 用户用自己的 OA 账号调用 `oa_login` 授权连接。
4. 调用 `oa_list_todos` 查询当前登录账号有权限看到的待审批清单。

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

调用 MCP 工具 `oa_login`：

```json
{
  "baseUrl": "<OA_BASE_URL>",
  "username": "用户自己的 OA 账号",
  "password": "用户自己的 OA 密码",
  "session": "default"
}
```

返回：

```json
{
  "ok": true,
  "session": "default",
  "baseUrl": "<OA_BASE_URL>"
}
```

密码不会保存。MCP 只保存登录后的 cookie、baseUrl 和待确认审批状态：

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
4. 如果 guide 提示未授权，向用户索取 OA 账号密码，调用 `oa_login`。
5. `oa_login` 成功后，再调用 `oa_list_todos`。

注意边界：

- 如果 MCP 客户端根本没有配置 `oa-agent-mcp`，这个 MCP 无法被调用，因此不能由 MCP 自己弹出引导；需要客户端安装页、插件市场说明或人工文档先完成 MCP 配置。
- 一旦 MCP 已配置但缺少 `OA_BASE_URL`、没有登录 cookie、cookie 过期，`oa_list_todos` 会返回分步引导，不会只抛出裸错误，也不会擅自删除已有 cookie。

可主动调用 `oa_setup_guide` 获取同一套引导：

```json
{
  "reason": "用户想查看 OA 待办，但尚未完成授权"
}
```

## 权限边界

- 必须先 `oa_login`，后续查询使用该登录 cookie。
- `oa_list_todos` 只返回 OA 现有接口对当前登录账号可见的数据。
- `oa_get_detail` 默认要求 `fdId` 必须在当前登录账号待办清单中。
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

- `oa_login`：登录授权，保存 cookie。
- `oa_setup_guide`：返回配置、授权、查询的分步引导。
- `oa_auth_status`：检查当前 session 是否仍有效。
- `oa_list_todos`：查询当前登录账号待办清单。
- `oa_get_detail`：查看待审批单据详情。
- `oa_prepare_approval`：准备审批动作，生成待用户确认的摘要和 token。
- `oa_confirm_approval`：用户确认后执行审批，执行前再次校验权限。
- `oa_approve`：同意审批 dry-run 兼容工具，MCP 禁止直接执行。
- `oa_reject`：驳回审批 dry-run 兼容工具，MCP 禁止直接执行。
