"""Memory snapshots: process figures, Python objects and resources per integration.

A snapshot is taken on request only. Comparing two snapshots shows what grows:
objects of an integration's classes, or Home Assistant resources it registered
and never released, such as event listeners, timers and tasks.
"""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import functools
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any

from .attribution import (
    K_INTEGRATION,
    callable_owner,
    classify_file,
    module_domain,
    owner_domain,
    top_package,
)
from .heap import CODE, DATA, SHARED, UNREACHED, Root, walk

TOP_TYPES = 5
CHUNK = 20_000
_KIB = 1024


@dataclass(frozen=True, slots=True)
class Resources:
    """Home Assistant resources one integration holds."""

    listeners: int = 0
    dispatcher: int = 0
    timers: int = 0
    tasks: int = 0


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Memory at one point in time."""

    taken: datetime
    walk_ms: float
    process: dict[str, float]
    heap: dict[str, dict[str, int]]  # owner -> category -> bytes
    resources: dict[str, Resources]


@dataclass(slots=True)
class _ResourceCount:
    listeners: int = 0
    dispatcher: int = 0
    timers: int = 0
    tasks: int = 0


def _read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":") if ":" in line else line.partition(" ")
        values[key.strip()] = value.strip()
    return values


def read_process() -> dict[str, float]:
    """Read process memory, memory pressure and cgroup figures (does I/O).

    Values in MiB, pressure in percent. What a system does not offer is left out.
    """
    process: dict[str, float] = {}
    try:
        status = _read_key_values(Path("/proc/self/status"))
    except OSError:
        status = {}
    for key, name in (
        ("VmRSS", "rss_mib"),
        ("RssAnon", "rss_anon_mib"),
        ("RssFile", "rss_file_mib"),
        ("VmSwap", "swap_mib"),
    ):
        if key in status:
            process[name] = round(int(status[key].split()[0]) / _KIB, 1)
    try:
        for line in (
            Path("/proc/pressure/memory").read_text(encoding="utf-8").splitlines()
        ):
            kind, _, rest = line.partition(" ")
            fields = dict(item.split("=") for item in rest.split())
            process[f"pressure_{kind}_avg60"] = float(fields["avg60"])
    except OSError, ValueError, KeyError:
        pass
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        path = Path("/sys/fs/cgroup") / cgroup.strip().split("::", 1)[1].lstrip("/")
        process["cgroup_mib"] = round(
            int((path / "memory.current").read_text(encoding="utf-8")) / _KIB**2, 1
        )
        stat = _read_key_values(path / "memory.stat")
        for key in ("anon", "file"):
            if key in stat:
                process[f"cgroup_{key}_mib"] = round(int(stat[key]) / _KIB**2, 1)
    except OSError, ValueError, IndexError:
        pass
    return process


def make_module_owner(library_map: Mapping[str, str]) -> Callable[[str], str]:
    """Return a function that maps a module name to its integration or "core"."""

    def owner(module: str) -> str:
        return owner_domain(module_domain(module), (top_package(module),), library_map)

    return owner


def _is_job(obj: Any) -> bool:
    return type(obj).__name__ == "HassJob"


def _is_callback(obj: Any) -> bool:
    return callable(obj) or _is_job(obj)


def _inner_job(target: Any) -> Any:
    """Return the job a Home Assistant helper object runs, if it holds one.

    One time listeners, timer trackers and their bound methods keep the
    callback as a job attribute.
    """
    for holder in (target, getattr(target, "__self__", None)):
        if holder is None or isinstance(holder, ModuleType):
            continue
        for name in ("listener_job", "job"):
            inner = getattr(holder, name, None)
            if inner is not None and _is_callback(inner):
                return inner
    return None


def make_callable_owner(library_map: Mapping[str, str]) -> Callable[[Any], str]:
    """Return a function that maps a registered callback to its integration.

    Home Assistant wraps callbacks in jobs, one time listeners and partials; the
    callback inside decides.
    """

    def unwrap(target: Any, depth: int = 0) -> str | None:
        if depth > 4 or target is None:
            return None
        if _is_job(target):
            return unwrap(target.target, depth + 1)
        if (inner := _inner_job(target)) is not None:
            return unwrap(inner, depth + 1)
        domain, libraries, _label = callable_owner(target)
        if (
            domain is None
            and (mapped := owner_domain(None, libraries, library_map)) != "core"
        ):
            domain = mapped
        if domain is not None:
            return domain
        if isinstance(target, functools.partial):
            for arg in (*target.args, *target.keywords.values()):
                if _is_callback(arg) and (found := unwrap(arg, depth + 1)) is not None:
                    return found
        return None

    def owner(target: Any, args: Iterable[Any] = ()) -> str:
        if (domain := unwrap(target)) is not None:
            return domain
        for arg in args:
            if _is_callback(arg) and (domain := unwrap(arg)) is not None:
                return domain
        return "core"

    return owner


def _task_owner(task: asyncio.Task[Any], module_owner: Callable[[str], str]) -> str:
    coro = task.get_coro()
    code = getattr(coro, "cr_code", None)
    if code is not None:
        kind, name = classify_file(code.co_filename)
        if kind == K_INTEGRATION and name is not None:
            return name
    frame = getattr(coro, "cr_frame", None)
    if frame is not None and (obj := frame.f_locals.get("self")) is not None:
        return module_owner(type(obj).__module__)
    return "core"


def count_resources(
    hass: Any,
    callable_owner_of: Callable[..., str],
    module_owner: Callable[[str], str],
) -> dict[str, Resources]:
    """Count listeners, dispatcher connections, timers and tasks per integration.

    Must run in the event loop: it reads structures the loop changes.
    """
    counts: dict[str, _ResourceCount] = defaultdict(_ResourceCount)
    for jobs in getattr(hass.bus, "_listeners", {}).values():
        for job, _event_filter in jobs:
            counts[callable_owner_of(job)].listeners += 1
    for targets in hass.data.get("dispatcher", {}).values():
        for target in targets:
            counts[callable_owner_of(target)].dispatcher += 1
    for handle in list(getattr(hass.loop, "_scheduled", ())):
        if not handle.cancelled():
            counts[
                callable_owner_of(
                    getattr(handle, "_callback", None), getattr(handle, "_args", ())
                )
            ].timers += 1
    for task in asyncio.all_tasks(hass.loop):
        if not task.done():
            counts[_task_owner(task, module_owner)].tasks += 1
    return {
        domain: Resources(c.listeners, c.dispatcher, c.timers, c.tasks)
        for domain, c in counts.items()
    }


def make_heap_owner(library_map: Mapping[str, str]) -> Callable[[str], str]:
    """Return a function that names the owner of a module for the heap.

    Integrations by domain, Home Assistant itself as "core", the standard
    library as "python", a library only one integration needs as that
    integration, any other library as "lib:<package>".
    """

    def owner(module: str) -> str:
        if (domain := module_domain(module)) is not None:
            return domain
        package = top_package(module)
        if package == "homeassistant":
            return "core"
        if package in sys.stdlib_module_names or package.startswith("_"):
            return "python"
        return library_map.get(package, f"lib:{package}")

    return owner


def heap_roots(
    hass: Any, heap_owner: Callable[[str], str], module_owner: Callable[[str], str]
) -> tuple[list[Root], list[Any]]:
    """Collect where the heap walk starts, and where it must stop.

    Must run in the event loop: it reads structures the loop changes.
    """
    components: list[str] = sorted(
        hass.config.top_level_components, key=len, reverse=True
    )

    def data_owner(key: Any) -> str | None:
        if not isinstance(key, str):
            return None
        for domain in components:
            if key == domain or key.startswith(f"{domain}_"):
                return domain
        return None

    data = [
        Root(entry.domain, DATA, entry) for entry in hass.config_entries.async_entries()
    ]
    core_data: list[Root] = []
    for key, value in list(hass.data.items()):
        if (domain := data_owner(key)) is not None:
            data.append(Root(domain, DATA, value))
        else:
            core_data.append(Root(f"core:{key}", DATA, value))
    data.extend(
        Root(platform.platform_name, DATA, entity)
        for platforms in hass.data.get("entity_platform", {}).values()
        for platform in platforms
        for entity in list(platform.entities.values())
    )
    data.extend(
        Root(_task_owner(task, module_owner), DATA, task)
        for task in asyncio.all_tasks(hass.loop)
        if not task.done()
    )
    data.sort(key=lambda root: root.owner)
    code = [
        Root(heap_owner(name), CODE, module.__dict__, name)
        for name, module in list(sys.modules.items())
        if isinstance(module, ModuleType)
    ]
    core = [
        Root("core:states", DATA, hass.states),
        Root("core:bus", DATA, hass.bus),
        Root("core:services", DATA, hass.services),
        Root("core:config_entries", DATA, hass.config_entries),
        *core_data,
        Root("core:hass", DATA, hass),
    ]
    stoppers = [hass, hass.loop, hass.data, hass.config]
    return [*data, *code, *core], stoppers


def account_heap(
    roots: list[Root], stoppers: list[Any], heap_owner: Callable[[str], str]
) -> tuple[dict[str, dict[str, int]], float]:
    """Walk the heap and return bytes per owner and category, and the time in ms."""
    start = time.perf_counter()
    accounts = walk(
        roots,
        stoppers,
        lambda cls: heap_owner(
            cls.__module__ if isinstance(cls.__module__, str) else ""
        ),
    )
    heap: dict[str, dict[str, int]] = defaultdict(dict)
    for (owner, category), tally in accounts.items():
        if tally.size:
            heap[owner][category] = heap[owner].get(category, 0) + tally.size
    return dict(heap), round((time.perf_counter() - start) * 1000, 1)


def _group(owner: str) -> str:
    """Return the report group of a heap owner."""
    if owner.startswith("core:") or owner in ("core", "python", SHARED):
        return "core"
    if owner.startswith("lib:"):
        return "library"
    return "integration"


def to_report(snapshot: Snapshot, previous: Snapshot | None) -> dict[str, Any]:
    """Return a snapshot as report data, with changes since the previous one."""

    def kib(value: int) -> float:
        return round(value / _KIB, 1)

    def mib(value: int) -> float:
        return round(value / _KIB**2, 1)

    empty_resources = Resources()
    owners = set(snapshot.heap) | set(snapshot.resources)
    rows = []
    for owner in owners:
        parts = snapshot.heap.get(owner, {})
        resources = snapshot.resources.get(owner, empty_resources)
        row: dict[str, Any] = {
            "owner": owner,
            "group": _group(owner),
            "kib": kib(sum(parts.values())),
            "data_kib": kib(parts.get(DATA, 0)),
            "code_kib": kib(parts.get(CODE, 0)),
            "unreached_kib": kib(parts.get(UNREACHED, 0)),
            "listeners": resources.listeners,
            "dispatcher": resources.dispatcher,
            "timers": resources.timers,
            "tasks": resources.tasks,
        }
        if previous is not None:
            before = previous.heap.get(owner, {})
            before_resources = previous.resources.get(owner, empty_resources)
            row["change"] = {
                "kib": kib(sum(parts.values()) - sum(before.values())),
                "listeners": resources.listeners - before_resources.listeners,
                "dispatcher": resources.dispatcher - before_resources.dispatcher,
                "timers": resources.timers - before_resources.timers,
                "tasks": resources.tasks - before_resources.tasks,
            }
        rows.append(row)
    rows.sort(key=lambda row: (-row["kib"], row["owner"]))
    python = sum(sum(parts.values()) for parts in snapshot.heap.values())
    groups = {
        group: kib(sum(row["kib"] for row in rows if row["group"] == group) * _KIB)
        for group in ("integration", "library", "core")
    }
    balance: dict[str, Any] = {
        "python_mib": mib(python),
        "integration_mib": round(groups["integration"] / _KIB, 1),
        "library_mib": round(groups["library"] / _KIB, 1),
        "core_mib": round(groups["core"] / _KIB, 1),
    }
    if (rss := snapshot.process.get("rss_mib")) is not None:
        balance["rss_mib"] = rss
        balance["not_in_python_mib"] = round(rss - python / _KIB**2, 1)
    report: dict[str, Any] = {
        "taken": snapshot.taken.isoformat(),
        "walk_ms": snapshot.walk_ms,
        "process": snapshot.process,
        "balance": balance,
        "owners": rows,
    }
    if previous is not None:
        report["previous_taken"] = previous.taken.isoformat()
        report["process_change"] = {
            key: round(value - previous.process[key], 1)
            for key, value in snapshot.process.items()
            if key in previous.process
        }
    return report
