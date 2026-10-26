// Sidebar panel: which integration keeps the event loop busy, and where.
// Plain custom element, no build step. Home Assistant sets `hass`, `narrow`
// and `panel` on it.

const WINDOWS = [5, 15, 60];
const REFRESH_MS = 10000;
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

const ms = (value) =>
  value >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1);

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
    grid-template-columns: 1fr 120px 90px 70px;
    gap: 12px;
    padding: 10px 16px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--secondary-text-color);
    border-bottom: 1px solid var(--divider-color);
  }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .row {
    display: grid;
    grid-template-columns: 1fr 120px 90px 70px;
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
  .footer { margin-top: 12px; font-size: 13px; color: var(--secondary-text-color); }
  @media (max-width: 600px) {
    main { padding: 12px; }
    .card-head, .row { grid-template-columns: 1fr 76px 56px; gap: 8px; }
    .col-samples { display: none; }
    .detail { padding-left: 16px; }
    .header-button { display: none; }
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
    this._loading = false;
    this._onClick = this._onClick.bind(this);
    this._onKeyDown = this._onKeyDown.bind(this);
    this._onToggle = this._onToggle.bind(this);
    this._onVisibility = this._onVisibility.bind(this);
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first && this.isConnected) this._load();
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
    this._timer = setInterval(() => {
      if (!document.hidden) this._load();
    }, REFRESH_MS);
    this._render();
    if (this._hass) this._load();
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._onClick);
    this.shadowRoot.removeEventListener("keydown", this._onKeyDown);
    this.shadowRoot.removeEventListener("toggle", this._onToggle, true);
    document.removeEventListener("visibilitychange", this._onVisibility);
    clearInterval(this._timer);
  }

  _onVisibility() {
    if (!document.hidden) this._load();
  }

  async _load() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    try {
      this._data = await this._hass.connection.sendMessagePromise({
        type: "integration_insights/report",
        minutes: this._minutes,
      });
      this._error = undefined;
      this._updated = new Date();
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
    } finally {
      this._loading = false;
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
      this._render();
      this._load();
    } else if (action === "toggle") {
      if (this._expanded.has(value)) this._expanded.delete(value);
      else this._expanded.add(value);
      this._render();
    } else if (action === "download") {
      this._download();
    } else if (action === "copy") {
      this._copy(value, target);
    }
  }

  _onKeyDown(ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    const row = ev.target.closest('.row[data-action="toggle"]');
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

  _download() {
    if (!this._data) return;
    // Same content as the diagnostics download: no entry titles or names.
    const blob = new Blob([JSON.stringify(this._data.report, null, 2)], {
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
    const item = this._item(domain);
    if (!item) return;
    const text = this._describe(item);
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
      button.textContent = "Copy as text";
    }, 1500);
  }

  _item(domain) {
    return this._data?.report.integrations.find((i) => i.domain === domain);
  }

  // A plain text summary for a bug report or an LLM.
  _describe(item) {
    const report = this._data.report;
    const lines = [
      `Integration Insights: ${item.domain}`,
      `Event loop CPU: ${item.loop_cpu_ms_per_min} ms per minute ` +
        `(${corePercent(item.loop_cpu_ms_per_min)} of one core), ` +
        `${item.samples} samples over ${report.window_minutes} min` +
        (item.low_confidence ? " (few samples)" : ""),
    ];
    if (item.via_library_ms_per_min) {
      lines.push(
        `Charged through libraries only this integration uses: ${item.via_library_ms_per_min} ms/min`
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
    return `<header>${menu}<h1>Integration Insights</h1>
      <div class="segmented" role="group" aria-label="Time window">${windows}</div>
      <button class="header-button" data-action="download" ${this._data ? "" : "disabled"}>Download report</button>
    </header>`;
  }

  _renderBody() {
    if (!this._data) {
      return this._error
        ? `<div class="message error">${escapeHtml(this._error)}</div>`
        : `<div class="message">Loading…</div>`;
    }
    const { report } = this._data;
    const sampler = report.sampler;
    const samplesPerMinute = report.samples / Math.max(report.window_minutes, 1 / 60);
    const samplerCost = (sampler.handler_cpu_us_avg * samplesPerMinute) / 1000;
    const problems = sampler.samples_lost + sampler.handler_errors;
    const integrations = report.integrations;
    const top = integrations.length ? integrations[0].loop_cpu_ms_per_min : 0;

    const tiles = `<div class="summary">
      <div class="tile"><div class="label">Event loop busy</div>
        <div class="value">${ms(report.loop_cpu_ms_per_min)} ms/min</div>
        <div class="sub">${corePercent(report.loop_cpu_ms_per_min)} of one core</div></div>
      <div class="tile"><div class="label">Window</div>
        <div class="value">${report.window_minutes} min</div>
        <div class="sub">${report.samples.toLocaleString()} samples</div></div>
      <div class="tile"><div class="label">Measuring costs</div>
        <div class="value">${ms(samplerCost)} ms/min</div>
        <div class="sub">${sampler.handler_cpu_us_avg} µs per sample</div></div>
      <div class="tile${problems ? " warn" : ""}"><div class="label">Sampler</div>
        <div class="value">${problems ? `${problems} problems` : "OK"}</div>
        <div class="sub">lost ${sampler.samples_lost} · errors ${sampler.handler_errors} · slowdowns ${sampler.backoffs}</div></div>
    </div>`;

    if (!integrations.length) {
      return `${tiles}<div class="card"><div class="message">No samples yet. The loop has been idle.</div></div>`;
    }

    const rows = integrations
      .map((item) => {
        const expanded = this._expanded.has(item.domain);
        const name =
          item.domain === "core"
            ? "Home Assistant core and unassigned work"
            : this._data.names[item.domain] || item.domain;
        const width = top ? Math.max((item.loop_cpu_ms_per_min / top) * 100, 1) : 0;
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
            <div class="num">${corePercent(item.loop_cpu_ms_per_min)}</div>
            <div class="num col-samples">${item.samples}</div>
          </div>${expanded ? this._renderDetail(item) : ""}`;
      })
      .join("");

    const updated = this._updated ? this._updated.toLocaleTimeString() : "";
    return `${tiles}<div class="card">
        <div class="card-head"><div>Integration</div><div class="num">ms/min</div><div class="num">of a core</div><div class="num col-samples">Samples</div></div>
        ${rows}
      </div>
      <div class="footer">CPU time on the event loop, sampled every ${sampler.interval_ms} ms of loop CPU. Updated ${updated}${this._error ? ` · <span class="error">${escapeHtml(this._error)}</span>` : ""}</div>`;
  }

  _renderDetail(item) {
    const parts = [];
    if (item.domain === "core") parts.push(`<p class="note">${CORE_NOTE}</p>`);
    if (item.via_library_ms_per_min) {
      parts.push(
        `<p class="note">${ms(item.via_library_ms_per_min)} ms/min of this ran in libraries that only this integration uses.</p>`
      );
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
    pairs("Libraries", item.libraries, undefined, true);
    pairs("Triggered by", item.triggered_by, (key) => this._data.names[key] || key);
    pairs("Config entries", item.config_entries, (key) => this._data.entry_titles[key] || key);
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

customElements.define("integration-insights-panel", IntegrationInsightsPanel);
