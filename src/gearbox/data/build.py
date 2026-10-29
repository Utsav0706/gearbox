"""Build data/<version>/prompts.jsonl from the public sources in sources.py (free).

    py -m gearbox.data.build            # writes data/v1/prompts.jsonl + manifest.json

Reproducible: every source file is pinned to a commit and every random draw is seeded,
so rebuilding gives byte-identical output (manifest.json records its sha256). Needs the
optional "data" extra (pip install -e ".[data]"); downloads ~18 MB into the Hugging Face
cache on first run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gearbox.data.schema import PromptItem
from gearbox.data.sources import PICKS, Pick, SourceFile

SEED = 20261029
Loader = Callable[[SourceFile], list[dict[str, Any]]]


def load_rows(source: SourceFile) -> list[dict[str, Any]]:
    """Download (or reuse the cached copy of) one pinned dataset file."""
    # Imported here so the rest of Gearbox (and CI) doesn't need the "data" extra.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(source.repo, source.file, repo_type="dataset", revision=source.revision)
    if source.file.endswith(".parquet"):
        import pyarrow.parquet as pq

        return pq.read_table(path).to_pylist()
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def select(
    pick: Pick, rows: list[dict[str, Any]], seen: set[str], seed: int = SEED
) -> tuple[list[PromptItem], int]:
    """Up to pick.n items from rows, skipping prompts already in `seen`.
    Returns (items, number of usable rows) so the manifest can show the pool size."""
    candidates = []
    for i, row in enumerate(rows):
        item = pick.convert(row, i)
        if item is not None and _norm(item.prompt) not in seen:
            candidates.append(item)
    usable = len(candidates)

    # A string seed per pick: changing one quota doesn't reshuffle every other source.
    random.Random(f"{seed}:{pick.name}").shuffle(candidates)
    if pick.strata is not None:
        groups: dict[str, list[PromptItem]] = defaultdict(list)
        for item in candidates:
            groups[pick.strata(item)].append(item)
        queues = [groups[k] for k in sorted(groups)]
        candidates = []
        while any(queues):
            for q in queues:
                if q:
                    candidates.append(q.pop(0))

    chosen: list[PromptItem] = []
    for item in candidates:
        if pick.n is not None and len(chosen) == pick.n:
            break
        key = _norm(item.prompt)
        if key in seen:  # duplicate inside the same source
            continue
        seen.add(key)
        chosen.append(item)
    if pick.n is not None and len(chosen) < pick.n:
        raise ValueError(f"{pick.name}: wanted {pick.n} prompts, only {len(chosen)} usable")
    return chosen, usable


def build(out_dir: Path, loader: Loader = load_rows, seed: int = SEED) -> list[PromptItem]:
    cache: dict[SourceFile, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    items: list[PromptItem] = []
    sources = []
    for pick in PICKS:
        if pick.source not in cache:
            cache[pick.source] = loader(pick.source)
        chosen, usable = select(pick, cache[pick.source], seen, seed)
        items.extend(chosen)
        sources.append(
            {
                "pick": pick.name,
                "repo": pick.source.repo,
                "revision": pick.source.revision,
                "file": pick.source.file,
                "licence": pick.source.licence,
                "taken": len(chosen),
                "usable": usable,
            }
        )

    ids = Counter(item.id for item in items)
    dupes = [i for i, n in ids.items() if n > 1]
    if dupes:
        raise ValueError(f"duplicate ids: {dupes[:5]}")

    out_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(item.model_dump_json() + "\n" for item in items).encode("utf-8")
    (out_dir / "prompts.jsonl").write_bytes(body)
    manifest = {
        "version": out_dir.name,
        "seed": seed,
        "prompts_sha256": hashlib.sha256(body).hexdigest(),
        "total": len(items),
        "by_task_type": dict(sorted(Counter(i.task_type for i in items).items())),
        "by_difficulty": {
            t: dict(sorted(Counter(i.difficulty for i in items if i.task_type == t).items()))
            for t in sorted({i.task_type for i in items})
        },
        "with_reference": sum(i.reference is not None for i in items),
        "sources": sources,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return items


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="py -m gearbox.data.build", description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("data/v1"))
    args = parser.parse_args(argv)

    items = build(args.out)
    manifest = json.loads((args.out / "manifest.json").read_text(encoding="utf-8"))
    print(f"{len(items)} prompts -> {args.out / 'prompts.jsonl'}")
    for task_type, counts in manifest["by_difficulty"].items():
        print(f"  {task_type:<10} {manifest['by_task_type'][task_type]:>4}  {counts}")
    print(f"  with a reference answer: {manifest['with_reference']}")
    print(f"  sha256 {manifest['prompts_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
