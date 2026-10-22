# MCP 整仓接管开发验收

日期：2026-10-22。范围：本地 main 开发与自动化测试，未推送、未部署、未提交真实订单或接管真实账户。

## 变更

- 新增 `list_managed_positions`：R 权限，读取当前用户账户的策略管理归属。
- 新增 `adopt_position_management`：W 权限，外部 OAuth 使用 `quantdinger.write`；明确确认后执行现有整仓登记与自动启动。
- 复用现有 `create_managed_strategy`，不按订单拆仓，不限制既有人工仓位，不新增只支持永续或禁止剩余挂单等业务条件。
- 原共享服务仅增加可选的实际仓位授权回调，防止合约标识解析出的实际品种绕过 Agent 白名单；原 Web 调用不变。
- 接管不要求 T，不使用快捷开仓的模拟模式、实盘开关或名义额度；W 的接管授权包含启动后的真实策略行为。
- 不增加表或字段，不修改前端、交易所下单实现、Worker 调度或现有开仓权限。

## 测试结果

| 环境与范围 | 结果 |
|---|---|
| 本机后端：新工具、原接管、归属、生命周期、同步契约、历史 ext、Agent 契约 | 93 项通过 |
| MCP 包完整测试 | 72 项通过 |
| 独立 Linux 容器：上述后端范围及策略部署、周期和编译沙箱 | 101 项通过，1 项跳过 |

Linux 跳过项为 MCP 到 HTTP 的集成调用，该镜像未安装 MCP；该项已在本机后端测试中实际通过。本机策略编译遇到 macOS 内存限制接口不兼容，相关回归改在 Linux 原安全限制下通过，没有关闭沙箱检查。

MCP 环境有第三方 Pydantic lifespan 注解告警；临时 Linux 测试环境未装 pytest-timeout，产生既有 timeout 配置未识别告警。以上无测试失败。依赖仅补充到本机或临时容器 /tmp，没有修改项目依赖文件。

测试包括：W 独立接管、T 无 W 拒绝、OAuth write 映射、明确确认、数量以真实快照为准、同键重放与异参冲突、账户隔离、实际品种授权、现货、永续、剩余挂单、无持仓、非法止损、错误源码、重复归属和启动失败警告。交易所数据与策略持久化通过测试替身隔离，没有接触真实账户。

## 检查入口

- [调用参数与业务边界](MCP_POSITION_MANAGEMENT_CN.md)
- [Agent Gateway 路由](../../backend_api_python/app/routes/agent_v1/position_management.py)
- [MCP 工具](../../mcp_server/src/quantdinger_mcp/position_management.py)
- [Gateway 到共享服务及 MCP 集成测试](../../backend_api_python/tests/test_agent_position_management.py)

## 保持的边界

- 返回 `starting` 只代表启动命令入队；启动失败保留实例及警告，不能宣布管理已生效。
- 接管当前整仓不保证未成交挂单未来增量自动归属，也不自动撤单或改保护单。
- 开仓与接管仍为两个操作；状态不明先查询，不能盲目重开仓或重复创建。
- 幂等沿用现有 Gateway。极端崩溃发生在创建成功但幂等响应未落库之间时，需要先核实已有归属，未新增持久化编排或自动恢复系统。

开发使用 `/private/tmp/quantdinger-main-trade-history` 中现有 main 工作目录。原主目录与其他工作目录的开发分支未切换，未提交改动未处理。
