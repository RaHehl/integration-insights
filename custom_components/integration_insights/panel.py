"""Sidebar panel for Integration Insights."""

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN, PANEL_COMPONENT, PANEL_URL_PATH, STATIC_URL

_FRONTEND_DIR = Path(__file__).parent / "frontend"
_STATIC_REGISTERED: HassKey[bool] = HassKey(f"{DOMAIN}_static")


async def async_register_panel(hass: HomeAssistant, version: str) -> None:
    """Add the panel to the sidebar, for admins only."""
    # Static paths can't be removed, so they stay across reloads.
    if not hass.data.get(_STATIC_REGISTERED):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL, str(_FRONTEND_DIR), cache_headers=False)]
        )
        hass.data[_STATIC_REGISTERED] = True
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title="Integration Insights",
        sidebar_icon="mdi:speedometer",
        module_url=f"{STATIC_URL}/panel.js?v={version}",
        require_admin=True,
    )


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Remove the panel from the sidebar."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
