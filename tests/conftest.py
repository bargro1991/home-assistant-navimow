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


def _resolve_navimow_dir() -> Path:
    """Locate the navimow integration directory (where coordinator.py and
    config_flow.py actually live), regardless of how these test files were
    placed. Two layouts are supported:

    Layout A - dropped straight into custom_components/navimow, right next
    to coordinator.py (e.g. testing in-place inside a live Home Assistant
    config directory: /config/custom_components/navimow/).

    Layout B - kept in a separate tests/ directory next to
    custom_components/navimow/ (the layout used during development of this
    integration).
    """
    conftest_dir = Path(__file__).resolve().parent

    if (conftest_dir / "coordinator.py").exists():
        return conftest_dir

    candidate = conftest_dir.parent / "custom_components" / "navimow"
    if (candidate / "coordinator.py").exists():
        return candidate

    raise RuntimeError(
        "Could not find the navimow integration directory: no coordinator.py "
        f"next to {conftest_dir} and none under {candidate}. Run these tests "
        "either from inside custom_components/navimow itself, or from a "
        "tests/ directory placed next to a custom_components/navimow/ "
        "directory."
    )


NAVIMOW_DIR = _resolve_navimow_dir()


def _install_ha_stubs() -> None:
    """Force our lightweight stand-ins into sys.modules for the specific
    homeassistant.* leaf modules coordinator.py/config_flow.py import.

    This always overwrites those entries, even if a *real* homeassistant
    install already imported them for real (e.g. inside an actual Home
    Assistant container, some other pytest plugin may import the real
    homeassistant.helpers.update_coordinator before this conftest runs).
    The real DataUpdateCoordinator enforces internal invariants (a running
    HA "frame helper"/config entry context) that our minimal FakeHass in
    these tests can't satisfy and doesn't need to - we only want to
    exercise our own coordinator/config_flow logic in isolation, not
    Home Assistant's own machinery.

    We deliberately do NOT touch "homeassistant" or "homeassistant.helpers"
    themselves if they already exist for real, so anything else relying on
    the genuine package elsewhere in the same process is left alone.
    """
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

    # Only create bare top-level package placeholders if nothing (real or
    # otherwise) is already registered there - these two are never imported
    # from directly by our code, they just need to exist so Python doesn't
    # complain about a fully-dotted import having no parent at all in an
    # environment with no real homeassistant installed at all.
    sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    sys.modules.setdefault("homeassistant.helpers", ha_helpers)

    # These specific leaves are always overwritten with our stand-ins,
    # regardless of whether a real homeassistant install already loaded
    # its own (much stricter) versions elsewhere in this process.
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
