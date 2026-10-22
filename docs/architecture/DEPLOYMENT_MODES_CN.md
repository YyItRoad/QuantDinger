# QuantDinger 产品模式与轻量部署审计

## 结论

当前仓库已经有 `BILLING_ENABLED`、`ENABLE_REGISTRATION`、`SINGLE_USER_MODE` 和
`QUANTDINGER_DEPLOYMENT_MODE`，但它们分别控制计费、注册、旧版认证兼容和 Agent Token
策略，没有形成统一的产品模式。关闭计费只会停止扣积分，不会隐藏积分、充值、会员、推荐、
市场入口，也不会阻止所有相关 API 和后台初始化。

建议把“产品模式”和“运行拓扑”分开：

- 产品模式决定用户能看到和调用什么。
- 运行拓扑决定 Docker 启动哪些基础设施和 worker。
- 数据库保持一套迁移，不因模式删除表或维护分支 schema。

产品模式提供三个预设：`personal`、`team`、`saas`。设置页只让普通部署者选择预设，
高级运营者可以在“高级功能”中覆盖少数能力。不要把现有 `BILLING_ENABLED` 直接当作唯一总开关。

## 当前规模

- 管理设置后端定义了 17 个分组、233 个可编辑项，其中 119 个被标记为高级项。
- `.env.example` 包含约 410 个配置键。
- 默认 `docker-compose.yml` 定义了 18 个服务；目前只有 MCP 使用可选 Compose profile。
- PC、移动端和后端分别维护市场、积分、充值和推荐入口，没有共同的能力配置来源。

## 已确认的问题

### 设置页

`backend_api_python/app/routes/settings.py` 的 `_effective_schema()` 当前直接返回完整 schema。
它没有根据部署模式、已启用功能或依赖关系裁剪分组。前端设置页再把所有分组固定归入
Core、Research、Operations 和 Organization，所以新部署仍会看到完整运营后台。

`QUANTDINGER_DEPLOYMENT_MODE` 已经存在，但位于 Agent 分组，当前只影响 Agent Token
策略。它不是全局产品模式。

### PC 前端

`quantdinger_frontend/src/config/router.config.js` 总是注册市场、计费、个人资料和用户管理路由。
动态路由只按 admin 权限过滤，不按产品能力过滤。

`quantdinger_frontend/src/components/GlobalHeader/AvatarDropdown.vue` 总是显示积分和充值，并每
分钟请求一次计费计划接口。个人资料页还固定包含积分、会员和推荐内容。指标与策略编辑器固定
提供发布到市场的入口。

### 移动端

移动端固定注册 `/market/*`、`/profile/credits` 和 `/profile/referral`，个人中心会主动读取购买
记录和积分状态。关闭后端计费后，移动端产品表面仍然是 SaaS 形态。

### 后端语义

计费关闭时，主要 AI、回测和复盘路径会跳过扣款，这部分行为可以保留。但是：

- `/api/billing/plans` 仍可被调用，并会触发会员计划相关读取或初始化。
- 推荐奖励 API 没有统一的计费能力守卫。
- Community 和 Billing blueprint 始终注册。
- 市场购买在计费关闭时把有效价格改为 0，仍会交付原本的付费商品。这不等价于“关闭市场”，
  也可能违背运营者预期。
- USDT worker 会进入空闲循环，但相关路由、配置和表仍然对用户可见。

### 数据库

基础迁移包含用户积分、积分流水、会员订单、会员计划、Stripe、USDT、推荐奖励、市场购买和评论
等表。保留这些表的成本很低，并且能让安装实例以后无损切换到 SaaS。删除或按模式跳过这些表会
产生 schema 分叉、升级顺序和回滚问题。

### 部署拓扑

默认 Compose 同时包含 PostgreSQL、两个 Redis、Kafka、Kafka 初始化、API、交易 worker、
Kafka 审计、策略调度与评估、调度器、Celery worker/beat、PC 和移动端。对于只做研究、指标、
回测或少量本地交易的用户，这个默认拓扑过重。

## 目标产品模式

| 能力 | Personal 单机 | Team 私有团队 | SaaS 运营 |
| --- | --- | --- | --- |
| 数据库账号认证 | 开启，仅首个管理员 | 开启 | 开启 |
| 公开注册 | 关闭 | 关闭，管理员建号或邀请 | 开启 |
| 用户管理 | 隐藏 | 开启 | 开启 |
| 积分与额度扣费 | 关闭 | 关闭 | 开启 |
| 会员与充值 | 关闭 | 关闭 | 开启 |
| 推荐与提现 | 关闭 | 关闭 | 可选开启 |
| 市场目录 | 关闭 | 默认关闭，可选内部目录 | 开启 |
| 市场付费交易 | 关闭 | 关闭 | 开启，且依赖计费 |
| 品牌与法律设置 | 精简 | 开启 | 开启 |
| AI、指标、回测、策略 | 开启 | 开启 | 开启 |
| 实盘交易 | 按风险开关 | 按风险开关 | 按风险开关 |

Personal 不应自动启用 `SINGLE_USER_MODE=true`。该变量是旧版环境账号认证兼容路径。
新的单机模式仍使用 PostgreSQL 用户和首个管理员，只关闭公开注册和多用户管理界面，避免降低
认证与凭据安全性。

## 统一能力模型

继续使用 `QUANTDINGER_DEPLOYMENT_MODE`，把规范值扩展为：

```text
personal
team
saas
```

兼容值：`hosted` 映射为 `saas`。升级旧实例时，不能把空值直接视为 Personal，否则会突然隐藏
现有用户和运营入口。建议迁移规则为：

1. 明确设置了 `saas` 或 `hosted`：保持 SaaS。
2. 未设置且 `BILLING_ENABLED=true`：迁移为 SaaS。
3. 未设置且计费关闭：迁移为 Team，保持现有多用户行为。
4. 新安装模板明确写入 `personal`。

增加集中式 `SystemCapabilities` 服务。所有组件都从这里读取派生能力，禁止自行散落读取多个环境变量。
建议能力键：

```text
auth.public_registration
users.management
billing.credits
billing.checkout
billing.referrals
marketplace.catalog
marketplace.publish
marketplace.commerce
branding.customization
runtime.distributed
```

高级覆盖使用 `auto/on/off`，例如 `FEATURE_MARKETPLACE=auto`。`auto` 继承模式预设。依赖关系由
后端强制验证：

- `billing.checkout` 依赖 `billing.credits`。
- `billing.referrals` 依赖 `billing.checkout`。
- `marketplace.commerce` 依赖 `marketplace.catalog` 和 `billing.credits`。
- 关闭市场时，发布、购买、作者中心、审核和同步全部关闭。

## API 与前端改造

新增安全的启动配置接口，例如 `GET /api/system/capabilities`，登录前即可返回：

```json
{
  "product_mode": "personal",
  "capabilities": {
    "auth.public_registration": false,
    "users.management": false,
    "billing.credits": false,
    "billing.checkout": false,
    "billing.referrals": false,
    "marketplace.catalog": false,
    "marketplace.publish": false,
    "marketplace.commerce": false
  }
}
```

响应不得包含密钥、价格内部配置或基础设施信息。它可以逐步合并现有 `security-config` 和
`public-config` 中的公开开关，避免启动时重复请求。

后端需要同时在路由层和服务层守卫能力。路由层返回统一的 `feature_disabled`，服务层防止内部调用
绕过路由。不能只靠前端隐藏。

PC 路由增加 `meta.capability`，动态路由生成器按权限和能力共同过滤，导航守卫阻止手输 URL。
顶部积分、充值、个人资料标签、市场发布按钮都读取同一个 capability store。关闭后不请求计费和
市场 API。

移动端在应用 bootstrap 时加载相同接口，过滤市场、充值、推荐路由和个人中心卡片。默认首页不能
固定重定向到市场；Personal 应进入研究、指标或上次使用的工作区。

## 设置页重构

设置首页增加“运行方式”卡片，优先显示三种预设及其影响，不直接展示环境变量名：

- 个人使用：一个管理员，功能直接可用，无积分、充值和市场。
- 私有团队：管理员创建成员，无公开注册和商业计费。
- SaaS 运营：用户注册、积分、会员、支付和市场运营。

选择模式后只展示适用设置。Schema 的 group 和 item 增加：

```text
modes: [personal, team, saas]
requires_capabilities: [billing.checkout]
setup_level: essential | optional | advanced
```

Personal 的首次设置只需要：管理员、语言与时区、一个 AI Provider、使用的市场，以及可选的交易
账户。公共行情可用时不要强迫用户配置额外数据源。其余 200 多项放在“可选能力”和“高级设置”。

切换模式时先显示变更预览：将隐藏哪些入口、停止哪些新操作、哪些服务需要重启。SaaS 切回 Team
只停止新收费、充值和市场交易，保留余额、订单、购买记录和审计数据。

## 首次启动向导

增加一次性 Setup Wizard，不要求用户先理解完整设置页：

1. 选择 Personal、Team 或 SaaS。
2. 设置管理员和访问方式；Personal/Team 默认关闭公开注册。
3. 配置一个 AI Provider；允许跳过并先使用非 AI 功能。
4. 选择需要的市场和是否启用实盘组件。
5. 运行连接检查并进入产品。

向导保存后生成明确配置，不通过是否访问过某个页面来猜测状态。建议增加
`INITIAL_SETUP_COMPLETED=true`。管理员仍可从设置页重新运行向导。

## Compose 运行档位

产品模式不应直接等于 Compose profile，因为个人用户也可能需要实盘 worker，团队也可能只做研究。
建议拆成以下运行档位：

| Profile | 服务 |
| --- | --- |
| `core` | env-sync、PostgreSQL、Redis、migration、backend、frontend |
| `jobs` | redis-jobs、Celery worker；需要异步 AI/回测时开启 |
| `live` | trading-worker、scheduler-worker；需要持续实盘时开启 |
| `distributed` | Kafka、kafka-init、audit、strategy dispatcher/evaluator；规模化策略运行时开启 |
| `mobile` | 移动端站点 |
| `mcp` | MCP gateway |
| `ops` | Celery beat、监控与运营任务 |

默认快速启动使用 `core + jobs`，不启动 Kafka 和分布式 worker。完整 SaaS 示例显式启用全部所需
profile。修改 Compose 前要为核心研究、回测和本地交易分别做端到端验证，确认关闭 Celery 或 Kafka
时存在明确的同步/本地执行路径。

## 数据与停用规则

- 保留统一 `init.sql` 和后续迁移中的所有表。
- 能力关闭时停止新写入，不删除历史数据。
- 支付订单处于 pending 时禁止直接关闭 checkout，或要求管理员确认并先处理订单。
- 市场关闭时已购买代码仍保留在用户工作区；市场 API 不再提供新的购买和同步。
- 重新开启能力时复用原有余额、会员、订单和购买记录。
- 审计日志记录模式变更、操作者、变更前后值和时间。

## 实施顺序

### P0：建立正确边界

1. 新建集中式 capability 服务和模式兼容映射。
2. 新增 `/api/system/capabilities`。
3. 给 Billing、Referral、Community 的路由和服务增加能力守卫。
4. 修正“计费关闭即付费市场商品免费”的语义。
5. 添加模式矩阵测试，覆盖手输 URL 和直接调用 API。

### P1：真正减轻产品表面

1. PC bootstrap capability store，过滤路由、顶部积分、个人资料和发布入口。
2. 移动端应用相同过滤。
3. Settings schema 增加 mode、capability 和 setup level 元数据。
4. 设置首页加入三种运行方式和变更预览。

### P2：缩短首次启动

1. 增加 Setup Wizard 和最小连接检查。
2. 新安装模板默认 Personal；安装脚本只询问必要参数。
3. 补充 Personal、Team、SaaS 三条独立快速开始文档。

### P3：减轻基础设施

1. 为 Compose 增加 core、jobs、live、distributed、mobile 和 ops profiles。
2. 验证缺少 Kafka、Celery beat 和分布式 worker 时的降级路径。
3. 让关闭的支付和运营 worker 不初始化、不轮询，并在健康状态中报告 disabled。

## 验收标准

- 新 Personal 安装只填写少量必要项即可进入研究、指标和回测。
- PC 与移动端均不出现积分、充值、会员、推荐、市场及其空白入口。
- 直接访问被关闭的前端路由会跳回可用首页。
- 直接调用被关闭的后端 API 返回统一错误，不产生写入或免费交付。
- Personal 使用 AI、回测和策略时不读取或扣除积分。
- Team 可由管理员创建用户，但不暴露公开注册和商业计费。
- SaaS 能完整启用充值、会员、推荐和市场，并验证所有依赖配置。
- 从 Personal/Team 切换到 SaaS 不需要数据库迁移分支或数据搬迁。
- 默认轻量 Compose 不启动 Kafka 和分布式策略 worker，核心工作流仍通过测试。
