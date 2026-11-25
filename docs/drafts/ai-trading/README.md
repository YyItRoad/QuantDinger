# 意图交易：暂停开发

状态：**2026-10-26 按用户要求暂停开发**。近期先使用和验证已上线的“计划”模块；未来是否继续意图交易，需用户重新明确决定。暂停不是等待自动续做的任务，不安排后续开发、模型调用或上线。

## 当前分支与归档

- 后端保留分支：`codex/ai-intent-shadow-v1`；暂停前业务代码提交 `ff1226c`。
- 前端保留分支：`codex/ai-intent-shadow-v1`；业务代码提交 `9e8a829`。
- 两仓库本地恢复标签：`archive/ai-intent-shadow-v1-2026-10-26`。归档仅保存在本机 Git，未推送这套原型或部署生产。
- 日常开发目录切回各自最新 `main`。main 不包含意图原型；本说明不是产品可用性证明。现有计划、市场分析和持仓管理继续按其各自文档使用。

归档保留源码、测试、迁移和必要的设计状态；旧阶段报告、演示轨迹、重复方案已从当前文档目录移除，原已提交版本仍可通过 Git 历史追溯。

## 需要恢复时

先由用户明确决定范围，再核对当前 main 与归档差异。可先只读：

```bash
git show archive/ai-intent-shadow-v1-2026-10-26:docs/drafts/ai-trading/需求与边界.md
git show archive/ai-intent-shadow-v1-2026-10-26:docs/drafts/ai-trading/实现与验证现状.md
git log --oneline main..codex/ai-intent-shadow-v1
```

前端从其同名本地标签读取 `src/views/strategy-center/`、`src/utils/intentShadow.js` 及对应测试。不要把整个旧分支直接合入 main；先评估哪些观点、版本、分析引用和复盘能力可以复用已上线的“计划”模块，这一步尚未设计或实现。

旧测试结论仅说明当时的隔离原型，不构成当前生产验证、继续开发、迁移、付费模型调用或真实交易的授权。

## 其他未提交改动的恢复点

切换 main 前，与意图归档分开的未提交内容已保存在本地 Git stash，并逐文件校验原始内容。主要是计划开发期间的混合改动；当前计划代码以 main 为准，勿整包恢复覆盖主线。

- `QuantDinger`：`7979471d7323d27422f1de1b112ba55ca237ebde`，校验 55 个文件。
- `QuantDinger-Vue`：`5550967b98c25e10e525e5383452decba96d1fdf`，校验 17 个文件。

这些恢复点与本地归档标签均未推送。原有 stash、其他工作目录及无关文件保持原样。
