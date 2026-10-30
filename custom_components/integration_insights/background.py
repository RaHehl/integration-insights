"""CPU outside the event loop: executor jobs and threads, and loop wall time.

Executor jobs are timed exactly with the worker thread's CPU clock and charged
to the integration that submitted them. Other threads are read from
/proc/self/task every drain and charged to the integration that owns them.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Executor, Future
import os
from pathlib import Path
import sys
import threading
import time
from types import CodeType, FrameType
from typing import Any, NamedTuple

from .attribution import callable_owner, module_domain, top_package
from .sampler import Record

MAX_PENDING_JOBS = 20_000
# Pool workers run a callable handed to them, often C code with no frame of its
# own, so their stack only shows the pool. Their loop holds it in a local.
_POOL_LOCALS = {
    "anyio._backends._asyncio": "func",
    "concurrent.futures.thread": "fn",
}
# Code that only passes work along; the label names what is behind it.
_PLUMBING = ("anyio", "asyncio", "concurrent", "threading")
_TICK_NS = 1_000_000_000 // os.sysconf("SC_CLK_TCK")


class Owner(NamedTuple):
    """Who a job or thread belongs to."""

    domain: str  # "core" or an integration domain
    libraries: tuple[str, ...]  # third party packages, for the library map


class JobRecord(NamedTuple):
    """One finished executor job."""

    owner: Owner
    label: str  # module and qualname of the callable, never its arguments
    cpu_ns: int
    wall_ns: int


class ThreadRecord(NamedTuple):
    """CPU a thread used since the previous poll."""

    owner: Owner
    name: str
    cpu_ns: int


type Walk = Callable[[FrameType | None, int], Record]


class Background:
    """Charge executor jobs and threads to integrations."""

    def __init__(self, walk: Walk, library_map: Callable[[], dict[str, str]]) -> None:
        """Initialize.

        walk classifies a stack like the loop sampler does. library_map returns
        the current import name to integration map.
        """
        self._walk = walk
        self._library_map = library_map
        self.jobs: deque[JobRecord] = deque(maxlen=MAX_PENDING_JOBS)
        self.jobs_total = 0
        self._hooked: list[tuple[Executor, Callable[..., Future[Any]]]] = []
        self._thread_cpu: dict[int, int] = {}
        self._thread_owners: dict[int, tuple[threading.Thread, Owner | None]] = {}
        self._primed = False

    # Executor jobs

    def hook_executor(self, executor: Executor) -> None:
        """Time every job submitted to executor from now on."""
        original = executor.submit
        resolve = self.resolve
        jobs = self.jobs

        def submit(fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
            try:
                owner, label = resolve(fn, sys._getframe(1))  # noqa: SLF001
            except Exception:  # noqa: BLE001
                # Whatever a callable does when looked at, the job must run.
                owner, label = Owner("core", ()), "?"
            self.jobs_total += 1
            return original(_timed, jobs, owner, label, fn, args, kwargs)

        executor.submit = submit  # type: ignore[method-assign]
        self._hooked.append((executor, submit))

    def unhook_executors(self) -> None:
        """Remove the hooks that are still in place."""
        for executor, submit in self._hooked:
            if vars(executor).get("submit") is submit:
                del executor.submit
        self._hooked.clear()

    def resolve(self, fn: Any, frame: FrameType | None) -> tuple[Owner, str]:
        """Find the integration a job runs for: its callable, else its caller."""
        domain, libraries, label = callable_owner(fn)
        if domain is not None:
            return Owner(domain, ()), label
        library_map = self._library_map()
        for library in libraries:
            if (owner := library_map.get(library)) is not None:
                return Owner(owner, libraries), label
        record = self._walk(frame, 0)
        domain = record.domain if record.domain != "idle" else "core"
        return Owner(domain, record.libraries or libraries), label

    def drain_jobs(self) -> list[JobRecord]:
        """Return the jobs that finished since the last drain."""
        jobs = self.jobs
        records = []
        while jobs:
            try:
                records.append(jobs.popleft())
            except IndexError:
                break
        return records

    def executor_threads(self, executors: Iterable[Any]) -> set[int]:
        """Return the native ids of executor workers, which jobs already cover."""
        ids: set[int] = set()
        for executor in executors:
            for thread in list(getattr(executor, "_threads", ())):
                if thread.native_id is not None:
                    ids.add(thread.native_id)
        return ids

    # Threads

    def poll_threads(self, skip: set[int]) -> list[ThreadRecord]:
        """Return the CPU each thread used since the last poll (does I/O).

        The first poll only takes a baseline. Threads in skip are left out.
        """
        threads = {
            t.native_id: t for t in threading.enumerate() if t.native_id is not None
        }
        frames: dict[int, FrameType] | None = None
        records: list[ThreadRecord] = []
        seen: set[int] = set()
        try:
            tids = [int(path.name) for path in Path("/proc/self/task").iterdir()]
        except OSError:
            return records
        for tid in tids:
            seen.add(tid)
            if (cpu := _task_cpu_ns(tid)) is None:
                continue
            previous = self._thread_cpu.get(tid, 0 if self._primed else cpu)
            self._thread_cpu[tid] = cpu
            if tid in skip or cpu <= previous:
                continue
            thread = threads.get(tid)
            name: str | None = None
            owner = self._static_owner(tid, thread)
            if owner is None:
                if thread is not None and thread.ident is not None:
                    if frames is None:
                        frames = sys._current_frames()  # noqa: SLF001
                    frame = frames.get(thread.ident)
                    record = self._walk(frame, 0)
                    domain = record.domain if record.domain != "idle" else "core"
                    owner = Owner(domain, record.libraries)
                    if domain == "core" and (pool := _pool_frame(frame)) is not None:
                        owner, label = self._pool_owner(pool)
                        name = f"{thread.name} ({label})"
                else:
                    owner = Owner("core", ())
            if name is None:
                name = thread.name if thread is not None else _task_name(tid)
            records.append(ThreadRecord(owner, name, cpu - previous))
        self._primed = True
        for tid in set(self._thread_cpu) - seen:
            del self._thread_cpu[tid]
            self._thread_owners.pop(tid, None)
        return records

    def _pool_owner(self, pool: FrameType) -> tuple[Owner, str]:
        """Owner of the work a pool worker runs: the task waiting for it, else the callable."""
        fn = pool.f_locals.get(_POOL_LOCALS[pool.f_globals["__name__"]])
        owner, label = self.resolve(fn, None)
        # anyio passes the future a task in the event loop waits on. That task
        # started the call, whatever language the callable is written in.
        if (task := _waiting_task(pool.f_locals.get("future"))) is not None:
            domain, libraries, task_label = _task_owner(task)
            label = task_label or label
            if domain is not None:
                return Owner(domain, libraries), label
            if mapped := next(
                (
                    self._library_map()[lib]
                    for lib in libraries
                    if lib in self._library_map()
                ),
                None,
            ):
                return Owner(mapped, libraries), label
            # Unmapped for now: passing the libraries on lets the map pick them up.
            owner = Owner(owner.domain, owner.libraries + libraries)
        return owner, label

    def _static_owner(self, tid: int, thread: threading.Thread | None) -> Owner | None:
        """Owner from the thread's target or class, cached per thread object."""
        if thread is None:
            return None
        cached = self._thread_owners.get(tid)
        if cached is not None and cached[0] is thread:
            return cached[1]
        owner: Owner | None = None
        target = getattr(thread, "_target", None)
        candidates: list[tuple[str | None, tuple[str, ...]]] = []
        if target is not None:
            domain, libraries, _label = callable_owner(target)
            candidates.append((domain, libraries))
        cls_module = type(thread).__module__
        if cls_module != "threading":
            candidates.append((module_domain(cls_module), (top_package(cls_module),)))
        library_map = self._library_map()
        for domain, libraries in candidates:
            if domain is not None:
                owner = Owner(domain, ())
                break
            if mapped := next(
                (library_map[lib] for lib in libraries if lib in library_map), None
            ):
                owner = Owner(mapped, libraries)
                break
        # Unresolved threads are looked at again: the library map fills later.
        if owner is not None:
            self._thread_owners[tid] = (thread, owner)
        return owner


def _pool_frame(frame: FrameType | None) -> FrameType | None:
    """Return the frame of a pool worker's loop that holds a callable, or None."""
    while frame is not None:
        if (
            local := _POOL_LOCALS.get(frame.f_globals.get("__name__", ""))
        ) and frame.f_locals.get(local) is not None:
            return frame
        frame = frame.f_back
    return None


def _waiting_task(future: Any) -> asyncio.Task[Any] | None:
    """Return the task waiting on an asyncio future, from its done callbacks."""
    if not isinstance(future, asyncio.Future):
        return None
    for callback, _context in future._callbacks or ():  # noqa: SLF001
        if isinstance(task := getattr(callback, "__self__", None), asyncio.Task):
            return task
    return None


def _await_frames(task: asyncio.Task[Any]) -> Iterator[FrameType]:
    """Frames of a suspended task, from its coroutine down to what it awaits."""
    awaitable: Any = task.get_coro()
    while awaitable is not None:
        frame = (
            getattr(awaitable, "cr_frame", None)
            or getattr(awaitable, "ag_frame", None)
            or getattr(awaitable, "gi_frame", None)
        )
        if frame is not None:
            yield frame
        awaitable = (
            getattr(awaitable, "cr_await", None)
            or getattr(awaitable, "ag_await", None)
            or getattr(awaitable, "gi_yieldfrom", None)
        )


def _task_owner(task: asyncio.Task[Any]) -> tuple[str | None, tuple[str, ...], str]:
    """Integration, libraries and label of the innermost real code a task awaits in."""
    domain = None
    libraries: dict[str, None] = {}
    label = ""
    for frame in _await_frames(task):
        module = frame.f_globals.get("__name__", "")
        if (found := module_domain(module)) is not None:
            domain = found
        elif not module.startswith("homeassistant"):
            libraries[top_package(module)] = None
        if not module.startswith(_PLUMBING):
            label = f"{module}.{frame.f_code.co_qualname}"
    return (
        domain,
        tuple(lib for lib in libraries if not lib.startswith(_PLUMBING)),
        label,
    )


def _timed(
    jobs: deque[JobRecord],
    owner: Owner,
    label: str,
    fn: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    cpu = time.thread_time_ns()
    wall = time.perf_counter_ns()
    try:
        return fn(*args, **kwargs)
    finally:
        jobs.append(
            JobRecord(
                owner,
                label,
                time.thread_time_ns() - cpu,
                time.perf_counter_ns() - wall,
            )
        )


def _task_cpu_ns(tid: int) -> int | None:
    """CPU time of a thread: schedstat in ns, else stat in clock ticks."""
    task = Path(f"/proc/self/task/{tid}")
    try:
        return int((task / "schedstat").read_bytes().split(None, 1)[0])
    except OSError, ValueError, IndexError:
        pass
    try:
        fields = (task / "stat").read_bytes().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) * _TICK_NS
    except OSError, ValueError, IndexError:
        return None


def _task_name(tid: int) -> str:
    try:
        name = Path(f"/proc/self/task/{tid}/comm").read_text(encoding="utf-8")
    except OSError:
        return str(tid)
    return name.strip() or str(tid)


class LoopWait:
    """Measure the wall time the loop spends waiting in select()."""

    def __init__(self) -> None:
        """Initialize."""
        self.waiting_ns = 0
        self.wrapper_code: CodeType | None = None
        self._selector: Any = None
        self._hook: Callable[..., Any] | None = None

    def install(self, loop: Any) -> bool:
        """Wrap the loop's selector; return whether it worked."""
        selector = getattr(loop, "_selector", None)
        original = getattr(selector, "select", None)
        if selector is None or original is None:
            return False
        perf = time.perf_counter_ns

        def select(timeout: float | None = None) -> Any:
            start = perf()
            try:
                return original(timeout)
            finally:
                self.waiting_ns += perf() - start

        selector.select = select
        self._selector = selector
        self._hook = select
        self.wrapper_code = select.__code__
        return True

    def uninstall(self) -> None:
        """Remove the wrapper if it is still in place."""
        if self._selector is not None and vars(self._selector).get("select") is (
            self._hook
        ):
            del self._selector.select
        self._selector = None
        self._hook = None
