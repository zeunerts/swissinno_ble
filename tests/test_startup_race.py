"""Behaviour tests: advertisements that arrive before entities are added."""

import time
import unittest
from datetime import UTC, datetime

from ha_harness import HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
TRAP_ID = "c8aedc738048"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
CONNECT_CAUGHT = bytes.fromhex("01 3F CE 03 04 00 01 DA 03 00")


def unique_id(suffix=None):
    return f"swissinno_trap_{TRAP_ID}" + (f"_{suffix}" if suffix else "")


class StartupRaceTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        self.ha.setup_integration()

    def advertise(self, payload, rssi):
        self.ha.feed_advertisement(
            TRAP_ADDRESS, payload, rssi=rssi, source="attic-proxy"
        )

    def test_advertisements_before_entities_are_added_raise_nothing(self):
        self.advertise(CONNECT_READY, rssi=-70)
        self.advertise(CONNECT_CAUGHT, rssi=-58)

    def test_added_entities_show_the_latest_value_received(self):
        self.advertise(CONNECT_READY, rssi=-70)
        time.sleep(0.01)
        between = datetime.now(UTC)
        time.sleep(0.01)
        self.advertise(CONNECT_CAUGHT, rssi=-58)

        self.ha.finish_adding()

        def state(suffix=None):
            return self.ha.state(unique_id(suffix)).state

        self.assertEqual(state(), "on")  # Caught
        self.assertEqual(state("battery"), "3.08")
        self.assertGreater(datetime.fromisoformat(state("last_seen")), between)
        self.assertEqual(state("trigger_count"), "1")
        self.assertNotEqual(state("last_triggered"), "unknown")

    def test_trap_unavailable_before_entities_are_added(self):
        self.advertise(CONNECT_READY, rssi=-70)
        self.advertise(CONNECT_READY, rssi=-70)
        self.ha.fire_unavailable(TRAP_ADDRESS)

        self.ha.finish_adding()

        self.assertEqual(self.ha.state(unique_id()).state, "unavailable")
        self.assertEqual(self.ha.state(unique_id("rssi")).state, "unavailable")
        self.assertEqual(self.ha.state(unique_id("battery")).state, "unavailable")
        self.assertNotEqual(self.ha.state(unique_id("last_seen")).state, "unavailable")


if __name__ == "__main__":
    unittest.main()
