"""Structured-output parsing: model text -> validated Pydantic object, or an explicit failure."""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(ValueError):
    pass


def extract_json(text: str) -> object:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if not m:
            raise StructuredOutputError("no JSON object found in model output") from None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"invalid JSON: {exc.msg}") from None


def parse_structured(text: str, model: type[T]) -> T:
    try:
        return model.model_validate(extract_json(text))
    except ValidationError as exc:
        raise StructuredOutputError(f"output does not match {model.__name__}: {exc.error_count()} error(s)") from exc
