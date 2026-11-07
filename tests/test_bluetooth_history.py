"""Behaviour tests: the Trap stays in Home Assistant's Bluetooth history."""

import time
import unittest

from ha_harness import HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
TRAP_ID = "c8aedc738048"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
# Same Trap, Ready, with an implausible battery byte.
CONNECT_READY_LOW_BATTERY = bytes.fromhex("00 3F CE 03 04 00 01 B0 03 00")


def unique_id(suffix=None):
    return f"swissinno_trap_{TRAP_ID}" + (f"_{suffix}" if suffix else "")


class BluetoothHistoryTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        self.ha.setup_integration()

    def advertise(self, payload=CONNECT_READY, **kwargs):
        kwargs.setdefault("rssi", -58)
        kwargs.setdefault("source", "attic-proxy")
        self.ha.feed_advertisement(TRAP_ADDRESS, payload, **kwargs)
        self.ha.finish_adding()

    def test_history_keeps_the_trap_after_many_advertisements(self):
        for _ in range(20):
            self.advertise()

        self.assertEqual(self.ha.bluetooth.cleared_addresses, [])
        self.assertIsNotNone(self.ha.history(TRAP_ADDRESS, connectable=False))
        self.assertIsNotNone(self.ha.history(TRAP_ADDRESS, connectable=True))

    def test_battery_appears_from_repeated_identical_advertisements(self):
        # The matcher callback sees only the first of these, as Home
        # Assistant drops unchanged payloads before it.
        self.advertise()
        self.advertise()

        self.assertEqual(self.ha.state(unique_id("battery")).state, "3.08")

    def test_one_odd_battery_reading_is_not_published(self):
        self.advertise()
        self.advertise()
        self.advertise(CONNECT_READY_LOW_BATTERY)
        self.advertise()

        self.assertEqual(self.ha.state(unique_id("battery")).state, "3.08")

    def test_last_seen_advances_on_unchanged_repeat_advertisement(self):
        self.advertise()
        first = self.ha.state(unique_id("last_seen")).state
        time.sleep(0.01)
        self.advertise()

        self.assertGreater(self.ha.state(unique_id("last_seen")).state, first)


if __name__ == "__main__":
    unittest.main()
