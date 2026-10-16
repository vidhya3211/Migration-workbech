"""Unsafe generated-code guard: generated transformation code is untrusted until it passes this."""
from __future__ import annotations

import ast
from dataclasses import dataclass

from guardrails.pipeline import GuardrailContext, Stage, block, ok
from schemas.execution import GuardrailResult

ALLOWED_IMPORTS = frozenset({"decimal", "datetime", "re", "typing", "dataclasses", "math", "pytest"})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals", "vars",
                             "getattr", "setattr", "delattr", "breakpoint", "memoryview"})


@dataclass
class CodeSubject:
    source: str
    label: str


def unsafe_findings(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                if name.split(".")[0] not in ALLOWED_IMPORTS and not name.startswith("generated_"):
                    out.append(f"import of {name!r} not allowed (line {node.lineno})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            out.append(f"call to {node.func.id}() not allowed (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in ("__name__",):
            out.append(f"dunder attribute access {node.attr!r} not allowed (line {node.lineno})")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.append(f"global/nonlocal not allowed (line {node.lineno})")
    return out


class UnsafeCodePolicy:
    name = "unsafe_generated_code"
    stages = frozenset({Stage.OUTPUT_SCHEMA})

    def check(self, subject: CodeSubject, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        findings = unsafe_findings(subject.source)
        if findings:
            return block(stage, self.name, f"{subject.label}: " + "; ".join(findings[:5]))
        return ok(stage, self.name, f"{subject.label}: static safety checks passed")
