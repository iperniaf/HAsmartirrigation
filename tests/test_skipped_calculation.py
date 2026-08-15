"""Tests for calculation windows skipped by the master switch."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.smart_irrigation import SmartIrrigationCoordinator, const


def _coordinator_for_skipped_calculation(zones):
    """Build a coordinator with only the dependencies used by the test."""
    coordinator = SmartIrrigationCoordinator.__new__(SmartIrrigationCoordinator)
    coordinator.hass = Mock()
    coordinator.store = Mock()
    coordinator.store.async_get_zones = AsyncMock(return_value=zones)
    coordinator.store.async_update_config = AsyncMock()
    coordinator.store.async_update_mapping = AsyncMock()
    coordinator.store.async_update_zone = AsyncMock()
    coordinator.master_switch_is_on = Mock(return_value=False)
    return coordinator


@pytest.mark.asyncio
async def test_skipped_calculation_closes_window_without_changing_bucket():
    """An off master switch clears samples and sets automatic durations to zero."""
    zones = [
        {
            const.ZONE_ID: 1,
            const.ZONE_STATE: const.ZONE_STATE_AUTOMATIC,
            const.ZONE_MAPPING: 7,
            const.ZONE_BUCKET: -12.5,
            const.ZONE_DURATION: 900,
        },
        {
            const.ZONE_ID: 2,
            const.ZONE_STATE: const.ZONE_STATE_DISABLED,
            const.ZONE_MAPPING: 8,
            const.ZONE_BUCKET: -4.0,
            const.ZONE_DURATION: 300,
        },
    ]
    coordinator = _coordinator_for_skipped_calculation(zones)

    with patch(
        "custom_components.smart_irrigation.calculation.async_dispatcher_send"
    ) as dispatch:
        await coordinator._async_calculate_all(delete_weather_data=True)

    mapping_changes = coordinator.store.async_update_mapping.await_args.args
    assert mapping_changes[0] == 7
    assert mapping_changes[1][const.MAPPING_DATA] == []
    last_calculation = mapping_changes[1][const.MAPPING_DATA_LAST_CALCULATION]
    assert isinstance(last_calculation[const.MAPPING_TIMESTAMP], datetime)

    zone_changes = coordinator.store.async_update_zone.await_args.args
    assert zone_changes[0] == 1
    assert zone_changes[1][const.ZONE_DURATION] == 0
    assert const.ZONE_BUCKET not in zone_changes[1]
    assert const.ZONE_DELTA not in zone_changes[1]
    assert dispatch.call_count == 1


@pytest.mark.asyncio
async def test_skipped_calculation_resets_multiplier_baseline():
    """The next calculation starts from the skipped calculation timestamp."""
    old_calculation = datetime.now() - timedelta(days=2)
    zones = [
        {
            const.ZONE_ID: 1,
            const.ZONE_STATE: const.ZONE_STATE_AUTOMATIC,
            const.ZONE_MAPPING: 7,
        }
    ]
    coordinator = _coordinator_for_skipped_calculation(zones)

    with patch("custom_components.smart_irrigation.calculation.async_dispatcher_send"):
        await coordinator._async_calculate_all(delete_weather_data=True)

    changes = coordinator.store.async_update_mapping.await_args.args[1]
    new_calculation = changes[const.MAPPING_DATA_LAST_CALCULATION][
        const.MAPPING_TIMESTAMP
    ]

    assert new_calculation > old_calculation
    assert changes[const.MAPPING_DATA] == []

    # A subsequent hourly sample is measured from the new baseline, not from
    # the last real calculation before the master switch was disabled.
    multiplier = coordinator._calc_hour_multiplier(
        {
            const.RETRIEVED_AT: [datetime.now()],
        },
        {
            const.MAPPING_DATA_LAST_CALCULATION: {
                const.MAPPING_TIMESTAMP: new_calculation,
            }
        },
    )
    assert multiplier < 1 / 24


@pytest.mark.asyncio
async def test_active_master_switch_stop_empties_buckets_and_locks_day():
    """An emergency stop empties every bucket and blocks watering for today."""
    zones = [
        {
            const.ZONE_ID: 1,
            const.ZONE_BUCKET: -8.0,
            const.ZONE_DURATION: 1800,
        },
        {
            const.ZONE_ID: 2,
            const.ZONE_BUCKET: 4.0,
            const.ZONE_DURATION: 600,
        },
    ]
    coordinator = _coordinator_for_skipped_calculation(zones)
    coordinator._running_valves = {2: "switch.zone_2"}
    coordinator._active_valve_runs = {2: {"entity": "switch.zone_2"}}
    coordinator._running_master_valve = None
    coordinator._valve_run_tasks = set()
    coordinator._emergency_stop_today = False
    coordinator.async_stop_direct_valves = AsyncMock()

    event = SimpleNamespace(
        data={"new_state": SimpleNamespace(state="off")},
    )
    with patch("custom_components.smart_irrigation.async_dispatcher_send") as dispatch:
        await coordinator._async_master_switch_changed(event)

    coordinator.store.async_update_config.assert_awaited_once_with(
        {const.EMERGENCY_STOP_TODAY: True}
    )
    coordinator.async_stop_direct_valves.assert_awaited_once()
    assert coordinator._emergency_stop_today is True
    assert coordinator.store.async_update_zone.await_count == 2
    for call in coordinator.store.async_update_zone.await_args_list:
        assert call.args[1] == {const.ZONE_BUCKET: 0, const.ZONE_DURATION: 0}
    assert dispatch.call_count == 3


@pytest.mark.asyncio
async def test_master_switch_stop_without_active_run_does_not_empty_buckets():
    """A normal master switch shutdown only stops valves, without an emergency."""
    coordinator = _coordinator_for_skipped_calculation([])
    coordinator._running_valves = {}
    coordinator._active_valve_runs = {}
    coordinator._running_master_valve = None
    coordinator._valve_run_tasks = set()
    coordinator._emergency_stop_today = False
    coordinator.async_stop_direct_valves = AsyncMock()

    event = SimpleNamespace(
        data={"new_state": SimpleNamespace(state="off")},
    )
    await coordinator._async_master_switch_changed(event)

    coordinator.async_stop_direct_valves.assert_awaited_once()
    coordinator.store.async_update_config.assert_not_awaited()
    coordinator.store.async_update_zone.assert_not_awaited()
    assert coordinator._emergency_stop_today is False


@pytest.mark.asyncio
async def test_emergency_stop_blocks_direct_valve_control():
    """Direct control refuses to start while the daily emergency lock is set."""
    coordinator = _coordinator_for_skipped_calculation([])
    coordinator.master_switch_is_on = Mock(return_value=True)
    coordinator._emergency_stop_today = True
    coordinator.store.config = SimpleNamespace(
        **{const.CONF_DIRECT_VALVE_CONTROL_ENABLED: True}
    )

    await coordinator.async_run_direct_valves()

    coordinator.store.async_get_zones.assert_not_awaited()
