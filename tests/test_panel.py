"""Tests for the sidebar panel and its WebSocket API."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.integration_insights.const import DOMAIN, PANEL_URL_PATH
from custom_components.integration_insights.sampler import is_supported
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .helpers import spin_at

pytestmark = pytest.mark.skipif(not is_supported(), reason="needs timer_create")


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Integration Insights")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_panel_follows_entry(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The panel is there while the entry is loaded and serves its module."""
    entry = await _setup(hass)
    panel = hass.data[DATA_PANELS][PANEL_URL_PATH]
    assert panel.require_admin
    module_url = panel.config["_panel_custom"]["module_url"]

    client = await hass_client()
    response = await client.get(module_url)
    assert response.status == 200
    assert "integration-insights-panel" in await response.text()

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert PANEL_URL_PATH not in hass.data[DATA_PANELS]
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert PANEL_URL_PATH in hass.data[DATA_PANELS]


async def test_report_command(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The report carries integration names and the config entry."""
    entry = await _setup(hass)
    spin_at("/config/custom_components/busy/coordinator.py")(0.3)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/report", "minutes": 5})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert result["entry_id"] == entry.entry_id
    assert result["report"]["integrations"][0]["domain"] == "busy"
    assert result["report"]["window_minutes"] <= 5


async def test_report_command_not_loaded(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Without a loaded entry the command answers with an error."""
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/report"})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_report_command_rejects_window(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The window is limited to what the collector keeps."""
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/report", "minutes": 61})
    msg = await client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "invalid_format"
