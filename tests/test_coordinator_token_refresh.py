"""Regression tests for the account-timeout / token-refresh bugfixes.

Run with:  pytest tests/ -v
(see tests/README.md for setup)
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest


class FakeConfigEntry:
    """Stand-in for homeassistant.config_entries.ConfigEntry."""

    def __init__(self, data):
        self.data = data
        self.title = "Test Navimow"


class FakeConfigEntries:
    """Stand-in for hass.config_entries."""

    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, data):
        entry.data = data
        self.updates.append(dict(data))


class FakeHass:
    """Stand-in for homeassistant.core.HomeAssistant, just enough for the
    coordinator methods under test."""

    def __init__(self):
        self.config_entries = FakeConfigEntries()
        self.jobs = []

    def add_job(self, target, *args):
        """Thread-safe scheduling in real HA. Here we just record the call
        so tests can assert *how* something was scheduled."""
        self.jobs.append((target, args))

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def fake_api():
    api = MagicMock()
    api.async_refresh_token = AsyncMock()
    api.async_get_all_vehicles_status = AsyncMock()
    api.async_get_mqtt_info = AsyncMock(return_value={})
    api._token = "old-access-token"
    return api


@pytest.fixture
def make_coordinator(coordinator_module, fake_api):
    def _make(entry_data=None):
        hass = FakeHass()
        entry = FakeConfigEntry(entry_data or {"refresh_token": "rt-1", "expires_at": 0})
        devices = [{"id": "dev-1"}]
        coord = coordinator_module.NavimowDataUpdateCoordinator(hass, fake_api, entry, devices)
        return coord, hass, entry

    return _make


# ---------------------------------------------------------------------------
# Bug: no locking around token refresh -> concurrent refreshes could use the
# same (possibly-already-rotated) refresh_token and race each other.
# ---------------------------------------------------------------------------
async def test_concurrent_refresh_calls_are_serialized(make_coordinator, fake_api):
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": 0})

    call_count = 0

    async def slow_refresh(refresh_token):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return {"access_token": "new-access", "refresh_token": "rt-2", "expires_in": 3600}

    fake_api.async_refresh_token.side_effect = slow_refresh

    # Simulate the periodic coordinator poll and the MQTT-disconnect handler
    # both deciding "the token needs refreshing" at (almost) the same time.
    results = await asyncio.gather(
        coord._async_ensure_valid_token(),
        coord._async_ensure_valid_token(),
    )

    assert results == [True, True]
    # Only one actual HTTP refresh should have happened; the second caller
    # must observe the already-refreshed token instead of racing with rt-1.
    assert call_count == 1
    assert entry.data["refresh_token"] == "rt-2"


async def test_no_refresh_when_token_still_valid(make_coordinator, fake_api):
    future_expiry = time.time() + 1800
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": future_expiry})

    result = await coord._async_ensure_valid_token()

    assert result is True
    fake_api.async_refresh_token.assert_not_called()


# ---------------------------------------------------------------------------
# Bug: a genuinely invalid/rejected refresh token raised UpdateFailed, which
# just leaves entities "unavailable" and never offers a reauth flow.
# ---------------------------------------------------------------------------
async def test_invalid_refresh_token_raises_auth_failed(make_coordinator, fake_api, coordinator_module):
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": 0})

    fake_api.async_refresh_token = AsyncMock(return_value={"error": "invalid_grant"})
    fake_api.async_get_all_vehicles_status = AsyncMock(return_value={"error": "TOKEN_EXPIRED"})

    with pytest.raises(coordinator_module.ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_successful_reactive_refresh_returns_data(make_coordinator, fake_api):
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": 0})

    fake_api.async_refresh_token = AsyncMock(
        return_value={"access_token": "new-tok", "refresh_token": "rt-2", "expires_in": 3600}
    )
    fake_api.async_get_all_vehicles_status = AsyncMock(
        side_effect=[{"error": "TOKEN_EXPIRED"}, {"dev-1": {"vehicleState": "Mowing"}}]
    )

    data = await coord._async_update_data()

    assert data == {"dev-1": {"vehicleState": "Mowing"}}
    assert entry.data["access_token"] == "new-tok"


# ---------------------------------------------------------------------------
# Bug: _token_expires_at was hardcoded to 0 on every coordinator init, so a
# HA restart always forced an immediate, unnecessary refresh.
# ---------------------------------------------------------------------------
def test_token_expiry_seeded_from_entry_data(make_coordinator):
    future_expiry = time.time() + 1800
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": future_expiry})
    assert coord._token_expires_at == future_expiry


# ---------------------------------------------------------------------------
# Bug: refreshing the HTTP token stored the new token in `_pending_mqtt_token`
# but nothing ever read that field back, so a live MQTT connection kept using
# the stale token until (if ever) it happened to disconnect.
# ---------------------------------------------------------------------------
async def test_refresh_updates_live_mqtt_connection(make_coordinator, fake_api):
    coord, hass, entry = make_coordinator({"refresh_token": "rt-1", "expires_at": 0})
    fake_api.async_refresh_token = AsyncMock(
        return_value={"access_token": "new-tok", "refresh_token": "rt-2", "expires_in": 3600}
    )

    fake_mqtt_client = MagicMock()
    coord._mqtt_client = fake_mqtt_client
    coord._mqtt_info = {"mqttHost": "wss://broker.example.com", "mqttUrl": "/mqtt"}

    await coord._async_ensure_valid_token()

    fake_mqtt_client.ws_set_options.assert_called_once()
    fake_mqtt_client.connect.assert_called_once()


# ---------------------------------------------------------------------------
# Bug: on_disconnect (running on paho-mqtt's own background thread) called
# hass.create_task(), which is not thread-safe. It must use hass.add_job()
# instead, like on_message already correctly does.
# ---------------------------------------------------------------------------
async def test_mqtt_disconnect_schedules_via_thread_safe_add_job(
    make_coordinator, coordinator_module, monkeypatch
):
    coord, hass, entry = make_coordinator()

    captured = {}

    class FakeMqttClient:
        def __init__(self, *args, **kwargs):
            captured["client"] = self

        def username_pw_set(self, *a, **k):
            pass

        def ws_set_options(self, *a, **k):
            pass

        def tls_set(self, *a, **k):
            pass

        def tls_insecure_set(self, *a, **k):
            pass

        def connect(self, *a, **k):
            pass

        def loop_start(self):
            pass

        def loop_stop(self):
            pass

        def disconnect(self):
            pass

    monkeypatch.setattr(coordinator_module.mqtt, "Client", FakeMqttClient)

    mqtt_info = {
        "mqttHost": "wss://broker.example.com",
        "userName": "u",
        "pwdInfo": "p",
        "mqttUrl": "/mqtt",
    }
    coord._connect_mqtt(mqtt_info)

    client = captured["client"]
    # Fire the disconnect callback exactly as paho-mqtt's background thread
    # would (i.e. NOT from the asyncio event loop thread). CallbackAPIVersion.
    # VERSION2 passes (client, userdata, disconnect_flags, reason_code, properties).
    client.on_disconnect(client, None, {}, 1, None)

    assert len(hass.jobs) == 1
    scheduled_target, _ = hass.jobs[0]
    assert scheduled_target == coord._async_refresh_mqtt_credentials_on_disconnect


async def test_mqtt_client_requests_callback_api_version2(make_coordinator, coordinator_module, monkeypatch):
    """Home Assistant core's own `mqtt` integration now requires
    paho-mqtt>=2.1.0, which needs callback_api_version passed explicitly
    or Client() behaves unpredictably across versions. Guards against
    silently reverting to the old implicit-VERSION1 call."""
    coord, hass, entry = make_coordinator()

    captured_kwargs = {}

    def spy_client(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(coordinator_module.mqtt, "Client", spy_client)
    coord._connect_mqtt({"mqttHost": "wss://broker.example.com", "userName": "u", "pwdInfo": "p"})

    assert (
        captured_kwargs.get("callback_api_version")
        == coordinator_module.mqtt.CallbackAPIVersion.VERSION2
    )
