/* vpsbot Mini App. Every request carries the initData Telegram signed for this session; the bot
   verifies the signature, so there is no login. Vanilla JS only - served by the bot itself. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

const tg = window.Telegram && window.Telegram.WebApp;
const INIT = tg ? tg.initData : "";
if (tg) { tg.ready(); tg.expand(); applyTheme(); }

let ME = null;          // {user, servers, active}
const state = { cwd: "", tab: "files", view: null };

/* ---------------------------------------------------------------- theme */
function applyTheme() {
  const p = tg.themeParams || {};
  const v = (k, d) => p[k] || d;
  const r = document.documentElement.style;
  r.setProperty("--bg", v("bg_color", "#ffffff"));
  r.setProperty("--panel", v("secondary_bg_color", "#f1f2f6"));
  r.setProperty("--text", v("text_color", "#0f172a"));
  r.setProperty("--dim", v("hint_color", "#7c8290"));
  r.setProperty("--link", v("link_color", "#2aabee"));
  r.setProperty("--accent", v("button_color", "#2aabee"));
  r.setProperty("--accent-text", v("button_text_color", "#ffffff"));
  r.setProperty("--line", v("secondary_bg_color", "#e3e6ea"));
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
  $("#srv-chip").innerHTML = ME.active ? "<b>&#9679;</b> " + esc(ME.active) : "no server";
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
  if (credit.dev_channel) {
    const a = $("#foot-dev");
    a.href = "https://t.me/" + credit.dev_channel;
    a.textContent = "by @" + credit.dev_channel;
    a.classList.remove("hidden");
    a.previousElementSibling.classList.remove("hidden");
  } else {
    $("#foot-dev").previousElementSibling.classList.add("hidden");
  }
}

function showTab(name) {
  state.tab = name;
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("#v-" + name).classList.add("active");
  document.querySelectorAll("nav#tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  if (name === "files") loadFiles();
  if (name === "servers") renderServers();
  if (name === "console") { /* keeps its output */ }
  if (name === "stats") loadStats();
  if (name === "queue") { loadJobs(); loadAudit(); }
  if (name === "admin") loadAdmin();
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------------- files */
async function loadFiles() {
  const box = $("#filelist");
  box.innerHTML = "";
  box.appendChild(el("div", "dim", "loading..."));
  let r;
  try {
    r = await api("ls", {});
  } catch (e) { box.innerHTML = ""; box.appendChild(el("div", "dim", e.message)); return; }
  state.cwd = r.cwd;
  $("#cwd").textContent = r.cwd || "/";
  $("#srv-chip").innerHTML = "<b>&#9679;</b> " + esc(ME.active);
  box.innerHTML = "";
  if (!r.rows.length) box.appendChild(el("div", "dim", "(empty directory)"));
  r.rows.forEach((row) => box.appendChild(fileRow(row)));
}

function fileRow(row) {
  const n = el("div", "row");
  n.appendChild(el("div", "ico", row.d ? "\uD83D\uDCC1" : "\uD83D\uDCC4"));
  const name = el("div", "grow", row.n);
  n.appendChild(name);
  n.appendChild(el("div", "size", row.d ? "" : human(row.s)));
  const mini = el("div", "mini");

  const send = el("button", null, "TG");
  send.title = "Send to my Telegram chat";
  send.onclick = async () => {
    send.disabled = true;
    try { toast((await api("send", { path: (state.cwd ? state.cwd + "/" : "") + row.n })).note); }
    catch (e) { toast(e.message, true); }
    send.disabled = false;
  };
  mini.appendChild(send);

  const ren = el("button", null, "mv");
  ren.onclick = () => sheetRename(row.n);
  mini.appendChild(ren);

  const del = el("button", null, "rm");
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

$("#reload").onclick = loadFiles;

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
  const fill = $("#progress-fill"), text = $("#progress-text");
  let sent = 0;
  try {
    for (let off = 0; off < total; off += chunk) {
      const blob = f.slice(off, Math.min(off + chunk, total));
      const buf = await blob.arrayBuffer();
      await sendChunk(init.token, off, buf);
      sent = off + blob.size;
      fill.style.width = (sent * 100 / total) + "%";
      text.textContent = f.name + " - " + human(sent) + " / " + human(total);
    }
    const done = await api("up/finish", { token: init.token, dest: "" });
    fill.style.width = "100%";
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
  ME = await api("me");
  const box = $("#serverlist");
  box.innerHTML = "";
  if (!ME.servers.length) box.appendChild(el("div", "dim", "No servers yet. Add one below - it becomes active."));
  ME.servers.forEach((s) => {
    const row = el("div", "row");
    row.appendChild(el("div", "ico", s.scheme === "ssh" ? "\uD83D\uDDA5" : "\uD83D\uDCE6"));
    const mid = el("div", "grow");
    const name = el("div", null, (s.name === ME.active ? "\u25B6 " : "") + s.name);
    mid.appendChild(name);
    mid.appendChild(el("div", "dim", s.scheme + "://" + s.user + "@" + s.host + ":" + s.port + "  " + (s.cwd || "/")));
    row.appendChild(mid);
    if (s.monitor) row.appendChild(el("span", "badge on", "monitored"));
    const mini = el("div", "mini");
    const use = el("button", null, s.name === ME.active ? "active" : "use");
    use.disabled = s.name === ME.active;
    use.onclick = async () => { try { await api("servers/use", { name: s.name }); renderServers(); } catch (e) { toast(e.message, true); } };
    mini.appendChild(use);
    const set = el("button", null, "set");
    set.onclick = () => sheetServerSettings(s);
    mini.appendChild(set);
    const del = el("button", null, "del");
    del.onclick = async () => {
      if (!(await confirmDialog("Delete server " + s.name + "? Its stored credentials are erased."))) return;
      try { await api("servers/del", { name: s.name }); renderServers(); } catch (e) { toast(e.message, true); }
    };
    mini.appendChild(del);
    row.appendChild(mini);
    box.appendChild(row);
  });
}

function sheetServerSettings(s) {
  const c = $("#sheet-content");
  c.innerHTML = "";
  c.appendChild(el("h3", null, s.name + " settings"));
  const folder = el("input"); folder.value = s.folder || ""; folder.placeholder = "folder template, e.g. {user}/{date}";
  const dup = el("select");
  ["overwrite", "skip", "rename"].forEach((d) => {
    const o = el("option", null, d); o.value = d; if (s.dup === d) o.selected = true; dup.appendChild(o);
  });
  dup.style.cssText = "width:100%;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)";
  const mon = el("input"); mon.type = "checkbox"; mon.checked = !!s.monitor;
  mon.style.cssText = "width:18px;height:18px;margin:8px 0";
  const monLabel = el("label", null, " monitor for alerts (/monitor)");
  monLabel.style.cssText = "display:flex;align-items:center;font-size:14px";
  monLabel.prepend(mon);
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
  const line = el("div", null, "$ " + cmd);
  line.style.color = "#7dd3fc";
  out.appendChild(line);
  out.scrollTop = out.scrollHeight;
  $("#term-in").value = "";
  let r;
  try { r = await api("term", { cmd, force }); }
  catch (e) { out.appendChild(el("div", null, "error: " + e.message)); out.scrollTop = out.scrollHeight; return; }
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
async function loadStats() {
  const box = $("#stat-cards");
  box.innerHTML = "";
  try {
    const d = await api("stats", {});
    box.innerHTML = "";
    const add = (k, v) => { const s = el("div", "stat"); s.appendChild(el("div", "k", k)); s.appendChild(el("div", "v", v)); box.appendChild(s); };
    add("host", d.host || "?");
    add("os", d.os || "");
    if (d.mem_total) add("RAM", d.mem_used + " / " + d.mem_total + " MB (" + Math.round(d.mem_used * 100 / d.mem_total) + "%)");
    if (d.disk_total) add("disk", d.disk_pct + " - " + (d.disk_used / 1048576).toFixed(1) + " / " + (d.disk_total / 1048576).toFixed(1) + " GB");
    add("load", (d.load || "?") + "  (" + (d.cores || "?") + " cores)");
    add("uptime", d.uptime || "?");
  } catch (e) {
    box.appendChild(el("div", "dim", e.message));
  }
}

document.querySelectorAll("[data-view]").forEach((b) => {
  b.onclick = async () => {
    const out = $("#view-out");
    out.classList.remove("hidden");
    out.textContent = "loading...";
    try { const r = await api("view", { key: b.dataset.view }); out.textContent = r.out; }
    catch (e) { out.textContent = e.message; }
  };
});

/* ---------------------------------------------------------------- queue + audit */
const STATE_LABEL = { queued: ["queued", "dim"], running: ["running", "dim"], done: ["done", "on"],
                      failed: ["failed", "bad"], cancelled: ["stopped", "dim"] };

async function loadJobs() {
  const box = $("#jobs");
  box.innerHTML = "";
  let r;
  try { r = await api("jobs", {}); } catch (e) { box.appendChild(el("div", "dim", e.message)); return; }
  if (!r.jobs.length) box.appendChild(el("div", "dim", "No upload jobs yet. Files you send to the bot or upload here appear in this queue."));
  r.jobs.forEach((j) => {
    const row = el("div", "row");
    const mid = el("div", "grow");
    const [label, cls] = STATE_LABEL[j.state] || [j.state, "dim"];
    const head = el("div", null, "");
    head.appendChild(el("span", "badge " + cls, label));
    head.appendChild(el("span", null, "  #" + j.id + " " + j.name));
    mid.appendChild(head);
    const sub = el("div", "dim", (j.server || "") + ":" + (j.dest || "") + (j.size ? " - " + human(j.size) : "") + (j.error ? " - " + j.error.slice(0, 80) : ""));
    mid.appendChild(sub);
    row.appendChild(mid);
    const mini = el("div", "mini");
    if (j.state === "failed" || j.state === "cancelled") {
      const b = el("button", null, "retry");
      b.onclick = async () => { try { await api("jobs/action", { id: j.id, op: "retry" }); loadJobs(); } catch (e) { toast(e.message, true); } };
      mini.appendChild(b);
    }
    if (j.state === "queued" || j.state === "running") {
      const b = el("button", null, "stop");
      b.onclick = async () => { try { await api("jobs/action", { id: j.id, op: "stop" }); loadJobs(); } catch (e) { toast(e.message, true); } };
      mini.appendChild(b);
    }
    if (mini.childNodes.length) row.appendChild(mini);
    box.appendChild(row);
  });
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
  try { renderAdminOverview(await api("admin/overview", {})); }
  catch (e) { $("#adm-cards").innerHTML = ""; $("#adm-cards").appendChild(el("div", "dim", e.message)); }
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
  const add = (k, v) => { const s = el("div", "stat"); s.appendChild(el("div", "k", k)); s.appendChild(el("div", "v", String(v))); box.appendChild(s); };
  add("users", d.users.total + (d.users.banned ? " (" + d.users.banned + " banned)" : ""));
  add("servers", d.servers.total + " (" + d.servers.monitored + " monitored)");
  add("uploads done", d.jobs.done || 0);
  add("moved", human(d.done.total[1] || 0));
  add("today", (d.done.day[0] || 0) + " - " + human(d.done.day[1] || 0));
  add("queued / running", (d.jobs.queued || 0) + " / " + (d.jobs.running || 0));
  add("failed", d.jobs.failed || 0);
  add("uptime", fmtUptime(d.bot.uptime));
  add("database", d.bot.database);
  add("force-sub", d.bot.force_sub);
  add("staged", d.staging.files + " - " + human(d.staging.bytes || 0));
  add("today / 7d", (d.done.day[0] || 0) + " / " + (d.done.week[0] || 0));
  const top = $("#adm-top");
  top.textContent = (d.top || []).map((t) => {
    const who = (t.uid === (ME.user && ME.user.id) ? "you" : "#" + t.uid);
    return who + ": " + t.uploads + " uploads, " + human(t.bytes);
  }).join("\n") || "(no finished uploads yet)";
}

function renderAdminUsers(d) {
  const box = $("#adm-users");
  box.innerHTML = "";
  if (!d.users.length) { box.appendChild(el("div", "dim", "No known users yet.")); return; }
  d.users.forEach((u) => {
    const row = el("div", "row");
    const ico = el("div", "ico", u.banned ? "\uD83D\uDED1" : "\uD83D\uDC64");
    row.appendChild(ico);
    const mid = el("div", "grow");
    const head = el("div", null, "");
    if (u.banned) head.appendChild(el("span", "badge bad", "banned"));
    head.appendChild(el("span", null, (u.username ? "@" + u.username : u.name || "") + " (" + u.uid + ")"));
    mid.appendChild(head);
    mid.appendChild(el("div", "dim", u.servers + " server(s) - " + u.uploads + " uploads, " + human(u.bytes) + " - seen " + timeAgo(u.last_seen)));
    row.appendChild(mid);
    const mini = el("div", "mini");
    const ban = el("button", null, u.banned ? "unban" : "ban");
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
    const t = el("div", "term small", acts);
    t.style.marginTop = "10px";
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
