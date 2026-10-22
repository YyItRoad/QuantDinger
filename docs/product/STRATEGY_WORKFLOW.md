# From strategy idea to operation

The QuantDinger strategy lifecycle covers hypothesis, code, validation, backtest, versioning, deployment, and monitoring. Each stage is a separate checkpoint so that “it runs” is never treated as “it is ready for live trading.”

## 1. Define the contract

Before coding, define the market, instruments, frequency, entries, exits, position limits, costs, direction, and stop conditions. Declare direction capability for Crypto swaps. Design spot and currently supported non-Crypto strategies as long-only.

## 2. Implement in the strategy editor

Start from a Strategy API V2 template. Keep `initialize`, subscriptions, universe setup, and `handle_data` responsibilities clear. Use full market identifiers and never infer a venue or product from a display name.

An explicit venue is part of both the data and execution contract. For example, `Crypto:XAUT/USDT@okx:swap` reads the matching OKX product only; when that feed is unavailable the system reports a failure instead of silently switching exchanges. Research may use `Crypto:XAUT/USDT@swap` without pinning a venue, but a live deployment must select a real catalog product and venue.

See the [Strategy API V2 development guide](../trading/STRATEGY_DEV_GUIDE.md) for the programming surface, manifest, and sandbox rules.

## 3. Validate and version

Run source validation and resolve every error and warning. Record the strategy purpose and important parameters when saving. Create a new version when universe, frequency, sizing, or exit behavior changes so results remain comparable and reversible.

## 4. Backtest

Choose a range that includes warmup and multiple market regimes. Check coverage, benchmark, costs, slippage, funding, and liquidity constraints. Use the [Backtest Center guide](BACKTEST_CENTER.md) to review results.

## 5. Create a stopped deployment

Create deployments in the stopped state. Confirm that credential, market, settlement currency, and product type match. Use `signal` mode to validate notifications and state restoration. Do not mix incompatible markets or accounts in one deployment.

Changing from `signal` to `live` affects only entry orders created after the change. A signal order that was already queued is never promoted into a real order. Risk-reducing stop, take-profit, and close intents may still complete while the mode transition is taking place.

## 6. Start and monitor

Complete the [live-trading safety checklist](../trading/LIVE_TRADING_SAFETY.md). After startup, monitor runtime state, orders, position ownership, unknown deltas, notifications, and errors. Stop new entries first when behavior is uncertain; stop the strategy or use emergency stop when required.

## Release checklist

- Source and manifest validation pass.
- Backtest history and warmup are complete.
- Costs, slippage, funding, and minimum-order rules are understood.
- The credential matches the strategy market.
- Signal mode has been observed for an adequate period.
- Stop, recovery, and manual incident paths have been tested.
