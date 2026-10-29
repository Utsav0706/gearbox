"""Dataset v2 = v1's 450 everyday prompts + a 150-prompt hard slice (probe.HARD_PICKS).

Why: on v1, Haiku (M) passed as often as Sonnet (L), so no router could earn its keep
by ever picking L. The probe found where Sonnet is worth its price (niche facts, very
hard maths). v2 adds those, so the labels contain real "only L passes" cases to learn.

    py -m gearbox.data.v2       # writes data/v2/prompts.jsonl, reusing paid results

Nothing already paid for is paid for again: answers and judgments for prompts that
exist in data/v1 or data/probe are copied in (same model, same max_tokens, same judge
and rubric). The normal tools then only fill the gaps, because they skip anything done:

    py -m gearbox.data.answer --data data/v2 local --tier S
    py -m gearbox.data.answer --data data/v2 submit --tier M --yes   (and --tier L)
    py -m gearbox.data.judge  --data data/v2 submit --yes
    py -m gearbox.data.labels --data data/v2 build
"""

from __future__ import annotations

import sys
from pathlib import Path

from gearbox.data.answer import load_answers, save_answers
from gearbox.data.build import build
from gearbox.data.judge import load_judgments, save_judgments
from gearbox.data.probe import HARD_PICKS
from gearbox.data.sources import PICKS

REUSE_FROM = [Path("data/v1"), Path("data/probe")]


def build_v2(out: Path, reuse_from: list[Path] = REUSE_FROM) -> dict[str, int]:
    items = build(out, picks=PICKS + HARD_PICKS)
    ids = {i.id for i in items}
    answers = [a for d in reuse_from for a in load_answers(d) if a.id in ids]
    judgments = [j for d in reuse_from for j in load_judgments(d) if j.id in ids]
    save_answers(out, answers)
    save_judgments(out, judgments)
    return {
        "prompts": len(items),
        "answers_reused": len(answers),
        "judgments_reused": len(judgments),
    }


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else Path("data/v2")
    counts = build_v2(out)
    print(f"{counts['prompts']} prompts -> {out / 'prompts.jsonl'}")
    print(
        f"reused {counts['answers_reused']} answers and "
        f"{counts['judgments_reused']} judgments from {[str(p) for p in REUSE_FROM]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
