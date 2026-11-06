import sqlite3

import pytest

from mandate import ledger
from mandate.models import ReasonCode, Verdict

from .conftest import NOW, intent


def test_append_and_page_newest_first(conn):
    a = ledger.append(conn, Verdict.allow("ok"), NOW, mandate_id="mnd_1", intent=intent())
    b = ledger.append(conn, Verdict.deny(ReasonCode.REVOKED, "no"), NOW, mandate_id="mnd_1", intent=intent())
    rows = ledger.page(conn)
    assert [r["ledger_id"] for r in rows] == [b, a]
    assert rows[0]["verdict"] == "DENY" and rows[0]["reason_code"] == "REVOKED"
    assert rows[1]["verdict"] == "ALLOW" and rows[1]["reason_code"] is None


def test_pagination(conn):
    ids = [ledger.append(conn, Verdict.allow("ok"), NOW) for _ in range(5)]
    first = ledger.page(conn, limit=2)
    assert [r["ledger_id"] for r in first] == [ids[4], ids[3]]
    second = ledger.page(conn, limit=2, before=first[-1]["seq"])
    assert [r["ledger_id"] for r in second] == [ids[2], ids[1]]


def test_get(conn):
    lid = ledger.append(conn, Verdict.allow("ok"), NOW, order_id="ORDER1")
    assert ledger.get(conn, lid)["order_id"] == "ORDER1"
    assert ledger.get(conn, "led_missing") is None


def test_update_is_blocked_by_the_database(conn):
    ledger.append(conn, Verdict.deny(ReasonCode.REVOKED, "no"), NOW)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger SET verdict='ALLOW', reason_code=NULL")


def test_delete_is_blocked_by_the_database(conn):
    ledger.append(conn, Verdict.deny(ReasonCode.REVOKED, "no"), NOW)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger")


def test_allow_with_reason_is_impossible(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ledger(ledger_id, ts, verdict, reason_code, message) "
            "VALUES ('led_x', 't', 'ALLOW', 'REVOKED', 'm')"
        )
