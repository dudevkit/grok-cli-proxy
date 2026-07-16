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
  return data;
}

async function doLogout() {
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {}
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
    } else {
      progressWrap.style.display = "none";
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
  fetch("/api/events/clear", { method: "POST" }).catch(() => {});
};

// Init
async function bootApp() {
  showApp();
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