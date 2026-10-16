"""In-process enterprise MCP servers for local development and tests.

They speak the real MCP protocol (``mcp.server.mcpserver.MCPServer``) so the governed client path is
identical to production; in Azure the same tool names are served by the enterprise Git / Azure DevOps MCP
servers behind private endpoints.
"""
from __future__ import annotations

import json
import threading

from mcp.server.mcpserver import MCPServer

from ingestion.evidence_store import EvidenceReader


def build_git_server(reader: EvidenceReader) -> MCPServer:
    srv = MCPServer("git")

    @srv.tool()
    def read_file(uri: str, line_start: int = 1, line_end: int = 0) -> str:
        """Read lines of a versioned artefact (repository content for the run)."""
        lines = reader.lines(uri)
        end = line_end or len(lines)
        return "\n".join(lines[max(0, line_start - 1):end])

    return srv


def build_devops_server(objects, container: str) -> MCPServer:
    """Local stand-in for the Azure DevOps MCP server: work items are kept in the object store."""
    srv = MCPServer("devops")
    lock = threading.Lock()
    key = "devops/work_items.json"

    @srv.tool()
    def create_work_item(title: str, description: str, work_item_type: str = "User Story",
                         approval_ref: str = "") -> dict:
        """Create an Azure DevOps work item (requires a human approval reference)."""
        from storage.objects import make_uri
        with lock:
            uri = make_uri(container, key)
            items = json.loads(objects.get_bytes(uri).decode("utf-8")) if objects.exists(uri) else []
            item = {"id": len(items) + 1, "title": title, "type": work_item_type, "description": description,
                    "approval_ref": approval_ref}
            items.append(item)
            objects.put_bytes(container, key, json.dumps(items, indent=2).encode("utf-8"))
        return {"id": item["id"], "url": f"devops://workitems/{item['id']}"}

    return srv
