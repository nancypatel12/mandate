"""The red-team suite runs with the unit tests, offline (no money moves)."""

import importlib.util
from pathlib import Path

RUN = Path(__file__).resolve().parents[3] / "redteam" / "run.py"


def test_every_redteam_attack_is_stopped(capsys):
    spec = importlib.util.spec_from_file_location("redteam_run", RUN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main([]) == 0, capsys.readouterr().out
    assert "14/14 PASS" in capsys.readouterr().out
