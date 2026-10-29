"""Tests for code attribution."""

from pathlib import Path
import sys

import pytest

from custom_components.integration_insights.attribution import (
    K_CORE,
    K_INTEGRATION,
    K_LIB,
    K_PLATFORM,
    Packages,
    _scan_providers,
    classify_file,
    distribution_owners,
    library_owners,
    module_domain,
    requirement_name,
    short_path,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("/config/custom_components/foo/sensor.py", (K_INTEGRATION, "foo")),
        (
            "/usr/src/homeassistant/homeassistant/components/hue/light.py",
            (K_INTEGRATION, "hue"),
        ),
        (
            "/usr/src/homeassistant/homeassistant/components/light/__init__.py",
            (K_PLATFORM, "light"),
        ),
        (
            "/usr/src/homeassistant/homeassistant/helpers/update_coordinator.py",
            (K_PLATFORM, None),
        ),
        ("/usr/src/homeassistant/homeassistant/core.py", (K_CORE, None)),
        ("/usr/local/lib/python3.14/asyncio/events.py", (K_CORE, None)),
        (
            "/usr/local/lib/python3.14/site-packages/aiohue/v2/__init__.py",
            (K_LIB, "aiohue"),
        ),
        ("/usr/local/lib/python3.14/site-packages/six.py", (K_LIB, "six")),
        (
            "/usr/local/lib/python3.14/site-packages/homeassistant/core.py",
            (K_CORE, None),
        ),
    ],
)
def test_classify_file(filename: str, expected: tuple[int, str | None]) -> None:
    """Files map to the integration, platform or package they belong to."""
    assert classify_file(filename) == expected


def test_module_domain() -> None:
    """Modules map to their integration."""
    assert module_domain("homeassistant.components.hue.light") == "hue"
    assert module_domain("custom_components.foo") == "foo"
    assert module_domain("homeassistant.helpers.entity") is None


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("/config/custom_components/foo/sensor.py", "custom_components/foo/sensor.py"),
        (
            "/usr/src/homeassistant/homeassistant/components/hue/light.py",
            "homeassistant/components/hue/light.py",
        ),
        (
            "/usr/local/lib/python3.14/site-packages/aiohue/v2/__init__.py",
            "aiohue/v2/__init__.py",
        ),
        ("/usr/local/lib/python3.14/asyncio/events.py", "python/asyncio/events.py"),
        ("/home/someone/secret/unlock_door.py", "<other>"),
        ("<frozen importlib._bootstrap>", "<frozen importlib._bootstrap>"),
        (
            "/srv/homeassistant/lib/python3.14/site-packages/aiohue/v2/__init__.py",
            "aiohue/v2/__init__.py",
        ),
    ],
)
def test_short_path(filename: str, expected: str) -> None:
    """Reports carry no local directories."""
    assert short_path(filename) == expected


def test_requirement_name() -> None:
    """Requirement strings reduce to normalized distribution names."""
    assert requirement_name("aiohue==4.7.1") == "aiohue"
    assert requirement_name("Py_Foo.Bar>=1") == "py-foo-bar"
    assert requirement_name("pkg[extra]==1") == "pkg"
    assert requirement_name("") is None


def _packages(
    requires: dict[str, tuple[str, ...]],
    imports: dict[str, tuple[str, ...]],
    scan: dict[str, tuple[str, ...]] | None = None,
    reads: list[str] | None = None,
) -> Packages:
    def read_requires(dist: str) -> tuple[str, ...] | None:
        if reads is not None:
            reads.append(dist)
        return requires.get(dist, () if dist in imports else None)

    return Packages(
        read_requires,
        imports.get,
        lambda names: {name: (scan or {}).get(name, ()) for name in names},
    )


def test_library_owner() -> None:
    """Map libraries that exactly one loaded integration needs, also indirectly."""
    packages = _packages(
        requires={
            "homeassistant": ("aiohttp",),
            "aiohue": ("aiohttp",),
            "py-foo": ("foo-dep",),
        },
        imports={
            "aiohue": ("aiohue",),
            "shared": ("shared",),
            "py-foo": ("pyfoo", "pyfoo_c"),
            "foo-dep": ("foodep",),
            "aiohttp": ("aiohttp",),
        },
        scan={
            "pyfoo": ("py-foo",),
            "pyfoo_c": ("py-foo",),
            "foodep": ("foo-dep",),
            "google": ("google-a", "google-b"),
        },
    )
    owners = distribution_owners(
        {
            "hue": ["aiohue==4.7.1"],
            "foo": ["shared==1", "py-foo==2"],
            "bar": ["shared==1", "google-a==1"],
        },
        packages,
    )
    names = (
        "aiohue",
        "pyfoo",
        "pyfoo_c",
        "foodep",
        "shared",
        "aiohttp",
        "google",
        "unknown",
    )
    assert library_owners(names, owners, packages) == {
        "aiohue": "hue",
        "pyfoo": "foo",
        "pyfoo_c": "foo",
        "foodep": "foo",
    }


def test_packages_read_only_what_is_needed() -> None:
    """Only the dependency tree is read, each distribution once."""
    reads: list[str] = []
    packages = _packages(
        requires={"homeassistant": ("aiohttp",), "aiohue": ("aiohttp",)},
        imports={"aiohue": ("aiohue",), "aiohttp": ("aiohttp",), "other": ("other",)},
        reads=reads,
    )
    distribution_owners({"hue": ["aiohue"]}, packages)
    distribution_owners({"hue": ["aiohue"]}, packages)
    assert sorted(reads) == ["aiohttp", "aiohue", "homeassistant"]


def test_real_packages() -> None:
    """The real readers find installed packages and their dependencies."""
    packages = Packages()
    assert "aiohttp" in packages.closure(["homeassistant"])
    assert packages.providers(["aiohttp", "yaml", "not_installed_anywhere"]) == {
        "aiohttp": ("aiohttp",),
        "yaml": ("pyyaml",),
        "not_installed_anywhere": (),
    }


def test_scan_providers_reads_file_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import names are found in top_level.txt or RECORD, once per name."""
    site = tmp_path / "site"
    for dist, files in {
        "PyYAML-6.0.dist-info": {"top_level.txt": "_yaml\nyaml\n"},
        "attrs-24.1.dist-info": {
            "RECORD": "attr/__init__.py,sha256=x,1\nattrs/__init__.py,,\n"
        },
        "six-1.16.dist-info": {
            "RECORD": "six.py,sha256=x,1\nsix-1.16.dist-info/RECORD,,\n"
        },
        "attrs_fork-1.0.dist-info": {"RECORD": "attr/fork.py,,\n"},
    }.items():
        (site / dist).mkdir(parents=True)
        for name, text in files.items():
            (site / dist / name).write_text(text)
    monkeypatch.setattr(sys, "path", [str(site), str(tmp_path / "missing")])

    found = _scan_providers(["yaml", "attr", "six", "nothing"])
    # Directory order is up to the file system.
    assert {name: set(dists) for name, dists in found.items()} == {
        "yaml": {"pyyaml"},
        "attr": {"attrs", "attrs-fork"},
        "six": {"six"},
        "nothing": set(),
    }
