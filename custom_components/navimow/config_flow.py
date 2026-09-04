"""Config flow for Segway Navimow integration."""
import time
import urllib.parse
import logging
from aiohttp import web
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.network import get_url
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, CLIENT_ID, CLIENT_SECRET, TOKEN_URL, AUTH_BASE_URL

_LOGGER = logging.getLogger(__name__)


def _is_reachable_base_url(url: str) -> bool:
    """Basic sanity check for a URL a browser should be able to open.

    This can't know whether the URL is *actually* reachable from the
    person's laptop/phone (that would need a real network probe from the
    browser side), but it catches the obvious mistakes: missing scheme,
    missing host, or pasting something that clearly isn't a URL at all.
    """
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class NavimowCallbackView(HomeAssistantView):
    """HTTP endpoint in Home Assistant to handle Navimow OAuth redirect."""
    
    url = "/api/navimow/callback"
    name = "api:navimow:callback"
    requires_auth = False

    def __init__(self, hass: HomeAssistant, flow_id: str):
        """Initialize the view with the config flow ID."""
        self.hass = hass
        self.flow_id = flow_id

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET request from Navimow OAuth redirect."""
        code = request.query.get("code")
        
        if not code:
            return web.Response(text="Error: 'code' parameter not found in redirect URL.", status=400)

        await self.hass.config_entries.flow.async_configure(
            flow_id=self.flow_id,
            user_input={"code": code}
        )

        html_response = """
        <html>
            <head><title>Navimow Authentication</title></head>
            <body style="font-family: sans-serif; text-align: center; padding: 50px; background-color: #121212; color: white;">
                <h2 style="color: #4CAF50;">Authentication successful!</h2>
                <p>Home Assistant has received the data. You can close this window and return to the app.</p>
            </body>
        </html>
        """
        return web.Response(text=html_response, content_type="text/html")


class NavimowConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup and config flow for Navimow."""
    
    VERSION = 1

    def __init__(self):
        """Initialize the flow."""
        self.redirect_uri = None
        self.account_name = None
        self._reauth_entry = None

    async def async_step_user(self, user_input=None):
        """First step: Ask user for account name."""
        errors = {}

        if user_input is not None:
            self.account_name = user_input["account_name"]

            await self.async_set_unique_id(self.account_name)
            self._abort_if_unique_id_configured()

            return await self.async_step_url_confirm()

        data_schema = vol.Schema({
            vol.Required("account_name", default="My Navimow"): str
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Handle reauthentication triggered by ConfigEntryAuthFailed.

        Home Assistant calls this automatically when the coordinator raises
        ConfigEntryAuthFailed, instead of leaving the user to remove and
        re-add the integration by hand.
        """
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self.account_name = self._reauth_entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask the user to confirm before re-launching the OAuth login."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_url_confirm()

    async def async_step_url_confirm(self, user_input=None):
        """Let the user confirm or correct the base URL used for the OAuth redirect.

        get_url(prefer_external=True) falls back to whatever internal URL
        Home Assistant knows about when no external URL is configured in
        Settings > System > Network. In a Docker/container setup that can be
        a container-internal hostname or IP that the browser on a laptop or
        phone can never reach - the person only discovers this after Segway
        redirects them nowhere. Showing the detected value and letting them
        edit it fixes this without requiring them to configure HA's network
        settings first.
        """
        errors = {}

        if user_input is not None:
            base_url = user_input["base_url"].strip().rstrip("/")
            if not _is_reachable_base_url(base_url):
                errors["base_url"] = "invalid_url"
            else:
                self.redirect_uri = f"{base_url}/api/navimow/callback"
                return await self.async_step_auth()

        try:
            detected_url = get_url(self.hass, prefer_external=True)
        except Exception:
            detected_url = get_url(self.hass)

        data_schema = vol.Schema({
            vol.Required("base_url", default=detected_url): str
        })

        return self.async_show_form(
            step_id="url_confirm",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={"detected_url": detected_url},
        )

    async def async_step_auth(self, user_input=None):
        """Second step: Show OAuth login link and wait for callback."""

        if user_input is not None and "code" in user_input:
            return await self.async_step_exchange(user_input["code"])

        if not self.redirect_uri:
            # Defensive fallback in case this step is ever entered directly
            # without going through async_step_url_confirm first.
            return await self.async_step_url_confirm()

        params = {
            "channel": "homeassistant",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": self.redirect_uri
        }
        auth_url = f"{AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"

        self.hass.http.register_view(NavimowCallbackView(self.hass, self.flow_id))

        return self.async_show_form(
            step_id="auth", 
            description_placeholders={"auth_url": auth_url}
        )

    async def async_step_exchange(self, code: str):
        """Third step: Exchange authorization code for access token."""
        session = async_get_clientsession(self.hass)
        
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            async with session.post(TOKEN_URL, data=payload, headers=headers) as response:
                response.raise_for_status()
                token_data = await response.json()

                if "access_token" in token_data:
                    expires_in = token_data.get("expires_in", 3600)
                    entry_data = {
                        "access_token": token_data["access_token"],
                        "refresh_token": token_data.get("refresh_token"),
                        # Store the absolute expiry timestamp (not just the
                        # relative expires_in) so the coordinator can tell,
                        # even after a Home Assistant restart much later,
                        # whether the stored access token is still valid.
                        "expires_at": time.time() + expires_in,
                    }

                    if self._reauth_entry is not None:
                        return self.async_update_reload_and_abort(
                            self._reauth_entry, data=entry_data
                        )

                    return self.async_create_entry(
                        title=self.account_name,
                        data=entry_data,
                    )
                else:
                    _LOGGER.error("Error token response: %s", token_data)
                    return self.async_abort(reason="auth_failed")
                    
        except Exception as e:
            _LOGGER.error("Error during token exchange: %s", e)
            return self.async_abort(reason="cannot_connect")