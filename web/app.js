/**
 * DEADMAN — War-Room Frontend
 * Beacon / frontend engineer
 *
 * Architecture:
 *  - healthz poll → MODE badge + conn status
 *  - Chaos buttons → POST /api/chaos/{toggle}
 *  - "RUN" button → EventSource /api/demo/stream, fallback POST /api/demo/run
 *  - SSE beats → animate split-screen panels + live scoreboard
 *  - Scoreboard headline: DOUBLE-EXECUTIONS NAIVE:N DEADMAN:M (the visual climax)
 *  - Postmortem fetch → /incident/{id}/postmortem
 *
 * Defensive: all fields optional; backend offline → clear banner, UI still loads.
 */

'use strict';

/* ─────────────────────────────────────────────────────────────────────────────
   Constants
───────────────────────────────────────────────────────────────────────────── */
const API = '';           // same origin
const HEALTHZ_INTERVAL = 8000; // ms
const FAST_PARAM = 'fast=1';

// Chaos toggles that map to POST /api/chaos/{toggle}
const CHAOS_TOGGLES = [
  { id: 'correlated_blackout', label: '☠ Correlated Blackout',  cls: 'btn-chaos', desc: 'us-east-1 EVENT' },
  { id: 'rate_limit_storm',    label: '⚡ 429 Storm',            cls: 'btn-chaos', desc: 'rate limit tier-0' },
  { id: 'kill_bedrock',        label: '☠ All-Bedrock-Down',     cls: 'btn-chaos', desc: 'every live tier' },
  { id: 'corrupt_output',      label: '⚠ Corrupt Output',       cls: 'btn-chaos', desc: 'garbage JSON' },
  { id: 'kill_mid_rollback',   label: '🔴 KILL mid-rollback',   cls: 'btn-kill',  desc: 'SIGKILL between side-effect+commit' },
];

/* ─────────────────────────────────────────────────────────────────────────────
   State
───────────────────────────────────────────────────────────────────────────── */
const state = {
  mode: 'unknown',          // 'mock' | 'real' | 'offline'
  online: false,
  running: false,
  chaosActive: {},          // toggle id -> bool
  eventSource: null,
  currentIncidentId: null,
  scoreboard: {
    naive: null,
    deadman: null,
    headline: { double_executions_naive: 0, double_executions_deadman: 0 },
  },
  naiveTimeline: [],
  deadmanTimeline: [],
  beatCount: 0,
};

/* ─────────────────────────────────────────────────────────────────────────────
   DOM refs (populated in init)
───────────────────────────────────────────────────────────────────────────── */
const el = {};

/* ─────────────────────────────────────────────────────────────────────────────
   Utilities
───────────────────────────────────────────────────────────────────────────── */
function $(id) { return document.getElementById(id); }
function safe(v, fallback = '—') {
  if (v === null || v === undefined) return fallback;
  return String(v);
}
function safeNum(v, fallback = 0) {
  const n = Number(v);
  return isNaN(n) ? fallback : n;
}

function fmtTime() {
  const d = new Date();
  return `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}:${String(d.getSeconds()).padStart(2,'0')}`;
}

function classifyBeat(note, beat) {
  const n = (note || beat || '').toLowerCase();
  if (n.includes('kill') || n.includes('dead') || n.includes('double') || n.includes('stall') || n.includes('lost') || n.includes('outage') || n.includes('down')) return 'beat-danger';
  if (n.includes('warn') || n.includes('429') || n.includes('corrupt') || n.includes('block') || n.includes('throttl') || n.includes('revok') || n.includes('degraded') || n.includes('leash')) return 'beat-warn';
  if (n.includes('surviv') || n.includes('success') || n.includes('resolved') || n.includes('skip') || n.includes('resume') || n.includes('dedupe') || n.includes('audit') || n.includes('postmortem') || n.includes('commit')) return 'beat-success';
  return 'beat-info';
}

function toast(msg, type = 'info', duration = 4000) {
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  el.toastArea.appendChild(t);
  setTimeout(() => t.remove(), duration);
}

/* ─────────────────────────────────────────────────────────────────────────────
   Health poll
───────────────────────────────────────────────────────────────────────────── */
async function pollHealth() {
  try {
    const r = await fetch(`${API}/healthz`, { signal: AbortSignal.timeout(4000) });
    if (!r.ok) throw new Error('not ok');
    const data = await r.json();
    state.online = true;
    state.mode = data.mode || 'mock';
    setOffline(false);
    updateModeBadge();
    updateConnDot('online');
  } catch {
    state.online = false;
    state.mode = 'offline';
    setOffline(true);
    updateModeBadge();
    updateConnDot('offline');
  }
}

function setOffline(isOffline) {
  if (isOffline) {
    el.offlineBanner.classList.add('visible');
    el.runBtn.disabled = true;
    el.runBtn.textContent = 'BACKEND OFFLINE';
  } else {
    el.offlineBanner.classList.remove('visible');
    if (!state.running) {
      el.runBtn.disabled = false;
      el.runBtn.textContent = '▶  RUN CORRELATED BLACKOUT';
      el.runBtn.className = 'btn btn-primary';
    }
  }
}

function updateModeBadge() {
  const badge = el.modeBadge;
  badge.className = `mode-badge ${state.mode}`;
  badge.textContent = state.mode.toUpperCase();
}

function updateConnDot(status) {
  el.connDot.className = `conn-dot ${status}`;
  el.connText.textContent = status === 'online' ? `BACKEND ${state.mode.toUpperCase()}` :
                            status === 'streaming' ? 'STREAMING' : 'OFFLINE';
}

/* ─────────────────────────────────────────────────────────────────────────────
   Chaos controls
───────────────────────────────────────────────────────────────────────────── */
async function toggleChaos(id) {
  const btn = el.chaosBtns[id];
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`${API}/api/chaos/${id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(5000),
    });
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      // Update all active states from response if available
      updateChaosState(id, data);
    } else {
      // Toggle local visual even if backend isn't wired (mock mode)
      state.chaosActive[id] = !state.chaosActive[id];
      renderChaosButtons();
    }
  } catch {
    // Offline: toggle locally so the UI is still interactive for demo purposes
    state.chaosActive[id] = !state.chaosActive[id];
    renderChaosButtons();
    if (!state.online) toast('Backend offline — chaos toggle shown locally only', 'warn');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function updateChaosState(toggled, data) {
  // data may contain chaos flags: correlated_blackout, rate_limit_storm, etc.
  if (data && typeof data === 'object') {
    for (const k of Object.keys(state.chaosActive)) {
      if (k in data) state.chaosActive[k] = Boolean(data[k]);
    }
    // If none of the keys were in data, just flip the toggled one
    if (!Object.keys(data).some(k => k in state.chaosActive)) {
      state.chaosActive[toggled] = !state.chaosActive[toggled];
    }
  }
  renderChaosButtons();
  renderChaosStateDisplay();
}

function renderChaosButtons() {
  for (const cfg of CHAOS_TOGGLES) {
    const btn = el.chaosBtns[cfg.id];
    if (!btn) continue;
    const on = state.chaosActive[cfg.id];
    if (on) btn.classList.add('active');
    else btn.classList.remove('active');
  }
}

function renderChaosStateDisplay() {
  const parts = CHAOS_TOGGLES.map(c => {
    const on = state.chaosActive[c.id];
    return `<span class="${on ? 'on' : 'off'}">${c.id.replace(/_/g,' ').toUpperCase()}:${on ? 'ON' : 'off'}</span>`;
  });
  el.chaosState.innerHTML = parts.join(' &nbsp;|&nbsp; ');
}

async function resetChaos() {
  for (const k of Object.keys(state.chaosActive)) state.chaosActive[k] = false;
  renderChaosButtons();
  renderChaosStateDisplay();
  try {
    await fetch(`${API}/api/chaos/reset`, { method: 'POST', signal: AbortSignal.timeout(4000) });
  } catch {
    // ignore if offline
  }
  toast('Chaos reset', 'info', 2000);
}

/* ─────────────────────────────────────────────────────────────────────────────
   Demo run — SSE stream + POST fallback
───────────────────────────────────────────────────────────────────────────── */
function startDemo() {
  if (state.running) return;
  state.running = true;
  state.beatCount = 0;
  state.naiveTimeline = [];
  state.deadmanTimeline = [];
  state.currentIncidentId = `inc-${Date.now()}`;

  el.runBtn.textContent = '⏳ RUNNING…';
  el.runBtn.className = 'btn btn-primary running';
  el.runBtn.disabled = true;

  clearPanels();
  resetScoreboard();
  setPanelStatus('naive', 'running', 'RUNNING');
  setPanelStatus('deadman', 'running', 'RUNNING');
  setMeta('naive', { backend: 'claude@us-east-1', fallback_depth: 0 });
  setMeta('deadman', { backend: 'claude@us-east-1', fallback_depth: 0 });
  updateConnDot('streaming');

  const fast = el.fastCheck && el.fastCheck.checked;
  const url = `${API}/api/demo/stream${fast ? `?${FAST_PARAM}` : ''}`;

  // Try SSE first
  try {
    const es = new EventSource(url);
    state.eventSource = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        handleBeat(data);
      } catch (err) {
        // ignore parse errors on individual frames
      }
    };

    es.onerror = () => {
      es.close();
      state.eventSource = null;
      if (state.running) {
        // SSE failed — fall back to POST
        runViaPost();
      }
    };

  } catch (err) {
    runViaPost();
  }
}

async function runViaPost() {
  try {
    const r = await fetch(`${API}/api/demo/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: AbortSignal.timeout(30000),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    // Synthesize a done-beat from the POST response
    handleBeat({
      beat: 'done',
      side: 'both',
      scoreboard: data,
      note: 'Demo complete (batch response)',
    });
  } catch (err) {
    finishDemo(false);
    toast(`Demo failed: ${err.message}`, 'error');
  }
}

function handleBeat(data) {
  if (!data || typeof data !== 'object') return;
  const beat = data.beat || '';
  const side = data.side || 'both';
  const note = safe(data.note, '');
  const sb = data.scoreboard || data;  // some events nest, others don't

  state.beatCount++;

  // Route timeline entries
  if (note) {
    if (side === 'naive' || side === 'both') {
      addTimelineEntry('naive', beat, note);
    }
    if (side === 'deadman' || side === 'both') {
      addTimelineEntry('deadman', beat, note);
    }
  }

  // Extract per-side scoreboard data — supports both {naive:{...}, deadman:{...}} and flat
  const naiveSb = sb.naive || (side === 'naive' ? sb : null);
  const deadmanSb = sb.deadman || (side === 'deadman' ? sb : null);
  const headline = sb.headline || data.headline || null;

  if (naiveSb) {
    updatePanelFromSb('naive', naiveSb);
  }
  if (deadmanSb) {
    updatePanelFromSb('deadman', deadmanSb);
  }

  // Kill flash
  if (beat === 'kill' || (note && note.toLowerCase().includes('kill'))) {
    triggerKillFlash();
  }

  // Update scoreboard
  if (headline) {
    state.scoreboard.headline = headline;
  }
  if (sb.naive) state.scoreboard.naive = sb.naive;
  if (sb.deadman) state.scoreboard.deadman = sb.deadman;
  renderScoreboard();

  if (beat === 'done') {
    // Final scoreboard from done event
    if (sb.naive) state.scoreboard.naive = sb.naive;
    if (sb.deadman) state.scoreboard.deadman = sb.deadman;
    if (headline) state.scoreboard.headline = headline;
    renderScoreboard();
    finishDemo(true);
    if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  }
}

function updatePanelFromSb(side, sb) {
  if (!sb || typeof sb !== 'object') return;
  const statusMap = {
    survived: 'survived',
    dead: 'dead',
    stalled: 'stalled',
  };
  let chipCls = 'chip-running';
  let chipLabel = 'RUNNING';
  if (sb.survived === true) { chipCls = 'chip-survived'; chipLabel = 'SURVIVED'; }
  else if (sb.survived === false && sb.state_losses > 0) { chipCls = 'chip-dead'; chipLabel = 'DEAD'; }

  setPanelStatus(side, chipCls.replace('chip-',''), chipLabel);
  setMeta(side, {
    backend: sb.backend,
    fallback_depth: sb.fallback_depth,
    drain_authority: sb.drain_authority,
    guardrail_blocks: sb.guardrail_blocks,
    state_losses: sb.state_losses,
  });
}

function finishDemo(success) {
  state.running = false;
  updateConnDot(state.online ? 'online' : 'offline');
  el.runBtn.disabled = !state.online;
  el.runBtn.className = `btn btn-primary ${success ? 'done' : ''}`;
  el.runBtn.textContent = success ? '✓ DEMO COMPLETE — Run again?' : '▶  RUN CORRELATED BLACKOUT';
  setTimeout(() => {
    if (!state.running) {
      el.runBtn.className = 'btn btn-primary';
      el.runBtn.textContent = '▶  RUN CORRELATED BLACKOUT';
    }
  }, 6000);

  if (success) {
    setPanelStatus('naive', 'dead', 'DOUBLE-EXECUTED');
    setPanelStatus('deadman', 'survived', 'SURVIVED');
    renderScoreboard();
    toast('Demo complete', 'success');
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Panel helpers
───────────────────────────────────────────────────────────────────────────── */
function clearPanels() {
  el.naiveTimeline.innerHTML = `<div class="timeline-empty">Waiting for events…</div>`;
  el.deadmanTimeline.innerHTML = `<div class="timeline-empty">Waiting for events…</div>`;
  el.naivePanel.className = 'agent-panel naive';
  el.deadmanPanel.className = 'agent-panel deadman';
}

function setPanelStatus(side, status, label) {
  const chip = el[`${side}Chip`];
  const panel = el[`${side}Panel`];
  if (!chip || !panel) return;

  chip.className = `status-chip chip-${status}`;
  chip.textContent = label;

  // Update panel border class
  panel.className = `agent-panel ${side}`;
  if (status === 'running') panel.classList.add('active');
  if (status === 'dead') panel.classList.add('dead');
  if (status === 'survived') panel.classList.add('survived');
  if (status === 'active') panel.classList.add('active');
}

function setMeta(side, data) {
  if (!data) return;
  const metaEl = el[`${side}Meta`];
  if (!metaEl) return;

  // BRAIN = backend + tier (fallback depth)
  let brainVal = '—';
  let brainCls = '';
  if (data.backend !== undefined && data.backend !== null) {
    brainVal = safe(data.backend);
  }
  if (data.fallback_depth !== undefined && data.fallback_depth !== null) {
    const depth = safeNum(data.fallback_depth);
    brainCls = depth >= 3 ? 'danger' : depth >= 1 ? 'warn' : '';
    brainVal = `${brainVal === '—' ? '—' : brainVal} · T${depth}`;
  }

  // MEMORY = state-loss (0 lost green / 1+ lost red)
  let memVal = '—';
  let memCls = '';
  if (data.state_losses !== undefined && data.state_losses !== null) {
    const lost = safeNum(data.state_losses);
    memVal = `${lost} LOST`;
    memCls = lost > 0 ? 'danger' : 'good';
  }

  // LEASH = drain ON/OFF
  let leashVal = '—';
  let leashCls = '';
  if (data.drain_authority !== undefined && data.drain_authority !== null) {
    const on = String(data.drain_authority).toUpperCase() === 'ON';
    leashVal = on ? 'ON' : 'OFF';
    leashCls = on ? 'good' : 'danger';
  }

  metaEl.innerHTML =
    `<span class="meta-item"><span class="meta-label">BRAIN</span><span class="meta-val ${brainCls}">${brainVal}</span></span>` +
    `<span class="meta-item"><span class="meta-label">MEMORY</span><span class="meta-val ${memCls}">${memVal}</span></span>` +
    `<span class="meta-item"><span class="meta-label">LEASH</span><span class="meta-val ${leashCls}">${leashVal}</span></span>`;
}

function addTimelineEntry(side, beat, text) {
  const container = el[`${side}Timeline`];
  if (!container) return;

  // Remove empty placeholder
  const empty = container.querySelector('.timeline-empty');
  if (empty) empty.remove();

  const entry = document.createElement('div');
  const cls = classifyBeat(text, beat);
  entry.className = `timeline-entry ${cls}`;
  entry.innerHTML = `<span class="t-time">${fmtTime()}</span><span class="t-text">${escapeHtml(text)}</span>`;
  container.appendChild(entry);
  container.scrollTop = container.scrollHeight;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function triggerKillFlash() {
  const flashes = document.querySelectorAll('.kill-flash');
  flashes.forEach(f => {
    f.classList.add('active');
    setTimeout(() => f.classList.remove('active'), 300);
  });
  // Extra: flash naive panel red
  if (el.naivePanel) {
    el.naivePanel.classList.add('flash-red');
    setTimeout(() => el.naivePanel.classList.remove('flash-red'), 400);
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Scoreboard
───────────────────────────────────────────────────────────────────────────── */
function resetScoreboard() {
  state.scoreboard = {
    naive: null,
    deadman: null,
    headline: { double_executions_naive: 0, double_executions_deadman: 0 },
  };
  renderScoreboard();
}

function renderScoreboard() {
  const { naive, deadman, headline } = state.scoreboard;

  // ── Headline: DOUBLE-EXECUTIONS NAIVE:N DEADMAN:M ──
  const naiveDE = safeNum(headline?.double_executions_naive ?? naive?.double_executions, 0);
  const deadmanDE = safeNum(headline?.double_executions_deadman ?? deadman?.double_executions, 0);

  if (el.naiveDE) {
    el.naiveDE.textContent = naiveDE;
    el.naiveDE.className = `headline-num naive${naiveDE > 0 ? ' nonzero' : ''}`;
  }
  if (el.deadmanDE) {
    el.deadmanDE.textContent = deadmanDE;
    el.deadmanDE.className = `headline-num deadman${deadmanDE === 0 ? ' zero' : ''}`;
  }

  // ── Metric grid ──
  renderMetric('backend', naive?.backend, deadman?.backend, false);
  renderMetric('fallback', naive?.fallback_depth, deadman?.fallback_depth, false);
  renderMetric('state-losses', naive?.state_losses, deadman?.state_losses, false);
  // Hide the State Losses tile entirely when neither side reports the metric
  const slTile = $('sb-metric-state-losses');
  if (slTile) {
    const hasSL = (naive?.state_losses !== undefined && naive?.state_losses !== null) ||
                  (deadman?.state_losses !== undefined && deadman?.state_losses !== null);
    slTile.hidden = !hasSL;
  }
  renderMetric('guardrail-blocks', naive?.guardrail_blocks, deadman?.guardrail_blocks, false);

  renderDrainAuthority(naive?.drain_authority, deadman?.drain_authority);
  renderSurvived(naive?.survived, deadman?.survived);
}

function renderMetric(id, naiveVal, deadmanVal, highlight) {
  const el_n = $(`sb-naive-${id}`);
  const el_d = $(`sb-deadman-${id}`);
  if (el_n) el_n.textContent = naiveVal !== undefined && naiveVal !== null ? naiveVal : '—';
  if (el_d) el_d.textContent = deadmanVal !== undefined && deadmanVal !== null ? deadmanVal : '—';
}

function renderDrainAuthority(naiveDA, deadmanDA) {
  const nb = $('drain-naive-badge');
  const db = $('drain-deadman-badge');
  if (nb) {
    const on = String(naiveDA || 'ON').toUpperCase() === 'ON';
    nb.textContent = on ? 'ON' : 'OFF';
    nb.className = `drain-badge ${on ? 'on' : 'off'}`;
  }
  if (db) {
    const on = String(deadmanDA || 'ON').toUpperCase() === 'ON';
    db.textContent = on ? 'ON' : 'OFF';
    db.className = `drain-badge ${on ? 'on' : 'off'}`;
  }
}

function renderSurvived(naiveSurv, deadmanSurv) {
  const ne = $('survived-naive');
  const de = $('survived-deadman');
  if (ne) {
    if (naiveSurv === true)  { ne.textContent = 'YES'; ne.className = 'survived-pill yes'; }
    else if (naiveSurv === false) { ne.textContent = 'NO'; ne.className = 'survived-pill no'; }
    else { ne.textContent = '—'; ne.className = 'survived-pill na'; }
  }
  if (de) {
    if (deadmanSurv === true)  { de.textContent = 'YES'; de.className = 'survived-pill yes'; }
    else if (deadmanSurv === false) { de.textContent = 'NO'; de.className = 'survived-pill no'; }
    else { de.textContent = '—'; de.className = 'survived-pill na'; }
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Postmortem
───────────────────────────────────────────────────────────────────────────── */
async function fetchPostmortem() {
  const id = el.pmInput.value.trim() || state.currentIncidentId || 'incident-42';
  if (!id) { toast('Enter an incident ID', 'warn'); return; }
  el.pmContent.textContent = 'Fetching…';
  try {
    const r = await fetch(`${API}/incident/${encodeURIComponent(id)}/postmortem`, {
      signal: AbortSignal.timeout(8000),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    const trail = data.audit_trail || data.timeline || data.notes || [];
    if (Array.isArray(trail) && trail.length > 0) {
      el.pmContent.innerHTML = trail.map((t, i) =>
        `<div class="pm-entry"><span class="pm-idx">${String(i+1).padStart(2,'0')}.</span><span class="pm-text">${escapeHtml(String(t))}</span></div>`
      ).join('');
    } else {
      el.pmContent.innerHTML = `<div class="pm-entry"><span class="pm-text">${escapeHtml(JSON.stringify(data, null, 2))}</span></div>`;
    }
  } catch (err) {
    el.pmContent.textContent = `Error: ${err.message}`;
    toast(`Postmortem fetch failed: ${err.message}`, 'error');
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Build DOM
───────────────────────────────────────────────────────────────────────────── */
function buildUI() {
  const app = document.getElementById('app');
  if (!app) return;

  app.innerHTML = `
    <!-- HEADER -->
    <header id="header">
      <div id="header-left">
        <div id="header-title">
          <span class="skull">💀</span>
          <span class="brand">DEADMAN</span>
        </div>
        <div id="header-tagline">the agent that survives its own outage</div>
      </div>
      <div id="header-right">
        <div class="conn-status">
          <div class="conn-dot" id="conn-dot"></div>
          <span id="conn-text">CONNECTING…</span>
        </div>
        <div class="mode-badge offline" id="mode-badge">OFFLINE</div>
      </div>
    </header>

    <!-- OFFLINE BANNER -->
    <div id="offline-banner">
      <span class="offline-icon">⚠</span>
      Backend not reachable — start with: <strong style="margin-left:6px">uvicorn deadman.webhook:app --reload --port 8080</strong>
      &nbsp;&nbsp;(UI still works for visual preview)
    </div>

    <!-- CONTROLS -->
    <section id="controls">
      <div class="controls-bar">
        <div class="chaos-buttons" id="chaos-buttons">
          ${CHAOS_TOGGLES.map(c => `
            <button class="btn ${c.cls}" id="chaos-btn-${c.id}" title="${c.desc}">${c.label}</button>
          `).join('')}
          <button class="btn btn-reset" id="btn-reset">↺ Reset</button>
        </div>
        <div class="demo-controls">
          <button class="btn btn-primary" id="run-btn">▶  RUN CORRELATED BLACKOUT</button>
          <label class="fast-toggle">
            <input type="checkbox" id="fast-check"> Fast replay
          </label>
        </div>
      </div>
      <div id="chaos-state">
        ${CHAOS_TOGGLES.map(c => `<span class="off">${c.id.replace(/_/g,' ').toUpperCase()}:off</span>`).join(' &nbsp;|&nbsp; ')}
      </div>
    </section>

    <!-- HERO STRIP -->
    <div id="hero">
      <!-- LEFT: Double-Executions duel -->
      <div id="headline-block">
        <span class="sb-incident" id="sb-incident">—</span>
        <div class="headline-label">Double-Executions</div>
        <div class="headline-duel">
          <div class="headline-pair">
            <span class="headline-who naive">NAIVE</span>
            <span class="headline-num naive" id="naive-de">0</span>
          </div>
          <div class="headline-divider"></div>
          <div class="headline-pair">
            <span class="headline-who deadman">DEADMAN</span>
            <span class="headline-num deadman zero" id="deadman-de">0</span>
          </div>
        </div>
      </div>

      <!-- RIGHT: Leash tile -->
      <div id="leash-tile">
        <div class="leash-title">LEASH — Drain Authority</div>
        <div class="leash-row">
          <span class="leash-who">NAIVE</span>
          <span class="drain-badge on" id="drain-naive-badge">ON</span>
        </div>
        <div class="leash-row">
          <span class="leash-who">DEADMAN</span>
          <span class="drain-badge on" id="drain-deadman-badge">ON</span>
        </div>
      </div>
    </div>

    <!-- SPLIT SCREEN -->
    <div id="arena-caption">BRAIN · MEMORY · LEASH</div>
    <div id="arena">
      <!-- NAIVE Panel -->
      <div class="agent-panel naive" id="naive-panel">
        <div class="kill-flash"></div>
        <div class="panel-header">
          <div>
            <div class="panel-title">NAIVE AGENT</div>
            <div class="panel-subtitle">Raw Bedrock, us-east-1 only · in-process state · no idempotency</div>
          </div>
          <div class="status-chip chip-idle" id="naive-chip">IDLE</div>
        </div>
        <div class="panel-meta" id="naive-meta"></div>
        <div class="panel-timeline" id="naive-timeline">
          <div class="timeline-empty">Start the demo to see the naive agent's fate…</div>
        </div>
      </div>

      <!-- DEADMAN Panel -->
      <div class="agent-panel deadman" id="deadman-panel">
        <div class="kill-flash"></div>
        <div class="panel-header">
          <div>
            <div class="panel-title">DEADMAN</div>
            <div class="panel-subtitle">TFY AI+MCP+Agent Gateways · append-only state · exactly-once</div>
          </div>
          <div class="status-chip chip-idle" id="deadman-chip">IDLE</div>
        </div>
        <div class="panel-meta" id="deadman-meta"></div>
        <div class="panel-timeline" id="deadman-timeline">
          <div class="timeline-empty">DEADMAN is ready. Start the demo to watch it survive.</div>
        </div>
      </div>
    </div>

    <!-- FOOTER: compact metric tiles + postmortem -->
    <footer id="footer">
      <div class="sb-grid">
        <div class="sb-metric">
          <div class="sb-metric-label">Backend in Use</div>
          <div class="sb-metric-row">
            <span class="sb-val naive-val" id="sb-naive-backend">—</span>
            <span class="sb-val deadman-val" id="sb-deadman-backend">—</span>
          </div>
        </div>
        <div class="sb-metric">
          <div class="sb-metric-label">Fallback Depth</div>
          <div class="sb-metric-row">
            <span class="sb-val naive-val" id="sb-naive-fallback">—</span>
            <span class="sb-val deadman-val" id="sb-deadman-fallback">—</span>
          </div>
        </div>
        <div class="sb-metric">
          <div class="sb-metric-label">Guardrail Blocks</div>
          <div class="sb-metric-row">
            <span class="sb-val naive-val" id="sb-naive-guardrail-blocks">—</span>
            <span class="sb-val deadman-val" id="sb-deadman-guardrail-blocks">—</span>
          </div>
        </div>
        <div class="sb-metric" id="sb-metric-state-losses" hidden>
          <div class="sb-metric-label">State Losses</div>
          <div class="sb-metric-row">
            <span class="sb-val naive-val" id="sb-naive-state-losses">—</span>
            <span class="sb-val deadman-val" id="sb-deadman-state-losses">—</span>
          </div>
        </div>
        <div class="sb-metric">
          <div class="sb-metric-label">Survived</div>
          <div class="sb-metric-row survived-row">
            <span class="survived-pill na" id="survived-naive">—</span>
            <span class="survived-pill na" id="survived-deadman">—</span>
          </div>
        </div>
        <div class="sb-metric pm-metric">
          <div class="sb-metric-label">Incident Postmortem</div>
          <div class="pm-controls">
            <input class="pm-input" id="pm-input" type="text" placeholder="incident-42" />
            <button class="btn btn-pm" id="pm-fetch-btn">Fetch</button>
          </div>
        </div>
      </div>
      <div id="pm-content"></div>
    </footer>

    <!-- TOAST AREA -->
    <div id="toast-area"></div>
  `;
}

/* ─────────────────────────────────────────────────────────────────────────────
   Wire DOM refs after build
───────────────────────────────────────────────────────────────────────────── */
function wireRefs() {
  el.modeBadge     = $('mode-badge');
  el.connDot       = $('conn-dot');
  el.connText      = $('conn-text');
  el.offlineBanner = $('offline-banner');
  el.runBtn        = $('run-btn');
  el.fastCheck     = $('fast-check');
  el.chaosState    = $('chaos-state');
  el.toastArea     = $('toast-area');

  el.naivePanel    = $('naive-panel');
  el.deadmanPanel  = $('deadman-panel');
  el.naiveChip     = $('naive-chip');
  el.deadmanChip   = $('deadman-chip');
  el.naiveMeta     = $('naive-meta');
  el.deadmanMeta   = $('deadman-meta');
  el.naiveTimeline  = $('naive-timeline');
  el.deadmanTimeline = $('deadman-timeline');

  el.naiveDE  = $('naive-de');
  el.deadmanDE = $('deadman-de');

  el.pmInput  = $('pm-input');
  el.pmContent = $('pm-content');

  el.chaosBtns = {};
  for (const c of CHAOS_TOGGLES) {
    el.chaosBtns[c.id] = $(`chaos-btn-${c.id}`);
    state.chaosActive[c.id] = false;
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Wire event handlers
───────────────────────────────────────────────────────────────────────────── */
function wireEvents() {
  // Chaos buttons
  for (const c of CHAOS_TOGGLES) {
    const btn = el.chaosBtns[c.id];
    if (btn) btn.addEventListener('click', () => toggleChaos(c.id));
  }

  // Reset button
  const resetBtn = $('btn-reset');
  if (resetBtn) resetBtn.addEventListener('click', resetChaos);

  // Run demo button
  if (el.runBtn) el.runBtn.addEventListener('click', startDemo);

  // Postmortem fetch
  const pmBtn = $('pm-fetch-btn');
  if (pmBtn) pmBtn.addEventListener('click', fetchPostmortem);

  if (el.pmInput) {
    el.pmInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') fetchPostmortem();
    });
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   Init
───────────────────────────────────────────────────────────────────────────── */
function init() {
  buildUI();
  wireRefs();
  wireEvents();
  renderChaosStateDisplay();
  renderScoreboard();

  // Initial health check + recurring poll
  pollHealth();
  setInterval(pollHealth, HEALTHZ_INTERVAL);
}

// Boot when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
