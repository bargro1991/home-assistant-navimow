"""Shared test setup.

These tests exercise custom_components/navimow/coordinator.py in isolation,
without requiring a full `homeassistant` install (which is large and not
needed to validate this logic). We register minimal stand-ins for the two
homeassistant symbols coordinator.py imports, and load coordinator.py
directly by file path so importing it doesn't trigger navimow/__init__.py
(which pulls in aiohttp/config_entries wiring we don't need here).
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NAVIMOW_DIR = REPO_ROOT / "custom_components" / "navimow"


def _install_ha_stubs() -> None:
    if "homeassistant.helpers.update_coordinator" in sys.modules:
        return

    ha = types.ModuleType("homeassistant")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")

    class UpdateFailed(Exception):
        """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""

    class DataUpdateCoordinator:
        """Minimal stand-in exposing only what coordinator.py touches."""

        def __init__(self, hass, logger, name=None, update_interval=None):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None

        async def async_set_updated_data(self, data):
            self.data = data

    class ConfigEntryAuthFailed(Exception):
        """Stand-in for homeassistant.exceptions.ConfigEntryAuthFailed."""

    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = UpdateFailed
    ha_exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.exceptions"] = ha_exceptions


def _load_coordinator_module():
    _install_ha_stubs()

    # Register a fake parent package "navimow" pointing at the real
    # custom_components/navimow directory, so coordinator.py's
    # `from .const import DOMAIN` resolves to the real const.py, without
    # executing the real navimow/__init__.py.
    if "navimow" not in sys.modules:
        pkg = types.ModuleType("navimow")
        pkg.__path__ = [str(NAVIMOW_DIR)]
        sys.modules["navimow"] = pkg

    spec = importlib.util.spec_from_file_location(
        "navimow.coordinator", NAVIMOW_DIR / "coordinator.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["navimow.coordinator"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def coordinator_module():
    return _load_coordinator_module()
