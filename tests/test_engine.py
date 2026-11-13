"""Engine tests — no Home Assistant needed (stdlib + PyYAML only)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from .fake_config import build

ENGINE = Path(__file__).resolve().parent.parent / "custom_components" / "ha_audit" / "engine.py"
spec = importlib.util.spec_from_file_location("ha_audit_engine", ENGINE)
engine = importlib.util.module_from_spec(spec)
sys.modules["ha_audit_engine"] = engine
spec.loader.exec_module(engine)


@pytest.fixture()
def cfg(tmp_path):
    return build(tmp_path, with_db=True)


def ids(report, key):
    sec = next(s for s in report["sections"] if s["key"] == key)
    return {i["id"] for i in sec["items"]}


def test_report_is_json_serialisable(cfg):
    report, _ = engine.run_audit(cfg)
    json.dumps(report)


def test_hacs_cards_use_real_custom_elements(cfg):
    report, _ = engine.run_audit(cfg)
    assert ids(report, "fe") == {"x/unused-card"}          # mushroom & card-mod are used


def test_hacs_integrations_and_archived(cfg):
    report, _ = engine.run_audit(cfg)
    assert ids(report, "int") == {"x/archived-int"}
    assert ids(report, "arch") == {"x/archived-int"}


def test_resources_and_leftovers(cfg):
    report, _ = engine.run_audit(cfg)
    assert "missing:/hacsfiles/gone/gone.js" in ids(report, "res")
    assert ids(report, "cc") == {"leftover"}
    assert ids(report, "bp") == {"automation/test/bp1.yaml"}


def test_orphans_respect_area_targets_templates_and_diagnostic(cfg):
    report, _ = engine.run_audit(cfg)
    ent = ids(report, "ent")
    assert "sensor.temp_b" in ent
    assert "sensor.living_room_lux" not in ent     # used via area_id target
    assert "sensor.temp_a" not in ent              # used in dashboard + template
    assert "sensor.rssi" not in ent                # diagnostic skipped
    assert report["meta"]["diagnostic_skipped"] == 1


def test_helpers_ghosts_disabled_refs(cfg):
    report, _ = engine.run_audit(cfg)
    assert ids(report, "help") == {"input_boolean.unused_ui", "input_boolean.yaml_flag"}
    assert ids(report, "ghost") == {"light.missing_one"}
    assert ids(report, "disref") == {"switch.disabled_sw"}


def test_registry_duplicates_unavailable(cfg):
    report, _ = engine.run_audit(cfg)
    assert ids(report, "reg") == {"sensor.gone_entry"}
    assert ids(report, "dup") == {"sensor.temp_b_2"}
    assert ids(report, "unavail") == {"sensor.dead_unavail"}


def test_automations_scripts(cfg):
    report, _ = engine.run_audit(cfg)
    a = ids(report, "auto")
    assert "off:automation.off_one" in a and "dupalias:Light on" in a
    assert "stale:automation.light_on" in a
    assert ids(report, "scr") == {"script.old_script"}


def test_devices_and_entries(cfg):
    report, _ = engine.run_audit(cfg)
    assert ids(report, "ce") == {"e2"}
    assert ids(report, "dev") == {"d2", "d3"}


def test_recorder(cfg):
    report, _ = engine.run_audit(cfg)
    assert "sensor.temp_a" in ids(report, "noisy")
    assert "sensor.temp_b" in ids(report, "stale")
    assert report["meta"]["recorder"]["rows"] == 525


def test_recorder_can_be_disabled(cfg):
    report, _ = engine.run_audit(cfg, {"analyze_db": False})
    assert "rows" not in report["meta"]["recorder"]


def test_new_diff(cfg):
    r1, prev = engine.run_audit(cfg)
    assert r1["summary"]["new"] == r1["summary"]["total"] > 0
    r2, _ = engine.run_audit(cfg, previous_ids=prev)
    assert r2["summary"]["new"] == 0


def test_broken_yaml_reported_not_fatal(cfg):
    (cfg / "broken.yaml").write_text("foo: [unclosed\n  bar: {\n")
    report, _ = engine.run_audit(cfg)
    assert "broken.yaml" in ids(report, "yerr")


def test_live_states_with_datetimes(cfg):
    from datetime import datetime, timedelta, timezone
    states = [{"entity_id": "automation.light_on", "state": "on",
               "attributes": {"friendly_name": "Light on",
                              "last_triggered": datetime.now(timezone.utc) - timedelta(days=100)},
               "last_changed": datetime.now(timezone.utc)},
              {"entity_id": "light.missing_one", "state": "off", "attributes": {}, "last_changed": None}]
    report, _ = engine.run_audit(cfg, states=states)
    assert report["live"] is True
    assert "light.missing_one" not in ids(report, "ghost")      # exists live → not a ghost
    assert "stale:automation.light_on" in ids(report, "auto")


def test_ignore_file(cfg):
    (cfg / "ha_audit_ignore.yaml").write_text("entities: ['sensor.temp_b*']\nhacs: ['x/unused-card']\n")
    report, _ = engine.run_audit(cfg)
    assert "sensor.temp_b" not in ids(report, "ent")
    assert ids(report, "fe") == set()


def test_markdown_renders(cfg):
    report, _ = engine.run_audit(cfg)
    md = engine.render_markdown(report)
    assert "# 🏠 HA Audit Report" in md and "sensor.temp_b" in md


def test_missing_storage_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        engine.run_audit(tmp_path)


# ── robustness: hostile / malformed input must never crash the audit ─────────────────────────

def _run(cfg, **kw):
    report, _ = engine.run_audit(cfg, **kw)
    return report


def test_cyclic_yaml_alias_does_not_crash(cfg):
    (cfg / "cyc.yaml").write_text("a: &x [*x]\nb: &y {k: *y}\n")
    _run(cfg)


@pytest.mark.parametrize("content", [
    "- just\n- a list\n",
    "entities: {a: b}\nhacs: 5\ndomains: null\nplatforms: [1, null, {x: y}]\n",
    "{{{ not yaml",
])
def test_ignore_file_with_wrong_shape(cfg, content):
    (cfg / "ha_audit_ignore.yaml").write_text(content)
    _run(cfg)


@pytest.mark.parametrize("name,content", [
    ("lovelace", "[1,2,3]"),
    ("core.device_registry", '{"data": {"devices": "nope"}}'),
    ("core.config_entries", '{"data": null}'),
    ("core.entity_registry", "[]"),
    ("core.entity_registry", '{"data": {"entities": [null, 5, {"x": 1}, {"entity_id": null}]}}'),
    ("core.restore_state", '{"data": [1, "x", {"state": 5}, {"state": {"no_entity": 1}}]}'),
    ("hacs.repositories", '{"data": null}'),
    ("hacs.repositories", '{"data": [1, {"installed": true, "category": "plugin"}]}'),
    ("hacs.repositories", '{"data": {"repositories": {"1": {"installed": true, "category": "plugin", "name": null}}}}'),
    ("core.area_registry", '{"data": {"areas": [{"id": 5}, "x"]}}'),
])
def test_malformed_storage_files(cfg, name, content):
    (cfg / ".storage" / name).write_text(content)
    _run(cfg)


def test_invalid_json_is_reported_not_fatal(cfg):
    (cfg / ".storage" / "lovelace").write_text("{ not json")
    assert any(i["id"].endswith("lovelace") for i in _run(cfg)["sections"][-1]["items"]
               ) or "yerr" in {s["key"] for s in _run(cfg)["sections"]}


def test_empty_storage(tmp_path):
    (tmp_path / ".storage").mkdir()
    assert _run(tmp_path)["summary"]["total"] == 0


def test_odd_yaml_files(cfg):
    (cfg / "automations.yaml").write_text("just a string")
    (cfg / "scripts.yaml").write_text("[1, 2]")
    (cfg / "scenes.yaml").write_text("{1: 2, null: 3}")
    (cfg / "tabs.yaml").write_text("a:\n\t- b\n")
    assert "tabs.yaml" in ids(_run(cfg), "yerr")


def test_deeply_nested_dashboard(cfg):
    (cfg / ".storage" / "lovelace").write_text('{"data":' + "[" * 3000 + "]" * 3000 + "}")
    _run(cfg)


def test_junk_live_states(cfg):
    states = [
        {"entity_id": "automation.x", "state": "on", "attributes": {"last_triggered": "not-a-date"},
         "last_changed": "garbage"},
        {"entity_id": "scene.y", "state": "weird", "attributes": None, "last_changed": None},
        {"entity_id": "sensor.z", "state": "unavailable", "attributes": {}, "last_changed": None},
    ]
    assert "sensor.z" in ids(_run(cfg, states=states), "unavail")


def test_unicode_and_html_in_names(cfg):
    (cfg / "automations.yaml").write_text(
        "- alias: 'Φως <img src=x onerror=alert(1)>'\n  trigger: []\n  action:\n    - entity_id: light.καλημέρα\n")
    _run(cfg)


def test_a_failing_check_is_reported_not_fatal(cfg, monkeypatch):
    monkeypatch.setattr(engine, "check_devices", lambda ctx: 1 / 0)
    report = _run(cfg)
    assert "check: devices" in ids(report, "yerr")
    assert ids(report, "fe")                      # other checks still produced results


def test_recorder_wal_db_with_live_writer_and_odd_path(tmp_path):
    import sqlite3
    import time
    root = tmp_path / "dir with #hash and space"
    root.mkdir()
    build(root)
    c = sqlite3.connect(root / "home-assistant_v2.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE states_meta(metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
    c.execute("CREATE TABLE states(state_id INTEGER PRIMARY KEY, metadata_id INTEGER, last_updated_ts REAL)")
    c.execute("INSERT INTO states_meta VALUES (1, 'sensor.temp_a')")
    c.executemany("INSERT INTO states(metadata_id, last_updated_ts) VALUES (1, ?)", [(time.time(),)] * 50)
    c.commit()
    c.execute("INSERT INTO states(metadata_id, last_updated_ts) VALUES (1, ?)", (time.time(),))  # open write txn
    try:
        rec = _run(root)["meta"]["recorder"]
    finally:
        c.close()
    assert rec.get("rows") == 50, rec


def test_version_is_single_sourced():
    manifest = json.loads((ENGINE.parent / "manifest.json").read_text())
    assert manifest["version"] == engine.VERSION
