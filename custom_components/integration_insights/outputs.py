"""What integrations write out: log records and files through the storage helper.

Both are counted where they happen, in any thread, and handed to the loop on
drain. The counting is a dictionary update under a lock; the names are
mapped to integrations on the loop.
"""

from collections import Counter
from collections.abc import Callable, Mapping
import logging
import threading
from typing import Any

from homeassistant.helpers.storage import Store

from .collector import LogCounts, StoreWrites

DEBUG, WARNING, ERROR = 0, 1, 2


class LogCounter(logging.Handler):
    """Count the log records that reach the root logger, by logger and level."""

    def __init__(self) -> None:
        """Initialize; install() attaches the handler."""
        super().__init__(logging.NOTSET)
        self._lock = threading.Lock()
        # logger name -> [debug and info, warning, error and up]
        self._counts: dict[str, list[int]] = {}
        self._installed = False

    def emit(self, record: logging.LogRecord) -> None:
        """Count the record; it is never formatted or written here."""
        level = record.levelno
        slot = (
            ERROR
            if level >= logging.ERROR
            else WARNING
            if level >= logging.WARNING
            else DEBUG
        )
        with self._lock:
            if (row := self._counts.get(record.name)) is None:
                row = self._counts[record.name] = [0, 0, 0]
            row[slot] += 1

    def handleError(self, record: logging.LogRecord) -> None:
        """Never report on ourselves: that would log again."""

    def install(self) -> None:
        """Attach to the root logger."""
        if not self._installed:
            logging.getLogger().addHandler(self)
            self._installed = True

    def uninstall(self) -> None:
        """Detach from the root logger."""
        if self._installed:
            logging.getLogger().removeHandler(self)
            self._installed = False

    def drain(self, owner_of: Callable[[str], str]) -> list[LogCounts]:
        """Return the counts since the last drain, by integration."""
        with self._lock:
            counts, self._counts = self._counts, {}
        rows: dict[str, list[Any]] = {}
        for name, (debug, warning, error) in counts.items():
            domain = owner_of(name)
            if (row := rows.get(domain)) is None:
                row = rows[domain] = [0, 0, 0, Counter()]
            row[0] += debug
            row[1] += warning
            row[2] += error
            if warning or error:
                row[3][name] += warning + error
        return [LogCounts(domain, *row) for domain, row in rows.items()]


def logger_domain(
    name: str, library_map: Mapping[str, str], cache: dict[str, str]
) -> str:
    """Return the integration a logger belongs to, else core."""
    if (domain := cache.get(name)) is not None:
        return domain
    parts = name.split(".")
    if parts[0] == "homeassistant":
        domain = parts[2] if len(parts) > 2 and parts[1] == "components" else "core"
    elif parts[0] == "custom_components" and len(parts) > 1:
        domain = parts[1]
    else:
        domain = library_map.get(parts[0], "core")
    cache[name] = domain
    return domain


class StoreCounter:
    """Count the files written through Home Assistant's storage helper, by key."""

    def __init__(self) -> None:
        """Initialize; install() wraps the write."""
        self._lock = threading.Lock()
        # storage key -> [writes, bytes]; swapped as a whole on drain
        self._counts_ref: list[dict[str, list[int]]] = [{}]
        self._original: Callable[..., None] | None = None
        self._wrapper: Callable[..., None] | None = None
        self._active = [False]

    def install(self) -> None:
        """Wrap Store._write_prepared_data, which every write goes through."""
        if self._wrapper is not None:
            return
        original = Store._write_prepared_data  # noqa: SLF001
        counts = self._counts_ref
        lock = self._lock
        active = self._active = [True]

        def _write_prepared_data(store: Any, mode: str, json_data: Any) -> None:
            # Once removed, a wrapper that something else wrapped in turn
            # stays in the chain; it only passes calls on
            if active[0]:
                try:
                    size = len(json_data)
                    with lock:
                        table = counts[0]
                        if (row := table.get(store.key)) is None:
                            row = table[store.key] = [0, 0]
                        row[0] += 1
                        row[1] += size
                except Exception:  # noqa: BLE001
                    pass  # The write must happen whatever the counting does
            original(store, mode, json_data)

        self._original = original
        self._wrapper = _write_prepared_data
        Store._write_prepared_data = _write_prepared_data  # type: ignore[method-assign, assignment]  # noqa: SLF001

    def uninstall(self) -> None:
        """Remove the wrapper if it is still in place."""
        self._active[0] = False
        if self._wrapper is None:
            return
        if Store._write_prepared_data is self._wrapper:  # noqa: SLF001
            Store._write_prepared_data = self._original  # type: ignore[method-assign, assignment]  # noqa: SLF001
        self._wrapper = None
        self._original = None

    def drain(self, owner_of: Callable[[str], str]) -> list[StoreWrites]:
        """Return the writes since the last drain, by integration."""
        with self._lock:
            counts, self._counts_ref[0] = self._counts_ref[0], {}
        rows: dict[str, list[Any]] = {}
        for key, (writes, size) in counts.items():
            domain = owner_of(key)
            if (row := rows.get(domain)) is None:
                row = rows[domain] = [0, 0, Counter()]
            row[0] += writes
            row[1] += size
            row[2][key] += writes
        return [StoreWrites(domain, *row) for domain, row in rows.items()]


def store_domain(key: str, components: set[str] | frozenset[str]) -> str:
    """Return the integration a storage key belongs to, else core."""
    first = key.split(".", 1)[0]
    return first if first in components and first != "core" else "core"
