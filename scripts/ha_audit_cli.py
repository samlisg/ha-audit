#!/usr/bin/env python3
"""Standalone CLI for the HA Audit engine (debugging / running outside Home Assistant).

Examples
  python3 scripts/ha_audit_cli.py --config /config
  python3 scripts/ha_audit_cli.py --config /config --no-db --stale-days 60 --json report.json
  HA_TOKEN=... python3 scripts/ha_audit_cli.py --config /config --url http://localhost:8123   # live states

Security: this script NEVER writes inside <config>/www. Reports are printed to stdout
(Markdown) or written only where you tell it to with --md / --json.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent / "custom_components" / "ha_audit" / "engine.py"
spec = importlib.util.spec_from_file_location("ha_audit_engine", ENGINE)
engine = importlib.util.module_from_spec(spec)
sys.modules["ha_audit_engine"] = engine
spec.loader.exec_module(engine)


def live_states(url: str, token: str) -> list[dict]:
    req = urllib.request.Request(url.rstrip("/") + "/api/states",
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def main() -> int:
    p = argparse.ArgumentParser(description="HA Audit (read-only)")
    p.add_argument("--config", default=os.environ.get("HA_CONFIG", "/config"))
    p.add_argument("--stale-days", type=int, default=30)
    p.add_argument("--unavail-days", type=float, default=3)
    p.add_argument("--no-db", action="store_true")
    p.add_argument("--db-timeout", type=int, default=60)
    p.add_argument("--include-diagnostic", action="store_true")
    p.add_argument("--url", default=os.environ.get("HA_URL", "http://localhost:8123"))
    p.add_argument("--token-file", help="file containing a long-lived token (or set HA_TOKEN)")
    p.add_argument("--state-file", help="remember findings between runs to show what is new")
    p.add_argument("--md", help="write Markdown report here (default: print to stdout)")
    p.add_argument("--json", help="write JSON report here")
    a = p.parse_args()

    www = Path(a.config, "www").resolve()
    for out in (a.md, a.json, a.state_file):
        if out and www in (Path(out).resolve(), *Path(out).resolve().parents):
            print(f"Refusing to write '{out}' into <config>/www (publicly readable).", file=sys.stderr)
            return 2

    token = os.environ.get("HA_TOKEN", "")
    if a.token_file:
        token = Path(a.token_file).read_text().strip()
    states = None
    if token:
        try:
            states = live_states(a.url, token)
        except Exception as ex:  # noqa: BLE001
            print(f"[warn] live states failed ({ex}); using restore_state", file=sys.stderr)

    prev = None
    if a.state_file and Path(a.state_file).exists():
        prev = json.loads(Path(a.state_file).read_text()).get("ids")

    report, ids = engine.run_audit(a.config, {
        "stale_days": a.stale_days, "unavail_days": a.unavail_days,
        "analyze_db": not a.no_db, "db_timeout": a.db_timeout,
        "include_diagnostic": a.include_diagnostic}, states, prev)

    if a.state_file:
        Path(a.state_file).write_text(json.dumps({"ids": ids}))
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1, ensure_ascii=False))
    md = engine.render_markdown(report)
    if a.md:
        Path(a.md).write_text(md, encoding="utf-8")
    else:
        print(md)
    s = report["summary"]
    print(f"AUDIT|total={s['total']}|new={s['new']}|" +
          "|".join(f"{k}={v}" for k, v in s["counts"].items()), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
