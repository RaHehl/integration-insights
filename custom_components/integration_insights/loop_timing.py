"""Time the callbacks of the event loop, exactly.

Every step of the event loop, a task step included, runs through
asyncio.events.Handle._run. A wrapper around it charges the wall time of
each callback to what ran: a task step to its coroutine and the class of
its `self`, a HassJob to its target, anything else to its function or
method and class, each split by the config entry whose context it runs in.
Unlike the sampler this sees time a callback spends waiting, not only CPU.

A callback times everything that runs inside it, including listeners of
other integrations it triggers. So a long block is not blamed on the
callback: a watchdog thread looks at the loop thread's stack while the
block lasts and names the code that is actually running.

The same code is proposed for Home Assistant's profiler integration
(home-assistant/core#189557). Once a release has it, this can use the
profiler's actions instead of its own wrapper.
"""

import asyncio
from asyncio import events
from collections import deque
from collections.abc import Callable
from functools import partial
import sys
import threading
import time
from types import FrameType, FunctionType, ModuleType
from typing import Any, NamedTuple

from homeassistant.config_entries import current_entry
from homeassistant.core import HassJob

# Only code compiled at runtime can add rows without bound
MAX_ROWS = 10_000
# Coroutines remembered between two drains
MAX_COROUTINES = 50_000


# A callback this long is a block: the watchdog looks at the stack
BLOCK_NS = 1_000_000_000
WATCHDOG_INTERVAL = 0.5
MAX_BLOCKS = 100

type _Key = tuple[Any, type | None, str | None]
_OTHER: _Key = ("other", None, None)
# Helpers that run an integration's job from a method of their own; the job
# is the work, the helper is plumbing.
_HELPER_MODULES = ("homeassistant.helpers.",)
_HELPER_JOBS = ("_run_job", "job", "_job", "listener_job")
# asyncio transports hand data to a protocol; the protocol is the work.
_TRANSPORT_MODULES = ("asyncio.",)


# What kind of owner a class is, decided once per class: plain, a transport,
# or a helper with the names under which it may hold a HassJob.
_PLAIN = 0
_TRANSPORT = 1
_KINDS: dict[type, int | tuple[str, ...]] = {}


def _classify(cls: type, owner: Any) -> int | tuple[str, ...]:
    module = cls.__module__
    if module.startswith(_HELPER_MODULES):
        instance = getattr(owner, "__dict__", None) or {}
        names = tuple(
            name
            for name in _HELPER_JOBS
            if name in instance or any(name in vars(klass) for klass in cls.__mro__)
        )
        return names or _PLAIN
    if module.startswith(_TRANSPORT_MODULES):
        return _TRANSPORT
    return _PLAIN


def _unwrap_helper(owner: Any) -> Any:
    """Return the integration's callable behind a helper or transport, or None.

    Only helpers of Home Assistant and asyncio transports are looked into,
    whose attributes are plain, so getattr runs no code of the object.
    """
    cls = type(owner)
    if (kind := _KINDS.get(cls)) is None:
        kind = _KINDS[cls] = _classify(cls, owner)
    if kind == _PLAIN:
        return None
    if kind == _TRANSPORT:
        protocol = getattr(owner, "_protocol", None)
        # An SSL protocol wraps the application's protocol
        if protocol is not None and type(protocol).__module__.startswith(
            _TRANSPORT_MODULES
        ):
            protocol = getattr(protocol, "_app_protocol", None) or protocol
        return protocol
    for name in kind:  # type: ignore[union-attr]
        if isinstance(job := getattr(owner, name, None), HassJob):
            return job.target
    return None


class CallbackRecord(NamedTuple):
    """What one callback did on the loop since the last drain."""

    label: str  # module.Class.function
    entry_id: str | None
    calls: int
    total_ns: int
    max_ns: int
    max_at: float  # time.time() when the longest run ended


class BlockRecord(NamedTuple):
    """One callback that held the loop for BLOCK_NS or longer."""

    at: float  # time.time() when it ended
    duration_ns: int
    callback: str  # label of the callback that was started
    blocker: tuple[str, str] | None  # (domain, function) from the stack, if seen


class _Row:
    """What one owner did on the loop."""

    __slots__ = ("code", "count", "label", "max_at", "max_ns", "total_ns")

    def __init__(self, label: str, code: Any = None) -> None:
        self.label = label
        # Held so that the id of the code object in the row's key stays valid
        self.code = code
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

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        blocker: Callable[[FrameType | None], tuple[str, str] | None] | None = None,
    ) -> None:
        """Initialize; start() installs the wrapper.

        blocker names the code a frame of the loop thread is running, for the
        watchdog. Without it, blocks are kept without a name.
        """
        self._loop = loop
        self._blocker = blocker
        self._rows: dict[_Key, _Row] = {}
        # Coroutine -> its row, cleared with the rows; a coroutine that
        # finished stays referenced until the next drain, nothing else
        self._coro_rows: dict[Any, _Row] = {}
        # (function, id(object), entry) -> (object, row); the object is kept
        # so the id stays its own until the next drain
        self._method_rows: dict[tuple[Any, int, str | None], tuple[Any, _Row]] = {}
        self._blocks: deque[BlockRecord] = deque(maxlen=MAX_BLOCKS)
        # Start of the callback running now, and who the watchdog saw in it
        self._running = [0]
        self._seen: dict[int, tuple[str, str] | None] = {}
        self._active = [False]
        self._overhead = [0]
        self._resolves = [0]
        self._original: Callable[[Any], None] | None = None
        self._wrapper: Callable[[Any], None] | None = None
        self._watchdog: threading.Thread | None = None
        self._stop_watchdog = threading.Event()
        # For the sampler: frames of the wrapper are not an integration's
        self.wrapper_code: Any = None
        self.errors = 0
        self.started_at = 0.0

    @property
    def resolves(self) -> int:
        """Return how many callbacks were resolved the slow way."""
        return self._resolves[0]

    @property
    def overhead_ns(self) -> int:
        """Return the time the wrapper itself has used, in ns."""
        return self._overhead[0]

    def start(self) -> None:  # noqa: C901
        """Install the wrapper on Handle._run."""
        if self._wrapper is not None:
            return
        original = events.Handle._run  # noqa: SLF001
        loop = self._loop
        rows = self._rows
        coro_rows = self._coro_rows
        method_rows = self._method_rows
        kinds = _KINDS
        resolves = self._resolves
        function_type = FunctionType
        partial_type = partial
        blocks = self._blocks
        running = self._running
        seen = self._seen
        active = self._active
        overhead = self._overhead
        counter = [0]
        perf = time.perf_counter_ns
        wall = time.time
        task_type = asyncio.Task

        def resolve(handle: Any, func: Any, owner: Any, coro: Any) -> _Row:
            """Return the row of a callback, from the live objects."""
            module = None
            if coro is not None:
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
                if owner is not None and ((inner := _unwrap_helper(owner)) is not None):
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
            # Code objects compare by value, so two alike functions in
            # different modules would share a row, and hashing one costs
            # with its size: the id stands for it, and the row holds the
            # object so the id stays its own. C code has no code object;
            # module and qualified name must do.
            key: _Key = (
                id(code)
                if code is not None
                else (
                    module or type(func).__module__,
                    getattr(func, "__qualname__", type(func).__qualname__),
                ),
                cls,
                entry.entry_id if entry is not None else None,
            )
            if (row := rows.get(key)) is not None:
                return row
            if len(rows) >= MAX_ROWS:
                key = _OTHER
                if (row := rows.get(key)) is not None:
                    return row
            label = (
                "other callbacks" if key is _OTHER else _label(func, code, cls, module)
            )
            row = rows[key] = _Row(label, code)
            return row

        # The wrapper runs once per callback: as little as possible inline
        def _run(handle: Any) -> None:  # noqa: C901
            # Once stopped, a wrapper that something else wrapped in turn
            # stays in the chain; it only passes calls on
            if not active[0] or handle._loop is not loop:  # noqa: SLF001
                original(handle)
                return
            # The wrapper's own cost is measured on every 32nd callback
            count = counter[0] = counter[0] + 1
            entered = perf() if count & 31 == 0 else 0
            # Nothing in here may escape into the loop, hence one wide try
            row = coro = mkey = None
            pin: Any = None
            try:
                # Before the run: a callback that cancels its handle drops
                # _callback, and a coroutine that finishes drops its frame
                func = handle._callback  # noqa: SLF001
                owner = getattr(func, "__self__", None)
                if type(owner) is task_type:
                    # Every step of a task runs the same coroutine for the same
                    # object in the same context: resolved once
                    coro = owner.get_coro()
                    row = coro_rows.get(coro)
                else:
                    # The same row for every call in the same context. A
                    # partial or a HassJob passed as argument is often a
                    # long-lived object, a reader or a timer: its identity
                    # is the key, a one-shot one is dropped with the next
                    # drain
                    args = handle._args  # noqa: SLF001
                    fn_key = None
                    if type(func) is partial_type:
                        pin = func
                    elif args and isinstance(args[-1], HassJob):
                        pin = args[-1]
                    else:
                        if owner is None and not hasattr(func, "__qualname__"):
                            # A callable object, such as a helper that
                            # schedules itself: its class is the key
                            owner = func
                            func = type(func).__call__
                        if owner is None:
                            # A closure made per call is a new function each
                            # time, its code is not; C functions are themselves.
                            # Code objects compare by value, so the pin holds
                            # the object and the key is its id
                            pin = func.__code__ if type(func) is function_type else func
                        else:
                            cls = type(owner)
                            if (kind := kinds.get(cls)) is None:
                                kind = kinds[cls] = _classify(cls, owner)
                            if kind == _PLAIN:
                                # A plain object's row depends on its class, so
                                # a one-shot object such as a future hits too
                                pin = cls
                            elif kind != _TRANSPORT:
                                # A one-shot helper such as a timer is a new
                                # object each time, the job it runs is not
                                for name in kind:  # type: ignore[union-attr]
                                    if (job := getattr(owner, name, None)) is not None:
                                        target = job.target
                                        if (
                                            inner := getattr(target, "__self__", None)
                                        ) is not None:
                                            func, owner = target, inner
                                            pin = owner
                                        elif type(target) is function_type:
                                            func, owner = target, None
                                        break
                            if owner is not None:
                                if pin is None:
                                    pin = owner
                                fn_key = (
                                    getattr(func, "__func__", None)
                                    or getattr(func, "__name__", None)
                                    or func
                                )
                            else:
                                pin = (
                                    func.__code__
                                    if type(func) is function_type
                                    else func
                                )
                    entry = handle._context.get(current_entry)  # noqa: SLF001
                    mkey = (
                        fn_key,
                        id(pin),
                        entry.entry_id if entry is not None else None,
                    )
                    if (hit := method_rows.get(mkey)) is not None and hit[0] is pin:
                        row = hit[1]
                if row is None:
                    resolves[0] += 1
                    row = resolve(handle, func, owner, coro)
                    if coro is not None:
                        if len(coro_rows) >= MAX_COROUTINES:
                            coro_rows.clear()
                        coro_rows[coro] = row
                    elif mkey is not None:
                        if len(method_rows) >= MAX_COROUTINES:
                            method_rows.clear()
                        # The object or class is held so its id stays its own
                        method_rows[mkey] = (pin, row)
            except Exception:  # noqa: BLE001
                self.errors += 1
            outer = running[0]
            start = running[0] = perf()
            try:
                original(handle)
            finally:
                end = perf()
                running[0] = outer
                elapsed = end - start
                if row is not None:
                    row.count += 1
                    row.total_ns += elapsed
                    if elapsed > row.max_ns:
                        row.max_ns = elapsed
                        row.max_at = wall()
                if elapsed >= BLOCK_NS:
                    blocks.append(
                        BlockRecord(
                            wall(),
                            elapsed,
                            row.label if row is not None else "?",
                            seen.pop(start, None),
                        )
                    )
                if entered:
                    overhead[0] += 32 * ((start - entered) + (perf() - end))

        self._original = original
        self._wrapper = _run
        self.wrapper_code = _run.__code__
        self.started_at = time.time()
        self._active[0] = True
        events.Handle._run = _run  # type: ignore[method-assign, assignment]  # noqa: SLF001
        if self._blocker is not None:
            self._stop_watchdog.clear()
            self._watchdog = threading.Thread(
                target=self._watch,
                args=(threading.get_ident(),),
                name="integration_insights watchdog",
                daemon=True,
            )
            self._watchdog.start()

    def _watch(self, loop_thread: int) -> None:
        """Name the code of a callback that has run for BLOCK_NS, once per callback."""
        assert self._blocker is not None
        last = 0
        while not self._stop_watchdog.wait(WATCHDOG_INTERVAL):
            start = self._running[0]
            if not start or start == last:
                continue
            if time.perf_counter_ns() - start < BLOCK_NS:
                continue
            last = start
            try:
                frame = sys._current_frames().get(loop_thread)  # noqa: SLF001
                self._seen[start] = self._blocker(frame)
            except Exception:  # noqa: BLE001
                self.errors += 1
            # Entries of callbacks that ended between the check and here
            if len(self._seen) > MAX_BLOCKS:
                self._seen.clear()

    def stop(self) -> None:
        """Remove the wrapper, if it is still in place, and stop the watchdog."""
        self._active[0] = False
        self._stop_watchdog.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=1)
            self._watchdog = None
        if self._wrapper is None:
            return
        if events.Handle._run is self._wrapper:  # noqa: SLF001
            events.Handle._run = self._original  # type: ignore[method-assign, assignment]  # noqa: SLF001
        self._wrapper = None
        self._original = None

    def drain(self) -> list[CallbackRecord]:
        """Return what ran since the last drain and start over.

        Runs on the loop, like the wrapper, and works in place: the wrapper
        holds the same dicts. Rows that ran stay, with their counts reset,
        so the caches pointing at them stay valid; rows that did not run are
        dropped together with their cache entries, which also lets go of
        the objects those entries hold.
        """
        rows = self._rows
        idle = {id(row) for row in rows.values() if not row.count}
        if idle:
            coro_rows = self._coro_rows
            for coro in [c for c, row in coro_rows.items() if id(row) in idle]:
                del coro_rows[coro]
            method_rows = self._method_rows
            for mkey in [k for k, (_, row) in method_rows.items() if id(row) in idle]:
                del method_rows[mkey]
            for key in [k for k, row in rows.items() if id(row) in idle]:
                del rows[key]
        # A coroutine that finished keeps nothing but itself alive; still,
        # there is no point in remembering it
        coro_rows = self._coro_rows
        for coro in [c for c in coro_rows if getattr(c, "cr_frame", False) is None]:
            del coro_rows[coro]
        records = [
            CallbackRecord(
                row.label, key[2], row.count, row.total_ns, row.max_ns, row.max_at
            )
            for key, row in rows.items()
        ]
        for row in rows.values():
            row.count = row.total_ns = row.max_ns = 0
            row.max_at = 0.0
        return records

    def drain_blocks(self) -> list[BlockRecord]:
        """Return the blocks since the last drain."""
        blocks = list(self._blocks)
        self._blocks.clear()
        return blocks
