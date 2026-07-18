const $ = (id) => document.getElementById(id);
const tbody = $("tbody");
const statsEl = $("stats");
const actionMsg = $("actionMsg");
const importMsg = $("importMsg");
const logContent = $("logContent");
const progressWrap = $("progressWrap");
const progressFill = $("progressFill");
const progressLabel = $("progressLabel");
const progressCount = $("progressCount");
const progressStopBtn = $("progressStopBtn");

const DEFAULT_MODELS = [
  { id: "grok-4.5", name: "Grok 4.5" },
  { id: "grok-4.5-high", name: "High" },
  { id: "grok-4.5-medium", name: "Medium" },
  { id: "grok-4.5-low", name: "Low" },
];

const MAX_LOG = 500;
let logCache = [];
let logTimer = null;
let appReady = false;

function getKey() {
  return localStorage.getItem("grok_proxy_api_key") || "";
}
function setKey(v) {
  localStorage.setItem("grok_proxy_api_key", v || "");
}
function authHeaders() {
  // Session cookie is primary for dashboard; optional bearer for scripts
  const k = getKey();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

async function api(path, opts = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...authHeaders(),
    ...(opts.headers || {}),
  };
  const res = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (res.status === 401 && appReady) {
    showLogin("Session expired. Sign in again.");
    throw new Error("Unauthorized");
  }
  if (!res.ok) throw new Error(data.detail || data.error || text || res.status);
  return data;
}

function showLogin(msg) {
  appReady = false;
  const screen = $("loginScreen");
  const root = $("appRoot");
  if (screen) {
    screen.classList.remove("hidden");
    screen.style.display = "flex";
  }
  if (root) root.style.display = "none";
  if (msg && $("loginError")) $("loginError").textContent = msg;
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  if (typeof stopUsagePolling === "function") stopUsagePolling();
}

function showApp() {
  appReady = true;
  const screen = $("loginScreen");
  const root = $("appRoot");
  if (screen) {
    screen.classList.add("hidden");
    screen.style.display = "none";
  }
  if (root) root.style.display = "flex";
  if ($("loginError")) $("loginError").textContent = "";
}

async function checkAuth() {
  try {
    const r = await fetch("/api/auth/me", { credentials: "same-origin" });
    const data = await r.json();
    return !!data.authenticated;
  } catch {
    return false;
  }
}

async function doLogin(password) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = {}; }
  if (!res.ok) throw new Error(data.detail || "Invalid password");
  if (data.api_key) setKey(data.api_key);
  return data;
}

async function doLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {}
  setKey("");
  showLogin("");
}

function statusDot(status) {
  return `<div class="status-dot ${status || ""}">${status || "-"}</div>`;
}

function onOffTag(enabled) {
  return enabled 
    ? `<div class="on-off-tag on">ON</div>` 
    : `<div class="on-off-tag off">OFF</div>`;
}

function selectedIds() {
  return [...tbody.querySelectorAll("input.rowchk:checked")].map((x) => x.value);
}

async function copyText(text, msgEl, msg) {
  try {
    await navigator.clipboard.writeText(text || "");
    const target = msgEl || actionMsg;
    if (target) {
      const orig = target.textContent;
      target.textContent = msg || "Copied to clipboard";
      setTimeout(() => target.textContent = orig, 2000);
    }
  } catch {
    const target = msgEl || actionMsg;
    if (target) target.textContent = "Copy failed";
  }
}

function renderModels(models) {
  const list = models && models.length ? models : DEFAULT_MODELS;
  $("modelChips").innerHTML = list.map((m) => `
    <button class="model-chip btn-ghost" data-copy="${m.id}" title="Copy ${m.id}">
      ${m.id} <span>${m.name || ""}</span>
    </button>
  `).join("");
}

async function loadModels() {
  let models = DEFAULT_MODELS;
  try {
    const data = await api("/api/models");
    if (data.data && data.data.length) {
      models = data.data.map((m) => ({ id: m.id, name: m.id }));
    }
  } catch {}
  renderModels(models);
}

async function loadPublic() {
  try {
    const cfg = await fetch("/api/config/public").then((r) => r.json());
    const base = (cfg.endpoints && cfg.endpoints.openai_base) || `${cfg.base_url || ""}/v1`;
    $("endpointBase").textContent = base;
    $("endpointBase").dataset.base = base;
  } catch {
    $("endpointBase").textContent = `${location.origin}/v1`;
  }
  await loadModels();
}

async function reloadKeys() {
  const body = $("keysBody");
  try {
    const data = await api("/api/keys");
    const keys = data.keys || [];
    if (!getKey() && keys.length) {
      const first = keys.find((k) => k.enabled) || keys[0];
      if (first?.key) setKey(first.key);
    }
    body.innerHTML = keys.map((k) => `
      <tr class="${k.enabled ? "" : "disabled-row"}">
        <td>
          <div class="acc-email">${k.name || "-"}</div>
          <div class="acc-meta">${k.note || ""}</div>
        </td>
        <td class="mono-text">${k.key}</td>
        <td class="text-center">${onOffTag(k.enabled)}</td>
        <td>
          <div class="cell-actions">
            <button class="btn-icon" data-copy="${k.key}" title="Copy"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg></button>
            <button class="btn-icon" data-key-act="${k.enabled ? "disable" : "enable"}" data-id="${k.id}" title="Toggle"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="6" width="22" height="12" rx="6" ry="6"></rect><circle cx="${k.enabled ? '16' : '8'}" cy="12" r="2"></circle></svg></button>
            <button class="btn-icon" data-rr="${k.round_robin ? "on" : "off"}" data-id="${k.id}" title="Round-robin toggle" style="${k.round_robin ? "color:var(--info)" : "color:var(--text-tertiary)"}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg></button>
            <button class="btn-icon" data-key-act="delete" data-id="${k.id}" title="Delete"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>
            <button class="btn btn-secondary btn-sm" style="margin-left:8px" data-use-key="${k.key}">Select UI</button>
          </div>
        </td>
      </tr>
    `).join("") || `<tr><td colspan="4" style="text-align:center;padding:24px;color:var(--text-tertiary)">No API keys created yet. Generate one to authenticate clients.</td></tr>`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="err-text" style="text-align:center;padding:24px">${e.message || e}</td></tr>`;
  }
}

async function createKey() {
  const msg = $("createKeyMsg");
  msg.textContent = "Generating...";
  try {
    const name = $("newKeyName").value.trim() || "default";
    const note = $("newKeyNote").value.trim();
    const data = await api("/api/keys", {
      method: "POST",
      body: JSON.stringify({ name, note }),
    });
    const key = data.key?.key || "";
    msg.textContent = key ? `Generated key: ${key}` : "Key generated successfully";
    if (key) setKey(key);
    $("newKeyName").value = "";
    $("newKeyNote").value = "";
    await reloadKeys();
  } catch (e) {
    msg.textContent = e.message || String(e);
  }
}

function formatDate(isoStr) {
  if (!isoStr) return "-";
  return isoStr.replace("T", " ").replace(/\..+Z$/, "").slice(5, 16);
}

let progressTimer = null;

async function pollProgress() {
  try {
    const p = await api("/api/accounts/action-progress");
    if (p.running) {
      progressWrap.style.display = "block";
      const pct = p.total > 0 ? Math.round((p.done / p.total) * 100) : 0;
      progressFill.style.width = pct + "%";
      progressLabel.textContent = p.label || "Working...";
      progressCount.textContent = `${p.done}/${p.total}`;
      if (progressStopBtn) {
        progressStopBtn.style.display = p.can_stop ? "inline-flex" : "none";
        progressStopBtn.disabled = !!p.cancelled;
        progressStopBtn.textContent = p.cancelled ? "Stopping..." : "Stop";
      }
    } else {
      progressWrap.style.display = "none";
      if (progressStopBtn) progressStopBtn.style.display = "none";
      if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
    }
  } catch {}
}

function startProgressPoll() {
  if (progressTimer) return;
  pollProgress();
  progressTimer = setInterval(pollProgress, 1200);
}

// start progress poll on every action
function triggerProgress() {
  startProgressPoll();
}

if (progressStopBtn) {
  progressStopBtn.onclick = async () => {
    progressStopBtn.disabled = true;
    progressStopBtn.textContent = "Stopping...";
    try {
      await api("/api/warmup/stop", { method: "POST", body: "{}" });
      triggerProgress();
    } catch (e) {
      progressStopBtn.disabled = false;
      progressStopBtn.textContent = "Stop";
      if (actionMsg) {
        actionMsg.textContent = e.message || String(e);
        actionMsg.style.display = "block";
      }
    }
  };
}

async function reload() {
  const status = $("statusFilter").value;
  const enabled = $("enabledFilter").value;
  const q = $("q").value.trim();
  const qs = new URLSearchParams();
  if (status) qs.set("status", status);
  if (enabled !== "") qs.set("enabled", enabled);
  if (q) qs.set("q", q);
  
  const data = await api("/api/accounts?" + qs.toString());
  const st = data.stats || {};
  
  const formatter = new Intl.NumberFormat('en-US');
  const usedFormatted = formatter.format(st.active_tokens_used || 0);
  const remainingFormatted = formatter.format(st.pool_remaining || 0);
  const capacityFormatted = formatter.format(st.pool_capacity || 0);
  
  statsEl.innerHTML = `
    <div class="stat-item"><span class="stat-val">${st.total || 0}</span><span class="stat-label">Total Pool</span></div>
    <div class="stat-item"><span class="stat-val" style="color:var(--success)">${st.active_enabled || 0}</span><span class="stat-label">Active & Ready</span></div>
    <div class="stat-item"><span class="stat-val" style="color:var(--info)">${remainingFormatted}</span><span class="stat-label">Token Credit</span></div>
    <div class="stat-item"><span class="stat-val" style="color:var(--text-tertiary); font-size: 16px">${usedFormatted}</span><span class="stat-label">Tokens Used</span></div>
  `;
  
  tbody.innerHTML = (data.accounts || []).map((a) => `
    <tr class="${a.enabled ? "" : "disabled-row"}">
      <td class="text-center"><input type="checkbox" class="rowchk" value="${a.id}" /></td>
      <td>
        <div class="acc-email">${a.email}</div>
        <div class="acc-meta">${a.display_name || "-"} • Priority ${a.priority}</div>
      </td>
      <td>${statusDot(a.status)}</td>
      <td class="text-center">${onOffTag(a.enabled)}</td>
      <td class="mono-text">
        <div style="color:var(--text-secondary)">Exp: ${formatDate(a.expires_at)}</div>
        <div style="color:var(--text-tertiary)">Ref: ${formatDate(a.last_refresh_at)}</div>
      </td>
      <td>
        <div style="font-weight:500;margin-bottom:2px">${formatter.format(a.tokens_used || 0)} tk</div>
        <div class="acc-meta">${a.success_count || 0} ok / ${a.fail_count || 0} err</div>
      </td>
      <td class="err-text">${a.last_error || a.last_refresh_error || "-"}</td>
      <td>
        <div class="cell-actions">
          <button class="btn-icon" data-one="warmup" data-id="${a.id}" title="Warmup"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg></button>
          <button class="btn-icon" data-one="refresh" data-id="${a.id}" title="Refresh"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg></button>
          <button class="btn-icon" data-one="${a.enabled ? "disable" : "enable"}" data-id="${a.id}" title="Toggle"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="6" width="22" height="12" rx="6" ry="6"></rect><circle cx="${a.enabled ? '16' : '8'}" cy="12" r="2"></circle></svg></button>
          <button class="btn-icon danger" data-one="delete" data-id="${a.id}" title="Delete"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path></svg></button>
        </div>
      </td>
    </tr>
  `).join("");
}

function escapeHtml(s) {
  return (s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// kept for backward compat — WebSocket handles realtime now
function loadLog() {}

function badge(text, cls) {
  return `<span class="log-badge ${cls}">${escapeHtml(text)}</span>`;
}

function renderLogLine(e) {
  const kind = (e.kind || "").replace(/[^a-z0-9_]/g, "");
  const ts = new Date((e.ts || 0) * 1000);
  const time = ts.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const klass = kind.startsWith("proxy_ok") ? "green" : kind.startsWith("proxy_err") || kind.startsWith("refresh_err") ? "red" : kind.startsWith("refresh_ok") ? "blue" : "";
  let msg = escapeHtml(e.message || "");
  let prefix = badge(kind.replace(/_/g, " "), klass);

  // Rich proxy log rendering
  if (e.method) {
    const st = e.status || 0;
    const stCls = st < 300 ? "st-ok" : st < 400 ? "st-redir" : "st-err";
    prefix = badge(e.method, "method-" + e.method.toLowerCase()) + badge(st, stCls);
    msg = `${escapeHtml(e.account || "?")} ${msg}`;
  }
  if (e.duration_ms) {
    const d = e.duration_ms < 1000 ? `${e.duration_ms}ms` : `${(e.duration_ms/1000).toFixed(1)}s`;
    msg += ` ${badge(d, "dur")}`;
  }
  if (e.tokens) {
    msg += ` ${badge(e.tokens + " tk", "tk")}`;
  }
  if (e.model) {
    msg += ` ${badge(e.model, "model")}`;
  }
  return `<div class="log-line">${badge(time, "time")} ${prefix} <span class="log-msg">${msg}</span></div>`;
}

function flushLog() {
  if (!logCache.length) {
    logContent.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-tertiary)">No recent activity</div>';
    return;
  }
  const html = logCache.map(renderLogLine).join("");
  logContent.innerHTML = html;
}

function pushLog(event) {
  logCache.push(event);
  if (logCache.length > MAX_LOG) logCache.shift();
  // batch render: only append if already at bottom
  const atBottom = logContent.scrollTop >= logContent.scrollHeight - logContent.clientHeight - 50;
  const html = renderLogLine(event);
  if (logCache.length === 1 || logContent.children.length === 0 || logContent.querySelector(".log-line:first-child") === null) {
    flushLog();
  } else if (atBottom) {
    logContent.insertAdjacentHTML("beforeend", html);
  } else {
    flushLog();
  }
  if (atBottom) logContent.scrollTop = logContent.scrollHeight;
}

function connectLogWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/log`);
  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.kind === "ping") return;
      pushLog(data);
    } catch {}
  };
  ws.onclose = () => {
    setTimeout(connectLogWs, 2000);
  };
  ws.onerror = () => ws.close();
}

async function preloadLog() {
  try {
    const data = await api("/api/events?limit=120");
    const events = data.events || [];
    for (const e of events) {
      logCache.push({
        kind: e.kind || "event",
        message: e.message || "",
        ts: new Date(e.created_at || Date.now()).getTime() / 1000,
      });
    }
    flushLog();
  } catch {}
}

async function doImport() {
  const msgEl = $("importMsg");
  msgEl.textContent = "Processing import...";
  msgEl.style.color = "var(--info)";
  triggerProgress();
  try {
    const raw = $("importBox").value.trim();
    if (!raw) throw new Error("Please paste JSON first");
    const body = JSON.parse(raw);
    const data = await api("/api/accounts/import", { method: "POST", body: JSON.stringify(body) });
    msgEl.textContent = `Import: imported=${data.imported ?? 0}, active=${data.active ?? data.success ?? 0}, exhausted=${data.exhausted ?? 0}, rejected=${data.rejected ?? 0}, error=${data.error ?? 0}`;
    msgEl.style.color = (data.active || data.success) > 0 ? "var(--success)" : "var(--warning)";
    $("importBox").value = "";
    await reload();
    await loadLog();
  } catch (e) {
    msgEl.textContent = e.message || String(e);
    msgEl.style.color = "var(--danger)";
  }
}

async function act(kind) {
  triggerProgress();
  actionMsg.textContent = "Processing action...";
  actionMsg.style.display = "block";
  try {
    const ids = selectedIds();
    let data;
    if (kind === "refresh-selected") {
      if (!ids.length) throw new Error("Select accounts first");
      data = await api("/api/accounts/refresh", { method: "POST", body: JSON.stringify({ ids }) });
    } else if (kind === "refresh-active") {
      data = await api("/api/accounts/refresh", { method: "POST", body: JSON.stringify({ mode: "active" }) });
    } else if (kind === "refresh-exhausted") {
      data = await api("/api/accounts/refresh", { method: "POST", body: JSON.stringify({ mode: "exhausted" }) });
    } else if (kind === "refresh-all") {
      data = await api("/api/accounts/refresh", { method: "POST", body: JSON.stringify({ mode: "all" }) });
    } else if (kind === "warmup-selected") {
      if (!ids.length) throw new Error("Select accounts first");
      data = await api("/api/warmup", { method: "POST", body: JSON.stringify({ ids }) });
    } else if (kind === "warmup-active") {
      data = await api("/api/warmup", { method: "POST", body: JSON.stringify({ mode: "active" }) });
    } else if (kind === "warmup-exhausted") {
      data = await api("/api/warmup", { method: "POST", body: JSON.stringify({ mode: "exhausted" }) });
    } else if (kind === "warmup-all") {
      data = await api("/api/warmup", { method: "POST", body: JSON.stringify({ mode: "all" }) });
    } else if (kind === "disable-exhausted") {
      data = await api("/api/accounts/disable", { method: "POST", body: JSON.stringify({ mode: "exhausted" }) });
    } else if (kind === "enable-exhausted") {
      data = await api("/api/accounts/enable", { method: "POST", body: JSON.stringify({ mode: "exhausted" }) });
    } else if (kind === "disable-selected") {
      if (!ids.length) throw new Error("Select accounts first");
      data = await api("/api/accounts/disable", { method: "POST", body: JSON.stringify({ ids }) });
    } else if (kind === "enable-selected") {
      if (!ids.length) throw new Error("Select accounts first");
      data = await api("/api/accounts/enable", { method: "POST", body: JSON.stringify({ ids }) });
    } else if (kind === "select-exhausted") {
      // select only exhausted rows currently visible in table
      const rows = [...tbody.querySelectorAll("tr")];
      let n = 0;
      rows.forEach((tr) => {
        const chk = tr.querySelector("input.rowchk");
        const isExhausted = !!tr.querySelector(".status-dot.exhausted");
        if (chk) {
          chk.checked = isExhausted;
          if (isExhausted) n += 1;
        }
      });
      $("checkAll").checked = false;
      actionMsg.textContent = `Selected exhausted: ${n}`;
      actionMsg.style.display = "block";
      setTimeout(() => { if (actionMsg.textContent.startsWith("Selected exhausted")) actionMsg.textContent = ""; }, 3000);
      return;
    } else if (kind === "delete-selected") {
      if (!ids.length) throw new Error("Select accounts first");
      if (!confirm(`Delete ${ids.length} selected account(s)?`)) return;
      data = await api("/api/accounts/delete", { method: "POST", body: JSON.stringify({ ids }) });
    } else if (kind === "disable-dead") {
      data = await api("/api/accounts/disable", { method: "POST", body: JSON.stringify({ mode: "dead" }) });
    } else if (kind === "delete-dead") {
      if (!confirm("Delete all dead accounts?")) return;
      data = await api("/api/accounts/delete", { method: "POST", body: JSON.stringify({ mode: "dead" }) });
    }
    
    const s = data.success !== undefined ? `Success: ${data.success}` : '';
    const f = data.failed !== undefined ? `Failed: ${data.failed}` : '';
    const u = data.updated !== undefined ? `Updated: ${data.updated}` : '';
    const d = data.deleted !== undefined ? `Deleted: ${data.deleted}` : '';
    actionMsg.textContent = `Completed. ${[s,f,u,d].filter(Boolean).join(" • ")}`;
    
    await reload();
    await loadLog();
    setTimeout(() => { if(actionMsg.textContent.startsWith("Completed")) actionMsg.textContent = ""; }, 4000);
  } catch (e) {
    actionMsg.textContent = `Error: ${e.message || String(e)}`;
  }
}

// Event Listeners
$("importBtn").onclick = doImport;

// Drag & drop JSON files
const dropZone = $("dropZone");
dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); });
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", async (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const files = [...e.dataTransfer.files].filter(f => f.name.endsWith(".json"));
  if (!files.length) { $("importMsg").textContent = "No .json files found"; return; }
  $("importMsg").textContent = `Reading ${files.length} file(s)...`;
  const accounts = [];
  for (const file of files) {
    try {
      const text = await file.text();
      const data = JSON.parse(text);
      if (Array.isArray(data)) accounts.push(...data);
      else accounts.push(data);
    } catch (err) {
      $("importMsg").textContent = `Error reading ${file.name}: ${err.message}`;
      return;
    }
  }
  $("importMsg").textContent = `Importing ${accounts.length} account(s)...`;
  triggerProgress();
  try {
    const data = await api("/api/accounts/import", { method: "POST", body: JSON.stringify(accounts) });
    $("importMsg").textContent = `Import: imported=${data.imported ?? 0}, active=${data.active ?? data.success ?? 0}, exhausted=${data.exhausted ?? 0}, rejected=${data.rejected ?? 0}, error=${data.error ?? 0}`;
    $("importBox").value = "";
    await reload();
    await loadLog();
  } catch (err) {
    $("importMsg").textContent = err.message || String(err);
  }
});
$("reload").onclick = () => reload().catch((e) => actionMsg.textContent = e.message);
$("createKeyBtn").onclick = createKey;
$("reloadKeys").onclick = () => reloadKeys();
$("copyBase").onclick = async () => {
  await copyText($("endpointBase").dataset.base || $("endpointBase").textContent || "");
};
$("checkAll").onchange = (e) => {
  tbody.querySelectorAll("input.rowchk").forEach((c) => { c.checked = e.target.checked; });
};
document.querySelectorAll("button[data-act]").forEach((b) => {
  b.onclick = () => act(b.getAttribute("data-act"));
});

document.body.addEventListener("click", async (e) => {
  const copyBtn = e.target.closest("button[data-copy]");
  if (copyBtn) {
    await copyText(copyBtn.getAttribute("data-copy") || "");
    return;
  }
  const useKey = e.target.closest("button[data-use-key]");
  if (useKey) {
    setKey(useKey.getAttribute("data-use-key") || "");
    const orig = useKey.textContent;
    useKey.textContent = "Selected!";
    setTimeout(() => useKey.textContent = orig, 1500);
    return;
  }
    const keyAct = e.target.closest("button[data-key-act]");
    if (keyAct) {
      const id = keyAct.getAttribute("data-id");
      const actName = keyAct.getAttribute("data-key-act");
      try {
        if (actName === "delete") {
          if(!confirm("Delete this API key permanently?")) return;
          await api(`/api/keys/${id}`, { method: "DELETE" });
        }
        else if (actName === "enable") await api(`/api/keys/${id}/enable`, { method: "POST", body: "{}" });
        else if (actName === "disable") await api(`/api/keys/${id}/disable`, { method: "POST", body: "{}" });
        await reloadKeys();
      } catch (err) {
        alert(err.message || String(err));
      }
      return;
    }
    const rrBtn = e.target.closest("button[data-rr]");
    if (rrBtn) {
      const id = rrBtn.getAttribute("data-id");
      const cur = rrBtn.getAttribute("data-rr") === "on";
      try {
        const data = await api(`/api/keys/${id}/round-robin`, { method: "POST", body: JSON.stringify({ enabled: !cur }) });
        await reloadKeys();
      } catch (err) {
        alert(err.message || String(err));
      }
      return;
    }
});

tbody.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-one]");
  if (!btn) return;
  const id = btn.getAttribute("data-id");
  const one = btn.getAttribute("data-one");
  
  const origHTML = btn.innerHTML;
  btn.innerHTML = '...';
  triggerProgress();
  
  try {
    if (one === "warmup") await api(`/api/accounts/${id}/warmup`, { method: "POST", body: "{}" });
    else if (one === "refresh") await api("/api/accounts/refresh", { method: "POST", body: JSON.stringify({ ids: [id] }) });
    else if (one === "enable") await api("/api/accounts/enable", { method: "POST", body: JSON.stringify({ ids: [id] }) });
    else if (one === "disable") await api("/api/accounts/disable", { method: "POST", body: JSON.stringify({ ids: [id] }) });
    else if (one === "delete") {
      if (!confirm("Delete this account?")) {
        btn.innerHTML = origHTML;
        return;
      }
      await api(`/api/accounts/${id}`, { method: "DELETE" });
    }
    await reload();
    await loadLog();
  } catch (err) {
    alert(err.message || String(err));
    btn.innerHTML = origHTML;
  }
});

$("clearLog").onclick = () => {
  logContent.innerHTML = '<div style="padding:16px;text-align:center;color:var(--text-tertiary)">Log cleared</div>';
  logCache = [];
  fetch("/api/events/clear", { method: "POST", credentials: "same-origin", headers: authHeaders() }).catch(() => {});
};

// Activity rail collapse
const ACTIVITY_LS_KEY = "gcp_live_log_collapsed";
function setActivityCollapsed(collapsed) {
  const rail = $("activityRail");
  const btn = $("toggleActivity");
  if (!rail) return;
  rail.classList.toggle("collapsed", !!collapsed);
  if (btn) btn.title = collapsed ? "Expand activity" : "Collapse activity";
  try { localStorage.setItem(ACTIVITY_LS_KEY, collapsed ? "1" : "0"); } catch {}
}
function initActivityRail() {
  let collapsed = false;
  try { collapsed = localStorage.getItem(ACTIVITY_LS_KEY) === "1"; } catch {}
  setActivityCollapsed(collapsed);
  const btn = $("toggleActivity");
  if (btn) {
    btn.onclick = () => {
      const rail = $("activityRail");
      setActivityCollapsed(!rail.classList.contains("collapsed"));
    };
  }
}

// ---- Usage page ----
let currentPage = "setup";
let usageTab = "overview";
let usageTimer = null;

function fmtNum(n) {
  const v = Number(n) || 0;
  return v.toLocaleString("en-US");
}
function fmtTokensShort(n) {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(v % 1_000_000 === 0 ? 0 : 1) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(v % 1_000 === 0 ? 0 : 1) + "k";
  return String(v);
}
function fmtWhen(iso) {
  if (!iso) return "-";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso.slice(11, 16) || iso;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return String(iso).slice(11, 16) || "-";
  }
}
function relativeWhen(iso) {
  if (!iso) return "-";
  try {
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return fmtWhen(iso);
    const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
    return fmtWhen(iso);
  } catch {
    return fmtWhen(iso);
  }
}

function renderUsageChart(hourly) {
  const el = $("usageChart");
  if (!el) return;
  const data = Array.isArray(hourly) ? hourly : [];
  const values = data.map((h) => Number(h.tokens) || 0);
  const maxRaw = Math.max(...values, 0);
  const fixedTicks = [400_000, 800_000, 1_200_000, 1_600_000];
  let yMax = 1_600_000;
  if (maxRaw > yMax) {
    yMax = Math.ceil(maxRaw / 400_000) * 400_000;
  }
  const ticks = maxRaw > 1_600_000
    ? [yMax * 0.25, yMax * 0.5, yMax * 0.75, yMax]
    : fixedTicks;

  if (maxRaw === 0 && values.every((v) => v === 0)) {
    el.innerHTML = `<div class="chart-empty">No token usage recorded today</div>`;
    if ($("usageChartMeta")) $("usageChartMeta").textContent = "Peak 0";
    return;
  }

  const W = 900;
  const H = 300;
  const padL = 52;
  const padR = 16;
  const padT = 16;
  const padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const n = Math.max(data.length, 24);
  const xAt = (i) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const yAt = (v) => padT + plotH - (Math.min(v, yMax) / yMax) * plotH;

  const grid = ticks.map((t) => {
    const y = yAt(t);
    return `<line class="chart-grid-line" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>
      <text class="chart-axis-label" x="${padL - 8}" y="${y + 3}" text-anchor="end">${fmtTokensShort(t)}</text>`;
  }).join("");

  const baseY = padT + plotH;
  const pts = values.map((v, i) => `${xAt(i)},${yAt(v)}`).join(" ");
  const areaPts = `${xAt(0)},${baseY} ${pts} ${xAt(n - 1)},${baseY}`;
  const xLabels = [0, 6, 12, 18, 23].map((h) => {
    const i = Math.min(h, n - 1);
    return `<text class="chart-axis-label" x="${xAt(i)}" y="${H - 8}" text-anchor="middle">${String(h).padStart(2, "0")}:00</text>`;
  }).join("");
  const dots = values.map((v, i) =>
    v > 0 ? `<circle class="chart-dot" cx="${xAt(i)}" cy="${yAt(v)}" r="3"/>` : ""
  ).join("");

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    ${grid}
    <line class="chart-grid-line" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"/>
    <polygon class="chart-area" points="${areaPts}"/>
    <polyline class="chart-line" points="${pts}"/>
    ${dots}
    ${xLabels}
  </svg>`;
  if ($("usageChartMeta")) $("usageChartMeta").textContent = `Peak ${fmtTokensShort(maxRaw)}`;
}

function renderUsageRecent(rows) {
  const el = $("usageRecent");
  if (!el) return;
  const list = Array.isArray(rows) ? rows : [];
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">No requests yet today</div>`;
    return;
  }
  el.innerHTML = list.map((r) => {
    const model = r.model || "—";
    const inn = fmtNum(r.input_tokens);
    const out = fmtNum(r.output_tokens);
    return `<div class="recent-row">
      <span class="recent-model">${escapeHtml(model)}</span>
      <span class="recent-io">${inn} in / ${out} out</span>
      <span class="recent-when">${relativeWhen(r.created_at)}</span>
    </div>`;
  }).join("");
}

async function loadUsageOverview() {
  const data = await api("/api/usage/overview");
  if ($("usageTotalReq")) $("usageTotalReq").textContent = fmtNum(data.total_requests);
  if ($("usageInputTok")) $("usageInputTok").textContent = fmtNum(data.input_tokens);
  if ($("usageCachedTok")) $("usageCachedTok").textContent = fmtNum(data.cached_tokens);
  if ($("usageOutputTok")) $("usageOutputTok").textContent = fmtNum(data.output_tokens);
  renderUsageChart(data.hourly || []);
  renderUsageRecent(data.recent || []);
  return data;
}

async function loadUsageDetails() {
  const body = $("usageDetailsBody");
  const msg = $("usageDetailsMsg");
  if (!body) return;
  body.innerHTML = `<tr><td colspan="8" class="empty-state">Loading...</td></tr>`;
  try {
    const data = await api("/api/usage/requests?limit=200");
    const rows = data.requests || [];
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="8"><div class="empty-state">No requests yet today</div></td></tr>`;
      if (msg) msg.textContent = "";
      return;
    }
    body.innerHTML = rows.map((r) => `
      <tr>
        <td class="mono-text">${fmtWhen(r.created_at)}</td>
        <td class="mono-text">${escapeHtml(r.model || "—")}</td>
        <td class="text-right mono-text">${fmtNum(r.input_tokens)}</td>
        <td class="text-right mono-text">${fmtNum(r.cached_tokens)}</td>
        <td class="text-right mono-text">${fmtNum(r.output_tokens)}</td>
        <td class="text-right mono-text">${fmtNum(r.total_tokens)}</td>
        <td class="acc-meta">${escapeHtml(r.email || "—")}</td>
        <td class="text-center">${r.ok ? '<span class="on-off-tag on">OK</span>' : `<span class="on-off-tag off">${r.status_code || "ERR"}</span>`}</td>
      </tr>
    `).join("");
    if (msg) msg.textContent = `${rows.length} of ${data.total || rows.length} requests`;
  } catch (e) {
    body.innerHTML = `<tr><td colspan="8" class="err-text">${escapeHtml(e.message || String(e))}</td></tr>`;
  }
}

function setUsageTab(tab) {
  usageTab = tab === "details" ? "details" : "overview";
  document.querySelectorAll("[data-usage-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-usage-tab") === usageTab);
  });
  if ($("usageOverview")) $("usageOverview").classList.toggle("active", usageTab === "overview");
  if ($("usageDetails")) $("usageDetails").classList.toggle("active", usageTab === "details");
  if (usageTab === "details") loadUsageDetails().catch(() => {});
  else loadUsageOverview().catch(() => {});
}

function stopUsagePolling() {
  if (usageTimer) {
    clearInterval(usageTimer);
    usageTimer = null;
  }
}
function startUsagePolling() {
  stopUsagePolling();
  usageTimer = setInterval(() => {
    if (currentPage !== "usage") return;
    if (usageTab === "details") loadUsageDetails().catch(() => {});
    else loadUsageOverview().catch(() => {});
  }, 20000);
}

// ---- Image Generation page ----
let imgHistory = [];

function detectImageMime(b64) {
  try {
    const s = String(b64 || "").replace(/\s/g, "");
    if (s.startsWith("/9j/")) return "image/jpeg";
    if (s.startsWith("iVBOR")) return "image/png";
    if (s.startsWith("R0lGOD")) return "image/gif";
    if (s.startsWith("UklGR")) return "image/webp";
  } catch {}
  return "image/jpeg";
}

function buildImagePayload() {
  const prompt = ($("imgPrompt")?.value || "").trim();
  const model = $("imgModel")?.value || "grok-4.5";
  const size = $("imgSize")?.value || "1024x1024";
  const n = Math.max(1, Math.min(4, parseInt($("imgN")?.value || "1", 10) || 1));
  const quality = $("imgQuality")?.value || "standard";
  const negative = ($("imgNegative")?.value || "").trim();
  const payload = {
    model,
    prompt,
    n,
    size,
    quality,
    response_format: "b64_json",
  };
  if (negative) payload.negative_prompt = negative;
  return payload;
}

function refreshImagePayloadPreview() {
  const pre = $("imgPayloadPreview");
  if (!pre) return;
  try {
    pre.textContent = JSON.stringify(buildImagePayload(), null, 2);
  } catch {
    pre.textContent = "{}";
  }
}

function renderImageGallery() {
  const el = $("imgGallery");
  const meta = $("imgResultMeta");
  if (!el) return;
  if (meta) meta.textContent = `${imgHistory.length} image${imgHistory.length === 1 ? "" : "s"}`;
  if (!imgHistory.length) {
    el.innerHTML = `
      <div class="img-empty">
        <div class="img-empty-icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
        </div>
        <div class="empty-state" style="padding:8px 0 0;margin:0">No images yet. Write a prompt and generate.</div>
      </div>`;
    return;
  }
  el.innerHTML = imgHistory.map((item, idx) => {
    const statusCls = item.status === "ok" ? "ok" : item.status === "err" ? "err" : "pending";
    const statusLabel = item.status === "ok" ? "ready" : item.status === "err" ? "error" : "queued";
    const thumb = item.url
      ? `<img src="${escapeHtml(item.url)}" alt="generated" loading="lazy" />`
      : `<div class="img-card-placeholder">${item.status === "err" ? escapeHtml(item.error || "Failed") : "No preview"}</div>`;
    return `
      <div class="img-card" data-img-idx="${idx}">
        <div class="img-card-thumb">${thumb}</div>
        <div class="img-card-meta">
          <span class="img-status-badge ${statusCls}">${statusLabel}</span>
          <div class="img-card-prompt" title="${escapeHtml(item.prompt || "")}">${escapeHtml(item.prompt || "—")}</div>
          <div class="img-card-sub">${escapeHtml(item.model || "")} · ${escapeHtml(item.size || "")}</div>
          <div class="img-card-actions">
            ${item.url ? `<button class="btn btn-ghost btn-sm" data-img-copy="${idx}">Copy URL</button>` : ""}
            <button class="btn btn-ghost btn-sm" data-img-remove="${idx}">Remove</button>
          </div>
        </div>
      </div>`;
  }).join("");
}

function initImagePage() {
  const fields = ["imgPrompt", "imgModel", "imgSize", "imgN", "imgQuality", "imgNegative"];
  fields.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", refreshImagePayloadPreview);
    el.addEventListener("change", refreshImagePayloadPreview);
  });
  refreshImagePayloadPreview();
  renderImageGallery();

  const genBtn = $("imgGenerateBtn");
  if (genBtn) {
    genBtn.onclick = async () => {
      const payload = buildImagePayload();
      const msg = $("imgMsg");
      if (!payload.prompt) {
        if (msg) msg.textContent = "Prompt is required";
        return;
      }
      if (msg) msg.textContent = "Generating...";
      genBtn.disabled = true;
      const placeholders = [];
      for (let i = 0; i < payload.n; i++) {
        const row = {
          id: `${Date.now()}-${i}`,
          prompt: payload.prompt,
          model: payload.model,
          size: payload.size,
          quality: payload.quality,
          status: "pending",
          url: null,
          error: null,
          created_at: new Date().toISOString(),
        };
        placeholders.push(row);
        imgHistory.unshift(row);
      }
      renderImageGallery();
      try {
        const data = await api("/v1/images/generations", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        const results = Array.isArray(data?.data) ? data.data : [];
        placeholders.forEach((row, i) => {
          const item = results[i];
          if (item && (item.url || item.b64_json)) {
            row.status = "ok";
            if (item.url) {
              row.url = item.url;
            } else {
              const b64 = String(item.b64_json || "").replace(/\s/g, "");
              const mime = detectImageMime(b64);
              row.url = `data:${mime};base64,${b64}`;
              row.b64 = b64;
            }
            row.revised_prompt = item.revised_prompt || null;
          } else if (results.length === 0 && i === 0 && data?.error) {
            row.status = "err";
            row.error = data.error.message || String(data.error);
          } else {
            row.status = "err";
            row.error = "No image in response";
          }
        });
        if (msg) {
          const okN = placeholders.filter((r) => r.status === "ok").length;
          const usage = data?.usage;
          const usageTxt = usage?.total_tokens ? ` · ${Number(usage.total_tokens).toLocaleString()} tok` : "";
          msg.textContent = okN
            ? `Generated ${okN} image${okN === 1 ? "" : "s"}${usageTxt}`
            : (data?.error?.message || data?.detail || "Generation failed or endpoint unavailable");
        }
      } catch (e) {
        placeholders.forEach((row) => {
          row.status = "err";
          row.error = e.message || String(e);
        });
        if (msg) msg.textContent = e.message || String(e);
      } finally {
        genBtn.disabled = false;
        renderImageGallery();
      }
    };
  }

  const clearBtn = $("imgClearBtn");
  if (clearBtn) {
    clearBtn.onclick = () => {
      if ($("imgPrompt")) $("imgPrompt").value = "";
      if ($("imgNegative")) $("imgNegative").value = "";
      if ($("imgMsg")) $("imgMsg").textContent = "";
      refreshImagePayloadPreview();
    };
  }

  const gallery = $("imgGallery");
  if (gallery) {
    gallery.addEventListener("click", async (e) => {
      const copyBtn = e.target.closest("[data-img-copy]");
      if (copyBtn) {
        const idx = parseInt(copyBtn.getAttribute("data-img-copy"), 10);
        const item = imgHistory[idx];
        if (item?.url) await copyText(item.url, $("imgMsg"), "Image URL copied");
        return;
      }
      const remBtn = e.target.closest("[data-img-remove]");
      if (remBtn) {
        const idx = parseInt(remBtn.getAttribute("data-img-remove"), 10);
        if (!Number.isNaN(idx)) {
          imgHistory.splice(idx, 1);
          renderImageGallery();
        }
      }
    });
  }
}

function showPage(page) {
  const allowed = new Set(["setup", "usage", "images"]);
  currentPage = allowed.has(page) ? page : "setup";
  const setup = $("pageSetup");
  const usage = $("pageUsage");
  const images = $("pageImages");
  if (setup) setup.classList.toggle("active", currentPage === "setup");
  if (usage) usage.classList.toggle("active", currentPage === "usage");
  if (images) images.classList.toggle("active", currentPage === "images");
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.classList.toggle("active", el.getAttribute("data-page") === currentPage);
  });
  try { history.replaceState(null, "", `#${currentPage}`); } catch {}
  if (currentPage === "usage") {
    setUsageTab(usageTab);
    startUsagePolling();
  } else {
    stopUsagePolling();
  }
  if (currentPage === "images") refreshImagePayloadPreview();
}

function initNav() {
  document.querySelectorAll(".nav-item[data-page]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.preventDefault();
      showPage(el.getAttribute("data-page"));
    });
  });
  document.querySelectorAll("[data-usage-tab]").forEach((btn) => {
    btn.addEventListener("click", () => setUsageTab(btn.getAttribute("data-usage-tab")));
  });
  const reloadBtn = $("reloadUsageDetails");
  if (reloadBtn) reloadBtn.onclick = () => loadUsageDetails().catch(() => {});
  initImagePage();

  const hash = (location.hash || "").replace("#", "");
  if (hash === "usage" || hash === "images") showPage(hash);
  else showPage("setup");
}

// Init
async function bootApp() {
  showApp();
  initActivityRail();
  initNav();
  await loadPublic();
  await reload().catch((e) => { console.error(e); });
  await reloadKeys().catch(() => {});
  await preloadLog();
  connectLogWs();
}

async function init() {
  $("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    const pw = ($("loginPassword").value || "").trim();
    const err = $("loginError");
    const btn = $("loginBtn");
    err.textContent = "";
    if (!pw) { err.textContent = "Password required"; return; }
    btn.disabled = true;
    btn.textContent = "Signing in...";
    try {
      await doLogin(pw);
      $("loginPassword").value = "";
      await bootApp();
    } catch (ex) {
      err.textContent = ex.message || "Login failed";
    } finally {
      btn.disabled = false;
      btn.textContent = "Sign in";
    }
  };
  $("logoutBtn").onclick = () => doLogout();

  const ok = await checkAuth();
  if (ok) await bootApp();
  else showLogin("");
}

init();