"""Aggregate samples into per minute buckets and build the report."""

from bisect import bisect_right
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .attribution import short_path
from .sampler import Record

WINDOW_MINUTES = 60
MAX_STACKS_PER_MINUTE = 500
MIN_CONFIDENT_SAMPLES = 20
TOP_HOT_SPOTS = 5
TOP_STACKS = 3
TOP_TRIGGERS = 5
TOP_LINES = 5

type Location = tuple[str, str, int]  # short path, qualname, line
type Stack = tuple[Location, ...]


@dataclass(slots=True)
class _Domain:
    cpu_ns: int = 0
    samples: int = 0
    via_library_ns: int = 0
    triggers: Counter[str] = field(default_factory=Counter)
    entries: Counter[str] = field(default_factory=Counter)
    libraries: Counter[str] = field(default_factory=Counter)
    hot_spots: Counter[Location] = field(default_factory=Counter)
    hot_spot_samples: Counter[Location] = field(default_factory=Counter)
    stacks: Counter[Stack] = field(default_factory=Counter)

    def merge(self, other: _Domain) -> None:
        self.cpu_ns += other.cpu_ns
        self.samples += other.samples
        self.via_library_ns += other.via_library_ns
        self.triggers.update(other.triggers)
        self.entries.update(other.entries)
        self.libraries.update(other.libraries)
        self.hot_spots.update(other.hot_spots)
        self.hot_spot_samples.update(other.hot_spot_samples)
        self.stacks.update(other.stacks)


@dataclass(slots=True)
class _Bucket:
    minute: int
    cpu_ns: int = 0
    samples: int = 0
    stack_count: int = 0
    dropped_stack_ns: int = 0
    domains: dict[str, _Domain] = field(default_factory=dict)


GAP: Location = ("...", "", 0)


class Collector:
    """Keep the last hour of samples in one minute buckets."""

    def __init__(self) -> None:
        """Initialize the collector."""
        self.library_map: dict[str, str] = {}
        self._buckets: deque[_Bucket] = deque(maxlen=WINDOW_MINUTES)
        # Keyed by id: code objects compare equal across files when their
        # bytecode matches. The value keeps the code alive so ids stay unique.
        self._locations: dict[tuple[int, int], tuple[Any, Location]] = {}
        self._lines: dict[int, tuple[Any, list[int], list[int | None]]] = {}
        self._prefixes: dict[str, tuple[str, str]] = {}

    def add(self, records: list[Record], now: float) -> None:
        """Add records that were sampled before `now` (monotonic seconds)."""
        minute = int(now // 60)
        if not self._buckets or self._buckets[-1].minute != minute:
            self._buckets.append(_Bucket(minute))
        bucket = self._buckets[-1]
        for record in records:
            self._add(bucket, record)
        if len(self._locations) > 50_000:
            self._locations.clear()
            self._lines.clear()

    def _add(self, bucket: _Bucket, record: Record) -> None:
        ns = record.cpu_ns
        domain = record.domain
        via_library = False
        if domain == "core":
            for library in record.libraries:
                if (owner := self.library_map.get(library)) is not None:
                    domain = owner
                    via_library = True
                    break
        bucket.cpu_ns += ns
        bucket.samples += 1
        if (stats := bucket.domains.get(domain)) is None:
            stats = bucket.domains[domain] = _Domain()
        stats.cpu_ns += ns
        stats.samples += 1
        if via_library:
            stats.via_library_ns += ns
        if record.trigger is not None and record.trigger != domain:
            stats.triggers[record.trigger] += ns
        # current_entry leaks into tasks that shared helpers start during an
        # entry's setup, so only count it for its own integration.
        if record.entry_id is not None and record.entry_domain == domain:
            stats.entries[record.entry_id] += ns
        if record.libraries:
            stats.libraries[record.libraries[0]] += ns
        if domain == "idle":
            return
        stack = self._stack(record.frames)
        if (
            location := self._hot_location(domain, stack, record.self_index)
        ) is not None:
            stats.hot_spots[location] += ns
            stats.hot_spot_samples[location] += 1
        if stack in stats.stacks:
            stats.stacks[stack] += ns
        elif bucket.stack_count < MAX_STACKS_PER_MINUTE:
            bucket.stack_count += 1
            stats.stacks[stack] += ns
        else:
            bucket.dropped_stack_ns += ns

    def _hot_location(
        self, domain: str, stack: Stack, self_index: int
    ) -> Location | None:
        """Pick the frame to blame, preferring the integration's own files.

        For an entity, the innermost "self" frame is often a core helper such as
        Entity._async_write_ha_state; the line in the integration that called
        it is what a developer needs.
        """
        prefixes = self._prefixes.get(domain)
        if prefixes is None:
            prefixes = self._prefixes[domain] = (
                f"custom_components/{domain}/",
                f"homeassistant/components/{domain}/",
            )
        for location in stack:
            if location[0].startswith(prefixes):
                return location
        if 0 <= self_index < len(stack):
            return stack[self_index]
        return stack[0] if stack and stack[0] is not GAP else None

    def _stack(self, frames: tuple[Any, ...]) -> Stack:
        return tuple(
            self._location(frames[i], frames[i + 1]) for i in range(0, len(frames), 2)
        )

    def _location(self, code: Any, lasti: int) -> Location:
        if code is None:
            return GAP
        key = (id(code), lasti)
        if (cached := self._locations.get(key)) is None:
            cached = self._locations[key] = (
                code,
                (
                    short_path(code.co_filename),
                    code.co_qualname,
                    self._line(code, lasti),
                ),
            )
        return cached[1]

    def _line(self, code: Any, lasti: int) -> int:
        if (table := self._lines.get(id(code))) is None:
            starts: list[int] = []
            lines: list[int | None] = []
            for start, _end, line in code.co_lines():
                starts.append(start)
                lines.append(line)
            table = self._lines[id(code)] = (code, starts, lines)
        index = bisect_right(table[1], lasti) - 1
        line = table[2][index] if index >= 0 else None
        return line if line is not None else code.co_firstlineno

    def report(self, now: float, started: float) -> dict[str, Any]:
        """Build the report over the window."""
        first_minute = int(now // 60) - WINDOW_MINUTES + 1
        window_start = max(started, first_minute * 60)
        minutes = max((now - window_start) / 60, 1 / 60)
        cpu_ns = samples = dropped_ns = 0
        domains: dict[str, _Domain] = {}
        for bucket in self._buckets:
            if bucket.minute < first_minute:
                continue
            cpu_ns += bucket.cpu_ns
            samples += bucket.samples
            dropped_ns += bucket.dropped_stack_ns
            for domain, stats in bucket.domains.items():
                if (total := domains.get(domain)) is None:
                    total = domains[domain] = _Domain()
                total.merge(stats)

        def per_minute(ns: int) -> float:
            return round(ns / 1e6 / minutes, 1)

        def top(counter: Counter[str], n: int | None = None) -> dict[str, float]:
            return {key: per_minute(ns) for key, ns in counter.most_common(n)}

        integrations = []
        for domain, stats in sorted(
            domains.items(), key=lambda item: item[1].cpu_ns, reverse=True
        ):
            if domain == "idle":
                continue
            item: dict[str, Any] = {
                "domain": domain,
                "loop_cpu_ms_per_min": per_minute(stats.cpu_ns),
                "samples": stats.samples,
                "low_confidence": stats.samples < MIN_CONFIDENT_SAMPLES,
            }
            if stats.via_library_ns:
                item["via_library_ms_per_min"] = per_minute(stats.via_library_ns)
            item["triggered_by"] = top(stats.triggers, TOP_TRIGGERS)
            item["config_entries"] = top(stats.entries)
            item["libraries"] = top(stats.libraries)
            item["hot_spots"] = _hot_spots(stats, per_minute)
            item["stacks"] = [
                {
                    "loop_cpu_ms_per_min": per_minute(ns),
                    "frames": [_format(location) for location in stack],
                }
                for stack, ns in stats.stacks.most_common(TOP_STACKS)
            ]
            integrations.append(item)

        idle = domains.get("idle")
        return {
            "window_minutes": round(minutes, 1),
            "loop_cpu_ms_per_min": per_minute(cpu_ns),
            "idle_ms_per_min": per_minute(idle.cpu_ns if idle else 0),
            "samples": samples,
            "stacks_not_kept_ms_per_min": per_minute(dropped_ns),
            "integrations": integrations,
        }


def _hot_spots(
    stats: _Domain, per_minute: Callable[[int], float]
) -> list[dict[str, Any]]:
    """Group hot locations by function, with the hottest lines of each."""
    functions: dict[tuple[str, str], list[Any]] = {}
    for location, ns in stats.hot_spots.most_common():
        path, qualname, line = location
        entry = functions.setdefault((path, qualname), [0, 0, {}])
        entry[0] += ns
        entry[1] += stats.hot_spot_samples[location]
        if len(entry[2]) < TOP_LINES:
            entry[2][line] = ns
    ranked = sorted(functions.items(), key=lambda item: item[1][0], reverse=True)
    return [
        {
            "function": f"{path} {qualname}",
            "loop_cpu_ms_per_min": per_minute(ns),
            "samples": samples,
            "lines": {str(line): per_minute(l_ns) for line, l_ns in lines.items()},
        }
        for (path, qualname), (ns, samples, lines) in ranked[:TOP_HOT_SPOTS]
    ]


def _format(location: Location) -> str:
    if location is GAP:
        return "..."
    path, qualname, line = location
    return f"{path}:{line} {qualname}"
