"""Configuration: non-secret settings from config.yaml, secrets from the environment.

Two sources on purpose: config.yaml is committed and reviewed like code
(models, prices), while secrets only ever come from env vars / .env.

Check a config file without starting anything:  py -m gearbox.config [path]
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cheapest first. Routers predict one of these; the order is what "downgrade" and
# "next tier" mean in the policy and fallback layers.
Tier = Literal["S", "M", "L"]
TIERS: tuple[Tier, ...] = get_args(Tier)


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    # Only for self-hosted models (Ollama). API providers use their default endpoint.
    api_base: str | None = None
    max_tokens: int = Field(gt=0, le=16_000)
    timeout_s: float = Field(default=60, gt=0, le=600)
    # Extra provider options passed straight to the call, e.g. {think: false} to turn
    # off a local model's slow thinking mode.
    params: dict[str, Any] = Field(default_factory=dict)


class ModelPrice(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class JudgeConfig(BaseModel):
    """The LLM judge that scores answers to make the labels (M4) and the eval (M8)."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    prompt: Path  # versioned rubric file, e.g. prompts/judge_v1.md
    effort: Literal["low", "medium", "high"] = "low"
    max_tokens: int = Field(default=2000, gt=0, le=16_000)


class LLMRouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: Tier = "M"  # which tier's model does the classifying
    prompt: Path


class EmbedRouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: Path  # trained weights + metadata (JSON), written by gearbox.routers.embed


class RoutingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    router: str = "v3-embed"  # the router the service uses (any registered name)
    v2_llm: LLMRouterConfig
    v3_embed: EmbedRouterConfig


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: dict[Tier, TierConfig]
    routing: RoutingConfig
    judge: JudgeConfig
    prices_usd_per_mtok: dict[str, ModelPrice]
    usd_to_gbp: float = Field(gt=0)

    @model_validator(mode="after")
    def complete_and_priced(self) -> AppConfig:
        missing = set(TIERS) - set(self.tiers)
        if missing:
            raise ValueError(f"config.yaml is missing tiers: {sorted(missing)}")
        # Without a price, cost logging and the budget cap would silently read 0.
        models = {t.model for t in self.tiers.values()} | {self.judge.model}
        unpriced = models - set(self.prices_usd_per_mtok)
        if unpriced:
            raise ValueError(f"prices_usd_per_mtok is missing: {sorted(unpriced)}")
        return self


class Secrets(BaseSettings):
    """Read from env vars, falling back to a local .env file."""

    # env_ignore_empty: a blank line such as "ANTHROPIC_API_KEY=" (as in .env.example)
    # means "not set", not an empty value.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    anthropic_api_key: SecretStr | None = None
    # Request log. A local SQLite file by default; Postgres in Docker Compose and CI.
    database_url: str = "sqlite:///gearbox_local.db"
    log_level: str = "INFO"
    config_path: Path = Path("config.yaml")


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f))


@lru_cache
def get_secrets() -> Secrets:
    return Secrets()


@lru_cache
def get_config() -> AppConfig:
    return load_config(get_secrets().config_path)


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else get_secrets().config_path
    try:
        config = load_config(path)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        print(f"{path}: invalid\n{exc}", file=sys.stderr)
        return 1
    print(f"{path}: OK")
    for name, tier in config.tiers.items():
        print(f"  {name}: {tier.model} (max_tokens {tier.max_tokens})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
