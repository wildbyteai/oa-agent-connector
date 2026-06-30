# OA 待办助手连接器

这个连接器让 Agent 可以帮用户处理 OA 待办：查看待办清单、查看申请详情，并在用户明确确认后提交审批同意或驳回。

## 一句话给其他 Agent

把下面这句话发给另一台电脑上的 Agent 即可：

```text
请安装并配置 OA 待办助手连接器：[wildbyteai/oa-agent-connector.git](https://github.com/wildbyteai/oa-agent-connector.git)。安装后先向我询问 OA 地址，并把它配置为 OA_BASE_URL；然后配置 MCP 命令 oa-agent-mcp。配置完成后，我会说“查看我的 OA 待办”，你再引导我用自己的 OA 账号登录授权。
```

目标电脑需要能访问这个 GitHub 仓库。如果仓库是私有的，请先确保那台电脑已登录有权限的 GitHub 账号。

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

审批不会直接提交。Agent 会先整理确认信息，用户必须明确回复下面的固定确认词后才会执行：

- 同意：`确认审批`
- 驳回：`确认驳回`

## Agent 必须遵守

- 先登录授权，再查询和处理 OA。
- 只使用当前登录账号能看到的待办。
- 只允许查看当前账号待办里的申请详情。
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

## MCP 配置

让用户自己提供 OA 地址，然后填到 `<OA_BASE_URL>`：

```json
{
  "mcpServers": {
    "oa": {
      "command": "oa-agent-mcp",
      "env": {
        "OA_BASE_URL": "<OA_BASE_URL>"
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

如果还没有登录，Agent 应提示：

```text
当前还没有完成 OA 授权。请提供你的 OA 账号和密码，我会用它登录 OA。密码只用于本次授权，不会保存。
```

## 更多说明

- [同事使用标准流程](docs/oa_mcp_colleague_guide.md)
- [对用户输出标准](docs/oa_mcp_user_output_standard.md)
- [安装与授权流程](docs/mcp_install_flow.md)
- [连接器技术说明](docs/oa_agent_connector.md)
