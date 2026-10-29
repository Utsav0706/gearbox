"""Score every answer 1-5 with the LLM judge: data/<version>/judgments.jsonl (M4).

    py -m gearbox.data.judge submit --pilot      # dry run: 20 prompts x 3 tiers
    py -m gearbox.data.judge submit --yes        # every answer not yet judged
    py -m gearbox.data.judge collect
    py -m gearbox.data.judge status

The judge never sees which tier wrote an answer (blind grading), gets the reference
answer when the dataset has one, and must return JSON matching JUDGE_SCHEMA (structured
outputs, so no parsing guesswork). Rules that matter for the labels are enforced here in
code, not left to the prompt: an empty answer scores 1 without a model call, and the
overall score is never allowed above the correctness score.

Runs as one Message Batch at half price, with the same dry-run / --yes / cap guard as
gearbox.data.answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gearbox.config import AppConfig, Tier, get_config
from gearbox.data.answer import (
    BATCH_DISCOUNT,
    AnswerRow,
    _client,
    load_answers,
    load_prompts,
    pilot_ids,
)
from gearbox.data.schema import PromptItem
from gearbox.log import setup_logging
from gearbox.tiers import Usage, cost_gbp

DEFAULT_CAP_GBP = 3.50  # spend cap for judging one dataset version
PASS_SCORE = 4  # "good enough to send as it is" (see prompts/judge_v1.md)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # reason first: the model states the decisive flaw before it commits to numbers
        "reason": {"type": "string"},
        "correctness": {"type": "integer"},
        "completeness": {"type": "integer"},
        "clarity": {"type": "integer"},
        "score": {"type": "integer"},
    },
    "required": ["reason", "correctness", "completeness", "clarity", "score"],
    "additionalProperties": False,
}


class JudgeOutput(BaseModel):
    """What the judge returns. Structured outputs can't express 1-5, so it's checked here."""

    reason: str
    correctness: int = Field(ge=1, le=5)
    completeness: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    score: int = Field(ge=1, le=5)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tier: Tier
    score: int  # overall 1-5; >= PASS_SCORE means the answer passes
    correctness: int
    completeness: int
    clarity: int
    reason: str
    judge_model: str
    rubric: str  # prompt file stem, e.g. judge_v1, so a rubric change is traceable
    cost_gbp: float
    via: Literal["batch", "rule"]  # rule = scored in code (empty answer), no model call
    batch_id: str | None = None


class JudgeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    requests: int
    estimate_gbp: float
    collected: bool = False


# --- files ------------------------------------------------------------------------------


def load_judgments(data_dir: Path) -> list[Judgment]:
    path = data_dir / "judgments.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [Judgment.model_validate_json(line) for line in f if line.strip()]


def save_judgments(data_dir: Path, new: list[Judgment]) -> int:
    rows = {(j.id, j.tier): j for j in load_judgments(data_dir)}
    rows.update({(j.id, j.tier): j for j in new})
    body = "".join(rows[k].model_dump_json() + "\n" for k in sorted(rows))
    tmp = data_dir / "judgments.jsonl.tmp"
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(data_dir / "judgments.jsonl")
    return len(rows)


def load_batches(data_dir: Path) -> list[JudgeBatch]:
    path = data_dir / "judge_batches.json"
    if not path.exists():
        return []
    return [JudgeBatch.model_validate(b) for b in json.loads(path.read_text(encoding="utf-8"))]


def save_batches(data_dir: Path, batches: list[JudgeBatch]) -> None:
    body = json.dumps([b.model_dump() for b in batches], indent=2) + "\n"
    (data_dir / "judge_batches.json").write_text(body, encoding="utf-8", newline="\n")


# --- requests -----------------------------------------------------------------------------


def custom_id(prompt_id: str, tier: Tier) -> str:
    return f"{prompt_id}__{tier}"  # prompt ids are [a-z0-9-], so "__" can't be in them


def split_custom_id(value: str) -> tuple[str, Tier]:
    prompt_id, tier = value.rsplit("__", 1)
    return prompt_id, tier  # type: ignore[return-value]


def user_message(item: PromptItem, answer: AnswerRow) -> str:
    parts = [f"<request>\n{item.prompt}\n</request>"]
    if item.reference:
        parts.append(f'<reference kind="{item.reference_kind}">\n{item.reference}\n</reference>')
    parts.append(f"<answer>\n{answer.text}\n</answer>")
    return "\n\n".join(parts)


def judge_request(
    item: PromptItem, answer: AnswerRow, config: AppConfig, rubric: str
) -> dict[str, Any]:
    j = config.judge
    return {
        "custom_id": custom_id(item.id, answer.tier),
        "params": {
            "model": j.model.removeprefix("anthropic/"),
            "max_tokens": j.max_tokens,
            "system": rubric,
            "messages": [{"role": "user", "content": user_message(item, answer)}],
            "output_config": {
                "effort": j.effort,
                "format": {"type": "json_schema", "schema": JUDGE_SCHEMA},
            },
        },
    }


def rule_judgment(answer: AnswerRow, config: AppConfig, rubric_name: str) -> Judgment | None:
    """Answers that need no judge: an empty answer is a fail, whoever wrote it."""
    if answer.text.strip():
        return None
    return Judgment(
        id=answer.id,
        tier=answer.tier,
        score=1,
        correctness=1,
        completeness=1,
        clarity=1,
        reason="Empty answer.",
        judge_model=config.judge.model,
        rubric=rubric_name,
        cost_gbp=0.0,
        via="rule",
    )


def judgment_from_result(
    result: Any, config: AppConfig, rubric_name: str, batch_id: str
) -> Judgment | None:
    """One batch result -> Judgment, or None if it failed (it gets resubmitted)."""
    if result.result.type != "succeeded":
        return None
    msg = result.result.message
    if msg.stop_reason != "end_turn":  # max_tokens or refusal: no trustworthy JSON
        return None
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        out = JudgeOutput.model_validate_json(text)
    except ValidationError:
        return None
    usage = Usage(msg.usage.input_tokens or 0, msg.usage.output_tokens or 0)
    price = config.prices_usd_per_mtok[config.judge.model]
    prompt_id, tier = split_custom_id(result.custom_id)
    return Judgment(
        id=prompt_id,
        tier=tier,
        # The rubric says overall can't beat correctness; enforce it rather than trust it.
        score=min(out.score, out.correctness),
        correctness=out.correctness,
        completeness=out.completeness,
        clarity=out.clarity,
        reason=out.reason,
        judge_model=config.judge.model,
        rubric=rubric_name,
        cost_gbp=cost_gbp(usage, price, config.usd_to_gbp) * BATCH_DISCOUNT,
        via="batch",
        batch_id=batch_id,
    )


def todo(
    answers: list[AnswerRow],
    judgments: list[Judgment],
    batches: list[JudgeBatch],
    only: set[str] | None = None,
) -> list[AnswerRow]:
    if any(not b.collected for b in batches):
        raise RuntimeError("a judge batch is still open: run `collect` first")
    done = {(j.id, j.tier) for j in judgments}
    return [a for a in answers if (a.id, a.tier) not in done and (only is None or a.id in only)]


def estimate_gbp(
    requests: list[dict[str, Any]], config: AppConfig, judgments: list[Judgment]
) -> tuple[float, str]:
    """Input from the request text (~3.5 chars/token, errs high); output measured or
    worst case. Measured output is backed out of past costs at the judge's price."""
    price = config.prices_usd_per_mtok[config.judge.model]
    tokens_in = sum(
        (len(r["params"]["system"]) + len(r["params"]["messages"][0]["content"])) / 3.5 + 20
        for r in requests
    )
    paid = [j for j in judgments if j.via == "batch"]
    if paid:
        avg_gbp = sum(j.cost_gbp for j in paid) / len(paid)
        return avg_gbp * len(requests), f"measured average of {len(paid)} judgments"
    usage = Usage(int(tokens_in), config.judge.max_tokens * len(requests))
    basis = f"worst case: every judgment uses all {config.judge.max_tokens} tokens"
    return cost_gbp(usage, price, config.usd_to_gbp) * BATCH_DISCOUNT, basis


# --- CLI ----------------------------------------------------------------------------------


def collect(data_dir: Path, config: AppConfig, rubric_name: str, client: Any) -> None:
    batches = load_batches(data_dir)
    for b in batches:
        if b.collected:
            continue
        status = client.messages.batches.retrieve(b.batch_id)
        if status.processing_status != "ended":
            c = status.request_counts
            print(f"{b.batch_id}: {status.processing_status}, {c.succeeded} done")
            continue
        rows, failed = [], []
        for result in client.messages.batches.results(b.batch_id):
            row = judgment_from_result(result, config, rubric_name, b.batch_id)
            (failed.append(result.custom_id) if row is None else rows.append(row))
        save_judgments(data_dir, rows)
        b.collected = True
        save_batches(data_dir, batches)
        cost = sum(r.cost_gbp for r in rows)
        print(
            f"{b.batch_id}: {len(rows)} judgments, GBP {cost:.3f} "
            f"(estimate was {b.estimate_gbp:.3f}), {len(failed)} failed"
        )
        for cid in failed:
            print(f"  failed: {cid} (resubmitted by the next `submit`)")


def status(data_dir: Path) -> None:
    judgments = load_judgments(data_dir)
    for tier in ("S", "M", "L"):
        js = [j for j in judgments if j.tier == tier]
        if not js:
            print(f"tier {tier}: 0 judged")
            continue
        passed = sum(j.score >= PASS_SCORE for j in js)
        avg = sum(j.score for j in js) / len(js)
        print(
            f"tier {tier}: {len(js)} judged, pass rate {passed / len(js):.0%}, mean score {avg:.2f}"
        )
    spent = sum(j.cost_gbp for j in judgments)
    open_ = sum(b.estimate_gbp for b in load_batches(data_dir) if not b.collected)
    print(f"judge spend GBP {spent:.3f} (+ GBP {open_:.3f} in open batches)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="py -m gearbox.data.judge",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", type=Path, default=Path("data/v1"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_submit = sub.add_parser("submit")
    p_submit.add_argument("--pilot", action="store_true", help="5 prompts per task type")
    p_submit.add_argument("--cap-gbp", type=float, default=DEFAULT_CAP_GBP)
    p_submit.add_argument("--yes", action="store_true", help="actually send (costs money)")
    sub.add_parser("collect")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    setup_logging("WARNING")
    config = get_config()
    rubric = config.judge.prompt.read_text(encoding="utf-8")
    rubric_name = config.judge.prompt.stem

    if args.cmd == "status":
        status(args.data)
        return 0
    if args.cmd == "collect":
        collect(args.data, config, rubric_name, _client())
        return 0

    prompts = {p.id: p for p in load_prompts(args.data)}
    judgments, batches = load_judgments(args.data), load_batches(args.data)
    only = pilot_ids(list(prompts.values())) if args.pilot else None
    pending = todo(load_answers(args.data), judgments, batches, only)

    ruled = [r for a in pending if (r := rule_judgment(a, config, rubric_name)) is not None]
    if ruled:
        save_judgments(args.data, ruled)
        print(f"{len(ruled)} empty answer(s) scored 1 without a model call")
    to_send = [a for a in pending if a.text.strip()]
    if not to_send:
        print("nothing left to judge")
        return 0

    requests = [judge_request(prompts[a.id], a, config, rubric) for a in to_send]
    estimate, basis = estimate_gbp(requests, config, judgments)
    spent = sum(j.cost_gbp for j in judgments)
    print(f"{len(requests)} answers to judge, estimate GBP {estimate:.3f} ({basis})")
    print(f"judge spend so far GBP {spent:.3f}; cap GBP {args.cap_gbp:.2f}")
    if spent + estimate > args.cap_gbp:
        print("REFUSED: this batch could take judge spend past the cap.")
        return 1
    if not args.yes:
        print("dry run: add --yes to send it")
        return 0
    batch = _client().messages.batches.create(requests=requests)
    batches.append(
        JudgeBatch(batch_id=batch.id, requests=len(requests), estimate_gbp=round(estimate, 4))
    )
    save_batches(args.data, batches)
    print(f"sent batch {batch.id}; run `collect` when it has ended")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
