import json

import pytest

from mandate import cli

from .conftest import KEY


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MANDATE_SIGNING_KEY", KEY)
    monkeypatch.setenv("MANDATE_DB", str(tmp_path / "cli.db"))

    def _run(*argv):
        code = cli.main(list(argv))
        out = capsys.readouterr().out
        return code, json.loads(out)

    return _run


BUY = ["--category", "coffee", "--amount", "450", "--item", "Bella Donovan, 12 oz"]


def test_issue_allow_deny_revoke(run):
    code, issued = run(
        "issue", "--agent", "shopper", "--user", "nancy",
        "--merchant", "channel3:blue-bottle", "--category", "coffee",
        "--per-txn", "2500", "--period", "day", "--period-cap", "5000",
        "--max-txn", "2", "--ttl", "86400",
    )
    assert code == 0
    token, mid = issued["token"], issued["mandate"]["mandate_id"]

    code, r = run("authorize", "--token", token, "--merchant", "channel3:blue-bottle", *BUY)
    assert code == 0 and r["verdict"] == "ALLOW" and r["reason_code"] is None

    code, r = run("authorize", "--token", token, "--merchant", "channel3:evil-shop", *BUY)
    assert code == 2 and r["verdict"] == "DENY" and r["reason_code"] == "MERCHANT_NOT_ALLOWED"

    code, s = run("show", mid)
    assert code == 0 and s["spend"]["txn_count"] == 1

    code, r = run("revoke", mid)
    assert code == 0 and r["revoked"] is True

    code, r = run("authorize", "--token", token, "--merchant", "channel3:blue-bottle", *BUY)
    assert code == 2 and r["reason_code"] == "REVOKED"

    code, rows = run("ledger", "--limit", "10")
    assert [x["reason_code"] for x in rows] == ["REVOKED", "MERCHANT_NOT_ALLOWED", None]


def test_unknown_mandate(run):
    assert run("show", "mnd_nope")[0] == 1
    assert run("revoke", "mnd_nope")[0] == 1


def test_missing_key_is_refused(monkeypatch, tmp_path):
    monkeypatch.delenv("MANDATE_SIGNING_KEY", raising=False)
    monkeypatch.setenv("MANDATE_DB", str(tmp_path / "x.db"))
    with pytest.raises(SystemExit):
        cli.main(["authorize", "--token", "t", "--merchant", "channel3:blue-bottle", *BUY])
