# MCP 生产环境部署说明

QuantDinger MCP 使用官方 `mcp_server/Dockerfile` 独立构建，通过手动 GitHub Actions 工作流发布和部署。MCP 不公开映射 `7800` 端口，只允许前端 Nginx 通过 Docker 内网访问。

公网地址：`https://trade.yangyang.fun/mcp`

## 首次部署前准备

在云服务器创建 `/opt/quantdinger/mcp.env`：

```env
QUANTDINGER_AGENT_TOKEN=qd_agent_替换为后台生成的Agent令牌
QUANTDINGER_MCP_AUTH_TOKEN=替换为单独生成的至少32位随机字符串

# 默认 static；接入 ChatGPT 后改为 hybrid
QUANTDINGER_MCP_AUTH_MODE=hybrid

# 必须是一枚独立、仅授予 R Scope 的 Agent Token
QUANTDINGER_MCP_OAUTH_AGENT_TOKEN=qd_agent_替换为只读Agent令牌
QUANTDINGER_MCP_OAUTH_ISSUER=https://你的OAuth身份服务域名/
QUANTDINGER_MCP_OAUTH_AUDIENCE=https://trade.yangyang.fun/mcp
QUANTDINGER_MCP_OAUTH_JWKS_URL=https://你的OAuth身份服务域名/.well-known/jwks.json
QUANTDINGER_MCP_OAUTH_ALLOWED_SUBJECT=替换为唯一允许登录的OAuth用户ID
```

建议生成入站令牌：

```bash
openssl rand -hex 32
```

`mcp.env` 必须归 GitHub Actions 使用的 `SERVER_USER` 所有。假如你使用 `root` 创建文件，而工作流通过 `deploy` 用户登录，需要先执行：

```bash
chown deploy:deploy /opt/quantdinger/mcp.env
chmod 600 /opt/quantdinger/mcp.env
```

上面的 `deploy` 需要替换成仓库 Secret `SERVER_USER` 的实际用户名。部署脚本不会使用 `sudo` 修改密钥文件归属。

两个令牌用途不同，禁止设置为相同值：

- `QUANTDINGER_AGENT_TOKEN`：MCP 调用 QuantDinger 后端时使用，权限由 Agent Gateway 控制。
- `QUANTDINGER_MCP_AUTH_TOKEN`：公网客户端连接 MCP 时使用，仅保护 MCP 入口。

混合认证还会使用第三枚令牌：

- `QUANTDINGER_MCP_OAUTH_AGENT_TOKEN`：ChatGPT OAuth 请求调用 QuantDinger 后端时使用。个人单用户版本必须只授予 `R` Scope，并与前两枚令牌不同。

`hybrid` 模式在同一个 `/mcp` 地址接受两种凭据：

- Claude Code、Codex 等现有客户端继续发送静态 `QUANTDINGER_MCP_AUTH_TOKEN`；
- ChatGPT 完成 OAuth 授权后发送身份服务签发的 Access Token；
- MCP 根据入站身份选择对应的 Agent Token，OAuth 请求不会使用原有高权限 Agent Token。

OAuth 身份服务必须支持 OAuth 2.1 授权码流程、PKCE `S256`、OAuth/OIDC discovery，以及 CIMD、DCR 或预定义客户端中的一种。`issuer`、`audience` 和用户 `subject` 均采用精确匹配，不会自动修正尾部斜杠。

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

上面的静态请求头继续供 Claude Code、Codex 等客户端使用。ChatGPT 不配置该静态 Token，而是通过 OAuth 登录获取 Access Token。

混合认证部署后还要检查：

```text
https://trade.yangyang.fun/.well-known/oauth-protected-resource/mcp
```

该地址必须返回 JSON，且 `resource` 精确等于 `https://trade.yangyang.fun/mcp`，不能返回前端 HTML。

## 安全边界

- 云服务器防火墙和 Docker 均不需要开放 `7800`。
- 公网 TLS 由现有域名入口终止；前端到 MCP 的 Docker 内网连接使用 HTTP。
- 不要把 `mcp.env`、Agent Token 或 MCP Token 提交到 Git 仓库。
- Agent Token 具有哪些查询或交易能力，取决于后台创建该 Token 时授予的权限。
- OAuth 请求会在 MCP 工具层检查 `quantdinger.read`、`quantdinger.write` 或 `quantdinger.trade` Scope；个人版第一阶段只签发 `quantdinger.read`。
- 同一个 `/mcp` 会向不同客户端返回同一份工具列表；ChatGPT 可能看到写入工具名称，但没有对应 Scope，且只读 Agent Token 会阻止实际写入。
