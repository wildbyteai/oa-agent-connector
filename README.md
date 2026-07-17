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
6. 引导用户点击本机 OA 授权链接登录。密码只在本机授权页面输入，不进入聊天记录或连接器文件。用户默认可以勾选“在这台电脑上安全记住”。
7. 用户勾选后，登录信息保存在 macOS 钥匙串或 Windows 凭据管理器。Cookie 明确失效时，连接器会先自动恢复登录，不需要用户每天重复输入。
8. 只有自动恢复不可用或失败时，MCP 才会返回 `reauthRequired=true`。此时直接按 `nextAction` 调用 `oa_begin_auth`，把本机授权链接发给用户点击。
9. 如果 MCP 返回 `transportSecurityRequired=true` 且带有 `nextAction`，对普通用户只提示“请确认你正在登录公司 OA”，让用户回复“确认继续登录”；确认后再按 `nextAction` 继续授权。`nextAction` 里的确认令牌只给 Agent 内部使用，不展示给用户。
10. 如果 MCP 返回 `configurationRequired=true`，先补齐 OA 地址并重新生成 MCP 配置，再授权。

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

安全默认值：

- 普通授权默认走 `oa_begin_auth` 本机授权页。
- 本机授权页默认提供“在这台电脑上安全记住”选项。勾选后，密码只存入 macOS 钥匙串或 Windows 凭据管理器，不写入 `OA_AGENT_STATE_DIR`、Cookie 文件、日志或聊天记录。
- Cookie 明确失效时，连接器会使用同一 OA 地址、同一会话和同一账号自动登录，然后把原查询重试一次。失败后会暂停 15 分钟；连续失败 3 次就停止自动登录并要求重新授权，避免错误密码导致账号锁定。
- 用户可以对 Agent 说“关闭 OA 自动登录”。连接器只删除系统密码保险箱里的登录信息，保留当前 Cookie。
- 如果电脑暂时无法清理系统密码保险箱，连接器会停止使用旧信息并明确提示用户重试，不会假装已经删除。
- `oa_login` 默认不会出现在 MCP 工具列表里。只有管理员显式设置 `OA_AGENT_ENABLE_PASSWORD_LOGIN=1` 时，才开放兼容登录工具。
- 本机授权默认优先要求 OA 地址是 HTTPS。如果企业内网 OA 只能使用 HTTP，`oa_begin_auth` 会先返回一次性确认令牌；Agent 对普通用户只提示“请确认你正在登录公司 OA”。用户明确回复“确认继续登录”后，Agent 再按 MCP 返回的 `nextAction` 继续授权，不需要用户手动改配置或重启。
- `OA_AGENT_ALLOW_INSECURE_AUTH=1` 仅作为管理员预先批准可信内网 HTTP 或 HTTPS 跳过证书校验的全局例外；普通安装流程不需要设置。公司统一部署时可由管理员预置，普通用户就不会看到确认步骤。

## 用户怎么用

配置完成后，用户只需要对 Agent 说：

```text
查看我的 OA 待办
```

第一次使用时，Agent 会给用户一个本机 OA 授权链接。用户点击链接，在本机页面输入自己的 OA 账号和密码完成授权。授权页默认勾选“在这台电脑上安全记住”；登录过期会自动恢复，密码不会进入聊天记录或连接器文件。

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

用户说“完全匹配某个产品名”时，Agent 应优先使用 `matchMode=contains`，它会自动忽略 OA 标题里的空格，并按文档去重。`matchMode=exact` 表示标题去空格后必须和搜索词完全相等，适合标题非常确定的场景。不要为了“标题搜索”额外强制下发 OA 标题字段；MCP 会在本地处理标题过滤，避免 OA 搜索接口报错。

所有展示给用户的查询结果都应能继续查看详情。搜索工具默认只返回可查看详情的结果；结果会带 `detailUrl`，Agent 可以把它展示成“打开详情”的链接。如果某类 OA 数据暂不支持详情解析，Agent 不应把它放进可点击列表。

`detailUrl` 使用用户配置的 OA 地址在运行时生成，不会写死到仓库里。用户点击后能否直接进入详情，取决于当前浏览器是否也已登录 OA；未登录时 OA 可能先显示登录页。

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
- 同一个审批确认只能执行一次，并且必须仍是准备审批时的同一 OA 登录账号。
- 审批提交只走一条 OA 表单路径；请求结果不明确时不能自动换路径重复提交。
- 不把用户密码写入聊天、连接器文件、Cookie 文件或日志。只有用户在本机授权页勾选后，才允许存入电脑自带的系统密码保险箱。
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

把命令输出的 JSON 加到 Agent 客户端配置里。`OA_AGENT_STATE_DIR` 会是目标电脑上的真实固定绝对路径，用于保存登录 Cookie 和审批确认状态，避免不同沙箱或不同 MCP 调用之间状态不可见。自动登录密码不保存在这个目录，而是保存在操作系统的密码保险箱里。

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
当前还没有完成 OA 授权。

请点击下面的本机授权链接，在页面里输入 OA 账号和密码。

勾选“在这台电脑上安全记住”后，登录过期会自动恢复；密码不会发到聊天或写入连接器文件。
```

## 更多说明

- [同事使用标准流程](docs/oa_mcp_colleague_guide.md)
- [对用户输出标准](docs/oa_mcp_user_output_standard.md)
- [安装与授权流程](docs/mcp_install_flow.md)
- [连接器技术说明](docs/oa_agent_connector.md)
