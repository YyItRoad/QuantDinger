"""Archive snapshots and bounded repairs preserve historical attribution."""

from contextlib import contextmanager
import json
import sqlite3

import pytest

from app.services.live_trading import position_management_history as history
from app.services.live_trading import position_management_history_ext as ext


@pytest.mark.parametrize("value", [None, 0, -1, True, "invalid", float("inf"), float("nan")])
def test_invalid_or_unset_price_is_not_invented(value):
    assert ext.history_ext_from_run({"params": {"fixed_stop_price": value}}, 3) == {}


def test_archived_snapshot_uses_run_not_current_strategy_and_excludes_secrets():
    source = {"params": {"fixed_stop_price": "0.325", "api_key": "private"},
              "exchange_config": {"secret": "private"}}
    row = history._history_row({"parameter_snapshot_json": json.dumps(source),
        "trade_strategy_run_id": 3, "strategy_params": {"fixed_stop_price": 99}},
        funding_total=0, funding_currency="USDT")
    assert row["ext"] == {"schema_version": 1, "params": {"fixed_stop_price": 0.325},
                          "snapshot_source": "strategy_run"}
    assert "private" not in json.dumps(row)
    assert ext.history_ext_from_run(source, 0) == {}
    source["params"]["fixed_stop_price"] = 55
    assert row["ext"]["params"]["fixed_stop_price"] == 0.325


def test_archive_insert_encodes_ext_as_json():
    calls = []
    class Cursor:
        def execute(self, sql, params):
            calls.append((sql, params))
    row = {key: None for key in history.HISTORY_COLUMNS}
    row["ext"] = {"params": {"fixed_stop_price": 0.325}}
    history._insert_history_rows(Cursor(), [row])
    stored = calls[0][1][history.HISTORY_COLUMNS.index("ext")]
    assert json.loads(stored) == row["ext"]


def test_bounded_backfill_is_scoped_preserves_ext_and_is_idempotent(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE strategy_runs (id INTEGER, strategy_id INTEGER, user_id INTEGER,
            credential_id INTEGER, market_type TEXT, parameter_snapshot_json TEXT);
        CREATE TABLE qd_position_management_trade_history (id INTEGER PRIMARY KEY,
            source_strategy_id INTEGER, user_id INTEGER, credential_id INTEGER,
            market_type TEXT, strategy_run_id INTEGER, ext TEXT);
    """)
    snap = json.dumps({"params": {"fixed_stop_price": 0.325}, "secret": "private"})
    db.executemany("INSERT INTO strategy_runs VALUES (?, ?, ?, ?, ?, ?)", [
        (3, 24, 7, 2, "swap", snap),
        (4, 24, 99, 2, "swap", snap),
        (5, 999, 7, 2, "swap", snap),
        (6, 24, 7, 99, "swap", snap),
        (7, 24, 7, 2, "spot", snap),
    ])
    for id_, run, existing, uid in [
        (1, 3, {"note": "keep", "params": {"future_option": 8}}, 7),
        (2, 4, {}, 7), (3, 5, {}, 7), (4, 6, {}, 7), (5, 7, {}, 7),
        (6, 999, {}, 7), (7, 3, {"params": {"fixed_stop_price": 0.3}}, 7),
        (8, 3, {}, 99),
    ]:
        db.execute("INSERT INTO qd_position_management_trade_history VALUES (?,24,?,2,'swap',?,?)",
                   (id_, uid, run, json.dumps(existing)))
    db.commit()

    class Cursor:
        rowcount = 0
        def execute(self, sql, params):
            self.cursor = db.execute(sql.replace("%s::jsonb", "?").replace("%s", "?"), params)
            self.rowcount = self.cursor.rowcount
        def fetchall(self):
            rows = [dict(row) for row in self.cursor.fetchall()]
            for row in rows:
                for key in ("ext", "parameter_snapshot_json"):
                    if row.get(key) is not None:
                        row[key] = json.loads(row[key])
            return rows
        def close(self):
            self.cursor.close()

    class Connection:
        def cursor(self):
            return Cursor()
        def commit(self):
            db.commit()
        def rollback(self):
            db.rollback()

    @contextmanager
    def connect():
        yield Connection()
    monkeypatch.setattr(ext, "get_db_connection", connect)
    try:
        preview = ext.backfill_history_ext(user_id=7, limit=2)
        assert (preview["scanned"], preview["eligible"], preview["updated"], preview["last_id"]) == (2, 1, 0, 2)
        assert "fixed_stop_price" not in db.execute("SELECT ext FROM qd_position_management_trade_history WHERE id=1").fetchone()[0]
        applied = ext.backfill_history_ext(user_id=7, apply=True)
        assert applied["eligible"] == applied["updated"] == 1
        saved = json.loads(db.execute("SELECT ext FROM qd_position_management_trade_history WHERE id=1").fetchone()[0])
        assert saved["note"] == "keep" and saved["params"] == {"future_option": 8, "fixed_stop_price": 0.325}
        assert "private" not in json.dumps(saved)
        assert ext.backfill_history_ext(user_id=7, apply=True)["updated"] == 0
        assert ext.backfill_history_ext(user_id=7, after_id=7)["scanned"] == 0

        # A concurrent writer must not lose its extension data to this repair.
        db.execute("UPDATE qd_position_management_trade_history SET ext='{}' WHERE id=1")
        db.commit()
        execute = Cursor.execute
        def race(self, sql, params):
            if sql.lstrip().startswith("UPDATE"):
                db.execute('UPDATE qd_position_management_trade_history SET ext=? WHERE id=1',
                           (json.dumps({"concurrent": "keep"}),))
            execute(self, sql, params)
        monkeypatch.setattr(Cursor, "execute", race)
        assert ext.backfill_history_ext(user_id=7, apply=True)["updated"] == 0
        assert json.loads(db.execute("SELECT ext FROM qd_position_management_trade_history WHERE id=1").fetchone()[0]) == {"concurrent": "keep"}
    finally:
        db.close()
