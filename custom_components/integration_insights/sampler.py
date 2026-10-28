"""Stack sampler on the event loop thread's CPU clock (Linux, 64-bit).

A POSIX timer on the loop thread's CPU clock sends SIGPROF to that thread after
every interval of CPU it uses, so an idle loop gets almost no signals. The
handler walks the interrupted stack and writes one record into a fixed ring.
Each record carries the loop thread CPU used since the previous record, which
is its weight: a long C call that holds the GIL still gets its full share.
"""

from array import array
import atexit
from collections.abc import Callable
from contextvars import ContextVar
import ctypes
import fcntl
import os
import random
import signal
import sys
import termios
import threading
import time
from types import FrameType
from typing import Any, NamedTuple

from .attribution import (
    K_CORE,
    K_LIB,
    K_PLATFORM,
    classify_file,
    module_domain,
    platform_domains,
)

SIGNAL = signal.SIGPROF
SIGEV_THREAD_ID = 4  # include/uapi/asm-generic/siginfo.h
MAX_DEPTH = 256
MAX_RECORDED_FRAMES = 32
MAX_LIBRARY_FRAMES = 6
MAX_LIBRARIES = 4
# Every signal writes one byte to asyncio's wakeup socket, which the loop only
# reads between callbacks. A long block would fill it (about 280 bytes) and
# asyncio would then miss a real SIGTERM, so back off well before that.
BACKOFF_PENDING_BYTES = 32
BACKOFF_SAMPLES = 32  # without a wakeup socket to look at
BACKOFF_INTERVAL_NS = 1_000_000_000


class SamplerError(Exception):
    """The sampler cannot run here."""


class Record(NamedTuple):
    """One sample.

    frames is flat: code, lasti, code, lasti, ... innermost first. It holds the
    innermost frame, the innermost library frames and the integration frames.
    A code of None marks frames left out.
    """

    frames: tuple[Any, ...]
    self_index: int  # frame pair index of the innermost frame of `domain`, or -1
    domain: str  # "idle", "core" or an integration domain
    trigger: str | None  # outermost integration domain on the stack
    libraries: tuple[str, ...]  # third party packages on the stack, innermost first
    entry_id: str | None
    entry_domain: str | None
    cpu_ns: int
    entity_id: str | None = None  # innermost entity whose method is on the stack


class _sigevent(ctypes.Structure):
    # Same 64 byte layout for glibc and musl on x86_64 and aarch64:
    # sigval at 0, signo at 8, notify at 12, union with the thread id at 16.
    _fields_ = [
        ("sigev_value", ctypes.c_void_p),
        ("sigev_signo", ctypes.c_int),
        ("sigev_notify", ctypes.c_int),
        ("sigev_notify_thread_id", ctypes.c_int),
        ("_pad", ctypes.c_int * 11),
    ]


class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _itimerspec(ctypes.Structure):
    _fields_ = [("it_interval", _timespec), ("it_value", _timespec)]


def _load_libc() -> ctypes.CDLL | None:
    if not sys.platform.startswith("linux") or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    if ctypes.sizeof(_sigevent) != 64 or _sigevent.sigev_notify_thread_id.offset != 16:
        return None
    for name in (None, "libc.so.6", "librt.so.1"):
        try:
            lib = ctypes.CDLL(name, use_errno=True)
            create = lib.timer_create
        except OSError, AttributeError:
            continue
        create.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(_sigevent),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        create.restype = ctypes.c_int
        lib.timer_settime.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(_itimerspec),
            ctypes.c_void_p,
        ]
        lib.timer_settime.restype = ctypes.c_int
        lib.timer_delete.argtypes = [ctypes.c_void_p]
        lib.timer_delete.restype = ctypes.c_int
        return lib
    return None


_libc = _load_libc()


def is_supported() -> bool:
    """Return whether this system can run the sampler."""
    return _libc is not None and hasattr(time, "pthread_getcpuclockid")


class _CpuClockTimer:
    """Periodic POSIX timer on a thread's CPU clock, signal to that thread only."""

    def __init__(self, thread: threading.Thread) -> None:
        assert _libc is not None
        assert thread.ident is not None and thread.native_id is not None
        sev = _sigevent()
        sev.sigev_signo = SIGNAL
        sev.sigev_notify = SIGEV_THREAD_ID
        sev.sigev_notify_thread_id = thread.native_id
        self._timer_id: ctypes.c_void_p | None = ctypes.c_void_p()
        try:
            clock = time.pthread_getcpuclockid(thread.ident)
        except OSError as err:
            self._timer_id = None
            raise SamplerError(f"pthread_getcpuclockid failed: {err}") from err
        if _libc.timer_create(clock, ctypes.byref(sev), ctypes.byref(self._timer_id)):
            errno = ctypes.get_errno()
            self._timer_id = None
            raise SamplerError(f"timer_create failed: {os.strerror(errno)}")

    def arm(self, interval_ns: int) -> None:
        assert _libc is not None and self._timer_id is not None
        spec = _itimerspec()
        sec, nsec = divmod(interval_ns, 1_000_000_000)
        spec.it_value.tv_sec = spec.it_interval.tv_sec = sec
        spec.it_value.tv_nsec = spec.it_interval.tv_nsec = nsec
        if _libc.timer_settime(self._timer_id, 0, ctypes.byref(spec), None):
            raise SamplerError(
                f"timer_settime failed: {os.strerror(ctypes.get_errno())}"
            )

    def delete(self) -> None:
        if _libc is not None and self._timer_id is not None:
            _libc.timer_delete(self._timer_id)
            self._timer_id = None


class Sampler:
    """Sample the main thread's stack per interval of its CPU time."""

    def __init__(
        self,
        interval_ns: int = 10_000_000,
        ring_size: int = 4096,
        entry_var: ContextVar[Any] | None = None,
        wakeup_fd: int | None = None,
    ) -> None:
        """Initialize the sampler.

        ring_size must be a power of two. wakeup_fd is the read end of the
        loop's wakeup socket, used to back off during long blocks.
        """
        assert ring_size & (ring_size - 1) == 0
        self.interval_ns = interval_ns
        self.entry_var = entry_var
        self.wakeup_fd = wakeup_fd
        self.ring: list[Record | None] = [None] * ring_size
        self.count = 0  # records ever written
        self.lost = 0  # records overwritten before a drain
        self.cost_ns = 0  # loop thread CPU spent in the handler
        self.errors = 0
        self.backoffs = 0
        self.installed = False
        self._timer: _CpuClockTimer | None = None
        self._handler: Callable[[int, FrameType | None], None] | None = None
        self._previous_handler: Any = None
        self._last_cpu_ns = 0
        self._in_handler = False
        self._backed_off = False
        self._since_drain = 0
        # Code of a wrapper around the selector's select(), still idle time.
        self.select_wrapper: Any = None
        self._platforms = platform_domains()
        self._file_cache: dict[str, tuple[int, str | None]] = {}
        self._module_cache: dict[str, str | None] = {}

    def install(self) -> None:
        """Start sampling. Must run in the main thread, which runs the loop."""
        if not is_supported():
            raise SamplerError("needs Linux with timer_create")
        if threading.current_thread() is not threading.main_thread():
            raise SamplerError("the event loop does not run in the main thread")
        previous = signal.getsignal(SIGNAL)
        if previous not in (signal.SIG_DFL, signal.SIG_IGN):
            raise SamplerError("SIGPROF is already used by something else")
        if signal.getitimer(signal.ITIMER_PROF) != (0.0, 0.0):
            raise SamplerError("a profiling timer is already running")
        self._timer = _CpuClockTimer(threading.main_thread())
        self._previous_handler = previous
        self._last_cpu_ns = time.thread_time_ns()
        self._handler = self._make_handler()
        try:
            signal.signal(SIGNAL, self._handler)
            signal.siginterrupt(SIGNAL, False)
            self._timer.arm(self.interval_ns)
        except (OSError, ValueError, SamplerError) as err:
            self._teardown()
            raise SamplerError(f"could not start: {err}") from err
        # Interpreter shutdown resets SIGPROF to the default action, which
        # would kill the process if the timer still fired.
        atexit.register(self.uninstall)
        self.installed = True

    def rearm(self) -> None:
        """Restore the rate and pick a new random period.

        The random period keeps samples from locking onto periodic work.
        Raises SamplerError.
        """
        if self._timer is None:
            return
        if self._backed_off and self._wakeup_pending() >= BACKOFF_PENDING_BYTES:
            return
        self._backed_off = False
        self._since_drain = 0
        self._timer.arm(int(self.interval_ns * random.uniform(0.75, 1.25)))

    def loop_drained(self) -> None:
        """Restore the rate once the loop has read its wakeup socket.

        Call from the loop when it reads the socket. While backed off the loop
        sat in one long callback, so the CPU since the last sample still
        belongs to the stack that sample saw.
        """
        if not self._backed_off or self._timer is None or self._in_handler:
            return
        self._in_handler = True
        try:
            cpu = time.thread_time_ns()
            if self.count:
                mask = len(self.ring) - 1
                last = self.ring[(self.count - 1) & mask]
                if last is not None:
                    self.ring[self.count & mask] = last._replace(
                        cpu_ns=cpu - self._last_cpu_ns
                    )
                    self.count += 1
            self._last_cpu_ns = cpu
            self._backed_off = False
            self._since_drain = 0
            self._timer.arm(self.interval_ns)
        except SamplerError:
            self.errors += 1
        finally:
            self._in_handler = False

    def uninstall(self) -> None:
        """Stop sampling and restore the previous handler."""
        if self.installed:
            atexit.unregister(self.uninstall)
            self._teardown()
            self.installed = False

    def _teardown(self) -> None:
        # The signal goes to this thread only, so after timer_delete none can
        # still be in flight for another thread.
        if self._timer is not None:
            self._timer.delete()
            self._timer = None
        if self._handler is not None and signal.getsignal(SIGNAL) is self._handler:
            signal.signal(SIGNAL, self._previous_handler)
        self._handler = None

    def drain(self, since: int) -> tuple[list[Record], int]:
        """Return the records written after `since` and the new position."""
        while True:
            end = self.count
            ring = self.ring[:]  # one C call, a handler can't run inside it
            if self.count == end:
                break
        size = len(ring)
        start = max(since, end - size)
        self.lost += start - since
        mask = size - 1
        records = [ring[i & mask] for i in range(start, end)]
        return [r for r in records if r is not None], end

    def _wakeup_pending(self) -> int:
        if self.wakeup_fd is None:
            return 0
        buf = array("i", [0])
        try:
            fcntl.ioctl(self.wakeup_fd, termios.FIONREAD, buf, True)
        except OSError:
            return 0
        return buf[0]

    def _make_handler(self) -> Callable[[int, FrameType | None], None]:
        thread_time_ns = time.thread_time_ns
        walk = self.walk
        ring = self.ring
        mask = len(ring) - 1
        wakeup_fd = self.wakeup_fd
        ioctl = fcntl.ioctl
        fionread = termios.FIONREAD
        pending = array("i", [0])

        def handler(signum: int, frame: FrameType | None) -> None:
            if self._in_handler:
                # A collection inside walk outlasted an interval. Skip; the
                # CPU goes into the next sample's weight.
                return
            self._in_handler = True
            try:
                cpu = thread_time_ns()
                weight = cpu - self._last_cpu_ns
                self._last_cpu_ns = cpu
                record = walk(frame, weight)
                index = self.count
                ring[index & mask] = record
                self.count = index + 1
                if not self._backed_off and self._timer is not None:
                    if wakeup_fd is not None:
                        ioctl(wakeup_fd, fionread, pending, True)
                        full = pending[0] >= BACKOFF_PENDING_BYTES
                    else:
                        self._since_drain += 1
                        full = self._since_drain >= BACKOFF_SAMPLES
                    if full:
                        self._backed_off = True
                        self.backoffs += 1
                        self._timer.arm(BACKOFF_INTERVAL_NS)
                self.cost_ns += thread_time_ns() - cpu
            except Exception:  # noqa: BLE001
                # Never raise into whatever code the signal interrupted.
                self.errors += 1
            finally:
                self._in_handler = False

        return handler

    def walk(self, frame: FrameType | None, cpu_ns: int) -> Record:
        """Classify a stack, innermost frame first."""
        file_cache = self._file_cache
        frames: list[Any] = []
        domain: str | None = None
        self_index = -1
        first_platform: str | None = None
        first_platform_index = -1
        trigger: str | None = None
        libraries: list[str] = []
        library_frames = 0
        skipped = False
        idle = False
        entity_id: str | None = None
        if frame is not None and frame.f_code.co_name == "select":
            caller = frame.f_back
            if caller is not None and caller.f_code is self.select_wrapper:
                caller = caller.f_back
            idle = (
                frame.f_code.co_filename.endswith("selectors.py")
                and caller is not None
                and caller.f_code.co_name == "_run_once"
            )
        f = frame
        depth = 0
        while f is not None and depth < MAX_DEPTH:
            depth += 1
            code = f.f_code
            info = file_cache.get(code.co_filename)
            if info is None:
                info = file_cache[code.co_filename] = classify_file(code.co_filename)
            kind, name = info
            if kind == K_CORE:
                if not frames:
                    frames += (code, f.f_lasti)
                f = f.f_back
                continue
            if kind == K_LIB:
                if name not in libraries and len(libraries) < MAX_LIBRARIES:
                    libraries.append(name)  # type: ignore[arg-type]
                if library_frames < MAX_LIBRARY_FRAMES and len(frames) < 2 * (
                    MAX_RECORDED_FRAMES
                ):
                    library_frames += 1
                    if skipped:
                        frames += (None, 0)
                        skipped = False
                    frames += (code, f.f_lasti)
                else:
                    skipped = True
                f = f.f_back
                continue
            is_platform = kind == K_PLATFORM
            if (
                (is_platform or entity_id is None)
                and code.co_argcount
                and code.co_varnames[0] == "self"
            ):
                obj = f.f_locals.get("self")
                if is_platform and obj is not None:
                    # Platform base classes run on behalf of the integration
                    # that owns the entity: charge hue, not light.
                    owner = self._owner_domain(obj)
                    if owner is not None:
                        name = owner
                        is_platform = False
                if entity_id is None and obj is not None:
                    entity_id = _entity_id(obj)
            # The frame to blame is always kept, even past the cap.
            recorded = len(frames) < 2 * MAX_RECORDED_FRAMES or (
                name is not None and not is_platform and domain is None
            )
            if recorded:
                if skipped:
                    frames += (None, 0)
                    skipped = False
                frames += (code, f.f_lasti)
            else:
                skipped = True
            if name is not None:
                trigger = name
                pair = len(frames) // 2 - 1 if recorded else -1
                if is_platform:
                    if first_platform is None:
                        first_platform = name
                        first_platform_index = pair
                elif domain is None:
                    domain = name
                    self_index = pair
            f = f.f_back
        if idle:
            # Wrappers around select() must not get the blame for waiting.
            domain = "idle"
            self_index = -1
        elif domain is None:
            if first_platform is not None:
                domain = first_platform
                self_index = first_platform_index
            else:
                domain = "core"
        entry_id = entry_domain = None
        if self.entry_var is not None and (entry := self.entry_var.get(None)):
            entry_id = getattr(entry, "entry_id", None)
            entry_domain = getattr(entry, "domain", None)
        return Record(
            tuple(frames),
            self_index,
            domain,
            trigger,
            tuple(libraries),
            entry_id,
            entry_domain,
            cpu_ns,
            entity_id,
        )

    def _owner_domain(self, owner: object) -> str | None:
        module = type(owner).__module__
        cache = self._module_cache
        if module not in cache:
            cache[module] = module_domain(module)
        return cache[module]


def _entity_id(obj: object) -> str | None:
    """Return the entity id of an entity object, without running any code.

    Reading the instance dict skips properties and __getattr__, which must not
    run inside the signal handler.
    """
    try:
        value = object.__getattribute__(obj, "__dict__").get("entity_id")
    except AttributeError:
        return None
    return value if type(value) is str else None
