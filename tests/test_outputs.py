"""Tests for counting log records and storage writes."""

import json
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.integration_insights import outputs
from custom_components.integration_insights.const import DOMAIN
from custom_components.integration_insights.outputs import (
    RECORDER_SCHEMA_LOGGER,
    LogCounter,
    StoreCounter,
    logger_domain,
    store_domain,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

# The step every save goes through on the Home Assistant under test. The
# test harness replaces the write behind async_save, so tests call it.
SAVE_STEP = (
    "_prepare_data" if hasattr(Store, "_prepare_data") else "_write_prepared_data"
)


async def _save(hass: HomeAssistant, store: Store, data: dict[str, Any]) -> None:
    step = getattr(store, SAVE_STEP)
    if SAVE_STEP == "_prepare_data":
        await hass.async_add_executor_job(step, {"data": data})
    else:
        await hass.async_add_executor_job(step, "w", json.dumps({"data": data}))


def test_recorder_warnings_about_attributes_count_for_the_entity() -> None:
    """The integration that wrote the attributes gets the recorder's warning."""
    counter = LogCounter()
    counter.install()
    recorder = logging.getLogger(RECORDER_SCHEMA_LOGGER)
    try:
        for entity_id in ("sensor.big", "sensor.big", "sensor.unknown"):
            recorder.warning(
                "State attributes for %s exceed maximum size of %s bytes. "
                "This can cause database performance issues; Attributes "
                "will not be stored",
                entity_id,
                16384,
            )
        recorder.warning("Something else")
    finally:
        counter.uninstall()
    rows = {
        row.domain: row
        for row in counter.drain(
            lambda name: logger_domain(name, {}, {}),
            lambda entity_id: "solar" if entity_id == "sensor.big" else None,
        )
    }
    assert rows["solar"].warning == 2
    assert rows["solar"].oversized == 2
    assert rows["solar"].loggers == {RECORDER_SCHEMA_LOGGER: 2}
    # Without an integration for the entity it counts for core, with the rest
    # of what the recorder reports about others
    assert rows["core"].warning == 2
    assert rows["core"].loggers == {RECORDER_SCHEMA_LOGGER: 2}
    assert "recorder" not in rows


def test_loggers_belong_to_their_integration() -> None:
    """Integration loggers by name, libraries through the map, the rest is core."""
    cache: dict[str, str] = {}
    library_map = {"aiofoo": "foo"}
    assert (
        logger_domain("homeassistant.components.hue.light", library_map, cache) == "hue"
    )
    assert (
        logger_domain("custom_components.solar.sensor", library_map, cache) == "solar"
    )
    assert logger_domain("homeassistant.core", library_map, cache) == "core"
    assert logger_domain("aiofoo.client", library_map, cache) == "foo"
    assert logger_domain("urllib3.connectionpool", library_map, cache) == "core"
    assert cache["aiofoo.client"] == "foo"


def test_log_records_are_counted_by_level_and_logger() -> None:
    """Debug, info, warnings and errors are counted apart; loggers name the warnings."""
    counter = LogCounter()
    counter.install()
    logger = logging.getLogger("custom_components.chatty.api")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("one")
        logger.info("two")
        logger.warning("three")
        logger.error("four")
        logger.critical("five")
        logging.getLogger("homeassistant.core").warning("core")
    finally:
        logger.setLevel(previous)
        counter.uninstall()
    logger.warning("after uninstall")
    rows = {
        row.domain: row
        for row in counter.drain(lambda name: logger_domain(name, {}, {}))
    }
    chatty = rows["chatty"]
    assert (chatty.debug, chatty.info, chatty.warning, chatty.error) == (1, 1, 1, 2)
    assert chatty.loggers == {"custom_components.chatty.api": 3}
    assert rows["core"].warning == 1
    assert counter.drain(lambda name: "core") == []


async def test_storage_writes_are_counted_by_key(hass: HomeAssistant) -> None:
    """Every save through Store counts, with its size, for the integration of its key."""
    step = SAVE_STEP
    original = getattr(Store, step)
    counter = StoreCounter()
    counter.install()
    try:
        store: Store[dict[str, int]] = Store(hass, 1, "chatty.cache")
        other: Store[dict[str, int]] = Store(hass, 1, "core.restore_state")
        for value in range(3):
            await _save(hass, store, {"value": value})
        await _save(hass, other, {})
    finally:
        counter.uninstall()
    rows = {
        row.domain: row
        for row in counter.drain(lambda key: store_domain(key, {"chatty"}))
    }
    assert rows["chatty"].writes == 3
    assert rows["chatty"].size > 3 * len('{"value": 0}')
    assert rows["chatty"].keys == {"chatty.cache": 3}
    assert rows["core"].writes == 1
    assert getattr(Store, step) is original


async def test_a_storage_wrapper_left_below_another_passes_through(
    hass: HomeAssistant,
) -> None:
    """Uninstalled under someone else's wrapper, saves go through uncounted."""
    step = SAVE_STEP
    counter = StoreCounter()
    original = getattr(Store, step)
    counter.install()
    ours = getattr(Store, step)

    def other(*args: Any, **kwargs: Any) -> Any:
        return ours(*args, **kwargs)

    try:
        setattr(Store, step, other)
        counter.uninstall()
        assert getattr(Store, step) is other
        store: Store[dict[str, int]] = Store(hass, 1, "chatty.cache")
        await _save(hass, store, {})
    finally:
        setattr(Store, step, original)
    assert counter.drain(lambda key: "chatty") == []


async def test_a_new_argument_in_core_keeps_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper passes on whatever core passes, and still counts."""

    def prepare(store: Store, data: dict, *, extra: bool = False) -> tuple[str, str]:
        return "w", '{"extra": true}' if extra else "{}"

    monkeypatch.delattr(Store, "_write_prepared_data", raising=False)
    monkeypatch.setattr(Store, "_prepare_data", prepare, raising=False)
    counter = StoreCounter()
    assert counter.install()
    store = SimpleNamespace(key="chatty.cache")
    try:
        assert Store._prepare_data(store, {}, extra=True) == ("w", '{"extra": true}')  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        counter.uninstall()
    (row,) = counter.drain(lambda key: "chatty")
    assert row.size == len('{"extra": true}')


def test_newer_store_is_counted_where_it_serializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant 2026.11 serializes in _prepare_data and writes through an I/O object."""

    def prepare(store: Store, data: dict) -> tuple[str, str]:
        return "w", '{"value": 1}'

    monkeypatch.delattr(Store, "_write_prepared_data", raising=False)
    monkeypatch.setattr(Store, "_prepare_data", prepare, raising=False)
    counter = StoreCounter()
    assert counter.install()
    store = SimpleNamespace(key="chatty.cache")
    try:
        assert Store._prepare_data(store, {}) == ("w", '{"value": 1}')  # type: ignore[attr-defined]  # noqa: SLF001
        assert Store._prepare_data(store, {}) == ("w", '{"value": 1}')  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        counter.uninstall()
    assert Store._prepare_data is prepare  # type: ignore[attr-defined]  # noqa: SLF001
    (row,) = counter.drain(lambda key: "chatty")
    assert (row.writes, row.size) == (2, 2 * len('{"value": 1}'))


def test_a_foreign_prepare_result_does_not_stop_the_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Something else under the wrapper returns an odd value: it passes through."""
    odd = object()
    monkeypatch.setattr(Store, "_prepare_data", lambda store, data: odd, raising=False)
    counter = StoreCounter()
    assert counter.install()
    try:
        assert Store._prepare_data(SimpleNamespace(key="x"), {}) is odd  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        counter.uninstall()
    assert counter.drain(lambda key: "core") == []


def test_a_store_without_a_known_step_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a step to wrap, saves are not counted and nothing else breaks."""
    # A storage helper with neither step; the real one stays, core needs it
    monkeypatch.setattr(
        "custom_components.integration_insights.outputs.Store", type("Store", (), {})
    )
    counter = StoreCounter()
    assert not counter.install()
    assert not counter.installed
    counter.uninstall()
    assert counter.drain(lambda key: "core") == []


async def test_measuring_starts_on_a_store_it_cannot_count(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown storage helper must not stop everything else from being measured."""
    # A storage helper with neither step; the real one stays, core needs it
    monkeypatch.setattr(
        "custom_components.integration_insights.outputs.Store", type("Store", (), {})
    )
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.sampler.installed
    assert entry.runtime_data.summary()["sampler"]["stores_counted"] is False
    assert await hass.config_entries.async_unload(entry.entry_id)


def test_counting_never_breaks_a_logging_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure while counting is swallowed, the record still reaches others."""
    counter = LogCounter()

    def broken(record: logging.LogRecord) -> None:
        raise RuntimeError("counting failed")

    monkeypatch.setattr(counter, "emit", broken)
    counter.install()
    logger = logging.getLogger("custom_components.chatty.api")
    try:
        assert counter.handle(
            logger.makeRecord(logger.name, logging.ERROR, "f", 1, "x", None, None)
        )
        logger.error("still logged")
    finally:
        counter.uninstall()


def test_a_record_logged_while_counting_does_not_deadlock() -> None:
    """A finalizer that logs can run inside emit on the same thread."""
    counter = LogCounter()
    record = logging.LogRecord(
        "custom_components.chatty", logging.ERROR, "f", 1, "x", None, None
    )

    def nested() -> None:
        with counter._lock:  # noqa: SLF001
            counter.emit(record)

    thread = threading.Thread(target=nested, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    (row,) = counter.drain(lambda name: "chatty")
    assert row.error == 1


def test_counter_locks_are_renewed_in_a_forked_child() -> None:
    """A lock held at fork time would hang the child on its first count."""
    logs = LogCounter()
    assert logs._lock is logs.lock  # noqa: SLF001
    stores = StoreCounter()
    before = stores._lock  # noqa: SLF001
    outputs._renew_store_locks()  # noqa: SLF001
    assert stores._lock is not before  # noqa: SLF001


def test_the_store_wrapper_looks_like_the_original() -> None:
    """Executor jobs of a save are charged by the callable's module."""
    original = getattr(Store, SAVE_STEP)
    counter = StoreCounter()
    counter.install()
    try:
        wrapper = getattr(Store, SAVE_STEP)
        assert wrapper is not original
        assert wrapper.__module__ == original.__module__
        assert wrapper.__qualname__ == original.__qualname__
        assert wrapper.__code__ is counter.wrapper_code
    finally:
        counter.uninstall()


def test_records_that_cannot_name_their_integration_count_for_core() -> None:
    """Base platform loggers speak for many integrations, event data for anyone."""
    cache: dict[str, str] = {}
    assert logger_domain("homeassistant.components.sensor", {}, cache) == "core"
    assert (
        logger_domain("homeassistant.components.light.reproduce_state", {}, cache)
        == "core"
    )
    assert logger_domain("homeassistant.components.recorder", {}, cache) == "core"
    assert logger_domain("homeassistant.components.hue.light", {}, cache) == "hue"
    counter = LogCounter()
    counter.install()
    try:
        logging.getLogger(RECORDER_SCHEMA_LOGGER).warning(
            "Event data for %s exceed maximum size of %s bytes. "
            "This can cause database performance issues; Event data "
            "will not be stored",
            "big_event",
            32768,
        )
    finally:
        counter.uninstall()
    rows = {
        row.domain: row
        for row in counter.drain(lambda name: logger_domain(name, {}, {}))
    }
    assert rows["core"].warning == 1
    assert "recorder" not in rows


def test_records_about_another_integration_count_for_it() -> None:
    """Unserializable attributes and failed service calls name who they are about."""
    counter = LogCounter()
    counter.install()
    try:
        logging.getLogger(
            "homeassistant.components.recorder.table_managers.state_attributes"
        ).warning(
            "State is not JSON serializable: %s: %s",
            SimpleNamespace(entity_id="sensor.raw"),
            TypeError("bytes"),
        )
        websocket = logging.getLogger(
            "homeassistant.components.websocket_api.http.connection"
        )
        websocket.error(
            "Error during service call to %s.%s: %s", "solar", "refresh", "x"
        )
        websocket.error("Error during service call to %s.%s: %s", "typo", "x", "x")
    finally:
        counter.uninstall()
    rows = {
        row.domain: row
        for row in counter.drain(
            lambda name: logger_domain(name, {}, {}, {"solar"}),
            lambda entity_id: "solar" if entity_id == "sensor.raw" else None,
        )
    }
    assert (rows["solar"].warning, rows["solar"].oversized, rows["solar"].error) == (
        1,
        1,
        1,
    )
    # A domain that is not loaded does not become an integration of its own
    assert rows["core"].error == 1
    assert "websocket_api" not in rows
    assert "recorder" not in rows
