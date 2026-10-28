"""Run the sampler and feed the collector."""

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import timedelta
import logging
import threading
import time
from typing import Any

from homeassistant.config_entries import current_entry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration
from homeassistant.util import dt as dt_util

from .attribution import Packages, distribution_owners, library_owners
from .background import Background, LoopWait, ThreadRecord
from .collector import WINDOW_MINUTES, Collector
from .memory import (
    Snapshot,
    account_heap,
    count_resources,
    heap_roots,
    imported_packages,
    make_callable_owner,
    make_heap_owner,
    make_module_owner,
    read_process,
    to_report,
)
from .sampler import Sampler, SamplerError

_LOGGER = logging.getLogger(__name__)
DRAIN_INTERVAL = timedelta(seconds=10)
TOP_ENTITIES = 20
KEPT_SNAPSHOTS = 10


def _wakeup_fd(hass: HomeAssistant) -> int | None:
    """Return the read end of the loop's wakeup socket, if it has one."""
    sock = getattr(hass.loop, "_ssock", None)
    try:
        return sock.fileno() if sock is not None else None
    except OSError:
        return None


class Insights:
    """Sample the event loop and keep the results."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self.hass = hass
        self.sampler = Sampler(entry_var=current_entry, wakeup_fd=_wakeup_fd(hass))
        self.collector = Collector()
        self.background = Background(
            self.sampler.walk, lambda: self.collector.library_map
        )
        self.loop_wait = LoopWait()
        self.started = 0.0
        self._position = 0
        self._mapped_components: frozenset[str] | None = None
        self._mapping_lock = asyncio.Lock()
        self._packages = Packages()
        self._owners: dict[str, str] = {}
        self._checked_libraries: set[str] = set()
        self._rearm_failed = False
        self._wakeup_hook: Callable[[bytes], None] | None = None
        self._unsub_drain: CALLBACK_TYPE | None = None
        self._unsub_stop: CALLBACK_TYPE | None = None
        self._polling = False
        self._subscribers: list[tuple[int, Callable[[dict[str, Any]], None]]] = []
        self.snapshots: deque[Snapshot] = deque(maxlen=KEPT_SNAPSHOTS)
        self._snapshot_lock = asyncio.Lock()
        self._wall_ns = 0
        self._waiting_ns = 0

    def _executors(self) -> list[Any]:
        executors = [getattr(self.hass.loop, "_default_executor", None)]
        executors.append(getattr(self.hass, "import_executor", None))
        return [executor for executor in executors if executor is not None]

    def start(self) -> None:
        """Install the sampler; raises SamplerError."""
        self.sampler.install()
        self._hook_wakeup_reads()
        for executor in self._executors():
            self.background.hook_executor(executor)
        if self.loop_wait.install(self.hass.loop):
            self.sampler.select_wrapper = self.loop_wait.wrapper_code
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
        self.sampler.select_wrapper = None

    def _hook_wakeup_reads(self) -> None:
        # asyncio's selector loop hands the bytes it read from the wakeup
        # socket to _process_self_data. That is the moment a long block is
        # over, so the sampler can go back to its normal rate.
        loop = self.hass.loop
        original = getattr(loop, "_process_self_data", None)
        if original is None:
            return
        sampler = self.sampler

        def process_self_data(data: bytes) -> None:
            sampler.loop_drained()
            original(data)

        self._wakeup_hook = process_self_data
        loop._process_self_data = process_self_data  # type: ignore[attr-defined]  # noqa: SLF001

    def _unhook_wakeup_reads(self) -> None:
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
        if not self._mapping_lock.locked() and (
            components != self._mapped_components
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
        self.collector.add(records, now)
        self.collector.add_jobs(self.background.drain_jobs(), now)
        wall = time.perf_counter_ns()
        waiting = self.loop_wait.waiting_ns
        busy = (wall - self._wall_ns) - (waiting - self._waiting_ns)
        self._wall_ns, self._waiting_ns = wall, waiting
        if self.loop_wait.wrapper_code is not None and busy > 0:
            self.collector.add_loop_busy(busy, now)

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
            "running_minutes": round((time.monotonic() - self.started) / 60, 1),
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
        return {
            "sampler": self._sampler_stats(),
            **self.collector.summary(time.monotonic(), self.started, minutes),
        }

    def detail(
        self, domain: str, minutes: int = WINDOW_MINUTES
    ) -> dict[str, Any] | None:
        """Return everything known about one integration, or None."""
        self.drain()
        item = self.collector.detail(domain, time.monotonic(), self.started, minutes)
        return None if item is None else self._with_devices(item)

    def report(self, minutes: int = WINDOW_MINUTES) -> dict[str, Any]:
        """Return the full report over the last `minutes`."""
        self.drain()
        report = self.collector.report(time.monotonic(), self.started, minutes)
        for item in report["integrations"]:
            self._with_devices(item)
        return {"sampler": self._sampler_stats(), **report}

    async def async_take_snapshot(self) -> Snapshot:
        """Take a memory snapshot; the object walk runs in the executor."""
        async with self._snapshot_lock:
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
            heap, walk_ms = await self.hass.async_add_executor_job(
                account_heap, roots, stoppers, heap_owner
            )
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
