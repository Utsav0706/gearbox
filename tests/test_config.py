from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gearbox.config import TIERS, AppConfig, Secrets, load_config, main

ROOT = Path(__file__).resolve().parents[1]


def _raw() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_committed_config_is_valid():
    config = load_config(ROOT / "config.yaml")
    assert tuple(config.tiers) == TIERS  # cheapest first


def test_missing_tier_rejected():
    raw = _raw()
    del raw["tiers"]["M"]
    with pytest.raises(ValidationError, match="missing tiers"):
        AppConfig.model_validate(raw)


def test_unpriced_model_rejected():
    raw = _raw()
    raw["tiers"]["L"]["model"] = "anthropic/some-new-model"
    with pytest.raises(ValidationError, match="prices_usd_per_mtok is missing"):
        AppConfig.model_validate(raw)


def test_unknown_key_rejected():
    # extra="forbid": a typo like "max_token" must fail, not be silently ignored.
    raw = _raw()
    raw["tiers"]["S"]["max_token"] = 10
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_token_cap_enforced():
    raw = _raw()
    raw["tiers"]["L"]["max_tokens"] = 100_000
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_empty_env_value_means_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert Secrets(_env_file=None).anthropic_api_key is None


def test_check_command(tmp_path, capsys):
    assert main([str(ROOT / "config.yaml")]) == 0
    assert "OK" in capsys.readouterr().out

    bad = tmp_path / "bad.yaml"
    bad.write_text("tiers: {}\n", encoding="utf-8")
    assert main([str(bad)]) == 1
    assert "invalid" in capsys.readouterr().err
