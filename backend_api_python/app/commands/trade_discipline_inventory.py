"""Compatibility entry point for the trade discipline command."""

from app.trade_discipline.inventory_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
