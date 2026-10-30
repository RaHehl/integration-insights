"""Time the callbacks of the event loop, exactly.

Every step of the event loop, a task step included, runs through
asyncio.events.Handle._run. A wrapper around it charges the wall time of
each callback to what ran: a task step to its coroutine and the class of
its `self`, a HassJob to its target, anything else to its function or
method and class, each split by the config entry whose context it runs in.
Unlike the sampler this sees time a callback spends waiting, not only CPU.

The same code is proposed for Home Assistant's profiler integration
(home-assistant/core#189557). Once a release has it, this can use the
profiler's actions instead of its own wrapper.
"""

import asyncio
from asyncio import events
from collections.abc import Callable
from functools import partial
import inspect
import time
from types import MemberDescriptorType, ModuleType
from typing import Any, NamedTuple

from homeassistant.config_entries import current_entry
from homeassistant.core import HassJob

# Only code compiled at runtime can add rows without bound
MAX_ROWS = 10_000

type _Key = tuple[Any, type | None, str | None]
_OTHER: _Key = ("other", None, None)
# Helpers that run an integration's job from a method of their own; the job
# is the work, the helper is plumbing.
_HELPER_MODULES = ("homeassistant.helpers.",)
_HELPER_JOBS = ("_run_job", "job", "_job", "listener_job")
# asyncio transports hand data to a protocol; the protocol is the work.
_TRANSPORT_MODULES = ("asyncio.",)


def _static(obj: Any, name: str) -> Any:
    """Read an attribute without running Python code of the object."""
    try:
        value = inspect.getattr_static(obj, name)
    except AttributeError:
        return None
    if isinstance(value, MemberDescriptorType):
        try:
            return value.__get__(obj, type(obj))
        except AttributeError:
            return None
    return value


def _unwrap_helper(owner: Any) -> Any:
    """Return the integration's callable behind a helper or transport, or None."""
    module = type(owner).__module__
    if module.startswith(_HELPER_MODULES):
        for name in _HELPER_JOBS:
            if isinstance(job := _static(owner, name), HassJob):
                return job.target
        return None
    if module.startswith(_TRANSPORT_MODULES):
        return _static(owner, "_protocol")
    return None


class CallbackRecord(NamedTuple):
    """What one callback did on the loop since the last drain."""

    label: str  # module.Class.function
    entry_id: str | None
    calls: int
    total_ns: int
    max_ns: int
    max_at: float  # time.time() when the longest run ended


class _Row:
    """What one owner did on the loop."""

    __slots__ = ("count", "label", "max_at", "max_ns", "total_ns")

    def __init__(self, label: str) -> None:
        self.label = label
        self.count = 0
        self.total_ns = 0
        self.max_ns = 0
        self.max_at = 0.0


def _label(func: Any, code: Any, cls: type | None, module: str | None) -> str:
    """Return module.Class.function from the live objects of a callback."""
    if cls is not None:
        name = code.co_name if code is not None else getattr(func, "__name__", "?")
        return f"{cls.__module__}.{cls.__qualname__}.{name}"
    if code is not None:
        return f"{module}.{code.co_qualname}" if module else code.co_qualname
    name = getattr(func, "__qualname__", None) or type(func).__qualname__
    return f"{module or type(func).__module__}.{name}"


class LoopTiming:
    """Record how long each callback holds the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize; start() installs the wrapper."""
        self._loop = loop
        self._rows: dict[_Key, _Row] = {}
        self._original: Callable[[Any], None] | None = None
        self._wrapper: Callable[[Any], None] | None = None
        # For the sampler: frames of the wrapper are not an integration's
        self.wrapper_code: Any = None
        self.errors = 0
        self.started_at = 0.0

    def start(self) -> None:
        """Install the wrapper on Handle._run."""
        if self._wrapper is not None:
            return
        original = events.Handle._run  # noqa: SLF001
        loop = self._loop
        rows = self._rows
        perf = time.perf_counter_ns
        wall = time.time
        task_type = asyncio.Task

        # One function with everything inline: it runs once per callback
        def _run(handle: Any) -> None:
            if handle._loop is not loop:  # noqa: SLF001
                original(handle)
                return
            row = None
            key: _Key = _OTHER
            # Nothing in here may escape into the loop, hence one wide try
            try:
                # Before the run: a callback that cancels its handle drops
                # _callback, and a coroutine that finishes drops its frame
                func = handle._callback  # noqa: SLF001
                owner = getattr(func, "__self__", None)
                module = None
                if isinstance(owner, task_type):
                    coro: Any = owner.get_coro()
                    code = getattr(coro, "cr_code", None)
                    frame = getattr(coro, "cr_frame", None)
                    if frame is not None:
                        owner = frame.f_locals.get("self")
                        module = frame.f_globals.get("__name__")
                    else:
                        owner = None
                    if code is None:
                        # Not a coroutine: an async generator step, say
                        func = coro
                else:
                    args = handle._args  # noqa: SLF001
                    if args and isinstance(args[-1], HassJob):
                        func = args[-1].target
                    while isinstance(func, partial):
                        func = func.func
                    owner = getattr(func, "__self__", None)
                    if owner is None and not hasattr(func, "__qualname__"):
                        # A callable instance is charged to its class
                        owner = func
                        func = type(func).__call__
                    if owner is not None and (
                        (inner := _unwrap_helper(owner)) is not None
                    ):
                        if callable(inner):
                            func = inner
                            while isinstance(func, partial):
                                func = func.func
                            owner = getattr(func, "__self__", None)
                        else:
                            # A protocol object: the transport's method ran for it
                            owner = inner
                    func = getattr(func, "__func__", func)
                    code = getattr(func, "__code__", None)
                    module = getattr(func, "__module__", None)
                if owner is None or isinstance(owner, ModuleType):
                    cls = None
                else:
                    cls = owner if isinstance(owner, type) else type(owner)
                entry = handle._context.get(current_entry)  # noqa: SLF001
                # C code has no code object; module and qualified name must do
                key = (
                    code
                    if code is not None
                    else (
                        module or type(func).__module__,
                        getattr(func, "__qualname__", type(func).__qualname__),
                    ),
                    cls,
                    entry.entry_id if entry is not None else None,
                )
                if (row := rows.get(key)) is None:
                    if len(rows) >= MAX_ROWS:
                        key = _OTHER
                        row = rows.get(key)
                    if row is None:
                        label = (
                            "other callbacks"
                            if key is _OTHER
                            else _label(func, code, cls, module)
                        )
                        row = rows[key] = _Row(label)
            except Exception:  # noqa: BLE001
                self.errors += 1
            start = perf()
            try:
                original(handle)
            finally:
                if row is not None:
                    elapsed = perf() - start
                    row.count += 1
                    row.total_ns += elapsed
                    if elapsed > row.max_ns:
                        row.max_ns = elapsed
                        row.max_at = wall()

        self._original = original
        self._wrapper = _run
        self.wrapper_code = _run.__code__
        self.started_at = time.time()
        events.Handle._run = _run  # type: ignore[method-assign, assignment]  # noqa: SLF001

    def stop(self) -> None:
        """Remove the wrapper, if it is still in place."""
        if self._wrapper is None:
            return
        if events.Handle._run is self._wrapper:  # noqa: SLF001
            events.Handle._run = self._original  # type: ignore[method-assign, assignment]  # noqa: SLF001
        self._wrapper = None
        self._original = None

    def drain(self) -> list[CallbackRecord]:
        """Return what ran since the last drain and start over.

        Runs on the loop, like the wrapper, and clears in place: the wrapper
        holds the same dict.
        """
        rows = self._rows
        records = [
            CallbackRecord(
                row.label, key[2], row.count, row.total_ns, row.max_ns, row.max_at
            )
            for key, row in rows.items()
            if row.count
        ]
        rows.clear()
        return records
