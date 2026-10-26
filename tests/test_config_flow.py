"""Tests for the config flow."""

from unittest.mock import patch

from custom_components.integration_insights.const import DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_user_flow(hass: HomeAssistant) -> None:
    """The user step creates the entry."""
    with (
        patch(
            "custom_components.integration_insights.config_flow.is_supported",
            return_value=True,
        ),
        patch(
            "custom_components.integration_insights.async_setup_entry",
            return_value=True,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Integration Insights"


async def test_user_flow_unsupported(hass: HomeAssistant) -> None:
    """The flow aborts where the sampler can't run."""
    with patch(
        "custom_components.integration_insights.config_flow.is_supported",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"
