"""Check one historical EMA25 discipline decision using public market data."""

import argparse
import json

from app.trade_discipline.ema import check_ema25_at_order_time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("Crypto", "CNStock", "HKStock", "USStock"), required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--direction", choices=("long", "short"), required=True)
    parser.add_argument("--submitted-at", required=True, help="ISO 8601 order submission time with timezone.")
    parser.add_argument("--exchange-id", default="")
    parser.add_argument("--market-type", choices=("spot", "swap"), default="")
    result = check_ema25_at_order_time(**vars(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
