"""Summary sensors for HA Audit (small numbers only — the report itself stays private)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATED


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    rt = entry.runtime_data
    async_add_entities([
        FindingsSensor(rt, "findings", "Findings", "mdi:clipboard-alert-outline"),
        NewFindingsSensor(rt, "new_findings", "New findings", "mdi:new-box"),
        LastRunSensor(rt, "last_run", "Last run", "mdi:clock-check-outline"),
    ])


class _Base(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, rt, key: str, name: str, icon: str) -> None:
        self._rt = rt
        self._attr_unique_id = f"{rt.entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, rt.entry.entry_id)}, name="HA Audit",
            entry_type=DeviceEntryType.SERVICE)

    async def async_added_to_hass(self) -> None:
        @callback
        def _update() -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, _update))

    @property
    def _summary(self) -> dict[str, Any] | None:
        return (self._rt.report or {}).get("summary")


class FindingsSensor(_Base):
    @property
    def native_value(self) -> int | None:
        s = self._summary
        return None if s is None else s["total"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        s = self._summary
        return None if s is None else dict(s["counts"])


class NewFindingsSensor(_Base):
    @property
    def native_value(self) -> int | None:
        s = self._summary
        return None if s is None else s["new"]


class LastRunSensor(_Base):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self._rt.last_run()
