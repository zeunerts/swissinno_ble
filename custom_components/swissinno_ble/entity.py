"""Shared behaviour for SWISSINNO BLE entities."""

from homeassistant.core import callback


class PublishWhenAdded:
    """Write state only once Home Assistant has added the entity.

    An advertisement can arrive before Home Assistant has finished adding a
    newly discovered Trap's entities. The value is kept on the entity, and
    Home Assistant writes it when it adds the entity.
    """

    _added = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._added = True

    async def async_will_remove_from_hass(self) -> None:
        self._added = False
        await super().async_will_remove_from_hass()

    @callback
    def _publish_state(self) -> None:
        if self._added:
            self.async_write_ha_state()
