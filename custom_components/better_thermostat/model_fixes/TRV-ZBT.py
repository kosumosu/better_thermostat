"""Quirks for the Sonoff TRV-ZBT (TRV Gen2) Zigbee TRV.

The TRV-ZBT has no single valve helper. It carries one valve opening per
internal running state instead: ``heating_valve_position`` while its own
thermostat runs in ``heat``, ``idle_valve_position`` while it runs in
``idle``. The register of the running state moves the motor; the register of
the other state waits for its state to come up. A live TRV-ZBT was verified
to move its valve on a write to the idle register while it idled, with
``heating_valve_position`` at zero.

Better Thermostat's valve demand is written to both registers, so whichever
running state the device picks, the motor follows the demand. The device's
own PID mode (``smart_temperature_control``) holds the registers under its
control, so startup switches it off for a TRV configured for direct valve.

The device also regulates on an external temperature once its sensor selector
names one, and reads that temperature from an input nobody else keeps
current. That input and the selector carry the same names as on the TRVZB,
so the TRVZB write is reused to mirror the room temperature into it.

Everything else stays as the default quirks have it.
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
    maybe_set_external_temperature,
)
from custom_components.better_thermostat.model_fixes.types import (
    ExternalTemperatureQuirk,
    InitialTweakQuirk,
    ModelFixHost,
    ModelQuirks,
    ValveChannelQuirk,
    ValveQuirk,
)
from custom_components.better_thermostat.utils.const import CalibrationOutput
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_output,
    device_released,
    find_device_entity,
)

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "has_valve_channel",
    "initial_tweak",
    "maybe_set_external_temperature",
    "override_set_hvac_mode",
    "override_set_temperature",
    "override_set_valve",
]

_LOGGER = logging.getLogger(__name__)

# The valve opening the device holds in each of its own running states, in
# the order Better Thermostat writes them.
_VALVE_REGISTERS = ("heating_valve_position", "idle_valve_position")


def _valve_registers(self: ModelFixHost, entity_id: str) -> list[str]:
    """Return the enabled valve registers on the TRV's device.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying Home Assistant access.
    entity_id : str
        Entity id of the TRV whose device carries the registers.

    Returns
    -------
    list of str
        The register entities a position goes to, in the order it is
        written; empty when the device publishes none.
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None or reg_entity.device_id is None:
        return []
    device_id = reg_entity.device_id
    registers = []
    for keyword in _VALVE_REGISTERS:
        target = find_device_entity(entity_registry, device_id, ["number"], [keyword])
        if target is None:
            continue
        entry = entity_registry.async_get(target)
        if entry is None or entry.disabled_by is not None:
            continue
        registers.append(target)
    return registers


def _direct_valve(self: ModelFixHost, entity_id: str) -> bool:
    """Return whether the TRV is configured for direct valve control.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying the TRV records.
    entity_id : str
        Entity id of the TRV whose settings select the calibration type.

    Returns
    -------
    bool
        True when this TRV's calibration type is direct valve based.
    """
    return (
        configured_calibration_output(self.real_trvs[entity_id].advanced)
        == CalibrationOutput.DIRECT_VALVE_BASED
    )


def has_valve_channel(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether the device carries a valve register to write to.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying Home Assistant access.
    entity_id : str
        Entity id of the TRV whose device is asked.

    Returns
    -------
    bool
        True when at least one of the two state registers is present and
        enabled, so the valve is in reach through the quirk.
    """
    return bool(_valve_registers(self, entity_id))


async def override_set_valve(self: ModelFixHost, entity_id: str, percent: int) -> bool:
    """Write the demanded valve opening to both state registers.

    The demand goes to ``heating_valve_position`` and to
    ``idle_valve_position`` alike: only the register of the state the device
    is actually in drives the motor, so the other one lies in wait for the
    next state change and the valve follows Better Thermostat across running
    states.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying Home Assistant access.
    entity_id : str
        Entity id of the TRV whose valve is to be commanded.
    percent : int
        Valve opening percentage to write, clamped to ``0..100``.

    Returns
    -------
    bool
        True when every register the device names took the write, False
        when the device is the user's, names neither register, or refused
        one. A refusal leaves the position to the caller's next channel
        instead of recording a position a register never received.
    """
    try:
        position = max(0, min(100, percent))
    except TypeError:
        # A value that is not a number cannot name an opening: it is named
        # here and declined, so the caller's cycle keeps its other channels.
        _LOGGER.debug(
            "better_thermostat %s: TRV-ZBT valve position is not a number: %r",
            self.device_name,
            percent,
        )
        return False

    if device_released(self, entity_id):
        return False

    registers = _valve_registers(self, entity_id)
    if not registers:
        _LOGGER.debug(
            "better_thermostat %s: TRV-ZBT found no valve position register on "
            "the device of %s",
            self.device_name,
            entity_id,
        )
        return False

    try:
        for register in registers:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": register, "value": position},
                blocking=True,
                context=self.context,
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRV-ZBT valve position %s%% on %s (for %s)",
                self.device_name,
                position,
                register,
                entity_id,
            )
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # A register that refuses the value (device asleep, integration
        # reloading, entity out of range) is reported as a declined write:
        # the caller must not record a position the valve never received.
        _LOGGER.warning(
            "better_thermostat %s: TRV-ZBT valve write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    return True


async def initial_tweak(self: ModelFixHost, entity_id: str) -> None:
    """Run the default startup tweaks, then free the valve registers.

    The default tweak zeroes ``local_temperature_calibration`` and syncs the
    child lock. On top of that, a TRV configured for direct valve control
    needs the device's own PID mode off: while it runs, the device keeps the
    valve-position registers under its control instead of following the
    openings Better Thermostat writes there.

    Parameters
    ----------
    self : ModelFixHost
        The Better Thermostat instance, supplying Home Assistant access.
    entity_id : str
        Entity id of the TRV to prepare.
    """
    await default_initial_tweak(self, entity_id)

    if not _direct_valve(self, entity_id) or device_released(self, entity_id):
        return

    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None or reg_entity.device_id is None:
        return
    pid_switch = find_device_entity(
        entity_registry, reg_entity.device_id, ["switch"], ["smart_temperature_control"]
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
        # The registers stay under the device's control, so the writes
        # Better Thermostat sends may not reach the motor. Reporting it and
        # carrying on leaves the rest of the startup sequence untouched.
        _LOGGER.warning(
            "better_thermostat %s: TRV-ZBT could not switch device PID mode "
            "off on %s: %s",
            self.device_name,
            pid_switch,
            ex,
        )
        return
    _LOGGER.debug(
        "better_thermostat %s: TRV-ZBT switched device PID mode off on %s",
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


_MODEL_QUIRKS: ModelQuirks = _Surface()
_EXTERNAL_TEMPERATURE_QUIRK: ExternalTemperatureQuirk = _Surface()
_INITIAL_TWEAK_QUIRK: InitialTweakQuirk = _Surface()
_VALVE_QUIRK: ValveQuirk = _Surface()
_VALVE_CHANNEL_QUIRK: ValveChannelQuirk = _Surface()
