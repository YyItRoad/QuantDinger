"""Fork notification tool; reuse the server's transport and safety wrapper."""
from typing import Annotated, Any, Literal

from pydantic import Field


def register_notification_tools(server):
    @server._tool
    def send_notification(
        title: Annotated[str, Field(min_length=1, max_length=200, strict=True)],
        message: Annotated[str, Field(min_length=1, max_length=8000, strict=True)],
        idempotency_key: Annotated[str, Field(min_length=1, max_length=120, strict=True)],
        level: Literal["info", "warning", "critical"] = "info",
    ) -> Any:
        """Send via the user's saved Webhook (N / quantdinger.write).

        Reuse the key for retries; the Agent Gateway replays the original response.
        No destination or credentials can be supplied by the caller.
        """
        return server._post(
            "/api/agent/v1/notifications/send",
            json={"title": title, "message": message, "level": level},
            headers=server._idempotency_headers(idempotency_key),
        )

    return send_notification
