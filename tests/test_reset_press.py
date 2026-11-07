"""Behaviour tests: pressing Reset on the real button platform."""

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from ha_harness import HomeAssistantError, HomeAssistantHarness

TRAP_ADDRESS = "C8:AE:DC:73:80:48"
RESET_BUTTON = "swissinno_trap_c8aedc738048_reset"
CONNECT_READY = bytes.fromhex("00 3F CE 03 04 00 01 DA 03 00")
EN_TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "swissinno_ble"
    / "translations"
    / "en.json"
)


def english_message(error):
    """The English text Home Assistant shows for a translated error."""
    assert error.translation_domain == "swissinno_ble"
    exceptions = json.loads(EN_TRANSLATIONS.read_text())["exceptions"]
    message = exceptions[error.translation_key]["message"]
    return message.format(**(error.translation_placeholders or {}))


class ResetPressTests(unittest.TestCase):
    def setUp(self):
        self.ha = HomeAssistantHarness()
        self.addCleanup(self.ha.close)
        characteristic = Mock(properties=["write-without-response"])
        self.client = Mock()
        self.client.services.get_characteristic.return_value = characteristic
        self.client.write_gatt_char = AsyncMock()
        self.client.disconnect = AsyncMock()
        self.characteristic = characteristic
        self.ha.connector.establish_connection = AsyncMock(return_value=self.client)
        self.ha.setup_integration()

    def advertise(self, times=1):
        for _ in range(times):
            self.ha.feed_advertisement(
                TRAP_ADDRESS, CONNECT_READY, source="attic-proxy", connectable=True
            )
            self.ha.finish_adding()

    def test_reset_connects_through_receiver_and_writes_reset_byte(self):
        self.advertise(times=5)

        self.ha.press(RESET_BUTTON)

        connect = self.ha.connector.establish_connection
        connect.assert_awaited_once()
        self.assertEqual(connect.await_args.args[1].address, TRAP_ADDRESS)
        self.client.write_gatt_char.assert_awaited_once_with(
            self.characteristic, b"\x00", response=False
        )
        self.client.disconnect.assert_awaited_once()

    def test_reset_without_connectable_receiver_raises_readable_error(self):
        self.advertise()
        self.ha.fire_unavailable(TRAP_ADDRESS)

        with self.assertRaises(HomeAssistantError) as raised:
            self.ha.press(RESET_BUTTON)

        self.assertEqual(
            raised.exception.translation_key, "reset_no_connectable_device"
        )
        self.assertIn(TRAP_ADDRESS, english_message(raised.exception))
        self.ha.connector.establish_connection.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
