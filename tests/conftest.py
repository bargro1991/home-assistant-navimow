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
    ha_config_entries = types.ModuleType("homeassistant.config_entries")
    ha_core = types.ModuleType("homeassistant.core")
    ha_components = types.ModuleType("homeassistant.components")
    ha_components_http = types.ModuleType("homeassistant.components.http")
    ha_helpers_network = types.ModuleType("homeassistant.helpers.network")
    ha_helpers_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")

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

    class ConfigFlow:
        """Minimal stand-in exposing only what config_flow.py touches."""

        def __init_subclass__(cls, domain=None, **kwargs):
            cls.domain = domain
            super().__init_subclass__(**kwargs)

        def __init__(self):
            self.hass = None
            self.context = {}
            self.flow_id = "test-flow-id"

        async def async_set_unique_id(self, unique_id):
            self.unique_id = unique_id

        def _abort_if_unique_id_configured(self):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_update_reload_and_abort(self, entry, **kwargs):
            return {"type": "update_reload_and_abort", "entry": entry, **kwargs}

    class HomeAssistant:
        """Stand-in for homeassistant.core.HomeAssistant (type-hint only)."""

    class HomeAssistantView:
        """Stand-in for homeassistant.components.http.HomeAssistantView."""

    def get_url(hass, prefer_external=False):
        raise NotImplementedError("tests must monkeypatch get_url")

    def async_get_clientsession(hass):
        raise NotImplementedError("tests must monkeypatch async_get_clientsession")

    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = UpdateFailed
    ha_exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    ha_config_entries.ConfigFlow = ConfigFlow
    ha_core.HomeAssistant = HomeAssistant
    ha_components_http.HomeAssistantView = HomeAssistantView
    ha_helpers_network.get_url = get_url
    ha_helpers_aiohttp_client.async_get_clientsession = async_get_clientsession

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.config_entries"] = ha_config_entries
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.http"] = ha_components_http
    sys.modules["homeassistant.helpers.network"] = ha_helpers_network
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_helpers_aiohttp_client


def _load_module_from_navimow(filename: str, module_name: str):
    _install_ha_stubs()

    # Register a fake parent package "navimow" pointing at the real
    # custom_components/navimow directory, so relative imports like
    # `from .const import DOMAIN` resolve to the real const.py, without
    # executing the real navimow/__init__.py.
    if "navimow" not in sys.modules:
        pkg = types.ModuleType("navimow")
        pkg.__path__ = [str(NAVIMOW_DIR)]
        sys.modules["navimow"] = pkg

    spec = importlib.util.spec_from_file_location(module_name, NAVIMOW_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def coordinator_module():
    return _load_module_from_navimow("coordinator.py", "navimow.coordinator")


@pytest.fixture(scope="session")
def config_flow_module():
    return _load_module_from_navimow("config_flow.py", "navimow.config_flow")
