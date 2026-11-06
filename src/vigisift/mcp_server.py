"""MCP server: expose the assistant's tools to external AI clients (Claude Code,
Claude Desktop...).

uv run python -m vigisift.mcp_server        # stdio

This is just one more inbound adapter, calling the same Toolbox the in-app agent
uses: same validation, same "commands need human confirmation" rule. Writing the
logic twice for MCP is the surest way for the two sides to drift apart.
"""

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from vigisift import bootstrap
from vigisift.config import load_settings
from vigisift.features.assistant.tools import TOOLS, Toolbox, ToolOutcome
from vigisift.infra.cache.confirmations import RedisConfirmations
from vigisift.infra.cache.presence import RedisPresence
from vigisift.infra.db.database import create_engine

# Same descriptions as the in-app agent: any model reads the same contract.
_DESCRIPTIONS = {tool["name"]: tool["description"] for tool in TOOLS}
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def _to_mcp(outcome: ToolOutcome) -> str:
    if outcome.is_error:
        raise ToolError(outcome.content)
    return outcome.content


def build_server(tools: Toolbox) -> MCPServer:
    server = MCPServer(
        "vigisift",
        instructions="Read devices and camera alerts across sites. On/off commands only create a "
        "request waiting for a person to confirm in the UI; no tool controls a device by itself.",
    )

    @server.tool(description=_DESCRIPTIONS["list_devices"], annotations=_READ_ONLY)
    async def list_devices() -> str:
        return _to_mcp(await tools.run("list_devices", {}))

    @server.tool(description=_DESCRIPTIONS["get_recent_alerts"], annotations=_READ_ONLY)
    async def get_recent_alerts(
        site_id: str | None = None,
        since_minutes: int = 60,
        min_threat_level: int | None = None,
        limit: int = 20,
    ) -> str:
        args = {
            "site_id": site_id,
            "since_minutes": since_minutes,
            "min_threat_level": min_threat_level,
            "limit": limit,
        }
        return _to_mcp(await tools.run("get_recent_alerts", args))

    @server.tool(
        description=_DESCRIPTIONS["send_command"],
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
    )
    async def send_command(device_id: str, action: Literal["ON", "OFF"]) -> str:
        return _to_mcp(await tools.run("send_command", {"device_id": device_id, "action": action}))

    return server


def main() -> None:
    settings = load_settings()
    redis = bootstrap.build_redis(settings)
    tools = Toolbox(
        bootstrap.build_sessions(create_engine(settings)),
        RedisPresence(redis, settings.presence_ttl_sec),
        RedisConfirmations(redis),
    )
    build_server(tools).run("stdio")


if __name__ == "__main__":
    main()
