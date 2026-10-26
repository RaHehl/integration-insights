"""WebSocket API for the panel."""

from contextlib import suppress
from typing import Any

import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .collector import WINDOW_MINUTES
from .const import DOMAIN


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the commands."""
    websocket_api.async_register_command(hass, websocket_report)


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/report",
        vol.Optional("minutes", default=WINDOW_MINUTES): vol.All(
            int, vol.Range(min=1, max=WINDOW_MINUTES)
        ),
    }
)
def websocket_report(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the report, with names for integrations, entries, devices, entities."""
    if not (entries := hass.config_entries.async_loaded_entries(DOMAIN)):
        connection.send_error(
            msg["id"], websocket_api.ERR_NOT_FOUND, "Integration Insights is not loaded"
        )
        return
    entry = entries[0]
    report = entry.runtime_data.report(msg["minutes"])
    names: dict[str, str] = {}
    titles: dict[str, str] = {}
    device_names: dict[str, str] = {}
    entity_names: dict[str, str] = {}
    devices = dr.async_get(hass)
    for item in report["integrations"]:
        with suppress(IntegrationNotLoaded):
            names[item["domain"]] = async_get_loaded_integration(
                hass, item["domain"]
            ).name
        for entry_id in item["config_entries"]:
            if (
                config_entry := hass.config_entries.async_get_entry(entry_id)
            ) is not None:
                titles[entry_id] = config_entry.title
        for device_id in item["devices"]:
            if (device := devices.async_get(device_id)) is not None:
                device_names[device_id] = device.name_by_user or device.name or ""
        for entity_id in item["entities"]:
            if (state := hass.states.get(entity_id)) is not None:
                entity_names[entity_id] = state.name
    connection.send_result(
        msg["id"],
        {
            "report": report,
            "names": names,
            "entry_titles": titles,
            "device_names": device_names,
            "entity_names": entity_names,
            "entry_id": entry.entry_id,
        },
    )
