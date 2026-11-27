# QuantDinger Repository Instructions

This is the canonical instruction file for coding agents working in this repository. Keep tool-specific entrypoints thin and do not duplicate these rules elsewhere.

## Repository map

- `backend_api_python/`: Flask API, services, trading runtime, migrations, and backend tests.
- `mcp_server/`: the tenant-scoped MCP client/server package backed by QuantDinger APIs.
- `docs/`: product, architecture, deployment, API, Agent Gateway, and trading documentation.
- `docker-compose*.yml` and `scripts/`: deployment topology and lifecycle tooling.
- Frontend source is maintained outside this repository. The prebuilt frontend image is configured by the `frontend` service in `docker-compose.yml`.

## Read by task

Read only the documentation relevant to the change:

- Agent Gateway or MCP: [Agent documentation](docs/agent/README.md), [Agent OpenAPI](docs/agent/agent-openapi.json), and [MCP package documentation](mcp_server/README.md).
- Strategy API, backtests, or trading behavior: [Strategy development guide](docs/trading/STRATEGY_DEV_GUIDE.md) and [live-trading safety](docs/trading/LIVE_TRADING_SAFETY.md).
- Runtime ownership, concurrency, Kafka, workers, or durable state: [architecture index](docs/architecture/README.md) and the task-specific document it links.
- HTTP contracts: [API conventions](docs/architecture/API_CONVENTIONS.md) and the applicable OpenAPI document.
- Installation or operations: the root [README](README.md) and the applicable guide under `docs/deployment/`.

## Architecture and contract invariants

- Preserve the process ownership documented in the architecture index. Do not move persistent trading loops into the HTTP backend or let evaluator processes submit exchange orders directly.
- PostgreSQL is the durable source of truth. Kafka transports versioned events; Redis cache data is evictable, while the jobs Redis has a separate durability role. Do not silently substitute one for another.
- Keep tenant isolation, idempotency, leases, fencing, and audit behavior intact when changing distributed or trading workflows.
- `/api/agent/v1` is the Agent Gateway boundary. Update `docs/agent/agent-openapi.json` and its alignment tests whenever that HTTP contract changes.
- MCP capabilities must retain the same authentication, scopes, idempotency, limits, and live-trading safeguards as the backing API.

## Safety and repository hygiene

- Never commit real secrets, production `.env` files, API keys, or database passwords. Use `.env.example` and placeholders.
- Do not weaken live-trading safeguards or bypass explicit authorization and human review unless the user requests and scopes that change.
- Do not add upgrade-time data rewrites for one-off local cleanup. Use an explicit, reviewed migration only when shipped user data must change.
- Preserve unrelated working-tree changes.
- Keep user-facing application copy in the existing localization system rather than hardcoding it in source files.
- Keep machine-readable contracts and identifiers in English. When a human guide has English and `_CN` editions, update both when the change affects both audiences.

## 业务代码开发前置条件

新增功能、修改业务行为、调整架构或重构跨模块调用前，必须先调研现有系统，并在 `docs/` 下写出中文 Markdown 设计文档。以下四项齐全且由需求方确认后，才能开始写业务代码。

1. **需求与验收**。写清使用场景、触发条件、业务规则、异常分支和功能边界。区分已确认需求、待核实假设和待决定问题。每个步骤都要有可独立验证的输入、预期结果和验收方式。
2. **架构图与调用流程**。标出实际入口、运行进程、模块职责、调用顺序、数据来源及写入点。跨进程或涉及关键分支时，用流程图或时序图标明分支去向、进程间交接和失败后的恢复路径。
3. **详细设计与复用清单**。逐项核对现有代码，列出可直接复用的服务和函数、确需调整的接入点、计划删除的旧逻辑，以及新增部分的必要性。涉及配置、权限、交易或持久化时，说明默认值、作用范围、执行权限、幂等与回查方式。
4. **关键流程的伪代码**。按实际调用顺序写出输入、分支条件、返回结果和副作用。伪代码、架构图和验收项应能相互对照。

审核前先完成只读调研和关键假设的核实。涉及交易所、订单归属、权限或其他影响执行的证据缺口，先验证或明确标为待决，再进入实现。需求有变化时先更新受影响的设计与验收项并重新确认，再继续按可独立验收的步骤实施。

## 自研模块的中文注释

- 本仓库自研业务模块必须添加详细中文源码注释。现有交易纪律和交易计划模块、新增自研模块，以及 Scheduler、Trading Worker、网页和 Agent API 中接入这些模块的关键调用点都适用。
- 模块入口必须说明触发方、运行进程和后续调用。关键业务分支必须解释判断依据和先后顺序、数据来源和时间口径、通过或跳过的条件，以及证据不足或调用失败时如何处理。涉及写库、下单、接管和重试时，必须说明执行边界、重启后的恢复方式和幂等依据。
- 改动业务流程时同步更新相关注释和中文 Markdown 导读。注释应解释代码为何这样判定并帮助定位断点，命名和逐行翻译可以省略。沿用的上游代码继续遵循原有注释风格，接口字段、日志事件和机器可读标识继续使用英文。
- 交易纪律的进程入口、调用顺序、分支及 VS Code 调试位置见[交易纪律代码导读](docs/trading/交易纪律代码导读.md)。

## Verification

- Run focused backend tests from `backend_api_python/` with `python -m pytest tests/<test_file>.py -q`.
- Agent contract coverage lives in `backend_api_python/tests/test_ai_agent_contract_alignment.py` and related `test_agent_*.py` files.
- MCP tests live in `mcp_server/tests/`.
- Validate Compose changes with `docker compose config --quiet` before exercising the affected services.
- Match verification depth to risk; trading, migrations, tenancy, and distributed ownership require targeted regression tests.
