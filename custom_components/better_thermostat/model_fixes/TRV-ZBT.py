"""Quirks for Sonoff TRV-ZBT (Zigbee radiator thermostat, Gen2) devices.

The TRV-ZBT offers no direct valve control. It exposes a target valve
opening for each of its two internal running states instead:
``heating_valve_position`` is the live valve target while the device's own
thermostat runs in ``heat``, ``idle_valve_position`` while it runs in
``idle``. A register belonging to the state the device is not in is
accepted but not actuated.

Better Thermostat's valve demand is therefore written to both registers:
whichever running state the device's internal loop happens to pick, the
valve follows the demanded opening. Both registers were verified against a
live device to be actuated while their state is the running one, so no
pinning of the device's thermostat and no injected temperatures are needed.

``initial_tweak`` keeps the two device features that would break that
channel: the device's own PID mode (``smart_temperature_control``), which
makes both valve-position registers unavailable, and a stuck remote
temperature sensor, which would regulate the head on
``external_temperature_input`` instead of its room.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

# Absolute imports: like SHTRV-01, this module's hyphenated name is not a
# valid identifier, so static tooling cannot resolve relative imports here.
from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak as default_initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.utils.helpers import find_device_entity

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "override_set_hvac_mode",
    "override_set_temperature",
    "override_set_valve",
]


async def override_set_valve(self, entity_id, percent: int) -> bool:
    """Write the demanded valve opening to both state registers.

    The demand goes to ``heating_valve_position`` and to
    ``idle_valve_position`` alike: only the register of the state the
    device is actually in drives the motor, so the other one lies in
    wait for the next state change and the valve follows Better
    Thermostat across running states.

    Parameters
    ----------
    self :
        Better Thermostat entity owning the TRV.
    entity_id :
        Entity id of the TRV whose valve is to be commanded.
    percent :
        Valve opening percentage to write, clamped to ``0..100``.

    Returns
    -------
    bool
        True when every register the device names took the write, False
        when the TRV names neither register or when a write was refused.
        A refusal lets the caller fall back to its own valve channel
        instead of recording a position a register never received.
    """
    try:
        pct = max(0, min(100, int(percent)))

        if self.real_trvs.get(entity_id) is None:
            return False

        entity_registry = er.async_get(self.hass)
        reg_entity = entity_registry.async_get(entity_id)
        if reg_entity is None or reg_entity.device_id is None:
            _LOGGER.debug(
                "better_thermostat %s: TRV-ZBT override_set_valve: no registry entity for %s",
                self.device_name,
                entity_id,
            )
            return False

        targets = []
        for keyword in ("heating_valve_position", "idle_valve_position"):
            target = find_device_entity(
                entity_registry, reg_entity.device_id, ["number"], [keyword]
            )
            if target is not None:
                targets.append(target)

        if not targets:
            _LOGGER.debug(
                "better_thermostat %s: TRV-ZBT found no valve position register on the device of %s",
                self.device_name,
                entity_id,
            )
            return False

        for target in targets:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": target, "value": pct},
                blocking=True,
                context=self.context,
            )
            _LOGGER.debug(
                "better_thermostat %s: set TRV-ZBT valve position %s%% on %s (for %s)",
                self.device_name,
                pct,
                target,
                entity_id,
            )
        return True
    except (
        HomeAssistantError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
    ) as ex:
        # A register that refuses the value (device asleep, integration
        # reloading, entity out of range) is reported as a declined write,
        # the way the TRVZB quirk reports one: the caller must not record a
        # position the valve never received.
        _LOGGER.warning(
            "better_thermostat %s: TRV-ZBT valve write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False


async def initial_tweak(self, entity_id):
    """Run the default startup tweaks, then protect the valve channel.

    The default tweak zeroes ``local_temperature_calibration`` and syncs
    the child lock. On top of that the TRV-ZBT needs its own PID mode off
    and a stuck remote temperature sensor handed back to the built-in
    one; either feature left active takes the valve out of Better
    Thermostat's hands.

    Parameters
    ----------
    self :
        Better Thermostat entity owning the TRV.
    entity_id :
        Entity id of the TRV to set the device features up for.
    """
    await default_initial_tweak(self, entity_id)

    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    if reg_entity is None or reg_entity.device_id is None:
        return
    device_id = reg_entity.device_id

    sensor_select = find_device_entity(
        entity_registry, device_id, ["select"], ["temperature_sensor_select"]
    )
    if sensor_select is not None:
        state = self.hass.states.get(sensor_select)
        # ``remote_source_offline`` is the device saying it already fell
        # back to its built-in sensor; it hands itself back once the
        # remote source returns. Only a sensor deliberately parked on the
        # remote value needs correcting.
        if state is not None and state.state == "remote_temperature":
            try:
                await self.hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": sensor_select, "option": "local_temperature"},
                    blocking=True,
                    context=self.context,
                )
                _LOGGER.debug(
                    "better_thermostat %s: TRV-ZBT handed %s back to its local sensor",
                    self.device_name,
                    sensor_select,
                )
            except (HomeAssistantError, OSError, TypeError, ValueError) as ex:
                _LOGGER.warning(
                    "better_thermostat %s: TRV-ZBT could not restore the local sensor on %s: %s",
                    self.device_name,
                    sensor_select,
                    ex,
                )

    pid_switch = find_device_entity(
        entity_registry, device_id, ["switch"], ["smart_temperature_control"]
    )
    if pid_switch is not None:
        state = self.hass.states.get(pid_switch)
        if state is not None and state.state == STATE_ON:
            try:
                await self.hass.services.async_call(
                    "switch",
                    "turn_off",
                    {"entity_id": pid_switch},
                    blocking=True,
                    context=self.context,
                )
                _LOGGER.debug(
                    "better_thermostat %s: TRV-ZBT switched device PID mode off on %s",
                    self.device_name,
                    pid_switch,
                )
            except (HomeAssistantError, OSError, TypeError, ValueError) as ex:
                _LOGGER.warning(
                    "better_thermostat %s: TRV-ZBT could not switch device PID mode off on %s: %s",
                    self.device_name,
                    pid_switch,
                    ex,
                )
