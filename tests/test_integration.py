"""Integration tests — need pytest-homeassistant-custom-component (Python 3.13)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry, async_capture_events,
)

from custom_components.ha_audit.const import DOMAIN  # noqa: E402

from .fake_config import build  # noqa: E402

PANEL_JS = (Path(__file__).resolve().parent.parent / "custom_components" / "ha_audit" / "frontend"
            / "ha-audit-panel.js")


@pytest.fixture
async def setup(hass, tmp_path):
    build(tmp_path)
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"interval_days": 0, "analyze_db": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_service_run_updates_sensors_and_fires_event(hass, setup):
    events = async_capture_events(hass, "ha_audit_completed")
    # live state with a datetime attribute, exactly as automations expose last_triggered
    hass.states.async_set("automation.light_on", "on", {
        "friendly_name": "Light on", "last_triggered": datetime.now(timezone.utc) - timedelta(days=100)})
    await hass.services.async_call(DOMAIN, "run", blocking=True)
    await hass.async_block_till_done()

    total = hass.states.get("sensor.ha_audit_findings")
    new = hass.states.get("sensor.ha_audit_new_findings")
    last = hass.states.get("sensor.ha_audit_last_run")
    assert total and int(total.state) > 0
    assert new and int(new.state) == int(total.state)         # first run: everything is new
    assert last and last.state not in ("unknown", "unavailable")
    assert "ghost" in total.attributes and total.attributes["fe"] == 1
    assert len(events) == 1 and events[0].data["total"] == int(total.state)


async def test_second_run_has_no_new(hass, setup):
    await hass.services.async_call(DOMAIN, "run", blocking=True)
    await hass.services.async_call(DOMAIN, "run", blocking=True)
    await hass.async_block_till_done()
    assert int(hass.states.get("sensor.ha_audit_new_findings").state) == 0


async def test_websocket_report_admin_only(hass, hass_ws_client, hass_read_only_access_token, setup):
    admin = await hass_ws_client(hass)
    await admin.send_json({"id": 1, "type": "ha_audit/report"})
    res = await admin.receive_json()
    assert res["success"] and res["result"]["report"] is None      # nothing run yet

    await admin.send_json({"id": 2, "type": "ha_audit/run"})
    res = await admin.receive_json()
    assert res["success"]
    keys = {s["key"] for s in res["result"]["report"]["sections"]}
    assert {"fe", "ent", "ghost"} <= keys

    await admin.send_json({"id": 3, "type": "ha_audit/report"})
    assert (await admin.receive_json())["result"]["report"] is not None

    user = await hass_ws_client(hass, hass_read_only_access_token)
    await user.send_json({"id": 1, "type": "ha_audit/report"})
    res = await user.receive_json()
    assert not res["success"] and res["error"]["code"] == "unauthorized"


async def test_panel_registered_admin_only_and_unloads(hass, setup):
    panels = hass.data["frontend_panels"]
    assert "ha-audit" in panels and panels["ha-audit"].require_admin is True
    assert await hass.config_entries.async_unload(setup.entry_id)
    await hass.async_block_till_done()
    assert "ha-audit" not in hass.data["frontend_panels"]
    # re-setup must not crash on the already registered static path
    assert await hass.config_entries.async_setup(setup.entry_id)


async def test_report_persisted_across_restart(hass, setup):
    await hass.services.async_call(DOMAIN, "run", blocking=True)
    await hass.async_block_till_done()
    total = hass.states.get("sensor.ha_audit_findings").state
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.ha_audit_findings").state == total


async def test_panel_url_conflict_falls_back_and_never_touches_foreign_panel(hass, tmp_path):
    """Regression: a dashboard already owns /ha-audit → we must not crash nor remove it."""
    from homeassistant.components import frontend

    build(tmp_path)
    hass.config.config_dir = str(tmp_path)
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "frontend", {})
    frontend.async_register_built_in_panel(
        hass, "iframe", sidebar_title="Old", frontend_url_path="ha-audit",
        config={"url": "/local/old.html"})

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"interval_days": 0, "analyze_db": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)        # must not raise
    await hass.async_block_till_done()

    panels = hass.data["frontend_panels"]
    assert panels["ha-audit"].component_name == "iframe"                # foreign panel untouched
    assert "ha-audit-report" in panels and panels["ha-audit-report"].require_admin is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert "ha-audit" in hass.data["frontend_panels"]                   # still there after unload
    assert "ha-audit-report" not in hass.data["frontend_panels"]


async def test_config_flow_creates_entry_with_int_options_and_blocks_second(hass, tmp_path):
    build(tmp_path)
    hass.config.config_dir = str(tmp_path)
    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert res["type"] == "form"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {
        "interval_days": 3.0, "stale_days": 45.0, "unavail_days": 1.0,
        "analyze_db": False, "include_diagnostic": True, "notify": False})
    assert res["type"] == "create_entry"
    assert res["options"]["interval_days"] == 3 and isinstance(res["options"]["stale_days"], int)
    await hass.async_block_till_done()
    res2 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert res2["type"] == "abort"


async def test_options_flow_updates_and_reloads(hass, setup):
    res = await hass.config_entries.options.async_init(setup.entry_id)
    assert res["type"] == "form"
    res = await hass.config_entries.options.async_configure(res["flow_id"], {
        "interval_days": 14.0, "stale_days": 90.0, "unavail_days": 2.0,
        "analyze_db": False, "include_diagnostic": False, "notify": True})
    assert res["type"] == "create_entry"
    await hass.async_block_till_done()
    assert setup.options["stale_days"] == 90 and isinstance(setup.options["stale_days"], int)
    assert setup.runtime_data.options["stale_days"] == 90      # entry was reloaded with the new options


async def test_notification_links_to_the_url_we_actually_registered(hass, tmp_path):
    """After a fallback the notification must point to /ha-audit-report, not the foreign dashboard."""
    from unittest.mock import patch

    from homeassistant.components import frontend
    from homeassistant.setup import async_setup_component

    build(tmp_path)
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, "frontend", {})
    frontend.async_register_built_in_panel(
        hass, "iframe", sidebar_title="Old", frontend_url_path="ha-audit", config={"url": "/x"})
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"interval_days": 0, "analyze_db": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.panel_path == "ha-audit-report"

    with patch("custom_components.ha_audit.persistent_notification.async_create") as create:
        await hass.services.async_call(DOMAIN, "run", blocking=True)
    assert create.call_count == 1
    message = create.call_args.args[1]
    assert "(/ha-audit-report)" in message and "(/ha-audit)" not in message


async def test_panel_module_url_is_cache_busted_by_file_content(hass, setup):
    """Regression: a changed panel file must get a new URL, otherwise browsers keep the old JS."""
    import hashlib

    js = PANEL_JS
    expected = await hass.async_add_executor_job(lambda: hashlib.sha256(js.read_bytes()).hexdigest()[:10])
    panel = hass.data["frontend_panels"]["ha-audit"]
    assert panel.config["_panel_custom"]["module_url"].endswith(f"/ha-audit-panel.js?v={expected}")


async def test_websocket_reports_running_while_an_audit_is_in_progress(hass, hass_ws_client, setup):
    """The panel polls while `running` is true — so the server must report it truthfully."""
    import asyncio
    import threading
    from unittest.mock import patch

    from custom_components.ha_audit import engine

    gate, started = threading.Event(), threading.Event()
    real = engine.run_audit

    def slow(*args, **kwargs):
        started.set()
        gate.wait(10)
        return real(*args, **kwargs)

    client = await hass_ws_client(hass)
    with patch("custom_components.ha_audit.engine.run_audit", slow):
        task = hass.async_create_task(hass.services.async_call(DOMAIN, "run", blocking=True))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.05)
        assert started.is_set()

        await client.send_json({"id": 1, "type": "ha_audit/report"})
        assert (await client.receive_json())["result"]["running"] is True

        gate.set()
        await task
    await hass.async_block_till_done()
    await client.send_json({"id": 2, "type": "ha_audit/report"})
    res = (await client.receive_json())["result"]
    assert res["running"] is False and res["report"] is not None


async def test_completed_event_is_fired_for_every_run(hass, setup):
    events = async_capture_events(hass, "ha_audit_completed")
    for _ in range(3):
        await hass.services.async_call(DOMAIN, "run", blocking=True)
    await hass.async_block_till_done()
    assert len(events) == 3
