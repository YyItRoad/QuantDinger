"""Print a one-shot read-only inventory for a trading-discipline account."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal

from app.services.live_trading.position_management import (
    read_account_position_inventory, read_user_position_inventory,
)


def _monitor_candidate(position):
    if position["management_status"] == "unknown":
        return "management_unknown"
    registered = [row for row in position.get("registrations", []) if row.get("execution_mode") == "live"]
    if registered:
        allocated = sum(Decimal(row["size"]) for row in registered)
        account_size = Decimal(str(position["size"]))
        if account_size > allocated:
            return "unallocated_surplus"
        return "managed" if account_size == allocated else "allocation_shortfall"
    if position["management_status"] == "registered":
        return "management_unknown"
    return "opening_origin_unverified"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the configured account's positions and management registrations as JSON.",
        epilog="Set TRADE_DISCIPLINE_USER_ID, TRADE_DISCIPLINE_CREDENTIAL_ID and optionally "
        "TRADE_DISCIPLINE_MARKET_TYPE (swap, spot or all; default swap).",
    )
    parser.add_argument("--all-accounts", action="store_true",
                        help="Read the user's saved accounts and classify coverage without taking actions.")
    parser.add_argument("--evidence-symbol", help="Reconcile read-only Binance linear perpetual fills for one instrument.")
    parser.add_argument("--check-plan", action="store_true", help="Check plans at the evidenced order submission times.")
    parser.add_argument("--check-ema", action="store_true", help="Check EMA25 at the evidenced order submission times.")
    args = parser.parse_args()
    if args.all_accounts:
        if args.evidence_symbol or args.check_plan or args.check_ema:
            parser.error("--all-accounts cannot be combined with individual order checks")
        try:
            user_id = int(os.getenv("TRADE_DISCIPLINE_USER_ID", ""))
            if user_id <= 0:
                raise ValueError()
        except ValueError:
            parser.error("TRADE_DISCIPLINE_USER_ID must be a positive integer")
        accounts = read_user_position_inventory(user_id=user_id)
        for account in accounts:
            account["discipline_coverage"] = (
                "binance_linear_perpetual" if account["exchange_id"] in {"binance", "binanceusdm", "binancefutures"}
                else "inventory_only"
            )
            for position in account["positions"]:
                position["monitor_candidate"] = _monitor_candidate(position)
        print(json.dumps({"user_id": user_id, "accounts": accounts}, ensure_ascii=False, indent=2))
        return int(any(account["status"] != "ok" for account in accounts))
    if (args.check_plan or args.check_ema) and not args.evidence_symbol:
        parser.error("Order checks require --evidence-symbol")
    try:
        user_id = int(os.getenv("TRADE_DISCIPLINE_USER_ID", ""))
        credential_id = int(os.getenv("TRADE_DISCIPLINE_CREDENTIAL_ID", ""))
    except ValueError:
        parser.error("TRADE_DISCIPLINE_USER_ID and TRADE_DISCIPLINE_CREDENTIAL_ID must be positive integers")
    lookback_hours = None
    if args.check_plan and (lookback := os.getenv("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS")):
        try:
            lookback_hours = int(lookback)
            if lookback_hours <= 0:
                raise ValueError()
        except ValueError:
            parser.error("TRADE_DISCIPLINE_PLAN_LOOKBACK_HOURS must be a positive integer")
    market_type = os.getenv("TRADE_DISCIPLINE_MARKET_TYPE", "swap").strip().lower()
    try:
        inventory = read_account_position_inventory(
            user_id=user_id, credential_id=credential_id, market_type=market_type,
        )
        if args.evidence_symbol:
            from app.trade_discipline.position_evidence import read_position_evidence

            inventory["evidence"] = read_position_evidence(inventory, args.evidence_symbol)
        if args.check_plan:
            from app.trade_discipline.observation import check_plans

            inventory["plan_checks"] = check_plans(inventory, lookback_hours)
        if args.check_ema:
            from app.trade_discipline.observation import check_ema

            inventory["ema_checks"] = check_ema(inventory)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(inventory, ensure_ascii=False, indent=2))
    evidence = inventory.get("evidence")
    if (inventory["status"] != "ok" or (evidence and evidence["status"] != "reconciled")
            or any(check["status"] == "unavailable"
                   for checks in ("plan_checks", "ema_checks") for check in inventory.get(checks, []))):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
