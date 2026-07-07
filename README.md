# OA 助手连接器

这个连接器让 Agent 可以帮用户使用 OA：查看待办清单、查看申请详情、在用户明确确认后提交审批同意或驳回；也可以搜索 OA 文档、查看文档详情、下载当前账号有权限看到的附件。

## 一句话安装或升级

把下面这句话发给另一台电脑上的 Agent 即可：

```text
请安装或升级 OA 助手连接器：https://github.com/wildbyteai/oa-agent-connector.git
```

如果 Agent 支持从 GitHub 安装 MCP，它应自动读取本仓库 README，完成安装、升级、配置和授权引导。目标电脑需要能访问这个 GitHub 仓库。如果仓库是私有的，请先确保那台电脑已登录有权限的 GitHub 账号。

## Agent 应该怎么执行

收到上面那句话后，Agent 应按下面流程处理：

1. 如果未安装，安装本仓库；如果已安装旧版本，升级到本仓库最新版本。
2. 向用户询问 OA 地址，不要使用仓库外的默认地址。
3. 运行 `oa-agent-mcp-config --base-url "<用户提供的OA地址>"` 生成 MCP 配置。
4. 确认配置里有这台电脑真实的 `OA_AGENT_STATE_DIR`，不要手写占位符。
5. 把生成的 MCP 配置加入 Agent 客户端，并刷新或重启 MCP 工具。
6. 引导用户用自己的 OA 账号登录授权。密码只用于登录，不保存。
7. 不要主动删除已有 cookie，只有登录明确失效时才重新授权。

安装或升级命令：

```bash
python3 -m pip install --upgrade --force-reinstall "git+https://github.com/wildbyteai/oa-agent-connector.git"
```

Windows 可用：

```powershell
py -m pip install --upgrade --force-reinstall "git+https://github.com/wildbyteai/oa-agent-connector.git"
```

## 重要配置

MCP 配置里必须显式设置 `OA_AGENT_STATE_DIR`，并且必须是真实绝对路径。

审批是两步操作：先准备，再确认。部分 Agent 客户端会让每次 MCP 调用运行在不同进程或不同沙箱里。如果没有固定 `OA_AGENT_STATE_DIR`，准备审批时保存的确认状态，确认审批时可能读不到。

推荐不要手写这个路径，直接运行：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

把命令输出的 JSON 放到 Agent 客户端 MCP 配置里即可。macOS 和 Windows 都兼容。

## 用户怎么用

配置完成后，用户只需要对 Agent 说：

```text
查看我的 OA 待办
```

第一次使用时，Agent 会要求用户输入自己的 OA 账号和密码完成授权。密码只用于登录，不会保存。

之后可以继续说：

```text
看第 1 条详情
```

或：

```text
同意第 1 条，备注同意
```

或：

```text
驳回第 1 条，备注请补充资料
```

也可以搜索 OA 文档：

```text
在 OA 里搜索：示例产品
```

用户说“完全匹配某个产品名”时，Agent 应优先使用 `matchMode=contains`，它会自动忽略 OA 标题里的空格，并按文档去重。`matchMode=exact` 表示标题去空格后必须和搜索词完全相等，适合标题非常确定的场景。

所有展示给用户的查询结果都应能继续查看详情。搜索工具默认只返回可查看详情的结果；如果某类 OA 数据暂不支持详情解析，Agent 不应把它放进可点击列表。

继续查看和下载：

```text
打开第 1 条详情
```

```text
下载第 1 条附件
```

审批不会直接提交。Agent 会先整理确认信息，用户必须明确回复下面的固定确认词后才会执行：

- 同意：`确认审批`
- 驳回：`确认驳回`

## Agent 必须遵守

- 先登录授权，再查询和处理 OA。
- 只使用当前登录账号能看到的待办。
- 只允许查看当前账号待办里的申请详情。
- 搜索、详情和附件下载只使用当前登录账号在 OA 里能访问到的内容。
- 下载附件时只能下载详情页里列出的可见附件，不能让用户手写任意下载地址。
- 审批前必须展示单据、动作、备注，让用户确认。
- 用户没有明确回复 `确认审批` 或 `确认驳回`，不能提交。
- 不保存用户密码。
- 不修改 OA 服务端。
- 不绕过 OA 权限。

## 手动安装

如果需要手动安装，运行：

```bash
python3 -m pip install "git+https://github.com/wildbyteai/oa-agent-connector.git"
```

安装后会得到 MCP 命令：

```bash
oa-agent-mcp
```

普通用户不需要手动运行这个命令。它是给支持 MCP 的 Agent 客户端调用的。

还会得到一个配置生成命令：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

这个命令会输出已经带有本机真实 `OA_AGENT_STATE_DIR` 的 MCP 配置。macOS/Linux 会使用当前用户主目录下的 `.oa-agent-connector`，Windows 会使用当前用户目录下的 `.oa-agent-connector`。

## MCP 配置

让用户自己提供 OA 地址，然后优先运行：

```bash
oa-agent-mcp-config --base-url "<OA_BASE_URL>"
```

把命令输出的 JSON 加到 Agent 客户端配置里。`OA_AGENT_STATE_DIR` 会是目标电脑上的真实固定绝对路径，用于保存登录 cookie 和审批确认状态，避免不同沙箱或不同 MCP 调用之间状态不可见。

如果需要手动写配置，安装 Agent 必须在配置时把路径算出来并写进去，不要把 `<ABSOLUTE_STATE_DIR>` 原样留在配置里。

推荐路径：

- macOS/Linux：当前用户主目录下的 `.oa-agent-connector`，例如 `/Users/yourname/.oa-agent-connector`
- Windows：当前用户目录下的 `.oa-agent-connector`，例如 `C:\\Users\\yourname\\.oa-agent-connector`

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

配置后重启或刷新 Agent 客户端。

## 安装后验证

对 Agent 说：

```text
查看我的 OA 待办
```

或：

```text
在 OA 里搜索：示例产品
```

如果还没有登录，Agent 应提示：

```text
当前还没有完成 OA 授权。请提供你的 OA 账号和密码，我会用它登录 OA。密码只用于本次授权，不会保存。
```

## 更多说明

- [同事使用标准流程](docs/oa_mcp_colleague_guide.md)
- [对用户输出标准](docs/oa_mcp_user_output_standard.md)
- [安装与授权流程](docs/mcp_install_flow.md)
- [连接器技术说明](docs/oa_agent_connector.md)
