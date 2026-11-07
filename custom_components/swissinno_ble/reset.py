"""Bluetooth GATT helper for resetting SWISSINNO traps."""

import logging

from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RESET_CHARACTERISTIC_UUID = "02ECC6CD-2B43-4DB5-96E6-EDE92CF8778D"


async def async_reset_trap(hass, address: str) -> None:
    """Reset a SWISSINNO trap by writing 0x00 to the reset characteristic."""
    normalized = address.upper()
    _LOGGER.info("SWISSINNO BLE: Resetting trap at %s", normalized)

    device = async_ble_device_from_address(hass, normalized, connectable=True)
    if device is None:
        raise _reset_error("reset_no_connectable_device", normalized)

    try:
        client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            device.name or normalized,
        )
    except (BleakError, TimeoutError) as err:
        raise _reset_error(
            "reset_connection_failed", normalized, error=str(err)
        ) from err

    try:
        characteristic = client.services.get_characteristic(RESET_CHARACTERISTIC_UUID)
        if characteristic is None:
            raise _reset_error("reset_characteristic_missing", normalized)

        properties = set(characteristic.properties)
        if "write-without-response" in properties:
            response = False
        elif "write" in properties:
            response = True
        else:
            raise _reset_error(
                "reset_characteristic_not_writable",
                normalized,
                properties=", ".join(sorted(properties)) or "none",
            )

        try:
            await client.write_gatt_char(characteristic, b"\x00", response=response)
        except (BleakError, TimeoutError) as err:
            raise _reset_error("reset_write_failed", normalized, error=str(err)) from err
        _LOGGER.info("SWISSINNO BLE: Successfully reset trap %s", normalized)
    finally:
        try:
            await client.disconnect()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug(
                "SWISSINNO BLE: Disconnect from trap %s failed: %s", normalized, err
            )


def _reset_error(key: str, address: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"address": address, **placeholders},
    )
