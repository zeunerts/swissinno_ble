# SWISSINNO BLE

A Home Assistant integration that watches SWISSINNO rodent traps over Bluetooth and reports whether each one has caught something.

## Language

**Trap**:
One SWISSINNO trap, or a Connect SuperCat catch alarm fitted under a trap, identified by its Bluetooth address.
_Avoid_: Device, sensor, mousetrap

**Status**:
Whether a trap is Ready or Caught.
_Avoid_: State, tripped flag

**Ready**:
The status of a trap that is armed and has not gone off.
_Avoid_: OK, armed

**Caught**:
The status of a trap that has gone off and has not been reset since.
_Avoid_: Tripped, triggered (as a status)

**Trigger**:
One change from Ready to Caught that Home Assistant observed. Trigger count counts these, not animals.
_Avoid_: Catch, trip, alarm

**Reset**:
The Bluetooth command that tells a Caught trap to go back to Ready.
_Avoid_: Re-arm, clear

**Last seen**:
When Home Assistant last received an advertisement from the trap through any receiver.
_Avoid_: Last update, heartbeat
