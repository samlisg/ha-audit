"""
HA Audit engine — READ-ONLY audit of a Home Assistant config directory.

Pure Python (stdlib + PyYAML). Used both by the Home Assistant integration
(custom_components/ha_audit) and by the standalone CLI (scripts/ha_audit_cli.py).
It never modifies anything and never writes any file: it returns a report dict.

What it checks
  HACS        unused cards (matched via real custom elements inside the JS), unused
              integrations, archived repos, broken dashboard resources,
              leftover custom_components, unused blueprints
  Entities    orphaned entities, unused helpers, references to missing / disabled
              entities, registry leftovers, duplicates (_2), unavailable entities
  Automations disabled / broken / never-triggered / stale automations, duplicate
              aliases, unreferenced scripts & scenes
  Devices     empty config entries, dead devices, devices without area
  Recorder    DB size, noisiest entities, silent entities (read-only SQLite)
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

VERSION = "1.0.0"

_LOGGER = logging.getLogger(__name__)
MAX_DEPTH = 80          # guards against cyclic YAML aliases / absurdly nested JSON

# ══ Constants ════════════════════════════════════════════════════════════════

HELPER_DOMAINS = {
    "input_boolean", "input_number", "input_text", "input_select",
    "input_datetime", "input_button", "timer", "counter", "schedule",
}

# Infrastructure / self-managing domains: skipped by the orphan check
SKIP_DOMAINS = {
    "persistent_notification", "group", "scene", "automation", "script",
    "zone", "person", "sun", "homeassistant", "device_tracker", "update",
    "conversation", "stt", "tts", "wake_word", "number", "select", "button",
    "event", "assist_satellite",
}

COMMON_DOMAINS = HELPER_DOMAINS | SKIP_DOMAINS | {
    "alarm_control_panel", "binary_sensor", "calendar", "camera", "climate",
    "cover", "date", "datetime", "fan", "humidifier", "image", "lawn_mower",
    "light", "lock", "media_player", "notify", "remote", "sensor", "siren",
    "switch", "text", "time", "todo", "vacuum", "valve", "water_heater",
    "weather",
}

# Domains whose entities may legitimately exist without a registry entry
SOFT_DOMAINS = {
    "zone", "person", "sun", "device_tracker", "persistent_notification",
    "tts", "stt", "conversation", "update", "group",
}
ALWAYS_KNOWN = {"zone.home", "sun.sun"}

SERVICE_NAMES = {
    "turn_on", "turn_off", "toggle", "reload", "set_value", "press", "trigger",
    "select_option", "set_temperature", "send_message", "play_media", "restart",
    "stop", "start", "update_entity", "reload_all", "check_config",
}

YAML_DEF_DOMAINS = HELPER_DOMAINS | {"group"}

STRICT_KEYS = {"entity_id", "entity", "entities", "entity_ids", "source",
               "source_entity"}
AREA_KEYS = {"area_id", "area", "floor_id", "label_id"}

# Config entries that legitimately have no entities/devices
EMPTY_OK = {
    "hassio", "backup", "cloud", "hacs", "go2rtc", "google_translate",
    "radio_browser", "bluetooth", "thread", "otbr", "analytics",
    "assist_pipeline", "homeassistant_hardware", "homeassistant_yellow",
    "homeassistant_green", "homeassistant_sky_connect", "homeassistant_alerts",
    "met", "sun", "default_config",
}

EXCLUDE_DIRS = {
    "custom_components", "www", "deps", "__pycache__", "node_modules",
    "backups", "tts", "blueprints", "esphome", "themes", "venv", ".venv",
    "image", "media", "share", "ssl", "addons",
}
EXCLUDE_FILES = {"secrets.yaml", "known_devices.yaml"}

ENTITY_RE = re.compile(r"\b([a-z_]+)\.([a-z0-9_]+)\b")
FULL_ENTITY_RE = re.compile(r"^([a-z_]+)\.([a-z0-9_]+)$")
UUID_RE = re.compile(r"\b[0-9a-f]{32}\b")
TPL_QUOTED_RE = re.compile(r"""['"]([a-z_]+\.[a-z0-9_]+)['"]""")
TPL_STATES_RE = re.compile(r"\bstates\.([a-z_]+)\.([a-z0-9_]+)")
JS_DEFINE_RE = re.compile(
    r"""customElements\.define\(\s*["']([a-z][a-z0-9]*(?:-[a-z0-9]+)+)["']""")
JS_CARDS_RE = re.compile(
    r"""customCards[\s\S]{0,300}?\btype\s*:\s*["']([a-z0-9-]+)["']""")


# ══ Generic helpers ══════════════════════════════════════════════════════════

class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates HA tags (!include, !secret, !input, ...)."""


def _any_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _any_tag)


def dget(obj, *keys, default=None):
    """Safe nested get: returns `default` if any level is missing or not a dict."""
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k)
    return default if obj is None else obj


def as_list(x):
    return x if isinstance(x, list) else []


def as_dict(x):
    return x if isinstance(x, dict) else {}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def slugify_name(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(s).lower())).strip("_")


def parse_ts(v):
    if v in (None, "", 0):
        return None
    try:
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, timezone.utc)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_days(ts, now):
    return None if ts is None else (now - ts).total_seconds() / 86400.0


def fmt_age(d):
    if d is None:
        return "never"
    if d < 1:
        return f"{d * 24:.0f}h"
    return f"{d:.0f}d"


def short_list(items, n=3):
    items = sorted(items)
    s = ", ".join(items[:n])
    return s + (f" (+{len(items) - n})" if len(items) > n else "")


# ══ Context: files, registries, states ═══════════════════════════════════════

class Ctx:
    def __init__(self, config_dir, opts, states=None):
        self.args = opts                  # option namespace (see make_options)
        self._states_in = states          # live states list (from HA) or None
        self.config = Path(config_dir)
        self.storage = self.config / ".storage"
        self.now = datetime.now(timezone.utc)
        self.errors = []          # YAML parse problems
        self.notes = []           # info lines for the report header
        self.ignore = as_dict(self.yload(self.config / "ha_audit_ignore.yaml"))

        # registries
        self.entities = {e["entity_id"]: e for e in as_list(
            dget(self.jload("core.entity_registry"), "data", "entities"))
            if isinstance(e, dict) and isinstance(e.get("entity_id"), str)}
        self.entity_by_uuid = {e["id"]: eid for eid, e in self.entities.items()
                               if e.get("id")}
        dev = as_dict(dget(self.jload("core.device_registry"), "data"))
        self.devices = {d["id"]: d for d in as_list(dev.get("devices"))
                        if isinstance(d, dict) and isinstance(d.get("id"), str)}
        self.deleted_devices = as_list(dev.get("deleted_devices"))
        self.areas = {a["id"]: a for a in
                      as_list(dget(self.jload("core.area_registry"), "data", "areas"))
                      if isinstance(a, dict) and isinstance(a.get("id"), str)}
        ce = as_list(dget(self.jload("core.config_entries"), "data", "entries"))
        self.entries = {e["entry_id"]: e for e in ce
                        if isinstance(e, dict) and isinstance(e.get("entry_id"), str)}
        self.ce_domains = {e.get("domain", "") for e in self.entries.values()}
        self.platforms = {e.get("platform") for e in self.entities.values()}

        self.ents_by_device = defaultdict(list)
        self.ents_by_entry = defaultdict(list)
        for eid, e in self.entities.items():
            if e.get("device_id"):
                self.ents_by_device[e["device_id"]].append(eid)
            if e.get("config_entry_id"):
                self.ents_by_entry[e["config_entry_id"]].append(eid)

        # states: live (preferred) or restore_state
        self.live = False
        self.states = {}
        self._load_states()

        self.known_domains = (COMMON_DOMAINS
                              | {i.split(".")[0] for i in self.entities}
                              | {i.split(".")[0] for i in self.states})
        self.resources = [i for i in as_list(dget(self.jload("lovelace_resources"), "data", "items"))
                          if isinstance(i, dict)]

    # ---- file readers ------------------------------------------------------
    def jload(self, name):
        """Read a .storage JSON file; always returns a dict ({} if missing/invalid)."""
        p = self.storage / name
        try:
            data = json.loads(p.read_text(errors="replace"))
        except FileNotFoundError:
            return {}
        except Exception as ex:  # noqa: BLE001
            self.errors.append((str(p), f"JSON: {type(ex).__name__}: {str(ex)[:100]}"))
            return {}
        return data if isinstance(data, dict) else {}

    def yload(self, p: Path):
        try:
            if p.stat().st_size > 5_000_000:
                return {}
            return yaml.load(p.read_text(errors="replace"), Loader=_Loader) or {}
        except FileNotFoundError:
            return {}
        except Exception as ex:
            try:
                rel = p.relative_to(self.config)
            except ValueError:
                rel = p
            self.errors.append((str(rel), str(ex).splitlines()[0][:120]))
            return None

    # ---- states ------------------------------------------------------------
    def _load_states(self):
        if self._states_in is not None:
            for s in self._states_in:
                self.states[s["entity_id"]] = s
            self.live = True
            self.notes.append(f"Live states from Home Assistant ({len(self.states)} entities)")
            return
        for rec in as_list(dget(self.jload("core.restore_state"), "data")):
            s = rec.get("state") if isinstance(rec, dict) else None
            if isinstance(s, dict) and "entity_id" in s:
                self.states[s["entity_id"]] = s
        if self.states:
            self.notes.append(f"States from core.restore_state ({len(self.states)} entities; "
                              "approximate — live states give better results)")

    # ---- ignore file -------------------------------------------------------
    def _match(self, kind, *values):
        pats = self.ignore.get(kind)
        if isinstance(pats, str):
            pats = [pats]
        elif not isinstance(pats, (list, tuple, set)):
            pats = []
        for v in values:
            if v is None:
                continue
            for p in pats:
                if isinstance(p, (str, int, float)) and fnmatch.fnmatch(str(v).lower(), str(p).lower()):
                    return True
        return False

    def ig_entity(self, eid):
        e = self.entities.get(eid, {})
        return (self._match("entities", eid) or
                self._match("domains", eid.split(".")[0]) or
                self._match("platforms", e.get("platform")))


# ══ Reference collection ═════════════════════════════════════════════════════

class Refs:
    def __init__(self):
        self.loose = defaultdict(set)         # every entity-id-looking string
        self.strict = defaultdict(set)        # ids in entity_id/entity/... keys & templates
        self.custom_types = defaultdict(set)  # custom:xxx card/element types
        self.keys = set()                     # all dict keys (card_mod, kiosk_mode, ...)
        self.uuids = set()
        self.areas, self.labels, self.floors = set(), set(), set()
        self.blueprints = set()
        self.yaml_defined = set()             # entities defined in YAML (no registry entry)
        self.yaml_domains = set()             # keys / platforms seen in config YAML
        self.res_urls = set()
        self.auto_items = []                  # (alias, id) for automations
        self.db_url = ""
        self.docs = 0


def _strict_add(refs, label, s):
    if isinstance(s, str) and FULL_ENTITY_RE.match(s.strip()):
        refs.strict[s.strip()].add(label)


def _strict_collect(refs, label, v):
    if isinstance(v, str):
        if "{{" in v or "{%" in v:
            return
        for part in re.split(r"[,\s]+", v.strip()):
            _strict_add(refs, label, part)
    elif isinstance(v, list):
        for i in v:
            _strict_collect(refs, label, i)


def _area_collect(refs, key, v):
    target = {"area_id": refs.areas, "area": refs.areas,
              "floor_id": refs.floors, "label_id": refs.labels}[key]
    if isinstance(v, str) and "{{" not in v:
        target.add(v)
    elif isinstance(v, list):
        for i in v:
            if isinstance(i, str) and "{{" not in i:
                target.add(i)


def scan_doc(label, obj, refs, kd, parent=None, _depth=0):
    """Walk any nested YAML/JSON structure and collect every kind of reference."""
    if _depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            refs.keys.add(k)
            if parent == "entities" or FULL_ENTITY_RE.match(k):
                m = FULL_ENTITY_RE.match(k)
                if m and m.group(1) in kd:
                    refs.strict[k].add(label)
                    refs.loose[k].add(label)
            if k in STRICT_KEYS:
                _strict_collect(refs, label, v)
            elif k in AREA_KEYS:
                _area_collect(refs, k, v)
            elif k == "type" and isinstance(v, str) and v.startswith("custom:"):
                refs.custom_types[v[7:]].add(label)
            elif k == "use_blueprint" and isinstance(v, dict) and v.get("path"):
                refs.blueprints.add(str(v["path"]).replace("\\", "/"))
            scan_doc(label, v, refs, kd, k, _depth + 1)
    elif isinstance(obj, list):
        for i in obj:
            scan_doc(label, i, refs, kd, parent, _depth + 1)
    elif isinstance(obj, str):
        s = obj
        if s.startswith(("/hacsfiles/", "/local/")):
            refs.res_urls.add(s.split("?")[0])
        for m in ENTITY_RE.finditer(s):
            if m.group(1) in kd:
                refs.loose[m.group(0)].add(label)
        for m in UUID_RE.finditer(s):
            refs.uuids.add(m.group(0))
        if "{{" in s or "{%" in s:
            for m in TPL_QUOTED_RE.finditer(s):
                d, o = m.group(1).split(".", 1)
                if d in kd and o not in SERVICE_NAMES:
                    refs.strict[m.group(1)].add(label)
            for m in TPL_STATES_RE.finditer(s):
                if m.group(1) in kd:
                    refs.strict[f"{m.group(1)}.{m.group(2)}"].add(label)


def yaml_defs(obj, refs, _depth=0):
    """Find entities defined in YAML (they have no registry entry) + config keys."""
    if _depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            k = str(k)
            refs.yaml_domains.add(k.split(" ")[0])
            if k in YAML_DEF_DOMAINS and isinstance(v, dict):
                for key in v:
                    refs.yaml_defined.add(f"{k}.{key}")
            if k in COMMON_DOMAINS and isinstance(v, list):
                for it in v:
                    if not isinstance(it, dict):
                        continue
                    plat = it.get("platform")
                    if isinstance(plat, str):
                        refs.yaml_domains.add(plat)
                    n = it.get("name")
                    if isinstance(n, str) and "{{" not in n:
                        refs.yaml_defined.add(f"{k}.{slugify_name(n)}")
                    if plat == "template":
                        for sv in it.values():
                            if isinstance(sv, dict):
                                for key in sv:
                                    refs.yaml_defined.add(f"{k}.{key}")
            if k == "recorder" and isinstance(v, dict) and isinstance(v.get("db_url"), str):
                refs.db_url = v["db_url"]
            yaml_defs(v, refs, _depth + 1)
    elif isinstance(obj, list):
        for i in obj:
            yaml_defs(i, refs, _depth + 1)


def iter_yaml_files(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for f in sorted(fns):
            if f.endswith((".yaml", ".yml")) and f not in EXCLUDE_FILES:
                yield Path(dp) / f


def collect_refs(ctx: Ctx) -> Refs:
    r, kd = Refs(), ctx.known_domains

    def scan_items(kind, obj, rel):
        """Label every automation/script/scene individually."""
        if kind == "script" and isinstance(obj, dict):
            for key, v in obj.items():
                r.yaml_defined.add(f"script.{key}")
                scan_doc(f"script: {v.get('alias', key) if isinstance(v, dict) else key}", v, r, kd)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, dict):
                    name = v.get("alias") or v.get("name") or v.get("id") or f"#{i + 1}"
                    if kind == "automation":
                        r.auto_items.append((str(v.get("alias", "")), str(v.get("id", "")), str(rel)))
                    scan_doc(f"{kind}: {name}", v, r, kd)
        elif isinstance(obj, dict):
            scan_doc(f"{kind}: {rel}", obj, r, kd)

    for p in iter_yaml_files(ctx.config):
        try:
            rel = p.relative_to(ctx.config)
            parts, name = rel.parts, p.name
            obj = ctx.yload(p)
            if obj is None:
                continue
            r.docs += 1
            top = len(parts) == 1
            kind = None
            if (top and name.startswith("automation")) or parts[0] == "automations":
                kind = "automation"
            elif (top and name.startswith("script")) or parts[0] == "scripts":
                kind = "script"
            elif (top and name.startswith("scene")) or parts[0] == "scenes":
                kind = "scene"
            if kind:
                scan_items(kind, obj, rel)
            elif name.startswith("ui-lovelace") or name == "lovelace.yaml" or parts[0] == "dashboards":
                scan_doc(f"dashboard: {rel}", obj, r, kd)
            else:
                scan_doc(f"config: {rel}", obj, r, kd)
                yaml_defs(obj, r)
        except Exception as ex:  # noqa: BLE001 — one bad file must not stop the audit
            _LOGGER.debug("scan failed for %s", p, exc_info=True)
            ctx.errors.append((str(p.relative_to(ctx.config)), f"scan: {type(ex).__name__}: {str(ex)[:100]}"))

    # storage dashboards
    dash_meta = {i.get("id"): i for i in as_list(dget(ctx.jload("lovelace_dashboards"), "data", "items"))
                 if isinstance(i, dict)}
    if ctx.storage.is_dir():
        for p in sorted(ctx.storage.glob("lovelace*")):
            if p.name in ("lovelace_resources", "lovelace_dashboards"):
                continue
            did = p.name.split(".", 1)[1] if "." in p.name else None
            title = dash_meta.get(did, {}).get("title", did) if did else "Overview (default)"
            scan_doc(f"dashboard: {title}", dget(ctx.jload(p.name), "data", default={}), r, kd)
            r.docs += 1

    # UI helpers / integration options (template, group, threshold, utility_meter, ...)
    for e in ctx.entries.values():
        scan_doc(f"helper/integration: {e.get('title') or e.get('domain')} ({e.get('domain')})",
                 {"data": e.get("data"), "options": e.get("options")}, r, kd)

    # energy dashboard, person, voice exposure
    scan_doc("energy dashboard", dget(ctx.jload("energy"), "data", default={}), r, kd)
    scan_doc("person", dget(ctx.jload("person"), "data", default={}), r, kd)
    exp = as_dict(dget(ctx.jload("homeassistant.exposed_entities"), "data", "exposed_entities"))
    for eid, meta in exp.items():
        ass = (meta or {}).get("assistants", {}) if isinstance(meta, dict) else {}
        if any(isinstance(v, dict) and v.get("should_expose") for v in ass.values()):
            r.loose[eid].add("voice assistant exposure")
    return r


def compute_used(ctx: Ctx, r: Refs):
    """All entity ids that are referenced somewhere (directly, by UUID, area, label, device)."""
    used = set(r.loose)
    device_ids = set()
    for u in r.uuids:
        if u in ctx.entity_by_uuid:
            used.add(ctx.entity_by_uuid[u])
        elif u in ctx.devices:
            device_ids.add(u)
    for d in device_ids:
        used.update(ctx.ents_by_device.get(d, []))
    area_ids = set(r.areas)
    for aid, a in ctx.areas.items():
        if a.get("floor_id") in r.floors:
            area_ids.add(aid)
    if area_ids or r.labels:
        for eid, e in ctx.entities.items():
            area = e.get("area_id") or ctx.devices.get(e.get("device_id"), {}).get("area_id")
            if area in area_ids or set(e.get("labels") or []) & r.labels:
                used.add(eid)
    return used


# ══ Findings container ═══════════════════════════════════════════════════════

SECTIONS = [
    # key, title, counts toward total, empty message
    ("fe", "🧩 Unused HACS frontend cards", True, "All installed cards are used in dashboards."),
    ("int", "🔌 Unused HACS integrations", True, "All installed integrations are active."),
    ("arch", "🗄️ Archived / unmaintained HACS repositories", True, "None."),
    ("res", "📦 Dashboard resource problems", True, "All resources load and all cards are registered."),
    ("cc", "🧰 Leftover custom_components (not in HACS, not used)", True, "None."),
    ("bp", "📐 Unused blueprints", True, "All blueprints are used."),
    ("ent", "👻 Orphaned entities (not in dashboards/automations/helpers/areas)", True,
     "No orphaned entities."),
    ("help", "🧮 Unused helpers (input_*, timer, counter, schedule)", True, "All helpers are used."),
    ("ghost", "🕳️ References to missing entities (broken automations/cards)", True,
     "No references to missing entities."),
    ("disref", "🚫 References to DISABLED entities", True, "None."),
    ("reg", "🧹 Registry leftovers (removed integration / orphaned)", True, "None."),
    ("dup", "👯 Likely duplicate entities (_2, _3 …)", True, "None."),
    ("unavail", "📴 Unavailable entities", True, "Nothing unavailable."),
    ("auto", "🤖 Automation health", True, "All automations look healthy."),
    ("scr", "📜 Unreferenced scripts & scenes", True, "None."),
    ("ce", "🔧 Config entries without any entity/device", True, "None."),
    ("dev", "🖥️ Dead / orphaned devices", True, "None."),
    ("yerr", "⚠️ Files that failed to parse", True, "All files parsed."),
    ("ceoff", "⏸️ Disabled config entries (info)", False, "None."),
    ("noarea", "📍 Devices without area (info)", False, "All devices have an area."),
    ("noisy", "📊 Recorder — noisiest entities (info)", False, "Recorder check skipped/unavailable."),
    ("stale", "⌛ Recorder — entities silent for a long time (info)", False, "None."),
]
SEC_META = {k: (t, c, e) for k, t, c, e in SECTIONS}


def item(id_, text, group=None):
    return {"id": id_, "text": text, "group": group}


# ══ Checks ═══════════════════════════════════════════════════════════════════

def hacs_repos(ctx):
    raw = dget(ctx.jload("hacs.repositories"), "data", default={})
    archived_names = set()
    if isinstance(raw, dict):
        archived_names = set(raw.get("archived_repositories") or [])
        if isinstance(raw.get("repositories"), (dict, list)):
            raw = raw["repositories"]
    items = raw.values() if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    plugins, integrations = [], []
    for r in items:
        if not isinstance(r, dict) or not r.get("installed"):
            continue
        repo = r.get("full_name") or r.get("name") or "unknown"
        short = repo.split("/")[-1]
        name = (r.get("name") or r.get("manifest_name")
                or (r.get("manifest") or {}).get("name") or short)
        rec = SimpleNamespace(
            repo=repo, short=short, name=name,
            domain=r.get("domain") or slug(name),
            archived=bool(r.get("archived")) or repo in archived_names,
            version=r.get("installed_version") or r.get("installed_commit") or "")
        if r.get("category") == "plugin":
            plugins.append(rec)
        elif r.get("category") == "integration":
            integrations.append(rec)
    return plugins, integrations


def plugin_elements(ctx, short):
    base = ctx.config / "www" / "community"
    d = base / short
    if not d.is_dir() and base.is_dir():
        for c in base.iterdir():
            if c.name.lower() == short.lower():
                d = c
                break
    out = set()
    if not d.is_dir():
        return out, False
    for n, f in enumerate(d.rglob("*.js")):
        if n >= 30:
            break
        try:
            if f.stat().st_size > 10_000_000:
                continue
            t = f.read_text(errors="ignore")
        except Exception:
            continue
        out.update(JS_DEFINE_RE.findall(t))
        out.update(JS_CARDS_RE.findall(t))
    return out, True


def _name_variants(s):
    b = slug(s)
    st = re.sub(r"^(lovelace|homeassistant|hass|ha)", "", b)
    return b, st


def check_hacs(ctx, r):
    plugins, integrations = hacs_repos(ctx)
    out = {k: [] for k in ("fe", "int", "arch", "res", "cc")}
    types = set(r.custom_types)
    type_slugs = {slug(t) for t in types}
    key_slugs = {slug(k) for k in r.keys}

    # frontend cards
    for p in plugins:
        if ctx._match("hacs", p.repo, p.name):
            continue
        elems, found_dir = plugin_elements(ctx, p.short)
        used = bool(elems & types)
        if not used:
            b, st = _name_variants(p.short)
            nb, nst = _name_variants(p.name)
            for cand in {b, st, nb, nst}:
                if len(cand) < 5:
                    continue
                if any(cand in s for s in type_slugs | key_slugs):
                    used = True
                    break
            if not used:
                cardless = re.sub(r"card$", "", st)
                if len(cardless) >= 6 and any(cardless in s for s in type_slugs):
                    used = True
        if not used:
            detail = (f"{len(elems)} element(s): {short_list(elems, 3)}" if elems
                      else ("files found, no elements detected" if found_dir else "files not found in www/community"))
            out["fe"].append(item(p.repo, f"`{p.repo}` → card **{p.name}** — {detail}"))

    # integrations
    for p in integrations:
        if ctx._match("hacs", p.repo, p.name, p.domain):
            continue
        d = p.domain
        if d in ctx.ce_domains or d in ctx.platforms or d in r.yaml_domains:
            continue
        out["int"].append(item(p.repo, f"`{p.repo}` → domain **{d}** — no config entry, entities or YAML"))

    # archived
    for p in plugins + integrations:
        if p.archived and not ctx._match("hacs", p.repo, p.name):
            out["arch"].append(item(p.repo, f"`{p.repo}` — archived on GitHub (no more updates)"))

    # resources
    urls = []
    for res in ctx.resources:
        u = (res.get("url") or "").split("?")[0]
        urls.append(u)
        pth = None
        if u.startswith("/hacsfiles/"):
            pth = ctx.config / "www" / "community" / u[len("/hacsfiles/"):]
        elif u.startswith("/local/"):
            pth = ctx.config / "www" / u[len("/local/"):]
        if pth is not None and not pth.exists():
            out["res"].append(item(f"missing:{u}", f"Resource `{u}` — file missing (404 in browser)"))
    seen = Counter(urls)
    for u, n in seen.items():
        if n > 1:
            out["res"].append(item(f"dup:{u}", f"Resource `{u}` registered {n}× (duplicate)"))
    all_urls = set(urls) | r.res_urls
    if all_urls:
        for p in plugins:
            if ctx._match("hacs", p.repo, p.name):
                continue
            needle = f"/hacsfiles/{p.short}/".lower()
            if (ctx.config / "www" / "community" / p.short).is_dir() and \
                    not any(needle in u.lower() for u in all_urls):
                out["res"].append(item(f"unreg:{p.repo}",
                                       f"`{p.repo}` is installed but not registered as dashboard resource (not loaded)"))

    # leftover custom_components
    cc = ctx.config / "custom_components"
    hacs_domains = {p.domain for p in integrations} | {"hacs"}
    if cc.is_dir():
        for d in sorted(cc.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")) or d.name in hacs_domains:
                continue
            if d.name in ctx.ce_domains or d.name in ctx.platforms or d.name in r.yaml_domains:
                continue
            if ctx._match("hacs", d.name):
                continue
            out["cc"].append(item(d.name, f"`custom_components/{d.name}` — manually installed, never configured"))
    return out


def check_blueprints(ctx, r):
    res = []
    base = ctx.config / "blueprints"
    if not base.is_dir():
        return res
    for kind in ("automation", "script"):
        kb = base / kind
        if not kb.is_dir():
            continue
        for f in sorted(kb.rglob("*.y*ml")):
            rel = f.relative_to(kb).as_posix()
            if rel not in r.blueprints:
                res.append(item(f"{kind}/{rel}", f"`{kind}/{rel}` — no automation/script uses it"))
    return res


def check_entities(ctx, r, used):
    out = {k: [] for k in ("ent", "help", "ghost", "disref", "reg", "dup", "unavail")}
    stats = {"diagnostic_skipped": 0}
    a = ctx.args

    # orphaned entities
    for eid, e in ctx.entities.items():
        dom = eid.split(".")[0]
        if dom in SKIP_DOMAINS or dom in HELPER_DOMAINS or eid in used or ctx.ig_entity(eid):
            continue
        if e.get("entity_category") and not a.include_diagnostic:
            stats["diagnostic_skipped"] += 1
            continue
        tags = []
        if e.get("disabled_by"):
            tags.append("disabled")
        if e.get("hidden_by"):
            tags.append("hidden")
        plat = e.get("platform", "?")
        out["ent"].append(item(eid, f"`{eid}`" + (f" *({', '.join(tags)})*" if tags else ""), group=plat))

    # unused helpers
    helpers = {i for i in ctx.entities if i.split(".")[0] in HELPER_DOMAINS}
    helpers |= {i for i in r.yaml_defined if i.split(".")[0] in HELPER_DOMAINS}
    for eid in sorted(helpers):
        if eid not in used and not ctx.ig_entity(eid):
            src = "YAML" if eid not in ctx.entities else "UI"
            out["help"].append(item(eid, f"`{eid}` — {src} helper, never referenced"))

    # references to missing / disabled entities
    known = set(ctx.entities) | set(ctx.states) | r.yaml_defined | ALWAYS_KNOWN
    for eid, srcs in sorted(r.strict.items()):
        dom, obj = eid.split(".", 1)
        if dom not in ctx.known_domains or obj in SERVICE_NAMES or ctx.ig_entity(eid):
            continue
        where = short_list(srcs, 3)
        if eid in ctx.entities and ctx.entities[eid].get("disabled_by"):
            out["disref"].append(item(eid, f"`{eid}` is disabled — used in: {where}"))
        elif eid not in known and dom not in SOFT_DOMAINS:
            out["ghost"].append(item(eid, f"`{eid}` — used in: {where}"))

    # registry leftovers
    for eid, e in sorted(ctx.entities.items()):
        if ctx.ig_entity(eid):
            continue
        ceid = e.get("config_entry_id")
        if e.get("orphaned_timestamp"):
            out["reg"].append(item(eid, f"`{eid}` — orphaned (integration removed), platform `{e.get('platform')}`"))
        elif ceid and ceid not in ctx.entries:
            out["reg"].append(item(eid, f"`{eid}` — config entry no longer exists, platform `{e.get('platform')}`"))

    # duplicates
    for eid, e in sorted(ctx.entities.items()):
        m = re.match(r"^(.+?)_(\d+)$", eid)
        if m and m.group(1) in ctx.entities and not e.get("disabled_by") \
                and not ctx.ig_entity(eid) and eid.split(".")[0] not in SKIP_DOMAINS:
            base = ctx.entities[m.group(1)]
            if not base.get("disabled_by") and base.get("platform") == e.get("platform"):
                out["dup"].append(item(eid, f"`{eid}` duplicates `{m.group(1)}` (platform `{e.get('platform')}`)"))

    # unavailable
    thr = a.unavail_days
    for eid, s in sorted(ctx.states.items()):
        if s.get("state") != "unavailable":
            continue
        dom = eid.split(".")[0]
        meta = ctx.entities.get(eid, {})
        if meta.get("disabled_by") or dom in ("automation", "update") or ctx.ig_entity(eid):
            continue
        d = age_days(parse_ts(s.get("last_changed")), ctx.now)
        if d is not None and d < thr:
            continue
        attrs = s.get("attributes") or {}
        tag = " — restored (integration not loaded)" if attrs.get("restored") else ""
        out["unavail"].append(item(eid, f"`{eid}` — unavailable for {fmt_age(d) if d is not None else 'an unknown time'}{tag}",
                                   group=meta.get("platform", "yaml/other")))
    return out, stats


def check_automations(ctx, r, used):
    auto, scr = [], []
    stale = ctx.args.stale_days
    for eid, s in sorted(ctx.states.items()):
        dom = eid.split(".")[0]
        if ctx.ig_entity(eid):
            continue
        attrs = s.get("attributes") or {}
        name = attrs.get("friendly_name", eid)
        lt = parse_ts(attrs.get("last_triggered"))
        if dom == "automation":
            st = s.get("state")
            if st == "unavailable":
                auto.append(item(f"broken:{eid}", f"🔴 `{eid}` — **{name}**: unavailable (broken config / missing device)"))
            elif st == "off":
                auto.append(item(f"off:{eid}", f"⏸️ `{eid}` — **{name}**: disabled"))
            elif lt is None:
                auto.append(item(f"never:{eid}", f"💤 `{eid}` — **{name}**: never triggered"))
            elif age_days(lt, ctx.now) > stale:
                auto.append(item(f"stale:{eid}", f"💤 `{eid}` — **{name}**: last triggered {fmt_age(age_days(lt, ctx.now))} ago"))
        elif dom == "script" and eid not in used:
            if lt is None or age_days(lt, ctx.now) > stale:
                scr.append(item(eid, f"`{eid}` — **{name}**: never referenced, last run {fmt_age(age_days(lt, ctx.now)) if lt else 'never'}"))
        elif dom == "scene" and eid not in used:
            lt2 = parse_ts(s.get("state"))
            if lt2 is None or age_days(lt2, ctx.now) > stale:
                scr.append(item(eid, f"`{eid}` — **{name}**: never referenced, last activated {fmt_age(age_days(lt2, ctx.now)) if lt2 else 'never'}"))
    # duplicate aliases (automations.yaml)
    c = Counter(a for a, _, _ in r.auto_items if a)
    for alias, n in c.items():
        if n > 1:
            auto.append(item(f"dupalias:{alias}", f"🔁 alias **{alias}** used by {n} automations"))
    return auto, scr


def check_devices(ctx):
    ce, dev, noarea, ceoff = [], [], [], []
    for eid, e in sorted(ctx.entries.items()):
        title = f"{e.get('title') or e.get('domain')} ({e.get('domain')})"
        if ctx._match("config_entries", e.get("title"), e.get("domain")):
            continue
        if e.get("disabled_by"):
            ceoff.append(item(eid, f"{title} — disabled by `{e['disabled_by']}`"))
            continue
        if e.get("domain") in EMPTY_OK:
            continue
        n_e = len(ctx.ents_by_entry.get(eid, []))
        n_d = sum(1 for d in ctx.devices.values() if eid in (d.get("config_entries") or []))
        if n_e == 0 and n_d == 0:
            ce.append(item(eid, f"{title} — 0 entities, 0 devices"))
    via = {d.get("via_device_id") for d in ctx.devices.values() if d.get("via_device_id")}
    for did, d in sorted(ctx.devices.items(), key=lambda x: str(x[1].get("name"))):
        name = d.get("name_by_user") or d.get("name") or did[:8]
        if ctx._match("devices", name) or d.get("disabled_by"):
            continue
        ents = ctx.ents_by_device.get(did, [])
        entries = d.get("config_entries") or []
        if entries and not any(x in ctx.entries for x in entries):
            dev.append(item(did, f"**{name}** — its integration/config entry was removed"))
        elif not ents and did not in via and d.get("entry_type") != "service":
            dev.append(item(did, f"**{name}** — device with no entities"))
        elif ents and not d.get("area_id") and d.get("entry_type") != "service":
            noarea.append(item(did, f"**{name}**"))
    return ce, dev, noarea, ceoff


def check_recorder(ctx, r):
    noisy, stale, info = [], [], {}
    a = ctx.args
    if a.no_db:
        return noisy, stale, {"skipped": "--no-db"}
    url = r.db_url
    if url and not url.startswith("sqlite"):
        return noisy, stale, {"skipped": "recorder uses an external DB"}
    path = ctx.config / "home-assistant_v2.db"
    if url.startswith("sqlite:///"):
        path = Path(url[len("sqlite:///"):])
        if not path.is_absolute():
            path = ctx.config / path
    if not path.exists():
        return noisy, stale, {"skipped": "database file not found"}
    info["size_mb"] = round(path.stat().st_size / 1048576, 1)
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        t0 = time.time()
        con.set_progress_handler(lambda: 1 if time.time() - t0 > a.db_timeout else 0, 100000)
        cols = {row[1] for row in con.execute("PRAGMA table_info(states)")}
        ts_col = "last_updated_ts" if "last_updated_ts" in cols else "last_updated"
        has_meta = bool(list(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='states_meta'")))
        if has_meta:
            q = (f"SELECT m.entity_id, COUNT(*), MAX(s.{ts_col}) FROM states s "
                 "JOIN states_meta m ON m.metadata_id = s.metadata_id GROUP BY m.entity_id")
        else:
            q = f"SELECT entity_id, COUNT(*), MAX({ts_col}) FROM states GROUP BY entity_id"
        rows = con.execute(q).fetchall()
        con.close()
    except Exception as ex:
        return noisy, stale, {"skipped": f"query failed/timed out ({ex.__class__.__name__})", **info}
    total = sum(n for _, n, _ in rows) or 1
    info["rows"] = total
    for eid, n, _last in sorted(rows, key=lambda x: -x[1])[:a.top_noisy]:
        if ctx.ig_entity(eid):
            continue
        noisy.append(item(eid, f"`{eid}` — {n:,} rows ({n / total * 100:.1f}% of states table)"))
    for eid, _n, last in sorted(rows, key=lambda x: x[0]):
        e = ctx.entities.get(eid)
        if not e or e.get("disabled_by") or ctx.ig_entity(eid):
            continue
        if eid.split(".")[0] not in ("sensor", "binary_sensor", "light", "switch", "climate", "cover", "media_player"):
            continue
        if e.get("entity_category"):
            continue
        ts = parse_ts(last)
        d = age_days(ts, ctx.now)
        if d is not None and d > a.stale_days:
            stale.append(item(eid, f"`{eid}` — last recorded change {fmt_age(d)} ago", group=e.get("platform")))
    return noisy, stale, info


# ══ Public API ═══════════════════════════════════════════════════════════════

DEFAULT_OPTIONS = {
    "stale_days": 30,          # automations / entities silent this long are flagged
    "unavail_days": 3,         # min days unavailable before an entity is flagged
    "include_diagnostic": False,
    "analyze_db": True,        # recorder (SQLite) analysis
    "db_timeout": 60,          # seconds before the DB query is aborted
    "top_noisy": 15,
    "max_items": 500,          # max items per section kept in the report payload
}


def make_options(options=None):
    """Merge user options over defaults and return a namespace the checks use."""
    o = {**DEFAULT_OPTIONS, **(options or {})}
    return SimpleNamespace(
        stale_days=int(o["stale_days"]),
        unavail_days=float(o["unavail_days"]),
        include_diagnostic=bool(o["include_diagnostic"]),
        no_db=not bool(o["analyze_db"]),
        db_timeout=int(o["db_timeout"]),
        top_noisy=int(o["top_noisy"]),
        max_items=int(o["max_items"]),
    )


def run_audit(config_dir, options=None, states=None, previous_ids=None):
    """Run every check. Blocking — call it from an executor inside Home Assistant.

    config_dir    path of the HA config directory
    options       dict, see DEFAULT_OPTIONS
    states        optional list of live states: [{"entity_id", "state", "attributes",
                  "last_changed"}]; without it core.restore_state is used
    previous_ids  ids returned by the previous run → enables the "new" diff

    Returns (report, ids). `report` is JSON-serialisable; `ids` is the list of
    finding identifiers to pass as previous_ids next time.
    """
    opts = make_options(options)
    ctx = Ctx(config_dir, opts, states)
    if not ctx.storage.is_dir():
        raise FileNotFoundError(f"No .storage directory in {ctx.config}")

    r = collect_refs(ctx)
    used = compute_used(ctx, r)

    def guard(name, fn, default):
        """Run one check; a failure is reported in the 'failed to parse' section, not fatal."""
        try:
            return fn()
        except Exception as ex:  # noqa: BLE001
            _LOGGER.exception("HA Audit check %s failed", name)
            ctx.errors.append((f"check: {name}", f"{type(ex).__name__}: {str(ex)[:100]}"))
            return default

    found = dict(guard("hacs", lambda: check_hacs(ctx, r), {}))
    found["bp"] = guard("blueprints", lambda: check_blueprints(ctx, r), [])
    ent, stats = guard("entities", lambda: check_entities(ctx, r, used),
                       ({}, {"diagnostic_skipped": 0}))
    found.update(ent)
    found["auto"], found["scr"] = guard("automations", lambda: check_automations(ctx, r, used), ([], []))
    found["ce"], found["dev"], found["noarea"], found["ceoff"] = guard(
        "devices", lambda: check_devices(ctx), ([], [], [], []))
    found["noisy"], found["stale"], dbinfo = guard(
        "recorder", lambda: check_recorder(ctx, r), ([], [], {"skipped": "check failed"}))
    found["yerr"] = [item(f, f"`{f}` — {e}") for f, e in ctx.errors]

    cur = {f"{k}|{i['id']}" for k, _, c, _ in SECTIONS if c for i in found.get(k, [])}
    new_ids = cur if previous_ids is None else cur - set(previous_ids)
    counts = {k: len(found.get(k, [])) for k, _, c, _ in SECTIONS if c}

    sections = []
    for k, title, counts_toward, empty in SECTIONS:
        its = found.get(k, [])
        if not counts_toward and not its:
            continue
        sections.append({
            "key": k, "title": title, "counts": counts_toward, "empty": empty,
            "total": len(its), "truncated": max(0, len(its) - opts.max_items),
            "new": sum(1 for i in its if counts_toward and f"{k}|{i['id']}" in new_ids),
            "items": [{"id": i["id"], "text": i["text"], "group": i["group"],
                       "new": counts_toward and f"{k}|{i['id']}" in new_ids}
                      for i in its[:opts.max_items]],
        })

    report = {
        "version": VERSION,
        "generated": ctx.now.isoformat(),
        "live": ctx.live,
        "notes": ctx.notes,
        "summary": {"total": sum(counts.values()), "new": len(new_ids), "counts": counts},
        "meta": {
            "entities": len(ctx.entities),
            "disabled": sum(1 for e in ctx.entities.values() if e.get("disabled_by")),
            "diagnostic_skipped": stats["diagnostic_skipped"],
            "devices": len(ctx.devices), "entries": len(ctx.entries),
            "files_scanned": r.docs, "recorder": dbinfo,
        },
        "sections": sections,
    }
    return report, sorted(cur)


def render_markdown(report):
    """Plain Markdown version of a report (used by the CLI)."""
    s, m = report["summary"], report["meta"]
    L = ["# 🏠 HA Audit Report",
         f"_Generated: {report['generated']} · ha_audit v{report['version']}_",
         "> ⚠️ Read-only audit — review manually before removing anything.", ""]
    L += [f"_{n}_" for n in report["notes"]]
    L += ["", "## Summary", "", "| Section | Findings | New |", "|---|---:|---:|"]
    for sec in report["sections"]:
        L.append(f"| {sec['title']} | {sec['total']} | {sec['new'] if sec['counts'] else '–'} |")
    L += ["", f"**Total: {s['total']} · new since last run: {s['new']}**",
          f"_{m['entities']} entities ({m['disabled']} disabled, {m['diagnostic_skipped']} "
          f"diagnostic skipped), {m['devices']} devices, {m['entries']} config entries · "
          f"{m['files_scanned']} files scanned_"]
    db = m.get("recorder") or {}
    if db:
        L.append("_Recorder: " + (f"{db.get('size_mb', '?')} MB, {db['rows']:,} state rows"
                                  if "rows" in db else db.get("skipped", "n/a")) + "_")
    for sec in report["sections"]:
        L += ["", f"## {sec['title']} ({sec['total']})", ""]
        if not sec["items"]:
            L.append(f"_{sec['empty']}_")
            continue
        groups = defaultdict(list)
        for i in sec["items"]:
            groups[i["group"] or ""].append(i)
        for g, gi in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0])):
            if g:
                L.append(f"**{g}** ({len(gi)})")
            L += [f"- {i['text']}{' 🆕' if i['new'] else ''}" for i in gi]
            L.append("")
        if sec["truncated"]:
            L.append(f"_…and {sec['truncated']} more_")
    return "\n".join(L) + "\n"
