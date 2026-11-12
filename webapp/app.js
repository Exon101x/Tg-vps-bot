/* vpsbot Mini App. Every request carries the initData Telegram signed for this session; the bot
   verifies the signature, so there is no login. Vanilla JS only - served by the bot itself. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

const tg = window.Telegram && window.Telegram.WebApp;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); }
applyTheme();

let ME = null;          // {user, servers, active}
const state = { cwd: "", tab: "files", view: null };

/* ---------------------------------------------------------------- icons */
const P = {
  folder: '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  img: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
  film: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  code: '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
  pen: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  term: '<path d="m4 17 6-5-6-5"/><path d="M12 19h8"/>',
  cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
  ram: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 17v3M12 17v3M17 17v3"/>',
  disk: '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="2"/>',
  gauge: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  server: '<rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><path d="M6 6.5h.01M6 17.5h.01"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  up: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  down: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4M12 17h.01"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14a9 3 0 0 0 18 0V5"/><path d="M3 12a9 3 0 0 0 18 0"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/>',
  mega: '<path d="m3 11 18-5v12L3 14v-3Z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M7 13h3l3-6 4 9"/>',
  layers: '<path d="m12 2 8.5 4.8v9.6L12 21.2l-8.5-4.8V6.8L12 2Z"/><path d="m3.5 6.8 8.5 4.8 8.5-4.8M12 21.2v-9.6"/>',
  box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"/>',
  key: '<path d="m21 2-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777Zm0 0L15.5 7.5m0 0 3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
};
function icon(name, size) {
  const s = size || 18;
  return '<svg viewBox="0 0 24 24" width="' + s + '" height="' + s + '" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (P[name] || P.file) + "</svg>";
}
function tileIcon(name, tileCls) {
  const t = el("div", "tile " + (tileCls || "t-file"));
  t.innerHTML = icon(name, 20);
  return t;
}

/* ---------------------------------------------------------------- theme */
function applyTheme() {
  /* dark/light follows the Telegram client theme; the aurora stays behind everything */
  const p = (tg && tg.themeParams) || {};
  let dark = true;
  const m = String(p.bg_color || "").match(/^#?([0-9a-f]{6})$/i);
  if (m) {
    const n = parseInt(m[1], 16);
    dark = (0.299 * (n >> 16 & 255) + 0.587 * (n >> 8 & 255) + 0.114 * (n & 255)) < 128;
  } else if (window.matchMedia) {
    dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {
    if (tg && tg.setHeaderColor) tg.setHeaderColor(dark ? "secondary_bg_color" : "bg_color");
    if (tg && tg.setBackgroundColor) tg.setBackgroundColor(dark ? "secondary_bg_color" : "bg_color");
  } catch (e) { /* older clients */ }
}
if (tg && tg.onEvent) tg.onEvent("themeChanged", applyTheme);

/* ---------------------------------------------------------------- feel */
function haptic(kind) {
  try {
    const h = tg && tg.HapticFeedback;
    if (!h) return;
    if (kind === "error" || kind === "success" || kind === "warning") h.notificationOccurred(kind);
    else if (kind === "select") h.selectionChanged();
    else h.impactOccurred(kind || "light");
  } catch (e) { /* desktop */ }
}

/* ---------------------------------------------------------------- api */
async function api(route, data) {
  const res = await fetch("/api/" + route, {
    method: "POST",
    headers: { "X-Tg-Init-Data": INIT, "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.error) {
    const e = new Error(body.error || ("HTTP " + res.status));
    e.needJoin = !!body.need_join; e.channel = body.channel || ""; e.joinUrl = body.url || "";
    throw e;
  }
  return body;
}

function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " error" : "");
  haptic(isErr ? "error" : "success");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), isErr ? 6000 : 2600);
}

function confirmDialog(text) {
  if (tg && tg.showConfirm) return new Promise((ok) => tg.showConfirm(text, (yes) => ok(yes)));
  return Promise.resolve(window.confirm(text));
}

function human(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < 4) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
}

/* ------------------------------------------------------- loading + empty */
function skelRows(n) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < n; i++) {
    const r = el("div", "skel-row");
    r.appendChild(el("div", "skel tile-s"));
    const c = el("div", "col");
    c.appendChild(el("div", "skel l1"));
    c.appendChild(el("div", "skel l2"));
    r.appendChild(c);
    frag.appendChild(r);
  }
  return frag;
}
function skelCards(n) {
  const frag = document.createDocumentFragment();
  for (let i = 0; i < n; i++) frag.appendChild(el("div", "skel-card skel"));
  return frag;
}
function swap(parent, node) { parent.innerHTML = ""; parent.appendChild(node); }
function emptyState(iconName, text) {
  const e = el("div", "empty");
  e.innerHTML = icon(iconName, 34);
  e.appendChild(el("div", null, text));
  return e;
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  if (!INIT) { $("#gate").classList.remove("hidden"); return; }
  try {
    ME = await api("me");
  } catch (e) {
    if (e.needJoin) return showJoinGate(e);
    $("#gate").classList.remove("hidden");
    $("#gate .card p").textContent = e.message;
    return;
  }
  applyCredit(ME.credit);
  $("#srv-chip").innerHTML = ME.active ? "<b></b>" + esc(ME.active) : "no server";
  if (ME.admin) $("#tab-admin").classList.remove("hidden");
  if (!ME.active) { showTab("servers"); } else { showTab("files"); loadFiles(); }
}

function showJoinGate(e) {
  /* FORCE_SUB is on and this account has not joined the channel yet */
  $("#join-text").textContent = e.message || "Join the channel to use this bot, then come back.";
  const join = $("#join-btn");
  if (e.joinUrl) { join.classList.remove("hidden"); join.onclick = () => openLink(e.joinUrl); }
  else { join.classList.add("hidden"); }
  $("#join-retry").onclick = async () => {
    try { ME = await api("me"); }
    catch (err) { if (!err.needJoin) toast(err.message, true); return; }
    $("#joingate").classList.add("hidden");
    boot();
  };
  $("#joingate").classList.remove("hidden");
}

function openLink(url) {
  if (tg && tg.openLink) return tg.openLink(url);
  window.open(url, "_blank", "noopener");
}

function applyCredit(credit) {
  /* the fork and developer links - please keep them, so people can find the source */
  if (!credit) return;
  if (credit.repo) { $("#foot-repo").href = credit.repo; }
  const a = $("#foot-dev");
  if (credit.dev_channel) {
    a.href = "https://t.me/" + credit.dev_channel;
    a.textContent = "by @" + credit.dev_channel;
    a.classList.remove("hidden");
    a.nextElementSibling.classList.remove("hidden");
  } else {
    a.classList.add("hidden");
    a.nextElementSibling.classList.add("hidden");
  }
}

function showTab(name) {
  state.tab = name;
  haptic("select");
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#v-" + name).classList.add("active");
  document.querySelectorAll("nav#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  if (name === "files") loadFiles();
  if (name === "servers") renderServers();
  if (name === "console") { $("#term-title").textContent = "ssh " + (ME && ME.active ? "- " + ME.active : ""); }
  if (name === "stats") loadStats();
  if (name === "queue") { loadJobs(); loadAudit(); }
  if (name === "admin") loadAdmin();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- files */
function fileTile(row) {
  if (row.d) return tileIcon("folder", "t-folder");
  const ext = (row.n.split(".").pop() || "").toLowerCase();
  if (["pdf"].includes(ext)) return tileIcon("file", "t-pdf");
  if (["zip", "tar", "gz", "bz2", "xz", "7z", "rar"].includes(ext)) return tileIcon("box", "t-zip");
  if (["mp4", "mkv", "avi", "mov", "webm"].includes(ext)) return tileIcon("film", "t-vid");
  if (["mp3", "flac", "wav", "ogg", "m4a"].includes(ext)) return tileIcon("music", "t-aud");
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp"].includes(ext)) return tileIcon("img", "t-img");
  if (["py", "js", "ts", "json", "sh", "yml", "yaml", "html", "css", "c", "cpp", "go", "rs", "java", "conf"].includes(ext)) return tileIcon("code", "t-code");
  return tileIcon("file", "t-file");
}

async function loadFiles() {
  const box = $("#filelist");
  swap(box, skelRows(5));
  let r;
  try {
    r = await api("ls", {});
  } catch (e) { swap(box, emptyState("alert", e.message)); return; }
  state.cwd = r.cwd;
  $("#cwd").textContent = r.cwd || "/";
  $("#srv-chip").innerHTML = "<b></b>" + esc(ME.active);
  const list = el("div", "list");
  if (!r.rows.length) list.appendChild(emptyState("folder", "This directory is empty"));
  r.rows.forEach((row) => list.appendChild(fileRow(row)));
  swap(box, list);
}

function fileRow(row) {
  const n = el("div", "row");
  n.appendChild(fileTile(row));
  const name = el("div", "grow", row.n);
  n.appendChild(name);
  n.appendChild(el("div", "size", row.d ? "" : human(row.s)));
  const mini = el("div", "mini");
  mini.onclick = (e) => e.stopPropagation();   /* row tap must not double-fire actions */

  const send = el("button", "ibtn send");
  send.innerHTML = icon("send", 15);
  send.title = "Send to my Telegram chat";
  send.onclick = async () => {
    send.disabled = true;
    try { toast((await api("send", { path: (state.cwd ? state.cwd + "/" : "") + row.n })).note); }
    catch (e) { toast(e.message, true); }
    send.disabled = false;
  };
  mini.appendChild(send);

  const ren = el("button", "ibtn");
  ren.innerHTML = icon("pen", 15);
  ren.title = "Rename";
  ren.onclick = () => sheetRename(row.n);
  mini.appendChild(ren);

  const del = el("button", "ibtn del");
  del.innerHTML = icon("trash", 15);
  del.title = "Delete";
  del.onclick = async () => {
    if (!(await confirmDialog("Delete " + row.n + " on the server?"))) return;
    try { await api("rm", { path: (state.cwd ? state.cwd + "/" : "") + row.n, force: true }); loadFiles(); }
    catch (e) { toast(e.message, true); }
  };
  mini.appendChild(del);
  n.appendChild(mini);

  n.onclick = () => {
    if (row.d) openDir(row.n);
    else if (row.s > 0) send.click();
  };
  return n;
}

async function openDir(name) {
  try {
    const r = await api("cd", { path: name });
    state.cwd = r.cwd;
    ME = await api("me");
    loadFiles();
  } catch (e) { toast(e.message, true); }
}

$("#up-dir").onclick = async () => {
  try { const r = await api("cd", { path: ".." }); state.cwd = r.cwd; ME = await api("me"); loadFiles(); }
  catch (e) { toast(e.message, true); }
};

$("#reload").onclick = () => { haptic("light"); loadFiles(); };

$("#mkdir").onclick = async () => {
  const name = promptInSheet("New folder name", "folder", async (v) => {
    try { await api("mkdir", { path: v }); loadFiles(); } catch (e) { toast(e.message, true); }
  });
};

function promptInSheet(title, placeholder, onOk, value) {
  const c = $("#sheet-content");
  c.innerHTML = "";
  c.appendChild(el("h3", null, title));
  const inp = el("input"); inp.placeholder = placeholder; if (value) inp.value = value;
  c.appendChild(inp);
  const ok = el("button", "btn primary", "OK");
  ok.onclick = () => { const v = inp.value.trim(); closeSheet(); if (v) onOk(v); };
  c.appendChild(ok);
  const cancel = el("button", "btn", "Cancel");
  cancel.onclick = closeSheet;
  c.appendChild(cancel);
  $("#sheet").classList.remove("hidden");
  setTimeout(() => inp.focus(), 120);
}

function sheetRename(old) {
  promptInSheet("Rename " + old, "new name", async (v) => {
    const base = state.cwd ? state.cwd + "/" : "";
    try { await api("mv", { src: base + old, dst: base + v }); loadFiles(); }
    catch (e) { toast(e.message, true); }
  }, old);
}

function closeSheet() { $("#sheet").classList.add("hidden"); }
$("#sheet").onclick = (e) => { if (e.target.id === "sheet") closeSheet(); };

/* ---------------------------------------------------------------- upload */
$("#file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  if (!f.size) { toast("that file is empty", true); return; }
  if (ME.max_mb && f.size > ME.max_mb * 1024 * 1024) { toast("over the WEB_MAX_MB cap (" + ME.max_mb + " MB)", true); return; }
  let init;
  try { init = await api("up/init", { name: f.name, size: f.size }); }
  catch (err) { return toast(err.message, true); }
  const chunk = init.chunk, total = f.size;
  $("#progress").classList.remove("hidden");
  const fill = $("#progress-fill"), text = $("#progress-text"),
        name = $("#progress-name"), pct = $("#progress-pct");
  name.textContent = f.name;
  pct.textContent = "0%";
  let sent = 0;
  try {
    for (let off = 0; off < total; off += chunk) {
      const blob = f.slice(off, Math.min(off + chunk, total));
      const buf = await blob.arrayBuffer();
      await sendChunk(init.token, off, buf);
      sent = off + blob.size;
      const p = Math.round(sent * 100 / total);
      fill.style.width = p + "%";
      pct.textContent = p + "%";
      text.textContent = human(sent) + " / " + human(total);
    }
    const done = await api("up/finish", { token: init.token, dest: "" });
    fill.style.width = "100%";
    pct.textContent = "100%";
    text.textContent = "";
    toast("queued as #" + done.id + " -> " + done.dest);
    setTimeout(() => $("#progress").classList.add("hidden"), 1500);
    loadAudit();
  } catch (err) {
    text.textContent = err.message + " - tap Upload again to resume";
    toast(err.message, true);
  }
};

async function sendChunk(token, offset, buf) {
  for (let attempt = 1; ; attempt++) {
    try {
      const res = await fetch("/api/up/chunk", {
        method: "POST",
        headers: { "X-Tg-Init-Data": INIT, "X-Up-Token": token, "X-Up-Offset": String(offset),
                   "Content-Type": "application/octet-stream" },
        body: buf,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body.error) throw new Error(body.error || "HTTP " + res.status);
      return;
    } catch (e) {
      if (attempt >= 3) throw e;
      await new Promise((ok) => setTimeout(ok, attempt * 1200));
    }
  }
}

/* ---------------------------------------------------------------- fetch url */
$("#fetch-btn").onclick = () => {
  promptInSheet("Download a URL on " + (ME.active || "the server"), "https://...", async (v) => {
    try { toast((await api("fetch", { url: v })).note); loadAudit(); }
    catch (e) { toast(e.message, true); }
  });
};

/* ---------------------------------------------------------------- servers */
async function renderServers() {
  const box = $("#serverlist");
  swap(box, skelRows(3));
  ME = await api("me");
  const list = el("div", "list");
  if (!ME.servers.length) list.appendChild(emptyState("server", "No servers yet. Add one below - it becomes active."));
  ME.servers.forEach((s) => list.appendChild(serverRow(s)));
  swap(box, list);
}

function serverRow(s) {
  const row = el("div", "row");
  row.appendChild(tileIcon(s.scheme === "ssh" ? "term" : "cloud", s.scheme === "ssh" ? "t-ssh" : "t-ftp"));
  const mid = el("div", "grow");
  const head = el("div", "head-row");
  head.appendChild(el("span", null, s.name));
  if (s.name === ME.active) head.appendChild(el("span", "badge on", "active"));
  if (s.monitor) head.appendChild(el("span", "badge dim", "monitored"));
  mid.appendChild(head);
  mid.appendChild(el("div", "sub2", s.scheme + "://" + s.user + "@" + s.host + ":" + s.port + "  " + (s.cwd || "/")));
  row.appendChild(mid);
  const mini = el("div", "mini");
  mini.onclick = (e) => e.stopPropagation();
  const use = el("button", "ibtn" + (s.name === ME.active ? "" : " send"));
  use.innerHTML = icon("check", 15);
  use.title = s.name === ME.active ? "Active" : "Make active";
  use.disabled = s.name === ME.active;
  use.onclick = async () => { try { await api("servers/use", { name: s.name }); renderServers(); } catch (e) { toast(e.message, true); } };
  mini.appendChild(use);
  const set = el("button", "ibtn");
  set.innerHTML = icon("pen", 15);
  set.title = "Settings";
  set.onclick = () => sheetServerSettings(s);
  mini.appendChild(set);
  const del = el("button", "ibtn del");
  del.innerHTML = icon("trash", 15);
  del.title = "Delete";
  del.onclick = async () => {
    if (!(await confirmDialog("Delete server " + s.name + "? Its stored credentials are erased."))) return;
    try { await api("servers/del", { name: s.name }); renderServers(); } catch (e) { toast(e.message, true); }
  };
  mini.appendChild(del);
  row.appendChild(mini);
  return row;
}

function sheetServerSettings(s) {
  const c = $("#sheet-content");
  c.innerHTML = "";
  c.appendChild(el("h3", null, s.name + " settings"));
  const folder = el("input"); folder.value = s.folder || ""; folder.placeholder = "folder template, e.g. {user}/{date}";
  const dup = el("select", "field");
  ["overwrite", "skip", "rename"].forEach((d) => {
    const o = el("option", null, d); o.value = d; if (s.dup === d) o.selected = true; dup.appendChild(o);
  });
  const mon = el("input"); mon.type = "checkbox"; mon.checked = !!s.monitor;
  const monLabel = el("label", "check-label");
  monLabel.appendChild(mon);
  monLabel.appendChild(el("span", null, "monitor for alerts (/monitor)"));
  c.appendChild(el("div", "dim", "Folder template (applies to every upload)"));
  c.appendChild(folder);
  c.appendChild(el("div", "dim", "When the file already exists"));
  c.appendChild(dup);
  c.appendChild(monLabel);
  const save = el("button", "btn primary", "Save");
  save.onclick = async () => {
    try {
      await api("servers/set", { name: s.name, folder: folder.value, dup: dup.value, monitor: mon.checked ? 1 : 0 });
      closeSheet(); renderServers(); toast("saved");
    } catch (e) { toast(e.message, true); }
  };
  const cancel = el("button", "btn", "Cancel");
  cancel.onclick = closeSheet;
  c.appendChild(save); c.appendChild(cancel);
  $("#sheet").classList.remove("hidden");
}

$("#s-add").onclick = async () => {
  const btn = $("#s-add");
  btn.disabled = true; btn.textContent = "Testing connection...";
  try {
    const r = await api("servers/add", {
      name: $("#s-name").value, target: $("#s-target").value,
      password: $("#s-password").value || undefined, key: $("#s-key").value || undefined,
    });
    $("#s-name").value = $("#s-target").value = $("#s-password").value = $("#s-key").value = "";
    toast("Added " + r.name + (r.hostkey ? " - host key pinned" : ""));
    renderServers();
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "Add server";
};

/* ---------------------------------------------------------------- console */
async function runTerm(force) {
  const cmd = $("#term-in").value.trim();
  if (!cmd) return;
  const out = $("#term-out");
  const line = el("div", "cmdline", "$ " + cmd);
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
  $("#term-in").value = "";
  haptic("light");
  let r;
  try { r = await api("term", { cmd, force }); }
  catch (e) { out.appendChild(el("div", "errline", "error: " + e.message)); out.scrollTop = out.scrollHeight; return; }
  if (r.confirm) {
    if (await confirmDialog("This command looks dangerous. Run it anyway?\n\n" + r.confirm)) return runTerm(true);
    out.appendChild(el("div", null, "(cancelled)"));
    return;
  }
  if (r.out) out.appendChild(el("div", null, r.out + (r.rc ? "\n[exit " + r.rc + "]" : "")));
  ME = await api("me");
  if (state.tab === "files") loadFiles();
  out.scrollTop = out.scrollHeight;
}
$("#term-run").onclick = () => runTerm(false);
$("#term-in").addEventListener("keydown", (e) => { if (e.key === "Enter") runTerm(false); });

/* ---------------------------------------------------------------- stats */
function statCard(iconName, k, v, meterPct) {
  const s = el("div", "stat");
  const ic = el("div", "ic");
  ic.innerHTML = icon(iconName, 17);
  const t = el("div", "txt");
  t.appendChild(el("div", "k", k));
  t.appendChild(el("div", "v", String(v)));
  if (meterPct != null) {
    const pct = Math.max(0, Math.min(100, meterPct));
    const m = el("div", "meter" + (pct > 90 ? " bad" : pct > 80 ? " warn" : ""));
    const i = el("i");
    i.style.width = pct + "%";
    m.appendChild(i);
    t.appendChild(m);
  }
  s.appendChild(ic);
  s.appendChild(t);
  return s;
}

async function loadStats() {
  const box = $("#stat-cards");
  swap(box, skelCards(4));
  let d;
  try { d = await api("stats", {}); }
  catch (e) { swap(box, emptyState("alert", e.message)); return; }
  box.innerHTML = "";
  if (d.host) {
    const host = statCard("server", "host", d.host);
    host.classList.add("span2");
    box.appendChild(host);
  }
  if (d.mem_total) {
    const pct = Math.round(d.mem_used * 100 / d.mem_total);
    box.appendChild(statCard("ram", "memory \u00b7 " + pct + "%", d.mem_used + " / " + d.mem_total + " MB", pct));
  }
  if (d.disk_total) {
    const pct = parseInt(d.disk_pct, 10) || Math.round(d.disk_used * 100 / d.disk_total);
    box.appendChild(statCard("disk", "disk \u00b7 " + pct + "%", (d.disk_used / 1048576).toFixed(1) + " / " + (d.disk_total / 1048576).toFixed(1) + " GB", pct));
  }
  box.appendChild(statCard("gauge", "load \u00b7 " + (d.cores || "?") + " cores", d.load || "?"));
  box.appendChild(statCard("clock", "uptime", d.uptime || "?"));
  if (d.os) box.appendChild(statCard("cpu", "os", d.os));
}

document.querySelectorAll("[data-view]").forEach((b) => {
  b.onclick = async () => {
    haptic("select");
    document.querySelectorAll(".seg .btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const out = $("#view-out");
    out.classList.remove("hidden");
    out.textContent = "loading...";
    try { const r = await api("view", { key: b.dataset.view }); out.textContent = r.out; }
    catch (e) { out.textContent = e.message; }
  };
});

/* ---------------------------------------------------------------- queue + audit */
const STATE_LABEL = { queued: ["queued", "dim"], running: ["running", "run"], done: ["done", "on"],
                      failed: ["failed", "bad"], cancelled: ["stopped", "dim"] };

async function loadJobs() {
  const box = $("#jobs");
  swap(box, skelRows(3));
  let r;
  try { r = await api("jobs", {}); }
  catch (e) { swap(box, emptyState("alert", e.message)); return; }
  const list = el("div", "list");
  if (!r.jobs.length) list.appendChild(emptyState("inbox", "No upload jobs yet. Files you send to the bot or upload here appear in this queue."));
  r.jobs.forEach((j) => {
    const row = el("div", "row");
    const mid = el("div", "grow");
    const [label, cls] = STATE_LABEL[j.state] || [j.state, "dim"];
    const head = el("div", "head-row");
    const badge = el("span", "badge " + cls, label);
    if (j.state === "running" || j.state === "queued") badge.appendChild(el("i"));
    head.appendChild(badge);
    head.appendChild(el("span", null, "#" + j.id + " " + j.name));
    mid.appendChild(head);
    mid.appendChild(el("div", "sub2", (j.server || "") + ":" + (j.dest || "") + (j.size ? " - " + human(j.size) : "") + (j.error ? " - " + j.error.slice(0, 80) : "")));
    row.appendChild(mid);
    const mini = el("div", "mini");
    mini.onclick = (e) => e.stopPropagation();
    if (j.state === "failed" || j.state === "cancelled") {
      const b = el("button", "ibtn send");
      b.innerHTML = icon("refresh", 15);
      b.title = "Retry";
      b.onclick = async () => { try { await api("jobs/action", { id: j.id, op: "retry" }); loadJobs(); } catch (e) { toast(e.message, true); } };
      mini.appendChild(b);
    }
    if (j.state === "queued" || j.state === "running") {
      const b = el("button", "ibtn del");
      b.innerHTML = icon("x", 15);
      b.title = "Stop";
      b.onclick = async () => { try { await api("jobs/action", { id: j.id, op: "stop" }); loadJobs(); } catch (e) { toast(e.message, true); } };
      mini.appendChild(b);
    }
    if (mini.childNodes.length) row.appendChild(mini);
    list.appendChild(row);
  });
  swap(box, list);
}

async function loadAudit() {
  try {
    const r = await api("audit", {});
    const box = $("#audit");
    box.textContent = r.rows.map((a) => new Date(a.ts * 1000).toISOString().slice(5, 16).replace("T", " ") + "  [" + a.server + "] " + a.action).join("\n") || "(nothing yet)";
  } catch (e) { /* the audit tab shows it on open */ }
}

/* ---------------------------------------------------------------- nav */
document.querySelectorAll("nav#tabs .tab").forEach((t) => { t.onclick = () => showTab(t.dataset.tab); });
$("#srv-chip").onclick = () => showTab("servers");
$("#foot-repo").onclick = (e) => { e.preventDefault(); openLink($("#foot-repo").href); };
$("#foot-dev").onclick = (e) => { e.preventDefault(); openLink($("#foot-dev").href); };

/* ---------------------------------------------------------------- admin */
async function loadAdmin() {
  if (!ME || !ME.admin) return;
  const cards = $("#adm-cards");
  swap(cards, skelCards(6));
  try { renderAdminOverview(await api("admin/overview", {})); }
  catch (e) { swap(cards, emptyState("alert", e.message)); }
  try { renderAdminUsers(await api("admin/users", {})); }
  catch (e) { /* the overview already said it */ }
}

function fmtUptime(s) {
  if (!s && s !== 0) return "?";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + "d " : "") + (h || d ? h + "h " : "") + m + "m";
}

function timeAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function renderAdminOverview(d) {
  const box = $("#adm-cards");
  box.innerHTML = "";
  const add = (ic, k, v) => box.appendChild(statCard(ic, k, v));
  add("users", "users" + (d.users.banned ? " \u00b7 " + d.users.banned + " banned" : ""), d.users.total);
  add("server", "servers \u00b7 " + d.servers.monitored + " watched", d.servers.total);
  add("check", "uploads done", d.jobs.done || 0);
  add("down", "moved", human(d.done.total[1] || 0));
  add("chart", "today", (d.done.day[0] || 0) + " \u00b7 " + human(d.done.day[1] || 0));
  add("clock", "queued / running", (d.jobs.queued || 0) + " / " + (d.jobs.running || 0));
  add("alert", "failed", d.jobs.failed || 0);
  add("gauge", "uptime", fmtUptime(d.bot.uptime));
  add("db", "database", d.bot.database);
  add("shield", "force-sub", d.bot.force_sub);
  add("layers", "staged", d.staging.files + " \u00b7 " + human(d.staging.bytes || 0));
  add("chart", "today / 7d", (d.done.day[0] || 0) + " / " + (d.done.week[0] || 0));
  const top = $("#adm-top");
  top.textContent = (d.top || []).map((t) => {
    const who = (t.uid === (ME.user && ME.user.id) ? "you" : "#" + t.uid);
    return who + ": " + t.uploads + " uploads, " + human(t.bytes);
  }).join("\n") || "(no finished uploads yet)";
}

function renderAdminUsers(d) {
  const box = $("#adm-users");
  box.innerHTML = "";
  if (!d.users.length) { box.appendChild(emptyState("users", "No known users yet.")); return; }
  d.users.forEach((u) => {
    const row = el("div", "row");
    const av = el("div", "avatar a" + (u.uid % 6), (u.username || u.name || "u").slice(0, 1).toUpperCase());
    row.appendChild(av);
    const mid = el("div", "grow");
    const head = el("div", "head-row");
    if (u.banned) head.appendChild(el("span", "badge bad", "banned"));
    head.appendChild(el("span", null, (u.username ? "@" + u.username : u.name || "") + " (" + u.uid + ")"));
    mid.appendChild(head);
    mid.appendChild(el("div", "sub2", u.servers + " server(s) - " + u.uploads + " uploads, " + human(u.bytes) + " - seen " + timeAgo(u.last_seen)));
    row.appendChild(mid);
    const mini = el("div", "mini");
    mini.onclick = (e) => e.stopPropagation();
    const ban = el("button", "banbtn " + (u.banned ? "un" : "bn"), u.banned ? "unban" : "ban");
    ban.onclick = async () => {
      const what = u.banned ? "unban" : "ban";
      if (!(await confirmDialog(what + " user " + u.uid + "?"))) return;
      try { await api("admin/ban", { uid: u.uid, flag: !u.banned }); loadAdmin(); }
      catch (e) { toast(e.message, true); }
    };
    mini.appendChild(ban);
    row.appendChild(mini);
    row.onclick = () => sheetAdminUser(u.uid);
    box.appendChild(row);
  });
}

async function sheetAdminUser(uid) {
  let d;
  try { d = await api("admin/user", { uid }); }
  catch (e) { return toast(e.message, true); }
  const c = $("#sheet-content");
  c.innerHTML = "";
  const u = d.user;
  c.appendChild(el("h3", null, (u.username ? "@" + u.username : u.name || "user") + " (" + u.uid + ")" + (u.banned ? " - BANNED" : "")));
  c.appendChild(el("div", "dim", "joined " + new Date(u.joined * 1000).toISOString().slice(0, 10) + " - seen " + timeAgo(u.last_seen)));
  c.appendChild(el("div", null, "uploads: " + d.usage.today[0] + " today (" + human(d.usage.today[1]) + "), " + d.usage.week[0] + " in 7 days (" + human(d.usage.week[1]) + ")"));
  c.appendChild(el("div", null, "waiting/running " + d.usage.pending + " - failed " + d.usage.failed));
  c.appendChild(el("div", "dim", "servers: " + (d.servers.length ? d.servers.map((s) => s.name + " (" + s.scheme + "://" + s.host + ")").join(", ") : "none")));
  const acts = (d.audit || []).map((a) => new Date(a.ts * 1000).toISOString().slice(5, 16).replace("T", " ") + "  [" + a.server + "] " + a.action).join("\n");
  if (acts) {
    const t = el("div", "term small mt", acts);
    c.appendChild(t);
  }
  const close = el("button", "btn", "Close");
  close.onclick = closeSheet;
  c.appendChild(close);
  $("#sheet").classList.remove("hidden");
}

$("#adm-bc-send").onclick = async () => {
  const text = $("#adm-bc").value.trim();
  if (!text) return toast("write the message first", true);
  if (!(await confirmDialog("Send this message to every known user?"))) return;
  try {
    const r = await api("admin/broadcast", { text });
    $("#adm-bc").value = "";
    toast("queued for " + r.queued + " user(s)");
  } catch (e) { toast(e.message, true); }
};

boot();
