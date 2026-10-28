"""Tests for memory snapshots."""

import asyncio
from datetime import UTC, datetime
import functools
from typing import Any
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.integration_insights.const import DOMAIN
from custom_components.integration_insights.heap import (
    CODE,
    DATA,
    SHARED,
    UNREACHED,
    Root,
    walk,
)
from custom_components.integration_insights.memory import (
    Resources,
    Snapshot,
    count_resources,
    imported_packages,
    make_callable_owner,
    make_heap_owner,
    make_module_owner,
    read_process,
    to_report,
)
from homeassistant.core import HassJob, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

LEAKY = "custom_components.leaky.coordinator"
SOURCE = """\
class Cache:
    pass

def on_event(event):
    pass

async def run_forever(event):
    await event.wait()
"""


def _module(name: str = LEAKY) -> dict[str, Any]:
    namespace: dict[str, Any] = {"__name__": name}
    exec(compile(SOURCE, f"/config/{name.replace('.', '/')}.py", "exec"), namespace)  # noqa: S102
    return namespace


def test_walk_counts_each_object_once() -> None:
    """Data goes to the first root that reaches it, shared strings to the pool."""
    payload = ["x" * 1000 + str(i) for i in range(50)]
    shared_key = "a key used in many places"
    holders = [shared_key] * 3
    first = {"payload": payload, "key": shared_key}
    second = {"same": payload, "own": [1.5] * 100}
    hass_like = {"everything": first}
    accounts = walk(
        [Root("first", DATA, first), Root("second", DATA, second)],
        [hass_like],
        lambda cls: "core",
    )
    assert accounts[("first", DATA)].size > 50_000
    # The list itself went to the first owner; the second keeps only its own.
    assert accounts[("second", DATA)].size < 2_000
    assert accounts[(SHARED, DATA)].count >= 1
    del holders


def test_walk_stops_at_stoppers_and_other_roots() -> None:
    """A reference to Home Assistant or to another owner's root ends the walk."""
    big = ["y" * 1000 + str(i) for i in range(20)]
    hass_like = {"big": big}
    other = {"big": big}
    mine = {"hass": hass_like, "other": other}
    accounts = walk(
        [Root("mine", DATA, mine), Root("other", DATA, other)],
        [hass_like],
        lambda cls: "core",
    )
    assert accounts[("mine", DATA)].size < 2_000
    assert accounts[("other", DATA)].size > 20_000


def test_walk_code_belongs_to_the_defining_module() -> None:
    """Functions and classes count for their module, not for whoever imports them."""
    module = _module()
    importer = _module("custom_components.other.sensor")
    importer["on_event"] = module["on_event"]
    accounts = walk(
        [
            Root("leaky", CODE, module, LEAKY),
            Root("other", CODE, importer, "custom_components.other.sensor"),
        ],
        [],
        lambda cls: "core",
    )
    assert accounts[("leaky", CODE)].size > accounts[("other", CODE)].size > 0


async def test_walk_counts_locals_of_running_tasks() -> None:
    """A suspended coroutine's locals are data of the task's owner."""

    async def hold(event: asyncio.Event) -> None:
        buffer = bytearray(200_000)
        await event.wait()
        del buffer

    event = asyncio.Event()
    task = asyncio.get_running_loop().create_task(hold(event))
    await asyncio.sleep(0)
    accounts = walk([Root("leaky", DATA, task)], [], lambda cls: "core")
    assert accounts[("leaky", DATA)].size > 200_000
    event.set()
    await task


def test_walk_puts_unreached_objects_by_type() -> None:
    """Objects no root reached are accounted by the module of their class."""
    cache = _module()["Cache"]
    orphans = [cache() for _ in range(100)]
    accounts = walk([], [], lambda cls: "leaky" if cls is cache else "core")
    assert accounts[("leaky", UNREACHED)].count >= 100
    del orphans


def test_heap_owner_names() -> None:
    """Modules map to integrations, core, Python, mapped libraries or libraries."""
    owner = make_heap_owner({"aiofoo": "foo"})
    assert owner("custom_components.leaky.x") == "leaky"
    assert owner("homeassistant.components.hue.light") == "hue"
    assert owner("homeassistant.helpers.entity") == "core"
    assert owner("asyncio.events") == "python"
    assert owner("aiofoo.client") == "foo"
    assert owner("aiohttp.client") == "lib:aiohttp"


def test_module_owner_uses_library_map() -> None:
    """Library classes count for the one integration that needs the library."""
    owner = make_module_owner({"aiofoo": "foo"})
    assert owner("custom_components.leaky.x") == "leaky"
    assert owner("homeassistant.components.hue.light") == "hue"
    assert owner("aiofoo.client") == "foo"
    assert owner("builtins") == "core"


def test_callable_owner_unwraps_home_assistant_wrappers() -> None:
    """Jobs, one time listeners and partials are traced to the callback inside."""
    on_event = _module()["on_event"]
    owner = make_callable_owner({})
    job = HassJob(on_event)

    class OneTime:
        listener_job = job

    def helper(callback: Any) -> None:
        pass

    assert owner(on_event) == "leaky"
    assert owner(job) == "leaky"
    assert owner(OneTime()) == "leaky"
    assert owner(functools.partial(helper, on_event)) == "leaky"
    assert owner(print, (job,)) == "leaky"
    assert owner(print) == "core"


async def test_count_resources(hass: HomeAssistant) -> None:
    """Listeners, dispatcher connections, timers and tasks count per integration."""
    module = _module()
    before = count_resources(hass, make_callable_owner({}), make_module_owner({})).get(
        "leaky", Resources()
    )
    unsubs = [
        hass.bus.async_listen("leaky_event", module["on_event"]),
        async_dispatcher_connect(hass, "leaky_signal", module["on_event"]),
        async_call_later(hass, 3600, module["on_event"]),
    ]
    event = asyncio.Event()
    # A plain task: hass.async_block_till_done would wait for a tracked one.
    task = asyncio.get_running_loop().create_task(module["run_forever"](event))
    await asyncio.sleep(0)

    resources = count_resources(hass, make_callable_owner({}), make_module_owner({}))
    leaky = resources["leaky"]
    assert leaky.listeners == before.listeners + 1
    assert leaky.dispatcher == before.dispatcher + 1
    assert leaky.timers == before.timers + 1
    assert leaky.tasks == before.tasks + 1

    for unsub in unsubs:
        unsub()
    event.set()
    await task


def test_read_process() -> None:
    """The process figures include the resident size."""
    assert read_process()["rss_mib"] > 0


def test_report_with_balance_and_changes() -> None:
    """The report shows the balance against the resident size and the changes."""
    first = Snapshot(
        datetime(2026, 1, 1, tzinfo=UTC),
        10.0,
        {"rss_mib": 100.0},
        {"leaky": {DATA: 1024 * 1024}, "core:states": {DATA: 2 * 1024 * 1024}},
        {"leaky": Resources(listeners=1)},
    )
    second = Snapshot(
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        12.0,
        {"rss_mib": 150.0},
        {
            "leaky": {DATA: 3 * 1024 * 1024, CODE: 1024 * 1024},
            "core:states": {DATA: 2 * 1024 * 1024},
            "lib:aiohttp": {CODE: 1024 * 1024},
        },
        {"leaky": Resources(listeners=4, timers=2)},
    )
    report = to_report(second, first)
    assert report["balance"] == {
        "python_mib": 7.0,
        "integration_mib": 4.0,
        "library_mib": 1.0,
        "core_mib": 2.0,
        "rss_mib": 150.0,
        "not_in_python_mib": 143.0,
    }
    leaky = report["owners"][0]
    assert leaky["owner"] == "leaky"
    assert (leaky["data_kib"], leaky["code_kib"]) == (3072.0, 1024.0)
    assert leaky["change"] == {
        "kib": 3072.0,
        "listeners": 3,
        "dispatcher": 0,
        "timers": 2,
        "tasks": 0,
    }
    assert report["process_change"] == {"rss_mib": 50.0}
    assert "change" not in to_report(first, None)["owners"][0]


async def test_snapshot_commands(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """Snapshots are taken on request and compared with the one before."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/memory"})
    assert (await client.receive_json())["result"]["memory"] == {"snapshots": []}

    await client.send_json_auto_id({"type": f"{DOMAIN}/memory_snapshot"})
    first = (await client.receive_json())["result"]["memory"]
    assert len(first["snapshots"]) == 1
    assert first["latest"]["process"]["rss_mib"] > 0
    assert "previous_taken" not in first["latest"]

    module = _module()
    unsub = hass.bus.async_listen("leaky_event", module["on_event"])
    hass.data["leaky"] = ["z" * 1000 + str(i) for i in range(500)]
    hass.config.components.add("leaky")
    await client.send_json_auto_id({"type": f"{DOMAIN}/memory_snapshot"})
    result = (await client.receive_json())["result"]
    latest = result["memory"]["latest"]
    leaky = next(row for row in latest["owners"] if row["owner"] == "leaky")
    assert leaky["change"]["listeners"] == 1
    assert leaky["data_kib"] > 500
    assert latest["balance"]["python_mib"] > 0
    assert latest["balance"]["not_in_python_mib"] > 0
    assert "names" in result
    unsub()
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_walk_runs_no_python_code() -> None:
    """A mock creates children when measured; the walk must still end."""
    mock = MagicMock()
    mock.child.grandchild.value = 1
    accounts = walk([Root("mocked", DATA, {"mock": mock})], [], lambda cls: "core")
    assert accounts[("mocked", DATA)].count > 0


def test_imported_packages_are_third_party() -> None:
    """Only third party packages count, not Python or Home Assistant itself."""
    packages = imported_packages()
    assert "aiohttp" in packages
    assert {"asyncio", "homeassistant", "custom_components"}.isdisjoint(packages)
