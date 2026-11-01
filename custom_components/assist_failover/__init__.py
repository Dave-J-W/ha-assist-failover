"""Assist Failover: use a fast speech engine, and fall back to a local one when it's gone."""

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_STT_ADDRESS,
    CONF_STT_BACKUP,
    CONF_STT_PRIMARY,
    CONF_TTS_ADDRESS,
    CONF_TTS_BACKUP,
    CONF_TTS_PRIMARY,
    HEARTBEAT_SECONDS,
    HEARTBEAT_TIMEOUT,
    KIND_STT,
    KIND_TTS,
    REACHABLE_TIMEOUT,
    REVIVE_SECONDS,
)
from .logic import settings
from .wyoming_direct import describe, parse_address

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.STT, Platform.TTS]


def wyoming_entry(hass: HomeAssistant, entity_id: str):
    """Return this engine's Wyoming config entry, or None. Works while the entry is down."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.config_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    return entry if entry is not None and entry.domain == "wyoming" else None


async def _revive_engines(hass: HomeAssistant, entity_ids) -> None:
    """Reload a Wyoming entry whose service is back.

    A service that's away while Home Assistant starts can leave its entry in setup_error,
    and nothing retries that. Without this the engine stays gone until someone reloads it.
    """
    for entity_id in entity_ids:
        entry = wyoming_entry(hass, entity_id)
        if (
            entry is None
            or entry.disabled_by is not None
            or entry.state not in (ConfigEntryState.SETUP_ERROR, ConfigEntryState.SETUP_RETRY)
        ):
            continue
        if await describe(entry.data["host"], entry.data["port"], HEARTBEAT_TIMEOUT) is not None:
            _LOGGER.info("Reloading %s, because its service is back", entity_id)
            await hass.config_entries.async_reload(entry.entry_id)


class Primary:
    """A primary engine: where it is, and whether it's up. An engine with no address is never skipped."""

    def __init__(self, name, address, entity_id):
        self.name = name
        self.address = address
        self.entity_id = entity_id
        self.up = None
        self.info = None
        self._listeners = []

    @property
    def direct(self):
        """True if Assist Failover talks to the server itself, not through an entity."""
        return self.entity_id is None

    def add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def beat(self, _now=None):
        if self.address is None:
            return
        info = await describe(*self.address, HEARTBEAT_TIMEOUT)
        if info is not None:
            self.info = info
        up = info is not None
        if up == self.up:
            return
        if self.up is not None:
            _LOGGER.info("%s is %s", self.name, "up" if up else "down")
        self.up = up
        for listener in self._listeners:
            listener()

    async def reachable(self):
        """False straight away if the last beat failed; otherwise a quick check of the port."""
        if self.address is None:
            return True
        if self.up is False:
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(*self.address), REACHABLE_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        return True


def _primary(hass, config, address_key, entity_key):
    if address := parse_address(config.get(address_key)):
        return Primary(f"{address[0]}:{address[1]}", address, None)
    if entity_id := config.get(entity_key):
        entry = wyoming_entry(hass, entity_id)
        address = (entry.data["host"], entry.data["port"]) if entry is not None else None
        return Primary(entity_id, address, entity_id)
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = settings(entry)
    entry.runtime_data = {}
    for kind, address_key, entity_key in (
        (KIND_STT, CONF_STT_ADDRESS, CONF_STT_PRIMARY),
        (KIND_TTS, CONF_TTS_ADDRESS, CONF_TTS_PRIMARY),
    ):
        if primary := _primary(hass, config, address_key, entity_key):
            await primary.beat()
            entry.async_on_unload(
                async_track_time_interval(hass, primary.beat, timedelta(seconds=HEARTBEAT_SECONDS))
            )
            entry.runtime_data[kind] = primary

    engines = [
        config[key]
        for key in (CONF_STT_PRIMARY, CONF_STT_BACKUP, CONF_TTS_PRIMARY, CONF_TTS_BACKUP)
        if config.get(key)
    ]

    async def revive(_now):
        await _revive_engines(hass, engines)

    entry.async_on_unload(
        async_track_time_interval(hass, revive, timedelta(seconds=REVIVE_SECONDS))
    )
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
