import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic

from homeassistant.components import bluetooth
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_register_callback,
    async_track_unavailable,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    ADVERTISEMENT_MATCHER,
    DATA_COORDINATOR,
    DOMAIN,
    MANUFACTURER_ID,
    entity_unique_id,
    legacy_unique_ids,
    normalized_address,
)
from .battery import BatteryStabilizer
from .coordinator import TrapObservation, TrapObservationCoordinator
from .decoder import DecodedTrapFrame, decode_frame
from .entity import PublishWhenAdded
from .freshness import is_fresh_advertisement

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    """Set up SWISSINNO BLE binary sensors."""
    _LOGGER.info("SWISSINNO BLE: Registering Bluetooth scanner callback...")

    sensors: dict[str, SwissinnoTrapSensor] = {}
    known_traps: set[str] = set()
    entity_registry = er.async_get(hass)
    coordinator: TrapObservationCoordinator = hass.data[DOMAIN][DATA_COORDINATOR]
    accept_after = monotonic()
    # Per Trap: the latest observation, the receive time of the arbitrated
    # advertisement it came from, and the deferred liveness tick.
    observations: dict[str, TrapObservation] = {}
    processed_at: dict[str, float] = {}
    battery_stabilizers: dict[str, BatteryStabilizer] = {}
    pending_ticks: dict[str, asyncio.Handle] = {}

    @callback
    def publish_advertisement(
        service_info: BluetoothServiceInfoBleak, frame: DecodedTrapFrame
    ) -> None:
        """Publish Status, battery, signal strength and Last seen of a Trap.

        ``service_info`` must be Home Assistant's arbitrated advertisement: the
        best receiver's latest one, so its signal strength is the best
        receiver's and its Status is never older than one already published.
        """
        trap_id = normalized_address(service_info.address)
        processed_at[trap_id] = service_info.time
        payload = service_info.manufacturer_data[MANUFACTURER_ID]

        _LOGGER.debug(
            "Trap %s (%s): status=0x%02X, tripped=%s, RSSI=%s dBm, "
            "battery=%s V, manufacturer_payload=%s, source=%s",
            trap_id,
            frame.model,
            frame.status,
            frame.is_tripped,
            service_info.rssi,
            frame.battery_volts,
            payload.hex(" ").upper(),
            service_info.source,
        )

        if trap_id in sensors:
            sensors[trap_id].update_state(frame.is_tripped)
        else:
            entity = SwissinnoTrapSensor(
                trap_id, frame.is_tripped, frame.model
            )
            sensors[trap_id] = entity
            async_add_entities([entity])

        # Battery readings can briefly be invalid during startup or switching.
        # Keep the last stable value until two consecutive readings agree.
        stabilizer = battery_stabilizers.setdefault(trap_id, BatteryStabilizer())
        battery_v = stabilizer.update(frame.battery_volts)
        if battery_v is None and (previous := observations.get(trap_id)):
            battery_v = previous.battery_v

        # Cache before publishing so sensor setup can safely finish after the
        # synchronous Bluetooth history replay performed during registration.
        observation = TrapObservation(
            rssi=service_info.rssi,
            battery_v=battery_v,
            legacy_trap_ids=frame.legacy_trap_ids,
            model=frame.model,
            last_seen=datetime.now(UTC),
        )
        observations[trap_id] = observation
        coordinator.update(trap_id, observation, is_tripped=frame.is_tripped)

    @callback
    def handle_advertisement(service_info: BluetoothServiceInfoBleak) -> None:
        """Liveness tick: one advertisement from the Trap via any receiver.

        Home Assistant calls this once per receiver per packet, before it
        arbitrates between receivers and updates its history, so the values
        are read from that history once this packet has been handled.
        """
        trap_id = normalized_address(service_info.address)
        if trap_id not in pending_ticks:
            pending_ticks[trap_id] = hass.loop.call_soon(
                handle_tick, service_info.address
            )

    @callback
    def handle_tick(address: str) -> None:
        trap_id = normalized_address(address)
        pending_ticks.pop(trap_id, None)
        best = bluetooth.async_last_service_info(hass, address, connectable=False)
        if (
            best is not None
            and best.time > processed_at.get(trap_id, float("-inf"))
            and (frame := _trap_frame(best)) is not None
        ):
            publish_advertisement(best, frame)
            return

        # Only a receiver that lost arbitration heard it: the Trap is alive.
        if (previous := observations.get(trap_id)) is not None:
            observation = replace(previous, last_seen=datetime.now(UTC))
            observations[trap_id] = observation
            coordinator.update(trap_id, observation)

    @callback
    def cancel_pending_ticks() -> None:
        for handle in pending_ticks.values():
            handle.cancel()
        pending_ticks.clear()

    entry.async_on_unload(cancel_pending_ticks)

    @callback
    def track_trap(service_info: BluetoothServiceInfoBleak, frame) -> None:
        """Start following a newly discovered Trap."""
        trap_id = normalized_address(service_info.address)

        # Versions before 1.0.16 used manufacturer-data bytes in the unique ID.
        # Preserve the user's existing entity_id when the MAC-based identity is
        # not already registered. Existing duplicate entities are left alone so
        # Home Assistant configuration is never destructively rewritten.
        old_unique_ids = legacy_unique_ids(frame.legacy_trap_ids)
        unique_id = entity_unique_id(service_info.address)
        if not entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, unique_id
        ):
            for legacy_unique_id in old_unique_ids:
                legacy_entity_id = entity_registry.async_get_entity_id(
                    "binary_sensor", DOMAIN, legacy_unique_id
                )
                if legacy_entity_id:
                    entity_registry.async_update_entity(
                        legacy_entity_id, new_unique_id=unique_id
                    )
                    break

        @callback
        def unavailable_callback(_service_info) -> None:
            if trap_id in sensors:
                sensors[trap_id].set_unavailable()
            coordinator.set_unavailable(trap_id)

        # Home Assistant fires this callback for every advertisement from every
        # receiver, including unchanged repeats the matcher callback never sees.
        entry.async_on_unload(
            bluetooth.async_register_advertisement_callback(
                hass, handle_advertisement, service_info.address
            )
        )
        entry.async_on_unload(
            async_track_unavailable(
                hass,
                unavailable_callback,
                service_info.address,
                connectable=False,
            )
        )

    @callback
    def detection_callback(service_info: BluetoothServiceInfoBleak, change):
        """Discover Traps from changed advertisements."""
        if (frame := _trap_frame(service_info)) is None:
            return

        trap_id = normalized_address(service_info.address)
        if trap_id in known_traps:
            # The liveness tick handles this one.
            return
        known_traps.add(trap_id)
        track_trap(service_info, frame)

        if not is_fresh_advertisement(service_info.time, accept_after):
            _LOGGER.debug(
                "Ignoring cached advertisement for %s from before integration setup",
                service_info.address,
            )
            return

        # The every-advertisement callback was registered after this
        # advertisement arrived, so handle it here once. Home Assistant calls
        # this after updating its history, so it is the arbitrated one.
        publish_advertisement(service_info, frame)

    cancel_callback = async_register_callback(
        hass,
        detection_callback,
        ADVERTISEMENT_MATCHER,
        BluetoothScanningMode.PASSIVE,
    )

    entry.async_on_unload(cancel_callback)

    _LOGGER.info("SWISSINNO BLE: Bluetooth scanner callback registered.")


def _trap_frame(service_info: BluetoothServiceInfoBleak) -> DecodedTrapFrame | None:
    """Decode the Trap advertisement, or None if it is not one."""
    payload = service_info.manufacturer_data.get(MANUFACTURER_ID)
    if payload is None:
        return None
    return decode_frame(payload)


class SwissinnoTrapSensor(PublishWhenAdded, BinarySensorEntity):
    """Representation of a SWISSINNO BLE trap."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:rodent"
    _attr_translation_key = "trap_status"

    def __init__(self, trap_id: str, tripped: bool | None, model: str):
        self._trap_id = trap_id
        self._state = tripped
        self._attr_available = True

        self._attr_unique_id = entity_unique_id(trap_id)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, trap_id)},
            manufacturer="SWISSINNO",
            name=f"SWISSINNO Trap {trap_id}",
            model=model,
        )

    @property
    def is_on(self) -> bool | None:
        return self._state

    def update_state(self, tripped: bool | None):
        self._state = tripped
        self._attr_available = True
        self._publish_state()

    @callback
    def set_unavailable(self) -> None:
        """Mark the trap unavailable when advertisements stop."""
        self._attr_available = False
        self._publish_state()
