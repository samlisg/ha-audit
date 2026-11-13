"""Constants for HA Audit."""
from __future__ import annotations

DOMAIN = "ha_audit"
PANEL_URL_PATH = "ha-audit"
PANEL_FALLBACK_PATH = "ha-audit-report"   # used when "ha-audit" is taken by another panel/dashboard
PANEL_ELEMENT = "ha-audit-panel"
STATIC_URL = "/ha_audit_static"
SIGNAL_UPDATED = f"{DOMAIN}_updated"
EVENT_COMPLETED = f"{DOMAIN}_completed"
STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN                      # → /config/.storage/ha_audit (never web-served)
CHECK_EVERY_HOURS = 6                     # how often we check if a scheduled run is due
STARTUP_DELAY_SECONDS = 120               # first run is delayed so startup stays fast

CONF_INTERVAL_DAYS = "interval_days"
CONF_STALE_DAYS = "stale_days"
CONF_UNAVAIL_DAYS = "unavail_days"
CONF_INCLUDE_DIAGNOSTIC = "include_diagnostic"
CONF_ANALYZE_DB = "analyze_db"
CONF_NOTIFY = "notify"

DEFAULTS = {
    CONF_INTERVAL_DAYS: 7,        # 0 = manual runs only
    CONF_STALE_DAYS: 30,
    CONF_UNAVAIL_DAYS: 3,
    CONF_INCLUDE_DIAGNOSTIC: False,
    CONF_ANALYZE_DB: True,
    CONF_NOTIFY: True,
}
