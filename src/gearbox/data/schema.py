"""One row of data/<version>/prompts.jsonl.

The spec's fields are {id, prompt, task_type, source}. The extras each earn their place:
- reference: a known answer, so the M4 judge grades against facts, not its own opinion.
  "gold" = the correct answer (maths, code, trivia); "example" = one good answer among
  many (creative writing), which the judge must not treat as the only right one.
- difficulty: a hint from the source (e.g. MATH level 5 = hard), used to check the mix.
  It is NOT the label: the label is the cheapest tier the judge passes (M4).
- licence: so anyone reusing the file knows the terms of each row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskType = Literal["coding", "research", "writing", "reasoning"]
Difficulty = Literal["easy", "medium", "hard"]
ReferenceKind = Literal["gold", "example"]


class PromptItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]+$")
    prompt: str = Field(min_length=1)
    task_type: TaskType
    source: str
    source_id: str
    difficulty: Difficulty
    reference: str | None = None
    reference_kind: ReferenceKind | None = None
    licence: str
