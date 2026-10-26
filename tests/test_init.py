"""Tests for setup, unload and diagnostics."""

from pathlib import Path
import signal
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.integration_insights.const import DOMAIN
from custom_components.integration_insights.sampler import is_supported
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .helpers import spin_at

linux_only = pytest.mark.skipif(not is_supported(), reason="needs timer_create")


async def test_setup_fails_on_unsupported_system(hass: HomeAssistant) -> None:
    """Setup stops with an error where the sampler can't run."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.integration_insights.is_supported", return_value=False
    ):
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL


@linux_only
async def test_setup_diagnostics_unload(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    """The sampler runs while the entry is loaded and shows up in diagnostics."""
    assert await async_setup_component(hass, "diagnostics", {})
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert signal.getsignal(signal.SIGPROF) is not signal.SIG_DFL

    spin_at("/config/custom_components/busy/coordinator.py")(0.3)
    report = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    domains = [item["domain"] for item in report["integrations"]]
    assert domains[0] == "busy"
    assert report["sampler"]["handler_errors"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL


def _timers() -> int:
    lines = Path("/proc/self/timers").read_text().splitlines()
    return sum(line.startswith("ID:") for line in lines)


@linux_only
async def test_reload_keeps_one_timer(hass: HomeAssistant) -> None:
    """Reloading replaces the timer, unloading removes it."""
    timers = _timers()
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert _timers() == timers + 1
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert _timers() == timers + 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _timers() == timers


@linux_only
async def test_stop_event_removes_sampler(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Home Assistant stopping removes the sampler; unload after it is clean."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "Unable to remove unknown job listener" not in caplog.text
