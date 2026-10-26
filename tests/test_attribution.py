"""Tests for code attribution."""

import pytest

from custom_components.integration_insights.attribution import (
    K_CORE,
    K_INTEGRATION,
    K_LIB,
    K_PLATFORM,
    PackageIndex,
    build_library_map,
    classify_file,
    load_package_index,
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


def test_build_library_map() -> None:
    """Map packages that exactly one loaded integration needs, also indirectly."""
    index = PackageIndex(
        imports={
            "aiohue": ("aiohue",),
            "shared": ("shared",),
            "py-foo": ("pyfoo", "pyfoo_c"),
            "foo-dep": ("foodep",),
            "aiohttp": ("aiohttp",),
            "google-a": ("google",),
            "google-b": ("google",),
        },
        requires={
            "homeassistant": ("aiohttp",),
            "aiohue": ("aiohttp",),
            "py-foo": ("foo-dep",),
        },
        shared_imports=frozenset({"google"}),
    )
    library_map = build_library_map(
        {
            "hue": ["aiohue==4.7.1"],
            "foo": ["shared==1", "py-foo==2"],
            "bar": ["shared==1", "google-a==1"],
        },
        index,
    )
    assert library_map == {
        "aiohue": "hue",
        "pyfoo": "foo",
        "pyfoo_c": "foo",
        "foodep": "foo",
    }


def test_load_package_index() -> None:
    """The real index knows installed packages and their dependencies."""
    index = load_package_index()
    assert "homeassistant" in index.imports["homeassistant"]
    assert "aiohttp" in index.closure(["homeassistant"])
