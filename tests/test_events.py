import re

from tests.conftest import ROOT
from vigisift.domain.events import EventType

INDEX_HTML = ROOT / "src" / "vigisift" / "web" / "index.html"


def test_the_console_handles_every_event_type_and_no_other() -> None:
    handled = set(re.findall(r'event\.type === "([a-z_]+)"', INDEX_HTML.read_text()))
    assert handled == {event.value for event in EventType}
