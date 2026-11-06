import pytest

from tests.fakes.ports import FakePresence
from vigisift.features.assistant.tools import Toolbox
from vigisift.runtime import Runtime


@pytest.fixture
def toolbox(runtime: Runtime) -> Toolbox:
    return Toolbox(runtime.session, FakePresence(), runtime.confirmations)
