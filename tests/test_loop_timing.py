"""Tests for the callback timing and its watchdog."""

import asyncio
from asyncio import events
import time
from types import FrameType
from unittest.mock import patch

from custom_components.integration_insights import loop_timing
from custom_components.integration_insights.loop_timing import LoopTiming


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
