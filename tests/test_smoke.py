"""Import smoke tests for core app modules."""
import importlib
import pytest

CORE = ["app", "app.config", "app.models", "app.planner", "app.shackle"]
# engine/server import playwright/fastapi; skip cleanly if not installed in a
# minimal test env.
OPTIONAL = ["app.engine", "app.server"]


@pytest.mark.parametrize("m", CORE)
def test_core_imports(m):
    importlib.import_module(m)


@pytest.mark.parametrize("m", OPTIONAL)
def test_optional_imports(m):
    try:
        importlib.import_module(m)
    except ModuleNotFoundError:
        pytest.skip(f"{m} deps not installed")
