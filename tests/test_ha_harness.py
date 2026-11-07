"""Pin the fake Bluetooth stack to Home Assistant's documented behaviour."""

import unittest

from ha_harness import HomeAssistantHarness

ADDRESS = "C8:AE:DC:73:80:48"
READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
CAUGHT = bytes.fromhex("01 3F CE 03 04 00 01 DA 03 00")


class FakeBluetoothStackTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        module = self.ha.bluetooth.module()
        self.bt = module
        self.matcher_calls = []
        self.bt.async_register_callback(
            self.ha.hass,
            lambda info, _change: self.matcher_calls.append(info),
            {"manufacturer_id": 3003, "connectable": False},
            self.bt.BluetoothScanningMode.PASSIVE,
        )

    def test_matcher_callback_skips_unchanged_payload_but_history_updates(self):
        self.ha.feed_advertisement(ADDRESS, READY, rssi=-60)
        self.ha.feed_advertisement(ADDRESS, READY, rssi=-70)
        self.ha.feed_advertisement(ADDRESS, CAUGHT, rssi=-65)

        self.assertEqual([info.rssi for info in self.matcher_calls], [-60, -65])
        self.ha.feed_advertisement(ADDRESS, CAUGHT, rssi=-61)
        self.assertEqual(self.ha.history(ADDRESS).rssi, -61)

    def test_advertisement_callback_fires_for_every_packet_from_every_scanner(self):
        seen = []
        cancel = self.bt.async_register_advertisement_callback(
            self.ha.hass, lambda info: seen.append(info.source), ADDRESS
        )
        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-58)
        self.ha.feed_advertisement(ADDRESS, READY, source="shelly", rssi=-79)
        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-58)
        cancel()
        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-58)

        self.assertEqual(seen, ["attic", "shelly", "attic"])

    def test_advertisement_callback_runs_before_the_history_update(self):
        seen = []
        self.bt.async_register_advertisement_callback(
            self.ha.hass,
            lambda info: seen.append(self.ha.history(ADDRESS)),
            ADDRESS,
        )
        first = self.ha.feed_advertisement(ADDRESS, READY, rssi=-60)
        self.ha.feed_advertisement(ADDRESS, READY, rssi=-70)

        self.assertEqual(seen, [None, first])

    def test_work_deferred_from_advertisement_callback_sees_the_packet(self):
        deferred = []

        def on_advertisement(_info):
            self.ha.hass.loop.call_soon(
                lambda: deferred.append(self.ha.history(ADDRESS).rssi)
            )

        self.bt.async_register_advertisement_callback(
            self.ha.hass, on_advertisement, ADDRESS
        )
        self.ha.feed_advertisement(ADDRESS, READY, rssi=-60)
        self.ha.feed_advertisement(ADDRESS, READY, rssi=-70)

        self.assertEqual(deferred, [-60, -70])

    def test_weaker_receiver_waits_for_the_owner_to_be_durably_gone(self):
        # habluetooth 7.1.2: without a tracked advertisement interval the
        # owner is stale only after FALLBACK_MAXIMUM_STALE_ADVERTISEMENT_SECONDS.
        self.ha.feed_advertisement(
            ADDRESS, READY, source="attic", rssi=-58, received_at=1000.0
        )
        self.ha.feed_advertisement(
            ADDRESS, CAUGHT, source="shelly", rssi=-79, received_at=1200.0
        )
        self.assertEqual(self.ha.history(ADDRESS).source, "attic")

        self.ha.feed_advertisement(
            ADDRESS, CAUGHT, source="shelly", rssi=-79, received_at=1901.0
        )
        self.assertEqual(self.ha.history(ADDRESS).source, "shelly")

    def test_stronger_receiver_takes_over_only_beyond_the_threshold(self):
        self.ha.feed_advertisement(ADDRESS, READY, source="shelly", rssi=-80)
        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-64)
        self.assertEqual(self.ha.history(ADDRESS).source, "shelly")

        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-63)
        self.assertEqual(self.ha.history(ADDRESS).source, "attic")

    def test_last_service_info_keeps_the_best_source(self):
        self.ha.feed_advertisement(ADDRESS, READY, source="attic", rssi=-58)
        self.ha.feed_advertisement(ADDRESS, CAUGHT, source="shelly", rssi=-79)

        best = self.bt.async_last_service_info(self.ha.hass, ADDRESS, False)
        self.assertEqual((best.source, best.rssi), ("attic", -58))
        # The weaker receiver's packet lost arbitration and was dropped.
        self.assertEqual(len(self.matcher_calls), 1)

    def test_connectable_history_holds_only_connectable_receivers(self):
        self.ha.feed_advertisement(ADDRESS, READY, source="shelly", connectable=False)
        self.assertIsNone(
            self.bt.async_ble_device_from_address(self.ha.hass, ADDRESS, True)
        )
        self.ha.feed_advertisement(ADDRESS, READY, source="proxy", connectable=True)
        device = self.bt.async_ble_device_from_address(self.ha.hass, ADDRESS, True)
        self.assertEqual(device.address, ADDRESS)

    def test_unavailable_fires_only_for_addresses_in_history(self):
        gone = []
        self.bt.async_track_unavailable(
            self.ha.hass, lambda info: gone.append(info.address), ADDRESS, False
        )
        self.ha.feed_advertisement(ADDRESS, READY)
        self.bt.async_clear_advertisement_history(self.ha.hass, ADDRESS)

        self.assertIsNone(self.ha.history(ADDRESS))
        self.assertFalse(self.ha.fire_unavailable(ADDRESS))
        self.ha.feed_advertisement(ADDRESS, READY)
        self.assertTrue(self.ha.fire_unavailable(ADDRESS))
        self.assertEqual(gone, [ADDRESS])

    def test_registering_a_matcher_replays_history(self):
        self.ha.feed_advertisement(ADDRESS, READY)
        replayed = []
        self.bt.async_register_callback(
            self.ha.hass,
            lambda info, _change: replayed.append(info.address),
            {"manufacturer_id": 3003, "connectable": False},
            self.bt.BluetoothScanningMode.PASSIVE,
        )
        self.assertEqual(replayed, [ADDRESS])


class FakeEntityPlatformTests(unittest.TestCase):
    def test_entities_get_hass_only_when_adding_finishes(self):
        ha = HomeAssistantHarness()
        self.addCleanup(ha.close)
        ha.setup_integration()
        ha.feed_advertisement(ADDRESS, READY)

        pending = [item.entity for item in ha.pending]
        self.assertTrue(pending)
        self.assertTrue(all(entity.hass is None for entity in pending))
        with self.assertRaises(RuntimeError):
            pending[0].async_write_ha_state()

        ha.finish_adding()
        self.assertTrue(all(entity.hass is ha.hass for entity in pending))


if __name__ == "__main__":
    unittest.main()
