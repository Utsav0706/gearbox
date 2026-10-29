"""Answer every prompt on every tier: data/<version>/answers.jsonl (M3).

    py -m gearbox.data.answer local --tier S          # local model, free, resumable
    py -m gearbox.data.answer submit --tier M --pilot # 20 prompts (5 per task type)
    py -m gearbox.data.answer submit --tier L --yes   # all prompts not yet answered
    py -m gearbox.data.answer collect                 # fetch finished batches
    py -m gearbox.data.answer status

Why two paths: tier S runs on this laptop through call_tier, exactly as the service will
call it. Tiers M and L go through Anthropic's Message Batches API, which costs half and
needs no rate-limit handling; the request (model, max_tokens, messages) is the same one
call_tier sends, so the answers match what the router will get live.

Spend guard: `submit` prints a cost estimate and only sends with --yes, and refuses any
batch that would take the paid total for this dataset past --cap-gbp. Before a tier has
answers to measure, the estimate assumes every answer uses its full max_tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gearbox.config import AppConfig, Tier, get_config, get_secrets
from gearbox.data.schema import PromptItem
from gearbox.log import setup_logging
from gearbox.tiers import TierError, Usage, call_tier, cost_gbp

BATCH_DISCOUNT = 0.5  # Message Batches bill every token at half price
DEFAULT_CAP_GBP = 2.50  # spend cap for answering one dataset version
PILOT_PER_TYPE = 5
# Anthropic stop reasons, in the OpenAI-style names call_tier (LiteLLM) returns.
_FINISH = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop"}


class AnswerRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tier: Tier
    model: str
    text: str
    finish_reason: str  # "stop", "length" (cut off by max_tokens) or "refusal"
    input_tokens: int
    output_tokens: int  # includes thinking tokens, which are billed as output
    cost_gbp: float
    latency_ms: int | None  # None for batch answers (no meaningful per-request latency)
    via: Literal["live", "batch"]
    batch_id: str | None = None


class BatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    tier: Tier
    model: str
    requests: int
    estimate_gbp: float
    collected: bool = False


# --- files ------------------------------------------------------------------------------


def load_prompts(data_dir: Path) -> list[PromptItem]:
    with (data_dir / "prompts.jsonl").open(encoding="utf-8") as f:
        return [PromptItem.model_validate_json(line) for line in f if line.strip()]


def load_answers(data_dir: Path) -> list[AnswerRow]:
    path = data_dir / "answers.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [AnswerRow.model_validate_json(line) for line in f if line.strip()]


def save_answers(data_dir: Path, new: Iterable[AnswerRow]) -> int:
    """Merge new rows in (a newer answer for the same id+tier replaces the old one) and
    rewrite the file sorted, so the committed file diffs cleanly. Returns the total."""
    rows = {(r.id, r.tier): r for r in load_answers(data_dir)}
    rows.update({(r.id, r.tier): r for r in new})
    body = "".join(rows[k].model_dump_json() + "\n" for k in sorted(rows))
    tmp = data_dir / "answers.jsonl.tmp"
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(data_dir / "answers.jsonl")  # atomic: a crash never leaves half a file
    return len(rows)


def load_batches(data_dir: Path) -> list[BatchRecord]:
    path = data_dir / "batches.json"
    if not path.exists():
        return []
    return [BatchRecord.model_validate(b) for b in json.loads(path.read_text(encoding="utf-8"))]


def save_batches(data_dir: Path, batches: list[BatchRecord]) -> None:
    body = json.dumps([b.model_dump() for b in batches], indent=2) + "\n"
    (data_dir / "batches.json").write_text(body, encoding="utf-8", newline="\n")


# --- choosing what to answer --------------------------------------------------------------


def pilot_ids(prompts: list[PromptItem], per_type: int = PILOT_PER_TYPE) -> set[str]:
    """The same small, seeded sample per task type for every tier, so pilots compare."""
    chosen: set[str] = set()
    for task_type in sorted({p.task_type for p in prompts}):
        ids = sorted(p.id for p in prompts if p.task_type == task_type)
        random.Random(f"pilot:{task_type}").shuffle(ids)
        chosen.update(ids[:per_type])
    return chosen


def todo(
    prompts: list[PromptItem],
    answers: list[AnswerRow],
    tier: Tier,
    batches: list[BatchRecord] | None = None,
    only: set[str] | None = None,
    max_tokens: int | None = None,
) -> list[PromptItem]:
    """Prompts with no answer yet on this tier (and not waiting in an open batch).

    An answer cut off by a lower max_tokens than the tier has now counts as not done:
    raising the cap re-answers exactly the prompts the old cap truncated."""
    done = {
        a.id
        for a in answers
        if a.tier == tier
        and not (a.finish_reason == "length" and max_tokens and a.output_tokens < max_tokens)
    }
    if batches and any(b.tier == tier and not b.collected for b in batches):
        raise RuntimeError(f"a tier {tier} batch is still open: run `collect` first")
    return [p for p in prompts if p.id not in done and (only is None or p.id in only)]


# --- cost ---------------------------------------------------------------------------------


def estimate_gbp(
    items: list[PromptItem], tier: Tier, config: AppConfig, answers: list[AnswerRow]
) -> tuple[float, str]:
    """Expected batch cost: average measured output so far, else the worst case."""
    t = config.tiers[tier]
    price = config.prices_usd_per_mtok[t.model]
    done = [a for a in answers if a.tier == tier]
    if done:
        out = sum(a.output_tokens for a in done) / len(done)
        basis = f"measured average of {len(done)} answers ({out:.0f} output tokens)"
    else:
        out = t.max_tokens
        basis = f"worst case: every answer uses all {t.max_tokens} tokens"
    # ~4 characters per token is an over-estimate for English, so this errs high.
    tokens_in = sum(len(p.prompt) / 3.5 + 10 for p in items)
    usage = Usage(int(tokens_in), int(out * len(items)))
    return cost_gbp(usage, price, config.usd_to_gbp) * BATCH_DISCOUNT, basis


def paid_so_far(answers: list[AnswerRow], batches: list[BatchRecord]) -> float:
    spent = sum(a.cost_gbp for a in answers)
    pending = sum(b.estimate_gbp for b in batches if not b.collected)
    return spent + pending


# --- tier S (local) -----------------------------------------------------------------------


async def answer_local(data_dir: Path, tier: Tier, items: list[PromptItem]) -> None:
    pending: list[AnswerRow] = []
    failed = 0
    for n, item in enumerate(items, 1):
        try:
            r = await call_tier(tier, [{"role": "user", "content": item.prompt}])
        except TierError as exc:
            failed += 1
            print(f"  {item.id}: {exc}", file=sys.stderr)
            continue
        pending.append(
            AnswerRow(
                id=item.id,
                tier=tier,
                model=r.model,
                text=r.text,
                finish_reason=r.finish_reason,
                input_tokens=r.usage.input_tokens,
                output_tokens=r.usage.output_tokens,
                cost_gbp=r.cost_gbp,
                latency_ms=r.latency_ms,
                via="live",
            )
        )
        if len(pending) == 10 or n == len(items):  # save often: a crash loses <10 answers
            total = save_answers(data_dir, pending)
            pending = []
            print(f"  {n}/{len(items)} answered ({total} rows in file, {failed} failed)")
    if failed:
        print(f"{failed} failed: run the same command again to retry just those.")


# --- tiers M and L (Message Batches) ------------------------------------------------------


def batch_request(item: PromptItem, tier: Tier, config: AppConfig) -> dict[str, Any]:
    """The same request call_tier would send for this prompt, as a batch entry."""
    t = config.tiers[tier]
    if not t.model.startswith("anthropic/"):
        raise ValueError(f"tier {tier} ({t.model}) is not an Anthropic model")
    if t.params:
        raise ValueError(f"tier {tier} has params {t.params}; batches don't send them")
    return {
        "custom_id": item.id,  # ids are [a-z0-9-], within the API's 64-char limit
        "params": {
            "model": t.model.removeprefix("anthropic/"),
            "max_tokens": t.max_tokens,
            "messages": [{"role": "user", "content": item.prompt}],
        },
    }


def row_from_result(result: Any, tier: Tier, config: AppConfig, batch_id: str) -> AnswerRow | None:
    """One batch result -> AnswerRow, or None if it failed (it stays in `todo`)."""
    if result.result.type != "succeeded":
        return None
    msg = result.result.message
    t = config.tiers[tier]
    usage = Usage(msg.usage.input_tokens or 0, msg.usage.output_tokens or 0)
    cost = cost_gbp(usage, config.prices_usd_per_mtok[t.model], config.usd_to_gbp)
    return AnswerRow(
        id=result.custom_id,
        tier=tier,
        model=t.model,
        # Thinking blocks come back empty by default; only text blocks are the answer.
        text="".join(b.text for b in msg.content if b.type == "text"),
        finish_reason=_FINISH.get(msg.stop_reason, msg.stop_reason),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_gbp=cost * BATCH_DISCOUNT,
        latency_ms=None,
        via="batch",
        batch_id=batch_id,
    )


def _client() -> Any:
    import anthropic  # the "data" extra; the service itself calls models via LiteLLM

    key = get_secrets().anthropic_api_key
    if key is None:
        raise SystemExit("ANTHROPIC_API_KEY is not set (add it to .env)")
    return anthropic.Anthropic(api_key=key.get_secret_value())


def collect(data_dir: Path, config: AppConfig, client: Any) -> None:
    batches = load_batches(data_dir)
    for b in batches:
        if b.collected:
            continue
        status = client.messages.batches.retrieve(b.batch_id)
        if status.processing_status != "ended":
            c = status.request_counts
            print(
                f"{b.batch_id} (tier {b.tier}): {status.processing_status}, "
                f"{c.processing} processing, {c.succeeded} done"
            )
            continue
        rows, failed = [], []
        for result in client.messages.batches.results(b.batch_id):
            row = row_from_result(result, b.tier, config, b.batch_id)
            if row is None:
                failed.append(f"{result.custom_id}: {result.result.type}")
            else:
                rows.append(row)
        save_answers(data_dir, rows)
        b.collected = True
        save_batches(data_dir, batches)
        cost = sum(r.cost_gbp for r in rows)
        cut = sum(r.finish_reason == "length" for r in rows)
        print(
            f"{b.batch_id} (tier {b.tier}): {len(rows)} answers, GBP {cost:.3f} "
            f"(estimate was {b.estimate_gbp:.3f}), {cut} cut off by max_tokens"
        )
        for line in failed:
            print(f"  failed {line} (will be resubmitted by the next `submit`)")


# --- CLI ----------------------------------------------------------------------------------


def status(data_dir: Path, prompts: list[PromptItem]) -> None:
    answers = load_answers(data_dir)
    batches = load_batches(data_dir)
    for tier in ("S", "M", "L"):
        rows = [a for a in answers if a.tier == tier]
        cut = sum(a.finish_reason == "length" for a in rows)
        cost = sum(a.cost_gbp for a in rows)
        print(f"tier {tier}: {len(rows)}/{len(prompts)} answered, {cut} cut off, GBP {cost:.3f}")
    open_ = [b for b in batches if not b.collected]
    print(
        f"open batches: {len(open_)}; paid so far incl. open estimates: "
        f"GBP {paid_so_far(answers, batches):.3f}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="py -m gearbox.data.answer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", type=Path, default=Path("data/v1"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_local = sub.add_parser("local", help="answer on a local tier (free)")
    p_local.add_argument("--tier", choices=["S"], default="S")
    p_local.add_argument("--limit", type=int, default=None)
    p_submit = sub.add_parser("submit", help="send a paid tier's prompts as one batch")
    p_submit.add_argument("--tier", choices=["M", "L"], required=True)
    p_submit.add_argument("--pilot", action="store_true", help="only 5 prompts per task type")
    p_submit.add_argument("--cap-gbp", type=float, default=DEFAULT_CAP_GBP)
    p_submit.add_argument("--yes", action="store_true", help="actually send (costs money)")
    sub.add_parser("collect", help="download finished batches")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    setup_logging("WARNING")
    config = get_config()
    prompts = load_prompts(args.data)

    if args.cmd == "status":
        status(args.data, prompts)
    elif args.cmd == "collect":
        collect(args.data, config, _client())
    elif args.cmd == "local":
        cap = config.tiers[args.tier].max_tokens
        items = todo(prompts, load_answers(args.data), args.tier, max_tokens=cap)[: args.limit]
        print(f"tier {args.tier}: {len(items)} prompts to answer")
        start = time.perf_counter()
        asyncio.run(answer_local(args.data, args.tier, items))
        print(f"done in {(time.perf_counter() - start) / 60:.1f} min")
    else:
        answers, batches = load_answers(args.data), load_batches(args.data)
        only = pilot_ids(prompts) if args.pilot else None
        cap = config.tiers[args.tier].max_tokens
        items = todo(prompts, answers, args.tier, batches, only, cap)
        if not items:
            print(f"tier {args.tier}: nothing left to answer")
            return 0
        estimate, basis = estimate_gbp(items, args.tier, config, answers)
        already = paid_so_far(answers, batches)
        print(f"tier {args.tier}: {len(items)} prompts, estimate GBP {estimate:.3f} ({basis})")
        print(f"paid so far GBP {already:.3f}; cap GBP {args.cap_gbp:.2f}")
        if already + estimate > args.cap_gbp:
            print("REFUSED: this batch could take the total past the cap.")
            return 1
        if not args.yes:
            print("dry run: add --yes to send it")
            return 0
        batch = _client().messages.batches.create(
            requests=[batch_request(p, args.tier, config) for p in items]
        )
        batches.append(
            BatchRecord(
                batch_id=batch.id,
                tier=args.tier,
                model=config.tiers[args.tier].model,
                requests=len(items),
                estimate_gbp=round(estimate, 4),
            )
        )
        save_batches(args.data, batches)
        print(f"sent batch {batch.id}; run `collect` when it has ended (usually < 1 h)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
