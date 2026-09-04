"""DataUpdateCoordinator for Segway Navimow integration."""
import asyncio
from datetime import timedelta
import logging
import json
import time
import uuid
from urllib.parse import urlparse
import paho.mqtt.client as mqtt

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

class NavimowDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator for centralized data fetching and token refresh."""

    def __init__(self, hass, api, entry, devices):
        """Initialize coordinator."""
        self.api = api
        self.entry = entry
        self.devices = devices
        self._mqtt_client = None
        self._mqtt_info = None
        # A single lock shared by every code path that can trigger a token
        # refresh (the periodic poll, the reactive 401 handler and the MQTT
        # on_disconnect handler). Without this, two of those paths could
        # refresh concurrently with the *same* refresh_token; if the auth
        # server rotates refresh tokens (single use), one of the two calls
        # is then rejected with "invalid refresh token" even though the
        # account itself is fine.
        self._token_lock = asyncio.Lock()
        # Seed from the absolute expiry timestamp we persisted last time we
        # got a token, so a HA restart doesn't blindly assume the token is
        # brand new (which used to force a redundant refresh on every
        # restart) nor blindly assume it's still valid.
        self._token_expires_at = entry.data.get("expires_at", 0)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )

    async def _async_ensure_valid_token(self, force: bool = False) -> bool:
        """Refresh the OAuth token if it is expired or expiring soon.

        Returns True if a valid access token is available afterwards, False
        if the refresh failed outright (e.g. the refresh token itself was
        rejected by the server).

        Guarded by `_token_lock` so concurrent callers (periodic poll,
        reactive 401 handling, MQTT disconnect handler) never race each
        other with the same refresh_token. The expiry check is repeated
        after acquiring the lock in case another caller already refreshed
        while we were waiting for it.
        """
        async with self._token_lock:
            now = time.time()
            if not force and now < self._token_expires_at - 10:
                return True  # Token still valid, no need to refresh

            refresh_token = self.entry.data.get("refresh_token")
            if not refresh_token:
                return False

            try:
                token_response = await self.api.async_refresh_token(refresh_token)
            except Exception as err:
                _LOGGER.warning("Failed to refresh OAuth token: %s", err)
                return False

            if not token_response or "access_token" not in token_response:
                _LOGGER.warning("Refresh token rejected by server: %s", token_response)
                return False

            new_access = token_response["access_token"]
            new_refresh = token_response.get("refresh_token", refresh_token)

            # Calculate and persist the *absolute* expiry timestamp (not just
            # the relative expires_in) so it survives a HA restart.
            expires_in = token_response.get("expires_in", 3600)  # Default 1 hour if not provided
            self._token_expires_at = now + expires_in

            self.api._token = new_access

            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    "access_token": new_access,
                    "refresh_token": new_refresh,
                    "expires_at": self._token_expires_at,
                },
            )
            _LOGGER.debug("OAuth token refreshed (expires in %ds)", expires_in)

            # Keep any already-open MQTT connection in sync with the new
            # access token right away, instead of waiting for it to
            # disconnect on its own (which previously never actually
            # happened - the old code stored the new token in
            # `_pending_mqtt_token` but never read it back).
            await self._async_update_mqtt_auth(new_access)

            return True

    async def _async_update_mqtt_auth(self, new_access_token: str) -> None:
        """Push a refreshed access token into an already-running MQTT client."""
        if not self._mqtt_client or not self._mqtt_info:
            return

        mqtt_info = self._mqtt_info

        def _update_auth():
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()

                auth_headers = {"Authorization": f"Bearer {new_access_token}"}
                ws_path = mqtt_info.get("mqttUrl", "/mqtt")
                self._mqtt_client.ws_set_options(path=ws_path, headers=auth_headers)

                mqtt_host = mqtt_info.get("mqttHost", "")
                parsed = urlparse(mqtt_host)
                hostname = parsed.hostname or mqtt_host

                _LOGGER.info("Reconnecting MQTT with refreshed access token")
                self._mqtt_client.connect(hostname, 443, 60)
                self._mqtt_client.loop_start()
            except Exception as err:
                _LOGGER.error("Failed to update MQTT auth header: %s", err)

        await self.hass.async_add_executor_job(_update_auth)

    async def _async_apply_new_mqtt_info(self, mqtt_info: dict) -> None:
        """Apply freshly fetched MQTT broker credentials to a running client."""
        self._mqtt_info = mqtt_info
        new_username = mqtt_info.get("userName")
        new_password = mqtt_info.get("pwdInfo")

        if not (new_username and new_password and self._mqtt_client):
            return

        _LOGGER.info("MQTT credentials refreshed after disconnect, updating client credentials")

        def _update_credentials():
            try:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()

                self._mqtt_client.username_pw_set(new_username, new_password)

                token = self.entry.data.get("access_token")
                auth_headers = {"Authorization": f"Bearer {token}"}
                ws_path = mqtt_info.get("mqttUrl", "/mqtt")
                self._mqtt_client.ws_set_options(path=ws_path, headers=auth_headers)

                mqtt_host = mqtt_info.get("mqttHost", "")
                parsed = urlparse(mqtt_host)
                hostname = parsed.hostname or mqtt_host

                _LOGGER.info("Reconnecting MQTT with new credentials")
                self._mqtt_client.connect(hostname, 443, 60)
                self._mqtt_client.loop_start()
            except Exception as err:
                _LOGGER.error("Failed to update MQTT client credentials: %s", err)

        await self.hass.async_add_executor_job(_update_credentials)

    async def _async_refresh_mqtt_credentials_on_disconnect(self) -> None:
        """Refresh MQTT credentials after disconnection.

        When MQTT disconnects, it's often because the OAuth token expired.
        We need to refresh the token first, then fetch fresh MQTT credentials.
        Shares `_async_ensure_valid_token`'s lock, so this never races the
        periodic/reactive HTTP token refresh.
        """
        try:
            await self._async_ensure_valid_token()
            new_mqtt_info = await self.api.async_get_mqtt_info()
            if new_mqtt_info:
                await self._async_apply_new_mqtt_info(new_mqtt_info)
        except Exception as err:
            _LOGGER.warning("Failed to refresh MQTT credentials on disconnect: %s", err)

    async def _async_update_data(self):
        """Fetch vehicle data and handle token refresh."""
        # Proactively refresh token before each update to keep API and MQTT credentials in sync.
        # If only refreshed during HTTP fallback, MQTT would have stale token for extended periods,
        # causing commands to fail with CODE_OAUTH_INFO_ILLEGAL when token eventually expires.
        try:
            await self._async_ensure_valid_token()
        except Exception as err:
            _LOGGER.warning("Token refresh failed during update: %s", err)

        device_ids = [d["id"] for d in self.devices]

        if not device_ids:
            _LOGGER.debug("No devices found for this account")
            return {}

        data = await self.api.async_get_all_vehicles_status(device_ids)

        if isinstance(data, dict) and data.get("error") == "TOKEN_EXPIRED":
            _LOGGER.info("Access token expired, attempting refresh...")

            # Goes through the same locked path as the proactive refresh
            # above and the MQTT disconnect handler, so this can never use
            # a refresh_token that one of those already consumed/rotated.
            # `force=True` because the server just told us the current
            # access token is invalid, regardless of our local expiry timer.
            refreshed = await self._async_ensure_valid_token(force=True)

            if refreshed:
                _LOGGER.info("New access token obtained successfully")
                data = await self.api.async_get_all_vehicles_status(device_ids)
            else:
                _LOGGER.error("Refresh token is invalid or server rejected the request")
                # ConfigEntryAuthFailed (not UpdateFailed) tells Home Assistant
                # this is an auth problem, so it offers a native "Reauthenticate"
                # flow instead of leaving the user to remove and re-add the
                # integration by hand.
                raise ConfigEntryAuthFailed("Session expired, please reauthenticate")

        if data is None:
            raise UpdateFailed("Communication error with Navimow servers")

        return data

    async def async_setup_mqtt(self, mqtt_info):
        """Initialize MQTT connection without blocking the loop."""
        if not mqtt_info:
            return

        _LOGGER.debug("MQTT info: %s", mqtt_info)
        # Store mqtt_info for later credential refresh
        self._mqtt_info = mqtt_info

        # Don't block the setup by waiting for MQTT connection - run it in background
        self.hass.create_task(self._async_connect_mqtt(mqtt_info))

    async def _async_connect_mqtt(self, mqtt_info):
        """Connect to MQTT without blocking the main thread."""
        try:
            await self.hass.async_add_executor_job(self._connect_mqtt, mqtt_info)
        except Exception as e:
            _LOGGER.error("Error setting up MQTT: %s", e)

    def _connect_mqtt(self, mqtt_info):
        """Connect to MQTT broker (blocking operation, run in executor)."""
        try:
            mqtt_host = mqtt_info.get("mqttHost", "")
            parsed = urlparse(mqtt_host)
            hostname = parsed.hostname or mqtt_host
            
            username = mqtt_info.get("userName", "unknown")
            rand_suffix = uuid.uuid4().hex[:10]
            client_id = f"web_{username}_{rand_suffix}"

            # Home Assistant core's own "mqtt" integration already pins
            # paho-mqtt>=2.1.0 on modern installs, which uses a new
            # constructor/callback signature (CallbackAPIVersion.VERSION2).
            # Explicitly requesting it here (rather than relying on the
            # deprecated implicit VERSION1 default) keeps this working
            # regardless of what other integrations already pulled in.
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                transport="websockets",
            )
            
            password = mqtt_info.get("pwdInfo")
            client.username_pw_set(username, password)
            
            ws_path = mqtt_info.get("mqttUrl", "/mqtt")
            
            token = self.entry.data.get("access_token")
            auth_headers = {"Authorization": f"Bearer {token}"}
            client.ws_set_options(path=ws_path, headers=auth_headers)
            
            client.tls_set()
            client.tls_insecure_set(False)
            
            def on_message(client, userdata, msg):
                _LOGGER.debug("MQTT message received: topic=%s", msg.topic)
                try:
                    payload = json.loads(msg.payload.decode())
                    _LOGGER.debug("MQTT payload (JSON): %s", payload)
                    self.hass.add_job(self._handle_mqtt_payload, msg.topic, payload)
                except json.JSONDecodeError as e:
                    _LOGGER.warning("Invalid JSON payload: %s (raw: %s)", e, msg.payload)
                except Exception as e:
                    _LOGGER.error("Error parsing MQTT: %s", e)

            # CallbackAPIVersion.VERSION2 signatures: on_connect gets
            # (client, userdata, connect_flags, reason_code, properties)
            # instead of the old (client, userdata, flags, rc).
            # `reason_code` compares equal to 0 for success, same as the old `rc`.
            def on_connect(client, userdata, connect_flags, reason_code, properties=None):
                if reason_code == 0:
                    _LOGGER.info("MQTT connected successfully")
                    for device in self.devices:
                        device_id = device.get("id")
                        if device_id:
                            topics = [
                                f"/downlink/vehicle/{device_id}/realtimeDate/state",
                                f"/downlink/vehicle/{device_id}/realtimeDate/event",
                                f"/downlink/vehicle/{device_id}/realtimeDate/attributes",
                            ]
                            for topic in topics:
                                client.subscribe(topic)
                                _LOGGER.debug("Subscribed to topic: %s", topic)
                else:
                    _LOGGER.error("MQTT connection error (reason_code=%s)", reason_code)

            def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
                _LOGGER.info("MQTT disconnected: reason_code=%s", reason_code)
                # After MQTT disconnection, refresh credentials from server.
                # MQTT credentials (userName/pwdInfo) are bound to the OAuth token.
                # If token expired, causing the disconnection, we need to fetch fresh credentials.
                #
                # This callback runs on paho-mqtt's own background thread
                # (started via loop_start()), NOT on the HA event loop thread.
                # hass.create_task() is only safe to call from the event loop
                # thread; hass.add_job() is the thread-safe way to schedule a
                # coroutine from here (same as on_message does above).
                self.hass.add_job(self._async_refresh_mqtt_credentials_on_disconnect)

            client.on_message = on_message
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            
            port = 443
            
            _LOGGER.debug("Connecting to MQTT %s:%d with path %s", hostname, port, ws_path)
            client.connect(hostname, port, 60)
            client.loop_start()
            
            self._mqtt_client = client
            _LOGGER.info("MQTT client initialized and connecting")
        except Exception as e:
            _LOGGER.error("Error connecting to MQTT: %s", e)
            raise

    async def _handle_mqtt_payload(self, topic, payload):
        """Update data with MQTT updates in the main loop."""
        _LOGGER.debug("MQTT payload received: topic=%s payload=%s", topic, payload)

        # Extract channel from topic: /downlink/vehicle/{id}/realtimeDate/{channel}
        parts = topic.split("/")
        channel = parts[-1] if parts else ""

        device_id = payload.get("device_id")
        if not device_id:
            # Fallback: extract device_id from topic position
            try:
                device_id = parts[3]
            except IndexError:
                pass
        if not device_id:
            _LOGGER.debug("Payload has no device_id and topic has no device segment")
            return

        if not self.data or device_id not in self.data:
            _LOGGER.debug("Device '%s' not found", device_id)
            return

        if channel == "state":
            old_state = self.data[device_id].get("vehicleState", "unknown")
            if "state" in payload:
                self.data[device_id]["vehicleState"] = payload["state"]
            for key in ["battery", "timestamp", "position", "signal_strength"]:
                if key in payload:
                    self.data[device_id][key] = payload[key]
            new_state = self.data[device_id].get("vehicleState", "unknown")
            _LOGGER.debug("Device '%s': vehicleState %s -> %s", device_id, old_state, new_state)

        elif channel == "event":
            event_type = payload.get("event", "")
            level = payload.get("level", "")
            _LOGGER.debug("Device '%s' event: type=%s level=%s", device_id, event_type, level)
            # Propagate error events immediately so the error sensor updates in real time
            if level == "error" or event_type in {"Error", "error", "isLifted", "stuck"}:
                self.data[device_id]["error_code"] = event_type or level
                self.data[device_id]["vehicleState"] = "Error"
            elif level == "info" and event_type in {"errorRecovery", "clear"}:
                self.data[device_id]["error_code"] = "none"

        elif channel == "attributes":
            attributes = payload.get("attributes", payload)
            if isinstance(attributes, dict):
                _LOGGER.debug("Device '%s' attributes: %s", device_id, list(attributes.keys()))
                self.data[device_id].update(attributes)

        await self.async_set_updated_data(self.data)