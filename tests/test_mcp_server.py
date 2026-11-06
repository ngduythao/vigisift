import json
from collections.abc import AsyncIterator

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes.ports import FakeMqttClient, FakePresence
from vigisift.features.assistant.tools import Toolbox
from vigisift.mcp_server import build_server
from vigisift.runtime import Runtime


@pytest.fixture
async def server(runtime: Runtime) -> AsyncIterator[MCPServer]:
    yield build_server(Toolbox(runtime.session, FakePresence(), runtime.confirmations))


async def test_the_same_tools_are_exposed(server: MCPServer) -> None:
    names = {tool.name for tool in await server.list_tools()}

    assert names == {"list_devices", "get_recent_alerts", "send_command"}


async def test_reading_devices_goes_through_the_shared_toolbox(server: MCPServer) -> None:
    result = await server.call_tool("list_devices", {})

    devices = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert {d["id"] for d in devices} == {"lamp-1", "fan-1", "plug-1"}


async def test_an_mcp_command_still_needs_a_human(server: MCPServer, mqtt: FakeMqttClient) -> None:
    result = await server.call_tool("send_command", {"device_id": "lamp-1", "action": "OFF"})

    assert json.loads(result.content[0].text)["status"] == "needs_confirmation"  # type: ignore[union-attr]
    assert mqtt.published == [], "MCP doesn't control devices by itself either"


async def test_tool_failures_surface_as_tool_errors(server: MCPServer) -> None:
    with pytest.raises(ToolError):
        await server.call_tool("send_command", {"device_id": "ghost", "action": "ON"})
