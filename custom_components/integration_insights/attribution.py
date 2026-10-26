"""Map code to the integration it belongs to."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import distributions, packages_distributions
import re

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


@dataclass(frozen=True, slots=True)
class PackageIndex:
    """What is installed, as far as attribution needs it."""

    imports: dict[str, tuple[str, ...]]  # distribution -> import names
    requires: dict[str, tuple[str, ...]]  # distribution -> distributions it needs
    shared_imports: frozenset[str]  # import names that several distributions ship

    def closure(self, roots: Iterable[str]) -> set[str]:
        """Return the distributions roots need, including themselves."""
        seen: set[str] = set()
        todo = list(roots)
        while todo:
            dist = todo.pop()
            if dist in seen:
                continue
            seen.add(dist)
            todo.extend(self.requires.get(dist, ()))
        return seen


def load_package_index() -> PackageIndex:
    """Read the installed distributions (does I/O)."""
    imports: dict[str, list[str]] = defaultdict(list)
    providers: dict[str, set[str]] = defaultdict(set)
    for package, dists in packages_distributions().items():
        for dist in dists:
            name = _normalize(dist)
            imports[name].append(package)
            providers[package].add(name)
    requires: dict[str, tuple[str, ...]] = {}
    for distribution in distributions():
        name = _normalize(distribution.metadata["Name"] or "")
        needed = []
        for requirement in distribution.requires or ():
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            if (needed_name := requirement_name(requirement)) is not None:
                needed.append(needed_name)
        requires[name] = tuple(needed)
    return PackageIndex(
        {dist: tuple(names) for dist, names in imports.items()},
        requires,
        frozenset(p for p, dists in providers.items() if len(dists) > 1),
    )


def build_library_map(
    requirements: Mapping[str, Iterable[str]], index: PackageIndex
) -> dict[str, str]:
    """Map import names to the one integration that needs them.

    Dependencies count too. A package that Home Assistant itself or several
    loaded integrations need stays unmapped, as does an import name that
    several distributions ship (namespace packages such as google).
    """
    core = index.closure(["homeassistant"])
    owners: dict[str, set[str]] = defaultdict(set)
    for domain, reqs in requirements.items():
        roots = [name for r in reqs if (name := requirement_name(r)) is not None]
        for dist in index.closure(roots) - core:
            for package in index.imports.get(dist, ()):
                owners[package].add(domain)
    return {
        package: next(iter(domains))
        for package, domains in owners.items()
        if len(domains) == 1 and package not in index.shared_imports
    }
