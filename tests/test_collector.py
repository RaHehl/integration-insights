"""Tests for the collector and its report."""

import random
from typing import Any

import pytest

from custom_components.integration_insights.background import (
    JobRecord,
    Owner,
    ThreadRecord,
)
from custom_components.integration_insights.collector import KEPT_WINDOWS, Collector
from custom_components.integration_insights.sampler import Record

from .helpers import offset_of_line, spin_at

MS = 1_000_000


def _frames(*functions_and_lines: tuple[Any, int]) -> tuple[Any, ...]:
    frames: list[Any] = []
    for function, line in functions_and_lines:
        code = function.__code__
        frames += (code, offset_of_line(code, line))
    return tuple(frames)


def _record(
    frames: tuple[Any, ...],
    domain: str,
    cpu_ns: int,
    self_index: int = 0,
    trigger: str | None = None,
    libraries: tuple[str, ...] = (),
    entry: tuple[str, str] | None = None,
    entity_id: str | None = None,
) -> Record:
    entry_id, entry_domain = entry or (None, None)
    return Record(
        frames,
        self_index,
        domain,
        trigger,
        libraries,
        entry_id,
        entry_domain,
        cpu_ns,
        entity_id,
    )


def test_report_hot_spots_and_triggers() -> None:
    """Report charges CPU per minute with the hot code location."""
    spin = spin_at("/config/custom_components/foo/coordinator.py")
    collector = Collector()
    frames = _frames((spin, 6))
    collector.add(
        [
            _record(frames, "foo", 30 * MS, trigger="automation", entry=("e1", "foo")),
            _record(frames, "foo", 30 * MS, entry=("e2", "bar")),
            _record((), "idle", 5 * MS, self_index=-1),
        ],
        now=600.0,
    )
    report = collector.report(now=630.0, started=570.0)

    assert report["window_minutes"] == 1.0
    assert report["loop_cpu_ms_per_min"] == 65.0
    assert report["idle_ms_per_min"] == 5.0
    assert [item["domain"] for item in report["integrations"]] == ["foo"]
    foo = report["integrations"][0]
    assert foo["loop_cpu_ms_per_min"] == 60.0
    assert foo["samples"] == 2
    assert foo["low_confidence"] is True
    assert foo["triggered_by"] == {"automation": 30.0}
    assert foo["config_entries"] == {"e1": 30.0}
    assert foo["hot_spots"] == [
        {
            "function": "custom_components/foo/coordinator.py spin",
            "loop_cpu_ms_per_min": 60.0,
            "samples": 2,
            "lines": {"6": 60.0},
        }
    ]
    assert foo["stacks"] == [
        {
            "loop_cpu_ms_per_min": 60.0,
            "frames": ["custom_components/foo/coordinator.py:6 spin"],
        }
    ]


def test_library_code_is_charged_to_its_integration() -> None:
    """Samples in a package that one integration requires go to it."""
    lib = spin_at("/usr/lib/python3.14/site-packages/aiofoo/client.py")
    collector = Collector()
    collector.library_map = {"aiofoo": "foo"}
    collector.add(
        [
            _record(
                _frames((lib, 6)), "core", 10 * MS, -1, libraries=("aiohttp", "aiofoo")
            ),
            _record(_frames((lib, 6)), "core", 10 * MS, -1, libraries=("other",)),
        ],
        now=60.0,
    )
    report = collector.report(now=120.0, started=0.0)

    by_domain = {item["domain"]: item for item in report["integrations"]}
    assert by_domain["foo"]["via_library_ms_per_min"] == 5.0
    assert by_domain["foo"]["libraries"] == {"aiohttp": 5.0}
    assert by_domain["core"]["libraries"] == {"other": 5.0}
    assert by_domain["foo"]["hot_spots"][0]["function"] == "aiofoo/client.py spin"
    assert by_domain["foo"]["stacks"][0]["frames"] == ["aiofoo/client.py:6 spin"]


def test_window_drops_old_minutes() -> None:
    """Only the last hour counts."""
    spin = spin_at("/config/custom_components/foo/x.py")
    collector = Collector()
    collector.add([_record(_frames((spin, 6)), "foo", 60 * MS)], now=0.0)
    collector.add([_record(_frames((spin, 6)), "bar", 60 * MS)], now=3600.0)
    report = collector.report(now=3690.0, started=0.0)

    assert [item["domain"] for item in report["integrations"]] == ["bar"]
    # 60 finished minutes and the current one
    assert report["window_minutes"] == 60.5
    assert (
        collector.report(now=3630.0, started=0.0)["integrations"][1]["domain"] == "foo"
    )


def test_hot_spot_prefers_the_integrations_own_code() -> None:
    """A core helper running for an entity points to the integration's caller."""
    helper = spin_at("/usr/src/homeassistant/homeassistant/helpers/entity.py")
    caller = spin_at("/usr/src/homeassistant/homeassistant/components/demo/light.py")
    collector = Collector()
    collector.add(
        [_record(_frames((helper, 6), (caller, 5)), "demo", 10 * MS)], now=60.0
    )
    demo = collector.report(now=120.0, started=60.0)["integrations"][0]
    assert demo["hot_spots"][0]["function"] == (
        "homeassistant/components/demo/light.py spin"
    )
    assert demo["hot_spots"][0]["lines"] == {"5": 10.0}
    assert demo["stacks"][0]["frames"] == [
        "homeassistant/helpers/entity.py:6 spin",
        "homeassistant/components/demo/light.py:5 spin",
    ]


def test_gap_marker_is_shown() -> None:
    """Frames the sampler left out show up as a gap in the stack."""
    spin = spin_at("/config/custom_components/foo/x.py")
    lib = spin_at("/usr/lib/python3.14/site-packages/aiofoo/client.py")
    frames = (*_frames((lib, 6)), None, 0, *_frames((spin, 6)))
    collector = Collector()
    collector.add([_record(frames, "foo", 10 * MS, self_index=2)], now=60.0)
    foo = collector.report(now=120.0, started=60.0)["integrations"][0]
    assert foo["stacks"][0]["frames"] == [
        "aiofoo/client.py:6 spin",
        "...",
        "custom_components/foo/x.py:6 spin",
    ]
    assert foo["hot_spots"][0]["function"] == "custom_components/foo/x.py spin"


def test_entities_are_summed_per_domain() -> None:
    """CPU is summed per entity of the charged integration, hottest first."""
    spin = spin_at("/config/custom_components/foo/sensor.py")
    frames = _frames((spin, 6))
    collector = Collector()
    collector.add(
        [
            _record(frames, "foo", 2 * MS, entity_id="sensor.a"),
            _record(frames, "foo", 5 * MS, entity_id="sensor.b"),
            _record(frames, "foo", 1 * MS, entity_id="sensor.a"),
            _record(frames, "foo", 4 * MS),
        ],
        now=60.0,
    )
    report = collector.report(now=120.0, started=60.0)
    assert report["integrations"][0]["entities"] == {"sensor.b": 5.0, "sensor.a": 3.0}


@pytest.mark.parametrize("seed", range(5))
def test_kept_windows_match_a_fresh_sum(seed: int) -> None:
    """Running window sums always equal summing the minutes from scratch."""
    rng = random.Random(seed)
    spins = [spin_at(f"/config/custom_components/d{i}/x.py") for i in range(4)]
    collector = Collector()
    collector.library_map = {"aiofoo": "d1"}
    now = 600.0
    for _ in range(400):
        # Mostly a drain every 10 s, sometimes a gap of several minutes.
        now += rng.choice([10] * 9 + [rng.randint(60, 900)])
        records = []
        for _ in range(rng.randint(0, 30)):
            spin = rng.choice(spins)
            domain = rng.choice(["d0", "d1", "d2", "d3", "core", "idle"])
            records.append(
                _record(
                    _frames((spin, rng.choice([3, 4, 5, 6]))),
                    domain,
                    rng.randint(1, 30) * MS,
                    libraries=rng.choice([(), ("aiofoo",)]),
                    entity_id=rng.choice([None, "sensor.a", "sensor.b"]),
                )
            )
        collector.add(records, now)
        collector.add_jobs(
            [
                JobRecord(Owner(rng.choice(["d0", "d2", "core"]), ()), "f", MS, 2 * MS)
                for _ in range(rng.randint(0, 3))
            ],
            now,
        )
        collector.add_threads(
            [ThreadRecord(Owner("d3", ()), "t", MS)] * rng.randint(0, 2), now
        )
        collector.add_loop_busy(rng.randint(0, 50) * MS, now)
        for minutes in KEPT_WINDOWS:
            kept = collector.report(now, 0.0, minutes)
            collector._windows.pop(minutes)  # noqa: SLF001
            assert kept == collector.report(now, 0.0, minutes)


def test_summary_matches_report() -> None:
    """The summary carries the same numbers as the full report."""
    collector = Collector()
    spin = spin_at("/config/custom_components/busy/x.py")
    collector.add([_record(_frames((spin, 4)), "busy", 30 * MS)], 600.0)
    collector.add([_record(_frames((spin, 4)), "busy", 30 * MS)], 670.0)
    report = collector.report(680.0, 0.0, 5)
    summary = collector.summary(680.0, 0.0, 5)
    brief = summary["integrations"][0]
    assert {k: v for k, v in report.items() if k != "integrations"} == {
        k: v for k, v in summary.items() if k != "integrations"
    }
    assert {k: report["integrations"][0][k] for k in brief} == brief
    assert collector.detail("busy", 680.0, 0.0, 5) == report["integrations"][0]
    assert collector.detail("missing", 680.0, 0.0, 5) is None


def test_callbacks_add_wall_time_and_the_longest_block() -> None:
    """Timed callbacks give exact wall time per integration and the longest run."""
    from custom_components.integration_insights.collector import CallbackRecord  # noqa: PLC0415

    collector = Collector()
    collector.add_callbacks(
        [
            CallbackRecord(
                "foo", "custom_components.foo.a", 10, 300 * MS, 80 * MS, 1.0
            ),
            CallbackRecord("foo", "custom_components.foo.b", 2, 100 * MS, 90 * MS, 2.0),
            CallbackRecord("bar", "custom_components.bar.c", 1, 50 * MS, 50 * MS, 3.0),
        ],
        now=60.0,
    )
    # A later minute with a longer block for foo
    collector.add_callbacks(
        [
            CallbackRecord(
                "foo", "custom_components.foo.a", 1, 2_500 * MS, 2_500 * MS, 4.0
            )
        ],
        now=120.0,
    )
    summary = collector.summary(now=150.0, started=0.0, minutes=5)
    assert summary["loop_wall_ms_per_min"] == 2_950 / 2.5
    foo = next(item for item in summary["integrations"] if item["domain"] == "foo")
    assert foo["loop_wall_ms_per_min"] == 2_900 / 2.5
    assert (foo["longest_ms"], foo["longest_at"], foo["longest_function"]) == (
        2_500,
        4.0,
        "custom_components.foo.a",
    )
    bar = next(item for item in summary["integrations"] if item["domain"] == "bar")
    assert bar["longest_ms"] == 50

    detail = collector.detail("foo", now=150.0, started=0.0, minutes=5)
    assert detail is not None
    callbacks = detail["callbacks"]
    assert callbacks["per_min"] == 13 / 2.5
    assert callbacks["longest_ms"] == 2_500
    assert [row["function"] for row in callbacks["by_function"]] == [
        "custom_components.foo.a",
        "custom_components.foo.b",
    ]
    assert callbacks["by_function"][0]["per_min"] == 11 / 2.5

    # Only the window's minutes count for the longest block
    later = collector.summary(now=60 * 62.0, started=0.0, minutes=1)
    assert all("longest_ms" not in item for item in later["integrations"])


def test_core_work_in_an_entry_context_goes_to_the_entry() -> None:
    """Shared code with no integration frame counts for the config entry it ran for."""
    spin = spin_at("/usr/src/homeassistant/homeassistant/helpers/aiohttp_client.py")
    collector = Collector()
    collector.add(
        [
            _record(_frames((spin, 6)), "core", 30 * MS, entry=("e1", "foo")),
            _record(_frames((spin, 6)), "core", 20 * MS),
        ],
        now=60.0,
    )
    report = collector.report(now=120.0, started=60.0)
    rows = {item["domain"]: item for item in report["integrations"]}
    assert rows["foo"]["loop_cpu_ms_per_min"] == 30.0
    assert rows["foo"]["via_context_ms_per_min"] == 30.0
    assert rows["core"]["loop_cpu_ms_per_min"] == 20.0
    assert "via_context_ms_per_min" not in rows["core"]
