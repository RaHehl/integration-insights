"""Tests for how the sampler classifies a stack (no timer needed)."""

from contextvars import ContextVar
from types import SimpleNamespace

from custom_components.integration_insights.sampler import Sampler

from .helpers import compile_at, walk_through

AUTOMATION = "/usr/src/homeassistant/homeassistant/components/automation/__init__.py"
FOO = "/config/custom_components/foo/coordinator.py"
LIB = "/usr/local/lib/python3.14/site-packages/aiofoo/client.py"
OTHER_LIB = "/usr/local/lib/python3.14/site-packages/aiohttp/client.py"
SELECTORS = "/usr/local/lib/python3.14/selectors.py"
BASE_EVENTS = "/usr/local/lib/python3.14/asyncio/base_events.py"

SELECT = """\
def select(fn):
    return fn()
"""

RUN_ONCE = """\
def _run_once(fn):
    return fn()
"""


def test_innermost_integration_and_trigger() -> None:
    """The innermost integration is charged, the outermost one triggered it."""
    record = walk_through(Sampler(), AUTOMATION, FOO, OTHER_LIB, LIB)
    assert record.domain == "foo"
    assert record.trigger == "automation"
    assert record.libraries[:2] == ("aiofoo", "aiohttp")


def test_entry_is_recorded_with_its_domain() -> None:
    """The current config entry is kept with the domain it belongs to."""
    var: ContextVar[object] = ContextVar("entry")
    var.set(SimpleNamespace(entry_id="abc", domain="foo"))
    record = walk_through(Sampler(entry_var=var), FOO)
    assert (record.entry_id, record.entry_domain) == ("abc", "foo")


def test_idle_only_inside_the_loops_select() -> None:
    """select() counts as idle only when the event loop itself waits in it."""
    sampler = Sampler()
    select = compile_at(SELECTORS, SELECT)["select"]
    run_once = compile_at(BASE_EVENTS, RUN_ONCE)["_run_once"]
    poll = compile_at(FOO, "def poll(fn):\n    return fn()\n")["poll"]
    walk = compile_at(
        SELECTORS,
        "def select(sampler):\n    import sys\n    return sampler.walk(sys._getframe(), 0)\n",
    )["select"]
    assert run_once(lambda: walk(sampler)).domain == "core"  # lambda in between
    assert (
        compile_at(BASE_EVENTS, "def _run_once(f, s):\n    return f(s)\n")["_run_once"](
            walk, sampler
        ).domain
        == "idle"
    )
    assert poll(lambda: walk(sampler)).domain == "foo"
    assert select(lambda: None) is None


def test_select_wrapper_still_counts_as_idle() -> None:
    """The loop wait wrapper between _run_once and select() keeps it idle."""
    sampler = Sampler()
    walk = compile_at(
        SELECTORS,
        "def select(sampler):\n    import sys\n    return sampler.walk(sys._getframe(), 0)\n",
    )["select"]
    wrapper = compile_at(
        "/config/custom_components/integration_insights/background.py",
        "def select(f, s):\n    return f(s)\n",
    )["select"]
    run_once = compile_at(BASE_EVENTS, "def _run_once(w, f, s):\n    return w(f, s)\n")[
        "_run_once"
    ]
    assert run_once(wrapper, walk, sampler).domain != "idle"
    sampler.select_wrapper = wrapper.__code__
    assert run_once(wrapper, walk, sampler).domain == "idle"


def test_sample_inside_the_select_wrapper_is_idle() -> None:
    """A sample that lands in the wrapper right after select() returns is idle."""
    sampler = Sampler()
    wrapper = compile_at(
        "/config/custom_components/integration_insights/background.py",
        "def select(sampler):\n    import sys\n    return sampler.walk(sys._getframe(), 0)\n",
    )["select"]
    run_once = compile_at(BASE_EVENTS, "def _run_once(w, s):\n    return w(s)\n")[
        "_run_once"
    ]
    assert run_once(wrapper, sampler).domain == "integration_insights"
    sampler.select_wrapper = wrapper.__code__
    assert run_once(wrapper, sampler).domain == "idle"


def test_deep_library_stack_keeps_the_integration() -> None:
    """A deep library recursion neither loses the integration nor its line."""
    sampler = Sampler()
    record = walk_through(sampler, AUTOMATION, FOO, *[LIB] * 50)
    assert record.domain == "foo"
    assert record.trigger == "automation"
    frames = record.frames
    codes = frames[0::2]
    assert None in codes  # gap where library frames were left out
    assert codes[record.self_index].co_filename == FOO


def test_class_of_a_platform_package_counts_for_that_package() -> None:
    """A conversation agent running under assist_pipeline is conversation's."""
    agent_class = compile_at(
        "/usr/src/homeassistant/homeassistant/components/conversation/default_agent.py",
        "class DefaultAgent:\n"
        "    def process(self, sampler):\n"
        "        import sys\n"
        "        return sampler.walk(sys._getframe(), 0)\n",
    )["DefaultAgent"]
    agent_class.__module__ = "homeassistant.components.conversation.default_agent"
    pipeline = compile_at(
        "/usr/src/homeassistant/homeassistant/components/assist_pipeline/pipeline.py",
        "def run(agent, sampler):\n    return agent.process(sampler)\n",
    )["run"]
    record = pipeline(agent_class(), Sampler())
    assert record.domain == "conversation"
    assert record.trigger == "assist_pipeline"


def test_platform_base_class_without_owner_falls_back() -> None:
    """Platform code with no owning integration is charged to the platform."""
    record = walk_through(
        Sampler(), "/usr/src/homeassistant/homeassistant/components/light/__init__.py"
    )
    assert record.domain == "light"


ENTITY = """\
class Entity:
    entity_id = None

    def update(self, sampler):
        import sys
        return sampler.walk(sys._getframe(), 0)

    def outer(self, other, sampler):
        return other.update(sampler)


class Template:
    def render(self, entity, sampler):
        return entity.update(sampler)
"""


def test_innermost_entity_is_recorded() -> None:
    """The entity whose method runs is kept; objects without one are skipped."""
    namespace = compile_at("/config/custom_components/foo/sensor.py", ENTITY)
    inner = namespace["Entity"]()
    inner.entity_id = "sensor.inner"
    outer = namespace["Entity"]()
    outer.entity_id = "sensor.outer"
    sampler = Sampler()
    assert outer.outer(inner, sampler).entity_id == "sensor.inner"
    assert namespace["Template"]().render(inner, sampler).entity_id == "sensor.inner"
    assert namespace["Entity"]().update(sampler).entity_id is None


def test_entity_id_property_is_not_called() -> None:
    """An entity_id behind a property is ignored instead of running code."""
    namespace = compile_at(
        "/config/custom_components/foo/sensor.py",
        "class Entity:\n"
        "    calls = 0\n"
        "    @property\n"
        "    def entity_id(self):\n"
        "        type(self).calls += 1\n"
        "        return 'sensor.x'\n"
        "    def update(self, sampler):\n"
        "        import sys\n"
        "        return sampler.walk(sys._getframe(), 0)\n",
    )
    entity = namespace["Entity"]()
    assert entity.update(Sampler()).entity_id is None
    assert namespace["Entity"].calls == 0
