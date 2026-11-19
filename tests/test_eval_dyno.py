from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gearbox.eval import dyno

DATA = Path(__file__).resolve().parents[1] / "data" / "v2"


def test_sample_is_stratified_and_repeatable() -> None:
    ids = dyno.sample_ids(DATA, 30)
    assert ids == dyno.sample_ids(DATA, 30) and len(set(ids)) == 30
    prompts = dyno._prompts(DATA)
    tasks = Counter(prompts[i]["task_type"] for i in ids)
    assert set(tasks) == {"coding", "reasoning", "research", "writing"}
    assert max(tasks.values()) - min(tasks.values()) <= 1
    test = set(json.loads((DATA / "split.json").read_text(encoding="utf-8"))["test"])
    assert set(ids) <= test  # held-out prompts only


def test_cases_have_keys_except_for_code(tmp_path: Path) -> None:
    ids = dyno.sample_ids(DATA, 119)
    cases = dyno.build_cases(DATA, ids)
    prompts = dyno._prompts(DATA)
    for case in cases:
        row = prompts[case["id"]]
        assert set(case) <= {"id", "input", "tags", "expected"}
        if row["task_type"] == "coding":
            assert "expected" not in case
        elif row.get("reference_kind") == "gold" and row.get("reference"):
            assert case["expected"] == str(row["reference"])
        else:
            assert "expected" not in case
    path = tmp_path / "cases.yaml"
    dyno.write_cases(cases, path)
    assert len(yaml.safe_load(path.read_text(encoding="utf-8"))["cases"]) == 119


def test_suite_file_names_dynos_gearbox_agent() -> None:
    suite = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "eval/dyno_suite.yaml").read_text()
    )
    assert suite["agent"] == "gearbox" and "quality" in suite["scorers"]


def test_dry_run_and_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    out = tmp_path / "c.yaml"
    assert dyno.main(["run", "--n", "10", "--out", str(out)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert dyno.main(["run", "--n", "30", "--cap-gbp", "0.01", "--out", str(out)]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_missing_dyno_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(dyno.DynoError, match="DYNO_BIN"):
        dyno.run_dyno(tmp_path / "nope.exe", tmp_path / "c.yaml", tmp_path / "d.db", 0.1)


def test_run_reads_dynos_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exe = tmp_path / "dyno.exe"
    exe.write_text("")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="run abc123def456: x\n", stderr="")

    monkeypatch.setattr(dyno.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from gearbox.config import get_secrets

    get_secrets.cache_clear()
    try:
        assert dyno.run_dyno(exe, tmp_path / "c.yaml", tmp_path / "d.db", 0.25) == "abc123def456"
    finally:
        get_secrets.cache_clear()
    cmd, env = calls[0]
    assert cmd[1:3] == ["run", str(dyno.SUITE)] and "--cases" in cmd
    assert env["JUDGE_BUDGET_GBP"] == "0.2500" and env["ANTHROPIC_API_KEY"] == "sk-test"


def test_summary_in_gearbox_terms() -> None:
    ds = dyno.load_dataset(DATA)
    ids = dyno.sample_ids(DATA, 4)
    picks = {pid: "M" for pid in ids}
    stored = [ds.scores[(pid, "M")] >= 4 for pid in ids]
    # Dyno fails exactly the first prompt on quality.
    report = {
        "run_id": "r1",
        "cost_gbp": 0.002,
        "judge_cost_gbp": 0.01,
        "scorers": [{"scorer": "quality", "scored": 4, "pass_rate": 0.75}],
        "failures": [{"case_id": ids[0], "scorer": "quality", "explanation": "x"}],
    }
    rows = [{"tier": "M", "cost_gbp": 0.0005, "status": "ok", "router": "v3-embed"}] * 4
    s = dyno.summarise(report, ids, rows, DATA, picks)
    assert s["tiers"] == {"M": 4} and s["cost_gbp"] == 0.002
    always_l = sum(ds.costs[(pid, "L")] for pid in ids)
    assert s["always_l_cost_gbp"] == round(always_l, 6)
    assert s["saved_vs_always_l"] == round(1 - 0.002 / always_l, 4)
    dyno_pass = [False, True, True, True]
    agree = sum(d == st for d, st in zip(dyno_pass, stored, strict=True))
    assert s["agreement_rate"] == round(agree / 4, 4)
    assert len(s["disagreements"]) == 4 - agree
    text = dyno.render(s)
    assert "Saved vs always-L" in text and "Agreement:" in text
