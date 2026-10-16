"""MCP registry: the allow-list. Only tools named in configuration are callable, whatever a server exposes."""
from __future__ import annotations

from config.settings import MCPSettings
from tools.mcp.client import MCPClient
from tools.mcp.models import MCPToolDefinition, MCPToolPolicy


class MCPRegistry:
    def __init__(self, settings: MCPSettings, inprocess_servers: dict[str, object] | None = None) -> None:
        self.settings = settings
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, MCPToolDefinition] = {}
        for server_name, srv in settings.servers.items():
            if not srv.enabled:
                continue
            self.clients[server_name] = MCPClient(server_name, srv, (inprocess_servers or {}).get(server_name))
            for qualified, tool_cfg in srv.tools.items():
                if not qualified.startswith(server_name + "."):
                    raise ValueError(f"tool {qualified} must be namespaced by its server '{server_name}'")
                self.tools[qualified] = MCPToolDefinition(
                    qualified_name=qualified, server=server_name, remote_name=qualified.split(".", 1)[1],
                    policy=MCPToolPolicy(allowed_agents=frozenset(tool_cfg.allowed_agents),
                                         data_classification=tool_cfg.data_classification,
                                         rate_limit_per_minute=tool_cfg.rate_limit_per_minute,
                                         timeout_seconds=tool_cfg.timeout_seconds,
                                         requires_human_approval=tool_cfg.requires_human_approval))

    def discover(self) -> dict[str, list[str]]:
        """Fetch remote input schemas for allow-listed tools; report exposed-but-not-allowed tools."""
        report: dict[str, list[str]] = {}
        for name, client in self.clients.items():
            remote = {t["name"]: t for t in client.list_tools()}
            for d in self.tools.values():
                if d.server == name and d.remote_name in remote:
                    self.tools[d.qualified_name] = d.model_copy(update={
                        "input_schema": remote[d.remote_name]["input_schema"] or {},
                        "description": remote[d.remote_name]["description"]})
            allowed = {d.remote_name for d in self.tools.values() if d.server == name}
            report[name] = sorted(set(remote) - allowed)
        return report

    def get(self, qualified: str) -> MCPToolDefinition | None:
        return self.tools.get(qualified)
