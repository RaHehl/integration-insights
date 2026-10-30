"""Tests for findings."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.integration_insights.const import DOMAIN
from custom_components.integration_insights.findings import (
    INFO,
    NOTICE,
    PROBLEM,
    Finding,
    find,
    verdict,
)
from homeassistant.core import HomeAssistant


def _summary(
    integrations: list[dict[str, Any]],
    loop: float = 3_000,
    busy: float | None = None,
    minutes: float = 15,
    samples_lost: int = 0,
    handler_errors: int = 0,
) -> dict[str, Any]:
    return {
        "window_minutes": minutes,
        "loop_cpu_ms_per_min": loop,
        "loop_busy_ms_per_min": loop if busy is None else busy,
        "samples": 10_000,
        "samples_lost": samples_lost,
        "handler_errors": handler_errors,
        "integrations": integrations,
    }


def _item(
    domain: str, loop: float = 0, background: float = 0, jobs: float = 0
) -> dict[str, Any]:
    return {
        "domain": domain,
        "loop_cpu_ms_per_min": loop,
        "background_cpu_ms_per_min": background,
        "jobs_per_min": jobs,
    }


def _detail(**extra: Any) -> dict[str, Any]:
    return {"hot_spots": [], "triggered_by": {}, **extra}


def _keys(findings: list[Finding]) -> list[tuple[str, str, str | None]]:
    return [(f.key, f.severity, f.domain) for f in findings]


def test_quiet_system_has_no_findings() -> None:
    """Small numbers everywhere: nothing to say."""
    findings = find(_summary([_item("hue", 200, 100)]), lambda _: _detail(), {})
    assert findings == []
    assert verdict(findings) == "ok"


def test_busy_loop_needs_both_amount_and_share() -> None:
    """A big integration on a busy system is not flagged for being the biggest."""
    hot = {
        "function": "custom_components/foo/coordinator.py FooCoordinator._parse",
        "loop_cpu_ms_per_min": 900,
        "lines": {"41": 100, "57": 700},
    }
    detail = _detail(hot_spots=[hot], triggered_by={"event state_changed": 800})

    findings = find(_summary([_item("foo", 1_500)], loop=3_000), lambda _: detail, {})
    assert _keys(findings) == [("busy_loop", NOTICE, "foo")]
    assert findings[0].values == {"ms": "1,500", "share": "50", "minutes": "15"}
    assert findings[0].evidence == [
        "custom_components/foo/coordinator.py FooCoordinator._parse (line 57): 900 ms/min",
        "triggered by event state_changed: 800 ms/min",
    ]

    assert (
        find(_summary([_item("foo", 1_500)], loop=10_000), lambda _: detail, {}) == []
    )
    assert find(_summary([_item("foo", 999)], loop=1_000), lambda _: detail, {}) == []
    assert _keys(
        find(_summary([_item("foo", 6_000)], loop=8_000), lambda _: detail, {})
    ) == [("busy_loop", PROBLEM, "foo")]


def test_background_threads_and_jobs() -> None:
    """A thread working around the clock, and an integration that polls through the executor."""
    detail = _detail(
        threads={
            "cpu_ms_per_min": 510,
            "by_name": {"AnyIO worker thread (watchfiles.RustNotify.watch)": 510},
        },
        executor={
            "cpu_ms_per_min": 40,
            "jobs_per_min": 900,
            "jobs": [
                {
                    "function": "foo.api.Client.read",
                    "per_min": 900,
                    "cpu_ms_per_min": 40,
                }
            ],
        },
    )
    findings = find(_summary([_item("solar", 0, 550)]), lambda _: detail, {})
    assert _keys(findings) == [
        ("busy_thread", NOTICE, "solar"),
        ("busy_executor", NOTICE, "solar"),
    ]
    assert findings[0].evidence == [
        "thread AnyIO worker thread (watchfiles.RustNotify.watch): 510 ms/min"
    ]
    assert findings[1].values == {"ms": "40", "jobs": "900", "minutes": "15"}
    assert findings[1].evidence == ["foo.api.Client.read: 900/min, 40 ms/min"]

    quiet = _detail(
        threads={"cpu_ms_per_min": 299, "by_name": {}},
        executor={"cpu_ms_per_min": 0, "jobs_per_min": 599, "jobs": []},
    )
    assert find(_summary([_item("solar", 0, 300)]), lambda _: quiet, {}) == []


def test_heavy_executor_work_is_its_own_finding() -> None:
    """Few jobs that use a lot of CPU are heavy work, not polling."""
    heavy = _detail(
        executor={
            "cpu_ms_per_min": 7_000,
            "jobs_per_min": 2,
            "jobs": [{"function": "foo.render", "per_min": 2, "cpu_ms_per_min": 7_000}],
        }
    )
    findings = find(_summary([_item("foo", 0, 7_000)]), lambda _: heavy, {})
    assert _keys(findings) == [("heavy_executor", PROBLEM, "foo")]
    assert findings[0].values == {"ms": "7,000", "jobs": "2", "minutes": "15"}

    # Heavy and many at once: one finding, the heavy one
    both = _detail(executor={"cpu_ms_per_min": 400, "jobs_per_min": 900, "jobs": []})
    assert _keys(find(_summary([_item("foo", 0, 400)]), lambda _: both, {})) == [
        ("heavy_executor", NOTICE, "foo")
    ]


def test_home_assistant_itself_is_not_flagged() -> None:
    """Nothing a user can update or disable."""
    detail = _detail(threads={"cpu_ms_per_min": 9_000, "by_name": {}})
    assert (
        find(_summary([_item("core", 9_000, 9_000)], loop=9_000), lambda _: detail, {})
        == []
    )


def test_loop_waits_measuring_and_sampler() -> None:
    """Findings that are about the system or the measurement, not one integration."""
    findings = find(
        _summary([], loop=1_000, busy=14_000, samples_lost=200), lambda _: None, {}
    )
    assert _keys(findings) == [
        ("loop_waits", PROBLEM, None),
        ("measuring_incomplete", INFO, None),
    ]
    assert findings[1].evidence == ["errors 0, lost 200 of 10000"]
    assert verdict(findings) == PROBLEM
    assert find(_summary([], loop=1_000, busy=3_999), lambda _: None, {}) == []
    assert find(_summary([], samples_lost=99), lambda _: None, {}) == []

    # Too short a window: only say so, and how long it takes
    (short,) = find(_summary([], busy=14_000, minutes=3.2), lambda _: None, {})
    assert (short.key, short.values) == ("measuring", {"minutes": "2"})


def test_growing_resources_need_two_snapshots() -> None:
    """Listeners and timers that pile up between snapshots, for integrations only."""

    def row(owner: str, group: str, listeners: int) -> dict[str, Any]:
        return {
            "owner": owner,
            "group": group,
            "listeners": 130,
            "dispatcher": 0,
            "timers": 4,
            "tasks": 0,
            "change": {
                "listeners": listeners,
                "dispatcher": 0,
                "timers": 19,
                "tasks": 0,
            },
        }

    latest = {
        "previous_taken": "2026-10-29T12:00:00+00:00",
        "owners": [
            row("mqtt", "integration", 46),
            row("lib:paho", "library", 90),
            row("hue", "integration", 3),
        ],
    }
    findings = find(_summary([]), lambda _: None, {"latest": latest})
    assert _keys(findings) == [("growing_resources", NOTICE, "mqtt")]
    assert findings[0].values == {"what": "46 listeners"}
    assert (
        find(_summary([]), lambda _: None, {"latest": {"owners": latest["owners"]}})
        == []
    )


async def test_findings_are_rendered_with_where_to_act(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """The panel gets the text in the user's language and the links to act on."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    client = await hass_ws_client(hass)
    finding = Finding(
        "busy_thread",
        NOTICE,
        DOMAIN,
        {"ms": "510", "minutes": "15"},
        ["thread X: 510 ms/min"],
    )

    with patch(
        "custom_components.integration_insights.insights.Insights.findings",
        return_value=[finding],
    ):
        await client.send_json_auto_id({"type": f"{DOMAIN}/findings", "language": "en"})
        result = (await client.receive_json())["result"]

    assert result["verdict"] == NOTICE
    (item,) = result["findings"]
    assert item["title"] == "Integration Insights keeps background threads busy"
    assert "Over the last 15 minutes" in item["description"]
    assert "510 ms of CPU a minute" in item["description"]
    assert (
        "(https://github.com/RaHehl/integration-insights/issues)" in item["description"]
    )
    assert "update of Integration Insights" in item["description"]
    assert item["built_in"] is False
    assert item["evidence"] == ["thread X: 510 ms/min"]

    # A built-in integration updates with Home Assistant
    built_in = Finding(
        "busy_loop", NOTICE, "recorder", {"ms": "1,500", "minutes": "{name}"}
    )
    recorder = SimpleNamespace(
        name="Recorder", is_built_in=True, version=None, issue_tracker=None
    )
    with (
        patch(
            "custom_components.integration_insights.insights.Insights.findings",
            return_value=[built_in],
        ),
        patch(
            "custom_components.integration_insights.websocket.async_get_loaded_integration",
            return_value=recorder,
        ),
    ):
        await client.send_json_auto_id({"type": f"{DOMAIN}/findings", "language": "en"})
        (item,) = (await client.receive_json())["result"]["findings"]
    assert item["key"] == "busy_loop"
    assert item["built_in"] is True
    assert item["title"] == "Recorder keeps Home Assistant busy"
    assert "part of Home Assistant" in item["description"]
    assert "update of Recorder" not in item["description"]
    assert "label%3A%22integration%3A+recorder%22" in item["description"]
    # A value is not scanned for placeholders again
    assert "Over the last {name} minutes" in item["description"]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_built_in_background_work_needs_more() -> None:
    """The recorder writing its database is its job, a custom watcher polling is not."""
    detail = _detail(threads={"cpu_ms_per_min": 1_000, "by_name": {"Recorder": 1_000}})
    summary = _summary([_item("recorder", 0, 1_000), _item("solar", 0, 1_000)])
    findings = find(
        summary, lambda _: detail, {}, built_in=lambda domain: domain == "recorder"
    )
    assert _keys(findings) == [("busy_thread", NOTICE, "solar")]


def test_own_cost_is_not_a_finding() -> None:
    """The panel shows it as measuring cost."""
    detail = _detail(executor={"cpu_ms_per_min": 1_400, "jobs_per_min": 7, "jobs": []})
    assert find(_summary([_item(DOMAIN, 100, 1_400)]), lambda _: detail, {}) == []


async def test_findings_wait_for_home_assistant_to_settle(hass: HomeAssistant) -> None:
    """Start-up work would look like load: findings begin five settled minutes later."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    started = insights.started

    with patch(
        "custom_components.integration_insights.insights.time.monotonic",
        return_value=started + 6 * 60,
    ):
        assert _keys(insights.findings()) == [("measuring", INFO, None)]
        assert insights.findings()[0].values == {"minutes": "5"}
    with (
        patch(
            "custom_components.integration_insights.insights.time.monotonic",
            return_value=started + 11 * 60,
        ),
        patch(
            "custom_components.integration_insights.insights.find", return_value=[]
        ) as find_mock,
    ):
        assert insights.findings(60) == []
    summary = find_mock.call_args.args[0]
    # The first five minutes are left out, the window is still long enough
    assert 5 <= summary["window_minutes"] <= 6
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_five_minute_window_is_long_enough_for_findings(
    hass: HomeAssistant,
) -> None:
    """The shortest window the panel offers covers at least five minutes."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    for seconds in range(0, 60, 7):
        with patch(
            "custom_components.integration_insights.insights.time.monotonic",
            return_value=insights.started + 30 * 60 + seconds,
        ):
            assert "measuring" not in [f.key for f in insights.findings(5)]
            assert insights.summary(5)["window_minutes"] >= 5
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_shown_findings_hold_until_clearly_below() -> None:
    """A value close to the threshold does not make a finding come and go."""
    detail = _detail(
        threads={"cpu_ms_per_min": 280, "by_name": {"poller": 280}},
        executor={"cpu_ms_per_min": 0, "jobs_per_min": 500, "jobs": []},
    )
    summary = _summary([_item("foo", 900, 280)], loop=4_000, busy=6_500)
    assert find(summary, lambda _: detail, {}) == []

    shown = {
        ("busy_loop", "foo"): NOTICE,
        ("busy_thread", "foo"): NOTICE,
        ("busy_executor", "foo"): NOTICE,
        ("loop_waits", None): NOTICE,
    }
    assert _keys(find(summary, lambda _: detail, {}, shown=shown)) == [
        ("busy_loop", NOTICE, "foo"),
        ("busy_thread", NOTICE, "foo"),
        ("busy_executor", NOTICE, "foo"),
        ("loop_waits", NOTICE, None),
    ]

    low = _detail(threads={"cpu_ms_per_min": 230, "by_name": {}})
    summary = _summary([_item("foo", 790, 230)], loop=4_000, busy=6_300)
    assert find(summary, lambda _: low, {}, shown=shown) == []


def test_severity_holds_near_the_problem_threshold() -> None:
    """A problem does not flip to a notice and back at the boundary."""
    detail = _detail(threads={"cpu_ms_per_min": 5_500, "by_name": {}})
    summary = _summary([_item("foo", 5_500, 5_500)], loop=6_000, busy=17_000)
    assert _keys(find(summary, lambda _: detail, {})) == [
        ("busy_loop", NOTICE, "foo"),
        ("busy_thread", NOTICE, "foo"),
        ("loop_waits", NOTICE, None),
    ]
    shown = {
        ("busy_loop", "foo"): PROBLEM,
        ("busy_thread", "foo"): PROBLEM,
        ("loop_waits", None): PROBLEM,
    }
    assert _keys(find(summary, lambda _: detail, {}, shown=shown)) == [
        ("busy_loop", PROBLEM, "foo"),
        ("busy_thread", PROBLEM, "foo"),
        ("loop_waits", PROBLEM, None),
    ]
    shown = dict.fromkeys(shown, NOTICE)
    assert all(
        f.severity == NOTICE for f in find(summary, lambda _: detail, {}, shown=shown)
    )


async def test_insights_remember_what_was_shown(hass: HomeAssistant) -> None:
    """The findings of the last call hold for a while, whatever the window."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    finding = Finding("busy_thread", NOTICE, "foo")
    monotonic = "custom_components.integration_insights.insights.time.monotonic"

    with patch(
        "custom_components.integration_insights.insights.find",
        return_value=[finding],
    ) as find_mock:
        with patch(monotonic, return_value=insights.started + 30 * 60):
            insights.findings(15)
            insights.findings(15)
            insights.findings(60)
        with patch(monotonic, return_value=insights.started + 40 * 60):
            insights.findings(15)
    shown = [dict(call.args[4]) for call in find_mock.call_args_list]
    held = {("busy_thread", "foo"): NOTICE}
    assert shown == [{}, held, held, {}]
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_many_cheap_jobs_are_found() -> None:
    """Polling through the executor shows in the job count, not in CPU."""
    detail = _detail(
        executor={
            "cpu_ms_per_min": 12,
            "jobs_per_min": 750,
            "jobs": [{"function": "foo.read", "per_min": 750, "cpu_ms_per_min": 12}],
        }
    )
    summary = _summary([_item("foo", 5, 12, jobs=750)])
    assert _keys(find(summary, lambda _: detail, {})) == [
        ("busy_executor", NOTICE, "foo")
    ]


def test_one_long_block_is_found_at_once() -> None:
    """A stop of the loop is named by the code that ran, no rate needed."""
    item = {
        **_item("foo"),
        "blocked_ms": 3_200,
        "blocked_at": 1_700_000_000.0,
        "blocked_function": "custom_components/foo/api.py Client.fetch",
        # A long callback alone is not enough: its own code may have waited
        "longest_ms": 9_000,
    }
    detail = _detail()
    findings = find(_summary([item]), lambda _: detail, {})
    assert _keys(findings) == [("loop_blocked", NOTICE, "foo")]
    assert findings[0].values == {
        "ms": "3,200",
        "function": "custom_components/foo/api.py Client.fetch",
        "minutes": "15",
    }
    assert findings[0].evidence == [
        "custom_components/foo/api.py Client.fetch: 3,200 ms,"
        " ended 2023-11-14 22:13:20 UTC"
    ]
    assert find(_summary([{**item, "blocked_ms": 1_999}]), lambda _: detail, {}) == []
    assert _keys(
        find(_summary([{**item, "blocked_ms": 12_000}]), lambda _: detail, {})
    ) == [("loop_blocked", PROBLEM, "foo")]
    unnamed = {
        key: value for key, value in item.items() if not key.startswith("blocked")
    }
    assert find(_summary([unnamed]), lambda _: detail, {}) == []


def test_too_many_state_writes_are_found() -> None:
    """Ten state changes a second is a device reporting, not changing."""
    item = {
        **_item("foo"),
        "state_writes_per_min": 5_231,
        "state_changes_per_min": 131,
        "state_attributes_only_per_min": 1_100,
        "state_repeated_per_min": 4_000,
    }
    detail = _detail(
        state_writes={
            "per_min": 5_231,
            "changes_per_min": 131,
            "attributes_only_per_min": 1_100,
            "repeated_per_min": 4_000,
            "entities": {"sensor.foo_power": 900.4, "sensor.foo_temp": 300},
        }
    )
    findings = find(_summary([item]), lambda _: detail, {})
    assert _keys(findings) == [("busy_states", NOTICE, "foo")]
    assert findings[0].values == {
        "per_min": "1,231",
        "attributes_only": "1,100",
        "minutes": "15",
    }
    assert findings[0].evidence == [
        "sensor.foo_power: 900/min",
        "sensor.foo_temp: 300/min",
    ]
    # Repeated writes change nothing and are cheap
    assert (
        find(
            _summary([{**item, "state_attributes_only_per_min": 468}]),
            lambda _: detail,
            {},
        )
        == []
    )
    # Built-in integrations such as mqtt write state for what the user wired up
    built_in = find(
        _summary([{**item, "domain": "mqtt"}]),
        lambda _: detail,
        {},
        built_in=lambda d: True,
    )
    assert built_in == []
    assert _keys(
        find(_summary([{**item, "state_changes_per_min": 3_000}]), lambda _: detail, {})
    ) == [("busy_states", PROBLEM, "foo")]
