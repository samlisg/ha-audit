"""HA Audit — read-only audit of your Home Assistant configuration.

Security model
  * Nothing is written to /config/www (that folder is served WITHOUT login).
  * The report lives in /config/.storage/ha_audit (not web-served).
  * The report is delivered to the UI over the authenticated WebSocket API and is
    admin-only. The panel's JS file is public but contains no data.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, persistent_notification
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import engine
from .const import (
    CHECK_EVERY_HOURS, CONF_ANALYZE_DB, CONF_INCLUDE_DIAGNOSTIC, CONF_INTERVAL_DAYS,
    CONF_NOTIFY, CONF_STALE_DAYS, CONF_UNAVAIL_DAYS, DEFAULTS, DOMAIN, EVENT_COMPLETED,
    PANEL_ELEMENT, PANEL_FALLBACK_PATH, PANEL_URL_PATH, SIGNAL_UPDATED, STARTUP_DELAY_SECONDS, STATIC_URL,
    STORAGE_KEY, STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

_PANEL_JS = Path(__file__).parent / "frontend" / "ha-audit-panel.js"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


type HaAuditConfigEntry = ConfigEntry[AuditRuntime]


class AuditRuntime:
    """Holds the latest report, runs audits and persists results."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.report: dict[str, Any] | None = None
        self.ids: list[str] = []
        self.panel_path: str | None = None   # only set when WE registered the panel
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    @property
    def options(self) -> dict[str, Any]:
        return {**DEFAULTS, **self.entry.options}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self.report = data.get("report")
            self.ids = data.get("ids") or []

    def last_run(self) -> datetime | None:
        if not self.report:
            return None
        return dt_util.parse_datetime(self.report.get("generated", ""))

    def is_due(self) -> bool:
        days = int(self.options[CONF_INTERVAL_DAYS])
        if days <= 0:
            return False
        last = self.last_run()
        return last is None or dt_util.utcnow() - last >= timedelta(days=days)

    def _snapshot_states(self) -> list[dict[str, Any]]:
        """Copy live states into plain dicts (must run in the event loop)."""
        out = []
        for s in self.hass.states.async_all():
            attrs = {k: (v.isoformat() if isinstance(v, datetime) else v)
                     for k, v in s.attributes.items()
                     if isinstance(v, (str, int, float, bool, datetime, type(None)))}
            out.append({
                "entity_id": s.entity_id, "state": s.state, "attributes": attrs,
                "last_changed": s.last_changed.isoformat(),
                "last_updated": s.last_updated.isoformat(),
            })
        return out

    async def async_run(self) -> dict[str, Any]:
        """Run an audit (serialised) and publish the result."""
        if self._lock.locked():
            # A run is already in progress: wait for it and return its result.
            async with self._lock:
                return self.report or {}
        async with self._lock:
            opts = self.options
            engine_opts = {
                "stale_days": opts[CONF_STALE_DAYS],
                "unavail_days": opts[CONF_UNAVAIL_DAYS],
                "include_diagnostic": opts[CONF_INCLUDE_DIAGNOSTIC],
                "analyze_db": opts[CONF_ANALYZE_DB],
            }
            states = self._snapshot_states()
            try:
                report, ids = await self.hass.async_add_executor_job(
                    partial(engine.run_audit, self.hass.config.config_dir,
                            engine_opts, states, self.ids or None))
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("HA Audit failed")
                raise HomeAssistantError(f"HA Audit failed: {err}") from err
            self.report, self.ids = report, ids
            await self._store.async_save({"report": report, "ids": ids})
            summary = report["summary"]
            async_dispatcher_send(self.hass, SIGNAL_UPDATED)
            self.hass.bus.async_fire(EVENT_COMPLETED, {
                "total": summary["total"], "new": summary["new"], **summary["counts"]})
            if opts[CONF_NOTIFY] and summary["new"] > 0:
                persistent_notification.async_create(
                    self.hass,
                    f"{summary['new']} new finding(s), {summary['total']} in total."
                    + (f" [Open HA Audit](/{self.panel_path})" if self.panel_path else ""),
                    title="🏠 HA Audit",
                    notification_id=f"{DOMAIN}_new",
                )
            return report


# ── setup ────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.data.setdefault(DOMAIN, {})
    websocket_api.async_register_command(hass, ws_report)
    websocket_api.async_register_command(hass, ws_run)

    async def _svc_run(call: ServiceCall) -> None:
        await _runtime(hass).async_run()

    hass.services.async_register(DOMAIN, "run", _svc_run)
    return True


def _runtime(hass: HomeAssistant) -> AuditRuntime:
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries or not hasattr(entries[0], "runtime_data"):
        raise HomeAssistantError("HA Audit is not set up")
    return entries[0].runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: HaAuditConfigEntry) -> bool:
    runtime = AuditRuntime(hass, entry)
    await runtime.async_load()
    entry.runtime_data = runtime

    runtime.panel_path = await _async_register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    def _launch() -> None:
        if runtime.is_due() and not runtime.running:
            entry.async_create_background_task(hass, _safe_run(runtime), "ha_audit_scheduled")

    @callback
    def _tick(_now: datetime) -> None:
        _launch()

    async def _started(_hass: HomeAssistant) -> None:
        entry.async_on_unload(async_call_later(hass, STARTUP_DELAY_SECONDS, lambda _n: _launch()))

    entry.async_on_unload(async_at_started(hass, _started))
    entry.async_on_unload(
        async_track_time_interval(hass, _tick, timedelta(hours=CHECK_EVERY_HOURS)))
    return True


async def _safe_run(runtime: AuditRuntime) -> None:
    with contextlib.suppress(HomeAssistantError):  # already logged in async_run
        await runtime.async_run()


async def async_unload_entry(hass: HomeAssistant, entry: HaAuditConfigEntry) -> bool:
    # Only remove a panel we registered ourselves — never someone else's dashboard.
    if entry.runtime_data.panel_path:
        frontend.async_remove_panel(hass, entry.runtime_data.panel_path)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_panel(hass: HomeAssistant) -> str | None:
    """Register the sidebar panel. Returns the URL path used, or None on failure."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get("static"):
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), True)])
        domain_data["static"] = True
    # Cache-bust by FILE CONTENT (not by version): the static path is served with long cache headers,
    # so a changed panel file must get a new URL or browsers keep running the old one.
    asset_v = await hass.async_add_executor_job(_file_hash, _PANEL_JS)
    for path in (PANEL_URL_PATH, PANEL_FALLBACK_PATH):
        try:
            await panel_custom.async_register_panel(
                hass,
                webcomponent_name=PANEL_ELEMENT,
                frontend_url_path=path,
                module_url=f"{STATIC_URL}/ha-audit-panel.js?v={asset_v}",
                sidebar_title="HA Audit",
                sidebar_icon="mdi:clipboard-check-outline",
                require_admin=True,
                config={},
            )
        except ValueError:
            _LOGGER.warning(
                "The URL /%s is already used by another panel or dashboard; trying the next one. "
                "To use /%s, delete that dashboard (Settings → Dashboards).", path, PANEL_URL_PATH)
            continue
        if path != PANEL_URL_PATH:
            _LOGGER.warning("HA Audit panel registered at /%s", path)
        return path
    _LOGGER.error("Could not register the HA Audit panel; sensors and services still work.")
    return None


# ── WebSocket API (authenticated, admin only) ────────────────────────────────

@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/report"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_report(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                    msg: dict[str, Any]) -> None:
    try:
        rt = _runtime(hass)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_loaded", str(err))
        return
    connection.send_result(msg["id"], {"report": rt.report, "running": rt.running})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/run"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_run(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                 msg: dict[str, Any]) -> None:
    try:
        report = await _runtime(hass).async_run()
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "run_failed", str(err))
        return
    connection.send_result(msg["id"], {"report": report, "running": False})
