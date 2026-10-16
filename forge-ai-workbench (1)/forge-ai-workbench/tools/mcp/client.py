"""Synchronous bridge over the MCP SDK client (in-process, stdio or Streamable HTTP servers).

Agents run inside LangGraph nodes (sync, possibly on worker threads, possibly under a running event loop in
the API process), so each call runs the async MCP client on its own event loop in a dedicated thread,
bounded by the tool's timeout.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import dataclass
from typing import Any

from config.settings import MCPServerSettings, secret


class MCPTransportError(RuntimeError):
    retryable = True


class MCPTimeout(MCPTransportError):
    pass


@dataclass
class ToolCallOutput:
    is_error: bool
    text: str
    structured: dict | None


class MCPClient:
    def __init__(self, name: str, settings: MCPServerSettings, inprocess_server: Any = None) -> None:
        self.name = name
        self.settings = settings
        self.inprocess_server = inprocess_server
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"mcp-{name}")

    def _target(self) -> Any:
        if self.settings.transport == "inprocess":
            if self.inprocess_server is None:
                raise MCPTransportError(f"MCP server {self.name} has no in-process implementation registered")
            return self.inprocess_server
        if self.settings.transport == "stdio":  # pragma: no cover - environment specific
            from mcp.client.stdio import StdioServerParameters
            return StdioServerParameters(command=self.settings.command[0], args=self.settings.command[1:])
        return self.settings.url  # pragma: no cover - Streamable HTTP behind APIM / private endpoint

    def _headers(self) -> dict[str, str]:  # pragma: no cover - environment specific
        if self.settings.auth == "bearer":
            token = secret(f"AIWB_MCP_{self.name.upper()}_TOKEN")
            if not token:
                raise MCPTransportError(f"no bearer token configured for MCP server {self.name}")
            return {"Authorization": f"Bearer {token}"}
        if self.settings.auth == "managed_identity":
            from azure.identity import DefaultAzureCredential
            tok = DefaultAzureCredential().get_token(f"api://{self.name}/.default").token
            return {"Authorization": f"Bearer {tok}"}
        return {}

    async def _call_async(self, tool: str, arguments: dict, timeout: float) -> ToolCallOutput:
        from mcp import Client
        target = self._target()
        if isinstance(target, str) and self.settings.auth != "none":  # pragma: no cover
            import httpx
            from mcp.client.streamable_http import streamable_http_client
            target = streamable_http_client(target, http_client=httpx.AsyncClient(headers=self._headers()))
        async with Client(target, read_timeout_seconds=timeout) as client:
            res = await client.call_tool(tool, arguments, read_timeout_seconds=timeout)
        text = "\n".join(getattr(c, "text", "") for c in (res.content or []) if getattr(c, "type", "") == "text")
        return ToolCallOutput(bool(res.is_error), text, res.structured_content)

    def call(self, tool: str, arguments: dict, timeout: float) -> ToolCallOutput:
        future = self._pool.submit(asyncio.run, asyncio.wait_for(self._call_async(tool, arguments, timeout), timeout))
        try:
            return future.result(timeout=timeout + 2)
        except (concurrent.futures.TimeoutError, asyncio.TimeoutError) as exc:
            raise MCPTimeout(f"{self.name}.{tool} timed out after {timeout}s") from exc
        except MCPTransportError:
            raise
        except Exception as exc:
            raise MCPTransportError(f"{self.name}.{tool}: {type(exc).__name__}: {exc}") from exc

    def list_tools(self, timeout: float = 10) -> list[dict]:
        async def _list() -> list[dict]:
            from mcp import Client
            async with Client(self._target(), read_timeout_seconds=timeout) as client:
                res = await client.list_tools()
            return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                    for t in res.tools]
        return self._pool.submit(asyncio.run, _list()).result(timeout=timeout + 2)
