"""Tests for setup, unload and diagnostics."""

from asyncio.events import Handle
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)

from custom_components.integration_insights.attribution import Packages
from custom_components.integration_insights.const import DOMAIN
from custom_components.integration_insights.sampler import is_supported
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, HomeAssistant
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
async def test_subscription_survives_a_reload(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """A panel subscribed before a reload keeps getting summaries after it."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe", "minutes": 5})
    msg_id = (await client.receive_json())["id"]
    assert (await client.receive_json())["id"] == msg_id

    assert await hass.config_entries.async_reload(entry.entry_id)
    entry.runtime_data._async_drain()  # noqa: SLF001
    assert (await client.receive_json())["id"] == msg_id
    assert await hass.config_entries.async_unload(entry.entry_id)


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


@linux_only
async def test_executor_jobs_reach_the_report(hass: HomeAssistant) -> None:
    """Jobs from hass.async_add_executor_job count for the submitting module."""
    namespace = {"__name__": "custom_components.busy.worker"}
    exec(  # noqa: S102
        compile(
            "def spin(seconds):\n    import time\n    end = time.thread_time() + seconds\n"
            "    while time.thread_time() < end:\n        pass\n",
            "/config/custom_components/busy/worker.py",
            "exec",
        ),
        namespace,
    )
    await hass.async_add_executor_job(int)  # make sure the default executor exists
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    executor = hass.loop._default_executor  # noqa: SLF001
    assert "submit" in vars(executor)

    await hass.async_add_executor_job(namespace["spin"], 0.1)
    report = entry.runtime_data.report()
    busy = next(item for item in report["integrations"] if item["domain"] == "busy")
    assert busy["executor"]["cpu_ms_per_min"] > 0
    assert busy["loop_cpu_ms_per_min"] == 0
    assert report["sampler"]["executor_jobs_total"] >= 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert "submit" not in vars(executor)


@linux_only
async def test_library_map_looks_up_seen_libraries(hass: HomeAssistant) -> None:
    """Only libraries seen without an integration frame are looked up."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    looked_up: list[str] = []

    def read_imports(dist: str) -> tuple[str, ...] | None:
        looked_up.append(dist)
        return (dist,)

    insights._packages = Packages(  # noqa: SLF001
        lambda dist: {"aiofoo": (), "aiobar": ()}.get(dist, ()),
        read_imports,
        lambda names: {},
    )
    requirements = {"foo": ["aiofoo==1"], "bar": ["aiobar==1"]}
    with patch(
        "custom_components.integration_insights.insights.async_get_loaded_integration",
        side_effect=lambda _hass, domain: SimpleNamespace(
            requirements=requirements.get(domain, [])
        ),
    ):
        hass.config.top_level_components.update({"foo", "bar"})
        insights.collector.seen_libraries.add("aiofoo")
        insights._async_drain()  # noqa: SLF001
        await hass.async_block_till_done(wait_background_tasks=True)
        # Samples from the test itself may add real libraries such as aiohttp.
        assert insights.collector.library_map == {"aiofoo": "foo"}
        assert "aiofoo" in looked_up
        looked_up.clear()

        insights.collector.seen_libraries.add("aiobar")
        insights._async_drain()  # noqa: SLF001
        await hass.async_block_till_done(wait_background_tasks=True)
        assert insights.collector.library_map == {"aiofoo": "foo", "aiobar": "bar"}
        assert "aiobar" in looked_up
        assert "aiofoo" not in looked_up
    assert await hass.config_entries.async_unload(entry.entry_id)


@linux_only
async def test_measuring_waits_for_home_assistant_to_start(
    hass: HomeAssistant,
) -> None:
    """Nothing is measured while integrations set up; the start event begins it."""
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL
    assert [f.key for f in insights.findings()] == ["measuring"]
    assert insights.summary()["sampler"]["running_minutes"] == 0

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert signal.getsignal(signal.SIGPROF) is not signal.SIG_DFL
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL


@linux_only
async def test_unload_before_start_is_clean(hass: HomeAssistant) -> None:
    """Unloading while still waiting for the start installs nothing later."""
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL


@linux_only
async def test_callbacks_are_timed_and_charged_to_their_entry(
    hass: HomeAssistant,
) -> None:
    """A callback that blocks the loop shows up with its integration, exactly."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    namespace = {"__name__": "custom_components.blocker.api"}
    exec(  # noqa: S102
        compile(
            "import time\ndef fetch():\n    time.sleep(0.05)\n",
            "/config/custom_components/blocker/api.py",
            "exec",
        ),
        namespace,
    )
    blocker = MockConfigEntry(domain="blocker", title="Blocker")
    blocker.add_to_hass(hass)
    token = current_entry.set(blocker)
    try:
        hass.loop.call_soon(namespace["fetch"])
    finally:
        current_entry.reset(token)
    await hass.async_block_till_done()

    report = insights.report()
    row = next(item for item in report["integrations"] if item["domain"] == "blocker")
    assert row["loop_wall_ms_per_min"] > 0
    assert row["loop_cpu_ms_per_min"] == 0  # sleeping costs no CPU
    callbacks = row["callbacks"]
    assert callbacks["longest_ms"] >= 45
    assert callbacks["longest_function"] == "custom_components.blocker.api.fetch"
    assert callbacks["by_function"][0]["function"] == (
        "custom_components.blocker.api.fetch"
    )
    brief = next(
        item
        for item in insights.summary()["integrations"]
        if item["domain"] == "blocker"
    )
    assert brief["longest_ms"] == callbacks["longest_ms"]
    # A callback without an entry context counts for its module
    assert (
        insights.callback_domain("custom_components.hue.light.HueLight.turn_on", None)
        == "hue"
    )
    assert (
        insights.callback_domain("homeassistant.components.light.LightEntity.x", None)
        == "core"
    )
    assert (
        insights.callback_domain(
            "homeassistant.helpers.event._TrackTimeInterval.y", blocker.entry_id
        )
        == "blocker"
    )
    assert insights.callback_domain("asyncio.tasks.Task.step", "gone") == "core"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert Handle._run.__qualname__ == "Handle._run"  # noqa: SLF001
