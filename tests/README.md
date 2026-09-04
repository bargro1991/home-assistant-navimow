# Tests for the token-refresh fix

These tests exercise `custom_components/navimow/coordinator.py` in isolation.
They do **not** require a full Home Assistant install — `conftest.py` stubs
just the two `homeassistant` symbols the coordinator imports
(`DataUpdateCoordinator`, `UpdateFailed`, `ConfigEntryAuthFailed`) and loads
`coordinator.py` directly by file path, so `navimow/__init__.py` (which pulls
in aiohttp/config_entries wiring) is never executed.

## Setup

```bash
pip install pytest pytest-asyncio paho-mqtt
```

## Run

From the repo root:

```bash
pytest tests/ -v
```

## What's covered

- `test_concurrent_refresh_calls_are_serialized` — proves the `asyncio.Lock`
  actually prevents two simultaneous refreshes from both calling the API
  with the same refresh_token (this is the root cause of the
  "Session expired" / "Refresh token is invalid" errors).
- `test_no_refresh_when_token_still_valid` — no unnecessary refresh when the
  token isn't near expiry.
- `test_invalid_refresh_token_raises_auth_failed` — a genuinely rejected
  refresh token raises `ConfigEntryAuthFailed`, not the old generic
  `UpdateFailed`, so Home Assistant offers the native reauth flow.
- `test_successful_reactive_refresh_returns_data` — the 401 → refresh →
  retry path still works end-to-end.
- `test_token_expiry_seeded_from_entry_data` — a restart doesn't reset the
  known token expiry to 0.
- `test_refresh_updates_live_mqtt_connection` — a refreshed access token is
  actually pushed into an already-open MQTT client (the old
  `_pending_mqtt_token` dead-code bug).
- `test_mqtt_disconnect_schedules_via_thread_safe_add_job` — `on_disconnect`
  schedules the credential-refresh coroutine via `hass.add_job` (thread-safe)
  instead of `hass.create_task` (unsafe from paho-mqtt's background thread).

## Sanity-checking the tests themselves

To confirm these tests actually catch the original bugs (rather than being
no-ops), temporarily revert `coordinator.py` to the previous commit and
re-run — `test_concurrent_refresh_calls_are_serialized`,
`test_token_expiry_seeded_from_entry_data`,
`test_refresh_updates_live_mqtt_connection` and
`test_mqtt_disconnect_schedules_via_thread_safe_add_job` should all fail:

```bash
git stash
pytest tests/ -v   # expect several failures
git stash pop
```
