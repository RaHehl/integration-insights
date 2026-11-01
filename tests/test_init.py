"""Tests for setup, unload and diagnostics."""

from asyncio.events import Handle
from datetime import timedelta
import logging
from pathlib import Path
import signal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

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
async def test_failed_start_leaves_nothing_behind(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A start that fails half way removes what it had installed."""
    executor = hass.loop._default_executor  # noqa: SLF001
    submit = executor.submit if executor is not None else None
    hass.set_state(CoreState.starting)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    hass.set_state(CoreState.running)
    with patch(
        "custom_components.integration_insights.insights.Insights._listen_state_writes",
        side_effect=RuntimeError("boom"),
    ):
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
    assert "Could not start measuring: boom" in caplog.text
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL
    assert Handle._run.__qualname__ == "Handle._run"  # noqa: SLF001
    if executor is not None:
        assert executor.submit == submit
    ran: list[int] = []
    hass.loop.call_soon(ran.append, 1)
    await hass.async_block_till_done()
    assert ran == [1]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_translations_have_no_escaped_newlines() -> None:
    """A doubled backslash shows as text in the repair instead of a new line."""
    root = Path(__file__).parent.parent / "custom_components" / DOMAIN
    for path in (root / "strings.json", *(root / "translations").glob("*.json")):
        assert "\\\\n" not in path.read_text(), path


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
            "import time\ndef fetch():\n    time.sleep(0.05)\n"
            "def poll(_now):\n    time.sleep(0.02)\n",
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
    # A YAML integration's timer: no entry, the helper must be looked through
    unsub = async_track_time_interval(hass, namespace["poll"], timedelta(seconds=30))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    unsub()

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
    functions = {r["function"] for r in callbacks["by_function"]}
    assert "custom_components.blocker.api.poll" in functions
    # State writes of the integration's entities, with and without a change
    registry = er.async_get(hass)
    entity_id = registry.async_get_or_create("sensor", "blocker", "u1").entity_id
    for value in ("1", "1", "1", "2"):
        hass.states.async_set(entity_id, value)
    # The same value with another attribute: a write that keeps the state
    hass.states.async_set(entity_id, "2", {"voltage": 230})
    hass.states.async_set("sensor.unregistered", "x")
    # Measured in a callback of its own, not in the one that wrote the state
    assert insights._state_writes["blocker"][4] == 0  # noqa: SLF001
    await hass.async_block_till_done()
    # The first write brought empty attributes, the last one the voltage
    assert insights._state_writes["blocker"][4] == len(b"{}") + len(b'{"voltage":230}')  # noqa: SLF001
    report = insights.report()
    row = next(item for item in report["integrations"] if item["domain"] == "blocker")
    assert set(row["state_writes"]) == {
        "per_min",
        "changes_per_min",
        "attributes_only_per_min",
        "repeated_per_min",
        "entities",
        "attribute_kib_per_min",
        "attribute_entities",
    }
    # Only the write with the voltage attribute had attributes to measure
    assert list(row["state_writes"]["attribute_entities"]) == [entity_id]
    # 5 writes: 2 changed the value, 2 repeated it, 1 changed only attributes
    sw = row["state_writes"]
    assert sw["attributes_only_per_min"] > 0
    assert sw["changes_per_min"] == 2 * sw["attributes_only_per_min"]
    assert sw["repeated_per_min"] == 2 * sw["attributes_only_per_min"]
    assert sw["per_min"] == 5 * sw["attributes_only_per_min"]
    # Busiest entities by what reaches listeners: repeated writes do not
    assert sw["entities"][entity_id] == 3 * sw["attributes_only_per_min"]
    # Writes after a drain are counted too: the filters keep the same dict
    hass.states.async_set(entity_id, "3")
    await hass.async_block_till_done()
    later = next(
        item
        for item in insights.report()["integrations"]
        if item["domain"] == "blocker"
    )
    assert later["state_writes"]["per_min"] > row["state_writes"]["per_min"]
    # The sampler does not blame the timing wrapper for what it wraps
    assert "integration_insights" not in row["triggered_by"]
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
    assert (
        insights.callback_domain(
            "asyncio.unix_events._UnixSelectorEventLoop._read_from_self", None
        )
        == "core"
    )
    insights.collector.library_map["aiofoo"] = "foo"
    assert insights.callback_domain("aiofoo.client.Client.data_received", None) == "foo"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert Handle._run.__qualname__ == "Handle._run"  # noqa: SLF001


async def test_library_map_waits_until_the_integrations_hold_still(
    hass: HomeAssistant,
) -> None:
    """Integrations arriving one after another are mapped once they stop coming."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    with patch.object(insights, "_async_update_library_map") as update:
        for domain in ("foo", "bar"):
            hass.config.components.add(domain)
            insights._async_drain()  # noqa: SLF001
            await hass.async_block_till_done()
        assert update.call_count == 0
        insights._async_drain()  # noqa: SLF001
        await hass.async_block_till_done()
        assert update.call_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_entities_without_a_registry_entry_count_for_their_integration(
    hass: HomeAssistant,
) -> None:
    """A YAML template sensor has no unique ID and still belongs to template."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    "sensor": [
                        {
                            "name": "big",
                            "state": "{{ states('input_number.n') }}",
                            "attributes": {"blob": "{{ 'x' * 20000 }}"},
                        }
                    ]
                }
            ]
        },
    )
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get("sensor.big") is None
    hass.states.async_set("input_number.n", "1")
    await hass.async_block_till_done()
    hass.states.async_set("input_number.n", "2")
    # A drain measures what is queued, so sizes land with their writes
    insights.drain()
    logging.getLogger("homeassistant.components.recorder.db_schema").warning(
        "State attributes for %s exceed maximum size of %s bytes. "
        "This can cause database performance issues; Attributes "
        "will not be stored",
        "sensor.big",
        16384,
    )
    report = insights.report()
    row = next(item for item in report["integrations"] if item["domain"] == "template")
    assert row["state_writes"]["changes_per_min"] > 0
    assert row["state_writes"]["attribute_entities"]["sensor.big"] > 0
    assert row["logs"]["loggers"] == {
        "homeassistant.components.recorder.db_schema": row["logs"]["warnings_per_min"]
    }
    assert row["logs"]["oversized_per_min"] == row["logs"]["warnings_per_min"]
    assert "recorder" not in {item["domain"] for item in report["integrations"]}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_state_filters_survive_odd_events_and_threads(
    hass: HomeAssistant,
) -> None:
    """Events fired by hand and states set from a thread never raise in a filter."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    entity_id = (
        er.async_get(hass).async_get_or_create("sensor", "solar", "u1").entity_id
    )
    with patch("homeassistant.core._LOGGER.exception") as logged:
        hass.bus.async_fire(
            "state_changed",
            {"entity_id": entity_id, "old_state": None, "new_state": {"state": "1"}},
        )
        hass.bus.async_fire("state_changed", {"entity_id": entity_id})
        await hass.async_block_till_done()
    assert not logged.called
    # From a worker thread: counted, not measured, nothing scheduled off-loop
    await hass.async_add_executor_job(
        hass.states.async_set, entity_id, "2", {"big": "x" * 100}
    )
    await hass.async_block_till_done()
    insights.drain()
    row = insights._state_writes.get("solar")  # noqa: SLF001
    assert row is None  # moved into the collector by the drain
    sw = next(
        item for item in insights.report()["integrations"] if item["domain"] == "solar"
    )["state_writes"]
    assert sw["changes_per_min"] > 0
    assert "attribute_entities" not in sw
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_attribute_size_is_what_the_recorder_serializes(
    hass: HomeAssistant,
) -> None:
    """Attributes the integration leaves out and entities the recorder skips do not count."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    registry = er.async_get(hass)
    kept = registry.async_get_or_create("sensor", "solar", "kept").entity_id
    skipped = registry.async_get_or_create("sensor", "solar", "skipped").entity_id
    recorder = SimpleNamespace(
        entity_filter=lambda entity_id: entity_id != skipped,
        exclude_event_types=set(),
    )
    hass.data["recorder_instance"] = recorder
    try:
        hass.states.async_set(
            kept,
            "1",
            {"forecast": "x" * 1000, "unit_of_measurement": "W", "attribution": "x"},
            state_info={"unrecorded_attributes": frozenset({"forecast"})},
        )
        hass.states.async_set(skipped, "1", {"forecast": "x" * 1000})
        insights.drain()
        solar = insights.collector._buckets[-1].domains["solar"]  # noqa: SLF001
        assert solar.attribute_bytes == len(b'{"unit_of_measurement":"W"}')
        assert list(solar.attribute_entities) == [kept]
        # A recorder that stores no state changes serializes nothing
        recorder.exclude_event_types = {"state_changed"}
        hass.states.async_set(kept, "2", {"unit_of_measurement": "kW"})
        insights.drain()
        assert solar.attribute_bytes == len(b'{"unit_of_measurement":"W"}')
    finally:
        del hass.data["recorder_instance"]
    # Saves belong to whoever saves: the wrapper looks like the original and
    # the sampler looks through it
    assert insights.stores.wrapper_code in insights.sampler.skip_codes
    assert await hass.config_entries.async_unload(entry.entry_id)
