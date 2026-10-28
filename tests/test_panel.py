"""Tests for the sidebar panel and its WebSocket API."""

import time

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.integration_insights.const import DOMAIN, PANEL_URL_PATH
from custom_components.integration_insights.sampler import Record, is_supported
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from .helpers import spin_at

MS = 1_000_000

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
    assert {"device_names", "entity_names", "entry_titles", "names"} <= result.keys()


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


async def test_entities_grouped_by_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Entities add up per device, and the panel gets their names."""
    entry = await _setup(hass)
    source = MockConfigEntry(domain="foo")
    source.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("foo", "boiler")}, name="Boiler"
    )
    first, second = (
        entity_registry.async_get_or_create(
            "sensor", "foo", unique_id, device_id=device.id, config_entry=source
        ).entity_id
        for unique_id in ("a", "b")
    )
    spin = spin_at("/config/custom_components/foo/sensor.py")
    frames = (spin.__code__, 0)
    now = time.monotonic()
    entry.runtime_data.collector.add(
        [
            Record(frames, 0, "foo", None, (), None, None, 30 * MS, first),
            Record(frames, 0, "foo", None, (), None, None, 10 * MS, second),
            Record(frames, 0, "foo", None, (), None, None, 5 * MS, "sensor.unknown"),
        ],
        now,
    )

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/report", "minutes": 5})
    result = (await client.receive_json())["result"]
    foo = next(i for i in result["report"]["integrations"] if i["domain"] == "foo")
    assert list(foo["entities"]) == [first, second, "sensor.unknown"]
    assert list(foo["devices"]) == [device.id]
    per_minute = foo["entities"][first] + foo["entities"][second]
    assert foo["devices"][device.id] == pytest.approx(per_minute, abs=0.2)
    assert result["device_names"] == {device.id: "Boiler"}


async def test_detail_command(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Details come for one integration, None for an unknown one."""
    await _setup(hass)
    spin_at("/config/custom_components/busy/coordinator.py")(0.3)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/detail", "domain": "busy", "minutes": 5}
    )
    result = (await client.receive_json())["result"]
    assert result["item"]["domain"] == "busy"
    assert result["item"]["hot_spots"]
    assert {"device_names", "entity_names", "entry_titles"} <= result.keys()

    await client.send_json_auto_id({"type": f"{DOMAIN}/detail", "domain": "nope"})
    assert (await client.receive_json())["result"] == {"item": None}


async def test_subscribe_pushes_summary_after_drain(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The summary comes right away and after every drain, until unsubscribed."""
    entry = await _setup(hass)
    insights = entry.runtime_data
    spin_at("/config/custom_components/busy/coordinator.py")(0.3)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe", "minutes": 5})
    msg_id = (await client.receive_json())["id"]
    first = await client.receive_json()
    assert first["id"] == msg_id
    summary = first["event"]["report"]
    assert summary["integrations"][0]["domain"] == "busy"
    assert "hot_spots" not in summary["integrations"][0]
    assert first["event"]["names"]
    assert "sampler" in summary

    insights._async_drain()  # noqa: SLF001
    assert (await client.receive_json())["id"] == msg_id

    await client.send_json_auto_id(
        {"type": "unsubscribe_events", "subscription": msg_id}
    )
    assert (await client.receive_json())["success"]
    assert insights._subscribers == []  # noqa: SLF001
