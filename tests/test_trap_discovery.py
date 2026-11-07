"""Behaviour tests: real platform setup fed with fake advertisements."""

import unittest
from datetime import UTC, datetime, timedelta

from ha_harness import HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
TRAP_ID = "c8aedc738048"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")


class TrapDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        self.ha.setup_integration()

    def test_connect_supercat_advertisement_creates_trap_entities(self):
        # The battery value is published only once two readings agree, so the
        # Trap advertises twice before every entity exists.
        for _ in range(2):
            self.ha.feed_advertisement(
                TRAP_ADDRESS,
                CONNECT_READY,
                rssi=-58,
                source="attic-proxy",
                connectable=True,
            )
            self.ha.finish_adding()

        def state(suffix=None):
            unique_id = f"swissinno_trap_{TRAP_ID}" + (f"_{suffix}" if suffix else "")
            return self.ha.state(unique_id)

        self.assertEqual(state().state, "off")  # Ready
        self.assertEqual(state("battery").state, "3.08")
        self.assertEqual(state("rssi").state, "-58")
        last_seen = datetime.fromisoformat(state("last_seen").state)
        self.assertLess(datetime.now(UTC) - last_seen, timedelta(minutes=1))
        self.assertEqual(state("last_triggered").state, "unknown")
        self.assertEqual(state("trigger_count").state, "0")
        self.assertEqual(state("reset").state, "unknown")
        self.assertEqual(
            {entity.unique_id for entity in self.ha.entities()},
            {
                f"swissinno_trap_{TRAP_ID}",
                f"swissinno_trap_{TRAP_ID}_battery",
                f"swissinno_trap_{TRAP_ID}_rssi",
                f"swissinno_trap_{TRAP_ID}_last_seen",
                f"swissinno_trap_{TRAP_ID}_last_triggered",
                f"swissinno_trap_{TRAP_ID}_trigger_count",
                f"swissinno_trap_{TRAP_ID}_reset",
            },
        )


if __name__ == "__main__":
    unittest.main()
