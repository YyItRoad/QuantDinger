"""Persist generated marketplace preview images for chart indicators."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path


MAX_PREVIEW_BYTES = 5 * 1024 * 1024
PREVIEW_URL_PREFIX = "/api/indicator/preview-images"
_DATA_URL_RE = re.compile(
    r"^data:image/(?P<kind>jpeg|jpg|png|webp);base64,(?P<data>[A-Za-z0-9+/=\r\n]+)$",
    re.IGNORECASE,
)


def preview_image_root() -> Path:
    configured = str(os.getenv("INDICATOR_PREVIEW_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "storage" / "indicator_previews"


def _detect_image_extension(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp"
    raise ValueError("Unsupported preview image format")


def _decode_image_data_url(image_data: str) -> tuple[bytes, str]:
    value = str(image_data or "").strip()
    match = _DATA_URL_RE.fullmatch(value)
    if not match:
        raise ValueError("Invalid preview image data")
    try:
        payload = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid preview image encoding") from exc
    if not payload:
        raise ValueError("Preview image is empty")
    if len(payload) > MAX_PREVIEW_BYTES:
        raise ValueError("Preview image exceeds the 5 MB limit")
    extension = _detect_image_extension(payload)
    declared = match.group("kind").lower().replace("jpeg", "jpg")
    if declared != extension:
        raise ValueError("Preview image type does not match its content")
    return payload, extension


def save_indicator_preview(*, user_id: int, indicator_id: int, image_data: str) -> tuple[str, Path]:
    payload, extension = _decode_image_data_url(image_data)
    root = preview_image_root()
    root.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(payload).hexdigest()[:16]
    filename = f"u{int(user_id)}-i{int(indicator_id)}-{digest}.{extension}"
    destination = root / filename
    temporary = root / f".{filename}.tmp"
    temporary.write_bytes(payload)
    temporary.replace(destination)

    prefix = f"u{int(user_id)}-i{int(indicator_id)}-"
    for candidate in root.iterdir():
        if candidate.is_file() and candidate.name.startswith(prefix) and candidate.name != filename:
            try:
                candidate.unlink()
            except OSError:
                pass

    return f"{PREVIEW_URL_PREFIX}/{filename}", destination
