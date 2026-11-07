"""Fake Home Assistant Bluetooth stack and entity platform for behaviour tests.

The real SWISSINNO BLE ``async_setup_entry`` and the binary_sensor, sensor and
button platforms run against these fakes; no Home Assistant install is needed.
The fake modules live in ``sys.modules`` only while a harness is open.

The Bluetooth fake follows Home Assistant 2026.10+ / habluetooth 7.x:

- Every-advertisement callbacks fire for every packet from every scanner,
  before any history update or deduplication.
- Each history (all, connectable) keeps one entry per address, from the
  source that wins arbitration: same source, a much stronger signal, or the
  previous winner went stale. A packet that loses arbitration is dropped.
- History is written before deduplication. Matcher callbacks then fire only
  when the payload (manufacturer data, service data, service UUIDs, name)
  differs from the previous history entry; RSSI is not compared.
- Registering a matcher callback replays matching history entries.
- Unavailable tracking fires only for addresses that are in history.
- Clearing the advertisement history really removes the address.

Entities passed to ``async_add_entities`` stay pending, without ``hass``,
until the test calls ``finish_adding()``.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import importlib.util
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

INTEGRATION_DIR = (
    Path(__file__).parents[1] / "custom_components" / "swissinno_ble"
)
PACKAGE = "custom_components.swissinno_ble"
PLATFORMS = ("binary_sensor", "sensor", "button")

# habluetooth defaults for switching the history entry to another source.
RSSI_SWITCH_THRESHOLD = 16
STALE_ADVERTISEMENT_SECONDS = 195


# --- Bluetooth --------------------------------------------------------------


@dataclass(frozen=True)
class FakeBLEDevice:
    address: str
    name: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FakeServiceInfo:
    """Stands in for ``BluetoothServiceInfoBleak``."""

    name: str
    address: str
    rssi: int
    manufacturer_data: dict[int, bytes]
    service_data: dict[str, bytes]
    service_uuids: list[str]
    source: str
    device: FakeBLEDevice
    connectable: bool
    time: float
    tx_power: int | None = None

    def same_payload(self, other: "FakeServiceInfo") -> bool:
        return (
            self.manufacturer_data == other.manufacturer_data
            and self.service_data == other.service_data
            and self.service_uuids == other.service_uuids
            and self.name == other.name
        )


class BluetoothScanningMode(enum.Enum):
    PASSIVE = "passive"
    ACTIVE = "active"


class BluetoothChange(enum.Enum):
    ADVERTISEMENT = "advertisement"


@dataclass
class _MatcherRegistration:
    callback: Callable
    matcher: dict
    mode: BluetoothScanningMode


def _matches(matcher: dict, info: FakeServiceInfo) -> bool:
    if matcher.get("connectable", True) and not info.connectable:
        return False
    if "address" in matcher and matcher["address"] != info.address:
        return False
    if (
        "manufacturer_id" in matcher
        and matcher["manufacturer_id"] not in info.manufacturer_data
    ):
        return False
    return True


def _prefer_new(old: FakeServiceInfo, new: FakeServiceInfo) -> bool:
    return (
        old.source == new.source
        or new.time - old.time > STALE_ADVERTISEMENT_SECONDS
        or new.rssi - RSSI_SWITCH_THRESHOLD > old.rssi
    )


class FakeBluetooth:
    """The state behind the fake ``homeassistant.components.bluetooth``."""

    def __init__(self) -> None:
        self.all_history: dict[str, FakeServiceInfo] = {}
        self.connectable_history: dict[str, FakeServiceInfo] = {}
        self.matcher_callbacks: list[_MatcherRegistration] = []
        self.advertisement_callbacks: list[tuple[Callable, str]] = []
        self.unavailable_callbacks: list[tuple[Callable, str, bool]] = []
        self.cleared_addresses: list[str] = []

    def _history(self, connectable: bool) -> dict[str, FakeServiceInfo]:
        return self.connectable_history if connectable else self.all_history

    def receive(self, info: FakeServiceInfo) -> None:
        for callback, address in tuple(self.advertisement_callbacks):
            if address == info.address:
                callback(info)

        changed: set[bool] = set()
        kinds = (False, True) if info.connectable else (False,)
        for connectable in kinds:
            history = self._history(connectable)
            old = history.get(info.address)
            if old is not None and not _prefer_new(old, info):
                continue
            history[info.address] = info
            if old is None or not old.same_payload(info):
                changed.add(connectable)

        for registration in tuple(self.matcher_callbacks):
            wants_connectable = registration.matcher.get("connectable", True)
            if wants_connectable in changed and _matches(registration.matcher, info):
                registration.callback(info, BluetoothChange.ADVERTISEMENT)

    def fire_unavailable(self, address: str) -> bool:
        fired = False
        for connectable in (False, True):
            info = self._history(connectable).pop(address, None)
            if info is None:
                continue
            for callback, tracked, tracked_connectable in tuple(
                self.unavailable_callbacks
            ):
                if tracked == address and tracked_connectable == connectable:
                    callback(info)
                    fired = True
        return fired

    def module(self) -> types.ModuleType:
        stack = self
        module = types.ModuleType("homeassistant.components.bluetooth")
        module.BluetoothScanningMode = BluetoothScanningMode
        module.BluetoothChange = BluetoothChange
        module.BluetoothServiceInfoBleak = FakeServiceInfo

        def _cancel(items, item):
            def cancel():
                if item in items:
                    items.remove(item)

            return cancel

        def async_register_callback(hass, callback, matcher, mode):
            registration = _MatcherRegistration(callback, dict(matcher), mode)
            stack.matcher_callbacks.append(registration)
            history = stack._history(matcher.get("connectable", True))
            for info in tuple(history.values()):
                if _matches(registration.matcher, info):
                    callback(info, BluetoothChange.ADVERTISEMENT)
            return _cancel(stack.matcher_callbacks, registration)

        def async_register_advertisement_callback(hass, callback, address):
            item = (callback, address)
            stack.advertisement_callbacks.append(item)
            return _cancel(stack.advertisement_callbacks, item)

        def async_track_unavailable(hass, callback, address, connectable=True):
            item = (callback, address, connectable)
            stack.unavailable_callbacks.append(item)
            return _cancel(stack.unavailable_callbacks, item)

        def async_last_service_info(hass, address, connectable=True):
            return stack._history(connectable).get(address)

        def async_ble_device_from_address(hass, address, connectable=True):
            info = stack._history(connectable).get(address)
            return info.device if info else None

        def async_clear_advertisement_history(hass, address):
            stack.cleared_addresses.append(address)
            stack.all_history.pop(address, None)
            stack.connectable_history.pop(address, None)

        for function in (
            async_register_callback,
            async_register_advertisement_callback,
            async_track_unavailable,
            async_last_service_info,
            async_ble_device_from_address,
            async_clear_advertisement_history,
        ):
            setattr(module, function.__name__, function)
        return module


# --- Entities ---------------------------------------------------------------


class Entity:
    hass = None
    entity_id: str | None = None
    platform = None
    _attr_available = True
    _attr_unique_id = None
    _attr_device_info = None
    _attr_icon = None
    _attr_entity_category = None
    _attr_has_entity_name = False
    _attr_translation_key = None

    @property
    def unique_id(self):
        return self._attr_unique_id

    @property
    def available(self):
        return self._attr_available

    @property
    def device_info(self):
        return self._attr_device_info

    @property
    def state(self):
        return None

    async def async_added_to_hass(self) -> None:
        """Run when the entity has been added."""

    def async_write_ha_state(self) -> None:
        # Home Assistant raises the same way for an entity not yet added.
        if self.hass is None:
            raise RuntimeError(f"Attribute hass is None for {self}")
        if self.entity_id is None:
            raise RuntimeError(f"No entity id specified for entity {self}")
        self.hass.states.write(self)


class BinarySensorEntity(Entity):
    @property
    def is_on(self):
        return None

    @property
    def state(self):
        if self.is_on is None:
            return None
        return "on" if self.is_on else "off"


class SensorEntity(Entity):
    _attr_native_value = None
    _attr_device_class = None
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_suggested_display_precision = None

    @property
    def native_value(self):
        return self._attr_native_value

    @property
    def state(self):
        value = self.native_value
        if isinstance(value, datetime):
            return value.isoformat()
        return value


class ButtonEntity(Entity):
    async def async_press(self) -> None:
        raise NotImplementedError


class _StrEnum(str, enum.Enum):
    pass


class SensorDeviceClass(_StrEnum):
    VOLTAGE = "voltage"
    SIGNAL_STRENGTH = "signal_strength"
    TIMESTAMP = "timestamp"


class SensorStateClass(_StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class EntityCategory(_StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class UnitOfElectricPotential(_StrEnum):
    VOLT = "V"


class HomeAssistantError(Exception):
    def __init__(
        self,
        *args,
        translation_domain=None,
        translation_key=None,
        translation_placeholders=None,
    ):
        super().__init__(*args)
        self.translation_domain = translation_domain
        self.translation_key = translation_key
        self.translation_placeholders = translation_placeholders


@dataclass(frozen=True)
class WrittenState:
    entity_id: str
    state: str
    available: bool


class FakeStates:
    def __init__(self) -> None:
        self.written: list[WrittenState] = []
        self.current: dict[str, WrittenState] = {}

    def write(self, entity: Entity) -> None:
        if not entity.available:
            value = "unavailable"
        elif (state := entity.state) is None:
            value = "unknown"
        else:
            value = str(state)
        written = WrittenState(entity.entity_id, value, entity.available)
        self.written.append(written)
        self.current[entity.entity_id] = written


class FakeEntityRegistry:
    def __init__(self) -> None:
        # (domain, platform, unique_id) -> entity_id
        self.entries: dict[tuple[str, str, str], str] = {}
        self.updates: list[tuple[str, str]] = []

    def add(self, domain: str, platform: str, unique_id: str, entity_id: str):
        self.entries[(domain, platform, unique_id)] = entity_id

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.entries.get((domain, platform, unique_id))

    def async_update_entity(self, entity_id, *, new_unique_id=None, **_kwargs):
        for key, value in tuple(self.entries.items()):
            if value == entity_id and new_unique_id is not None:
                del self.entries[key]
                self.entries[(key[0], key[1], new_unique_id)] = entity_id
                self.updates.append((entity_id, new_unique_id))


# --- Config entries and hass ------------------------------------------------


class FakeConfigEntry:
    def __init__(self) -> None:
        self.entry_id = "swissinno_entry"
        self.domain = "swissinno_ble"
        self.data: dict = {}
        self._on_unload: list[Callable] = []

    def async_on_unload(self, func: Callable) -> None:
        self._on_unload.append(func)

    def run_unload_callbacks(self) -> None:
        while self._on_unload:
            self._on_unload.pop()()


@dataclass
class _PendingAdd:
    domain: str
    entity: Entity
    update_before_add: bool


class FakeConfigEntries:
    def __init__(self, harness: "HomeAssistantHarness") -> None:
        self._harness = harness

    async def async_forward_entry_setups(self, entry, platforms) -> None:
        for domain in platforms:
            module = importlib.import_module(f"{PACKAGE}.{domain}")

            def async_add_entities(entities, update_before_add=False, domain=domain):
                for entity in entities:
                    self._harness.pending.append(
                        _PendingAdd(domain, entity, update_before_add)
                    )

            await module.async_setup_entry(
                self._harness.hass, entry, async_add_entities
            )

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True


class FakeHass:
    def __init__(self, harness: "HomeAssistantHarness") -> None:
        self.data: dict = {}
        self.states = FakeStates()
        self.config_entries = FakeConfigEntries(harness)
        self.loop = harness.loop


class FakeStore:
    """Stands in for ``homeassistant.helpers.storage.Store``."""

    def __init__(self, hass, version, key, **_kwargs):
        self._storage = hass.storage
        self.key = key

    def __class_getitem__(cls, _item):
        return cls

    async def async_load(self):
        return self._storage.get(self.key)

    def async_delay_save(self, data_func, delay=0):
        self._storage[self.key] = data_func()


# --- Harness ----------------------------------------------------------------


def _module(name: str, **attributes) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _callback(func):
    return func


class HomeAssistantHarness:
    """A fake Home Assistant running the real SWISSINNO BLE integration."""

    def __init__(self, storage: dict | None = None) -> None:
        self.loop = asyncio.new_event_loop()
        self.bluetooth = FakeBluetooth()
        self.registry = FakeEntityRegistry()
        self.pending: list[_PendingAdd] = []
        self.added: list[Entity] = []
        self.hass = FakeHass(self)
        self.hass.storage = dict(storage or {})
        self.entry = FakeConfigEntry()
        self.connector = _module(
            "bleak_retry_connector",
            BleakClientWithServiceCache=object,
            establish_connection=None,
        )
        self._modules = patch.dict(sys.modules, self._fake_modules())
        self._modules.start()
        self.integration = self._load_integration()

    # Lifecycle

    def close(self) -> None:
        self._modules.stop()
        self.loop.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def run(self, awaitable):
        """Run a coroutine on the harness event loop."""
        return self.loop.run_until_complete(awaitable)

    def setup_integration(self) -> bool:
        return self.run(self.integration.async_setup_entry(self.hass, self.entry))

    def unload_integration(self) -> bool:
        unloaded = self.run(
            self.integration.async_unload_entry(self.hass, self.entry)
        )
        if unloaded:
            self.entry.run_unload_callbacks()
        return unloaded

    # Bluetooth

    def feed_advertisement(
        self,
        address: str,
        payload: bytes,
        *,
        rssi: int = -60,
        source: str = "proxy",
        connectable: bool = True,
        manufacturer_id: int = 3003,
        name: str | None = None,
        received_at: float | None = None,
    ) -> FakeServiceInfo:
        """Deliver one advertisement as one scanner heard it."""
        info = FakeServiceInfo(
            name=name or address,
            address=address,
            rssi=rssi,
            manufacturer_data={manufacturer_id: bytes(payload)},
            service_data={},
            service_uuids=[],
            source=source,
            device=FakeBLEDevice(address, name),
            connectable=connectable,
            time=time.monotonic() if received_at is None else received_at,
        )
        self.bluetooth.receive(info)
        return info

    def fire_unavailable(self, address: str) -> bool:
        """Report the address gone; returns whether any callback fired."""
        return self.bluetooth.fire_unavailable(address)

    def history(self, address: str, connectable: bool = False):
        """Return the history entry Home Assistant holds for the address."""
        return self.bluetooth._history(connectable).get(address)

    # Entities

    def finish_adding(self) -> list[Entity]:
        """Finish adding pending entities: set hass, entity_id, write state."""
        finished = []
        while self.pending:
            pending = self.pending.pop(0)
            entity = pending.entity
            unique_id = entity.unique_id
            entity_id = self.registry.async_get_entity_id(
                pending.domain, "swissinno_ble", unique_id
            ) or f"{pending.domain}.{unique_id}"
            self.registry.add(pending.domain, "swissinno_ble", unique_id, entity_id)
            entity.hass = self.hass
            entity.entity_id = entity_id
            self.run(entity.async_added_to_hass())
            entity.async_write_ha_state()
            self.added.append(entity)
            finished.append(entity)
        return finished

    def entities(self) -> list[Entity]:
        """Entities that finished adding."""
        return list(self.added)

    def entity(self, unique_id: str) -> Entity:
        for entity in self.added:
            if entity.unique_id == unique_id:
                return entity
        raise KeyError(unique_id)

    def state(self, unique_id: str) -> WrittenState:
        """Latest state written by the entity with this unique ID."""
        return self.hass.states.current[self.entity(unique_id).entity_id]

    def written_states(self, unique_id: str) -> list[str]:
        entity_id = self.entity(unique_id).entity_id
        return [s.state for s in self.hass.states.written if s.entity_id == entity_id]

    def press(self, unique_id: str) -> None:
        self.run(self.entity(unique_id).async_press())

    # Module wiring

    def _fake_modules(self) -> dict[str, types.ModuleType]:
        er = _module(
            "homeassistant.helpers.entity_registry",
            async_get=lambda hass: self.registry,
        )
        helpers_entity = _module(
            "homeassistant.helpers.entity",
            DeviceInfo=dict,
            EntityCategory=EntityCategory,
            Entity=Entity,
        )
        helpers = _module(
            "homeassistant.helpers",
            entity_registry=er,
            entity=helpers_entity,
        )
        bluetooth = self.bluetooth.module()
        components = _module("homeassistant.components", bluetooth=bluetooth)
        return {
            "homeassistant": _module("homeassistant"),
            "homeassistant.components": components,
            "homeassistant.components.bluetooth": bluetooth,
            "homeassistant.components.binary_sensor": _module(
                "homeassistant.components.binary_sensor",
                BinarySensorEntity=BinarySensorEntity,
            ),
            "homeassistant.components.sensor": _module(
                "homeassistant.components.sensor",
                SensorEntity=SensorEntity,
                SensorDeviceClass=SensorDeviceClass,
                SensorStateClass=SensorStateClass,
            ),
            "homeassistant.components.button": _module(
                "homeassistant.components.button", ButtonEntity=ButtonEntity
            ),
            "homeassistant.config_entries": _module(
                "homeassistant.config_entries", ConfigEntry=FakeConfigEntry
            ),
            "homeassistant.const": _module(
                "homeassistant.const",
                SIGNAL_STRENGTH_DECIBELS_MILLIWATT="dBm",
                UnitOfElectricPotential=UnitOfElectricPotential,
            ),
            "homeassistant.core": _module(
                "homeassistant.core", HomeAssistant=FakeHass, callback=_callback
            ),
            "homeassistant.exceptions": _module(
                "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
            ),
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity": helpers_entity,
            "homeassistant.helpers.entity_registry": er,
            "homeassistant.helpers.device_registry": _module(
                "homeassistant.helpers.device_registry", DeviceEntry=object
            ),
            "homeassistant.helpers.storage": _module(
                "homeassistant.helpers.storage", Store=FakeStore
            ),
            "bleak_retry_connector": self.connector,
            "custom_components": _module("custom_components", __path__=[]),
        }

    def _load_integration(self) -> types.ModuleType:
        spec = importlib.util.spec_from_file_location(
            PACKAGE,
            INTEGRATION_DIR / "__init__.py",
            submodule_search_locations=[str(INTEGRATION_DIR)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = module
        spec.loader.exec_module(module)
        return module
