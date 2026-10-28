"""Diagnostics for Integration Insights."""

from typing import Any

from homeassistant.core import HomeAssistant

from . import IntegrationInsightsConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: IntegrationInsightsConfigEntry
) -> dict[str, Any]:
    """Return the load report.

    It holds code locations only, no arguments or local variables.
    """
    insights = entry.runtime_data
    return {**insights.report(), "memory": insights.memory_report()}
