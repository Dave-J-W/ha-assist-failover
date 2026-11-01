"""Whether each primary answered its latest heartbeat. Only Wyoming primaries have one."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        PrimaryUp(entry, kind, heartbeat)
        for kind, heartbeat in entry.runtime_data.items()
        if heartbeat.address is not None
    )


class PrimaryUp(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, entry, kind, heartbeat):
        self._heartbeat = heartbeat
        self._attr_name = f"Assist Failover {kind.upper()} primary"
        self._attr_unique_id = f"{entry.entry_id}-{kind}-primary"
        self._attr_extra_state_attributes = {"engine": heartbeat.name}

    async def async_added_to_hass(self):
        self.async_on_remove(self._heartbeat.add_listener(self.async_write_ha_state))

    @property
    def is_on(self):
        return self._heartbeat.up
