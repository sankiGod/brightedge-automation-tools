"""
Zendesk MCP Server — sidecar service wrapping tools/zendesk.py.

Exposes Zendesk read operations as MCP tools so the Claude orchestrator
can interactively query ticket data during its reasoning phase.

Tools exposed:
  - get_ticket(ticket_id)         → ticket context JSON (no auth tuple)
  - get_ticket_comments(ticket_id) → structured comment list

Does NOT handle:
  - Attachment binary downloads (use tools/attachment.py for that)
  - Posting replies (done directly via tools/zendesk.py after skill execution)

Run as a subprocess (stdio transport). The orchestrator spawns it automatically.

Usage (manual):
    python mcp/zendesk_server.py
"""

import asyncio
import json
import os
import sys
import requests

# Make tools/ importable when run as a subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server
from mcp.server.models import InitializationOptions

# Zendesk config — passed via env by orchestrator
ZENDESK_SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
ZENDESK_EMAIL     = os.environ["ZENDESK_EMAIL"]
ZENDESK_API_TOKEN = os.environ["ZENDESK_API_TOKEN"]

server = Server("zendesk")


def _base():
    return f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"


def _auth():
    return (f"{ZENDESK_EMAIL}/token", ZENDESK_API_TOKEN)


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_ticket",
            description=(
                "Fetch a Zendesk ticket. Returns subject, full body text "
                "(credentials internal note first), and list of keyword file attachments."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The Zendesk ticket ID",
                    }
                },
                "required": ["ticket_id"],
            },
        ),
        types.Tool(
            name="get_ticket_comments",
            description=(
                "Get all comments on a Zendesk ticket as a structured list. "
                "Each comment includes body text, public/internal flag, and whether "
                "it has attachments. Use to inspect individual comment ordering."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "The Zendesk ticket ID",
                    }
                },
                "required": ["ticket_id"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:
    ticket_id = arguments["ticket_id"]

    if name == "get_ticket":
        result = _get_ticket(ticket_id)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "get_ticket_comments":
        result = _get_ticket_comments(ticket_id)
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    else:
        raise ValueError(f"Unknown tool: {name}")


def _get_ticket(ticket_id: str) -> dict:
    """Wraps tools/zendesk.fetch_ticket() but strips the auth tuple."""
    # Import here to avoid circular issues when running as subprocess
    from tools.zendesk import fetch_ticket

    ticket = fetch_ticket(ticket_id)
    if ticket is None:
        return {"error": "Ticket not found or subject does not match 'keyword upload'"}

    # Strip auth tuple — never expose credentials in MCP response
    safe = {k: v for k, v in ticket.items() if k != "auth"}
    return safe


def _get_ticket_comments(ticket_id: str) -> list:
    """Returns structured comment list for the orchestrator to inspect."""
    resp = requests.get(
        f"{_base()}/tickets/{ticket_id}/comments.json",
        auth=_auth(),
        timeout=15,
    )
    resp.raise_for_status()
    comments = resp.json().get("comments", [])

    return [
        {
            "id":              c.get("id"),
            "body":            c.get("body", "").strip(),
            "public":          c.get("public", True),
            "created_at":      c.get("created_at"),
            "has_attachments": len(c.get("attachments", [])) > 0,
            "attachment_names": [
                a["file_name"] for a in c.get("attachments", [])
            ],
        }
        for c in comments
    ]


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="zendesk",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=None,
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
