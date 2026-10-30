"""Map code to the integration it belongs to."""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
import functools
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import re
import sys
import time
from types import ModuleType
from typing import Any

from homeassistant.const import BASE_PLATFORMS

K_CORE, K_INTEGRATION, K_PLATFORM, K_LIB = range(4)

_CUSTOM = "/custom_components/"
_COMPONENTS = "/homeassistant/components/"
_PACKAGES = "-packages/"
# Core helpers whose methods run on behalf of an entity or coordinator that an
# integration owns; the owner comes from the class of `self`.
_OWNER_HELPERS = (
    "/homeassistant/helpers/entity.py",
    "/homeassistant/helpers/update_coordinator.py",
)
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def platform_domains() -> frozenset[str]:
    """Return the entity platform domains (light, sensor, ...)."""
    return frozenset(BASE_PLATFORMS)


_PLATFORMS = platform_domains()


def classify_file(filename: str) -> tuple[int, str | None]:
    """Return the kind of a source file and its integration or package name."""
    if (i := filename.rfind(_CUSTOM)) >= 0:
        start = i + len(_CUSTOM)
        if (end := filename.find("/", start)) > 0:
            return K_INTEGRATION, filename[start:end]
    if (i := filename.rfind(_COMPONENTS)) >= 0:
        start = i + len(_COMPONENTS)
        if (end := filename.find("/", start)) > 0:
            domain = filename[start:end]
            if domain in _PLATFORMS:
                return K_PLATFORM, domain
            return K_INTEGRATION, domain
    if filename.endswith(_OWNER_HELPERS):
        return K_PLATFORM, None
    if (i := filename.rfind(_PACKAGES)) >= 0:
        start = i + len(_PACKAGES)
        end = filename.find("/", start)
        name = filename[start:end] if end > 0 else filename[start:]
        name = name.removesuffix(".py")
        if name != "homeassistant":
            return K_LIB, name
    return K_CORE, None


def module_domain(module: str) -> str | None:
    """Return the integration a module belongs to."""
    for prefix in ("homeassistant.components.", "custom_components."):
        if module.startswith(prefix):
            return module[len(prefix) :].split(".", 1)[0]
    return None


def top_package(module: str) -> str:
    """Return the top level package of a module name."""
    return module.split(".", 1)[0]


def callable_owner(fn: Any) -> tuple[str | None, tuple[str, ...], str]:
    """Return the domain, packages and label of a callable, without calling it.

    The class of a bound method wins over the function: Entity.update runs for
    the integration that owns the entity.
    """
    while isinstance(fn, functools.partial):
        fn = fn.func
    func = getattr(fn, "__func__", fn)
    name = getattr(func, "__name__", None) or type(func).__name__
    owner = getattr(fn, "__self__", None)
    modules: list[str] = []
    if owner is not None and not isinstance(owner, ModuleType):
        cls = type(owner)
        modules.append(cls.__module__)
        label = f"{cls.__module__}.{cls.__qualname__}.{name}"
    else:
        label = f"{getattr(func, '__module__', None) or '?'}.{getattr(func, '__qualname__', name)}"
    if isinstance(module := getattr(func, "__module__", None), str):
        modules.append(module)
    for module in modules:
        if (domain := module_domain(module)) is not None:
            return domain, (), label
    return None, tuple(dict.fromkeys(top_package(m) for m in modules)), label


def owner_domain(
    domain: str | None, libraries: Iterable[str], library_map: Mapping[str, str]
) -> str:
    """Return the integration for a resolved owner, "core" if there is none."""
    if domain is not None:
        return domain
    for library in libraries:
        if (mapped := library_map.get(library)) is not None:
            return mapped
    return "core"


def short_path(filename: str) -> str:
    """Shorten a path so reports carry no local directories or file names."""
    if (i := filename.rfind(_PACKAGES)) >= 0:
        return filename[i + len(_PACKAGES) :]
    for marker in (_CUSTOM, "/homeassistant/"):
        if (i := filename.rfind(marker)) >= 0:
            return filename[i + 1 :]
    if (i := filename.rfind("/python3.")) >= 0 and (j := filename.find("/", i + 1)) > 0:
        return "python/" + filename[j + 1 :]
    if filename.startswith("<") and filename.endswith(">"):
        return filename  # <frozen ...>, <string>
    return "<other>"


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_name(requirement: str) -> str | None:
    """Return the normalized distribution name of a requirement string."""
    if match := _REQUIREMENT_NAME.match(requirement):
        return _normalize(match.group(1))
    return None


type Reader = Callable[[str], tuple[str, ...] | None]


def _read_requires(dist: str) -> tuple[str, ...] | None:
    """Distributions a distribution needs, without extras (does I/O)."""
    try:
        requirements = distribution(dist).requires or ()
    except PackageNotFoundError:
        return None
    needed = []
    for requirement in requirements:
        if "extra ==" in requirement or "extra==" in requirement:
            continue
        if (name := requirement_name(requirement)) is not None:
            needed.append(name)
    return tuple(needed)


def _read_imports(dist: str) -> tuple[str, ...] | None:
    """Top level import names a distribution ships (does I/O)."""
    try:
        found = distribution(dist)
    except PackageNotFoundError:
        return None
    if text := found.read_text("top_level.txt"):
        return tuple(line.strip() for line in text.splitlines() if line.strip())
    names: dict[str, None] = {}
    for path in found.files or ():
        if (top := _top_name(str(path))) is not None:
            names[top] = None
    return tuple(names)


def _top_name(entry: str) -> str | None:
    """Return the import name a file list entry is part of, if any.

    A directory is a package, a .py file a module, a .so or .pyd file an
    extension module whose name ends at the first dot.
    """
    top, sep, _ = entry.partition("/")
    if sep:
        if top.endswith((".dist-info", ".egg-info", ".data")) or top in (
            "..",
            "__pycache__",
        ):
            return None
        return top
    if top.endswith(".py"):
        return top[:-3]
    if top.endswith((".so", ".pyd")):
        return top.split(".", 1)[0]
    return None


def _record_tops(record: bytes) -> set[bytes]:
    """Return the top level names a RECORD file lists: packages and modules."""
    tops: set[bytes] = set()
    for line in record.split(b"\n"):
        entry = line.split(b",", 1)[0].decode("utf-8", "replace")
        if (top := _top_name(entry)) is not None:
            tops.add(top.encode())
    return tops


# Executor threads hold the GIL while parsing; a short sleep every so many
# distributions lets the event loop run.
_YIELD_EVERY = 64
_YIELD_SECONDS = 0.001


def _scan_providers(import_names: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Find the distributions that ship import names by reading their file lists.

    The slow path for names that differ from their distribution (yaml from
    PyYAML). One pass covers all names, each file list is read once.
    """
    needles = {name.encode(): name for name in import_names}
    found: dict[str, list[str]] = {name: [] for name in needles.values()}
    count = 0
    for entry in sys.path:
        try:
            infos = [
                p
                for p in Path(entry).iterdir()
                if p.name.endswith((".dist-info", ".egg-info"))
            ]
        except OSError:
            continue
        for info in infos:
            count += 1
            if count % _YIELD_EVERY == 0:
                time.sleep(_YIELD_SECONDS)
            if info.name.endswith(".dist-info"):
                dist = info.name.removesuffix(".dist-info").rsplit("-", 1)[0]
            else:
                dist = info.name.removesuffix(".egg-info").split("-", 1)[0]
            dist = _normalize(dist)
            try:
                shipped = set((info / "top_level.txt").read_bytes().split())
            except OSError:
                try:
                    shipped = _record_tops((info / "RECORD").read_bytes())
                except OSError:
                    continue
            for needle in needles.keys() & shipped:
                found[needles[needle]].append(dist)
    return {name: tuple(dict.fromkeys(dists)) for name, dists in found.items()}


# Seconds before a missing import name is looked for again
MISSING_RETRY = 900.0


class Packages:
    """Installed distributions, read on demand and cached.

    Only the dependency tree of loaded integrations and the import names that
    show up in samples are read, never every installed package. Every method
    may do I/O.
    """

    def __init__(
        self,
        read_requires: Reader = _read_requires,
        read_imports: Reader = _read_imports,
        scan_providers: Callable[
            [Iterable[str]], dict[str, tuple[str, ...]]
        ] = _scan_providers,
    ) -> None:
        """Initialize; the readers are replaceable for tests."""
        self._read_requires = read_requires
        self._read_imports = read_imports
        self._scan_providers = scan_providers
        self._requires: dict[str, tuple[str, ...]] = {}
        self._providers: dict[str, tuple[str, ...]] = {}
        # Import names no distribution ships, with the time of the scan
        self._missing: dict[str, float] = {}

    def requires(self, dist: str) -> tuple[str, ...]:
        """Return the distributions dist needs."""
        if (needed := self._requires.get(dist)) is None:
            # Not installed (yet) is not cached: it may get installed later.
            if (needed := self._read_requires(dist)) is None:
                return ()
            self._requires[dist] = needed
        return needed

    def closure(self, roots: Iterable[str]) -> set[str]:
        """Return the distributions roots need, including themselves."""
        seen: set[str] = set()
        todo = list(roots)
        while todo:
            dist = todo.pop()
            if dist in seen:
                continue
            seen.add(dist)
            todo.extend(self.requires(dist))
        return seen

    def providers(self, import_names: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """Return the distributions that ship each import name."""
        result: dict[str, tuple[str, ...]] = {}
        missing: list[str] = []
        now = time.monotonic()
        for name in import_names:
            if (found := self._providers.get(name)) is not None:
                result[name] = found
                continue
            # A name no package ships is looked for again now and then: it
            # may get installed later. Every look is a scan of all packages.
            if now - self._missing.get(name, -MISSING_RETRY) < MISSING_RETRY:
                result[name] = ()
                continue
            dist = _normalize(name)
            if name in (self._read_imports(dist) or ()):
                result[name] = self._providers[name] = (dist,)
            else:
                missing.append(name)
        if missing:
            for name, found in self._scan_providers(missing).items():
                result[name] = found
                if found:
                    self._providers[name] = found
                else:
                    self._missing[name] = now
        return result


def distribution_owners(
    requirements: Mapping[str, Iterable[str]], packages: Packages
) -> dict[str, str]:
    """Map distributions to the one loaded integration that needs them.

    Dependencies count too. A distribution that Home Assistant itself or
    several integrations need stays unmapped.
    """
    core = packages.closure(["homeassistant"])
    owners: dict[str, set[str]] = defaultdict(set)
    for domain, reqs in requirements.items():
        roots = [name for r in reqs if (name := requirement_name(r)) is not None]
        for dist in packages.closure(roots) - core:
            owners[dist].add(domain)
    return {
        dist: next(iter(domains))
        for dist, domains in owners.items()
        if len(domains) == 1
    }


def library_owners(
    import_names: Iterable[str], owners: Mapping[str, str], packages: Packages
) -> dict[str, str]:
    """Return the integration each import name belongs to, where exactly one.

    An import name that several distributions ship (namespace packages such
    as google) stays unmapped.
    """
    return {
        name: owner
        for name, providers in packages.providers(import_names).items()
        if len(providers) == 1 and (owner := owners.get(providers[0])) is not None
    }
