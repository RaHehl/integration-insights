"""Tests for findings."""

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
    **sampler: int,
) -> dict[str, Any]:
    return {
        "window_minutes": minutes,
        "loop_cpu_ms_per_min": loop,
        "loop_busy_ms_per_min": loop if busy is None else busy,
        "sampler": {
            "handler_errors": 0,
            "samples_lost": 0,
            "samples_total": 10_000,
            **sampler,
        },
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

    quiet = _detail(
        threads={"cpu_ms_per_min": 299, "by_name": {}},
        executor={"cpu_ms_per_min": 0, "jobs_per_min": 599, "jobs": []},
    )
    assert find(_summary([_item("solar", 0, 300)]), lambda _: quiet, {}) == []


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
    assert verdict(findings) == PROBLEM
    assert find(_summary([], loop=1_000, busy=3_999), lambda _: None, {}) == []

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
    assert item["built_in"] is False
    assert item["evidence"] == ["thread X: 510 ms/min"]
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
            return_value=started + 12 * 60,
        ),
        patch(
            "custom_components.integration_insights.insights.find", return_value=[]
        ) as find_mock,
    ):
        assert insights.findings(60) == []
    summary = find_mock.call_args.args[0]
    assert summary["window_minutes"] <= 7  # The first five minutes are left out
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_shown_findings_hold_until_clearly_below() -> None:
    """A value close to the threshold does not make a finding come and go."""
    detail = _detail(
        threads={"cpu_ms_per_min": 280, "by_name": {"poller": 280}},
        executor={"cpu_ms_per_min": 0, "jobs_per_min": 500, "jobs": []},
    )
    summary = _summary([_item("foo", 900, 280)], loop=4_000, busy=6_500)
    assert find(summary, lambda _: detail, {}) == []

    shown = frozenset(
        {
            ("busy_loop", "foo"),
            ("busy_thread", "foo"),
            ("busy_executor", "foo"),
            ("loop_waits", None),
        }
    )
    assert _keys(find(summary, lambda _: detail, {}, shown=shown)) == [
        ("busy_loop", NOTICE, "foo"),
        ("busy_thread", NOTICE, "foo"),
        ("busy_executor", NOTICE, "foo"),
        ("loop_waits", NOTICE, None),
    ]

    low = _detail(threads={"cpu_ms_per_min": 230, "by_name": {}})
    summary = _summary([_item("foo", 790, 230)], loop=4_000, busy=6_300)
    assert find(summary, lambda _: low, {}, shown=shown) == []


async def test_insights_remember_what_was_shown(hass: HomeAssistant) -> None:
    """Each window passes the findings of its last call on to the next one."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    insights = entry.runtime_data
    finding = Finding("busy_thread", NOTICE, "foo")

    with (
        patch(
            "custom_components.integration_insights.insights.time.monotonic",
            return_value=insights.started + 30 * 60,
        ),
        patch(
            "custom_components.integration_insights.insights.find",
            return_value=[finding],
        ) as find_mock,
    ):
        insights.findings(15)
        insights.findings(15)
        insights.findings(60)
    shown = [call.args[4] for call in find_mock.call_args_list]
    assert shown == [frozenset(), {("busy_thread", "foo")}, frozenset()]
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
