"""Asynchronous marketplace metadata translation tasks."""

from __future__ import annotations

import json

from app.celery_app import celery_app


@celery_app.task(
    name="quantdinger.tasks.indicator_translation",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def translate_indicator_metadata(
    indicator_id: int,
    user_id: int,
    name: str,
    description: str,
    source_language: str | None = None,
) -> None:
    from app.services.indicator_translator import translate_indicator
    from app.utils.db import get_db_connection

    name_i18n, description_i18n, resolved_language = translate_indicator(
        name=name,
        description=description,
        source_language=source_language,
    )

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_indicator_codes
            SET source_language = ?,
                name_i18n = ?,
                description_i18n = ?,
                updated_at = NOW()
            WHERE id = ? AND user_id = ? AND name = ? AND description = ?
            """,
            (
                resolved_language,
                json.dumps(name_i18n, ensure_ascii=False) if name_i18n else None,
                json.dumps(description_i18n, ensure_ascii=False) if description_i18n else None,
                int(indicator_id),
                int(user_id),
                name,
                description,
            ),
        )
        db.commit()
        cur.close()
