"""Tests for the collector and its report."""

from typing import Any

from custom_components.integration_insights.collector import Collector
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
    report = collector.report(now=3630.0, started=0.0)

    assert [item["domain"] for item in report["integrations"]] == ["bar"]
    assert report["window_minutes"] == 59.5


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
