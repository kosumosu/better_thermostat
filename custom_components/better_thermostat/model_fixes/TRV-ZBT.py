"""Quirks for the Sonoff TRV-ZBT (TRV Gen2) Zigbee TRV.

The TRV-ZBT regulates on an external temperature once its sensor selector
names one, and reads that temperature from an input nobody else keeps
current. That input and the selector carry the same names as on the TRVZB,
so the TRVZB write is reused to mirror the room temperature into it.

Its valve takes the opening its own controller holds while heating and the
one it holds while idle. Better Thermostat writes its position into both,
so the valve holds that position whichever state the controller is in.
The TRVZB write handles the pair; the close bump the TRVZB motor needs is
left out.

That pair is only Better Thermostat's while the device's own PID mode
(``smart_temperature_control``) is off, since the mode holds both numbers
under the device's algorithm. Startup switches it off for a TRV on Direct
Valve Based calibration; on any other calibration the device still
regulates, so the mode stays the user's.
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak as default_initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.model_fixes.TRVZB import (
    has_valve_channel,
    maybe_set_external_temperature,
    maybe_set_sonoff_valve_percent,
    valve_readings,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelFixHost,
    ModelQuirks,
    ValveChannelQuirk,
    ValveQuirk,
    ValveReadingsQuirk,
)
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_output,
    device_released,
    find_device_entity,
)

_LOGGER = logging.getLogger(__name__)

# The device feature that runs its own PID loop over the two openings.
_PID_SWITCH = "smart_temperature_control"

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "has_valve_channel",
    "initial_tweak",
    "maybe_set_external_temperature",
    "override_set_hvac_mode",
    "override_set_temperature",
    "override_set_valve",
    "valve_readings",
]


async def override_set_valve(self: ModelFixHost, entity_id: str, percent: int) -> bool:
    """Write the valve position into the heating and the idle opening.

    Returns True when the position went out, False to let the adapter
    fallback run.
    """
    return await maybe_set_sonoff_valve_percent(
        self, entity_id, max(0, min(100, percent))
    )


def _uses_direct_valve(self: ModelFixHost, entity_id: str) -> bool:
    """Return whether this TRV is configured for direct valve control."""
    return (
        configured_calibration_output(self.real_trvs[entity_id].advanced)
        == CalibrationOutput.DIRECT_VALVE_BASED
    )


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run the default tweaks, then free the two openings from the PID mode.

    With the device's own PID mode on, both openings describe where the
    device's algorithm wants the valve, not where Better Thermostat put it.
    A TRV on Direct Valve Based calibration switches the mode off once; the
    device keeps the setting, so startup finds it already off afterwards.
    """
    await default_initial_tweak(self, entity_id)

    if not _uses_direct_valve(self, entity_id) or device_released(self, entity_id):
        return

    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None or reg_entity.device_id is None:
        return
    pid_switch = find_device_entity(
        entity_registry,
        reg_entity.device_id,
        ["switch"],
        [_PID_SWITCH],
        owner=reg_entity,
    )
    if pid_switch is None:
        return
    state = self.hass.states.get(pid_switch)
    if state is None or state.state != STATE_ON:
        return
    try:
        await self.hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": pid_switch},
            blocking=True,
            context=self.context,
        )
    except (HomeAssistantError, OSError, TypeError, ValueError) as ex:
        # The openings stay the device's, so a write of Better Thermostat's
        # position may not reach the motor. Naming it and carrying on leaves
        # the rest of the startup sequence untouched.
        _LOGGER.warning(
            "better_thermostat %s: TRV-ZBT could not switch off the PID mode on %s: %s",
            self.device_name,
            pid_switch,
            ex,
        )
        return
    _LOGGER.debug(
        "better_thermostat %s: TRV-ZBT switched off the PID mode on %s",
        self.device_name,
        pid_switch,
    )


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    initial_tweak = staticmethod(initial_tweak)
    maybe_set_external_temperature = staticmethod(maybe_set_external_temperature)
    override_set_valve = staticmethod(override_set_valve)
    has_valve_channel = staticmethod(has_valve_channel)
    valve_readings = staticmethod(valve_readings)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_VALVE_QUIRK: ValveQuirk = _Surface()
_VALVE_CHANNEL_QUIRK: ValveChannelQuirk = _Surface()
_VALVE_READINGS_QUIRK: ValveReadingsQuirk = _Surface()
