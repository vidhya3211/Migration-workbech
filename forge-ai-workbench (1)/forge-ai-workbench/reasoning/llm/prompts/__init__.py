"""Versioned prompt registry. A prompt change changes its version, which is audited and triggers evaluation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    template: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256((self.system + self.template).encode("utf-8")).hexdigest()[:12]

    def render(self, **values: object) -> str:
        out = self.template
        for key, value in values.items():
            out = out.replace("{" + key + "}", str(value))
        return out


@lru_cache(maxsize=None)
def get_prompt(prompt_id: str) -> Prompt:
    data = yaml.safe_load((HERE / f"{prompt_id}.yaml").read_text(encoding="utf-8"))
    return Prompt(id=data["id"], version=str(data["version"]), system=data["system"], template=data["template"])


def all_prompts() -> list[Prompt]:
    return [get_prompt(p.stem) for p in sorted(HERE.glob("*.yaml"))]
