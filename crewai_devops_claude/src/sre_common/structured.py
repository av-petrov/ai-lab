"""Read a task's structured output inside a CrewAI guardrail.

CrewAI (1.15) runs guardrails *before* it exports ``output_pydantic`` on the first attempt, so
``output.pydantic`` is None there and a guardrail that only checks it rejects every first answer. Each
rejection re-runs the whole task (all tool and LLM calls again). Parse ``output.raw`` instead.
"""

from __future__ import annotations

import re
from typing import Any, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def structured_output(output: Any, model: type[M]) -> M | None:
    """``output.pydantic`` if it is a ``model``, else ``output.raw`` parsed as ``model`` (None if invalid).

    Tolerates a ```json fence or text around the JSON object.
    """
    obj = getattr(output, "pydantic", None)
    if isinstance(obj, model):
        return obj
    raw = getattr(output, "raw", output if isinstance(output, str) else None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    m = _FENCE.match(raw)
    text = m.group(1) if m else raw
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            return model.model_validate_json(c)
        except ValueError:
            continue
    return None
