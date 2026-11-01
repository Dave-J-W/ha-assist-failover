"""Assist Failover: use a fast speech engine, and fall back to a local one when it's gone."""

import asyncio
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_STT_PRIMARY,
    CONF_TTS_PRIMARY,
    HEARTBEAT_SECONDS,
    HEARTBEAT_TIMEOUT,
    KIND_STT,
    KIND_TTS,
    REACHABLE_TIMEOUT,
)
from .logic import server_alive

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.STT, Platform.TTS]


def wyoming_address(hass: HomeAssistant, entity_id: str):
    """Return (host, port) if this engine is a Wyoming service, else None."""
    entity = er.async_get(hass).async_get(entity_id)
    if entity is None or entity.config_entry_id is None:
        return None
    entry = hass.config_entries.async_get_entry(entity.config_entry_id)
    if entry is None or entry.domain != "wyoming":
        return None
    return entry.data["host"], entry.data["port"]


class Heartbeat:
    """Keeps a primary's up/down current. An engine with no address is never skipped."""

    def __init__(self, name, address):
        self.name = name
        self.address = address
        self.up = None
        self._listeners = []

    def add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def beat(self, _now=None):
        if self.address is None:
            return
        up = await server_alive(*self.address, HEARTBEAT_TIMEOUT)
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = {**entry.data, **entry.options}
    entry.runtime_data = {}
    for kind, key in ((KIND_STT, CONF_STT_PRIMARY), (KIND_TTS, CONF_TTS_PRIMARY)):
        if entity_id := config.get(key):
            heartbeat = Heartbeat(entity_id, wyoming_address(hass, entity_id))
            await heartbeat.beat()
            entry.async_on_unload(
                async_track_time_interval(hass, heartbeat.beat, timedelta(seconds=HEARTBEAT_SECONDS))
            )
            entry.runtime_data[kind] = heartbeat
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
