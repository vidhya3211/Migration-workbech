"""Built-in native tools: evidence access, deterministic expression evaluation, generated-code sandbox."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from ingestion.evidence_store import EvidenceReader
from reasoning.deterministic.expressions import ExpressionError, evaluate
from tools.native.registry import NativeToolRegistry, ToolSpec


class EvidenceReadIn(BaseModel):
    uri: str
    line_start: int = Field(ge=1, default=1)
    line_end: int | None = None


class EvidenceReadOut(BaseModel):
    text: str
    line_start: int
    line_end: int


class ExpressionIn(BaseModel):
    expression: str
    variables: dict[str, str] = Field(default_factory=dict)


class ExpressionOut(BaseModel):
    value: str | None
    error: str | None = None


class SandboxIn(BaseModel):
    module_name: str
    source: str
    tests: str
    timeout_seconds: float = 20


class SandboxOut(BaseModel):
    passed: bool
    returncode: int
    output: str


def run_generated_tests(module_name: str, source: str, tests: str, timeout: float = 20) -> SandboxOut:
    """Runs generated code + tests in an isolated subprocess (no network, temp dir, timeout).

    In Azure this executes in a locked-down sandbox pod; the contract is the same.
    """
    with tempfile.TemporaryDirectory(prefix="aiwb-sandbox-") as tmp:
        root = Path(tmp)
        (root / f"{module_name}.py").write_text(source, encoding="utf-8")
        (root / f"test_{module_name}.py").write_text(tests, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"test_{module_name}.py"],
                cwd=root, capture_output=True, text=True, timeout=timeout,
                env={"PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1", "SYSTEMROOT": _sysroot()},
            )
        except subprocess.TimeoutExpired:
            return SandboxOut(passed=False, returncode=-1, output="timeout")
        return SandboxOut(passed=proc.returncode == 0, returncode=proc.returncode,
                          output=(proc.stdout + proc.stderr)[-4000:])


def _sysroot() -> str:
    import os
    return os.environ.get("SYSTEMROOT", "")


def build_native_tools(reader: EvidenceReader) -> NativeToolRegistry:
    reg = NativeToolRegistry()

    def evidence_read(i: EvidenceReadIn) -> EvidenceReadOut:
        lines = reader.lines(i.uri)
        end = min(i.line_end or len(lines), len(lines))
        return EvidenceReadOut(text="\n".join(lines[i.line_start - 1:end]), line_start=i.line_start, line_end=end)

    def expression(i: ExpressionIn) -> ExpressionOut:
        try:
            return ExpressionOut(value=str(evaluate(i.expression, i.variables)))
        except ExpressionError as exc:
            return ExpressionOut(value=None, error=str(exc))

    def sandbox(i: SandboxIn) -> SandboxOut:
        return run_generated_tests(i.module_name, i.source, i.tests, i.timeout_seconds)

    reg.register(ToolSpec("evidence.read", "1.0", "Read lines of a raw artefact", EvidenceReadIn, EvidenceReadOut,
                          evidence_read))
    reg.register(ToolSpec("deterministic.expression", "1.0", "Evaluate an arithmetic expression exactly (Decimal)",
                          ExpressionIn, ExpressionOut, expression))
    reg.register(ToolSpec("sandbox.run_tests", "1.0", "Run generated code + tests in an isolated sandbox",
                          SandboxIn, SandboxOut, sandbox))
    return reg
