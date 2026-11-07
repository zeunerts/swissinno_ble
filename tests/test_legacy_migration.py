"""Behaviour tests: legacy unique-ID migration runs once per Trap."""

import unittest
from collections import Counter

from ha_harness import HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
TRAP_ID = "c8aedc738048"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
CONNECT_CAUGHT = bytes.fromhex("01 3F CE 03 04 00 01 DA 03 00")

# Pre-1.0.16 unique IDs were built from payload bytes (here "CE030400").
LEGACY_ENTITIES = {
    ("binary_sensor", None): "binary_sensor.attic_trap",
    ("button", "reset"): "button.attic_trap_reset",
    ("sensor", "battery"): "sensor.attic_trap_battery",
    ("sensor", "rssi"): "sensor.attic_trap_rssi",
}


def unique_id(suffix=None):
    return f"swissinno_trap_{TRAP_ID}" + (f"_{suffix}" if suffix else "")


def legacy_unique_id(suffix=None):
    return "swissinno_trap_CE030400" + (f"_{suffix}" if suffix else "")


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        for (domain, suffix), entity_id in LEGACY_ENTITIES.items():
            self.ha.registry.add(
                domain, "swissinno_ble", legacy_unique_id(suffix), entity_id
            )
        self.ha.setup_integration()

    def advertise(self, payload):
        self.ha.feed_advertisement(TRAP_ADDRESS, payload, source="attic-proxy")

    def test_legacy_entities_keep_their_entity_ids_on_first_sight(self):
        self.advertise(CONNECT_READY)
        self.advertise(CONNECT_READY)
        self.ha.finish_adding()

        for (domain, suffix), entity_id in LEGACY_ENTITIES.items():
            with self.subTest(domain=domain, suffix=suffix):
                self.assertEqual(
                    self.ha.registry.async_get_entity_id(
                        domain, "swissinno_ble", unique_id(suffix)
                    ),
                    entity_id,
                )
                self.assertEqual(
                    self.ha.entity(unique_id(suffix)).entity_id, entity_id
                )

    def test_registry_lookups_happen_once_per_trap(self):
        # Status changes every time, so the matcher callbacks of every
        # platform see each advertisement.
        for _ in range(10):
            self.advertise(CONNECT_READY)
            self.advertise(CONNECT_CAUGHT)

        lookups = Counter(self.ha.registry.lookups)
        repeated = {key: n for key, n in lookups.items() if n > 1}
        self.assertEqual(repeated, {})
        self.assertIn(
            ("button", "swissinno_ble", unique_id("reset")), lookups
        )


if __name__ == "__main__":
    unittest.main()
