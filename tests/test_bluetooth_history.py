"""Behaviour tests: the Trap stays in Home Assistant's Bluetooth history."""

import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from ha_harness import HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
TRAP_ID = "c8aedc738048"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
# Same Trap, Ready, with an implausible battery byte.
CONNECT_READY_LOW_BATTERY = bytes.fromhex("00 3F CE 03 04 00 01 B0 03 00")
CONNECT_CAUGHT = bytes.fromhex("01 3F CE 03 04 00 01 DA 03 00")


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

    def set_clock(self, now):
        """Fix the wall clock the integration stamps Last seen with."""
        binary_sensor = sys.modules["custom_components.swissinno_ble.binary_sensor"]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        patcher = patch.object(binary_sensor, "datetime", Clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_last_seen_advances_on_unchanged_repeat_advertisement(self):
        start = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.set_clock(start)
        self.advertise()
        self.set_clock(start + timedelta(seconds=61))
        self.advertise()

        self.assertEqual(
            self.ha.state(unique_id("last_seen")).state,
            (start + timedelta(seconds=61)).isoformat(),
        )

    def test_last_seen_is_written_at_most_once_a_minute(self):
        start = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        for seconds in range(0, 60, 2):
            self.set_clock(start + timedelta(seconds=seconds))
            self.advertise(source="attic-proxy", rssi=-58)
            self.advertise(source="shelly", rssi=-79, connectable=False)

        self.assertEqual(
            self.ha.written_states(unique_id("last_seen")), [start.isoformat()]
        )

    def test_last_seen_advances_when_only_a_weaker_receiver_hears_it(self):
        start = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.set_clock(start)
        self.advertise(source="attic-proxy", rssi=-58)
        self.set_clock(start + timedelta(seconds=61))
        self.advertise(source="shelly", rssi=-79, connectable=False)

        self.assertEqual(self.ha.history(TRAP_ADDRESS).source, "attic-proxy")
        self.assertEqual(
            self.ha.state(unique_id("last_seen")).state,
            (start + timedelta(seconds=61)).isoformat(),
        )

    def test_last_seen_is_written_when_the_trap_comes_back(self):
        start = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
        self.set_clock(start)
        self.advertise()
        self.assertTrue(self.ha.fire_unavailable(TRAP_ADDRESS))
        self.set_clock(start + timedelta(seconds=10))
        self.advertise()

        self.assertEqual(
            self.ha.state(unique_id("last_seen")).state,
            (start + timedelta(seconds=10)).isoformat(),
        )

    def test_signal_strength_shows_the_best_receiver(self):
        receivers = (
            ("shelly", -79, False),
            ("attic-proxy", -58, True),
            ("hall-proxy", -86, True),
        )
        for _ in range(3):
            for source, rssi, connectable in receivers:
                self.advertise(source=source, rssi=rssi, connectable=connectable)

        self.assertEqual(self.ha.state(unique_id("rssi")).state, "-58")

    def test_signal_strength_shows_the_latest_packet(self):
        self.advertise(rssi=-58)
        self.advertise(rssi=-64)

        self.assertEqual(self.ha.state(unique_id("rssi")).state, "-64")

    def test_two_receivers_hearing_one_packet_do_not_confirm_battery(self):
        self.advertise(source="attic-proxy", rssi=-58)
        self.advertise(source="shelly", rssi=-79, connectable=False)

        with self.assertRaises(KeyError):
            self.ha.entity(unique_id("battery"))

        self.advertise(source="attic-proxy", rssi=-58)

        self.assertEqual(self.ha.state(unique_id("battery")).state, "3.08")

    def test_late_caught_packet_from_weaker_receiver_after_reset(self):
        # A batching receiver (e.g. Shelly) can deliver a buffered Caught
        # packet after the Trap was Reset and is advertising Ready again.
        self.advertise(CONNECT_READY)
        self.advertise(CONNECT_CAUGHT)
        self.advertise(CONNECT_READY)
        self.advertise(CONNECT_CAUGHT, source="shelly", rssi=-79, connectable=False)

        self.assertEqual(self.ha.state(unique_id()).state, "off")  # Ready
        self.assertEqual(self.ha.state(unique_id("trigger_count")).state, "1")

    def test_silent_trap_goes_unavailable_and_comes_back(self):
        self.advertise()
        self.advertise()
        self.advertise(CONNECT_CAUGHT)
        history = {
            suffix: self.ha.state(unique_id(suffix)).state
            for suffix in ("last_seen", "last_triggered", "trigger_count")
        }

        self.assertTrue(self.ha.fire_unavailable(TRAP_ADDRESS))

        for suffix in (None, "battery", "rssi"):
            self.assertEqual(self.ha.state(unique_id(suffix)).state, "unavailable")
        for suffix, value in history.items():
            self.assertEqual(self.ha.state(unique_id(suffix)).state, value)
        self.assertEqual(history["trigger_count"], "1")

        self.advertise(CONNECT_CAUGHT)

        self.assertEqual(self.ha.state(unique_id()).state, "on")  # Caught
        self.assertEqual(self.ha.state(unique_id("battery")).state, "3.08")
        self.assertEqual(self.ha.state(unique_id("rssi")).state, "-58")
        self.assertGreater(
            self.ha.state(unique_id("last_seen")).state, history["last_seen"]
        )
        self.assertEqual(self.ha.state(unique_id("trigger_count")).state, "1")


class CachedAdvertisementTests(unittest.TestCase):
    """Advertisements in history from before setup don't set Status."""

    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)

    def advertise(self, payload, **kwargs):
        self.ha.feed_advertisement(
            TRAP_ADDRESS, payload, rssi=-58, source="attic-proxy", **kwargs
        )
        self.ha.finish_adding()

    def test_cached_caught_advertisement_does_not_set_status(self):
        self.advertise(CONNECT_CAUGHT, received_at=time.monotonic() - 60)
        self.ha.setup_integration()
        self.ha.finish_adding()

        with self.assertRaises(KeyError):
            self.ha.entity(unique_id())

        self.advertise(CONNECT_READY)

        self.assertEqual(self.ha.state(unique_id()).state, "off")  # Ready
        self.assertEqual(self.ha.state(unique_id("trigger_count")).state, "0")

    def test_unchanged_advertisement_after_setup_sets_status(self):
        # Home Assistant drops this repeat before the matcher callback, so
        # only the every-advertisement callback can see it.
        self.advertise(CONNECT_CAUGHT, received_at=time.monotonic() - 60)
        self.ha.setup_integration()
        self.advertise(CONNECT_CAUGHT)

        self.assertEqual(self.ha.state(unique_id()).state, "on")  # Caught
        self.assertEqual(self.ha.state(unique_id("trigger_count")).state, "0")


if __name__ == "__main__":
    unittest.main()
