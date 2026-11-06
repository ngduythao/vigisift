import json

from tests.fakes.ports import FakePresence
from tests.features.alerts.frigate import lifecycle
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.features.assistant.tools import Toolbox
from vigisift.runtime import Runtime


async def test_an_unknown_tool_is_an_error_not_a_crash(toolbox: Toolbox) -> None:
    outcome = await toolbox.run("rm_rf", {})

    assert outcome.is_error


async def test_list_devices_reports_heartbeat_separately_from_state(runtime: Runtime) -> None:
    presence = FakePresence()
    await presence.touch("lamp-1")
    tools = Toolbox(runtime.session, presence, runtime.confirmations)

    devices = {d["id"]: d for d in json.loads((await tools.run("list_devices", {})).content)}

    assert devices["lamp-1"]["online"] is True
    assert devices["fan-1"]["online"] is False, "OFF in the DB doesn't mean it's alive"


async def test_recent_alerts_are_readable_by_the_assistant(
    toolbox: Toolbox, runtime: Runtime
) -> None:
    async with runtime.session() as session:
        await repository.record(session, parse_review_message("demo", lifecycle("1.0-a")["genai"]))

    outcome = await toolbox.run("get_recent_alerts", {"min_threat_level": 1, "since_minutes": 120})

    [alert] = json.loads(outcome.content)
    assert (alert["site_id"], alert["threat_level"]) == ("demo", 1)


async def test_alert_arguments_are_bounded(toolbox: Toolbox) -> None:
    outcome = await toolbox.run("get_recent_alerts", {"since_minutes": 99999})

    assert outcome.is_error
