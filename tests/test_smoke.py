"""Smoke tests: ensure core application modules import cleanly.

These give CI a real signal on pull requests. They intentionally only import
modules that exist on the target branch, so new modules added by a PR (e.g.
app/shackle.py, app/agents.py) are exercised automatically once merged.
"""
import importlib

import pytest

CORE_MODULES = [
    "app",
    "app.config",
    "app.models",
    "app.engine",
    "app.planner",
    "app.server",
]

# Optional modules introduced by feature PRs. Imported when present.
OPTIONAL_MODULES = [
    "app.shackle",
    "app.agents",
]


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_imports(module_name):
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", OPTIONAL_MODULES)
def test_optional_module_imports(module_name):
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError:
        pytest.skip(f"{module_name} not present on this branch")
