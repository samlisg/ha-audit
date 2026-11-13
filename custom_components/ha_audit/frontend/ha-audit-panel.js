/*
 * HA Audit panel — custom element rendered inside Home Assistant (no iframe).
 * This file is public (static) but contains NO data: the report is fetched through
 * the authenticated WebSocket API (admin only) with hass.callWS().
 */
const RED = new Set(["ghost", "disref", "reg", "yerr", "dev", "res"]);
const OPEN_BY_DEFAULT = new Set(["ghost", "auto", "res", "reg", "dev"]);

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// tiny inline-markdown: `code`, **bold**, *italic* (input is escaped first)
const inline = (t) =>
  esc(t)
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\*(.+?)\*/g, "<i>$1</i>");

const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

const STYLE = `
:host{display:block;background:var(--primary-background-color);color:var(--primary-text-color);min-height:100vh;
  font-family:var(--paper-font-body1_-_font-family,Roboto,system-ui,sans-serif)}
.top{display:flex;align-items:center;gap:8px;height:var(--header-height,56px);padding:0 12px;
  background:var(--app-header-background-color,var(--primary-color));color:var(--app-header-text-color,#fff);
  position:sticky;top:0;z-index:5;padding-top:env(safe-area-inset-top)}
.top h1{font-size:20px;font-weight:400;margin:0;flex:1}
button.run{background:rgba(255,255,255,.18);color:inherit;border:0;border-radius:18px;padding:8px 14px;font:inherit;font-size:14px;cursor:pointer}
button.run[disabled]{opacity:.6;cursor:default}
button.icon{background:transparent;color:inherit;border:0;border-radius:50%;width:36px;height:36px;font-size:20px;cursor:pointer}
button.icon:hover{background:rgba(255,255,255,.18)}
.wrap{max-width:1100px;margin:0 auto;padding:14px;padding-bottom:calc(14px + env(safe-area-inset-bottom))}
.sub{color:var(--secondary-text-color);font-size:12.5px;margin:2px 0}
.hero{display:flex;gap:10px;margin:12px 0}
.pill{background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:14px;padding:8px 18px;text-align:center;min-width:96px}
.pill b{display:block;font-size:28px;line-height:1.1}
.pill span{font-size:11.5px;color:var(--secondary-text-color);text-transform:uppercase;letter-spacing:.04em}
.pill.pn b{color:var(--primary-color)}
input.q{width:100%;box-sizing:border-box;padding:11px 14px;border-radius:12px;border:1px solid var(--divider-color);
  background:var(--card-background-color);color:var(--primary-text-color);font-size:15px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:9px;margin-bottom:18px}
.tile{background:var(--card-background-color);border:1px solid var(--divider-color);border-left:5px solid var(--success-color,#2e9e5b);
  border-radius:12px;padding:10px 12px;cursor:pointer}
.tile.warn{border-left-color:var(--warning-color,#e0922b)}.tile.bad{border-left-color:var(--error-color,#d6453d)}
.tile.info{border-left-color:var(--secondary-text-color)}
.tile .n{font-size:22px;font-weight:700}
.tile .n small{font-size:11px;color:var(--primary-color);font-weight:600;margin-left:6px}
.tile .t{font-size:12.5px;color:var(--secondary-text-color)}
details{scroll-margin-top:72px;background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:14px;margin-bottom:10px;overflow:hidden}
summary{cursor:pointer;padding:12px 14px;font-weight:600;display:flex;justify-content:space-between;align-items:center;gap:10px;list-style:none}
summary::-webkit-details-marker{display:none}
summary .c{flex:none;background:var(--secondary-background-color);border-radius:99px;padding:1px 10px;font-size:13px;min-width:30px;text-align:center}
summary.bad .c{background:var(--error-color,#d6453d);color:#fff}
summary.warn .c{background:var(--warning-color,#e0922b);color:#fff}
summary.ok .c{background:var(--success-color,#2e9e5b);color:#fff}
.body{padding:2px 14px 12px;border-top:1px solid var(--divider-color)}
.g{font-weight:600;font-size:12.5px;color:var(--secondary-text-color);margin:12px 0 4px;text-transform:uppercase;letter-spacing:.04em}
ul{margin:4px 0;padding:0;list-style:none}
li{padding:5px 0;border-bottom:1px dashed var(--divider-color);word-break:break-word}
li:last-child{border:0}
code{background:var(--secondary-background-color);border-radius:6px;padding:1px 6px;font-size:13px}
.new{display:inline-block;background:var(--primary-color);color:#fff;border-radius:6px;font-size:10px;padding:0 6px;margin-left:6px;vertical-align:middle}
.empty,.more,.msg{color:var(--secondary-text-color);padding:10px 0;font-style:italic}
.msg{text-align:center;padding:40px 10px;font-style:normal}
footer{color:var(--secondary-text-color);font-size:12px;text-align:center;margin-top:18px}
`;

class HaAuditPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._narrow = false;
    this._report = null;
    this._running = false;
    this._error = null;
    this._open = new Set();
    this._seeded = false;
    this._query = "";
    this._unsub = null;
    this._subscribing = false;
    this._loading = false;
    this._again = false;
    this._pollTimer = null;
    this._ageTimer = null;
    this._onVis = () => { if (!document.hidden && this._hass) this._load(); };
    this._onReady = () => this._load();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="top"><span id="menu"></span><h1>HA Audit</h1><button class="icon" id="reload" title="Reload report">↻</button><button class="run" id="run">▶ Run now</button></div>
      <div class="wrap">
        <div id="head"></div>
        <input class="q" id="q" type="search" placeholder="🔎 Filter… (entity, card, automation)">
        <div id="grid" class="grid"></div>
        <div id="list"></div>
        <footer>Read-only audit · review manually before deleting anything</footer>
      </div>`;
    this.shadowRoot.getElementById("run").addEventListener("click", () => this._run());
    this.shadowRoot.getElementById("reload").addEventListener("click", () => this._load());
    this.shadowRoot.getElementById("q").addEventListener("input", (e) => {
      this._query = e.target.value.toLowerCase();
      this._applyFilter();
    });
    this.shadowRoot.getElementById("list").addEventListener("toggle", (e) => {
      const d = e.target;
      if (d.tagName === "DETAILS" && d.id) d.open ? this._open.add(d.id) : this._open.delete(d.id);
    }, true);
    this.shadowRoot.getElementById("grid").addEventListener("click", (e) => {
      const t = e.target.closest(".tile");
      if (!t) return;
      const d = this.shadowRoot.getElementById("s-" + t.dataset.key);
      if (d) { d.open = true; d.scrollIntoView({ behavior: "smooth", block: "start" }); }
    });
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    this._menu();
    if (first) { this._load(); this._subscribe(); }
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVis);
    this._ageTimer = setInterval(() => this._tickAge(), 30000);
    if (this._hass) { this._load(); this._subscribe(); }   // re-attached after navigation
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVis);
    clearInterval(this._ageTimer);
    clearTimeout(this._pollTimer);
    if (this._unsub) { try { this._unsub(); } catch (_e) { /* ignore */ } this._unsub = null; }
    try { this._hass?.connection?.removeEventListener?.("ready", this._onReady); } catch (_e) { /* ignore */ }
  }

  // Live updates: reload whenever ANY audit finishes (scheduled, service, automation, other tab).
  async _subscribe() {
    if (this._unsub || this._subscribing || !this._hass?.connection) return;
    this._subscribing = true;
    try {
      this._unsub = await this._hass.connection.subscribeEvents(() => this._load(), "ha_audit_completed");
      this._hass.connection.addEventListener?.("ready", this._onReady);   // missed events after a reconnect
    } catch (_e) { /* optional: polling + visibility reload still keep the view fresh */ }
    this._subscribing = false;
  }

  _pollIfRunning() {
    clearTimeout(this._pollTimer);
    if (this._running && this.isConnected) this._pollTimer = setTimeout(() => this._load(), 3000);
  }

  _tickAge() {
    const el = this.shadowRoot.getElementById("age");
    if (el && this._report) el.textContent = ago(this._report.generated);
  }
  set narrow(v) { this._narrow = v; this._menu(); }
  set panel(_v) {}
  set route(_v) {}

  _menu() {
    const slot = this.shadowRoot.getElementById("menu");
    if (!slot || !this._hass) return;
    let btn = slot.firstChild;
    if (!btn) { btn = document.createElement("ha-menu-button"); slot.appendChild(btn); }
    btn.hass = this._hass;
    btn.narrow = this._narrow;
  }

  async _load() {
    if (!this._hass) return;
    if (this._loading) { this._again = true; return; }
    this._loading = true;
    try {
      const res = await this._hass.callWS({ type: "ha_audit/report" });
      this._report = res.report;
      this._running = !!res.running;
      this._error = null;
    } catch (e) {
      this._error = (e && e.message) || String(e);
    }
    this._loading = false;
    this._render();
    this._pollIfRunning();
    if (this._again) { this._again = false; this._load(); }
  }

  async _run() {
    if (this._running) return;
    this._running = true;
    this._render();
    try {
      const res = await this._hass.callWS({ type: "ha_audit/run" });
      this._report = res.report;
      this._error = null;
    } catch (e) {
      this._error = (e && e.message) || String(e);
    }
    this._running = false;
    this._render();
    this._load();        // authoritative refresh (covers a run that was already in progress)
  }

  _render() {
    const root = this.shadowRoot;
    const run = root.getElementById("run");
    run.disabled = this._running;
    run.textContent = this._running ? "⏳ Running…" : "▶ Run now";
    root.getElementById("reload").disabled = this._loading;
    const head = root.getElementById("head");
    const grid = root.getElementById("grid");
    const list = root.getElementById("list");

    if (this._error) {
      head.innerHTML = `<div class="msg">⚠️ ${esc(this._error)}</div>`;
      grid.innerHTML = list.innerHTML = "";
      return;
    }
    const r = this._report;
    if (!r) {
      head.innerHTML = `<div class="msg">${this._running ? "⏳ Audit is running… this can take up to a minute."
        : "No report yet. Press <b>Run now</b> to create the first one."}</div>`;
      grid.innerHTML = list.innerHTML = "";
      return;
    }
    if (!this._seeded) {
      OPEN_BY_DEFAULT.forEach((k) => { if (r.sections.some((s) => s.key === k && s.total)) this._open.add("s-" + k); });
      this._seeded = true;
    }
    const m = r.meta, db = m.recorder || {};
    const runningNote = this._running ? `<div class="sub">⏳ A new audit is running — this view updates automatically.</div>` : "";
    const when = new Date(r.generated).toLocaleString();
    const dbtxt = db.rows ? ` · DB ${db.size_mb} MB, ${db.rows.toLocaleString()} rows` : "";
    head.innerHTML = `
      <div class="sub">${esc(when)} (<span id="age">${esc(ago(r.generated))}</span>) · ${m.entities} entities · ${m.devices} devices · ${m.entries} integrations${esc(dbtxt)}</div>
      <div class="sub">${esc((r.notes || [])[0] || "")}</div>${runningNote}
      <div class="hero">
        <div class="pill"><b>${r.summary.total}</b><span>findings</span></div>
        <div class="pill pn"><b>${r.summary.new}</b><span>new</span></div>
      </div>`;

    let tiles = "", blocks = "";
    for (const s of r.sections) {
      const cls = !s.counts ? "info" : s.total === 0 ? "" : RED.has(s.key) ? "bad" : "warn";
      const sev = !s.counts ? "" : s.total === 0 ? "ok" : RED.has(s.key) ? "bad" : "warn";
      const [emoji, ...rest] = s.title.split(" ");
      const label = rest.join(" ").replace(/\s*\(.*?\)\s*$/, "");
      tiles += `<div class="tile ${cls}" data-key="${s.key}"><div class="n">${s.total}${
        s.new ? `<small>+${s.new} new</small>` : ""}</div><div class="t">${emoji} ${esc(label)}</div></div>`;

      let body = "";
      if (!s.items.length) body = `<div class="empty">${esc(s.empty)}</div>`;
      else {
        const groups = new Map();
        for (const it of s.items) {
          const g = it.group || "";
          if (!groups.has(g)) groups.set(g, []);
          groups.get(g).push(it);
        }
        const grouped = s.items.some((i) => i.group);
        [...groups.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).forEach(([g, items]) => {
          if (grouped) body += `<div class="g">${esc(g || "other")} · ${items.length}</div>`;
          body += "<ul>" + items.map((i) => `<li>${inline(i.text)}${i.new ? '<span class="new">NEW</span>' : ""}</li>`).join("") + "</ul>";
        });
        if (s.truncated) body += `<div class="more">…and ${s.truncated} more not shown</div>`;
      }
      blocks += `<details id="s-${s.key}"${this._open.has("s-" + s.key) ? " open" : ""}>
        <summary class="${sev}"><span>${esc(s.title)}</span><span class="c">${s.total}</span></summary>
        <div class="body">${body}</div></details>`;
    }
    grid.innerHTML = tiles;
    list.innerHTML = blocks;
    this._applyFilter();
  }

  _applyFilter() {
    const q = this._query;
    this.shadowRoot.querySelectorAll("#list details").forEach((d) => {
      let any = false;
      d.querySelectorAll("li").forEach((li) => {
        const m = !q || li.textContent.toLowerCase().includes(q);
        li.style.display = m ? "" : "none";
        if (m) any = true;
      });
      d.style.display = !q || any ? "" : "none";
      if (q && any) d.open = true;
    });
  }
}

customElements.define("ha-audit-panel", HaAuditPanel);
