"""Regression tests for the OAuth redirect base-URL confirmation step.

Covers the bug where get_url(prefer_external=True) silently returned a
container/Docker-internal address that a laptop or phone browser could
never reach, with no way for the person to correct it.
"""
import pytest


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://192.168.1.10:8123", True),
        ("https://home.example.com", True),
        ("https://home.example.com/", True),
        ("http://172.17.0.2:8123", True),  # syntactically valid; the point is the *person* can judge this
        ("not-a-url", False),
        ("", False),
        ("ftp://home.example.com", False),
        ("192.168.1.10:8123", False),  # missing scheme
    ],
)
def test_is_reachable_base_url(config_flow_module, url, expected):
    assert config_flow_module._is_reachable_base_url(url) is expected


async def test_url_confirm_step_shows_detected_url_as_default(config_flow_module, monkeypatch):
    monkeypatch.setattr(
        config_flow_module, "get_url", lambda hass, prefer_external=False: "http://container-internal:8123"
    )

    flow = config_flow_module.NavimowConfigFlow()
    flow.hass = object()

    result = await flow.async_step_url_confirm()

    assert result["step_id"] == "url_confirm"
    assert result["description_placeholders"]["detected_url"] == "http://container-internal:8123"
    # The (possibly wrong) detected value is offered as an editable default,
    # not silently used.
    schema_keys = list(result["data_schema"].schema)
    assert schema_keys[0] == "base_url"
    assert schema_keys[0].default() == "http://container-internal:8123"


async def test_url_confirm_step_accepts_corrected_url(config_flow_module, monkeypatch):
    monkeypatch.setattr(
        config_flow_module, "get_url", lambda hass, prefer_external=False: "http://container-internal:8123"
    )

    flow = config_flow_module.NavimowConfigFlow()
    flow.hass = object()

    async def fake_auth_step(user_input=None):
        return {"step_id": "auth", "redirect_uri": flow.redirect_uri}

    monkeypatch.setattr(flow, "async_step_auth", fake_auth_step)

    result = await flow.async_step_url_confirm({"base_url": "http://192.168.1.10:8123/"})

    assert flow.redirect_uri == "http://192.168.1.10:8123/api/navimow/callback"
    assert result["step_id"] == "auth"


async def test_url_confirm_step_rejects_invalid_url(config_flow_module, monkeypatch):
    monkeypatch.setattr(
        config_flow_module, "get_url", lambda hass, prefer_external=False: "http://container-internal:8123"
    )

    flow = config_flow_module.NavimowConfigFlow()
    flow.hass = object()

    result = await flow.async_step_url_confirm({"base_url": "not-a-url"})

    assert result["step_id"] == "url_confirm"
    assert result["errors"] == {"base_url": "invalid_url"}
    # Must not have been (mis)used as a redirect_uri.
    assert flow.redirect_uri is None
