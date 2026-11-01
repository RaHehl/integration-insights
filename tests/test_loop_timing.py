"""Tests for the callback timing and its watchdog."""

import asyncio
from asyncio import events
import contextvars
import functools
import time
from types import FrameType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.integration_insights import loop_timing
from custom_components.integration_insights.loop_timing import LoopTiming
from homeassistant.config_entries import current_entry
from homeassistant.core import HassJob


def _innermost(frame: FrameType | None) -> tuple[str, str] | None:
    """Name the function the loop thread runs, like the real blocker would."""
    return ("foo", frame.f_code.co_name) if frame is not None else None


def culprit() -> None:
    """Wait without yielding."""
    time.sleep(0.3)


def innocent() -> None:
    """Be the callback, while the waiting is in culprit()."""
    culprit()


def test_watchdog_names_the_code_that_runs_not_the_callback() -> None:
    """A long callback is named by what the loop thread runs during it."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop, _innermost)
    with (
        patch.object(loop_timing, "BLOCK_NS", 100_000_000),
        patch.object(loop_timing, "WATCHDOG_INTERVAL", 0.01),
    ):
        try:
            loop.call_soon(timing.start)
            loop.run_until_complete(asyncio.sleep(0))
            loop.call_soon(innocent)
            # The wrapper measures its own cost on every 32nd callback
            for _ in range(40):
                loop.call_soon(lambda: None)
            loop.run_until_complete(asyncio.sleep(0.01))
        finally:
            loop.call_soon(timing.stop)
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()
    blocks = timing.drain_blocks()
    assert len(blocks) == 1
    assert blocks[0].callback.endswith("innocent")
    assert blocks[0].blocker == ("foo", "culprit")
    assert blocks[0].duration_ns >= 300_000_000
    assert timing.drain_blocks() == []
    assert events.Handle._run.__qualname__ == "Handle._run"  # noqa: SLF001
    assert timing.overhead_ns > 0


def test_stopped_wrapper_below_another_one_only_passes_calls_on() -> None:
    """Another tool wrapped the wrapper: after stop, nothing is recorded any more."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    original = events.Handle._run  # noqa: SLF001
    try:
        timing.start()
        ours = events.Handle._run  # noqa: SLF001

        def other(handle: events.Handle) -> None:
            ours(handle)

        events.Handle._run = other  # type: ignore[method-assign, assignment]  # noqa: SLF001
        timing.stop()
        # Not ours on top: the other wrapper stays in place
        assert events.Handle._run is other  # noqa: SLF001
        ran: list[int] = []
        loop.call_soon(ran.append, 1)
        loop.run_until_complete(asyncio.sleep(0))
        assert ran == [1]
        assert timing.drain() == []
    finally:
        events.Handle._run = original  # type: ignore[method-assign]  # noqa: SLF001
        loop.close()


def test_callbacks_of_another_loop_are_not_recorded() -> None:
    """Only the loop the timing was made for counts."""
    loop = asyncio.new_event_loop()
    other = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    try:
        timing.start()
        other.call_soon(lambda: None)
        other.run_until_complete(asyncio.sleep(0))
        assert timing.drain() == []
        loop.call_soon(lambda: None)
        loop.run_until_complete(asyncio.sleep(0))
        assert timing.drain()
    finally:
        timing.stop()
        loop.close()
        other.close()


class _Worker:
    """An object whose method and coroutine run on the loop."""

    def __init__(self) -> None:
        self.calls = 0

    def tick(self) -> None:
        self.calls += 1

    async def work(self, steps: int) -> None:
        for _ in range(steps):
            await asyncio.sleep(0)


def _records(timing: LoopTiming) -> dict[tuple[str, str | None], int]:
    return {(r.label, r.entry_id): r.calls for r in timing.drain()}


def test_task_steps_and_methods_are_resolved_once_and_counted_after_a_drain() -> None:
    """The cached rows count every call and are dropped with the drain."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    worker = _Worker()
    entry = SimpleNamespace(entry_id="e1")
    context = contextvars.copy_context()
    context.run(current_entry.set, entry)
    try:
        timing.start()
        loop.run_until_complete(worker.work(3))
        for _ in range(4):
            loop.call_soon(worker.tick)
        loop.call_soon(worker.tick, context=context)
        loop.run_until_complete(asyncio.sleep(0))
        records = _records(timing)
        work, tick = f"{__name__}._Worker.work", f"{__name__}._Worker.tick"
        assert records[(work, None)] == 4
        assert records[(tick, None)] == 4
        # The same method in an entry's context is its own row
        assert records[(tick, "e1")] == 1
        # After a drain the next calls are counted again, not lost in stale rows
        loop.run_until_complete(worker.work(2))
        loop.call_soon(worker.tick)
        loop.run_until_complete(asyncio.sleep(0))
        records = _records(timing)
        assert records[(work, None)] == 3
        assert records[(tick, None)] == 1
        assert timing.errors == 0
    finally:
        timing.stop()
        loop.close()


def _plain() -> None:
    pass


def _runner(job: HassJob) -> None:
    job.target()


def test_functions_are_cached_and_a_job_argument_is_not() -> None:
    """A function is one row; a HassJob passed as argument names the real callback."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    worker = _Worker()
    try:
        timing.start()
        for _ in range(3):
            loop.call_soon(_plain)
        loop.call_soon(_runner, HassJob(worker.tick))
        loop.call_soon(_runner, HassJob(_plain))
        loop.run_until_complete(asyncio.sleep(0))
        records = _records(timing)
        assert records[(f"{__name__}._plain", None)] == 4
        assert records[(f"{__name__}._Worker.tick", None)] == 1
        assert timing.errors == 0
    finally:
        timing.stop()
        loop.close()


def test_one_shot_helpers_are_resolved_by_their_job() -> None:
    """A new timer object per event is keyed by the job it runs, not resolved anew."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    worker = _Worker()

    class FakeTimer:
        __module__ = "homeassistant.helpers.fake"

        def __init__(self, job: HassJob) -> None:
            self.job = job

        def run(self) -> None:
            self.job.target()

    try:
        timing.start()
        loop.run_until_complete(asyncio.sleep(0))
        before = timing.resolves
        job = HassJob(worker.tick)
        for _ in range(6):
            loop.call_soon(FakeTimer(job).run)
        loop.run_until_complete(asyncio.sleep(0))
        records = _records(timing)
        assert records[(f"{__name__}._Worker.tick", None)] == 6
        # The first timer taught the class, the second filled the job's row,
        # the sleep is a new coroutine; the other four timers hit the cache
        assert timing.resolves - before <= 3
        assert timing.errors == 0
    finally:
        timing.stop()
        loop.close()


def test_one_shot_objects_and_c_functions_hit_by_class() -> None:
    """A future's set_result and a list's append are one row per class, resolved once."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    try:
        timing.start()
        loop.run_until_complete(asyncio.sleep(0))
        before = timing.resolves
        for i in range(6):
            future = loop.create_future()
            loop.call_soon(future.set_result, i)
            loop.call_soon([].append, i)
        loop.run_until_complete(asyncio.sleep(0))
        calls = {label: calls for (label, _entry), calls in _records(timing).items()}
        assert calls["_asyncio.Future.set_result"] == 6
        assert calls["builtins.list.append"] == 6
        # One resolve per class, one for the sleep coroutine
        assert timing.resolves - before <= 3
        assert timing.errors == 0
    finally:
        timing.stop()
        loop.close()


def test_a_callable_helper_without_hash_cannot_crash_the_loop() -> None:
    """Home Assistant schedules some helper objects themselves; one that cannot be hashed must still run."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    worker = _Worker()

    class SelfScheduling:
        __module__ = "homeassistant.helpers.fake"
        __hash__ = None  # type: ignore[assignment]

        def __init__(self, job: HassJob) -> None:
            self.job = job

        def __eq__(self, other: object) -> bool:
            return self is other

        def __call__(self) -> None:
            self.job.target()

    try:
        timing.start()
        job = HassJob(worker.tick)
        for _ in range(3):
            loop.call_soon(SelfScheduling(job))
        loop.run_until_complete(asyncio.sleep(0))
        assert worker.calls == 3
        assert timing.errors == 0
        assert _records(timing)[(f"{__name__}._Worker.tick", None)] == 3
    finally:
        timing.stop()
        loop.close()


def test_alike_functions_in_different_modules_keep_their_own_rows() -> None:
    """Code objects compare by value, so the row key must not be the code object."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    source = "def cb():\n    return 1\n"
    first: dict = {"__name__": "custom_components.foo.api"}
    second: dict = {"__name__": "custom_components.bar.api"}
    exec(compile(source, "/config/custom_components/foo/api.py", "exec"), first)  # noqa: S102
    exec(compile(source, "/config/custom_components/bar/api.py", "exec"), second)  # noqa: S102
    assert first["cb"].__code__ == second["cb"].__code__
    try:
        timing.start()
        loop.call_soon(first["cb"])
        loop.call_soon(second["cb"])
        loop.run_until_complete(asyncio.sleep(0))
        records = _records(timing)
        assert records[("custom_components.foo.api.cb", None)] == 1
        assert records[("custom_components.bar.api.cb", None)] == 1
    finally:
        timing.stop()
        loop.close()


def test_interval_jobs_and_reader_partials_are_resolved_once() -> None:
    """A timer passes its HassJob as argument and a reader is one partial: both keyed by identity."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    worker = _Worker()

    def listener(job: HassJob) -> None:
        job.target()

    try:
        timing.start()
        loop.run_until_complete(asyncio.sleep(0))
        before = timing.resolves
        job = HassJob(worker.tick)
        reader = functools.partial(worker.tick)
        for _ in range(5):
            loop.call_soon(listener, job)
            loop.call_soon(reader)
        loop.run_until_complete(asyncio.sleep(0))
        assert worker.calls == 10
        # One resolve each, plus the sleep coroutine
        assert timing.resolves - before <= 3
        assert _records(timing)[(f"{__name__}._Worker.tick", None)] == 10
    finally:
        timing.stop()
        loop.close()


def test_an_unattributable_callback_still_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A callable that breaks when looked at runs, and its time lands under other callbacks."""

    class Prickly:
        # Only the name breaks: asyncio's debug mode looks at other attributes
        def __getattr__(self, name: str) -> Any:
            if name == "__qualname__":
                raise RuntimeError("do not look at me")
            raise AttributeError(name)

        def __call__(self) -> None:
            ran.append(True)

    ran: list[bool] = []
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    try:
        timing.start()
        loop.call_soon(Prickly())
        loop.call_soon(Prickly())
        loop.run_until_complete(asyncio.sleep(0))
        assert ran == [True, True]
        assert timing.errors >= 1
        calls = {label: n for (label, _entry), n in _records(timing).items()}
        assert calls["other callbacks"] == 2
        assert caplog.text.count("Could not attribute a callback") == 1
    finally:
        timing.stop()
        loop.close()


def test_the_warning_does_not_wait_for_the_first_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error the watchdog counted first does not swallow the warning."""

    class Prickly:
        def __getattr__(self, name: str) -> Any:
            if name == "__qualname__":
                raise RuntimeError("do not look at me")
            raise AttributeError(name)

        def __call__(self) -> None:
            pass

    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    try:
        timing.start()
        timing.errors = 1
        loop.call_soon(Prickly())
        loop.run_until_complete(asyncio.sleep(0))
        assert caplog.text.count("Could not attribute a callback") == 1
    finally:
        timing.stop()
        loop.close()


def test_a_full_table_counts_the_rest_under_other_callbacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the cap, new callbacks share one row, are counted and warned about once."""
    loop = asyncio.new_event_loop()
    timing = LoopTiming(loop)
    try:
        with patch.object(loop_timing, "MAX_ROWS", 1):
            timing.start()
            loop.run_until_complete(asyncio.sleep(0))
            loop.call_soon(int)
            loop.call_soon(str)
            loop.run_until_complete(asyncio.sleep(0))
        calls = {label: n for (label, _entry), n in _records(timing).items()}
        assert calls.get("other callbacks", 0) >= 2
        assert timing.overflowed >= 2
        assert caplog.text.count("keeps 1 callbacks apart") == 1
    finally:
        timing.stop()
        loop.close()
