# OA Agent Connector

OA Agent Connector 是一个 stdio MCP 连接器，用于让 Agent 基于用户自己的 OA 登录会话查询待办、查看详情，并在用户明确确认后处理审批。

## 安全边界

- 不修改 OA 服务端源码。
- 必须先登录授权，后续操作只使用当前登录账号的 cookie。
- 查询待办只返回当前登录账号可见的数据。
- 查看详情默认要求单据在当前登录账号待办清单中。
- 审批必须走 `准备审批 -> 用户确认 -> 执行审批`。
- 用户没有明确回复 `确认审批` 或 `确认驳回` 时，不提交审批。
- 不保存用户密码，只保存登录后的 cookie。

## 安装

从 GitHub 安装：

```bash
python3 -m pip install "git+https://github.com/wildbyteai/oa-agent-connector.git"
```

如果要安装指定分支：

```bash
python3 -m pip install "git+https://github.com/<org>/<repo>.git@<branch>"
```

安装后检查命令是否可用：

```bash
oa-agent-mcp
```

这个命令是 MCP stdio server，直接运行时会等待 MCP 客户端输入；普通用户不需要手动常驻运行。

## 给其他 Agent 的直接安装提示词

最短可用版本：

```text
请安装并配置 OA MCP 连接器：[wildbyteai/oa-agent-connector.git](https://github.com/wildbyteai/oa-agent-connector.git)。安装后请向我询问 OA 地址，写入 MCP 环境变量 OA_BASE_URL，并配置 MCP 命令 oa-agent-mcp；之后我会说“查看我的 OA 待办”，你再引导我登录授权。
```

更完整版本如下。把 `<OA_BASE_URL>` 替换为用户自己的 OA 地址：

```text
请通过 GitHub 安装并配置 OA MCP 连接器。

GitHub 仓库地址：https://github.com/wildbyteai/oa-agent-connector.git
OA 地址：<OA_BASE_URL>

请按以下步骤执行：

1. 运行：
   python3 -m pip install "git+https://github.com/wildbyteai/oa-agent-connector.git"

2. 在当前 Agent 客户端中添加 MCP 配置：
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

3. 刷新或重启 Agent 客户端，让 MCP 生效。

4. 然后我会说“查看我的 OA 待办”。如果还没有授权，请引导我输入自己的 OA 账号和密码完成登录。密码只用于本次登录，不要保存。

5. 查询待办、查看详情、处理审批时，请遵守：只基于当前登录账号权限；审批必须先整理确认摘要；我明确回复“确认审批”或“确认驳回”后才允许提交。
```

如果仓库是私有仓库，目标电脑需要先具备 GitHub 访问权限，例如已登录 GitHub CLI 或已配置可访问该仓库的凭据。

## MCP 配置

安装后推荐配置：

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

把 `<OA_BASE_URL>` 替换为实际 OA 地址，例如企业内部 OA 地址。

如果不想安装，也可以 clone 后通过 `PYTHONPATH` 直接运行：

```json
{
  "mcpServers": {
    "oa": {
      "command": "python3",
      "args": ["-m", "oa_agent_connector.mcp_server"],
      "env": {
        "PYTHONPATH": "<repo_dir>",
        "OA_BASE_URL": "<OA_BASE_URL>"
      }
    }
  }
}
```

## 用户使用方式

用户可以直接对 Agent 说：

```text
查看我的 OA 待办
```

首次使用时，Agent 会引导用户输入自己的 OA 账号和密码完成授权。密码只用于本次登录，不会保存。

审批动作必须经过二次确认：

- 同意审批时，用户必须回复：`确认审批`
- 驳回审批时，用户必须回复：`确认驳回`

## 文档

- [安装与授权流程](docs/mcp_install_flow.md)
- [同事使用标准流程](docs/oa_mcp_colleague_guide.md)
- [对用户输出标准](docs/oa_mcp_user_output_standard.md)
- [连接器技术说明](docs/oa_agent_connector.md)

## 测试

```bash
python3 -m unittest tests/test_client.py tests/test_mcp_server.py
```
