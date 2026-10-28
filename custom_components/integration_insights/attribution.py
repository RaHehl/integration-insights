"""Map code to the integration it belongs to."""

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
import functools
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import re
import sys
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
        top = path.parts[0]
        if top.endswith((".dist-info", ".data")) or top in ("..", "__pycache__"):
            continue
        names[top.removesuffix(".py")] = None
    return tuple(names)


def _scan_providers(import_names: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """Find the distributions that ship import names by reading their file lists.

    The slow path for names that differ from their distribution (yaml from
    PyYAML). One pass covers all names. Reads raw bytes and parses nothing.
    """
    needles = {name: name.encode() for name in import_names}
    found: dict[str, list[str]] = {name: [] for name in needles}
    for entry in sys.path:
        try:
            infos = [p for p in Path(entry).iterdir() if p.name.endswith(".dist-info")]
        except OSError:
            continue
        for info in infos:
            dist = _normalize(info.name.removesuffix(".dist-info").rsplit("-", 1)[0])
            try:
                top_level = set((info / "top_level.txt").read_bytes().split())
            except OSError:
                top_level = None
            if top_level is not None:
                for name, needle in needles.items():
                    if needle in top_level:
                        found[name].append(dist)
                continue
            try:
                record = b"\n" + (info / "RECORD").read_bytes()
            except OSError:
                continue
            for name, needle in needles.items():
                if (
                    b"\n" + needle + b"/" in record
                    or b"\n" + needle + b".py," in record
                ):
                    found[name].append(dist)
    return {name: tuple(dict.fromkeys(dists)) for name, dists in found.items()}


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
        for name in import_names:
            if (found := self._providers.get(name)) is not None:
                result[name] = found
                continue
            dist = _normalize(name)
            if name in (self._read_imports(dist) or ()):
                result[name] = self._providers[name] = (dist,)
            else:
                missing.append(name)
        if missing:
            for name, found in self._scan_providers(missing).items():
                result[name] = found
                # Not installed (yet) is not cached: it may get installed later.
                if found:
                    self._providers[name] = found
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
