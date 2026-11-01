"""Tests for counting log records and storage writes."""

import logging
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
    assert rows["solar"].loggers == {RECORDER_SCHEMA_LOGGER: 2}
    # Without an integration for the entity it stays with the recorder
    assert rows["recorder"].warning == 2
    assert rows["recorder"].loggers == {RECORDER_SCHEMA_LOGGER: 2}


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
    """Every write through Store counts, with its size, for the integration of its key."""
    counter = StoreCounter()
    counter.install()
    try:
        store: Store[dict[str, int]] = Store(hass, 1, "chatty.cache")
        other: Store[dict[str, int]] = Store(hass, 1, "core.restore_state")
        for value in range(3):
            await hass.async_add_executor_job(
                store._write_prepared_data,  # noqa: SLF001
                "w",
                f'{{"value": {value}}}',
            )
        await hass.async_add_executor_job(
            other._write_prepared_data,  # noqa: SLF001
            "w",
            "{}",
        )
    finally:
        counter.uninstall()
    rows = {
        row.domain: row
        for row in counter.drain(lambda key: store_domain(key, {"chatty"}))
    }
    assert rows["chatty"].writes == 3
    assert rows["chatty"].size == 3 * len('{"value": 0}')
    assert rows["chatty"].keys == {"chatty.cache": 3}
    assert rows["core"].writes == 1
    assert Store._write_prepared_data.__qualname__ == "Store._write_prepared_data"  # noqa: SLF001


async def test_a_storage_wrapper_left_below_another_passes_through(
    hass: HomeAssistant,
) -> None:
    """Uninstalled under someone else's wrapper, writes go through uncounted."""
    counter = StoreCounter()
    original = Store._write_prepared_data  # noqa: SLF001
    counter.install()
    ours = Store._write_prepared_data  # noqa: SLF001

    def other(store: Store, mode: str, json_data: str) -> None:
        ours(store, mode, json_data)

    try:
        Store._write_prepared_data = other  # type: ignore[method-assign, assignment]  # noqa: SLF001
        counter.uninstall()
        assert Store._write_prepared_data is other  # noqa: SLF001
        store: Store[dict[str, int]] = Store(hass, 1, "chatty.cache")
        await hass.async_add_executor_job(
            store._write_prepared_data,  # noqa: SLF001
            "w",
            "{}",
        )
    finally:
        Store._write_prepared_data = original  # type: ignore[method-assign]  # noqa: SLF001
    assert counter.drain(lambda key: "chatty") == []


def test_newer_store_is_counted_where_it_serializes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home Assistant 2026.11 serializes in _prepare_data and writes through an I/O object."""

    def prepare(store: Store, data: dict) -> tuple[str, str]:
        return "w", '{"value": 1}'

    monkeypatch.delattr(Store, "_write_prepared_data")
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
    monkeypatch.delattr(Store, "_write_prepared_data")
    counter = StoreCounter()
    assert not counter.install()
    assert not counter.installed
    counter.uninstall()
    assert counter.drain(lambda key: "core") == []


async def test_measuring_starts_on_a_store_it_cannot_count(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown storage helper must not stop everything else from being measured."""
    monkeypatch.delattr(Store, "_write_prepared_data")
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
