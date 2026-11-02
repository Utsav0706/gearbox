"""Cascade experiment: would checking answers and escalating beat always-Haiku?

    py -m gearbox.eval.cascade submit [--yes]   # check stored S and M answers (one batch)
    py -m gearbox.eval.cascade collect
    py -m gearbox.eval.cascade report           # -> reports/cascade.md + cascade.json

Uses the answers already stored for dataset v2 (nothing is re-answered): every S and M
answer is checked once, by free signals and then the checker. The escalation threshold
(min_score) is chosen on the TRAINING split, by how well it separates failing answers
(judge score < 4) from passing ones; the TEST split is scored once with that choice.

Strategies compared on the test split, through the same offline evaluator as the
headline (gearbox.eval.offline), with check costs counted as routing cost:
- M->L: Haiku answers; if the check fails, Sonnet's answer is used instead.
- v3+cascade: the configured router picks; a failed check moves one tier up.
- S->M->L: the free model first, climbing a tier each time a check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from gearbox.cascade import check_params, escalate, free_signal, parse_check
from gearbox.config import TIERS, AppConfig, Tier, get_config
from gearbox.data.answer import BATCH_DISCOUNT, _client, load_answers, load_prompts
from gearbox.data.judge import PASS_SCORE
from gearbox.eval.offline import Dataset, Strategy, evaluate, load_dataset, load_strategies
from gearbox.log import setup_logging
from gearbox.tiers import Usage, cost_gbp

CHECKED_TIERS: tuple[Tier, ...] = ("S", "M")
DEFAULT_CAP_GBP = 0.40  # the C1 budget Uv approved


class CheckRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tier: Tier
    score: int | None  # checker's 1-5, None when a free signal decided (or the check failed)
    reason: str
    signal: str | None  # the free signal that fired, if any
    cost_gbp: float  # as billed (batch = half price)
    via: Literal["signal", "batch"]
    batch_id: str | None = None


# --- files ------------------------------------------------------------------------------


def load_checks(data_dir: Path) -> list[CheckRow]:
    path = data_dir / "checks.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [CheckRow.model_validate_json(line) for line in f if line.strip()]


def save_checks(data_dir: Path, new: list[CheckRow]) -> None:
    rows = {(c.id, c.tier): c for c in load_checks(data_dir)}
    rows.update({(c.id, c.tier): c for c in new})
    body = "".join(rows[k].model_dump_json() + "\n" for k in sorted(rows))
    (data_dir / "checks.jsonl").write_text(body, encoding="utf-8", newline="\n")


def _batches_path(data_dir: Path) -> Path:
    return data_dir / "check_batches.json"


def load_batches(data_dir: Path) -> list[dict[str, Any]]:
    p = _batches_path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_batches(data_dir: Path, batches: list[dict[str, Any]]) -> None:
    _batches_path(data_dir).write_text(
        json.dumps(batches, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


# --- submit / collect ---------------------------------------------------------------------


def plan_checks(data_dir: Path, config: AppConfig, rubric: str):
    """(rows decided by free signals, batch requests for the rest), skipping done ones."""
    prompts = {p.id: p.prompt for p in load_prompts(data_dir)}
    done = {(c.id, c.tier) for c in load_checks(data_dir)}
    signal_rows, requests = [], []
    for a in load_answers(data_dir):
        if a.tier not in CHECKED_TIERS or (a.id, a.tier) in done:
            continue
        signal = free_signal(a.text, a.finish_reason)
        if signal is not None:
            signal_rows.append(
                CheckRow(
                    id=a.id,
                    tier=a.tier,
                    score=None,
                    reason=signal,
                    signal=signal,
                    cost_gbp=0.0,
                    via="signal",
                )
            )
        else:
            requests.append(
                {
                    "custom_id": f"{a.id}__{a.tier}",
                    "params": check_params(prompts[a.id], a.text, config, rubric),
                }
            )
    return signal_rows, requests


def estimate_gbp(requests: list[dict[str, Any]], config: AppConfig) -> tuple[float, float]:
    """(expected, worst case). Expected assumes ~400 output tokens per check; worst case
    every check uses its full max_tokens. Input from characters (~3.5 per token)."""
    price = config.prices_usd_per_mtok[config.cascade.model]
    tokens_in = sum(
        (len(r["params"]["system"]) + len(r["params"]["messages"][0]["content"])) / 3.5
        for r in requests
    )

    def gbp(out_per_check: int) -> float:
        usage = Usage(int(tokens_in), out_per_check * len(requests))
        return cost_gbp(usage, price, config.usd_to_gbp) * BATCH_DISCOUNT

    return gbp(400), gbp(config.cascade.max_tokens)


def row_from_result(result: Any, config: AppConfig, batch_id: str) -> CheckRow:
    prompt_id, tier = result.custom_id.rsplit("__", 1)
    if result.result.type != "succeeded":
        return CheckRow(
            id=prompt_id,
            tier=tier,
            score=None,
            reason=f"check {result.result.type}",
            signal=None,
            cost_gbp=0.0,
            via="batch",
            batch_id=batch_id,
        )
    msg = result.result.message
    usage = Usage(msg.usage.input_tokens or 0, msg.usage.output_tokens or 0)
    price = config.prices_usd_per_mtok[config.cascade.model]
    cost = cost_gbp(usage, price, config.usd_to_gbp) * BATCH_DISCOUNT
    text = "".join(b.text for b in msg.content if b.type == "text")
    out = parse_check(text) if msg.stop_reason == "end_turn" else None
    return CheckRow(
        id=prompt_id,
        tier=tier,
        score=out.score if out else None,
        reason=out.reason if out else f"unreadable check ({msg.stop_reason})",
        signal=None,
        cost_gbp=cost,
        via="batch",
        batch_id=batch_id,
    )


# --- analysis -----------------------------------------------------------------------------


def detection(
    checks: dict[tuple[str, str], CheckRow],
    ds: Dataset,
    ids: list[str],
    tier: Tier,
    min_score: int,
) -> dict[str, float]:
    """How well "escalate" picks out failing answers (judge score < PASS_SCORE)."""
    tp = fp = fn = tn = 0
    for i in ids:
        c = checks[(i, tier)]
        flagged = escalate(c.score, c.signal, min_score)
        failing = ds.scores[(i, tier)] < PASS_SCORE
        tp += flagged and failing
        fp += flagged and not failing
        fn += failing and not flagged
        tn += not flagged and not failing
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "min_score": min_score,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "escalation_rate": round((tp + fp) / len(ids), 3),
        "failing_share": round((tp + fn) / len(ids), 3),
    }


def tune(checks, ds: Dataset, train_ids: list[str], tier: Tier) -> tuple[int, list[dict]]:
    """min_score with the best F1 on the training split (ties: the cheaper, lower one)."""
    table = [detection(checks, ds, train_ids, tier, m) for m in (2, 3, 4, 5)]
    best = max(table, key=lambda r: (r["f1"], -r["min_score"]))
    return best["min_score"], table


def _check_cost(checks, key) -> float:
    c = checks[key]
    return c.cost_gbp / BATCH_DISCOUNT  # live checks would pay list price


def cascade_strategy(
    name: str,
    ds: Dataset,
    checks,
    start: dict[str, Tier],
    min_scores: dict[str, int],
    max_steps: int,
) -> Strategy:
    """From each prompt's starting tier, check and climb at most max_steps tiers."""
    picks: dict[str, Tier] = {}
    extra: dict[str, float] = {}
    for i in ds.ids:
        tier = start[i]
        cost = 0.0
        for _ in range(max_steps):
            if tier == "L" or tier not in min_scores:
                break
            c = checks[(i, tier)]
            cost += _check_cost(checks, (i, tier))
            if not escalate(c.score, c.signal, min_scores[tier]):
                break
            cost += ds.costs[(i, tier)]  # the escalated-from answer was paid for too
            tier = TIERS[TIERS.index(tier) + 1]
        picks[i] = tier
        extra[i] = cost
    return Strategy(name, picks, extra, [])


def report(data_dir: Path, predictions: Path, router: str) -> dict[str, Any]:
    ds = load_dataset(data_dir)
    split = json.loads((data_dir / "split.json").read_text(encoding="utf-8"))
    checks = {(c.id, c.tier): c for c in load_checks(data_dir)}
    min_scores, tuning = {}, {}
    for tier in CHECKED_TIERS:
        min_scores[tier], tuning[tier] = tune(checks, ds, split["train"], tier)
    test_detection = {t: detection(checks, ds, ds.ids, t, min_scores[t]) for t in CHECKED_TIERS}

    base = load_strategies(ds, predictions)
    by_name = {s.name: s for s in base}
    v3 = by_name[router]
    strategies = [
        by_name["always-S"],
        by_name["always-M"],
        by_name["always-L"],
        by_name["oracle"],
        v3,
        cascade_strategy("M->L", ds, checks, dict.fromkeys(ds.ids, "M"), min_scores, 1),
        cascade_strategy(f"{router}+cascade", ds, checks, v3.picks, min_scores, 1),
        cascade_strategy("S->M->L", ds, checks, dict.fromkeys(ds.ids, "S"), min_scores, 2),
    ]
    results = evaluate(ds, strategies)
    paid = [c for c in checks.values() if c.via == "batch"]

    # Where M->L escalates, and how often a free signal (no checker call) decided it.
    sources = {p.id: p.source for p in load_prompts(data_dir)}
    escalated = [
        i
        for i in ds.ids
        if escalate(checks[(i, "M")].score, checks[(i, "M")].signal, min_scores["M"])
    ]
    by_source: dict[str, int] = {}
    for i in escalated:
        by_source[sources[i]] = by_source.get(sources[i], 0) + 1
    return {
        "escalations_m_to_l": {
            "total": len(escalated),
            "by_free_signal": sum(checks[(i, "M")].signal is not None for i in escalated),
            "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1])),
        },
        "min_scores": min_scores,
        "tuning_train": tuning,
        "detection_test": test_detection,
        "results": results,
        "checks": {
            "total": len(checks),
            "by_signal": sum(c.via == "signal" for c in checks.values()),
            "failed": sum(c.via == "batch" and c.score is None for c in checks.values()),
            "spend_gbp": round(sum(c.cost_gbp for c in paid), 4),
        },
        "test_prompts": len(ds.ids),
    }


def render(r: dict[str, Any]) -> str:
    res = r["results"]
    m, best = (
        res["always-M"],
        max(
            (k for k in res if "->" in k or k.endswith("+cascade")),
            key=lambda k: res[k]["pass_rate"],
        ),
    )
    gain = 100 * (res[best]["pass_rate"] - m["pass_rate"])
    cost_x = res[best]["cost_gbp"] / m["cost_gbp"] if m["cost_gbp"] else float("inf")
    worth = gain >= 2.0 and cost_x <= 2.0
    lines = [
        "# Cascade experiment",
        "",
        f"Would checking an answer and escalating to the next tier beat always using the "
        f"medium tier? Best cascade: **{best}**, {gain:+.1f} points of pass rate at "
        f"{cost_x:.1f}x always-M's cost: "
        + (
            "**clears the bar** (+2 points at no more than 2x the cost)."
            if worth
            else "**does not clear the bar** (+2 points at no more than 2x the cost)."
        ),
        "",
        "Generated by `py -m gearbox.eval.cascade report` from stored answers, judge scores "
        "and one batch of checks (`data/v2/checks.jsonl`). The escalation threshold was "
        "chosen on the training split; the table below is the test split, scored once.",
        "",
        "## How well the check spots failing answers",
        "",
        "| answers checked | min_score (from train) | precision | recall | F1 | escalation rate "
        "| failing share |",
        "|---|---|---|---|---|---|---|",
    ]
    for tier, d in r["detection_test"].items():
        lines.append(
            f"| {tier} | {d['min_score']} | {d['precision']:.2f} | {d['recall']:.2f} | "
            f"{d['f1']:.2f} | {100 * d['escalation_rate']:.0f}% | {100 * d['failing_share']:.0f}% |"
        )
    lines += [
        "",
        "Precision: of the answers it escalated, the share that really failed. Recall: of "
        "the answers that failed, the share it caught.",
        "",
        f"## Outcomes on {r['test_prompts']} test prompts",
        "",
        "| strategy | pass rate | quality | cost vs L | per 1k requests | final tier S/M/L |",
        "|---|---|---|---|---|---|",
    ]
    for name, x in res.items():
        p = x["picks"]
        lines.append(
            f"| {name} | {100 * x['pass_rate']:.1f}% | {x['quality_pct']:.1f}% | "
            f"{x['cost_pct']:.1f}% | GBP {x['cost_per_1k_requests_gbp']:.3f} | "
            f"{p['S']}/{p['M']}/{p['L']} |"
        )
    c = r["checks"]
    lines += [
        "",
        f"Checks: {c['total']} answers ({c['by_signal']} decided by free signals, "
        f"{c['failed']} checks unreadable); checker spend GBP {c['spend_gbp']:.3f} (batch). "
        "Cascade costs include the check at list price and, when it escalates, both answers.",
        "",
        "## Where M->L escalates (test split)",
        "",
        f"{r['escalations_m_to_l']['total']} escalations, "
        f"{r['escalations_m_to_l']['by_free_signal']} of them decided by a free signal "
        "(an empty answer, a cut-off one, or Haiku saying it can't verify) with no checker "
        "call. By source: "
        + ", ".join(f"{k} {v}" for k, v in r["escalations_m_to_l"]["by_source"].items())
        + ". These are the prompt types where the probe found Sonnet worth its price "
        "(niche facts, competition maths).",
        "",
        "## Tuning on the training split",
        "",
    ]
    for tier, table in r["tuning_train"].items():
        lines.append(f"Tier {tier} answers:")
        lines.append("")
        lines.append("| min_score | precision | recall | F1 | escalation rate |")
        lines.append("|---|---|---|---|---|")
        for d in table:
            lines.append(
                f"| {d['min_score']} | {d['precision']:.2f} | {d['recall']:.2f} | "
                f"{d['f1']:.2f} | {100 * d['escalation_rate']:.0f}% |"
            )
        lines.append("")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="py -m gearbox.eval.cascade",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("cmd", choices=["submit", "collect", "report"])
    parser.add_argument("--data", type=Path, default=Path("data/v2"))
    parser.add_argument(
        "--predictions", type=Path, default=Path("reports/router_predictions.jsonl")
    )
    parser.add_argument("--cap-gbp", type=float, default=DEFAULT_CAP_GBP)
    parser.add_argument("--yes", action="store_true", help="submit: actually send (paid)")
    args = parser.parse_args(argv)
    setup_logging("WARNING")
    config = get_config()

    if args.cmd == "submit":
        if any(not b["collected"] for b in load_batches(args.data)):
            print("a check batch is still open: run `collect` first")
            return 1
        rubric = config.cascade.prompt.read_text(encoding="utf-8")
        signal_rows, requests = plan_checks(args.data, config, rubric)
        expected, worst = estimate_gbp(requests, config)
        print(
            f"{len(signal_rows)} answers decided by free signals (no cost); "
            f"{len(requests)} to check: expected GBP {expected:.3f}, worst case "
            f"GBP {worst:.3f} (cap {args.cap_gbp:.2f})"
        )
        if worst > args.cap_gbp:
            print("REFUSED: the worst case is over the cap")
            return 1
        if not args.yes:
            print("dry run: add --yes to send it")
            return 0
        save_checks(args.data, signal_rows)
        if requests:
            batch = _client().messages.batches.create(requests=requests)
            save_batches(
                args.data,
                [
                    *load_batches(args.data),
                    {
                        "batch_id": batch.id,
                        "requests": len(requests),
                        "estimate_gbp": round(expected, 4),
                        "collected": False,
                    },
                ],
            )
            print(f"sent batch {batch.id}; run `collect` when it has ended")
    elif args.cmd == "collect":
        batches = load_batches(args.data)
        client = _client()
        for b in batches:
            if b["collected"]:
                continue
            while client.messages.batches.retrieve(b["batch_id"]).processing_status != "ended":
                print(f"{b['batch_id']}: waiting...")
                time.sleep(30)
            rows = [
                row_from_result(r, config, b["batch_id"])
                for r in client.messages.batches.results(b["batch_id"])
            ]
            save_checks(args.data, rows)
            b["collected"] = True
            save_batches(args.data, batches)
            spend = sum(r.cost_gbp for r in rows)
            print(
                f"{b['batch_id']}: {len(rows)} checks, GBP {spend:.3f}, "
                f"{sum(r.score is None for r in rows)} unreadable"
            )
    else:
        result = report(args.data, args.predictions, config.routing.router)
        Path("reports/cascade.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        md = render(result)
        Path("reports/cascade.md").write_text(md, encoding="utf-8", newline="\n")
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
