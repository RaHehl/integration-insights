"""Tests for counting log records and storage writes."""

import logging

from custom_components.integration_insights.outputs import (
    LogCounter,
    StoreCounter,
    logger_domain,
    store_domain,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store


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
    """Debug and info, warnings and errors are counted apart; loggers name the warnings."""
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
    assert (chatty.debug, chatty.warning, chatty.error) == (2, 1, 2)
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
