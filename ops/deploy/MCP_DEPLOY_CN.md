# MCP 生产环境部署说明

QuantDinger MCP 使用官方 `mcp_server/Dockerfile` 独立构建，通过手动 GitHub Actions 工作流发布和部署。MCP 不公开映射 `7800` 端口，只允许前端 Nginx 通过 Docker 内网访问。

公网地址：`https://trade.yangyang.fun/mcp`

## 首次部署前准备

在云服务器创建 `/opt/quantdinger/mcp.env`：

```env
QUANTDINGER_AGENT_TOKEN=qd_agent_替换为后台生成的Agent令牌
QUANTDINGER_MCP_AUTH_TOKEN=替换为单独生成的至少32位随机字符串
```

建议生成入站令牌：

```bash
openssl rand -hex 32
```

随后限制文件权限：

```bash
chmod 600 /opt/quantdinger/mcp.env
```

两个令牌用途不同，禁止设置为相同值：

- `QUANTDINGER_AGENT_TOKEN`：MCP 调用 QuantDinger 后端时使用，权限由 Agent Gateway 控制。
- `QUANTDINGER_MCP_AUTH_TOKEN`：公网客户端连接 MCP 时使用，仅保护 MCP 入口。

## 手动发布

在 QuantDinger 仓库的 GitHub Actions 页面选择 `Build and deploy MCP to personal server`，运行工作流即可。默认镜像标签为 `manual-<提交短哈希>`。

工作流只更新 `quantdinger-mcp` 容器，不会迁移数据库，也不会重启后端、Trading Worker、Celery 或前端。

首次发布 MCP 后，还需要发布一次包含 `/mcp` 反代配置的 QuantDinger-Vue 前端镜像。

## 客户端配置

MCP Endpoint：

```text
https://trade.yangyang.fun/mcp
```

请求头：

```text
Authorization: Bearer <QUANTDINGER_MCP_AUTH_TOKEN>
```

## 安全边界

- 云服务器防火墙和 Docker 均不需要开放 `7800`。
- 公网 TLS 由现有域名入口终止；前端到 MCP 的 Docker 内网连接使用 HTTP。
- 不要把 `mcp.env`、Agent Token 或 MCP Token 提交到 Git 仓库。
- Agent Token 具有哪些查询或交易能力，取决于后台创建该 Token 时授予的权限。
