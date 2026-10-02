from __future__ import annotations

from datetime import date, datetime

import pytest

from daylog import export, health
from daylog.vault import Vault

DAY = date(2026, 10, 2)


def test_parse_keeps_numbers_and_normalises_keys() -> None:
    day, metrics = health.parse(
        b'{"date": "2026-10-01", "Steps": "8,423", "sleep_h": 7.25, "note": "hi", "ok": true}', DAY
    )
    assert day == date(2026, 10, 1)
    assert metrics == {"steps": 8423.0, "sleep_h": 7.25}


def test_parse_defaults_the_day_and_rejects_bad_bodies() -> None:
    assert health.parse(b'{"steps": 10}', DAY)[0] == DAY
    for body in (b"nope", b"[1]", b'{"note": "x"}', b'{"date": "soon", "steps": 1}'):
        with pytest.raises(health.HealthError):
            health.parse(body, DAY)
    with pytest.raises(health.HealthError):
        health.parse(b'{"steps": 1, "pad": "' + b"x" * health.MAX_BODY_BYTES + b'"}', DAY)


def test_merge_updates_one_day_and_keeps_the_rest() -> None:
    stored = health.merge({"2026-10-01": {"steps": 5.0}}, DAY, {"steps": 100.0})
    stored = health.merge(stored, DAY, {"sleep_h": 7.0, "steps": 120.0})
    assert stored == {"2026-10-01": {"steps": 5.0}, "2026-10-02": {"steps": 120.0, "sleep_h": 7.0}}
    assert health.for_day(stored, DAY) == {"steps": 120.0, "sleep_h": 7.0}
    assert health.for_day(stored, date(2026, 9, 1)) == {}


def test_round_trips_through_the_vault_into_the_export(vault: Vault) -> None:
    vault.write_yaml(health.FILE, health.merge({}, DAY, {"steps": 9000.0}), "health")
    vault.write_journal_entry(
        datetime(2026, 10, 2, 9, 0), {"activities": [{"type": "surf"}]}, "t", "s"
    )
    rows = export.day_rows(
        vault.read_journal_range(DAY, DAY), None, vault.read_yaml(health.FILE, {})
    )
    assert rows[0]["health_steps"] == 9000.0
    assert "health_steps" in export.to_csv(rows, export.DAY_COLUMNS).splitlines()[0]
