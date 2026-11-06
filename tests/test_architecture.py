import ast
import sys
from pathlib import Path

import pytest

from tests.conftest import ROOT

SRC = ROOT / "src" / "vigisift"


ENTRYPOINTS = ("vigisift.app", "vigisift.bootstrap", "vigisift.mcp_server")


FORBIDDEN = {
    "domain": ("vigisift.",),
    "ports": ("vigisift.features", "vigisift.infra", "vigisift.runtime", *ENTRYPOINTS),
    "features": ("vigisift.infra", *ENTRYPOINTS),
    "infra": ("vigisift.features", "vigisift.runtime", "vigisift.dependencies", *ENTRYPOINTS),
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_layers_only_depend_inward(layer: str) -> None:
    allowed_self = f"vigisift.{layer}"
    violations = [
        f"{path.relative_to(SRC)} imports {name}"
        for path in sorted((SRC / layer).rglob("*.py"))
        for name in _imports(path)
        if name.startswith(FORBIDDEN[layer]) and not name.startswith(allowed_self)
    ]
    assert violations == []


def test_domain_is_pure_python() -> None:
    third_party = [
        f"{path.name} imports {name}"
        for path in sorted((SRC / "domain").rglob("*.py"))
        for name in _imports(path)
        if name.split(".")[0] not in sys.stdlib_module_names and not name.startswith("vigisift.")
    ]
    assert third_party == []
