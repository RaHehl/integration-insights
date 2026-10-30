"""Tests for the sampler (the real ones need Linux)."""

import asyncio
from collections import Counter
import os
from pathlib import Path
import signal
import subprocess
import sys
from unittest.mock import patch

import pytest

from custom_components.integration_insights import sampler as sampler_module
from custom_components.integration_insights.collector import Collector
from custom_components.integration_insights.sampler import (
    Sampler,
    SamplerError,
    is_supported,
)

from .helpers import compile_at, spin_at

linux_only = pytest.mark.skipif(not is_supported(), reason="needs timer_create")

LIGHT_BASE = """\
class LightBase:
    def heavy(self, seconds):
        import time
        end = time.thread_time() + seconds
        while time.thread_time() < end:
            pass
"""


def _timers() -> int:
    lines = Path("/proc/self/timers").read_text().splitlines()
    return sum(line.startswith("ID:") for line in lines)


def _weights(sampler: Sampler) -> Counter[str]:
    records, _ = sampler.drain(0)
    weights: Counter[str] = Counter()
    for record in records:
        weights[record.domain] += record.cpu_ns
    return weights


def test_install_refuses_unsupported_system() -> None:
    """Without timer_create the sampler does not start."""
    with (
        patch.object(sampler_module, "is_supported", return_value=False),
        pytest.raises(SamplerError),
    ):
        Sampler().install()


@linux_only
def test_charges_cpu_to_the_running_integration() -> None:
    """Loop thread CPU lands on the integration whose code runs."""
    spin = spin_at("/config/custom_components/busy/coordinator.py")
    timers = _timers()
    sampler = Sampler(interval_ns=1_000_000)
    sampler.install()
    try:
        assert _timers() == timers + 1
        spin(0.3)
    finally:
        sampler.uninstall()

    assert _timers() == timers
    assert signal.getsignal(signal.SIGPROF) is signal.SIG_DFL
    assert sampler.errors == 0
    weights = _weights(sampler)
    assert weights["busy"] > 0.8 * sum(weights.values())

    collector = Collector()
    collector.add(sampler.drain(0)[0], now=60.0)
    busy = collector.report(now=120.0, started=60.0)["integrations"][0]
    assert busy["domain"] == "busy"
    assert busy["hot_spots"][0]["function"] == (
        "custom_components/busy/coordinator.py spin"
    )


@linux_only
def test_platform_base_class_is_charged_to_the_owner() -> None:
    """Code of the light base class counts for the integration of the entity."""
    base = compile_at(
        "/usr/src/homeassistant/homeassistant/components/light/__init__.py",
        LIGHT_BASE,
    )["LightBase"]
    entity = type("FooLight", (base,), {"__module__": "custom_components.foo.light"})
    sampler = Sampler(interval_ns=1_000_000)
    sampler.install()
    try:
        entity().heavy(0.2)
    finally:
        sampler.uninstall()

    weights = _weights(sampler)
    assert weights["foo"] > 0.8 * sum(weights.values())
    assert weights["light"] == 0


@linux_only
def test_refuses_when_sigprof_is_taken() -> None:
    """Another SIGPROF user keeps its handler."""

    def other(signum: int, frame: object) -> None:
        """Belong to someone else."""

    previous = signal.signal(signal.SIGPROF, other)
    try:
        with pytest.raises(SamplerError):
            Sampler().install()
        assert signal.getsignal(signal.SIGPROF) is other
    finally:
        signal.signal(signal.SIGPROF, previous)


@linux_only
def test_ring_keeps_the_latest_records() -> None:
    """A full ring drops the oldest records."""
    sampler = Sampler(interval_ns=1_000_000, ring_size=8)
    sampler.install()
    try:
        spin_at("/config/custom_components/busy/x.py")(0.1)
    finally:
        sampler.uninstall()

    assert sampler.count > 8
    records, position = sampler.drain(0)
    assert len(records) == 8
    assert position == sampler.count


@linux_only
def test_long_block_keeps_asyncio_signals_working(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A long CPU block must not fill asyncio's wakeup socket and lose SIGUSR1."""
    loop = asyncio.new_event_loop()
    received: list[bool] = []
    loop.add_signal_handler(signal.SIGUSR1, lambda: received.append(True))
    sampler = Sampler(interval_ns=1_000_000)
    spin = spin_at("/config/custom_components/busy/coordinator.py")

    def block() -> None:
        spin(0.8)  # 800 signals at 1 ms would overflow the socket
        os.kill(os.getpid(), signal.SIGUSR1)
        spin(0.2)

    async def main() -> None:
        loop.call_soon(block)
        await asyncio.sleep(0.1)

    _hook_drain(loop, sampler)
    sampler.install()
    try:
        loop.run_until_complete(main())
    finally:
        sampler.uninstall()
        loop.remove_signal_handler(signal.SIGUSR1)
        loop.close()

    assert received == [True]
    assert sampler.backoffs == 1
    assert "wakeup fd" not in capfd.readouterr().err
    assert not sampler._backed_off  # noqa: SLF001
    # The second of CPU in block() lands on busy, except for the tail after
    # the last backed off sample, at most 100 ms.
    weights = _weights(sampler)
    assert 0.85e9 < weights["busy"] < 1.1e9


def _hook_drain(loop: asyncio.AbstractEventLoop, sampler: Sampler) -> None:
    original = loop._process_self_data  # type: ignore[attr-defined]  # noqa: SLF001

    def process_self_data(data: bytes) -> None:
        sampler.loop_drained()
        original(data)

    loop._process_self_data = process_self_data  # type: ignore[attr-defined]  # noqa: SLF001


@linux_only
def test_backed_off_sampler_still_tells_callbacks_apart() -> None:
    """Two long callbacks in one loop iteration each get their own CPU."""
    loop = asyncio.new_event_loop()
    sampler = Sampler(interval_ns=1_000_000)
    first = spin_at("/config/custom_components/aaa/__init__.py")
    second = spin_at("/config/custom_components/bbb/__init__.py")

    async def main() -> None:
        loop.call_soon(first, 0.5)
        loop.call_soon(second, 0.5)
        await asyncio.sleep(0.1)

    _hook_drain(loop, sampler)
    sampler.install()
    try:
        loop.run_until_complete(main())
    finally:
        sampler.uninstall()
        loop.close()

    assert sampler.backoffs == 1
    weights = _weights(sampler)
    assert 0.35e9 < weights["aaa"] < 0.65e9
    assert 0.35e9 < weights["bbb"] < 0.65e9


@linux_only
def test_handler_cost_is_not_part_of_the_samples() -> None:
    """The handler's own CPU is counted as cost, not charged to whoever runs."""
    spin = spin_at("/config/custom_components/busy/x.py")
    sampler = Sampler()  # 10 ms: no back off without a loop to drain it
    sampler.install()
    try:
        spin(0.2)
    finally:
        sampler.uninstall()
    assert sampler.cost_ns > 0
    weights = _weights(sampler)
    # spin() measures thread time, which includes the handler's: the samples
    # and the cost together make the 200 ms, less the tail after the last
    # sample, not the samples alone.
    assert 0.18e9 < weights["busy"] + sampler.cost_ns < 0.205e9


@linux_only
def test_exit_with_running_sampler_is_clean() -> None:
    """Interpreter exit while sampling must not die of SIGPROF."""
    code = (
        "from custom_components.integration_insights.sampler import Sampler\n"
        "import time\n"
        "Sampler(interval_ns=1_000_000).install()\n"
        "end = time.thread_time() + 0.2\n"
        "while time.thread_time() < end: pass\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@linux_only
def test_uninstall_leaves_a_later_handler_alone() -> None:
    """If someone replaced our handler, uninstall keeps theirs."""

    def other(signum: int, frame: object) -> None:
        """Belong to someone else."""

    sampler = Sampler()
    sampler.install()
    signal.signal(signal.SIGPROF, other)
    try:
        sampler.uninstall()
        assert signal.getsignal(signal.SIGPROF) is other
    finally:
        signal.signal(signal.SIGPROF, signal.SIG_DFL)


@linux_only
def test_refuses_when_a_profiling_timer_runs() -> None:
    """An armed ITIMER_PROF belongs to another profiler."""
    signal.signal(signal.SIGPROF, signal.SIG_IGN)
    signal.setitimer(signal.ITIMER_PROF, 10)
    try:
        with pytest.raises(SamplerError):
            Sampler().install()
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, signal.SIG_DFL)
