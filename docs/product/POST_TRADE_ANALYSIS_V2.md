# Strategy post-trade analysis V2 / 策略复盘 V2

## Boundary / 功能边界

The existing authenticated review and history endpoints return an additive
`schema_version: 2` report. No execution-engine, order, deployment, or strategy
parameter changes are performed. Existing history remains readable. AI failures
retain the deterministic evidence report and the existing billing refund path.

新版仅重构复盘报告，不改变执行引擎、订单、部署或策略参数。旧历史报告仍可读取。
AI 失败时保留证据报告，并沿用现有计费退款流程。

## Evidence / 证据口径

- `evidence.facts`: deterministic ledger statistics. Profit factor and average
  win/loss ratio are separate; undefined values are null, not invented zeroes.
- Commission add-back is a same-window accounting identity, **not** a hypothetical
  execution backtest. Missing currency conversion or mixed quote currencies block
  this decomposition. Other ledger statistics still require reconciliation.
- Close rows are fills, not necessarily complete trades. FIFO holding time includes
  pre-window entries, separates symbols and directions, and excludes unmatched
  closes. Five signal bars are a descriptive bucket, not a recommended holding rule.
- Selected evidence contains the worst ten closes and latest twenty fills, with
  duplicates removed. It is not a random or representative sample.
- Current source versions are read with the user's ownership scope. Static AST
  inspection never executes strategy code. String literals are redacted and only
  numeric/boolean parameters are included. Raw logs and exchange configuration are
  not sent to the AI. Current source is not proof of historical version continuity.
- Missing market paths, funding, execution costs, and historical version linkage
  are displayed as evidence gaps rather than silently inferred.

核心原则：区分窗口账面盈亏、完整交易与平仓成交、利润因子与平均盈亏比。
手续费加回不是零成本回测；短持仓亏损也不能证明延长持仓有效。
当前代码版本仅用于提出假设，不冒充整个复盘窗口的历史版本。

## Suggestions / 改进建议

The service exposes four setting-oriented `evidence.checks`: costs, frequency,
exits and position risk. AI may annotate only these check codes with an
interpretation, one prioritized adjustment, and a guardrail. The browser presents
each as a direct problem-and-optimization answer, without a downloadable plan or
experiment-status badge. Unsupported numeric thresholds are prohibited by the
prompt; missing evidence calls for a specific measurement, not a fabricated
optimal parameter. Suggestions do not create strategy copies, launch backtests,
or deploy changes. AI prose is untrusted and rendered as plain text.

新版围绕费用、交易频次、止盈止损、仓位杠杆四项诊断，直接回答“存在什么问题”和“怎么优化”，附证据与执行边界。
移除下载计划及实验状态展示；不创建策略副本、不运行回测、不修改实盘参数。
缺少依据时明确需要补齐的测量数据，不编造最佳参数，也不将建议冒充已验证的改善结果。

Three signed, zero-anchored charts show commission accounting, exit-reason net
P&L, and FIFO-matched holding-duration net P&L. Cohorts use the full evidence
population rather than selected example fills. Count and P&L coverage remain
visible; unknown amounts are omitted, not plotted as zero. Holding buckets are
descriptive durations (up to 5 minutes, 5–30 minutes, 30–120 minutes, over 2 hours),
not recommended holding limits. These views overlap and must not be summed as
independent causes of loss. Historical reports without cohort data omit those
charts until a new report is generated.

三类图表分别展示费用拆分、退出原因分组盈亏、已配对持仓时长分组盈亏。
使用完整分组而非抽样成交，展示金额覆盖笔数；未知金额不按零处理。
分组仅用于定位问题，不能叠加为独立亏损原因，也不能直接推导最优持仓时间。
旧报告缺少分组数据时不展示对应图表，需重新生成报告才能补齐。

Exit cohorts are mutually exclusive close-fill classifications based on explicit
reason codes, with unrecognized or absent reasons left unknown. Their net P&L
already includes allocated fees; it must not be added to fees as a separate causal
contribution. `pnl_count` discloses incomplete P&L coverage. Current raw settings
are not interpreted as effective historical thresholds. Configured leverage is
not effective leverage; largest close loss / initial capital is not risk at entry.
Calendar-day fill rates include inactive days and are not independent signal counts.
Missing data is an insufficient-evidence state, never a passing score.

退出分组用于定位亏损，不证明止损太紧或止盈太早，也不能与手续费相加进行因果归因。
缺少止损参数不等于没有保护；当前配置杠杆不等于历史实际敞口。
日均值包含窗口内未运行日期；分批成交不等于完整交易。强平成交优先展示风险检查。

## Validation / 验证

Backend: run the focused `test_strategy_review_evidence.py`,
`test_strategy_review_execution_mode.py`, and `test_strategy_review_billing.py`
tests from `backend_api_python` using the project environment.

Frontend: `node --test tests/unit/strategyReview.test.mjs`.
`node tests/preview-strategy-review.mjs` starts an isolated loopback-only preview
on port 5197, with fake APIs and fixture data, without live trading or AI calls.
Preview URL: `/tests/fixtures/strategy-review.html`; optional query parameters
`theme=light` and `lang=en-US` exercise responsive/localized rendering.

测试预览全部使用样例数据，不连接交易账户，也不调用付费 AI。
更新正式环境仍需按原有流程部署后端与前端构建；旧报告不会自动重算。
