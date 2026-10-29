"""Pick the tier S model: run the same 20 prompts on each local candidate (free).

Measures what matters for a laptop tier: speed, whether the model fits fully in GPU
memory (if not, it spills into system RAM and slows down), and how often answers are
cut off. Answers are saved so their quality can be read side by side; the proper
quality comparison comes from the judge in M4.

    py scripts/bench_tier_s.py qwen3:4b llama3.2:3b
Writes runs/bench_tier_s/<model>.jsonl and prints a summary table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

import httpx

from gearbox.config import ModelPrice, get_config
from gearbox.log import setup_logging
from gearbox.tiers import TierError, call_tier

# 5 per task type, easy to hard. GSM-style maths has a known answer to check by eye.
PROMPTS: list[tuple[str, str]] = [
    ("coding", "Write a Python function that returns True if a string is a palindrome."),
    ("coding", "What does `git rebase` do, in two sentences?"),
    ("coding", "Fix the bug: `def avg(xs): return sum(xs) / len(xs)` crashes on []."),
    (
        "coding",
        "Write a SQL query for the 3 customers with the highest total order value, "
        "given tables customers(id, name) and orders(id, customer_id, amount).",
    ),
    ("coding", "Implement an LRU cache class in Python with get and put in O(1)."),
    ("research", "What is the capital of Australia?"),
    ("research", "Who wrote 'Pride and Prejudice', and in which year was it published?"),
    ("research", "Explain the difference between TCP and UDP in three bullet points."),
    ("research", "What causes the seasons on Earth?"),
    ("research", "Summarise the main causes of the 2008 financial crisis in one paragraph."),
    ("writing", "Write a one-line subject for an email asking my manager for Friday off."),
    ("writing", "Rewrite more politely: 'Send me the report now.'"),
    ("writing", "Write a 4-line poem about a delivery rider in the rain."),
    ("writing", "Write a 100-word product description for a reusable water bottle."),
    ("writing", "Write a short cover letter opening paragraph for a junior AI engineer role."),
    ("reasoning", "What is 17 * 23?"),
    ("reasoning", "A shirt costs £20 after a 20% discount. What was the original price?"),
    (
        "reasoning",
        "If all bloops are razzies and all razzies are lazzies, are all bloops "
        "lazzies? Explain briefly.",
    ),
    (
        "reasoning",
        "Natalia sold clips to 48 friends in April, then half as many in May. "
        "How many clips did she sell altogether?",
    ),
    (
        "reasoning",
        "A bat and a ball cost £1.10 in total. The bat costs £1.00 more than the "
        "ball. How much does the ball cost? Show your reasoning.",
    ),
]


def gpu_share(api_base: str, model: str) -> float | None:
    """Fraction of the loaded model that sits in GPU memory (1.0 = fully on GPU)."""
    try:
        loaded = httpx.get(f"{api_base}/api/ps", timeout=5).json().get("models", [])
    except httpx.HTTPError:
        return None
    for m in loaded:
        if m.get("name") == model or m.get("model") == model:
            return m["size_vram"] / m["size"] if m.get("size") else None
    return None


async def bench(model: str, params: dict, out_dir: Path) -> dict:
    config = get_config().model_copy(deep=True)
    tier = config.tiers["S"]
    tier.model = f"ollama_chat/{model}"
    tier.params = params
    config.prices_usd_per_mtok[tier.model] = ModelPrice(input=0, output=0)

    # Warm-up: the first call loads the model into memory, which would skew latency.
    await call_tier("S", [{"role": "user", "content": "hi"}], config=config, max_tokens=5)
    share = gpu_share(tier.api_base or "http://localhost:11434", model)

    rows = []
    for task_type, prompt in PROMPTS:
        try:
            r = await call_tier("S", [{"role": "user", "content": prompt}], config=config)
        except TierError as exc:
            rows.append({"task_type": task_type, "prompt": prompt, "error": str(exc)})
            continue
        rows.append(
            {
                "task_type": task_type,
                "prompt": prompt,
                "answer": r.text,
                "finish_reason": r.finish_reason,
                "out_tokens": r.usage.output_tokens,
                "latency_ms": r.latency_ms,
            }
        )

    path = out_dir / f"{model.replace(':', '_')}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    ok = [r for r in rows if "error" not in r]
    lat = [r["latency_ms"] / 1000 for r in ok]
    return {
        "model": model,
        "gpu_share": share,
        "errors": len(rows) - len(ok),
        "truncated": sum(r["finish_reason"] == "length" for r in ok),
        "p50_s": statistics.median(lat) if lat else None,
        "max_s": max(lat) if lat else None,
        "tok_per_s": sum(r["out_tokens"] for r in ok) / sum(lat) if lat else None,
        "file": str(path),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="extra call option as key=json, e.g. think=false (applies to every model)",
    )
    args = parser.parse_args(argv)
    params = {k: json.loads(v) for k, v in (p.split("=", 1) for p in args.param)}

    setup_logging("WARNING")
    out_dir = Path("runs/bench_tier_s")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"{'model':<16}{'on GPU':>8}{'p50 s':>8}{'max s':>8}{'tok/s':>8}{'cut':>5}{'err':>5}")
    for model in args.models:
        s = asyncio.run(bench(model, params, out_dir))
        gpu = f"{s['gpu_share']:.0%}" if s["gpu_share"] is not None else "?"
        print(
            f"{s['model']:<16}{gpu:>8}{s['p50_s'] or 0:>8.1f}{s['max_s'] or 0:>8.1f}"
            f"{s['tok_per_s'] or 0:>8.1f}{s['truncated']:>5}{s['errors']:>5}"
        )
    print(f"answers in {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
