// Sidebar panel: which integration keeps the event loop busy, and where.
// Plain custom element, no build step. Home Assistant sets `hass`, `narrow`
// and `panel` on it.

const WINDOWS = [5, 15, 60];
const MENU_ICON =
  "M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z";

const CORE_NOTE =
  "Home Assistant itself, plus work that no single integration can be " +
  "blamed for, such as a library several integrations share or a protocol " +
  "implemented in compiled code.";

const escapeHtml = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

// Finding texts come from the translations: paragraphs, "- " lists and links,
// escaped first. Links go to http(s) or to a page of Home Assistant.
const renderText = (text) =>
  String(text)
    .split(/\n{2,}/)
    .map((block) => {
      const lines = block.split("\n");
      const inline = (line) =>
        escapeHtml(line).replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^)\s]*)\)/g, (_m, label, href) =>
          href.startsWith("/")
            ? `<a href="${href}" data-action="navigate" data-value="${href}">${label}</a>`
            : `<a href="${href}" target="_blank" rel="noreferrer">${label}</a>`
        );
      if (lines.every((line) => line.startsWith("- "))) {
        return `<ul>${lines.map((line) => `<li>${inline(line.slice(2))}</li>`).join("")}</ul>`;
      }
      const [first, ...rest] = lines;
      const list = rest.filter((line) => line.startsWith("- "));
      if (list.length && list.length === rest.length) {
        return `<p>${inline(first)}</p><ul>${list.map((line) => `<li>${inline(line.slice(2))}</li>`).join("")}</ul>`;
      }
      return `<p>${lines.map(inline).join("<br>")}</p>`;
    })
    .join("");

const plainText = (text) => String(text).replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
// Findings answer one question and get one window: long enough to hold, short enough to be today's.
const FINDINGS_MINUTES = 15;
const localTime = (epoch) => (epoch ? new Date(epoch * 1000).toLocaleTimeString() : "");
const isoTime = (epoch) => (epoch ? new Date(epoch * 1000).toISOString() : "");

const ms = (value) =>
  value >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1);

// A rate that may be missing: an empty cell is clearer than a zero
const rate = (value) => (value ? ms(value) : "");

// ms of CPU per minute as percent of one core
const corePercent = (value) => {
  const percent = value / 600;
  return percent >= 1 ? `${percent.toFixed(1)} %` : `${percent.toFixed(2)} %`;
};

const STYLE = `
  :host {
    display: block;
    min-height: 100%;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--ha-font-family-body, Roboto, sans-serif);
    --ii-mono: var(--ha-font-family-code, ui-monospace, SFMono-Regular, Menlo, monospace);
    --ii-radius: var(--ha-card-border-radius, 12px);
    --ii-card: var(--ha-card-background, var(--card-background-color, #fff));
  }
  header {
    position: sticky;
    top: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 56px;
    padding: 0 12px 0 16px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, var(--text-primary-color, #fff));
    border-bottom: var(--app-header-border-bottom, none);
  }
  header h1 {
    flex: 1;
    margin: 0;
    font-size: 20px;
    font-weight: 400;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .icon-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 40px;
    margin-left: -8px;
    border: none;
    border-radius: 50%;
    background: none;
    color: inherit;
    cursor: pointer;
  }
  .icon-button svg { width: 24px; height: 24px; fill: currentColor; }
  .segmented {
    display: inline-flex;
    border: 1px solid currentColor;
    border-radius: 18px;
    overflow: hidden;
    opacity: 0.95;
  }
  .segmented button {
    padding: 6px 12px;
    border: none;
    background: none;
    color: inherit;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .segmented button[aria-pressed="true"] {
    background: rgba(255, 255, 255, 0.22);
    font-weight: 500;
  }
  button:focus-visible, summary:focus-visible, .row:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
  header button:focus-visible { outline-color: currentColor; }
  .header-button {
    padding: 7px 12px;
    border: 1px solid currentColor;
    border-radius: 18px;
    background: none;
    color: inherit;
    font: inherit;
    font-size: 13px;
    cursor: pointer;
    white-space: nowrap;
  }
  main {
    max-width: 1100px;
    margin: 0 auto;
    padding: 16px;
    box-sizing: border-box;
  }
  .summary {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }
  .tile {
    padding: 12px 16px;
    border-radius: var(--ii-radius);
    background: var(--ii-card);
    border: 1px solid var(--divider-color);
  }
  .tile .label { font-size: 13px; color: var(--secondary-text-color); }
  .tile .value { margin-top: 4px; font-size: 24px; font-variant-numeric: tabular-nums; }
  .tile .sub { margin-top: 2px; font-size: 13px; color: var(--secondary-text-color); }
  .tile.warn .value { color: var(--warning-color, #ffa600); }
  .card {
    border-radius: var(--ii-radius);
    background: var(--ii-card);
    border: 1px solid var(--divider-color);
    overflow: hidden;
  }
  .card-head {
    display: grid;
    grid-template-columns: 1fr 96px 110px 90px 70px;
    gap: 12px;
    padding: 10px 16px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--secondary-text-color);
    border-bottom: 1px solid var(--divider-color);
  }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  button.sort {
    background: none; border: 0; padding: 0; margin: 0; font: inherit; color: inherit;
    text-transform: inherit; letter-spacing: inherit; cursor: pointer; text-align: right;
  }
  button.sort.active { color: var(--primary-text-color); }
  .card-head.mem, .row.mem { grid-template-columns: 1fr 100px 100px 84px 70px 60px; }
  .card-head.states, .row.states { grid-template-columns: 1fr 110px 110px 90px; }
  .delta { display: block; font-size: 12px; color: var(--secondary-text-color); }
  .delta.up { color: var(--error-color, #db4437); }
  .delta.down { color: var(--success-color, #43a047); }
  .row {
    display: grid;
    grid-template-columns: 1fr 96px 110px 90px 70px;
    gap: 12px;
    align-items: center;
    padding: 10px 16px;
    border-bottom: 1px solid var(--divider-color);
    cursor: pointer;
  }
  .row:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.04); }
  .row[aria-expanded="true"] { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.08); }
  .name { min-width: 0; }
  .name .title {
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .name .domain { font-size: 12px; color: var(--secondary-text-color); font-family: var(--ii-mono); }
  .chev { flex: none; width: 16px; transition: transform 0.15s; color: var(--secondary-text-color); }
  .row[aria-expanded="true"] .chev { transform: rotate(90deg); }
  .bar {
    height: 6px;
    margin-top: 6px;
    border-radius: 3px;
    background: var(--primary-color);
    min-width: 2px;
  }
  .badge {
    flex: none;
    padding: 1px 6px;
    border-radius: 8px;
    font-size: 11px;
    color: var(--secondary-text-color);
    border: 1px solid var(--divider-color);
  }
  .detail {
    padding: 4px 16px 16px 38px;
    border-bottom: 1px solid var(--divider-color);
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.03);
  }
  .detail h3 {
    margin: 16px 0 6px;
    font-size: 13px;
    font-weight: 500;
    color: var(--secondary-text-color);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .note { margin: 12px 0 0; font-size: 14px; color: var(--secondary-text-color); }
  .spot {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 4px 12px;
    padding: 6px 0;
    border-top: 1px solid var(--divider-color);
  }
  .spot:first-of-type { border-top: none; }
  .code { font-family: var(--ii-mono); font-size: 13px; overflow-wrap: anywhere; }
  .lines { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    padding: 1px 8px;
    border-radius: 10px;
    font-family: var(--ii-mono);
    font-size: 12px;
    background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.06);
  }
  .pairs { display: flex; flex-wrap: wrap; gap: 6px; }
  details { border-top: 1px solid var(--divider-color); }
  details:first-of-type { border-top: none; }
  summary { padding: 6px 0; cursor: pointer; font-size: 14px; }
  ol.frames {
    margin: 0 0 8px;
    padding-left: 24px;
    font-family: var(--ii-mono);
    font-size: 12px;
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .actions { display: flex; gap: 8px; margin-top: 16px; }
  .action {
    padding: 6px 14px;
    border: 1px solid var(--primary-color);
    border-radius: 16px;
    background: none;
    color: var(--primary-color);
    font: inherit;
    font-size: 13px;
    cursor: pointer;
  }
  .message {
    padding: 32px 16px;
    text-align: center;
    color: var(--secondary-text-color);
  }
  .error { color: var(--error-color, #db4437); }
  .verdict {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 16px;
    padding: 16px;
    border-radius: var(--ii-radius);
    background: var(--ii-card);
    box-shadow: var(--ha-card-box-shadow, none);
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
  }
  .verdict .mark { font-size: 28px; line-height: 1; }
  .verdict .head { font-size: 18px; }
  .verdict .sub { margin-top: 2px; font-size: 13px; color: var(--secondary-text-color); }
  .verdict.ok .mark { color: var(--success-color, #43a047); }
  .verdict.notice .mark { color: var(--warning-color, #ffa600); }
  .verdict.problem .mark { color: var(--error-color, #db4437); }
  .finding {
    margin-bottom: 12px;
    padding: 16px;
    border-radius: var(--ii-radius);
    background: var(--ii-card);
    box-shadow: var(--ha-card-box-shadow, none);
    border: 1px solid var(--divider-color, rgba(0, 0, 0, 0.12));
    border-left: 4px solid var(--info-color, #039be5);
  }
  .finding.notice { border-left-color: var(--warning-color, #ffa600); }
  .finding.problem { border-left-color: var(--error-color, #db4437); }
  .finding h2 { margin: 0 0 8px; font-size: 16px; font-weight: 500; }
  .finding .text p { margin: 0 0 8px; }
  .finding .text ul { margin: 0 0 8px; padding-left: 20px; }
  .finding .text a { color: var(--primary-color); }
  .finding .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .finding .actions button {
    padding: 6px 14px;
    border: 1px solid var(--primary-color);
    border-radius: 16px;
    background: none;
    color: var(--primary-color);
    font: inherit;
    font-size: 14px;
    cursor: pointer;
  }
  .finding details { margin-top: 10px; font-size: 13px; }
  .finding details ul { margin: 6px 0 0; padding-left: 20px; font-family: var(--ii-mono); word-break: break-word; }
  .footer { margin-top: 12px; font-size: 13px; color: var(--secondary-text-color); }
  @media (max-width: 600px) {
    main { padding: 12px; }
    .card-head, .row { grid-template-columns: 1fr 64px 76px 56px; gap: 8px; }
    .card-head.mem, .row.mem { grid-template-columns: 1fr 64px 64px 56px; }
    .card-head.states, .row.states { grid-template-columns: 1fr 76px 76px 56px; }
    .col-wide { display: none; }
    .col-samples { display: none; }
    .detail { padding-left: 16px; }
    /* Four views and three windows do not fit one row: wrap, title goes */
    header { flex-wrap: wrap; padding: 8px 12px; gap: 8px; }
    .header-button { flex: 1 1 100%; margin: 0; }
    header h1 { display: none; }
    .segmented { display: flex; flex: 1 1 100%; }
    .segmented button { flex: 1; padding: 8px 4px; }
  }
`;

class IntegrationInsightsPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._minutes = 60;
    this._data = undefined;
    this._error = undefined;
    this._expanded = new Set();
    this._openStacks = new Set();
    this._details = new Map();
    this._view = "overview";
    this._sort = "cpu_ms_per_min";
    this._sortAscending = false;
    this._statesSort = "state_changes_per_min";
    this._statesAscending = false;
    this._logsSort = "log_warnings_per_min";
    this._logsAscending = false;
    this._filesSort = "store_writes_per_min";
    this._filesAscending = false;
    this._findings = undefined;
    this._memory = undefined;
    this._memExpanded = new Set();
    this._snapshotting = false;
    this._onClick = this._onClick.bind(this);
    this._onKeyDown = this._onKeyDown.bind(this);
    this._onToggle = this._onToggle.bind(this);
    this._onVisibility = this._onVisibility.bind(this);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._subscribe();
  }

  set narrow(narrow) {
    if (this._narrow === narrow) return;
    this._narrow = narrow;
    this._render();
  }

  set panel(_panel) {}

  connectedCallback() {
    this.shadowRoot.addEventListener("click", this._onClick);
    this.shadowRoot.addEventListener("keydown", this._onKeyDown);
    this.shadowRoot.addEventListener("toggle", this._onToggle, true);
    document.addEventListener("visibilitychange", this._onVisibility);
    this._render();
    if (this._hass) this._subscribe();
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._onClick);
    this.shadowRoot.removeEventListener("keydown", this._onKeyDown);
    this.shadowRoot.removeEventListener("toggle", this._onToggle, true);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._unsubscribe();
  }

  // The server pushes the summary after every drain; nothing runs while the
  // panel is hidden or closed.
  _onVisibility() {
    if (document.hidden) this._unsubscribe();
    else this._subscribe();
  }

  async _subscribe() {
    if (!this._hass || this._token || document.hidden || this._view === "memory") return;
    const token = (this._token = {});
    try {
      const unsub = await this._hass.connection.subscribeMessage(
        (message) => this._onSummary(message),
        { type: "integration_insights/subscribe", minutes: this._minutes }
      );
      if (this._token !== token) unsub();
      else this._unsub = unsub;
    } catch (err) {
      if (this._token === token) this._token = undefined;
      this._error = err && err.message ? err.message : String(err);
      this._render();
    }
  }

  _unsubscribe() {
    this._token = undefined;
    if (this._unsub) {
      this._unsub();
      this._unsub = undefined;
    }
  }

  _onSummary(message) {
    this._data = message;
    this._error = undefined;
    this._updated = new Date();
    for (const domain of this._expanded) this._loadDetail(domain);
    if (this._view === "overview") this._loadFindings();
    this._render();
  }

  async _loadFindings() {
    if (this._findingsPending) return;
    this._findingsPending = true;
    try {
      this._findings = await this._hass.connection.sendMessagePromise({
        type: "integration_insights/findings",
        minutes: FINDINGS_MINUTES,
        language: this._hass.locale?.language || this._hass.language,
      });
      this._error = undefined;
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    } finally {
      this._findingsPending = false;
    }
    this._render();
  }

  async _loadMemory(take) {
    if (this._snapshotting) return;
    this._snapshotting = take;
    this._render();
    try {
      this._memory = await this._hass.connection.sendMessagePromise({
        type: take ? "integration_insights/memory_snapshot" : "integration_insights/memory",
      });
      this._error = undefined;
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    } finally {
      this._snapshotting = false;
    }
    this._render();
  }

  async _loadDetail(domain) {
    const minutes = this._minutes;
    try {
      const detail = await this._hass.connection.sendMessagePromise({
        type: "integration_insights/detail",
        domain,
        minutes,
      });
      if (minutes !== this._minutes) return;
      if (this._expanded.has(domain)) this._details.set(domain, detail);
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    }
    this._render();
  }

  _onClick(ev) {
    const target = ev.target.closest("[data-action]");
    if (!target) return;
    const { action, value } = target.dataset;
    if (action === "menu") {
      this.dispatchEvent(
        new Event("hass-toggle-menu", { bubbles: true, composed: true })
      );
    } else if (action === "window") {
      this._minutes = Number(value);
      this._unsubscribe();
      this._details.clear();
      this._render();
      this._subscribe();
    } else if (action === "toggle") {
      if (this._expanded.has(value)) {
        this._expanded.delete(value);
        this._details.delete(value);
      } else {
        this._expanded.add(value);
        this._loadDetail(value);
      }
      this._render();
    } else if (action === "view") {
      this._view = value;
      if (value !== "memory") {
        this._subscribe();
        if (value === "overview" && this._data) this._loadFindings();
      } else {
        this._unsubscribe();
        if (!this._memory) this._loadMemory(false);
      }
      this._render();
    } else if (action === "sort") {
      if (this._sort === value) this._sortAscending = !this._sortAscending;
      else {
        this._sort = value;
        this._sortAscending = false;
      }
      this._render();
    } else if (action === "sort-logs" || action === "sort-files") {
      const which = action === "sort-logs" ? "_logs" : "_files";
      if (this[`${which}Sort`] === value) this[`${which}Ascending`] = !this[`${which}Ascending`];
      else {
        this[`${which}Sort`] = value;
        this[`${which}Ascending`] = false;
      }
      this._render();
    } else if (action === "sort-states") {
      if (this._statesSort === value) this._statesAscending = !this._statesAscending;
      else {
        this._statesSort = value;
        this._statesAscending = false;
      }
      this._render();
    } else if (action === "snapshot") {
      this._loadMemory(true);
    } else if (action === "memtoggle") {
      if (this._memExpanded.has(value)) this._memExpanded.delete(value);
      else this._memExpanded.add(value);
      this._render();
    } else if (action === "download") {
      this._download();
    } else if (action === "copy") {
      this._copy(value, target);
    } else if (action === "copyfinding") {
      this._copyFinding(Number(value), target);
    } else if (action === "show") {
      this._view = "cpu";
      this._expanded.add(value);
      this._loadDetail(value);
      this._render();
    } else if (action === "navigate") {
      ev.preventDefault();
      history.pushState(null, "", value);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
    }
  }

  _onKeyDown(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.target.closest('.row[data-action="toggle"], .row[data-action="memtoggle"]');
    if (!row) return;
    ev.preventDefault();
    row.click();
  }

  _onToggle(ev) {
    const key = ev.target.dataset && ev.target.dataset.stack;
    if (!key) return;
    if (ev.target.open) this._openStacks.add(key);
    else this._openStacks.delete(key);
  }

  async _download() {
    if (!this._data) return;
    let full;
    try {
      full = await this._hass.connection.sendMessagePromise({
        type: "integration_insights/report",
        minutes: this._minutes,
      });
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
      this._render();
      return;
    }
    // Same content as the diagnostics download: no entry titles or names.
    const blob = new Blob([JSON.stringify(full.report, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `integration-insights-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async _copy(domain, button) {
    const detail = this._details.get(domain);
    if (!detail || !detail.item) return;
    await this._toClipboard(this._describe(detail), button, "Copy as text");
  }

  // What a developer needs to look into a finding, ready for an issue.
  async _copyFinding(index, button) {
    const finding = this._findings?.findings[index];
    if (!finding) return;
    const config = this._hass.config || {};
    const lines = [`Integration Insights: ${finding.title}`, ""];
    if (finding.domain) {
      lines.push(
        `Integration: ${finding.name} (${finding.domain})${finding.version ? `, version ${finding.version}` : ""}`
      );
    }
    lines.push(`Home Assistant ${config.version || "?"}, measured over the last ${FINDINGS_MINUTES} minutes`, "");
    lines.push(plainText(finding.description.split("\n\n")[0]));
    if (finding.evidence.length) {
      lines.push("", "Details:", ...finding.evidence.map((line) => `- ${line}`));
    }
    await this._toClipboard(lines.join("\n"), button, "Copy report for the developer");
  }

  async _toClipboard(text, button, label) {
    let done = false;
    try {
      await navigator.clipboard.writeText(text);
      done = true;
    } catch (_err) {
      // No clipboard API over plain http: fall back to a selection.
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      done = document.execCommand("copy");
      area.remove();
    }
    button.textContent = done ? "Copied" : "Copy failed";
    setTimeout(() => {
      button.textContent = label;
    }, 1500);
  }

  // A plain text summary for a bug report or an LLM.
  _describe(detail) {
    const item = detail.item;
    const report = this._data.report;
    const lines = [
      `Integration Insights: ${item.domain}`,
      `Event loop CPU: ${item.loop_cpu_ms_per_min} ms per minute ` +
        `(${corePercent(item.loop_cpu_ms_per_min)} of one core), ` +
        `${item.samples} samples over ${report.window_minutes} min` +
        (item.low_confidence ? " (few samples)" : ""),
    ];
    if (item.state_writes) {
      const sw = item.state_writes;
      lines.push(
        `State writes: ${sw.per_min} per min, ${sw.changes_per_min} changes, ${sw.attributes_only_per_min} attributes only, ${sw.repeated_per_min} repeated`
      );
      for (const [entityId, value] of Object.entries(sw.entities || {})) {
        lines.push(`- ${entityId}: ${value} per min`);
      }
      if (sw.attribute_kib_per_min) {
        lines.push(`Changed attributes: ${sw.attribute_kib_per_min} KiB per min`);
        for (const [entityId, value] of Object.entries(sw.attribute_entities || {})) {
          lines.push(`- ${entityId}: ${value} KiB per min`);
        }
      }
    }
    if (item.logs) {
      const lg = item.logs;
      lines.push(
        `Log records: ${lg.warnings_per_min} warnings and ${lg.errors_per_min} errors per min, ${lg.debug_per_min} debug and info`
      );
      for (const [name, value] of Object.entries(lg.loggers)) {
        lines.push(`- ${name}: ${value} per min`);
      }
    }
    if (item.storage) {
      const st = item.storage;
      lines.push(`Storage writes: ${st.writes_per_min} per min, ${st.kib_per_min} KiB per min`);
      for (const [key, value] of Object.entries(st.files)) {
        lines.push(`- ${key}: ${value} per min`);
      }
    }
    if (item.stall) {
      lines.push(
        `Event loop stood still: ${item.stall.ms} ms at ${isoTime(item.stall.at)}, running ${item.stall.function}`
      );
    }
    if (item.callbacks) {
      const cb = item.callbacks;
      lines.push(
        `Event loop callbacks: ${cb.wall_ms_per_min} ms/min wall time, ${cb.per_min} per min, ` +
          `longest ${cb.longest_ms} ms at ${isoTime(cb.longest_at)} in ${cb.longest_function}`
      );
      for (const row of cb.by_function) {
        lines.push(`- ${row.function}: ${row.wall_ms_per_min} ms/min, ${row.per_min} per min`);
      }
    }
    if (item.executor) {
      const ex = item.executor;
      lines.push(
        `Executor jobs: ${ex.cpu_ms_per_min} ms/min CPU, ${ex.wall_ms_per_min} ms/min wall, ${ex.jobs_per_min} jobs/min`
      );
      for (const job of ex.jobs) {
        lines.push(`- ${job.function}: ${job.cpu_ms_per_min} ms/min CPU, ${job.per_min} per min`);
      }
    }
    if (item.threads) {
      lines.push(`Threads: ${item.threads.cpu_ms_per_min} ms/min CPU`);
      for (const [name, value] of Object.entries(item.threads.by_name)) {
        lines.push(`- ${name}: ${value} ms/min`);
      }
    }
    if (item.via_library_ms_per_min) {
      lines.push(
        `Charged through libraries only this integration uses: ${item.via_library_ms_per_min} ms/min`
      );
    }
    if (item.via_context_ms_per_min) {
      lines.push(
        `Charged through the config entry context (core and shared library code run for this integration): ${item.via_context_ms_per_min} ms/min`
      );
    }
    lines.push("", "Hot spots (ms/min, hottest lines):");
    for (const spot of item.hot_spots) {
      const spotLines = Object.entries(spot.lines)
        .map(([line, value]) => `line ${line}: ${value}`)
        .join(", ");
      lines.push(`- ${spot.function}: ${spot.loop_cpu_ms_per_min} (${spotLines})`);
    }
    const pairs = (title, values) => {
      const entries = Object.entries(values);
      if (!entries.length) return;
      lines.push("", `${title}:`);
      for (const [key, value] of entries) lines.push(`- ${key}: ${value} ms/min`);
    };
    const names = (values, lookup) =>
      Object.fromEntries(
        Object.entries(values).map(([key, value]) => [lookup[key] ? `${lookup[key]} (${key})` : key, value])
      );
    pairs("Devices", names(item.devices, detail.device_names));
    pairs("Entities", names(item.entities, detail.entity_names));
    pairs("Libraries", item.libraries);
    pairs("Triggered by", item.triggered_by);
    item.stacks.forEach((stack, index) => {
      lines.push("", `Stack ${index + 1} (${stack.loop_cpu_ms_per_min} ms/min, innermost first):`);
      for (const frame of stack.frames) lines.push(`  ${frame}`);
    });
    return lines.join("\n");
  }

  _render() {
    const root = this.shadowRoot;
    const focusedRow = root.activeElement?.closest?.(".row")?.dataset.value;
    root.innerHTML = `<style>${STYLE}</style>${this._renderHeader()}<main>${this._renderBody()}</main>`;
    if (focusedRow) {
      root.querySelector(`.row[data-value="${CSS.escape(focusedRow)}"]`)?.focus();
    }
  }

  _renderHeader() {
    const menu = this._narrow
      ? `<button class="icon-button" data-action="menu" aria-label="Menu"><svg viewBox="0 0 24 24"><path d="${MENU_ICON}"></path></svg></button>`
      : "";
    const windows = WINDOWS.map(
      (minutes) =>
        `<button data-action="window" data-value="${minutes}" aria-pressed="${minutes === this._minutes}">${minutes} min</button>`
    ).join("");
    const views = [
      ["overview", "Overview"],
      ["cpu", "CPU"],
      ["states", "States"],
      ["logs", "Logs"],
      ["memory", "Memory"],
    ]
      .map(
        ([view, label]) =>
          `<button data-action="view" data-value="${view}" aria-pressed="${view === this._view}">${label}</button>`
      )
      .join("");
    const tools =
      this._view === "states" || this._view === "logs"
        ? `<div class="segmented" role="group" aria-label="Time window">${windows}</div>`
        : this._view === "overview"
        ? ""
        : this._view === "cpu"
        ? `<div class="segmented" role="group" aria-label="Time window">${windows}</div>
      <button class="header-button" data-action="download" ${this._data ? "" : "disabled"}>Download report</button>`
        : `<button class="header-button" data-action="snapshot" ${this._snapshotting ? "disabled" : ""}>${this._snapshotting ? "Taking snapshot…" : "Take snapshot"}</button>`;
    return `<header>${menu}<h1>Integration Insights</h1>
      <div class="segmented" role="group" aria-label="View">${views}</div>
      ${tools}
    </header>`;
  }

  _renderBody() {
    if (this._view === "memory") return this._renderMemory();
    if (this._view === "overview") return this._renderOverview();
    if (this._view === "states") return this._renderStates();
    if (this._view === "logs") return this._renderLogs();
    if (!this._data) {
      return this._error
        ? `<div class="message error">${escapeHtml(this._error)}</div>`
        : `<div class="message">Loading…</div>`;
    }
    const { report } = this._data;
    const sampler = report.sampler;
    const samplesPerMinute = report.samples / Math.max(report.window_minutes, 1 / 60);
    const samplerCost = (sampler.handler_cpu_us_avg * samplesPerMinute) / 1000;
    // Timing every callback is measured exactly; part of it also shows as core in the samples.
    const timingCost = sampler.timing_cpu_ms_per_min || 0;
    const problems = sampler.samples_lost + sampler.handler_errors + (sampler.timing_errors || 0);
    const integrations = report.integrations;
    // The sampler's own time is never sampled, so it is added to what this integration does.
    const ownWork = integrations.find((item) => item.domain === "integration_insights")?.cpu_ms_per_min || 0;
    const top = integrations.length ? integrations[0].cpu_ms_per_min : 0;
    const background = report.executor_cpu_ms_per_min + report.thread_cpu_ms_per_min;

    const tiles = `<div class="summary">
      <div class="tile"><div class="label">Event loop CPU</div>
        <div class="value">${ms(report.loop_cpu_ms_per_min)} ms/min</div>
        <div class="sub">${corePercent(report.loop_cpu_ms_per_min)} of one core</div></div>
      <div class="tile"><div class="label">Event loop busy</div>
        <div class="value">${ms(report.loop_busy_ms_per_min)} ms/min</div>
        <div class="sub">wall time, includes waits for the GIL or blocking calls</div></div>
      <div class="tile"><div class="label">Background CPU</div>
        <div class="value">${ms(background)} ms/min</div>
        <div class="sub">executor ${ms(report.executor_cpu_ms_per_min)} · threads ${ms(report.thread_cpu_ms_per_min)}</div></div>
      <div class="tile"><div class="label">Window</div>
        <div class="value">${report.window_minutes} min</div>
        <div class="sub">${report.samples.toLocaleString()} samples</div></div>
      <div class="tile"><div class="label">Measuring costs</div>
        <div class="value">${ms(samplerCost + timingCost + ownWork)} ms/min</div>
        <div class="sub">sampling ${ms(samplerCost)} · timing ${ms(timingCost)} · own work ${ms(ownWork)}</div></div>
      <div class="tile${problems ? " warn" : ""}"><div class="label">Sampler</div>
        <div class="value">${problems ? `${problems} problems` : "OK"}</div>
        <div class="sub">lost ${sampler.samples_lost} · errors ${sampler.handler_errors + (sampler.timing_errors || 0)} · slowdowns ${sampler.backoffs}</div></div>
    </div>`;

    if (!integrations.length) {
      return `${tiles}<div class="card"><div class="message">No samples yet. The loop has been idle.</div></div>`;
    }

    const key = this._sort;
    const direction = this._sortAscending ? 1 : -1;
    const sorted = [...integrations].sort(
      (a, b) => direction * ((a[key] || 0) - (b[key] || 0)) || a.domain.localeCompare(b.domain)
    );
    const rows = sorted
      .map((item) => {
        const expanded = this._expanded.has(item.domain);
        const name =
          item.domain === "core"
            ? "Home Assistant core and unassigned work"
            : this._data.names[item.domain] || item.domain;
        const width = top ? Math.max((item.cpu_ms_per_min / top) * 100, 1) : 0;
        const badge = item.low_confidence
          ? `<span class="badge" title="Fewer than 20 samples: the number is rough">few samples</span>`
          : "";
        return `<div class="row" data-action="toggle" data-value="${escapeHtml(item.domain)}" role="button" tabindex="0" aria-expanded="${expanded}">
            <div class="name">
              <div class="title"><svg class="chev" viewBox="0 0 24 24"><path fill="currentColor" d="M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z"></path></svg>
                <span>${escapeHtml(name)}</span>${badge}</div>
              <div class="domain">${escapeHtml(item.domain)}</div>
              <div class="bar" style="width:${width.toFixed(1)}%"></div>
            </div>
            <div class="num">${ms(item.loop_cpu_ms_per_min)}</div>
            <div class="num">${ms(item.background_cpu_ms_per_min)}</div>
            <div class="num">${corePercent(item.cpu_ms_per_min)}</div>
            <div class="num col-samples">${item.samples}</div>
          </div>${expanded ? this._renderDetail(item) : ""}`;
      })
      .join("");

    const updated = this._updated ? this._updated.toLocaleTimeString() : "";
    return `${tiles}<div class="card">
        <div class="card-head"><div>Integration</div>${this._sortHeader("loop_cpu_ms_per_min", "Loop ms/min")}${this._sortHeader("background_cpu_ms_per_min", "Background")}${this._sortHeader("cpu_ms_per_min", "of a core")}${this._sortHeader("samples", "Samples", "col-samples")}</div>
        ${rows}
      </div>
      <div class="footer">CPU time on the event loop, sampled every ${sampler.interval_ms} ms of loop CPU. Callbacks, executor jobs and threads are measured exactly. Updated ${updated}${this._error ? ` · <span class="error">${escapeHtml(this._error)}</span>` : ""}</div>`;
  }

  _renderStates() {
    if (!this._data) {
      return this._error
        ? `<div class="message error">${escapeHtml(this._error)}</div>`
        : `<div class="message">Loading…</div>`;
    }
    const { report } = this._data;
    const key = this._statesSort;
    const direction = this._statesAscending ? 1 : -1;
    const integrations = report.integrations
      .filter((item) => item.state_writes_per_min)
      .sort((a, b) => direction * ((a[key] || 0) - (b[key] || 0)) || a.domain.localeCompare(b.domain));
    const sum = (key) => integrations.reduce((total, item) => total + (item[key] || 0), 0);
    const changes = sum("state_changes_per_min");
    const attributesOnly = sum("state_attributes_only_per_min");
    const repeated = sum("state_repeated_per_min");
    const attributeKib = sum("attribute_kib_per_min");
    const busiest = [...integrations].sort((a, b) => b.state_writes_per_min - a.state_writes_per_min)[0];
    const tiles = `<div class="summary">
      <div class="tile"><div class="label">Changes</div>
        <div class="value">${ms(changes)} per min</div>
        <div class="sub">the state changed. Recorded, sent to the dashboard and every listener</div></div>
      <div class="tile"><div class="label">Attributes only</div>
        <div class="value">${ms(attributesOnly)} per min</div>
        <div class="sub">same state, other attributes. Costs as much as a change</div></div>
      <div class="tile"><div class="label">Repeated</div>
        <div class="value">${ms(repeated)} per min</div>
        <div class="sub">nothing changed. Cheap, only the last reported time moves</div></div>
      <div class="tile"><div class="label">Changed attributes</div>
        <div class="value">${ms(attributeKib)} KiB per min</div>
        <div class="sub">stored again by the recorder each time they change</div></div>
      <div class="tile"><div class="label">Busiest</div>
        <div class="value">${busiest ? escapeHtml(this._data.names[busiest.domain] || busiest.domain) : "none"}</div>
        <div class="sub">${busiest ? `${ms(busiest.state_writes_per_min)} writes per min` : "no state writes yet"}</div></div>
      <div class="tile"><div class="label">Window</div>
        <div class="value">${report.window_minutes} min</div>
        <div class="sub">counted at the event bus, exactly</div></div>
    </div>`;
    if (!integrations.length) {
      return `${tiles}<div class="card"><div class="message">No state writes in this window yet.</div></div>`;
    }
    const top = Math.max(...integrations.map((item) => item[key] || 0), 1);
    const rows = integrations
      .map((item) => {
        const expanded = this._expanded.has(item.domain);
        const name = this._data.names[item.domain] || item.domain;
        const width = Math.max(((item[key] || 0) / top) * 100, 1);
        return `<div class="row states" data-action="toggle" data-value="${escapeHtml(item.domain)}" role="button" tabindex="0" aria-expanded="${expanded}">
            <div class="name">
              <div class="title"><svg class="chev" viewBox="0 0 24 24"><path fill="currentColor" d="M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z"></path></svg>
                <span>${escapeHtml(name)}</span></div>
              <div class="domain">${escapeHtml(item.domain)}</div>
              <div class="bar" style="width:${width.toFixed(1)}%"></div>
            </div>
            <div class="num">${ms(item.state_changes_per_min || 0)}</div>
            <div class="num">${ms(item.state_attributes_only_per_min || 0)}</div>
            <div class="num">${ms(item.state_repeated_per_min || 0)}</div>
          </div>${expanded ? this._renderStateDetail(item) : ""}`;
      })
      .join("");
    const updated = this._updated ? this._updated.toLocaleTimeString() : "";
    return `${tiles}<div class="card">
        <div class="card-head states"><div>Integration</div>${this._statesHeader("state_changes_per_min", "Changes")}${this._statesHeader("state_attributes_only_per_min", "Attributes")}${this._statesHeader("state_repeated_per_min", "Repeated")}</div>
        ${rows}
      </div>
      <div class="footer">State writes per minute, counted exactly at the event bus. Changes and attribute-only changes reach the recorder, the dashboard and every automation that watches the entity. Repeated writes do not. Updated ${updated}</div>`;
  }

  _renderStateDetail(brief) {
    const detail = this._details.get(brief.domain);
    if (!detail) return `<div class="detail"><p class="note">Loading…</p></div>`;
    const item = detail.item;
    const sw = item && item.state_writes;
    if (!sw) return `<div class="detail"><p class="note">No state writes in this window.</p></div>`;
    const names = detail.entity_names || {};
    const chips = Object.entries(sw.entities)
      .map(
        ([entityId, value]) =>
          `<span class="chip" title="${escapeHtml(entityId)}">${escapeHtml(names[entityId] || entityId)} · ${value}/min</span>`
      )
      .join("");
    const sizes = Object.entries(sw.attribute_entities || {})
      .map(
        ([entityId, value]) =>
          `<span class="chip" title="${escapeHtml(entityId)}">${escapeHtml(names[entityId] || entityId)} · ${value} KiB/min</span>`
      )
      .join("");
    const attributes = sw.attribute_kib_per_min
      ? `<h3>Changed attributes, ${sw.attribute_kib_per_min} KiB per minute</h3><div class="pairs">${sizes}</div>`
      : "";
    return `<div class="detail"><p class="note">${sw.per_min} writes per minute: ${sw.changes_per_min} changes, ${sw.attributes_only_per_min} changed only attributes, ${sw.repeated_per_min} repeated.</p><h3>Most changes</h3><div class="pairs">${chips}</div>${attributes}</div>`;
  }

  _renderLogs() {
    if (!this._data) {
      return this._error
        ? `<div class="message error">${escapeHtml(this._error)}</div>`
        : `<div class="message">Loading…</div>`;
    }
    const { report } = this._data;
    const all = report.integrations;
    const sum = (items, key) => items.reduce((total, item) => total + (item[key] || 0), 0);
    const logged = all.filter(
      (item) => item.log_warnings_per_min || item.log_errors_per_min || item.log_debug_per_min
    );
    const saving = all.filter((item) => item.store_writes_per_min);
    const sorted = (items, key, ascending) =>
      [...items].sort(
        (a, b) => (ascending ? 1 : -1) * ((a[key] || 0) - (b[key] || 0)) || a.domain.localeCompare(b.domain)
      );
    const tiles = `<div class="summary">
      <div class="tile"><div class="label">Warnings</div>
        <div class="value">${ms(sum(logged, "log_warnings_per_min"))} per min</div>
        <div class="sub">a steady stream hides the messages that matter</div></div>
      <div class="tile"><div class="label">Errors</div>
        <div class="value">${ms(sum(logged, "log_errors_per_min"))} per min</div>
        <div class="sub">errors and critical messages</div></div>
      <div class="tile"><div class="label">Debug and info</div>
        <div class="value">${ms(sum(logged, "log_debug_per_min"))} per min</div>
        <div class="sub">a lot of them usually means debug logging was left on</div></div>
      <div class="tile"><div class="label">Saved files</div>
        <div class="value">${ms(sum(saving, "store_writes_per_min"))} per min</div>
        <div class="sub">${ms(sum(saving, "store_kib_per_min"))} KiB per min, every save replaces a whole file</div></div>
      <div class="tile"><div class="label">Window</div>
        <div class="value">${report.window_minutes} min</div>
        <div class="sub">counted where they happen, exactly</div></div>
    </div>`;
    const row = (item, cells) => {
      const expanded = this._expanded.has(item.domain);
      const name = this._data.names[item.domain] || item.domain;
      return `<div class="row states" data-action="toggle" data-value="${escapeHtml(item.domain)}" role="button" tabindex="0" aria-expanded="${expanded}">
          <div class="name">
            <div class="title"><svg class="chev" viewBox="0 0 24 24"><path fill="currentColor" d="M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z"></path></svg>
              <span>${escapeHtml(name)}</span></div>
            <div class="domain">${escapeHtml(item.domain)}</div>
          </div>
          ${cells.map((value) => `<div class="num">${value}</div>`).join("")}
        </div>${expanded ? this._renderOutputDetail(item) : ""}`;
    };
    const header = (action, sortKey, ascending, columns) =>
      columns
        .map(([key, label]) => {
          const active = sortKey === key;
          const arrow = active ? (ascending ? " ▲" : " ▼") : "";
          return `<button class="num sort${active ? " active" : ""}" data-action="${action}" data-value="${key}" aria-sort="${active ? (ascending ? "ascending" : "descending") : "none"}">${label}${arrow}</button>`;
        })
        .join("");
    const logRows = sorted(logged, this._logsSort, this._logsAscending)
      .map((item) =>
        row(item, [
          rate(item.log_warnings_per_min),
          rate(item.log_errors_per_min),
          rate(item.log_debug_per_min),
        ])
      )
      .join("");
    const fileRows = sorted(saving, this._filesSort, this._filesAscending)
      .map((item) => row(item, [rate(item.store_writes_per_min), ms(item.store_kib_per_min || 0), ""]))
      .join("");
    const logs = logged.length
      ? `<div class="card">
        <div class="card-head states"><div>Log</div>${header("sort-logs", this._logsSort, this._logsAscending, [
          ["log_warnings_per_min", "Warnings"],
          ["log_errors_per_min", "Errors"],
          ["log_debug_per_min", "Debug"],
        ])}</div>${logRows}</div>`
      : `<div class="card"><div class="message">Nothing logged in this window.</div></div>`;
    const files = saving.length
      ? `<div class="card">
        <div class="card-head states"><div>Saved files</div>${header("sort-files", this._filesSort, this._filesAscending, [
          ["store_writes_per_min", "Saves"],
          ["store_kib_per_min", "KiB"],
        ])}<div></div></div>${fileRows}</div>`
      : `<div class="card"><div class="message">No files saved in this window.</div></div>`;
    const updated = this._updated ? this._updated.toLocaleTimeString() : "";
    return `${tiles}${logs}${files}
      <div class="footer">Per minute. Log records are counted by logger as they reach Home Assistant's log, files when they are saved through Home Assistant's storage helper. Updated ${updated}</div>`;
  }

  _renderOutputDetail(brief) {
    const detail = this._details.get(brief.domain);
    if (!detail) return `<div class="detail"><p class="note">Loading…</p></div>`;
    const item = detail.item || {};
    const chips = (entries, unit) =>
      Object.entries(entries || {})
        .map(([name, value]) => `<span class="chip">${escapeHtml(name)} · ${value}${unit}</span>`)
        .join("");
    const parts = [];
    if (item.logs) {
      parts.push(`<h3>Loggers with the most warnings and errors</h3><div class="pairs">${chips(item.logs.loggers, "/min") || "none"}</div>`);
    }
    if (item.storage) {
      parts.push(`<h3>Files saved most</h3><div class="pairs">${chips(item.storage.files, "/min")}</div>`);
    }
    return `<div class="detail">${parts.join("") || `<p class="note">Nothing in this window.</p>`}</div>`;
  }

  _statesHeader(key, label) {
    const active = this._statesSort === key;
    const arrow = active ? (this._statesAscending ? " ▲" : " ▼") : "";
    return `<button class="num sort${active ? " active" : ""}" data-action="sort-states" data-value="${key}" aria-sort="${active ? (this._statesAscending ? "ascending" : "descending") : "none"}">${label}${arrow}</button>`;
  }

  _sortHeader(key, label, extra = "") {
    const active = this._sort === key;
    const arrow = active ? (this._sortAscending ? " ▲" : " ▼") : "";
    return `<button class="num sort${active ? " active" : ""} ${extra}" data-action="sort" data-value="${key}" aria-sort="${active ? (this._sortAscending ? "ascending" : "descending") : "none"}">${label}${arrow}</button>`;
  }

  _ownerName(owner) {
    if (owner.startsWith("lib:")) return `Library ${owner.slice(4)}`;
    if (owner.startsWith("core:")) return `Core: ${owner.slice(5)}`;
    if (owner === "core") return "Home Assistant core code";
    if (owner === "python") return "Python standard library";
    if (owner === "shared") return "Shared strings and bytes";
    return this._memory.names[owner] || owner;
  }

  _renderOverview() {
    if (!this._findings) {
      return this._error
        ? `<div class="message error">${escapeHtml(this._error)}</div>`
        : `<div class="message">Loading…</div>`;
    }
    const { verdict, findings } = this._findings;
    const worthALook = findings.filter((f) => f.severity !== "info").length;
    const head =
      verdict === "ok"
        ? `Nothing unusual in the last ${FINDINGS_MINUTES} minutes`
        : verdict === "problem"
        ? `${worthALook} ${worthALook === 1 ? "problem" : "problems"} found`
        : `${worthALook} ${worthALook === 1 ? "thing" : "things"} worth a look`;
    const mark = verdict === "ok" ? "✓" : verdict === "problem" ? "✕" : "!";
    const banner = `<div class="verdict ${verdict}" role="status">
        <div class="mark" aria-hidden="true">${mark}</div>
        <div><div class="head">${escapeHtml(head)}</div>
        <div class="sub">Checked: time on the event loop, background threads and jobs${this._memory?.memory?.latest ? ", memory snapshots" : ""}.</div></div>
      </div>`;
    const cards = findings
      .map((finding, index) => {
        const evidence = finding.evidence.length
          ? `<details><summary>Details for developers</summary><ul>${finding.evidence
              .map((line) => `<li>${escapeHtml(line)}</li>`)
              .join("")}</ul></details>`
          : "";
        const actions = finding.domain
          ? `<div class="actions">
              <button data-action="copyfinding" data-value="${index}">Copy report for the developer</button>
              <button data-action="show" data-value="${escapeHtml(finding.domain)}">Show details</button>
            </div>`
          : "";
        return `<section class="finding ${escapeHtml(finding.severity)}">
            <h2>${escapeHtml(finding.title)}</h2>
            <div class="text">${renderText(finding.description)}</div>
            ${actions}${evidence}
          </section>`;
      })
      .join("");
    const error = this._error ? `<div class="message error">${escapeHtml(this._error)}</div>` : "";
    return `${error}${banner}${cards}
      <div class="footer">Findings use the same numbers as the CPU and Memory views. A snapshot under Memory adds checks for leaks.</div>`;
  }

  _renderMemory() {
    const error = this._error ? `<div class="message error">${escapeHtml(this._error)}</div>` : "";
    if (!this._memory) return error || `<div class="message">Loading…</div>`;
    const latest = this._memory.memory.latest;
    if (!latest) {
      return `${error}<div class="card"><div class="message">No snapshot yet. A snapshot walks the Python heap once and splits it into what each integration holds, its code and the parts of Home Assistant core. Take two some time apart to see what grows.</div>
        <div class="actions"><button class="action" data-action="snapshot" ${this._snapshotting ? "disabled" : ""}>${this._snapshotting ? "Taking snapshot…" : "Take snapshot"}</button></div></div>`;
    }
    const delta = (value, unit = "") => {
      if (value === undefined || value === null || value === 0) return "";
      const cls = value > 0 ? "up" : "down";
      return `<span class="delta ${cls}">${value > 0 ? "+" : ""}${value.toLocaleString()}${unit}</span>`;
    };
    const balance = latest.balance;
    const mibTile = (label, value, sub) =>
      value === undefined
        ? ""
        : `<div class="tile"><div class="label">${label}</div>
        <div class="value">${value.toLocaleString()} MiB</div><div class="sub">${sub}</div></div>`;
    const share = (value) =>
      balance.rss_mib ? `${Math.round((value / balance.rss_mib) * 100)} % of resident` : "&nbsp;";
    const taken = new Date(latest.taken).toLocaleString();
    const since = latest.previous_taken
      ? `compared with ${new Date(latest.previous_taken).toLocaleString()}`
      : "first snapshot, take another one to compare";
    const tiles = `<div class="summary">
      ${mibTile("Resident memory", balance.rss_mib, delta((latest.process_change || {}).rss_mib, " MiB") || "&nbsp;")}
      ${mibTile("Integrations", balance.integration_mib, share(balance.integration_mib))}
      ${mibTile("Libraries", balance.library_mib, share(balance.library_mib))}
      ${mibTile("Home Assistant core", balance.core_mib, share(balance.core_mib))}
      ${mibTile("Outside Python objects", balance.not_in_python_mib, "interpreter, native code, allocator")}
      <div class="tile"><div class="label">Snapshot</div>
        <div class="value">${escapeHtml(taken)}</div>
        <div class="sub">${escapeHtml(since)} · walk ${latest.walk_ms.toLocaleString()} ms</div></div>
    </div>`;
    const rows = latest.owners
      .map((row) => {
        const expanded = this._memExpanded.has(row.owner);
        const c = row.change || {};
        const detail = expanded
          ? `<div class="detail"><p class="note">Data ${row.data_kib.toLocaleString()} KiB · code ${row.code_kib.toLocaleString()} KiB · not reached from a root ${row.unreached_kib.toLocaleString()} KiB · dispatcher connections ${row.dispatcher}${delta(c.dispatcher)}</p></div>`
          : "";
        return `<div class="row mem" data-action="memtoggle" data-value="${escapeHtml(row.owner)}" role="button" tabindex="0" aria-expanded="${expanded}">
            <div class="name"><div class="title"><svg class="chev" viewBox="0 0 24 24"><path fill="currentColor" d="M8.59,16.58L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.58Z"></path></svg>
              <span>${escapeHtml(this._ownerName(row.owner))}</span></div><div class="domain">${escapeHtml(row.owner)}</div></div>
            <div class="num">${row.kib.toLocaleString()}${delta(c.kib)}</div>
            <div class="num">${row.data_kib.toLocaleString()}</div>
            <div class="num">${row.listeners}${delta(c.listeners)}</div>
            <div class="num col-wide">${row.timers}${delta(c.timers)}</div>
            <div class="num col-wide">${row.tasks}${delta(c.tasks)}</div>
          </div>${detail}`;
      })
      .join("");
    return `${error}${tiles}<div class="card">
        <div class="card-head mem"><div>Owner</div><div class="num">KiB</div><div class="num">Data KiB</div><div class="num">Listeners</div><div class="num col-wide">Timers</div><div class="num col-wide">Tasks</div></div>
        ${rows}
      </div>
      <div class="footer">Every Python object is counted once, for the first owner that holds it. Integrations come first, through their config entries, hass.data and entities. Then code per module, then Home Assistant core. Sizes are shallow, so memory held by native code shows only in "Outside Python objects".</div>`;
  }

  _renderDetail(brief) {
    const detail = this._details.get(brief.domain);
    if (!detail) return `<div class="detail"><p class="note">Loading…</p></div>`;
    const item = detail.item;
    if (!item) return `<div class="detail"><p class="note">No data in this window.</p></div>`;
    const parts = [];
    if (item.domain === "core") parts.push(`<p class="note">${CORE_NOTE}</p>`);
    if (item.via_library_ms_per_min) {
      parts.push(
        `<p class="note">${ms(item.via_library_ms_per_min)} ms/min of this ran in libraries that only this integration uses.</p>`
      );
    }
    if (item.via_context_ms_per_min) {
      parts.push(
        `<p class="note">${ms(item.via_context_ms_per_min)} ms/min of this is core and shared library code that ran for one of this integration's config entries, such as parsing its HTTP responses.</p>`
      );
    }
    if (item.state_writes) {
      const sw = item.state_writes;
      const chips = Object.entries(sw.entities)
        .map(([entityId, value]) => `<span class="chip">${escapeHtml(entityId)} · ${value}/min</span>`)
        .join("");
      const size = sw.attribute_kib_per_min ? ` Changed attributes: ${sw.attribute_kib_per_min} KiB per minute.` : "";
      parts.push(
        `<h3>State writes</h3><p class="note">${sw.per_min} writes per minute: ${sw.changes_per_min} changes, ${sw.attributes_only_per_min} changed only attributes, ${sw.repeated_per_min} repeated.${size} Most changes:</p><div class="pairs">${chips}</div>`
      );
    }
    if (item.logs) {
      const lg = item.logs;
      const chips = Object.entries(lg.loggers)
        .map(([name, value]) => `<span class="chip">${escapeHtml(name)} · ${value}/min</span>`)
        .join("");
      parts.push(
        `<h3>Log</h3><p class="note">${lg.warnings_per_min} warnings and ${lg.errors_per_min} errors per minute, ${lg.debug_per_min} debug and info lines.</p><div class="pairs">${chips}</div>`
      );
    }
    if (item.storage) {
      const st = item.storage;
      const chips = Object.entries(st.files)
        .map(([key, value]) => `<span class="chip">${escapeHtml(key)} · ${value}/min</span>`)
        .join("");
      parts.push(
        `<h3>Saved files</h3><p class="note">${st.writes_per_min} saves per minute, ${st.kib_per_min} KiB per minute.</p><div class="pairs">${chips}</div>`
      );
    }
    if (item.stall) {
      parts.push(
        `<h3>Event loop stood still</h3><p class="note">${ms(item.stall.ms)} ms at a stretch, ended ${escapeHtml(localTime(item.stall.at))}. Running at that moment: <span class="code">${escapeHtml(item.stall.function)}</span>.</p>`
      );
    }
    if (item.callbacks) {
      const cb = item.callbacks;
      const rows = cb.by_function
        .map(
          (row) => `<div class="spot"><div class="code">${escapeHtml(row.function)}</div>
            <div class="num">${ms(row.wall_ms_per_min)} ms/min</div>
            <div class="lines"><span class="chip">${row.per_min} per min</span></div></div>`
        )
        .join("");
      parts.push(
        `<h3>Event loop callbacks</h3><p class="note">${ms(cb.wall_ms_per_min)} ms/min of wall time on the event loop, measured exactly, ${cb.per_min} callbacks per minute. Longest: ${ms(cb.longest_ms)} ms at ${escapeHtml(localTime(cb.longest_at))} in <span class="code">${escapeHtml(cb.longest_function)}</span>.</p>${rows}`
      );
    }
    if (item.executor) {
      const ex = item.executor;
      const jobs = ex.jobs
        .map(
          (job) => `<div class="spot"><div class="code">${escapeHtml(job.function)}</div>
            <div class="num">${ms(job.cpu_ms_per_min)} ms/min</div>
            <div class="lines"><span class="chip">${job.per_min} per min</span></div></div>`
        )
        .join("");
      parts.push(
        `<h3>Executor jobs</h3><p class="note">${ms(ex.cpu_ms_per_min)} ms/min CPU, ${ms(ex.wall_ms_per_min)} ms/min of worker time, ${ex.jobs_per_min} jobs per minute.</p>${jobs}`
      );
    }
    if (item.threads) {
      const chips = Object.entries(item.threads.by_name)
        .map(([name, value]) => `<span class="chip">${escapeHtml(name)} · ${ms(value)}</span>`)
        .join("");
      parts.push(`<h3>Threads</h3><div class="pairs">${chips}</div>`);
    }
    if (item.hot_spots.length) {
      const spots = item.hot_spots
        .map((spot) => {
          const chips = Object.entries(spot.lines)
            .map(([line, value]) => `<span class="chip">line ${escapeHtml(line)} · ${ms(value)}</span>`)
            .join("");
          return `<div class="spot"><div class="code">${escapeHtml(spot.function)}</div>
            <div class="num">${ms(spot.loop_cpu_ms_per_min)} ms/min</div>
            <div class="lines">${chips}</div></div>`;
        })
        .join("");
      parts.push(`<h3>Hot spots</h3>${spots}`);
    }
    const pairs = (title, values, label = (key) => key, mono = false) => {
      const entries = Object.entries(values);
      if (!entries.length) return;
      const chips = entries
        .map(
          ([key, value]) =>
            `<span class="chip"${mono ? "" : ' style="font-family:inherit"'}>${escapeHtml(label(key))} · ${ms(value)}</span>`
        )
        .join("");
      parts.push(`<h3>${title}</h3><div class="pairs">${chips}</div>`);
    };
    pairs("Devices", item.devices, (key) => detail.device_names[key] || key);
    pairs(
      "Entities",
      item.entities,
      (key) => {
        const name = detail.entity_names[key];
        return name ? `${name} (${key})` : key;
      }
    );
    pairs("Libraries", item.libraries, undefined, true);
    pairs("Triggered by", item.triggered_by, (key) => this._data.names[key] || key);
    pairs("Config entries", item.config_entries, (key) => detail.entry_titles[key] || key);
    if (item.stacks.length) {
      const stacks = item.stacks
        .map((stack, index) => {
          const key = `${item.domain}:${index}`;
          const frames = stack.frames.map((frame) => `<li>${escapeHtml(frame)}</li>`).join("");
          return `<details data-stack="${escapeHtml(key)}" ${this._openStacks.has(key) ? "open" : ""}>
            <summary>Stack ${index + 1} · ${ms(stack.loop_cpu_ms_per_min)} ms/min</summary>
            <ol class="frames">${frames}</ol></details>`;
        })
        .join("");
      parts.push(`<h3>Stacks, innermost first</h3>${stacks}`);
    }
    parts.push(
      `<div class="actions"><button class="action" data-action="copy" data-value="${escapeHtml(item.domain)}">Copy as text</button></div>`
    );
    return `<div class="detail">${parts.join("")}</div>`;
  }
}

// An open tab keeps the old module after an update until it reloads.
if (!customElements.get("integration-insights-panel")) {
  customElements.define("integration-insights-panel", IntegrationInsightsPanel);
}
