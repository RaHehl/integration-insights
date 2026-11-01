"""Run the sampler and feed the collector."""

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from datetime import timedelta
import logging
import math
import threading
import time
from typing import Any

from homeassistant.config_entries import current_entry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    EVENT_STATE_REPORTED,
    MATCH_ALL,
)
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import entity_sources
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.json import json_bytes
from homeassistant.helpers.recorder import DATA_INSTANCE as RECORDER
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
from homeassistant.util import dt as dt_util

from .attribution import (
    Packages,
    distribution_owners,
    library_owners,
    module_domain,
    platform_domains,
    short_path,
    top_package,
)
from .background import Background, LoopWait, ThreadRecord
from .collector import (
    WINDOW_MINUTES,
    BlockRecord,
    CallbackRecord,
    Collector,
    StateWrites,
)
from .const import DOMAIN
from .findings import INFO, MIN_MINUTES, Finding, Shown, find
from .loop_timing import LoopTiming
from .memory import (
    Snapshot,
    account_heap,
    check_headroom,
    count_resources,
    heap_roots,
    imported_packages,
    make_callable_owner,
    make_heap_owner,
    make_module_owner,
    read_process,
    to_report,
)
from .outputs import LogCounter, StoreCounter, logger_domain, store_domain
from .sampler import Sampler, SamplerError

_LOGGER = logging.getLogger(__name__)
DRAIN_INTERVAL = timedelta(seconds=10)
TOP_ENTITIES = 20
# States set by hand under made-up entity ids must not grow the cache forever
MAX_CACHED_ENTITIES = 50_000
# What the recorder keeps of an entity that leaves all its attributes out
RECORDED_WITH_MATCH_ALL = frozenset(
    {"device_class", "friendly_name", "state_class", "unit_of_measurement"}
)
# What the recorder never keeps
NEVER_RECORDED = frozenset({"attribution", "restored", "supported_features"})
KEPT_SNAPSHOTS = 10
# Left out of findings after a start.
STARTUP_MINUTES = 5
# Findings shown this long ago no longer hold, see findings.HOLD.
SHOWN_SECONDS = 120
# Panels stay subscribed across a reload, so the list outlives an instance.
SUBSCRIBERS_KEY = f"{DOMAIN}_subscribers"


class Insights:
    """Sample the event loop and keep the results."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self.sampler = Sampler(entry_var=current_entry)
        self.collector = Collector()
        self.background = Background(
            self.sampler.walk, lambda: self.collector.library_map
        )
        self.loop_wait = LoopWait()
        self.loop_timing = LoopTiming(hass.loop, self._blocker)
        self._snapshot_running = False
        self._entry_domains: dict[str, str] = {}
        self._platforms = platform_domains()
        self.started = 0.0
        self._position = 0
        self._mapped_components: frozenset[str] | None = None
        self._seen_components: frozenset[str] | None = None
        self._mapping_lock = asyncio.Lock()
        self._packages = Packages()
        self._owners: dict[str, str] = {}
        self._checked_libraries: set[str] = set()
        # Findings of the last call and when, see findings.HOLD
        self._shown: Shown = {}
        self._shown_at = 0.0
        # The window the last findings were judged on, in minutes
        self.findings_minutes = 0
        self._lost_seen = 0
        self._errors_seen = 0
        self._rearm_failed = False
        self._wakeup_hook: Callable[[bytes], None] | None = None
        self._wakeup_active = [False]
        self._unsub_drain: CALLBACK_TYPE | None = None
        self._unsub_stop: CALLBACK_TYPE | None = None
        self._unsub_started: CALLBACK_TYPE | None = None
        self._unsub_states: list[CALLBACK_TYPE] = []
        # States whose attributes to measure: integration, entity_id, state
        self._pending_attributes: list[tuple[str, str, Any]] = []
        # domain -> [changes, reports, entities], filled by the event filters
        self._state_writes: dict[str, list[Any]] = {}
        self.logs = LogCounter()
        self.stores = StoreCounter()
        self._logger_domains: dict[str, str] = {}
        self._logger_map: Any = None
        self._polling = False
        self._subscribers: list[tuple[int, Callable[[dict[str, Any]], None]]] = (
            hass.data.setdefault(SUBSCRIBERS_KEY, [])
        )
        self.snapshots: deque[Snapshot] = deque(maxlen=KEPT_SNAPSHOTS)
        self._snapshot_lock = asyncio.Lock()
        self._wall_ns = 0
        self._waiting_ns = 0

    def _executors(self) -> list[Any]:
        executors = [getattr(self.hass.loop, "_default_executor", None)]
        executors.append(getattr(self.hass, "import_executor", None))
        return [executor for executor in executors if executor is not None]

    @callback
    def start(self) -> None:
        """Measure from the moment Home Assistant has started; raises SamplerError.

        Integrations setting up and the imports behind them are not the load
        of a running system, and every new one would have the libraries
        mapped again. Before the start nothing is measured.
        """
        # is_running is also true while starting, so look at the state.
        if self.hass.state is CoreState.running:
            self._start()
            return
        self._unsub_started = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, self._async_on_started
        )

    @callback
    def _async_on_started(self, _event: Event) -> None:
        self._unsub_started = None
        try:
            self._start()
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Could not start measuring: %s", err)

    def _start(self) -> None:
        try:
            self._install()
        except BaseException:
            # Whatever was installed before the failure must not stay behind
            self.stop()
            raise

    def _install(self) -> None:
        self.sampler.install()
        self._hook_wakeup_reads()
        for executor in self._executors():
            self.background.hook_executor(executor)
        if self.loop_wait.install(self.hass.loop):
            self.sampler.select_wrapper = self.loop_wait.wrapper_code
        self.loop_timing.start()
        self.logs.install()
        if not self.stores.install():
            _LOGGER.debug("This Home Assistant saves files in a way not counted here")
        self.sampler.skip_codes = frozenset(
            {
                self.loop_timing.wrapper_code,
                *self.background.wrapper_codes,
                *([self.stores.wrapper_code] if self.stores.wrapper_code else []),
            }
        )
        self._listen_state_writes()
        self._wall_ns = time.perf_counter_ns()
        self._waiting_ns = self.loop_wait.waiting_ns
        self.started = time.monotonic()
        self._position = self.sampler.count
        self._unsub_drain = async_track_time_interval(
            self.hass, self._async_drain, DRAIN_INTERVAL, cancel_on_shutdown=True
        )
        self._unsub_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_on_stop
        )

    @callback
    def _async_on_stop(self, _event: Event) -> None:
        self._unsub_stop = None
        self.stop()

    @callback
    def stop(self) -> None:
        """Remove the sampler."""
        if self._unsub_started is not None:
            self._unsub_started()
            self._unsub_started = None
        if self._unsub_stop is not None:
            self._unsub_stop()
            self._unsub_stop = None
        if self._unsub_drain is not None:
            self._unsub_drain()
            self._unsub_drain = None
        self.sampler.uninstall()
        self._unhook_wakeup_reads()
        self.background.unhook_executors()
        self.loop_wait.uninstall()
        self.loop_timing.stop()
        self.logs.uninstall()
        self.stores.uninstall()
        self.sampler.select_wrapper = None
        self.sampler.skip_codes = frozenset()
        for unsub in self._unsub_states:
            unsub()
        self._unsub_states.clear()

    def _listen_state_writes(self) -> None:
        """Count state writes per integration in the bus event filters.

        The filters run for every event, counting there and returning False
        means no listener job is ever scheduled, so the cost stays at a dict
        update per write.
        """
        loop = self.hass.loop
        loop_thread = threading.get_ident()
        owner = self._entity_owner
        sources = entity_sources(self.hass)
        # entity_id -> integration or None, cached for the hot path. Entities
        # without a registry entry come and go without an event, so the cache
        # starts over whenever the number of entity sources changes.
        platforms: dict[str, str | None] = {}
        known = [len(sources)]
        pending = self._pending_attributes
        row_of = self._state_row

        def count(entity_id: str, slot: int) -> str | None:
            if len(sources) != known[0]:
                platforms.clear()
                known[0] = len(sources)
            if entity_id in platforms:
                platform = platforms[entity_id]
            else:
                if len(platforms) >= MAX_CACHED_ENTITIES:
                    platforms.clear()
                platform = platforms[entity_id] = owner(entity_id)
            if platform is None:
                return None
            row = row_of(platform)
            row[slot] += 1
            if slot != 1:
                # Busiest entities by what reaches listeners and the recorder
                row[3][entity_id] += 1
            return platform

        measure = self._measure_attributes

        @callback
        def changed(data: Any) -> bool:
            # Events fired by hand can hold anything; a filter must not raise
            try:
                # The same state with other attributes still reaches every
                # listener and the recorder like a change
                old, new = data["old_state"], data["new_state"]
                same = old is not None and new is not None and old.state == new.state
                entity_id = data["entity_id"]
                platform = count(entity_id, 2 if same else 0)
                # Core keeps the old attributes object when nothing in it
                # changed, so only new attributes are measured. A state set
                # from another thread is counted but not measured.
                if (
                    platform is not None
                    and new is not None
                    and (old is None or new.attributes is not old.attributes)
                    and threading.get_ident() == loop_thread
                ):
                    if not pending:
                        loop.call_soon(measure)
                    pending.append((platform, entity_id, new))
            except Exception:  # noqa: BLE001
                pass
            return False

        @callback
        def reported(data: Any) -> bool:
            # A plain try: suppress() costs a context manager on every write
            try:  # noqa: SIM105
                count(data["entity_id"], 1)
            except Exception:  # noqa: BLE001
                pass
            return False

        @callback
        def never(_event: Any) -> None:
            """Only the filters do the work."""

        @callback
        def registry_updated(_event: Any) -> None:
            platforms.clear()

        self._unsub_states = [
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, registry_updated
            ),
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, never, event_filter=changed
            ),
            self.hass.bus.async_listen(
                EVENT_STATE_REPORTED, never, event_filter=reported
            ),
        ]

    def _state_row(self, platform: str) -> list[Any]:
        if (row := self._state_writes.get(platform)) is None:
            row = self._state_writes[platform] = [0, 0, 0, Counter(), 0, Counter()]
        return row

    def _entity_owner(self, entity_id: str) -> str | None:
        """Return the integration of an entity, with or without a registry entry."""
        if (source := entity_sources(self.hass).get(entity_id)) is not None:
            return source["domain"]
        entry = er.async_get(self.hass).entities.get(entity_id)
        return entry.platform if entry is not None else None

    def _measure_attributes(self) -> None:
        """Measure the attributes queued by the state filter.

        A callback of its own, so serializing is the measuring cost and not
        part of the callback that wrote the state. A drain runs it first, so
        sizes land in the same minute as their writes.
        """
        pending = self._pending_attributes
        if not pending:
            return
        batch = pending[:]
        pending.clear()
        recorder = self.hass.data.get(RECORDER)
        included = getattr(recorder, "entity_filter", None)
        if EVENT_STATE_CHANGED in (getattr(recorder, "exclude_event_types", ()) or ()):
            return
        for platform, entity_id, state in batch:
            try:
                # What the recorder serializes: nothing of an entity it
                # excludes, and no attribute the integration leaves out.
                # Without a recorder everything counts.
                if included is not None and not included(entity_id):
                    continue
                attributes = state.attributes
                unrecorded = NEVER_RECORDED
                if (info := state.state_info) and info["unrecorded_attributes"]:
                    unrecorded = unrecorded | info["unrecorded_attributes"]
                if MATCH_ALL in unrecorded:
                    attributes = {
                        key: value
                        for key, value in attributes.items()
                        if key in RECORDED_WITH_MATCH_ALL
                    }
                elif not unrecorded.isdisjoint(attributes):
                    attributes = {
                        key: value
                        for key, value in attributes.items()
                        if key not in unrecorded
                    }
                size = len(json_bytes(attributes))
            except Exception:  # noqa: BLE001
                continue
            row = self._state_row(platform)
            row[4] += size
            row[5][entity_id] += size

    def _hook_wakeup_reads(self) -> None:
        # asyncio's selector loop hands the bytes it read from the wakeup
        # socket to _process_self_data, once per iteration after a signal.
        # That tells the sampler the loop is not blocked.
        loop = self.hass.loop
        original = getattr(loop, "_process_self_data", None)
        if original is None:
            return
        sampler = self.sampler
        active = self._wakeup_active = [True]

        def process_self_data(data: bytes) -> None:
            if active[0]:
                sampler.loop_drained()
            original(data)

        self._wakeup_hook = process_self_data
        loop._process_self_data = process_self_data  # type: ignore[attr-defined]  # noqa: SLF001

    def _unhook_wakeup_reads(self) -> None:
        self._wakeup_active[0] = False
        loop = self.hass.loop
        if (hook := self._wakeup_hook) is not None:
            if vars(loop).get("_process_self_data") is hook:
                del loop._process_self_data  # type: ignore[attr-defined]  # noqa: SLF001
            self._wakeup_hook = None

    @callback
    def _async_drain(self, _now: Any = None) -> None:
        self.drain()
        if self._subscribers:
            self._async_publish()
        if not self._polling:
            self._polling = True
            skip = self.background.executor_threads(self._executors())
            skip.add(threading.main_thread().native_id or 0)
            self.hass.async_create_background_task(
                self._async_poll_threads(skip), "integration_insights threads"
            )
        try:
            self.sampler.rearm()
        except SamplerError as err:
            if not self._rearm_failed:
                _LOGGER.warning("Could not re-arm the sampler: %s", err)
                self._rearm_failed = True
        components = frozenset(self.hass.config.top_level_components)
        # Integrations keep arriving for minutes after a start; mapping them
        # costs seconds on small hardware, so wait until the set holds still
        settled = components == self._seen_components
        self._seen_components = components
        if not self._mapping_lock.locked() and (
            (settled and components != self._mapped_components)
            or not self.collector.seen_libraries <= self._checked_libraries
        ):
            self.hass.async_create_background_task(
                self._async_update_library_map(components),
                "integration_insights library map",
            )

    def drain(self) -> None:
        """Move new samples, jobs and loop wall time into the collector."""
        records, self._position = self.sampler.drain(self._position)
        now = time.monotonic()
        self._measure_attributes()
        self.collector.add(records, now)
        self.collector.add_jobs(self.background.drain_jobs(), now)
        if writes := self._state_writes:
            # Cleared in place: the event filters hold this dict
            self.collector.add_state_writes(
                [
                    StateWrites(domain, row[0], row[1], row[2], row[3], row[4], row[5])
                    for domain, row in writes.items()
                ],
                now,
            )
            writes.clear()
        library_map = self.collector.library_map
        logger_domains = self._logger_domains
        # A new library map can own loggers that counted for core before
        if library_map is not self._logger_map or len(logger_domains) > 5_000:
            logger_domains.clear()
            self._logger_map = library_map
        self.collector.add_logs(
            self.logs.drain(
                lambda name: logger_domain(name, library_map, logger_domains),
                self._entity_owner,
            ),
            now,
        )
        components = self.hass.config.top_level_components
        self.collector.add_store_writes(
            self.stores.drain(lambda key: store_domain(key, components)), now
        )
        self.collector.add_blocks(
            [
                BlockRecord(
                    block.blocker[0], block.blocker[1], block.duration_ns, block.at
                )
                for block in self.loop_timing.drain_blocks()
                if block.blocker is not None
            ],
            now,
        )
        self.collector.add_callbacks(
            [
                CallbackRecord(
                    self.callback_domain(timed.label, timed.entry_id),
                    timed.label,
                    timed.calls,
                    timed.total_ns,
                    timed.max_ns,
                    timed.max_at,
                )
                for timed in self.loop_timing.drain()
            ],
            now,
        )
        lost, errors = self.sampler.lost, self.sampler.errors
        self.collector.add_losses(
            lost - self._lost_seen, errors - self._errors_seen, now
        )
        self._lost_seen, self._errors_seen = lost, errors
        wall = time.perf_counter_ns()
        waiting = self.loop_wait.waiting_ns
        busy = (wall - self._wall_ns) - (waiting - self._waiting_ns)
        self._wall_ns, self._waiting_ns = wall, waiting
        if self.loop_wait.wrapper_code is not None and busy > 0:
            self.collector.add_loop_busy(busy, now)

    def callback_domain(self, label: str, entry_id: str | None) -> str:
        """Return the integration a timed callback ran for.

        The module of the function or class decides, as for samples. Core,
        platform base classes and shared libraries go to the config entry in
        the callback's context, which Home Assistant sets for an entry's
        setup and everything started from it.
        """
        # The integration whose code it is decides, as for samples
        domain = module_domain(label)
        if domain is not None and domain not in self._platforms:
            return domain
        if domain is None and (
            mapped := self.collector.library_map.get(top_package(label))
        ):
            return mapped
        # Core, a platform base class or a shared library: the config entry
        # it runs for, if there is one
        return self._entry_domain(entry_id) or "core"

    def _entry_domain(self, entry_id: str | None) -> str | None:
        if entry_id is None:
            return None
        if (domain := self._entry_domains.get(entry_id)) is None:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            domain = self._entry_domains[entry_id] = (
                entry.domain if entry is not None else ""
            )
        return domain or None

    def _blocker(self, frame: Any) -> tuple[str, str] | None:
        """Name the integration whose code a frame of the loop thread runs.

        Runs in the watchdog thread while a callback blocks the loop. During
        a memory snapshot the loop waits for the walk's executor thread, so
        whatever runs then is not to blame.
        """
        if self._snapshot_running or frame is None:
            return None
        record = self.sampler.walk(frame, 0)
        domain: str | None = record.domain
        if domain in ("core", "idle"):
            domain = next(
                (
                    self.collector.library_map[library]
                    for library in record.libraries
                    if library in self.collector.library_map
                ),
                None,
            )
        if domain is None or domain == DOMAIN:
            return None
        index = max(record.self_index, 0)
        code = record.frames[2 * index] if record.frames else None
        if code is None:
            return domain, ""
        return domain, f"{short_path(code.co_filename)} {code.co_qualname}"

    async def _async_poll_threads(self, skip: set[int]) -> None:
        try:
            threads: list[ThreadRecord] = await self.hass.async_add_executor_job(
                self.background.poll_threads, skip
            )
        except Exception:
            _LOGGER.debug("Could not read thread CPU", exc_info=True)
            return
        finally:
            self._polling = False
        self.collector.add_threads(threads, time.monotonic())

    async def _async_update_library_map(self, components: frozenset[str]) -> None:
        async with self._mapping_lock:
            await self._async_update_library_map_locked(components)

    async def _async_update_library_map_locked(
        self, components: frozenset[str]
    ) -> None:
        # Only libraries that showed up are looked up. Integrations set up at
        # runtime may have installed packages, so a new set of integrations
        # maps everything again (the package cache stays).
        changed = components != self._mapped_components
        requirements: dict[str, list[str]] | None = None
        if changed:
            requirements = {}
            for domain in components:
                try:
                    integration = async_get_loaded_integration(self.hass, domain)
                except IntegrationNotLoaded:
                    continue
                requirements[domain] = integration.requirements
            libraries = set(self.collector.seen_libraries)
        else:
            libraries = self.collector.seen_libraries - self._checked_libraries
        if not changed and not libraries:
            return
        try:
            owners, mapped = await self.hass.async_add_executor_job(
                self._map_libraries, requirements, libraries
            )
        except Exception:
            _LOGGER.debug("Could not read the installed packages", exc_info=True)
            return
        if changed:
            self._mapped_components = components
            self._owners = owners
            self._checked_libraries = libraries
            self.collector.library_map = mapped
        else:
            self._checked_libraries |= libraries
            self.collector.library_map = {**self.collector.library_map, **mapped}

    def _map_libraries(
        self, requirements: dict[str, list[str]] | None, libraries: set[str]
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Return distribution owners and the owners of libraries (does I/O)."""
        owners = (
            distribution_owners(requirements, self._packages)
            if requirements is not None
            else self._owners
        )
        return owners, library_owners(libraries, owners, self._packages)

    def _sampler_stats(self) -> dict[str, Any]:
        sampler = self.sampler
        samples = max(sampler.count, 1)
        return {
            "interval_ms": sampler.interval_ns / 1e6,
            "samples_total": sampler.count,
            "handler_cpu_us_avg": round(sampler.cost_ns / samples / 1e3, 1),
            "handler_errors": sampler.errors,
            "samples_lost": sampler.lost,
            "backoffs": sampler.backoffs,
            "executor_jobs_total": self.background.jobs_total,
            "timing_cpu_ms_per_min": round(
                self.loop_timing.overhead_ns
                / 1e6
                / max((time.monotonic() - self.started) / 60, 1 / 60),
                1,
            )
            if self.sampler.installed
            else 0,
            "stores_counted": self.stores.installed,
            "timing_errors": self.loop_timing.errors,
            "timing_overflowed": self.loop_timing.overflowed,
            "timing_resolves_per_min": round(
                self.loop_timing.resolves
                / max((time.monotonic() - self.started) / 60, 1 / 60),
                1,
            )
            if self.sampler.installed
            else 0,
            "running_minutes": round((time.monotonic() - self.started) / 60, 1)
            if self.sampler.installed
            else 0,
        }

    def _with_devices(self, item: dict[str, Any]) -> dict[str, Any]:
        """Group an item's entities by device and keep the top entities."""
        registry = er.async_get(self.hass)
        entities: dict[str, float] = item["entities"]
        devices: dict[str, float] = {}
        for entity_id, ms in entities.items():
            if (
                entry := registry.async_get(entity_id)
            ) is not None and entry.device_id is not None:
                devices[entry.device_id] = round(
                    devices.get(entry.device_id, 0) + ms, 1
                )
        item["devices"] = dict(
            sorted(devices.items(), key=lambda kv: kv[1], reverse=True)
        )
        item["entities"] = dict(list(entities.items())[:TOP_ENTITIES])
        return item

    def summary(self, minutes: int = WINDOW_MINUTES) -> dict[str, Any]:
        """Return totals and the load per integration over the last `minutes`."""
        self.drain()
        return self._summary_at(time.monotonic(), minutes)

    def _summary_at(self, now: float, minutes: int) -> dict[str, Any]:
        return {
            "sampler": self._sampler_stats(),
            **self.collector.summary(now, self.started, minutes),
        }

    def detail(
        self, domain: str, minutes: int = WINDOW_MINUTES
    ) -> dict[str, Any] | None:
        """Return everything known about one integration, or None."""
        self.drain()
        return self._detail_at(domain, time.monotonic(), minutes)

    def _detail_at(
        self, domain: str, now: float, minutes: int
    ) -> dict[str, Any] | None:
        item = self.collector.detail(domain, now, self.started, minutes)
        return None if item is None else self._with_devices(item)

    def findings(self, minutes: int = WINDOW_MINUTES) -> list[Finding]:
        """Return what the measurements of the last `minutes` point to.

        The minutes right after a start are left out: imports, the library map
        and integrations setting up would look like a load that is not there.
        """
        self.drain()
        now = time.monotonic()
        # Before the start nothing is measured; count as if it were now.
        started = self.started if self.sampler.installed else now
        settled = (now - started) / 60 - STARTUP_MINUTES
        # A window of n minutes starts on a minute boundary up to n + 1
        # minutes ago, so it must be a minute shorter than the settled time
        # to leave the start out.
        if settled < MIN_MINUTES + 1:
            self.findings_minutes = 0
            return [
                Finding(
                    "measuring",
                    INFO,
                    values={"minutes": f"{math.ceil(MIN_MINUTES + 1 - settled)}"},
                )
            ]
        window = self.findings_minutes = min(minutes, math.floor(settled) - 1)
        if now - self._shown_at > SHOWN_SECONDS:
            self._shown = {}
        findings = find(
            self._summary_at(now, window),
            lambda domain: self._detail_at(domain, now, window),
            self.memory_report(),
            self._built_in,
            self._shown,
        )
        self._shown = {(f.key, f.domain): f.severity for f in findings}
        self._shown_at = now
        return findings

    def _built_in(self, domain: str) -> bool:
        try:
            return async_get_loaded_integration(self.hass, domain).is_built_in
        except IntegrationNotLoaded:
            return False

    def report(self, minutes: int = WINDOW_MINUTES) -> dict[str, Any]:
        """Return the full report over the last `minutes`."""
        self.drain()
        report = self.collector.report(time.monotonic(), self.started, minutes)
        for item in report["integrations"]:
            self._with_devices(item)
        return {"sampler": self._sampler_stats(), **report}

    async def async_take_snapshot(self) -> Snapshot:
        """Take a memory snapshot; the object walk runs in the executor.

        Raises SnapshotRefused when memory is too short for the walk.
        """
        async with self._snapshot_lock:
            await self.hass.async_add_executor_job(check_headroom)
            # The heap holds every imported library, not only those seen in
            # samples; map them all so code lands with its integration.
            self.collector.seen_libraries.update(imported_packages())
            await self._async_update_library_map(
                frozenset(self.hass.config.top_level_components)
            )
            library_map = self.collector.library_map
            module_owner = make_module_owner(library_map)
            heap_owner = make_heap_owner(library_map)
            resources = count_resources(
                self.hass, make_callable_owner(library_map), module_owner
            )
            roots, stoppers = heap_roots(self.hass, heap_owner, module_owner)
            self._snapshot_running = True
            try:
                heap, walk_ms = await self.hass.async_add_executor_job(
                    account_heap, roots, stoppers, heap_owner
                )
            finally:
                self._snapshot_running = False
            del roots, stoppers
            process = await self.hass.async_add_executor_job(read_process)
            snapshot = Snapshot(dt_util.utcnow(), walk_ms, process, heap, resources)
            self.snapshots.append(snapshot)
            return snapshot

    def memory_report(self) -> dict[str, Any]:
        """Return the latest snapshot compared with the one before, and all times."""
        if not self.snapshots:
            return {"snapshots": []}
        previous = self.snapshots[-2] if len(self.snapshots) > 1 else None
        return {
            "snapshots": [s.taken.isoformat() for s in self.snapshots],
            "latest": to_report(self.snapshots[-1], previous),
        }

    @callback
    def async_subscribe(
        self, minutes: int, send: Callable[[dict[str, Any]], None]
    ) -> CALLBACK_TYPE:
        """Send the summary now and after every drain until unsubscribed."""
        subscriber = (minutes, send)
        self._subscribers.append(subscriber)
        send(self.summary(minutes))

        @callback
        def unsubscribe() -> None:
            self._subscribers.remove(subscriber)

        return unsubscribe

    @callback
    def _async_publish(self) -> None:
        # One summary per window, however many panels show it.
        summaries: dict[int, dict[str, Any]] = {}
        for minutes, send in list(self._subscribers):
            if (summary := summaries.get(minutes)) is None:
                summary = summaries[minutes] = self.summary(minutes)
            send(summary)
