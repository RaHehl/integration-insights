"""Run the sampler and feed the collector."""

from collections.abc import Callable
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import current_entry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.loader import IntegrationNotLoaded, async_get_loaded_integration

from .attribution import PackageIndex, build_library_map, load_package_index
from .collector import WINDOW_MINUTES, Collector
from .sampler import Sampler, SamplerError

_LOGGER = logging.getLogger(__name__)
DRAIN_INTERVAL = timedelta(seconds=10)


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
        self.started = 0.0
        self._position = 0
        self._mapped_components: frozenset[str] | None = None
        self._mapping = False
        self._rearm_failed = False
        self._wakeup_hook: Callable[[bytes], None] | None = None
        self._unsub_drain: CALLBACK_TYPE | None = None
        self._unsub_stop: CALLBACK_TYPE | None = None

    def start(self) -> None:
        """Install the sampler; raises SamplerError."""
        self.sampler.install()
        self._hook_wakeup_reads()
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
        try:
            self.sampler.rearm()
        except SamplerError as err:
            if not self._rearm_failed:
                _LOGGER.warning("Could not re-arm the sampler: %s", err)
                self._rearm_failed = True
        components = frozenset(self.hass.config.top_level_components)
        if components != self._mapped_components and not self._mapping:
            self._mapping = True
            self.hass.async_create_background_task(
                self._async_update_library_map(components),
                "integration_insights library map",
            )

    def drain(self) -> None:
        """Move new samples into the collector."""
        records, self._position = self.sampler.drain(self._position)
        self.collector.add(records, time.monotonic())

    async def _async_update_library_map(self, components: frozenset[str]) -> None:
        # Integrations set up at runtime may have installed packages, so the
        # index is read again whenever the set of integrations changes.
        try:
            index: PackageIndex = await self.hass.async_add_executor_job(
                load_package_index
            )
        except Exception:
            _LOGGER.debug("Could not read the installed packages", exc_info=True)
            return
        finally:
            self._mapping = False
        self._mapped_components = components
        requirements: dict[str, list[str]] = {}
        for domain in components:
            try:
                integration = async_get_loaded_integration(self.hass, domain)
            except IntegrationNotLoaded:
                continue
            requirements[domain] = integration.requirements
        self.collector.library_map = build_library_map(requirements, index)

    def report(self, minutes: int = WINDOW_MINUTES) -> dict[str, Any]:
        """Return the report over the last `minutes`."""
        self.drain()
        sampler = self.sampler
        samples = max(sampler.count, 1)
        return {
            "sampler": {
                "interval_ms": sampler.interval_ns / 1e6,
                "samples_total": sampler.count,
                "handler_cpu_us_avg": round(sampler.cost_ns / samples / 1e3, 1),
                "handler_errors": sampler.errors,
                "samples_lost": sampler.lost,
                "backoffs": sampler.backoffs,
                "running_minutes": round((time.monotonic() - self.started) / 60, 1),
            },
            **self.collector.report(time.monotonic(), self.started, minutes),
        }
