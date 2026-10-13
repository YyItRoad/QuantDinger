"""Fork notification-send endpoint; reuse Agent Gateway authentication."""
from app.utils.agent_auth import SCOPE_N, agent_required, current_user_id

from . import agent_v1_bp
from ._helpers import envelope, error, get_json_or_400


@agent_v1_bp.route("/notifications/send", methods=["POST"])
@agent_required(SCOPE_N)
def send_notification():
    from app.services.agent_notifications import send_notification as deliver, validate_notification

    body, err = get_json_or_400()
    if err:
        return err
    try:
        payload = validate_notification(body)
    except ValueError as exc:
        return error(400, str(exc), http=400)
    result, status = deliver(current_user_id(), payload)
    return envelope(result, code=0 if status == 200 else status, status=status)
