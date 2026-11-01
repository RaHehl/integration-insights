"""Aggregate samples into per minute buckets and build the report."""

from bisect import bisect_right
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
import heapq
from typing import Any, NamedTuple

from .attribution import short_path
from .background import JobRecord, Owner, ThreadRecord
from .sampler import Record

TOP_CALLBACKS = 5
TOP_STATE_ENTITIES = 5
TOP_LOGGERS = 5
TOP_STORE_KEYS = 5
MAX_STATE_ENTITIES_PER_MINUTE = 100

WINDOW_MINUTES = 60
MAX_STACKS_PER_MINUTE = 500
MIN_CONFIDENT_SAMPLES = 20
TOP_HOT_SPOTS = 5
TOP_STACKS = 3
TOP_TRIGGERS = 5
TOP_LINES = 5
TOP_JOBS = 5
TOP_THREADS = 5
# Windows the panel offers; their sums are kept between reports.
KEPT_WINDOWS = (5, 15, 60)

type Location = tuple[str, str, int]  # short path, qualname, line
type Stack = tuple[Location, ...]


class StateWrites(NamedTuple):
    """State writes of one integration's entities since the last drain."""

    domain: str
    changes: int
    reports: int  # writes that changed nothing
    attributes_only: int  # writes with the same value and other attributes
    entities: Counter[str]
    # Size of the attributes that changed, in bytes, in total and by entity
    attribute_bytes: int = 0
    attribute_entities: Counter[str] | None = None


class LogCounts(NamedTuple):
    """Log records of one integration since the last drain."""

    domain: str
    debug: int
    info: int
    warning: int
    error: int  # error and critical
    loggers: Counter[str]  # warnings and errors by logger


class StoreWrites(NamedTuple):
    """Files one integration wrote through Home Assistant's storage helper."""

    domain: str
    writes: int
    size: int  # bytes
    keys: Counter[str]


class BlockRecord(NamedTuple):
    """A callback that held the loop, named by the code that ran during it."""

    domain: str
    function: str
    duration_ns: int
    at: float


class CallbackRecord(NamedTuple):
    """Loop callbacks of one function for one integration, from one drain."""

    domain: str
    label: str
    calls: int
    total_ns: int
    max_ns: int
    max_at: float


@dataclass(slots=True)
class _Domain:
    cpu_ns: int = 0
    samples: int = 0
    via_library_ns: int = 0
    # Core and library code with no integration frame, charged to the config
    # entry whose context it ran in
    via_context_ns: int = 0
    triggers: Counter[str] = field(default_factory=Counter)
    entries: Counter[str] = field(default_factory=Counter)
    entities: Counter[str] = field(default_factory=Counter)
    libraries: Counter[str] = field(default_factory=Counter)
    hot_spots: Counter[Location] = field(default_factory=Counter)
    hot_spot_samples: Counter[Location] = field(default_factory=Counter)
    stacks: Counter[Stack] = field(default_factory=Counter)
    executor_cpu_ns: int = 0
    executor_wall_ns: int = 0
    jobs: Counter[str] = field(default_factory=Counter)
    job_counts: Counter[str] = field(default_factory=Counter)
    # The sum of job_counts, kept as a number for the summary
    job_total: int = 0
    thread_cpu_ns: int = 0
    threads: Counter[str] = field(default_factory=Counter)
    # Wall time of the integration's callbacks on the loop, measured exactly
    loop_wall_ns: int = 0
    callback_total: int = 0
    callbacks: Counter[str] = field(default_factory=Counter)
    callback_counts: Counter[str] = field(default_factory=Counter)
    longest_ns: int = 0
    longest_at: float = 0.0
    longest_label: str = ""
    # The longest block of the loop while this integration's code ran
    block_ns: int = 0
    block_at: float = 0.0
    block_function: str = ""
    # State writes of the integration's entities: with and without a change
    state_changes: int = 0
    state_reports: int = 0
    state_attributes_only: int = 0
    state_entities: Counter[str] = field(default_factory=Counter)
    # Bytes of attributes that changed, and by entity
    attribute_bytes: int = 0
    attribute_entities: Counter[str] = field(default_factory=Counter)
    # Log records that reached the handlers
    log_debug: int = 0
    log_info: int = 0
    log_warning: int = 0
    log_error: int = 0
    log_loggers: Counter[str] = field(default_factory=Counter)
    # Files written through the storage helper
    store_writes: int = 0
    store_bytes: int = 0
    store_keys: Counter[str] = field(default_factory=Counter)

    def note_block(self, ns: int, at: float, function: str) -> None:
        if ns > self.block_ns:
            self.block_ns, self.block_at, self.block_function = ns, at, function

    def note_longest(self, ns: int, at: float, label: str) -> None:
        if ns > self.longest_ns:
            self.longest_ns, self.longest_at, self.longest_label = ns, at, label

    def merge(self, other: _Domain) -> None:
        self.cpu_ns += other.cpu_ns
        self.samples += other.samples
        self.via_library_ns += other.via_library_ns
        self.via_context_ns += other.via_context_ns
        self.triggers.update(other.triggers)
        self.entries.update(other.entries)
        self.entities.update(other.entities)
        self.libraries.update(other.libraries)
        self.hot_spots.update(other.hot_spots)
        self.hot_spot_samples.update(other.hot_spot_samples)
        self.stacks.update(other.stacks)
        self.executor_cpu_ns += other.executor_cpu_ns
        self.executor_wall_ns += other.executor_wall_ns
        self.jobs.update(other.jobs)
        self.job_counts.update(other.job_counts)
        self.job_total += other.job_total
        self.thread_cpu_ns += other.thread_cpu_ns
        self.threads.update(other.threads)
        self.loop_wall_ns += other.loop_wall_ns
        self.callback_total += other.callback_total
        self.callbacks.update(other.callbacks)
        self.callback_counts.update(other.callback_counts)
        self.note_longest(other.longest_ns, other.longest_at, other.longest_label)
        self.note_block(other.block_ns, other.block_at, other.block_function)
        self.state_changes += other.state_changes
        self.state_reports += other.state_reports
        self.state_attributes_only += other.state_attributes_only
        self.state_entities.update(other.state_entities)
        self.attribute_bytes += other.attribute_bytes
        self.attribute_entities.update(other.attribute_entities)
        self.log_debug += other.log_debug
        self.log_info += other.log_info
        self.log_warning += other.log_warning
        self.log_error += other.log_error
        self.log_loggers.update(other.log_loggers)
        self.store_writes += other.store_writes
        self.store_bytes += other.store_bytes
        self.store_keys.update(other.store_keys)

    def merge_totals(self, other: _Domain, sign: int = 1) -> None:
        """Add (or with sign -1 remove) only the numbers, not the counters.

        The longest callback is not a sum: the window reads it from the
        minute buckets.
        """
        self.cpu_ns += sign * other.cpu_ns
        self.samples += sign * other.samples
        self.via_library_ns += sign * other.via_library_ns
        self.via_context_ns += sign * other.via_context_ns
        self.executor_cpu_ns += sign * other.executor_cpu_ns
        self.executor_wall_ns += sign * other.executor_wall_ns
        self.job_total += sign * other.job_total
        self.thread_cpu_ns += sign * other.thread_cpu_ns
        self.loop_wall_ns += sign * other.loop_wall_ns
        self.callback_total += sign * other.callback_total
        self.state_changes += sign * other.state_changes
        self.state_reports += sign * other.state_reports
        self.state_attributes_only += sign * other.state_attributes_only
        self.attribute_bytes += sign * other.attribute_bytes
        self.log_debug += sign * other.log_debug
        self.log_info += sign * other.log_info
        self.log_warning += sign * other.log_warning
        self.log_error += sign * other.log_error
        self.store_writes += sign * other.store_writes
        self.store_bytes += sign * other.store_bytes

    def totals_are_zero(self) -> bool:
        return not (
            self.cpu_ns
            or self.samples
            or self.via_library_ns
            or self.via_context_ns
            or self.executor_cpu_ns
            or self.executor_wall_ns
            or self.job_total
            or self.thread_cpu_ns
            or self.loop_wall_ns
            or self.callback_total
            or self.state_changes
            or self.state_reports
            or self.state_attributes_only
            or self.block_ns
            or self.attribute_bytes
            or self.log_debug
            or self.log_info
            or self.log_warning
            or self.log_error
            or self.store_writes
        )

    @property
    def total_cpu_ns(self) -> int:
        return self.cpu_ns + self.executor_cpu_ns + self.thread_cpu_ns


@dataclass(slots=True)
class _Bucket:
    minute: int
    cpu_ns: int = 0
    loop_busy_ns: int = 0
    loop_wall_ns: int = 0
    executor_cpu_ns: int = 0
    thread_cpu_ns: int = 0
    samples: int = 0
    samples_lost: int = 0
    handler_errors: int = 0
    stack_count: int = 0
    dropped_stack_ns: int = 0
    domains: dict[str, _Domain] = field(default_factory=dict)

    def merge_totals(self, other: _Bucket, sign: int = 1) -> None:
        """Add (or remove) the numbers of another bucket, without counters."""
        self.cpu_ns += sign * other.cpu_ns
        self.loop_busy_ns += sign * other.loop_busy_ns
        self.loop_wall_ns += sign * other.loop_wall_ns
        self.executor_cpu_ns += sign * other.executor_cpu_ns
        self.thread_cpu_ns += sign * other.thread_cpu_ns
        self.samples += sign * other.samples
        self.samples_lost += sign * other.samples_lost
        self.handler_errors += sign * other.handler_errors
        self.dropped_stack_ns += sign * other.dropped_stack_ns
        for domain, stats in other.domains.items():
            if (total := self.domains.get(domain)) is None:
                total = self.domains[domain] = _Domain()
            total.merge_totals(stats, sign)
            if total.totals_are_zero():
                del self.domains[domain]


@dataclass(slots=True)
class _Window:
    """Running sum of the numbers of the finished minutes in a window.

    Counters (stacks, hot spots, entities) stay in the minute buckets only;
    details merge them for one integration when asked.
    """

    total: _Bucket = field(default_factory=lambda: _Bucket(-1))
    minutes: dict[int, _Bucket] = field(default_factory=dict)


GAP: Location = ("...", "", 0)


class Collector:
    """Keep the last hour of samples in one minute buckets.

    A window of n minutes covers the last n finished minutes and the current
    one, so it spans at least n minutes.
    """

    def __init__(self) -> None:
        """Initialize the collector."""
        self.library_map: dict[str, str] = {}
        # Libraries seen where no integration frame was on the stack.
        self.seen_libraries: set[str] = set()
        self._buckets: deque[_Bucket] = deque(maxlen=WINDOW_MINUTES + 1)
        # Keyed by id: code objects compare equal across files when their
        # bytecode matches. The value keeps the code alive so ids stay unique.
        self._locations: dict[tuple[int, int], tuple[Any, Location]] = {}
        self._lines: dict[int, tuple[Any, list[int], list[int | None]]] = {}
        self._prefixes: dict[str, tuple[str, str]] = {}
        self._windows: dict[int, _Window] = {}
        self._stacks: dict[Stack, Stack] = {}

    def _bucket(self, now: float) -> _Bucket:
        minute = int(now // 60)
        if not self._buckets or self._buckets[-1].minute != minute:
            self._buckets.append(_Bucket(minute))
        return self._buckets[-1]

    def _owner(self, owner: Owner) -> _Domain:
        """Return the stats of the integration that owns a job or thread."""
        domain = owner.domain
        if domain == "core":
            self.seen_libraries.update(owner.libraries)
            for library in owner.libraries:
                if (mapped := self.library_map.get(library)) is not None:
                    domain = mapped
                    break
        bucket = self._buckets[-1]
        if (stats := bucket.domains.get(domain)) is None:
            stats = bucket.domains[domain] = _Domain()
        return stats

    def add_jobs(self, jobs: list[JobRecord], now: float) -> None:
        """Add executor jobs that finished before `now`."""
        bucket = self._bucket(now)
        for job in jobs:
            stats = self._owner(job.owner)
            stats.executor_cpu_ns += job.cpu_ns
            stats.executor_wall_ns += job.wall_ns
            stats.jobs[job.label] += job.cpu_ns
            stats.job_counts[job.label] += 1
            stats.job_total += 1
            bucket.executor_cpu_ns += job.cpu_ns

    def add_threads(self, threads: list[ThreadRecord], now: float) -> None:
        """Add thread CPU measured before `now`."""
        bucket = self._bucket(now)
        for thread in threads:
            stats = self._owner(thread.owner)
            stats.thread_cpu_ns += thread.cpu_ns
            stats.threads[thread.name] += thread.cpu_ns
            bucket.thread_cpu_ns += thread.cpu_ns

    def add_callbacks(self, callbacks: list[CallbackRecord], now: float) -> None:
        """Add the loop callbacks timed before `now`, already mapped to owners."""
        bucket = self._bucket(now)
        for record in callbacks:
            if (stats := bucket.domains.get(record.domain)) is None:
                stats = bucket.domains[record.domain] = _Domain()
            stats.loop_wall_ns += record.total_ns
            stats.callback_total += record.calls
            stats.callbacks[record.label] += record.total_ns
            stats.callback_counts[record.label] += record.calls
            stats.note_longest(record.max_ns, record.max_at, record.label)
            bucket.loop_wall_ns += record.total_ns

    def add_blocks(self, blocks: list[BlockRecord], now: float) -> None:
        """Add blocks of the loop that ended before `now`."""
        bucket = self._bucket(now)
        for block in blocks:
            if (stats := bucket.domains.get(block.domain)) is None:
                stats = bucket.domains[block.domain] = _Domain()
            stats.note_block(block.duration_ns, block.at, block.function)

    def add_state_writes(self, writes: list[StateWrites], now: float) -> None:
        """Add state writes counted before `now`."""
        bucket = self._bucket(now)
        for record in writes:
            if (stats := bucket.domains.get(record.domain)) is None:
                stats = bucket.domains[record.domain] = _Domain()
            stats.state_changes += record.changes
            stats.state_reports += record.reports
            stats.state_attributes_only += record.attributes_only
            stats.state_entities = _capped(stats.state_entities, record.entities)
            if record.attribute_bytes:
                stats.attribute_bytes += record.attribute_bytes
                stats.attribute_entities = _capped(
                    stats.attribute_entities, record.attribute_entities or Counter()
                )

    def add_logs(self, logs: list[LogCounts], now: float) -> None:
        """Add log records counted before `now`."""
        bucket = self._bucket(now)
        for record in logs:
            if (stats := bucket.domains.get(record.domain)) is None:
                stats = bucket.domains[record.domain] = _Domain()
            stats.log_debug += record.debug
            stats.log_info += record.info
            stats.log_warning += record.warning
            stats.log_error += record.error
            stats.log_loggers = _capped(stats.log_loggers, record.loggers)

    def add_store_writes(self, writes: list[StoreWrites], now: float) -> None:
        """Add storage writes counted before `now`."""
        bucket = self._bucket(now)
        for record in writes:
            if (stats := bucket.domains.get(record.domain)) is None:
                stats = bucket.domains[record.domain] = _Domain()
            stats.store_writes += record.writes
            stats.store_bytes += record.size
            stats.store_keys = _capped(stats.store_keys, record.keys)

    def add_loop_busy(self, busy_ns: int, now: float) -> None:
        """Add wall time the loop spent outside select() before `now`."""
        self._bucket(now).loop_busy_ns += busy_ns

    def add_losses(self, samples_lost: int, handler_errors: int, now: float) -> None:
        """Add samples lost and handler errors that happened before `now`."""
        bucket = self._bucket(now)
        bucket.samples_lost += samples_lost
        bucket.handler_errors += handler_errors

    def add(self, records: list[Record], now: float) -> None:
        """Add records that were sampled before `now` (monotonic seconds)."""
        bucket = self._bucket(now)
        for record in records:
            self._add(bucket, record)
        if len(self._locations) > 50_000:
            self._locations.clear()
            self._lines.clear()
        if len(self._stacks) > 20_000:
            self._stacks.clear()

    def _add(self, bucket: _Bucket, record: Record) -> None:
        ns = record.cpu_ns
        domain = record.domain
        via_library = via_context = False
        if domain == "core":
            self.seen_libraries.update(record.libraries)
            for library in record.libraries:
                if (owner := self.library_map.get(library)) is not None:
                    domain = owner
                    via_library = True
                    break
            else:
                # No integration code on the stack and no library of one:
                # the config entry whose context this runs in is the owner.
                if record.entry_domain:
                    domain = record.entry_domain
                    via_context = True
        bucket.cpu_ns += ns
        bucket.samples += 1
        if (stats := bucket.domains.get(domain)) is None:
            stats = bucket.domains[domain] = _Domain()
        stats.cpu_ns += ns
        stats.samples += 1
        if via_library:
            stats.via_library_ns += ns
        if via_context:
            stats.via_context_ns += ns
        if record.trigger is not None and record.trigger != domain:
            stats.triggers[record.trigger] += ns
        # current_entry leaks into tasks that shared helpers start during an
        # entry's setup, so only count it for its own integration.
        if record.entry_id is not None and record.entry_domain == domain:
            stats.entries[record.entry_id] += ns
        if record.entity_id is not None:
            stats.entities[record.entity_id] += ns
        if record.libraries:
            stats.libraries[record.libraries[0]] += ns
        if domain == "idle":
            return
        stack = self._stack(record.frames)
        # One tuple per distinct stack, shared by every minute that saw it.
        stack = self._stacks.setdefault(stack, stack)
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

    def _window(self, minutes: int, now: float) -> _Window:
        """Return the sum of the last `minutes` finished minutes."""
        current = int(now // 60)
        first = current - minutes
        window = self._windows.get(minutes) if minutes in KEPT_WINDOWS else None
        if window is None:
            window = _Window()
            if minutes in KEPT_WINDOWS:
                self._windows[minutes] = window
        finished = {
            bucket.minute: bucket
            for bucket in self._buckets
            if first <= bucket.minute < current
        }
        for minute in [m for m in window.minutes if m not in finished]:
            window.total.merge_totals(window.minutes.pop(minute), -1)
        for minute, bucket in finished.items():
            if minute not in window.minutes:
                window.total.merge_totals(bucket)
                window.minutes[minute] = bucket
        return window

    @staticmethod
    def _first_minute(now: float, minutes: int) -> int:
        return int(now // 60) - min(minutes, WINDOW_MINUTES)

    def _view(
        self, now: float, started: float, minutes: int
    ) -> tuple[float, _Bucket, _Bucket | None]:
        """Return the span in minutes, the finished minutes and the current one."""
        minutes = min(minutes, WINDOW_MINUTES)
        window_start = max(started, self._first_minute(now, minutes) * 60)
        span = max((now - window_start) / 60, 1 / 60)
        current = (
            self._buckets[-1]
            if self._buckets and self._buckets[-1].minute == int(now // 60)
            else None
        )
        return span, self._window(minutes, now).total, current

    def summary(
        self, now: float, started: float, minutes: int = WINDOW_MINUTES
    ) -> dict[str, Any]:
        """Return totals and the load per integration, without details."""
        span, finished, current = self._view(now, started, minutes)
        parts = [finished] if current is None else [finished, current]

        def per_minute(ns: int) -> float:
            return round(ns / 1e6 / span, 1)

        sums: dict[str, list[int]] = {}
        for part in parts:
            for domain, stats in part.domains.items():
                row = sums.setdefault(domain, [0] * 17)
                row[0] += stats.cpu_ns
                row[1] += stats.executor_cpu_ns + stats.thread_cpu_ns
                row[2] += stats.samples
                row[3] += stats.via_library_ns
                row[4] += stats.job_total
                row[5] += stats.loop_wall_ns
                row[6] += stats.via_context_ns
                row[7] += stats.state_changes
                row[8] += stats.state_reports
                row[9] += stats.state_attributes_only
                row[10] += stats.attribute_bytes
                row[11] += stats.log_debug
                row[12] += stats.log_warning
                row[13] += stats.log_error
                row[14] += stats.store_writes
                row[15] += stats.store_bytes
                row[16] += stats.log_info
        idle = sums.pop("idle", [0] * 17)
        longest = self._longest(now, minutes)
        integrations = []
        for domain, (
            loop_ns,
            background_ns,
            samples,
            via_ns,
            jobs,
            wall_ns,
            context_ns,
            changes,
            reports,
            attributes_only,
            attribute_bytes,
            log_debug,
            log_warning,
            log_error,
            store_writes,
            store_bytes,
            log_info,
        ) in sorted(
            sums.items(), key=lambda item: (-(item[1][0] + item[1][1]), item[0])
        ):
            item: dict[str, Any] = {
                "domain": domain,
                "cpu_ms_per_min": per_minute(loop_ns + background_ns),
                "loop_cpu_ms_per_min": per_minute(loop_ns),
                "loop_wall_ms_per_min": per_minute(wall_ns),
                "background_cpu_ms_per_min": per_minute(background_ns),
                "jobs_per_min": round(jobs / span, 1),
                "samples": samples,
                "low_confidence": 0 < samples < MIN_CONFIDENT_SAMPLES,
            }
            if via_ns:
                item["via_library_ms_per_min"] = per_minute(via_ns)
            if context_ns:
                item["via_context_ms_per_min"] = per_minute(context_ns)
            if changes or reports or attributes_only:
                item["state_writes_per_min"] = round(
                    (changes + reports + attributes_only) / span, 1
                )
                item["state_changes_per_min"] = round(changes / span, 1)
                item["state_attributes_only_per_min"] = round(attributes_only / span, 1)
                item["state_repeated_per_min"] = round(reports / span, 1)
            if attribute_bytes:
                item["attribute_kib_per_min"] = round(attribute_bytes / 1024 / span, 1)
            # Two decimals: once an hour is still more than nothing
            if log_debug or log_info or log_warning or log_error:
                item["log_debug_per_min"] = round(log_debug / span, 2)
                item["log_info_per_min"] = round(log_info / span, 2)
                item["log_warnings_per_min"] = round(log_warning / span, 2)
                item["log_errors_per_min"] = round(log_error / span, 2)
            if store_writes:
                item["store_writes_per_min"] = round(store_writes / span, 2)
                item["store_kib_per_min"] = round(store_bytes / 1024 / span, 2)
            if (block := longest.get(domain)) is not None:
                if block[0]:
                    item["longest_ms"] = round(block[0] / 1e6, 1)
                    item["longest_at"] = block[1]
                    item["longest_function"] = block[2]
                if block[3]:
                    item["blocked_ms"] = round(block[3] / 1e6, 1)
                    item["blocked_at"] = block[4]
                    item["blocked_function"] = block[5]
            integrations.append(item)
        return {
            "window_minutes": round(span, 1),
            "loop_cpu_ms_per_min": per_minute(sum(p.cpu_ns for p in parts)),
            "loop_busy_ms_per_min": per_minute(sum(p.loop_busy_ns for p in parts)),
            "loop_wall_ms_per_min": per_minute(sum(p.loop_wall_ns for p in parts)),
            "executor_cpu_ms_per_min": per_minute(
                sum(p.executor_cpu_ns for p in parts)
            ),
            "thread_cpu_ms_per_min": per_minute(sum(p.thread_cpu_ns for p in parts)),
            "idle_ms_per_min": per_minute(idle[0]),
            "samples": sum(p.samples for p in parts),
            "samples_lost": sum(p.samples_lost for p in parts),
            "handler_errors": sum(p.handler_errors for p in parts),
            "stacks_not_kept_ms_per_min": per_minute(
                sum(p.dropped_stack_ns for p in parts)
            ),
            "integrations": integrations,
        }

    def _longest(
        self, now: float, minutes: int
    ) -> dict[str, tuple[int, float, str, int, float, str]]:
        """Return per integration its longest callback and longest block in the window."""
        first_minute = self._first_minute(now, minutes)
        longest: dict[str, list[Any]] = {}
        for bucket in self._buckets:
            if bucket.minute < first_minute:
                continue
            for domain, stats in bucket.domains.items():
                row = longest.setdefault(domain, [0, 0.0, "", 0, 0.0, ""])
                if stats.longest_ns > row[0]:
                    row[0:3] = [stats.longest_ns, stats.longest_at, stats.longest_label]
                if stats.block_ns > row[3]:
                    row[3:6] = [stats.block_ns, stats.block_at, stats.block_function]
        return {domain: tuple(row) for domain, row in longest.items()}

    def detail(
        self, domain: str, now: float, started: float, minutes: int = WINDOW_MINUTES
    ) -> dict[str, Any] | None:
        """Return everything known about one integration, or None."""
        span, _finished, _current = self._view(now, started, minutes)
        first_minute = self._first_minute(now, minutes)
        stats = _Domain()
        found = False
        for bucket in self._buckets:
            if bucket.minute >= first_minute and (
                part_stats := bucket.domains.get(domain)
            ):
                stats.merge(part_stats)
                found = True
        return _item(domain, stats, span) if found and domain != "idle" else None

    def report(
        self, now: float, started: float, minutes: int = WINDOW_MINUTES
    ) -> dict[str, Any]:
        """Build the full report over the last `minutes` (the current one included)."""
        report = self.summary(now, started, minutes)
        report["integrations"] = [
            item
            for brief in report["integrations"]
            if (item := self.detail(brief["domain"], now, started, minutes)) is not None
        ]
        return report


def _hot_spots(
    stats: _Domain, per_minute: Callable[[int], float]
) -> list[dict[str, Any]]:
    """Group hot locations by function, with the hottest lines of each."""
    functions: dict[tuple[str, str], list[Any]] = {}
    for location, ns in _ranked(stats.hot_spots):
        path, qualname, line = location
        entry = functions.setdefault((path, qualname), [0, 0, {}])
        entry[0] += ns
        entry[1] += stats.hot_spot_samples[location]
        if len(entry[2]) < TOP_LINES:
            entry[2][line] = ns
    ranked = sorted(functions.items(), key=lambda item: (-item[1][0], item[0]))
    return [
        {
            "function": f"{path} {qualname}",
            "loop_cpu_ms_per_min": per_minute(ns),
            "samples": samples,
            "lines": {str(line): per_minute(l_ns) for line, l_ns in lines.items()},
        }
        for (path, qualname), (ns, samples, lines) in ranked[:TOP_HOT_SPOTS]
    ]


def _capped(kept: Counter[str], new: Counter[str]) -> Counter[str]:
    """Add new counts; past twice the cap keep the busiest, a long tail only costs memory."""
    kept.update(new)
    if len(kept) > 2 * MAX_STATE_ENTITIES_PER_MINUTE:
        return Counter(dict(kept.most_common(MAX_STATE_ENTITIES_PER_MINUTE)))
    return kept


def _ranked[K](counter: Counter[K], n: int | None = None) -> list[tuple[K, int]]:
    """Return the largest entries, ties ordered by key so every sum ranks alike."""
    if n is None:
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return heapq.nsmallest(n, counter.items(), key=lambda kv: (-kv[1], kv[0]))


def _item(domain: str, stats: _Domain, span: float) -> dict[str, Any]:
    """Return the report entry of one integration."""

    def per_minute(ns: int) -> float:
        return round(ns / 1e6 / span, 1)

    def top(counter: Counter[str], n: int | None = None) -> dict[str, float]:
        return {key: per_minute(ns) for key, ns in _ranked(counter, n)}

    item: dict[str, Any] = {
        "domain": domain,
        "cpu_ms_per_min": per_minute(stats.total_cpu_ns),
        "loop_cpu_ms_per_min": per_minute(stats.cpu_ns),
        "loop_wall_ms_per_min": per_minute(stats.loop_wall_ns),
        "background_cpu_ms_per_min": per_minute(
            stats.executor_cpu_ns + stats.thread_cpu_ns
        ),
        "jobs_per_min": round(stats.job_total / span, 1),
        "samples": stats.samples,
        "low_confidence": 0 < stats.samples < MIN_CONFIDENT_SAMPLES,
    }
    if stats.via_library_ns:
        item["via_library_ms_per_min"] = per_minute(stats.via_library_ns)
    if stats.via_context_ns:
        item["via_context_ms_per_min"] = per_minute(stats.via_context_ns)
    item["triggered_by"] = top(stats.triggers, TOP_TRIGGERS)
    item["config_entries"] = top(stats.entries)
    # All of them: the caller groups them by device before trimming.
    item["entities"] = top(stats.entities)
    item["libraries"] = top(stats.libraries)
    item["hot_spots"] = _hot_spots(stats, per_minute)
    item["stacks"] = [
        {
            "loop_cpu_ms_per_min": per_minute(ns),
            "frames": [_format(location) for location in stack],
        }
        for stack, ns in _ranked(stats.stacks, TOP_STACKS)
    ]
    if stats.job_counts:
        item["executor"] = {
            "cpu_ms_per_min": per_minute(stats.executor_cpu_ns),
            "wall_ms_per_min": per_minute(stats.executor_wall_ns),
            "jobs_per_min": round(stats.job_counts.total() / span, 1),
            "jobs": [
                {
                    "function": label,
                    "cpu_ms_per_min": per_minute(ns),
                    "per_min": round(stats.job_counts[label] / span, 1),
                }
                for label, ns in _ranked(stats.jobs, TOP_JOBS)
            ],
        }
    if stats.threads:
        item["threads"] = {
            "cpu_ms_per_min": per_minute(stats.thread_cpu_ns),
            "by_name": top(stats.threads, TOP_THREADS),
        }
    writes = stats.state_changes + stats.state_reports + stats.state_attributes_only
    # Attributes are measured after the write, so a window can hold their
    # size without the write itself
    if writes or stats.attribute_bytes:
        item["state_writes"] = {
            "per_min": round(writes / span, 1),
            "changes_per_min": round(stats.state_changes / span, 1),
            "attributes_only_per_min": round(stats.state_attributes_only / span, 1),
            "repeated_per_min": round(stats.state_reports / span, 1),
            "entities": {
                entity_id: round(count / span, 1)
                for entity_id, count in _ranked(
                    stats.state_entities, TOP_STATE_ENTITIES
                )
            },
        }
    if stats.attribute_bytes:
        item["state_writes"]["attribute_kib_per_min"] = round(
            stats.attribute_bytes / 1024 / span, 1
        )
        item["state_writes"]["attribute_entities"] = {
            entity_id: round(size / 1024 / span, 1)
            for entity_id, size in _ranked(stats.attribute_entities, TOP_STATE_ENTITIES)
        }
    if stats.log_debug or stats.log_info or stats.log_warning or stats.log_error:
        item["logs"] = {
            "debug_per_min": round(stats.log_debug / span, 2),
            "info_per_min": round(stats.log_info / span, 2),
            "warnings_per_min": round(stats.log_warning / span, 2),
            "errors_per_min": round(stats.log_error / span, 2),
            "loggers": {
                name: round(count / span, 2)
                for name, count in _ranked(stats.log_loggers, TOP_LOGGERS)
            },
        }
    if stats.store_writes:
        item["storage"] = {
            "writes_per_min": round(stats.store_writes / span, 2),
            "kib_per_min": round(stats.store_bytes / 1024 / span, 2),
            "files": {
                key: round(count / span, 2)
                for key, count in _ranked(stats.store_keys, TOP_STORE_KEYS)
            },
        }
    if stats.block_ns:
        item["stall"] = {
            "ms": round(stats.block_ns / 1e6, 1),
            "at": stats.block_at,
            "function": stats.block_function,
        }
    if stats.callback_total:
        item["callbacks"] = {
            "wall_ms_per_min": per_minute(stats.loop_wall_ns),
            "per_min": round(stats.callback_total / span, 1),
            "longest_ms": round(stats.longest_ns / 1e6, 1),
            "longest_at": stats.longest_at,
            "longest_function": stats.longest_label,
            "by_function": [
                {
                    "function": label,
                    "wall_ms_per_min": per_minute(ns),
                    "per_min": round(stats.callback_counts[label] / span, 1),
                }
                for label, ns in _ranked(stats.callbacks, TOP_CALLBACKS)
            ],
        }
    return item


def _format(location: Location) -> str:
    if location is GAP:
        return "..."
    path, qualname, line = location
    return f"{path}:{line} {qualname}"
