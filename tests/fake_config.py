"""Builds a small fake Home Assistant /config directory for tests."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def build(root: Path, with_db: bool = False) -> Path:
    s = root / ".storage"
    w(root / "www/community/lovelace-mushroom/m.js", 'customElements.define("mushroom-light-card",class{});')
    w(root / "www/community/unused-card/u.js", 'customElements.define("unused-fancy-card",class{});')
    w(root / "www/community/lovelace-card-mod/card-mod.js", "x")
    w(root / "custom_components/leftover/__init__.py", "")
    w(root / "custom_components/goodint/__init__.py", "")
    w(root / "blueprints/automation/test/bp1.yaml", "blueprint: {}")
    w(root / "configuration.yaml", """
homeassistant:
  name: Home
input_boolean:
  yaml_flag:
    name: Flag
automation: !include automations.yaml
sensor: !include sensors.yaml
secret_thing: !secret abc
template:
  - sensor:
      - name: "Tpl Sensor"
        state: "{{ states('sensor.temp_a') }}"
""")
    w(root / "automations.yaml", """
- id: a1
  alias: Light on
  trigger:
    - platform: state
      entity_id: binary_sensor.motion
  action:
    - service: light.turn_on
      target:
        entity_id: light.kitchen
        area_id: living_room
    - service: script.used_script
- id: a2
  alias: Light on
  trigger: []
  action:
    - service: light.turn_on
      entity_id: light.missing_one
    - service: switch.turn_on
      entity_id: switch.disabled_sw
""")
    w(root / "sensors.yaml", "[]")
    w(root / "scripts.yaml", """
used_script:
  alias: Used
  sequence: []
old_script:
  alias: Old
  sequence: []
""")
    w(s / "lovelace", json.dumps({"data": {"config": {"views": [{"cards": [
        {"type": "custom:mushroom-light-card", "entity": "light.kitchen"},
        {"type": "entities", "card_mod": {"style": ""}, "entities": [{"entity": "sensor.temp_a"}]}]}]}}}))
    w(s / "lovelace_resources", json.dumps({"data": {"items": [
        {"url": "/hacsfiles/lovelace-mushroom/m.js?v=1"},
        {"url": "/hacsfiles/gone/gone.js"},
        {"url": "/hacsfiles/lovelace-card-mod/card-mod.js"}]}}))
    repos = {
        "1": {"installed": True, "category": "plugin", "full_name": "piitaya/lovelace-mushroom", "name": "Mushroom"},
        "2": {"installed": True, "category": "plugin", "full_name": "x/unused-card", "name": "Unused Card"},
        "3": {"installed": True, "category": "plugin", "full_name": "thomasloven/lovelace-card-mod", "name": "Card Mod"},
        "4": {"installed": True, "category": "integration", "full_name": "x/archived-int", "name": "Arch", "domain": "archint"},
        "5": {"installed": True, "category": "integration", "full_name": "x/goodint", "name": "Good", "domain": "goodint"},
    }
    w(s / "hacs.repositories", json.dumps({"data": {"archived_repositories": ["x/archived-int"], "repositories": repos}}))
    w(s / "core.config_entries", json.dumps({"data": {"entries": [
        {"entry_id": "e1", "domain": "goodint", "title": "Good"},
        {"entry_id": "e2", "domain": "zha", "title": "Empty ZHA"},
        {"entry_id": "e3", "domain": "x", "title": "Off", "disabled_by": "user"}]}}))
    ents = [
        ("light.kitchen", "u1", "goodint", "e1", "d1", {}),
        ("binary_sensor.motion", "u2", "goodint", "e1", "d1", {}),
        ("sensor.temp_a", "u3", "goodint", "e1", "d1", {}),
        ("sensor.temp_b", "u4", "goodint", "e1", "d1", {}),
        ("sensor.temp_b_2", "u5", "goodint", "e1", "d1", {}),
        ("sensor.living_room_lux", "u6", "goodint", "e1", None, {"area_id": "living_room"}),
        ("sensor.rssi", "u7", "goodint", "e1", None, {"entity_category": "diagnostic"}),
        ("switch.disabled_sw", "u8", "goodint", "e1", None, {"disabled_by": "user"}),
        ("sensor.gone_entry", "u9", "old", "dead", None, {}),
        ("input_boolean.unused_ui", "u10", "input_boolean", None, None, {}),
        ("sensor.dead_unavail", "u11", "goodint", "e1", None, {}),
        ("script.old_script", "u12", "script", None, None, {}),
        ("script.used_script", "u13", "script", None, None, {}),
    ]
    w(s / "core.entity_registry", json.dumps({"data": {"entities": [
        {"entity_id": e, "id": u, "platform": p, "config_entry_id": c, "device_id": d, **extra}
        for e, u, p, c, d, extra in ents]}}))
    w(s / "core.device_registry", json.dumps({"data": {"devices": [
        {"id": "d1", "name": "Dev1", "config_entries": ["e1"]},
        {"id": "d2", "name": "Empty dev", "config_entries": ["e1"]},
        {"id": "d3", "name": "Orphan dev", "config_entries": ["gone"]}]}}))
    w(s / "core.restore_state", json.dumps({"data": [
        {"state": {"entity_id": "sensor.dead_unavail", "state": "unavailable",
                   "last_changed": "2026-10-01T00:00:00+00:00", "attributes": {}}},
        {"state": {"entity_id": "automation.light_on", "state": "on",
                   "attributes": {"friendly_name": "Light on", "last_triggered": "2026-01-01T00:00:00+00:00"}}},
        {"state": {"entity_id": "automation.off_one", "state": "off",
                   "attributes": {"friendly_name": "Off one", "last_triggered": None}}},
        {"state": {"entity_id": "script.old_script", "state": "off",
                   "attributes": {"friendly_name": "Old", "last_triggered": None}}}]}))
    if with_db:
        db = root / "home-assistant_v2.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE states_meta(metadata_id INTEGER PRIMARY KEY, entity_id TEXT)")
        c.execute("CREATE TABLE states(state_id INTEGER PRIMARY KEY, metadata_id INTEGER, last_updated_ts REAL)")
        c.executemany("INSERT INTO states_meta VALUES(?,?)",
                      [(1, "sensor.temp_a"), (2, "sensor.temp_b"), (3, "sensor.living_room_lux")])
        now = time.time()
        c.executemany("INSERT INTO states(metadata_id,last_updated_ts) VALUES(?,?)",
                      [(1, now - 100)] * 500 + [(2, now - 90 * 86400)] * 20 + [(3, now - 60)] * 5)
        c.commit()
        c.close()
    return root
