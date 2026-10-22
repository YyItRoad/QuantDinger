"""Whole-position management adapters using the shared Gateway transport."""
from typing import Annotated, Any, Literal

from pydantic import Field

PositiveId = Annotated[int, Field(gt=0, strict=True)]


def register_position_management_tools(server):
    @server._tool
    def list_managed_positions(credential_id: PositiveId) -> Any:
        """Read an owned account's strategy position assignments (scope R).

        Combine with get_account_snapshot to find unmanaged exchange positions.
        """
        return server._get(f"/api/agent/v1/trading/accounts/{credential_id}/managed-positions")

    @server._tool
    def adopt_position_management(
        credential_id: PositiveId,
        symbol: str,
        market_type: Literal["spot", "swap"],
        side: Literal["long", "short"],
        source_id: PositiveId,
        name: str,
        initial_capital: Annotated[float, Field(gt=0, allow_inf_nan=False, strict=True)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120)],
        confirm_adoption: Annotated[bool, Field(strict=True)] = False,
        inst_id: str | None = None,
        timeframe: str | None = None,
        params: dict[str, Any] | None = None,
        entry_reason: Annotated[str, Field(max_length=500)] | None = None,
        direction_mode: str | None = None,
        account_risk: dict[str, Any] | None = None,
        notification_channels: list[str] | None = None,
        notification_targets: dict[str, Any] | None = None,
    ) -> Any:
        """Adopt an existing entire exchange position and start management (W).

        Requires explicit approval of the whole position and confirm_adoption=true.
        Reuses the Web workflow: refresh actual size/prices/leverage, create the
        strategy, register the whole leg and queue its start. Never opens a new
        position or cancels pending orders. Existing manual holdings are included.
        After startup, the selected source controls subsequent strategy orders.
        Use params.fixed_stop_price for a fixed stop; source parameters and
        timeframe rules remain authoritative. Reuse the key to recover an unknown
        outcome; completed errors also replay unchanged. Only a Gateway error
        explicitly reporting details.retry_safe=true permits a new key after
        refreshing assignments. Otherwise inspect the existing strategy first.
        Transport timeouts alone never prove a new key is safe. Startup warnings
        require recovery of the returned instance, not another adoption.
        starting is not proof of a running Worker; inspect returned warnings.
        W authorizes adoption and startup, independent of T quick-order controls.
        """
        if confirm_adoption is not True:
            return {"error": True, "status": 400, "body": {
                "message": "confirm_adoption=true is required after explicit whole-position approval",
            }}
        payload = dict(symbol=symbol, market_type=market_type, side=side,
                       source_id=source_id, name=name, initial_capital=initial_capital,
                       confirm_adoption=True)
        for key, value in (("inst_id", inst_id), ("timeframe", timeframe), ("params", params),
                           ("entry_reason", entry_reason), ("direction_mode", direction_mode),
                           ("account_risk", account_risk), ("notification_channels", notification_channels),
                           ("notification_targets", notification_targets)):
            if value is not None:
                payload[key] = value
        return server._post(
            f"/api/agent/v1/trading/accounts/{credential_id}/managed-strategies",
            json=payload, headers=server._idempotency_headers(idempotency_key),
        )

    return list_managed_positions, adopt_position_management
