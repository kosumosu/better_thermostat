"""Tests for the Sonoff TRV-ZBT valve position model quirks."""

import importlib
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError
import pytest

TRV = "climate.sonoff_termostat_pravyi"
HEAT_NUMBER = "number.sonoff_termostat_pravyi_heating_valve_position"
IDLE_NUMBER = "number.sonoff_termostat_pravyi_idle_valve_position"


@pytest.fixture
def quirk():
    """Provide the TRV-ZBT quirks module (hyphenated, so import by name)."""
    return importlib.import_module(
        "custom_components.better_thermostat.model_fixes.TRV-ZBT"
    )


def _make_self(states=None, trv=True, reg_device_id="device1"):
    """Create a mock BetterThermostat and the registry lookup it consults.

    Parameters
    ----------
    states :
        Mapping of entity id to state string for ``hass.states.get``.
    trv :
        Whether the TRV has a loaded ``real_trvs`` record.
    reg_device_id :
        Device id the registry reports for the TRV, or None for no device.
    """
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = Mock()
    mock_self.real_trvs = {TRV: Mock()} if trv else {}
    mock_self.hass.services.async_call = AsyncMock()
    state_objects = {}
    for entity_id, state in (states or {}).items():
        state_mock = Mock()
        state_mock.state = state
        state_objects[entity_id] = state_mock
    mock_self.hass.states.get = state_objects.get

    registry = Mock()
    if reg_device_id is None:
        registry.async_get = Mock(return_value=None)
    else:
        reg_entry = Mock()
        reg_entry.device_id = reg_device_id
        registry.async_get = Mock(return_value=reg_entry)
    return mock_self, registry


def _patch_discovery(quirk, monkeypatch, registry, registers):
    """Wire the entity registry and the sibling lookup of a quirks module.

    Parameters
    ----------
    quirk :
        The quirks module under test.
    monkeypatch :
        The pytest monkeypatch to register replacements on.
    registry :
        Mock entity registry returned by ``er.async_get``.
    registers :
        Mapping of discovery keyword to entity id the sibling lookup finds.
    """
    monkeypatch.setattr(quirk.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        quirk,
        "find_device_entity",
        lambda er_, device_id, domains, keywords: registers.get(keywords[0]),
    )


class TestOverrideSetValve:
    """The quirk commands both running-state registers with the demand."""

    @pytest.mark.asyncio
    async def test_both_registers_written(self, quirk, monkeypatch):
        """A demanded opening goes to the heat and the idle register."""
        mock_self, registry = _make_self()
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {"heating_valve_position": HEAT_NUMBER, "idle_valve_position": IDLE_NUMBER},
        )

        handled = await quirk.override_set_valve(mock_self, TRV, 42)

        assert handled is True
        calls = mock_self.hass.services.async_call.await_args_list
        assert [call.args[2]["entity_id"] for call in calls] == [
            HEAT_NUMBER,
            IDLE_NUMBER,
        ]
        assert all(call.args[2]["value"] == 42 for call in calls)
        for call in calls:
            assert call.args[:2] == ("number", "set_value")
            assert call.kwargs["blocking"] is True
            assert call.kwargs["context"] is mock_self.context

    @pytest.mark.asyncio
    async def test_percent_is_clamped(self, quirk, monkeypatch):
        """Demands outside 0..100 reach the device clamped, not raw."""
        mock_self, registry = _make_self()
        _patch_discovery(
            quirk, monkeypatch, registry, {"heating_valve_position": HEAT_NUMBER}
        )

        assert await quirk.override_set_valve(mock_self, TRV, 150) is True
        assert await quirk.override_set_valve(mock_self, TRV, -5) is True

        calls = mock_self.hass.services.async_call.await_args_list
        assert [call.args[2]["value"] for call in calls] == [100, 0]

    @pytest.mark.asyncio
    async def test_single_named_register_is_enough(self, quirk, monkeypatch):
        """A device naming only one register is fully served by it."""
        mock_self, registry = _make_self()
        _patch_discovery(
            quirk, monkeypatch, registry, {"idle_valve_position": IDLE_NUMBER}
        )

        handled = await quirk.override_set_valve(mock_self, TRV, 30)

        assert handled is True
        call = mock_self.hass.services.async_call.call_args
        assert call.args[2] == {"entity_id": IDLE_NUMBER, "value": 30}

    @pytest.mark.asyncio
    async def test_no_register_declines(self, quirk, monkeypatch):
        """Without a valve register the quirk declines to the adapter."""
        mock_self, registry = _make_self()
        _patch_discovery(quirk, monkeypatch, registry, {})

        assert await quirk.override_set_valve(mock_self, TRV, 50) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_trv_declines(self, quirk, monkeypatch):
        """A TRV without a loaded record cannot be commanded."""
        mock_self, registry = _make_self(trv=False)
        _patch_discovery(
            quirk, monkeypatch, registry, {"heating_valve_position": HEAT_NUMBER}
        )

        assert await quirk.override_set_valve(mock_self, TRV, 50) is False

    @pytest.mark.asyncio
    async def test_unregistered_trv_declines(self, quirk, monkeypatch):
        """A TRV the registry does not know declines without writing."""
        mock_self, registry = _make_self(reg_device_id=None)
        _patch_discovery(
            quirk, monkeypatch, registry, {"heating_valve_position": HEAT_NUMBER}
        )

        assert await quirk.override_set_valve(mock_self, TRV, 50) is False
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refused_write_declines(self, quirk, monkeypatch):
        """A register that errors on the write is reported as declined."""
        mock_self, registry = _make_self()
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {"heating_valve_position": HEAT_NUMBER, "idle_valve_position": IDLE_NUMBER},
        )
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=HomeAssistantError("device asleep")
        )

        assert await quirk.override_set_valve(mock_self, TRV, 50) is False

    @pytest.mark.asyncio
    async def test_partial_failure_declines(self, quirk, monkeypatch):
        """One register taken and one refused is not a served position."""
        mock_self, registry = _make_self()
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {"heating_valve_position": HEAT_NUMBER, "idle_valve_position": IDLE_NUMBER},
        )
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=[None, HomeAssistantError("entity gone")]
        )

        assert await quirk.override_set_valve(mock_self, TRV, 50) is False

    def test_quirk_unlocks_direct_valve(self, quirk):
        """Carrying the quirk is what offers direct valve in the flow."""
        from custom_components.better_thermostat.model_fixes.model_quirks import (
            quirk_writes_valve,
        )

        assert quirk_writes_valve(quirk) is True


class TestInitialTweak:
    """Startup protects the dual-register valve channel from device modes."""

    @pytest.fixture
    def default_tweak(self, quirk, monkeypatch):
        """Neutralize the default tweak the quirk chains to."""
        called = AsyncMock()
        monkeypatch.setattr(quirk, "default_initial_tweak", called)
        return called

    @pytest.mark.asyncio
    async def test_chains_default_and_leaves_healthy_device(
        self, quirk, monkeypatch, default_tweak
    ):
        """A device on its local sensor without PID mode gets no writes."""
        mock_self, registry = _make_self(
            states={
                "select.trv_temperature_sensor_select": "local_temperature",
                "switch.trv_smart_temperature_control": "off",
            }
        )
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {
                "temperature_sensor_select": "select.trv_temperature_sensor_select",
                "smart_temperature_control": "switch.trv_smart_temperature_control",
            },
        )

        await quirk.initial_tweak(mock_self, TRV)

        default_tweak.assert_awaited_once_with(mock_self, TRV)
        mock_self.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parked_remote_sensor_is_handed_back(
        self, quirk, monkeypatch, default_tweak
    ):
        """A sensor stuck on the remote value returns to the built-in one."""
        mock_self, registry = _make_self(
            states={
                "select.trv_temperature_sensor_select": "remote_temperature",
                "switch.trv_smart_temperature_control": "off",
            }
        )
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {
                "temperature_sensor_select": "select.trv_temperature_sensor_select",
                "smart_temperature_control": "switch.trv_smart_temperature_control",
            },
        )

        await quirk.initial_tweak(mock_self, TRV)

        call = mock_self.hass.services.async_call.call_args
        assert call.args[:2] == ("select", "select_option")
        assert call.args[2] == {
            "entity_id": "select.trv_temperature_sensor_select",
            "option": "local_temperature",
        }
        assert call.kwargs["blocking"] is True

    @pytest.mark.asyncio
    async def test_device_pid_mode_is_switched_off(
        self, quirk, monkeypatch, default_tweak
    ):
        """The device PID mode that hides both registers gets disabled."""
        mock_self, registry = _make_self(
            states={
                "select.trv_temperature_sensor_select": "local_temperature",
                "switch.trv_smart_temperature_control": "on",
            }
        )
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {
                "temperature_sensor_select": "select.trv_temperature_sensor_select",
                "smart_temperature_control": "switch.trv_smart_temperature_control",
            },
        )

        await quirk.initial_tweak(mock_self, TRV)

        call = mock_self.hass.services.async_call.call_args
        assert call.args[:2] == ("switch", "turn_off")
        assert call.args[2] == {"entity_id": "switch.trv_smart_temperature_control"}

    @pytest.mark.asyncio
    async def test_offline_remote_source_is_left_alone(
        self, quirk, monkeypatch, default_tweak
    ):
        """The device falls back by itself; the quirk does not fight it."""
        mock_self, registry = _make_self(
            states={
                "select.trv_temperature_sensor_select": "remote_source_offline",
                "switch.trv_smart_temperature_control": "off",
            }
        )
        _patch_discovery(
            quirk,
            monkeypatch,
            registry,
            {
                "temperature_sensor_select": "select.trv_temperature_sensor_select",
                "smart_temperature_control": "switch.trv_smart_temperature_control",
            },
        )

        await quirk.initial_tweak(mock_self, TRV)

        mock_self.hass.services.async_call.assert_not_awaited()
