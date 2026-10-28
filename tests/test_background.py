"""Tests for executor job and thread CPU."""

from concurrent.futures import ThreadPoolExecutor
import functools
import threading
import time

import pytest

from custom_components.integration_insights.background import (
    Background,
    LoopWait,
    Owner,
    callable_owner,
)
from custom_components.integration_insights.collector import Collector
from custom_components.integration_insights.sampler import Sampler, is_supported

from .helpers import SPIN, compile_at

BUSY = "/config/custom_components/busy/worker.py"
LIB = "/usr/local/lib/python3.14/site-packages/aiofoo/client.py"

linux_only = pytest.mark.skipif(not is_supported(), reason="needs /proc")


def _module(filename: str, name: str, source: str = SPIN) -> dict:
    namespace = {"__name__": name}
    exec(compile(source, filename, "exec"), namespace)  # noqa: S102
    return namespace


def _background(library_map: dict[str, str] | None = None) -> Background:
    return Background(Sampler().walk, lambda: library_map or {})


def test_callable_owner_follows_module_class_and_partial() -> None:
    """Functions, bound methods and partials map to the integration."""
    spin = _module(BUSY, "custom_components.busy.worker")["spin"]
    assert callable_owner(spin)[0] == "busy"
    assert callable_owner(functools.partial(spin, 1))[0] == "busy"

    entity_cls = _module(
        "/usr/src/homeassistant/homeassistant/components/hue/light.py",
        "homeassistant.components.hue.light",
        "class HueLight:\n    def update(self):\n        pass\n",
    )["HueLight"]
    domain, _libraries, label = callable_owner(entity_cls().update)
    assert domain == "hue"
    assert label == "homeassistant.components.hue.light.HueLight.update"

    domain, libraries, _label = callable_owner(time.sleep)
    assert domain is None
    assert libraries == ("time",)


def test_resolve_uses_library_map_then_caller() -> None:
    """A library callable goes to its only user, else to the calling code."""
    fetch = _module(LIB, "aiofoo.client", "def fetch():\n    return 1\n")["fetch"]
    owner, _label = _background({"aiofoo": "foo"}).resolve(fetch, None)
    assert owner == Owner("foo", ("aiofoo",))

    background = _background()
    caller = compile_at(
        BUSY,
        "def call(background, fn):\n    import sys\n"
        "    return background.resolve(fn, sys._getframe())\n",
    )["call"]
    owner, _label = caller(background, time.sleep)
    assert owner.domain == "busy"


def test_executor_jobs_are_timed_and_unhooked() -> None:
    """Jobs carry their CPU and wall time; unhooking restores submit."""
    spin = _module(BUSY, "custom_components.busy.worker")["spin"]
    background = _background()
    with ThreadPoolExecutor(max_workers=1) as executor:
        background.hook_executor(executor)
        executor.submit(spin, 0.05).result()
        executor.submit(time.sleep, 0.05).result()
        background.unhook_executors()
        assert "submit" not in vars(executor)
        executor.submit(spin, 0.01).result()
    jobs = background.drain_jobs()
    assert background.jobs_total == 2
    assert [job.owner.domain for job in jobs] == ["busy", "core"]
    assert jobs[0].cpu_ns >= 45_000_000
    assert jobs[1].cpu_ns < 20_000_000 <= jobs[1].wall_ns
    assert background.drain_jobs() == []


def test_job_exception_is_kept() -> None:
    """A failing job raises as before and is still recorded."""
    background = _background()
    with ThreadPoolExecutor(max_workers=1) as executor:
        background.hook_executor(executor)
        future = executor.submit(int, "x")
        with pytest.raises(ValueError):
            future.result()
    assert len(background.drain_jobs()) == 1


@linux_only
def test_threads_are_charged_by_target_and_stack() -> None:
    """A thread's CPU goes to its target's integration, else to its stack."""
    stop = threading.Event()
    owned = _module(
        BUSY,
        "custom_components.busy.worker",
        "def run(stop):\n    while not stop.is_set():\n        pass\n",
    )["run"]
    anonymous = compile_at(
        "/config/custom_components/other/loop.py",
        "def run(stop):\n    while not stop.is_set():\n        pass\n",
    )["run"]
    background = _background()
    background.poll_threads(set())  # baseline
    threads = [
        threading.Thread(target=owned, args=(stop,), name="busy-worker"),
        threading.Thread(target=anonymous, args=(stop,), name="other-loop"),
    ]
    for thread in threads:
        thread.start()
    try:
        time.sleep(0.3)
        records = background.poll_threads({threading.main_thread().native_id})
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    by_name = {record.name: record for record in records}
    assert by_name["busy-worker"].owner.domain == "busy"
    assert by_name["other-loop"].owner.domain == "other"
    assert by_name["busy-worker"].cpu_ns > 10_000_000
    assert threading.main_thread().name not in by_name


@linux_only
def test_executor_workers_are_skipped() -> None:
    """Executor workers are covered by their jobs, not as threads."""
    background = _background()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="Pool") as executor:
        executor.submit(int).result()
        skip = background.executor_threads([executor])
        assert len(skip) == 1
        background.poll_threads(skip)
        executor.submit(compile_at(BUSY)["spin"], 0.05).result()
        records = background.poll_threads(skip)
    assert not [r for r in records if r.name.startswith("Pool")]


def test_loop_wait_measures_select() -> None:
    """Time in select() is counted; uninstall removes the wrapper."""

    class Selector:
        def select(self, timeout=None):
            time.sleep(timeout)
            return []

    selector = Selector()
    loop = type("Loop", (), {"_selector": selector})()
    wait = LoopWait()
    assert wait.install(loop)
    selector.select(0.02)
    assert wait.waiting_ns >= 20_000_000
    assert wait.wrapper_code is not None
    wait.uninstall()
    assert "select" not in vars(selector)
    assert not LoopWait().install(object())


def test_collector_reports_background_cpu() -> None:
    """Jobs and threads show up per integration and in the totals."""
    from custom_components.integration_insights.background import (  # noqa: PLC0415
        JobRecord,
        ThreadRecord,
    )

    ms = 1_000_000
    collector = Collector()
    collector.library_map = {"aiofoo": "foo"}
    now = 600.0
    collector.add_jobs(
        [
            JobRecord(Owner("busy", ()), "mod.fetch", 30 * ms, 90 * ms),
            JobRecord(Owner("busy", ()), "mod.fetch", 10 * ms, 10 * ms),
            JobRecord(Owner("core", ("aiofoo",)), "aiofoo.get", 5 * ms, 5 * ms),
        ],
        now,
    )
    collector.add_threads(
        [ThreadRecord(Owner("busy", ()), "busy-thread", 20 * ms)], now
    )
    collector.add_loop_busy(70 * ms, now)
    report = collector.report(now + 60, 0.0, minutes=2)
    assert report["executor_cpu_ms_per_min"] == 45.0
    assert report["thread_cpu_ms_per_min"] == 20.0
    assert report["loop_busy_ms_per_min"] == 70.0
    busy, foo = report["integrations"]
    assert busy["domain"] == "busy"
    assert busy["cpu_ms_per_min"] == 60.0
    assert busy["background_cpu_ms_per_min"] == 60.0
    assert busy["executor"]["wall_ms_per_min"] == 100.0
    assert busy["executor"]["jobs"] == [
        {"function": "mod.fetch", "cpu_ms_per_min": 40.0, "per_min": 2.0}
    ]
    assert busy["threads"]["by_name"] == {"busy-thread": 20.0}
    assert foo["domain"] == "foo"
    assert "threads" not in foo
