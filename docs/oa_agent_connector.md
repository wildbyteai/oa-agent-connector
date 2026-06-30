# OA Agent Connector

这个连接器不改 OA 服务端源码，只使用源码里已经存在的接口：

- 登录：`POST /j_acegi_security_check`
- 待我审列表：`GET /km/review/km_review_index/kmReviewIndex.do?method=list&j_path=/listApproval&mydoc=approval`
- 审批处理：`POST /api/km-review/kmReviewRestService/approveProcess`
- 详情查看：`GET /km/review/km_review_main/kmReviewMain.do?method=view&fdId=...`

## 权限控制

审批执行前有强制 gate：

1. 必须先登录，连接器使用登录后 cookie。
2. 每次审批前重新查询当前登录账号的 `type=unExecuted` 待审列表。
3. 只有 `fdId` 在当前待审列表中才允许提交审批接口。
4. 连接器不接受也不传入任意 `handler`，避免绕过当前登录账号权限。
5. `approve` / `reject` 默认 dry-run，只有显式 `--execute` 才会真正提交。

## 用法

```bash
export OA_BASE_URL="https://oa.example.com/"
python3 -m oa_agent_connector.cli login --username "zhangsan"
python3 -m oa_agent_connector.cli todos
python3 -m oa_agent_connector.cli detail "fd_id_here"
python3 -m oa_agent_connector.cli approve "fd_id_here" --note "同意"
python3 -m oa_agent_connector.cli approve "fd_id_here" --note "同意" --execute
python3 -m oa_agent_connector.cli reject "fd_id_here" --note "不同意，原因..."
```

登录 cookie 默认保存在当前目录 `.oa-session.cookies`，文件权限会尽量设置为 `0600`。也可以用 `OA_COOKIE_FILE` 或 `--cookie-file` 指定。连接器不会因为一次请求失败或鉴权提示就删除 cookie；失效时先提示重新登录，只有用户明确清理会话或实现能确认服务端会话必须重置时才删除。

## Agent 工具封装建议

对外暴露工具时建议只暴露以下动作：

- `login(base_url, username, password)`
- `list_todos(page, page_size)`
- `get_detail(fd_id)`
- `approve(fd_id, note, execute=false)`
- `reject(fd_id, note, execute=false)`

不要暴露原始 `flowParam`、`handler`、任意 URL 请求能力。需要扩展会签、转办、人工决策节点时，也应增加白名单参数，并继续保留“当前登录账号待审列表包含 fdId”的 gate。

## MCP Server

本项目已提供 stdio MCP server：

```bash
oa-agent-mcp
```

面向实施和调试的完整安装、授权、查询流程见 [mcp_install_flow.md](mcp_install_flow.md)。

面向同事发布的标准使用流程见 [oa_mcp_colleague_guide.md](oa_mcp_colleague_guide.md)。

面向普通用户的标准回复话术见 [oa_mcp_user_output_standard.md](oa_mcp_user_output_standard.md)。
