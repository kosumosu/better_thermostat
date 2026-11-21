"""Tests for the Sonoff TRV-ZBT (TRV Gen2) quirk.

The TRV-ZBT reports model_id ``TRV-ZBT``. It takes the room temperature
through the same external input and sensor selector as the TRVZB, and its
valve numbers (heating_valve_position, idle_valve_position) are the openings
its own running state holds, so the quirk writes Better Thermostat's demand
to both of them.
"""

import importlib
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest

from custom_components.better_thermostat.model_fixes import model_quirks as quirks
from custom_components.better_thermostat.utils.helpers import find_valve_entity
from tests.factories import (
    ThermostatStandIn,
    make_entity_registry,
    make_registry_entry,
    trv_record,
)

TRV_ZBT_MODULE = "custom_components.better_thermostat.model_fixes.TRV-ZBT"
trv_zbt = importlib.import_module(TRV_ZBT_MODULE)
trvzb = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")

ENTITY = "climate.trv1"
DEVICE = "dev1"
HEATING_VALVE = "number.trv1_heating_valve_position"
IDLE_VALVE = "number.trv1_idle_valve_position"
EXTERNAL_INPUT = "number.trv1_external_temperature_input"
SENSOR_SELECT = "select.trv1_temperature_sensor_select"
PID_SWITCH = "switch.trv1_smart_temperature_control"

DIRECT_VALVE_ADVANCED = {"calibration": "direct_valve_based"}


def _entry(entity_id, **kwargs):
    """A registry entry on the TRV's device, named as Zigbee2MQTT names it.

    Zigbee2MQTT entities reach Home Assistant through MQTT discovery and
    carry no translation key, so only their ids name them.
    """
    return make_registry_entry(entity_id, device_id=DEVICE, **kwargs)


def _trv_zbt_entries():
    """The TRV-ZBT's climate entity and the entities next to it."""
    return [
        _entry(ENTITY),
        _entry(HEATING_VALVE),
        _entry(IDLE_VALVE),
        _entry(EXTERNAL_INPUT),
        _entry(SENSOR_SELECT),
        _entry(PID_SWITCH),
    ]


def _make_self(selector_state="internal", states=None, entries=None, advanced=None):
    """A BT stand-in whose only TRV is a TRV-ZBT."""
    mock_self = ThermostatStandIn()
    mock_self.device_name = "test_thermostat"
    mock_self.context = MagicMock()
    mock_self.sensor_entity_id = "sensor.room_temperature"
    mock_self.hass.services.async_call = AsyncMock()
    mock_self.real_trvs = {
        ENTITY: trv_record(entity_id=ENTITY, model="TRV-ZBT", advanced=advanced or {})
    }
    mock_self._registry = make_entity_registry(*(entries or _trv_zbt_entries()))

    state_objects = {
        SENSOR_SELECT: State(
            SENSOR_SELECT,
            selector_state,
            {"options": ["internal", "external", "external_2", "external_3"]},
        )
    }
    for entity_id, state in (states or {}).items():
        state_objects[entity_id] = State(entity_id, state)
    mock_self.hass.states.get.side_effect = state_objects.get
    return mock_self


def _service_payloads(mock_self):
    """Every (domain, service, data) the quirk dispatched, in order."""
    return [
        call.args[:3] for call in mock_self.hass.services.async_call.await_args_list
    ]


def _patch_registry(monkeypatch, mock_self):
    """Point the quirk's registry lookup at the stand-in's registry."""
    monkeypatch.setattr(trv_zbt.er, "async_get", lambda hass: mock_self._registry)


class TestModelDetection:
    """The model_id the device registry reports picks the TRV-ZBT module."""

    def test_the_model_names_its_own_module(self):
        """No alias redirects the TRV-ZBT to the TRVZB module."""
        assert quirks.get_model_quirks_name("TRV-ZBT") == "TRV-ZBT"

    @pytest.mark.asyncio
    async def test_the_loader_imports_the_trv_zbt_module(self):
        """The hyphen survives the sanitising and the module imports.

        The import is real, so a file the loader cannot reach would land on
        the default module instead.
        """
        mock_self = ThermostatStandIn()
        mock_self.device_name = "test_thermostat"

        async def _import(_hass, name):
            return importlib.import_module(name)

        with patch.object(quirks, "async_import_module", side_effect=_import):
            module = await quirks.load_model_quirks(mock_self, "TRV-ZBT", ENTITY)

        assert module is trv_zbt
        assert module.__name__ == TRV_ZBT_MODULE


class TestExternalTemperature:
    """The room temperature reaches the input the TRV-ZBT regulates on."""

    @pytest.mark.asyncio
    async def test_the_room_temperature_is_written_and_the_selector_follows(
        self, monkeypatch
    ):
        """The input gets the value on its 0.1 grid, then the selector is set."""
        mock_self = _make_self(selector_state="internal")
        _patch_registry(monkeypatch, mock_self)
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.maybe_set_external_temperature(mock_self, ENTITY, 21.46)

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": EXTERNAL_INPUT, "value": 21.5}),
            (
                "select",
                "select_option",
                {"entity_id": SENSOR_SELECT, "option": "external"},
            ),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room", "written"), [(-3.0, 0.0), (120.0, 99.9)], ids=["below", "above"]
    )
    async def test_a_value_outside_the_input_range_is_clamped(
        self, monkeypatch, room, written
    ):
        """The input accepts 0 to 99.9 degrees and nothing beyond."""
        mock_self = _make_self(selector_state="external")
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert await trv_zbt.maybe_set_external_temperature(mock_self, ENTITY, room)

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": EXTERNAL_INPUT, "value": written})
        ]


class TestValveWrite:
    """A Better Thermostat position reaches both running-state registers."""

    def test_the_module_offers_a_valve_channel(self):
        """Carrying the override is what offers direct valve in the flow."""
        assert callable(trv_zbt.override_set_valve)
        assert callable(trv_zbt.has_valve_channel)
        assert quirks.quirk_writes_valve(trv_zbt) is True

    @pytest.mark.asyncio
    async def test_both_registers_receive_the_position(self, monkeypatch):
        """The heat and the idle register get the same demanded opening."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 42) is True

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": HEATING_VALVE, "value": 42}),
            ("number", "set_value", {"entity_id": IDLE_VALVE, "value": 42}),
        ]
        for call in mock_self.hass.services.async_call.await_args_list:
            assert call.kwargs["blocking"] is True
            assert call.kwargs["context"] is mock_self.context

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("demanded", "written"), [(150, 100), (-5, 0)], ids=["above", "below"]
    )
    async def test_the_position_is_clamped(self, monkeypatch, demanded, written):
        """A demand outside 0..100 reaches the device clamped, not raw."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, demanded) is True

        values = [payload[2]["value"] for payload in _service_payloads(mock_self)]
        assert values == [written, written]

    @pytest.mark.asyncio
    async def test_a_device_naming_one_register_is_served_by_it(self, monkeypatch):
        """A device that publishes only the idle register still takes it."""
        mock_self = _make_self(
            entries=[_entry(ENTITY), _entry(IDLE_VALVE), _entry(SENSOR_SELECT)]
        )
        _patch_registry(monkeypatch, mock_self)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 30) is True

        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": IDLE_VALVE, "value": 30})
        ]

    @pytest.mark.asyncio
    async def test_no_register_declines(self, monkeypatch):
        """Without either register the quirk declines to the adapter."""
        mock_self = _make_self(entries=[_entry(ENTITY), _entry(SENSOR_SELECT)])
        _patch_registry(monkeypatch, mock_self)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 50) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_released_device_is_left_alone(self, monkeypatch):
        """A TRV the user released receives no position."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)
        mock_self.devices_released = True

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 50) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_position_that_is_not_a_number_declines(self, monkeypatch):
        """A value that cannot be a percentage is not sent anywhere."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)

        assert (
            await trv_zbt.override_set_valve(mock_self, ENTITY, cast(int, "x")) is False
        )
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_write_declines(self, monkeypatch):
        """A register that errors on the write is reported as declined."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device asleep")
        )

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 50) is False

    @pytest.mark.asyncio
    async def test_a_partial_write_declines(self, monkeypatch):
        """One register taken and one refused is not a served position."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=[None, HomeAssistantError("entity gone")]
        )

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 50) is False


class TestValveChannel:
    """The channel is offered while the device publishes a register."""

    def test_the_channel_follows_the_registers(self, monkeypatch):
        """Both registers present make the channel, none leaves it out."""
        with_registers = _make_self()
        _patch_registry(monkeypatch, with_registers)
        assert trv_zbt.has_valve_channel(with_registers, ENTITY) is True

        without = _make_self(entries=[_entry(ENTITY), _entry(SENSOR_SELECT)])
        monkeypatch.setattr(trv_zbt.er, "async_get", lambda hass: without._registry)
        assert trv_zbt.has_valve_channel(without, ENTITY) is False

    def test_a_trv_without_a_registry_entry_has_no_channel(self, monkeypatch):
        """An entity the registry does not hold names no register."""
        mock_self = _make_self(entries=[_entry(HEATING_VALVE), _entry(IDLE_VALVE)])
        _patch_registry(monkeypatch, mock_self)

        assert trv_zbt.has_valve_channel(mock_self, ENTITY) is False

    def test_a_trv_without_a_device_has_no_channel(self, monkeypatch):
        """A TRV the registry lists without a device names none either."""
        mock_self = _make_self(
            entries=[
                make_registry_entry(ENTITY, device_id=None),
                _entry(HEATING_VALVE),
                _entry(IDLE_VALVE),
            ]
        )
        _patch_registry(monkeypatch, mock_self)

        assert trv_zbt.has_valve_channel(mock_self, ENTITY) is False

    @pytest.mark.asyncio
    async def test_a_disabled_register_is_not_a_channel(self, monkeypatch):
        """A register disabled in Home Assistant drops every write."""
        mock_self = _make_self(
            entries=[
                _entry(ENTITY),
                _entry(HEATING_VALVE, disabled_by=er.RegistryEntryDisabler.USER),
                _entry(IDLE_VALVE),
            ]
        )
        _patch_registry(monkeypatch, mock_self)

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 40) is True
        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": IDLE_VALVE, "value": 40})
        ]

    @pytest.mark.asyncio
    async def test_a_register_the_registry_lost_is_skipped(self, monkeypatch):
        """A register discovery names but the registry no longer holds is skipped."""
        mock_self = _make_self()
        _patch_registry(monkeypatch, mock_self)
        found = {
            "heating_valve_position": "number.trv1_vanished",
            "idle_valve_position": IDLE_VALVE,
        }
        monkeypatch.setattr(
            trv_zbt,
            "find_device_entity",
            lambda registry, device_id, domains, keywords: found.get(keywords[0]),
        )

        assert await trv_zbt.override_set_valve(mock_self, ENTITY, 25) is True
        assert _service_payloads(mock_self) == [
            ("number", "set_value", {"entity_id": IDLE_VALVE, "value": 25})
        ]


class TestInitialTweak:
    """Startup frees the registers from the device's own PID mode."""

    @pytest.fixture
    def default_tweak(self, monkeypatch):
        """Neutralize the default tweak the quirk chains to."""
        called = AsyncMock()
        monkeypatch.setattr(trv_zbt, "default_initial_tweak", called)
        return called

    @pytest.mark.asyncio
    async def test_a_direct_valve_trv_switches_the_device_pid_mode_off(
        self, monkeypatch, default_tweak
    ):
        """The mode that keeps the registers does not stay on."""
        mock_self = _make_self(
            states={PID_SWITCH: STATE_ON}, advanced=DIRECT_VALVE_ADVANCED
        )
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        default_tweak.assert_awaited_once_with(mock_self, ENTITY)
        assert _service_payloads(mock_self) == [
            ("switch", "turn_off", {"entity_id": PID_SWITCH})
        ]

    @pytest.mark.asyncio
    async def test_a_trv_on_another_calibration_leaves_the_pid_mode_alone(
        self, monkeypatch, default_tweak
    ):
        """A TRV the device still regulates for keeps its PID mode."""
        mock_self = _make_self(states={PID_SWITCH: STATE_ON})
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        default_tweak.assert_awaited_once_with(mock_self, ENTITY)
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_released_trv_keeps_its_pid_mode(self, monkeypatch, default_tweak):
        """The device is the user's, so startup writes nothing to it."""
        mock_self = _make_self(
            states={PID_SWITCH: STATE_ON}, advanced=DIRECT_VALVE_ADVANCED
        )
        _patch_registry(monkeypatch, mock_self)
        mock_self.devices_released = True

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_pid_mode_already_off_sees_no_write(
        self, monkeypatch, default_tweak
    ):
        """A device that already runs without PID is left as it is."""
        mock_self = _make_self(
            states={PID_SWITCH: STATE_OFF}, advanced=DIRECT_VALVE_ADVANCED
        )
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_device_without_the_pid_switch_sees_no_write(
        self, monkeypatch, default_tweak
    ):
        """A device that names no PID switch cannot be asked to change it."""
        mock_self = _make_self(
            entries=[_entry(ENTITY), _entry(HEATING_VALVE), _entry(IDLE_VALVE)],
            advanced=DIRECT_VALVE_ADVANCED,
        )
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_pid_switch_without_a_state_sees_no_write(
        self, monkeypatch, default_tweak
    ):
        """A switch that reports no state is not assumed to be on."""
        mock_self = _make_self(advanced=DIRECT_VALVE_ADVANCED)
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_trv_the_registry_does_not_hold_sees_no_write(
        self, monkeypatch, default_tweak
    ):
        """An unregistered TRV has no device to find the switch on."""
        mock_self = _make_self(
            entries=[_entry(PID_SWITCH)], advanced=DIRECT_VALVE_ADVANCED
        )
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_trv_without_a_device_sees_no_write(
        self, monkeypatch, default_tweak
    ):
        """A registry entry without a device cannot be scanned for siblings."""
        mock_self = _make_self(
            entries=[make_registry_entry(ENTITY, device_id=None), _entry(PID_SWITCH)],
            advanced=DIRECT_VALVE_ADVANCED,
        )
        _patch_registry(monkeypatch, mock_self)

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_pid_write_is_reported(self, monkeypatch, default_tweak):
        """A device that refuses the switch write does not fail startup."""
        mock_self = _make_self(
            states={PID_SWITCH: STATE_ON}, advanced=DIRECT_VALVE_ADVANCED
        )
        _patch_registry(monkeypatch, mock_self)
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("entity gone")
        )

        await trv_zbt.initial_tweak(mock_self, ENTITY)

        mock_self.hass.services.async_call.assert_awaited_once()


class TestTheTrvzbStaysOffTheRegisters:
    """The TRVZB module keeps its opening and closing degrees away from the TRV-ZBT."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", ["TRV-ZBT", "SONOFF TRV-ZBT"])
    async def test_the_trvzb_valve_write_skips_the_trv_zbt(self, monkeypatch, model):
        """Called anyway, the TRVZB valve write declines without a service call.

        A model string that also names the vendor passes the TRVZB's Sonoff
        match, so it is the explicit TRV-ZBT check that turns it down.
        """
        mock_self = _make_self()
        mock_self.real_trvs[ENTITY].model = model
        monkeypatch.setattr(trvzb.er, "async_get", lambda hass: mock_self._registry)

        assert (
            await trvzb.maybe_set_sonoff_valve_percent(mock_self, ENTITY, 40) is False
        )
        assert trvzb.has_valve_channel(mock_self, ENTITY) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model_id", "model", "found"),
        [
            ("TRV-ZBT", None, None),
            ("trv-zbt", None, None),
            ("OTHER-TRV", None, HEATING_VALVE),
            (None, "TRV-ZBT", None),
            (None, "TRV-ZBT (Thermostatic radiator valve)", None),
            (None, "OTHER-TRV", HEATING_VALVE),
        ],
    )
    async def test_the_valve_lookup_turns_down_only_the_trv_zbt(
        self, model_id, model, found
    ):
        """The adapter finds no helper on a TRV-ZBT, in any letter case.

        The quirk writes both registers itself, so a single adapter entity
        would carry half the demand. Zigbee2MQTT registers the model as
        model_id, ZHA as model, so either field names the TRV-ZBT; the same
        entities on a device of another model are still found.
        """
        bt = ThermostatStandIn()
        device = dr.DeviceEntry(
            config_entry_id="entry",
            model_id=model_id,
            model=model,
            identifiers={("mqtt", DEVICE)},
        )
        registry = make_entity_registry(*_trv_zbt_entries())

        with (
            patch(
                "custom_components.better_thermostat.utils.helpers.er.async_get",
                return_value=registry,
            ),
            patch(
                "custom_components.better_thermostat.utils.helpers.dr.async_get"
            ) as dev_reg,
        ):
            dev_reg.return_value.async_get.return_value = device
            result = await find_valve_entity(bt, ENTITY)

        assert (result or {}).get("entity_id") == found
