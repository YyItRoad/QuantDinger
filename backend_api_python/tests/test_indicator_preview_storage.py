from __future__ import annotations

import base64

import pytest

from app.services.indicator_preview_storage import save_indicator_preview


def _jpeg_data_url(payload: bytes) -> str:
    encoded = base64.b64encode(b"\xff\xd8\xff" + payload).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def test_save_indicator_preview_uses_dedicated_directory(monkeypatch, tmp_path):
    preview_dir = tmp_path / "indicator_previews"
    monkeypatch.setenv("INDICATOR_PREVIEW_DIR", str(preview_dir))

    url, destination = save_indicator_preview(
        user_id=7,
        indicator_id=19,
        image_data=_jpeg_data_url(b"first"),
    )

    assert destination.parent == preview_dir
    assert destination.exists()
    assert url == f"/api/indicator/preview-images/{destination.name}"


def test_save_indicator_preview_replaces_older_image(monkeypatch, tmp_path):
    preview_dir = tmp_path / "indicator_previews"
    monkeypatch.setenv("INDICATOR_PREVIEW_DIR", str(preview_dir))

    _, first = save_indicator_preview(
        user_id=7,
        indicator_id=19,
        image_data=_jpeg_data_url(b"first"),
    )
    _, second = save_indicator_preview(
        user_id=7,
        indicator_id=19,
        image_data=_jpeg_data_url(b"second"),
    )

    assert not first.exists()
    assert second.exists()


def test_save_indicator_preview_rejects_non_image_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("INDICATOR_PREVIEW_DIR", str(tmp_path))
    payload = base64.b64encode(b"not-an-image").decode("ascii")

    with pytest.raises(ValueError, match="Unsupported preview image format"):
        save_indicator_preview(
            user_id=7,
            indicator_id=19,
            image_data=f"data:image/jpeg;base64,{payload}",
        )
