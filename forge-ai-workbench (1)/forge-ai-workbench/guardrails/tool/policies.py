"""Tool and MCP call guardrails: authorisation against the agent identity + argument sanity."""
from __future__ import annotations

from dataclasses import dataclass

from blackboard.control_plane import ControlPlaneViolation, validate_tool
from guardrails.pipeline import GuardrailContext, Stage, block, ok
from schemas.execution import GuardrailResult


@dataclass
class ToolCall:
    tool: str
    arguments: dict
    mcp: bool = False


class ToolAuthorizationPolicy:
    name = "tool_authorization"
    stages = frozenset({Stage.TOOL, Stage.MCP})

    def check(self, subject: ToolCall, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        try:
            validate_tool(ctx.agent, subject.tool, mcp=subject.mcp)
        except ControlPlaneViolation as exc:
            return block(stage, self.name, str(exc))
        return ok(stage, self.name, f"{ctx.agent} may call {subject.tool}")


class ToolArgumentPolicy:
    """Rejects path traversal and oversized arguments before any tool executes."""

    name = "tool_arguments"
    stages = frozenset({Stage.TOOL, Stage.MCP})
    max_chars = 20000

    def check(self, subject: ToolCall, ctx: GuardrailContext, stage: Stage) -> GuardrailResult:
        for key, value in subject.arguments.items():
            text = str(value)
            if len(text) > self.max_chars:
                return block(stage, self.name, f"argument {key!r} exceeds {self.max_chars} chars")
            if key in ("path", "file", "uri") and (".." in text.replace("\\", "/").split("/") or text.startswith(("/", "\\"))):
                return block(stage, self.name, f"argument {key!r} is not a safe relative path")
        return ok(stage, self.name)
