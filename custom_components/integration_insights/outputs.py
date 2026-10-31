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
        self._original: Callable[..., Any] | None = None
        self._wrapper: Callable[..., Any] | None = None
        self._name = ""
        self._active = [False]

    def install(self) -> bool:
        """Wrap the step every save goes through; return whether one was found.

        Home Assistant 2026.11 serializes in Store._prepare_data, which
        returns the bytes; before, Store._write_prepared_data got them.
        Without either, saves are not counted and nothing else changes.
        """
        if self._wrapper is not None:
            return True
        counts = self._counts_ref
        lock = self._lock
        active = self._active = [True]

        def note(store: Any, json_data: Any) -> None:
            try:
                size = len(json_data)
                with lock:
                    table = counts[0]
                    if (row := table.get(store.key)) is None:
                        row = table[store.key] = [0, 0]
                    row[0] += 1
                    row[1] += size
            except Exception:  # noqa: BLE001
                pass  # The save must happen whatever the counting does

        original: Any
        if (original := getattr(Store, "_prepare_data", None)) is not None:
            name = "_prepare_data"
            prepare = original

            def wrapper(store: Any, data: Any) -> Any:
                result = prepare(store, data)
                # Once removed, a wrapper that something else wrapped in turn
                # stays in the chain; it only passes calls on
                if active[0]:
                    note(store, result[1])
                return result

        elif (original := getattr(Store, "_write_prepared_data", None)) is not None:
            name = "_write_prepared_data"
            write = original

            def wrapper(store: Any, mode: str, json_data: Any) -> Any:  # type: ignore[misc]
                if active[0]:
                    note(store, json_data)
                return write(store, mode, json_data)

        else:
            active[0] = False
            return False
        self._name = name
        self._original = original
        self._wrapper = wrapper
        setattr(Store, name, wrapper)
        return True

    def uninstall(self) -> None:
        """Remove the wrapper if it is still in place."""
        self._active[0] = False
        if self._wrapper is None:
            return
        if getattr(Store, self._name, None) is self._wrapper:
            setattr(Store, self._name, self._original)
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
