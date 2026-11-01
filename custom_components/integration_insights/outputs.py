"""What integrations write out: log records and files through the storage helper.

Both are counted where they happen, in any thread, and handed to the loop on
drain. The counting is a dictionary update under a lock; the names are
mapped to integrations on the loop.
"""

from collections import Counter
from collections.abc import Callable, Mapping
import functools
import logging
import os
import threading
from typing import Any
import weakref

from homeassistant.const import BASE_PLATFORMS
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers.storage import Store

from .collector import LogCounts, StoreWrites

DEBUG, INFO, WARNING, ERROR = 0, 1, 2, 3
# Store counters whose lock a forked child must not inherit held
_STORE_COUNTERS: weakref.WeakSet[StoreCounter] = weakref.WeakSet()


def _renew_store_locks() -> None:
    for counter in _STORE_COUNTERS:
        counter._lock = threading.RLock()  # noqa: SLF001


os.register_at_fork(after_in_child=_renew_store_locks)
# The recorder warns about attributes it will not store, naming the state:
# the entity's integration wrote them, so the warnings count for it
OVERSIZED_ATTRIBUTES = "State attributes for %s exceed"
UNSERIALIZABLE_STATE = "State is not JSON serializable"
# Messages that name the integration they are about as their first argument
NAMES_DOMAIN = ("Error during service call to %s.%s", "Error with %s describe event")
# Integrations that serve all others: what their loggers report is about
# other integrations or about Home Assistant itself, so it counts for core
# unless a record names the integration it is about
SERVES_OTHERS = frozenset({"api", "http", "recorder", "websocket_api"})
# Integrations whose warnings often name the integration they are about:
# a failed service call, a failed automation, a logbook describe error
NAMES_SUBJECT = SERVES_OTHERS | {"automation", "logbook"}
# Never the subject of a record: these run on behalf of others
NOT_A_SUBJECT = SERVES_OTHERS | frozenset(BASE_PLATFORMS) | {"homeassistant"}
_COMPONENTS = "homeassistant.components."


class LogCounter(logging.Handler):
    """Count the log records that reach the root logger, by logger and level."""

    def __init__(self) -> None:
        """Initialize; install() attaches the handler."""
        super().__init__(logging.NOTSET)
        # logging's own handler lock: reentrant, since a finalizer that logs
        # can run inside emit on the same thread, and renewed after a fork
        self._lock = self.lock or threading.RLock()
        # logger name -> [debug, info, warning, error and up]
        self._counts: dict[str, list[int]] = {}
        # (entity_id, logger) -> recorder warnings about its attributes
        self._oversized: dict[tuple[str, str], int] = {}
        self._installed = False

    def handle(self, record: logging.LogRecord) -> bool:
        """Count without the handler lock: emit takes its own, shorter one."""
        if not (result := self.filter(record)):
            return False
        # Never break the logging call, and never log about it. A plain try:
        # suppress() costs a context manager on every record.
        try:  # noqa: SIM105
            self.emit(result if isinstance(result, logging.LogRecord) else record)
        except Exception:  # noqa: BLE001
            pass
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Count the record; it is never formatted or written here."""
        level = record.levelno
        slot = (
            ERROR
            if level >= logging.ERROR
            else WARNING
            if level >= logging.WARNING
            else INFO
            if level >= logging.INFO
            else DEBUG
        )
        name = record.name
        if (
            slot >= WARNING
            and name.startswith(_COMPONENTS)
            and name[len(_COMPONENTS) :].partition(".")[0] in NAMES_SUBJECT
            and isinstance(record.args, tuple)
            and record.args
        ):
            entity_id, domain = _subject(record.msg, record.args)
            if entity_id is not None:
                key = (entity_id, name)
                with self._lock:
                    self._oversized[key] = self._oversized.get(key, 0) + 1
                return
            if domain is not None:
                name = f"{name} [{domain}]"
        with self._lock:
            if (row := self._counts.get(name)) is None:
                row = self._counts[name] = [0, 0, 0, 0]
            row[slot] += 1

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

    def drain(
        self,
        owner_of: Callable[[str], str],
        entity_owner: Callable[[str], str | None] = lambda _entity_id: None,
    ) -> list[LogCounts]:
        """Return the counts since the last drain, by integration.

        Recorder warnings about an entity's attributes count for the
        integration of the entity, if it has one, and apart as oversized:
        the integration did not log them.
        """
        with self._lock:
            counts, self._counts = self._counts, {}
            oversized, self._oversized = self._oversized, {}
        rows: dict[str, list[Any]] = {}

        def add(domain: str, name: str, levels: list[int]) -> None:
            if (row := rows.get(domain)) is None:
                row = rows[domain] = [0, 0, 0, 0, Counter(), 0]
            for slot, count in enumerate(levels):
                row[slot] += count
            if noted := levels[WARNING] + levels[ERROR]:
                row[4][name] += noted

        for name, levels in counts.items():
            add(owner_of(name), name, levels)
        for (entity_id, name), warnings in oversized.items():
            domain = entity_owner(entity_id) or owner_of(name)
            add(domain, name, [0, 0, warnings, 0])
            rows[domain][5] += warnings
        return [LogCounts(domain, *row) for domain, row in rows.items()]


def _subject(msg: Any, args: tuple[Any, ...]) -> tuple[str | None, str | None]:
    """Return the entity or the integration a record is about, if it names one."""
    if not isinstance(msg, str):
        return None, None
    # The websocket API puts the connection in front: "[123] Error ..."
    if msg.startswith("[") and (end := msg.find("] ")) > 0:
        msg = msg[end + 2 :]
    first = args[0]
    if msg.startswith(OVERSIZED_ATTRIBUTES) and isinstance(first, str):
        return first, None
    if msg.startswith(UNSERIALIZABLE_STATE):
        entity_id = getattr(first, "entity_id", None)
        return (entity_id, None) if isinstance(entity_id, str) else (None, None)
    # The error a failed call or automation raised names the integration it
    # came from, also behind a platform service such as light.turn_on or a
    # script, so it goes before the domain the message names
    domain: str | None
    for arg in args:
        if isinstance(arg, ServiceNotFound):
            domain = arg.domain
        elif isinstance(arg, HomeAssistantError):
            domain = arg.translation_domain
        else:
            continue
        if domain and domain not in NOT_A_SUBJECT:
            return None, domain
    if (
        msg.startswith(NAMES_DOMAIN)
        and isinstance(first, str)
        and first not in NOT_A_SUBJECT
    ):
        return None, first
    return None, None


def logger_domain(
    name: str,
    library_map: Mapping[str, str],
    cache: dict[str, str],
    known: set[str] | frozenset[str] | None = None,
) -> str:
    """Return the integration a logger belongs to, else core.

    Entity platforms log through the logger of their base platform, such as
    homeassistant.components.sensor, for whichever integration provides the
    entities, and the homeassistant integration logs for triggers and
    scenes a user set up. Those records cannot be told apart by name, so
    they count for core, like those of integrations that serve all others.
    """
    if (domain := cache.get(name)) is not None:
        return domain
    parts = name.split(".")
    if name.endswith("]") and " [" in name:
        # A record that names the integration it is about; one that is not
        # loaded counts for the logger, as if nothing were named
        cut = name.rindex(" [")
        domain = name[cut + 2 : -1]
        if known is not None and domain not in known:
            domain = logger_domain(name[:cut], library_map, cache, known)
    elif parts[0] == "homeassistant":
        domain = (
            parts[2]
            if len(parts) > 2
            and parts[1] == "components"
            and parts[2] not in NOT_A_SUBJECT
            else "core"
        )
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
        self._lock = threading.RLock()
        # storage key -> [writes, bytes]; swapped as a whole on drain
        self._counts_ref: list[dict[str, list[int]]] = [{}]
        self._original: Callable[..., Any] | None = None
        self._wrapper: Callable[..., Any] | None = None
        self._name = ""
        self._active = [False]
        self.wrapper_code: Any = None
        _STORE_COUNTERS.add(self)

    def install(self) -> bool:
        """Wrap the step every save goes through; return whether one was found.

        Home Assistant 2026.11 serializes in Store._prepare_data, which
        returns the bytes; before, Store._write_prepared_data got them.
        Without either, saves are not counted and nothing else changes.
        """
        if self._wrapper is not None:
            return True
        counts = self._counts_ref
        active = self._active = [True]
        counter = self

        def note(store: Any, prepared: Any) -> None:
            try:
                # Characters of a str: json.dumps escapes everything else
                size = len(prepared[1])
                with counter._lock:
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

            # Any arguments pass through, so a new one in core keeps saving
            def wrapper(store: Any, *args: Any, **kwargs: Any) -> Any:
                result = prepare(store, *args, **kwargs)
                # Once removed, a wrapper that something else wrapped in turn
                # stays in the chain; it only passes calls on
                if active[0]:
                    note(store, result)
                return result

        elif (original := getattr(Store, "_write_prepared_data", None)) is not None:
            name = "_write_prepared_data"
            write = original

            def wrapper(store: Any, *args: Any, **kwargs: Any) -> Any:
                if active[0]:
                    note(
                        store,
                        args if len(args) > 1 else (None, kwargs.get("json_data")),
                    )
                return write(store, *args, **kwargs)

        else:
            active[0] = False
            return False
        # Executor jobs and samples of a save belong to whoever saves, not to
        # us: the wrapper takes the original's name and the sampler skips it
        functools.update_wrapper(wrapper, original)
        self.wrapper_code = wrapper.__code__
        self._name = name
        self._original = original
        self._wrapper = wrapper
        setattr(Store, name, wrapper)
        return True

    @property
    def installed(self) -> bool:
        """Return whether saves are being counted."""
        return self._wrapper is not None

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
