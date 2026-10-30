"""WebSocket API for the panel."""

from contextlib import suppress
import re
from typing import Any

import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.translation import async_get_translations
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .collector import WINDOW_MINUTES
from .const import DOMAIN
from .findings import Finding, verdict
from .insights import Insights
from .memory import SnapshotRefused

MINUTES = vol.All(int, vol.Range(min=1, max=WINDOW_MINUTES))
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@callback
def async_setup(hass: HomeAssistant) -> None:
    """Register the commands."""
    websocket_api.async_register_command(hass, websocket_report)
    websocket_api.async_register_command(hass, websocket_detail)
    websocket_api.async_register_command(hass, websocket_subscribe)
    websocket_api.async_register_command(hass, websocket_memory)
    websocket_api.async_register_command(hass, websocket_memory_snapshot)
    websocket_api.async_register_command(hass, websocket_findings)


def _insights(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg_id: int
) -> Insights | None:
    if not (entries := hass.config_entries.async_loaded_entries(DOMAIN)):
        connection.send_error(
            msg_id, websocket_api.ERR_NOT_FOUND, "Integration Insights is not loaded"
        )
        return None
    insights: Insights = entries[0].runtime_data
    return insights


def _integration_names(
    hass: HomeAssistant, items: list[dict[str, Any]]
) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in items:
        with suppress(IntegrationNotLoaded):
            names[item["domain"]] = async_get_loaded_integration(
                hass, item["domain"]
            ).name
    return names


def _detail_names(hass: HomeAssistant, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Return names for the entries, devices and entities of items."""
    titles: dict[str, str] = {}
    device_names: dict[str, str] = {}
    entity_names: dict[str, str] = {}
    devices = dr.async_get(hass)
    for item in items:
        for entry_id in item["config_entries"]:
            if (
                config_entry := hass.config_entries.async_get_entry(entry_id)
            ) is not None:
                titles[entry_id] = config_entry.title
        for device_id in item["devices"]:
            if (device := devices.async_get(device_id)) is not None:
                device_names[device_id] = device.name_by_user or device.name or ""
        for entity_id in (
            *item["entities"],
            *item.get("state_writes", {}).get("entities", {}),
        ):
            if (state := hass.states.get(entity_id)) is not None:
                entity_names[entity_id] = state.name
    return {
        "entry_titles": titles,
        "device_names": device_names,
        "entity_names": entity_names,
    }


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/report",
        vol.Optional("minutes", default=WINDOW_MINUTES): MINUTES,
    }
)
def websocket_report(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the full report, with names for integrations, entries, devices, entities."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    report = insights.report(msg["minutes"])
    connection.send_result(
        msg["id"],
        {
            "report": report,
            "names": _integration_names(hass, report["integrations"]),
            **_detail_names(hass, report["integrations"]),
            "entry_id": hass.config_entries.async_loaded_entries(DOMAIN)[0].entry_id,
        },
    )


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/detail",
        vol.Required("domain"): str,
        vol.Optional("minutes", default=WINDOW_MINUTES): MINUTES,
    }
)
def websocket_detail(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the details of one integration."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    if (item := insights.detail(msg["domain"], msg["minutes"])) is None:
        connection.send_result(msg["id"], {"item": None})
        return
    connection.send_result(msg["id"], {"item": item, **_detail_names(hass, [item])})


@callback
@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/subscribe",
        vol.Optional("minutes", default=WINDOW_MINUTES): MINUTES,
    }
)
def websocket_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Send the summary now and after every drain."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    msg_id = msg["id"]
    entry_id = hass.config_entries.async_loaded_entries(DOMAIN)[0].entry_id

    @callback
    def send(summary: dict[str, Any]) -> None:
        connection.send_message(
            websocket_api.event_message(
                msg_id,
                {
                    "report": summary,
                    "names": _integration_names(hass, summary["integrations"]),
                    "entry_id": entry_id,
                },
            )
        )

    connection.send_result(msg_id)
    connection.subscriptions[msg_id] = insights.async_subscribe(msg["minutes"], send)


@callback
@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/memory"})
def websocket_memory(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the latest memory snapshot compared with the one before."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    connection.send_result(msg["id"], _memory_result(hass, insights))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/memory_snapshot"})
@websocket_api.async_response
async def websocket_memory_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Take a memory snapshot and return it compared with the one before."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    try:
        await insights.async_take_snapshot()
    except SnapshotRefused as err:
        connection.send_error(msg["id"], "insufficient_memory", str(err))
        return
    connection.send_result(msg["id"], _memory_result(hass, insights))


def _memory_result(hass: HomeAssistant, insights: Insights) -> dict[str, Any]:
    memory = insights.memory_report()
    rows = [
        {"domain": row["owner"]}
        for row in memory.get("latest", {}).get("owners", [])
        if row["group"] == "integration"
    ]
    return {"memory": memory, "names": _integration_names(hass, rows)}


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/findings",
        vol.Optional("minutes", default=WINDOW_MINUTES): MINUTES,
        vol.Optional("language"): str,
    }
)
@websocket_api.async_response
async def websocket_findings(
    hass: HomeAssistant,
    connection: websocket_api.connection.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the findings in the user's language, with where to act on them."""
    if (insights := _insights(hass, connection, msg["id"])) is None:
        return
    findings = insights.findings(msg["minutes"])
    translations = await async_get_translations(
        hass, msg.get("language") or hass.config.language, "issues", {DOMAIN}
    )
    connection.send_result(
        msg["id"],
        {
            "verdict": verdict(findings),
            "findings": [_render(hass, finding, translations) for finding in findings],
        },
    )


def _render(
    hass: HomeAssistant, finding: Finding, translations: dict[str, str]
) -> dict[str, Any]:
    values = dict(finding.values)
    item: dict[str, Any] = {
        "key": finding.key,
        "severity": finding.severity,
        "domain": finding.domain,
        "evidence": finding.evidence,
    }
    key = finding.key
    if finding.domain is not None:
        about = _about(hass, finding.domain)
        item.update(about)
        values.update(
            name=about["name"],
            issues_url=about["issues_url"],
            config_url=f"/config/integrations/integration/{finding.domain}",
        )
        # Advice for a built-in integration differs: it updates with Home
        # Assistant and its reports go to the core issue tracker.
        if about["built_in"] and f"{_prefix(key)}_built_in.title" in translations:
            key = f"{key}_built_in"
    for part in ("title", "description"):
        item[part] = _PLACEHOLDER.sub(
            lambda match: values.get(match.group(1), match.group(0)),
            translations.get(f"{_prefix(key)}.{part}", ""),
        )
    return item


def _prefix(key: str) -> str:
    return f"component.{DOMAIN}.issues.{key}"


def _about(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """Name, version and issue tracker of an integration."""
    try:
        integration = async_get_loaded_integration(hass, domain)
    except IntegrationNotLoaded:
        return {"name": domain, "built_in": False, "version": None, "issues_url": ""}
    if integration.is_built_in:
        issues_url = (
            "https://github.com/home-assistant/core/issues"
            f"?q=is%3Aissue+is%3Aopen+label%3A%22integration%3A+{domain}%22"
        )
    else:
        issues_url = integration.issue_tracker or integration.documentation or ""
    return {
        "name": integration.name,
        "built_in": integration.is_built_in,
        "version": None if integration.version is None else str(integration.version),
        "issues_url": issues_url,
    }
