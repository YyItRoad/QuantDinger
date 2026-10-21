"""Preview or explicitly apply managed history configuration snapshot repairs."""

import argparse
import json

from app.services.live_trading.position_management_history_ext import backfill_history_ext


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(backfill_history_ext(user_id=args.user_id, apply=args.apply,
        after_id=args.after_id, limit=args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
