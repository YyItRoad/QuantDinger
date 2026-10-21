"""Account-scoped archive tools sharing the server transport and guards."""
from typing import Annotated, Any, Literal
from pydantic import Field

PositiveId = Annotated[int, Field(gt=0, strict=True)]


def register_position_management_history_tools(server):
    @server._tool
    def list_position_management_trade_history(
        credential_id: PositiveId,
        page: Annotated[int, Field(gt=0, strict=True)] = 1,
        page_size: Annotated[int, Field(gt=0, le=100, strict=True)] = 20,
        symbol: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
        market_type: Literal["spot", "swap", "futures"] | None = None,
        position_side: Literal["long", "short"] | None = None,
        source_strategy_id: PositiveId | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> Any:
        """Read one account's archived position-management fills (scope R).

        Obtain credential_id from list_trading_accounts (its id field).
        This is an archive of stopped, flat strategies, not all exchange trades.
        symbol is an exact canonical symbol. Time bounds require ISO 8601 with
        timezone, filter exit_time, include start_time and exclude end_time.
        entry_time reflects strategy creation, not necessarily the entry fill.
        Fee status must be checked before treating missing fees as zero.
        ext.params.fixed_stop_price is the saved run setting when available.
        """
        params = {"credential_id": credential_id, "page": page, "page_size": page_size}
        for key, value in (("symbol", symbol), ("market_type", market_type),
                           ("position_side", position_side),
                           ("source_strategy_id", source_strategy_id),
                           ("start_time", start_time), ("end_time", end_time)):
            if value is not None:
                params[key] = value
        return server._get("/api/agent/v1/trading/position-management/trade-history", params=params)

    @server._tool
    def get_position_management_trade_history(credential_id: PositiveId, history_id: PositiveId) -> Any:
        """Read an archived fill by account and history id (scope R).

        Includes saved entry/exit reasons, fee status and order references.
        ext holds public configuration snapshots; absent settings are unknown.
        Deleted source strategies/accounts do not invalidate owned archives.
        This read neither synchronizes trades nor edits reasons. entry_time is
        strategy creation time; profit_rate is a percentage using existing
        leverage semantics, not necessarily a net return after all fees.
        """
        return server._get(
            f"/api/agent/v1/trading/position-management/trade-history/{history_id}",
            params={"credential_id": credential_id},
        )

    return list_position_management_trade_history, get_position_management_trade_history
