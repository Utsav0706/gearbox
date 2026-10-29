"""Labels, the train/test split, and the human check on the judge (M4). All free.

    py -m gearbox.data.labels build         # labels.jsonl + split.json + manifest hashes
    py -m gearbox.data.labels export-hand   # 40 blind items for a human to score
    py -m gearbox.data.labels kappa         # judge vs human agreement (after scoring)

Label = the cheapest tier whose answer the judge scored >= 4 ("good enough to send").
If no tier passes, the label is L: the biggest model is the safest bet when nothing is
good, and it keeps "under-routing" (picking too small a model) the costly mistake.

Why Cohen's kappa and not plain agreement: if the judge passes 80% of answers, a judge
that always says "pass" agrees with a human 80% of the time while knowing nothing.
Kappa subtracts the agreement expected by chance, so 0 = no better than chance, 1 =
perfect. Reported for pass/fail (what the labels use) and, weighted, for the 1-5 scores.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from gearbox.config import TIERS, Tier
from gearbox.data.answer import load_answers, load_prompts
from gearbox.data.judge import PASS_SCORE, Judgment, load_judgments
from gearbox.data.schema import Difficulty, TaskType

SEED = 20261029
TEST_FRACTION = 0.2
HAND_ITEMS = 40


class LabelRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_type: TaskType
    difficulty: Difficulty
    label: Tier
    scores: dict[Tier, int]  # the judge's overall score per tier, kept for the eval


def label_for(scores: dict[Tier, int]) -> Tier:
    for tier in TIERS:  # cheapest first
        if scores[tier] >= PASS_SCORE:
            return tier
    return "L"


def make_labels(data_dir: Path) -> list[LabelRow]:
    by_key = {(j.id, j.tier): j for j in load_judgments(data_dir)}
    rows, missing = [], []
    for p in load_prompts(data_dir):
        if any((p.id, t) not in by_key for t in TIERS):
            missing.append(p.id)
            continue
        scores = {t: by_key[(p.id, t)].score for t in TIERS}
        rows.append(
            LabelRow(
                id=p.id,
                task_type=p.task_type,
                difficulty=p.difficulty,
                label=label_for(scores),
                scores=scores,
            )
        )
    if missing:
        raise ValueError(f"{len(missing)} prompts lack a judgment on some tier: {missing[:5]}")
    return rows


def split(labels: Sequence[LabelRow], seed: int = SEED) -> dict[str, list[str]]:
    """80/20, stratified by task type AND label: the test set gets the same mix of both,
    so a router can't look good just because the test set happens to be easy."""
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in labels:
        groups[(row.task_type, row.label)].append(row.id)
    train, test = [], []
    for key in sorted(groups):
        ids = sorted(groups[key])
        random.Random(f"{seed}:{key[0]}:{key[1]}").shuffle(ids)
        n_test = round(len(ids) * TEST_FRACTION)
        test += ids[:n_test]
        train += ids[n_test:]
    return {"train": sorted(train), "test": sorted(test)}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def update_manifest(data_dir: Path) -> None:
    """Record a hash of every data file, so any later edit to the data is detectable."""
    path = data_dir / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    names = ["answers.jsonl", "judgments.jsonl", "labels.jsonl", "split.json"]
    manifest["files_sha256"] = {n: _sha256(data_dir / n) for n in names if (data_dir / n).exists()}
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def build(data_dir: Path) -> tuple[list[LabelRow], dict[str, list[str]]]:
    labels = make_labels(data_dir)
    body = "".join(r.model_dump_json() + "\n" for r in labels)
    (data_dir / "labels.jsonl").write_text(body, encoding="utf-8", newline="\n")
    parts = split(labels)
    text = json.dumps({"seed": SEED, "test_fraction": TEST_FRACTION, **parts}, indent=2)
    (data_dir / "split.json").write_text(text + "\n", encoding="utf-8", newline="\n")
    update_manifest(data_dir)
    return labels, parts


# --- hand labels ------------------------------------------------------------------------


def pick_hand_items(judgments: Sequence[Judgment], n: int = HAND_ITEMS) -> list[tuple[str, Tier]]:
    """n (prompt, tier) answers, spread evenly over tiers and over judge pass/fail, so the
    check covers the decisions that matter, not just the easy passes."""
    groups: dict[tuple[str, bool], list[tuple[str, Tier]]] = defaultdict(list)
    for j in judgments:
        if j.via == "batch":  # rule-scored empty answers need no human check
            groups[(j.tier, j.score >= PASS_SCORE)].append((j.id, j.tier))
    queues = []
    for key in sorted(groups):
        items = sorted(groups[key])
        random.Random(f"{SEED}:hand:{key[0]}:{key[1]}").shuffle(items)
        queues.append(items)
    chosen: list[tuple[str, Tier]] = []
    while len(chosen) < n and any(queues):
        for q in queues:
            if q and len(chosen) < n:
                chosen.append(q.pop(0))
    random.Random(f"{SEED}:hand:order").shuffle(chosen)  # so tiers don't come in blocks
    return chosen


def export_hand(data_dir: Path) -> Path:
    prompts = {p.id: p for p in load_prompts(data_dir)}
    answers = {(a.id, a.tier): a for a in load_answers(data_dir)}
    items = pick_hand_items(load_judgments(data_dir))
    out = data_dir / "hand_labels"
    out.mkdir(exist_ok=True)

    lines = [
        "# Hand labels: score each answer 1-5",
        "",
        "Use the same scale as the judge (prompts/judge_v1.md). A 4 or 5 means you would",
        "send the answer to the user without changes. You are not told which model wrote",
        "each answer, on purpose. Put your scores in `scores.csv` (column `score`).",
        "",
    ]
    for n, (pid, tier) in enumerate(items, 1):
        p, a = prompts[pid], answers[(pid, tier)]
        lines += [
            f"---\n\n## Item {n}  ({p.task_type})",
            "",
            "**Request**",
            "",
            "````",
            p.prompt,
            "````",
            "",
        ]
        if p.reference:
            lines += [f"**Reference ({p.reference_kind})**", "", "````", p.reference, "````", ""]
        lines += ["**Answer**", "", "````", a.text, "````", ""]
    (out / "items.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")

    with (out / "scores.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["item", "score", "notes"])
        w.writerows([n, "", ""] for n in range(1, len(items) + 1))
    key = [{"item": n, "id": pid, "tier": tier} for n, (pid, tier) in enumerate(items, 1)]
    (out / "key.json").write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8", newline="\n")
    return out


# --- agreement ----------------------------------------------------------------------------


def cohen_kappa(a: Sequence[object], b: Sequence[object]) -> float:
    """Agreement beyond chance between two raters on the same items."""
    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n
    ca, cb = Counter(a), Counter(b)
    expected = sum(ca[c] * cb[c] for c in set(a) | set(b)) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def weighted_kappa(a: Sequence[int], b: Sequence[int], k: int = 5) -> float:
    """Quadratic-weighted kappa on 1..k scores: a 4-vs-5 disagreement costs far less
    than a 1-vs-5 one."""
    n = len(a)
    ca, cb = Counter(a), Counter(b)
    num = sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) / n
    den = sum(ca[i] * cb[j] * (i - j) ** 2 for i in range(1, k + 1) for j in range(1, k + 1))
    den /= n * n
    return 1.0 if den == 0 else 1 - num / den


def read_csv_text(path: Path) -> str:
    """Excel on Windows saves "CSV" in the cp1252 code page, not UTF-8, so curly quotes
    and symbols in notes would crash a UTF-8 read. Try UTF-8 first, then cp1252."""
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def agreement(data_dir: Path) -> dict[str, object]:
    out = data_dir / "hand_labels"
    key = {row["item"]: row for row in json.loads((out / "key.json").read_text(encoding="utf-8"))}
    judged = {(j.id, j.tier): j.score for j in load_judgments(data_dir)}
    human, judge = [], []
    for row in csv.DictReader(read_csv_text(out / "scores.csv").splitlines()):
        if not row["score"].strip():
            continue
        score = int(row["score"])
        if not 1 <= score <= 5:
            raise ValueError(f"item {row['item']}: score must be 1-5, got {score}")
        k = key[int(row["item"])]
        human.append(score)
        judge.append(judged[(k["id"], k["tier"])])
    if not human:
        raise ValueError("no scores filled in yet")
    h_pass = [s >= PASS_SCORE for s in human]
    j_pass = [s >= PASS_SCORE for s in judge]
    pairs = list(zip(human, judge, strict=True))
    report = {
        "items": len(human),
        "pass_fail_agreement": round(
            sum(x == y for x, y in zip(h_pass, j_pass, strict=True)) / len(human), 3
        ),
        "pass_fail_kappa": round(cohen_kappa(h_pass, j_pass), 3),
        "score_weighted_kappa": round(weighted_kappa(human, judge), 3),
        "judge_stricter": sum(j < h for h, j in pairs),
        "judge_more_lenient": sum(j > h for h, j in pairs),
    }
    (out / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="py -m gearbox.data.labels",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--data", type=Path, default=Path("data/v1"))
    parser.add_argument("cmd", choices=["build", "export-hand", "kappa"])
    args = parser.parse_args(argv)

    if args.cmd == "build":
        labels, parts = build(args.data)
        print(f"{len(labels)} labels; train {len(parts['train'])}, test {len(parts['test'])}")
        counts = Counter(r.label for r in labels)
        print("label mix:", {t: counts[t] for t in TIERS})
        for task_type in sorted({r.task_type for r in labels}):
            c = Counter(r.label for r in labels if r.task_type == task_type)
            print(f"  {task_type:<10}", {t: c[t] for t in TIERS})
    elif args.cmd == "export-hand":
        out = export_hand(args.data)
        print(f"wrote {out / 'items.md'} and {out / 'scores.csv'}")
    else:
        for k, v in agreement(args.data).items():
            print(f"{k:<22} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
