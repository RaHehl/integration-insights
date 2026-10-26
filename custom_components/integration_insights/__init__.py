"""Integration Insights: show how much each integration loads Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import DOMAIN
from .insights import Insights
from .sampler import SamplerError, is_supported

type IntegrationInsightsConfigEntry = ConfigEntry[Insights]


async def async_setup_entry(
    hass: HomeAssistant, entry: IntegrationInsightsConfigEntry
) -> bool:
    """Set up Integration Insights from a config entry."""
    if not is_supported():
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="not_supported"
        )
    insights = Insights(hass)
    try:
        insights.start()
    except SamplerError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="sampler_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    entry.runtime_data = insights
    entry.async_on_unload(insights.stop)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: IntegrationInsightsConfigEntry
) -> bool:
    """Unload a config entry."""
    return True
