"use strict";
/* Strings live in i18n.js: t(key, vars), num(n), fmtDate(ts), LANG, applyStaticI18n() */
const B = window.BASE || "";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let state = { links: [], nodes: [], settings: {}, ads: { tags: [] }, system: null, tab: "servers", timer: null };

const ago = (ts) => {
  if (!ts) return t("never");
  const s = Math.floor(Date.now() / 1000) - ts;
  if (s < 60) return t("just_now");
  if (s < 3600) return t("min_ago", { n: num(Math.floor(s / 60)) });
  if (s < 86400) return t("hour_ago", { n: num(Math.floor(s / 3600)) });
  return t("day_ago", { n: num(Math.floor(s / 86400)) });
};

async function api(path, opts = {}) {
  const r = await fetch(B + "/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Requested-With": "mtpanel", "X-Lang": LANG },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (r.status === 401 && path !== "/login") {
    showLogin();
    throw Object.assign(new Error(t("need_login")), { status: 401 });
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    let d = data.detail;
    if (Array.isArray(d)) d = d.map((x) => (x.loc || []).slice(-1)[0] + ": " + x.msg).join(t("sep"));
    throw Object.assign(new Error(d || t("err_status", { n: r.status })), { status: r.status });
  }
  return data;
}
function toast(msg, bad) {
  const el = $("#toast");
  el.textContent = msg; el.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.add("hidden"), bad ? 6000 : 3000);
}
function copy(text) {
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject())
    .then(() => toast(t("copied")), () => prompt(t("copy_prompt"), text));
}
function openModal(html) { $("#modalBody").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); $("#modalBody").innerHTML = ""; }
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
async function withBtn(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}
const publicUrl = () => (state.settings.public_base_url || location.origin) + "/p/" + state.settings.public_token;
const loggedIn = () => !$("#app").classList.contains("hidden");

/* ---------------- language ---------------- */
function setLang(lang) {
  if (lang === LANG) return;
  LANG = lang; LANG_SAVED = true;
  try { localStorage.setItem("mtp_lang", lang); } catch {}
  applyStaticI18n();
  if (!loggedIn()) return;
  state.settings.ui_lang = lang;
  api("/lang", { method: "POST", body: { lang } }).catch(() => {});
  if (!$("#wizard").classList.contains("hidden")) rerenderWizard();
  refresh();
}
document.addEventListener("click", (e) => { if (e.target.closest(".lang-toggle")) setLang(LANG === "fa" ? "en" : "fa"); });

/* ---------------- auth ---------------- */
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); clearInterval(state.timer); }
async function boot() {
  try { await api("/me"); } catch { return showLogin(); }
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  await loadSettingsState();
  const serverLang = state.settings.ui_lang;
  if (!LANG_SAVED && serverLang && serverLang !== LANG) {
    LANG = serverLang; applyStaticI18n();               // first visit from this browser: use the admin's last choice
  } else if (serverLang !== LANG) {
    api("/lang", { method: "POST", body: { lang: LANG } }).catch(() => {});  // remember it for background messages
  }
  await loadAdsState();
  await loadSystem();
  switchTab(location.hash.slice(1) || "servers");
  if (state.system && !state.system.setup_done) openWizard(0);
  clearInterval(state.timer); state.timer = setInterval(refresh, 15000);
}
async function loadSettingsState() {
  try {
    state.settings = await api("/settings");
    $("#title").textContent = document.title = state.settings.panel_title || "MTPanel";
  } catch {}
}
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault(); const f = new FormData(e.target);
  $("#loginErr").textContent = "";
  try { await api("/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } }); boot(); }
  catch (err) { $("#loginErr").textContent = err.message; }
});
$("#logout").onclick = async () => { await api("/logout", { method: "POST" }).catch(() => {}); showLogin(); };

/* ---------------- tabs ---------------- */
$$(".tabs a").forEach((a) => (a.onclick = () => switchTab(a.dataset.tab)));
function switchTab(tab) {
  if (!["servers", "links", "ads", "settings"].includes(tab)) tab = "servers";
  state.tab = tab; location.hash = tab;
  $$(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  $$("main > section").forEach((s) => s.classList.toggle("hidden", s.id !== "tab-" + tab));
  refresh();
}
function refresh() {
  if (document.hidden || !$("#modal").classList.contains("hidden")) return;
  if (state.tab === "servers") loadServers();
  if (state.tab === "links") loadLinks();
  if (state.tab === "ads") loadAds();
  if (state.tab === "settings") loadSettings();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

/* ---------------- servers ---------------- */
async function loadServers() {
  const d = await api("/dashboard").catch((e) => toast(e.message, true));
  if (!d) return;
  state.nodes = d.nodes;
  const up = d.nodes.filter((n) => n.online).length;
  $("#stats").innerHTML = [
    [t("st_online"), t("st_of", { a: num(up), b: num(d.nodes.length) })],
    [t("st_users"), num(d.users_online)],
    [t("st_conns"), num(d.connections)],
    [t("st_links"), num(d.links_active)],
  ].map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#nodeList").innerHTML = d.nodes.length ? d.nodes.map(nodeCard).join("")
    : `<div class="card"><b>${t("first_server_title")}</b><p class="muted">${t("first_server_text")}</p></div>`;
}
function nodeCard(n) {
  const s = n.status || {};
  const sponsorOk = s.route_mode === "middle" && !s.reroute_active;
  let dns = "";
  if (n.domain) {
    const ok = s.dns_ip === n.address;
    dns = `<div class="kv"><span>DNS</span><span class="${ok ? "okc" : "warnc"}">${ok ? t("dns_ok") : s.dns_ip ? `→ <span dir="ltr">${esc(s.dns_ip)}</span> ${t("dns_wait")}` : "—"}</span></div>`;
  }
  return `<div class="card node ${n.enabled ? "" : "off"}" data-id="${n.id}">
    <div class="node-h"><span class="dot ${!n.enabled ? "" : n.online ? "on" : "off"}"></span><b>${esc(n.name)}</b>
      ${s.route_mode ? `<span class="pill ${sponsorOk ? "active" : "limited"}">${sponsorOk ? t("sponsor_on") : t("sponsor_off")}</span>` : ""}</div>
    <div class="addr" dir="ltr">${esc(n.public_host)}:${n.public_port}</div>
    ${n.domain ? `<div class="kv"><span>${t("current_ip")}</span><span dir="ltr">${esc(n.address)}</span></div>` : ""}
    ${dns}
    <div class="kv"><span>${t("users_online")}</span><span>${num(s.users_online)}</span></div>
    <div class="kv"><span>${t("conns")}</span><span>${num(s.connections)}</span></div>
    <div class="kv"><span>${t("last_ip_change")}</span><span>${n.ip_changed_at ? ago(n.ip_changed_at) : "—"}</span></div>
    <div class="kv"><span>${t("last_seen")}</span><span>${n.enabled ? ago(n.last_seen) : t("disabled")}</span></div>
    ${n.last_error ? `<div class="node-err">${esc(n.last_error)}</div>` : ""}
    <div class="actions">
      <button class="btn sm primary" data-act="ip">${t("change_ip_btn")}</button>
      <button class="btn sm" data-act="edit">${t("edit")}</button>
      <button class="btn sm" data-act="sync">${t("sync_btn")}</button>
    </div></div>`;
}
$("#nodeList").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const n = state.nodes.find((x) => x.id == b.closest(".card").dataset.id);
  if (b.dataset.act === "ip") ipModal(n);
  if (b.dataset.act === "edit") nodeModal(n);
  if (b.dataset.act === "sync") withBtn(b, async () => { await api(`/nodes/${n.id}/sync`, { method: "POST" }); toast(t("synced")); loadServers(); });
});
$("#addNode").onclick = () => nodeModal(null);
const _nm = nodeModal; nodeModal = async (n) => { await loadAdsState(); _nm(n); };

function ipModal(n) {
  const cf = state.settings.cf_token_set;
  let note;
  if (n.domain && cf) note = `<p class="hint ok-box">${t("ip_note_ok", { domain: esc(n.domain) })}</p>`;
  else if (n.domain) note = `<p class="hint warn-box">${t("ip_note_nocf")}</p>`;
  else note = `<p class="hint warn-box">${t("ip_note_nodomain")}</p>`;
  openModal(`<h3>${t("ip_title", { name: esc(n.name) })}</h3>
    <p class="hint" style="margin:0 0 12px">${t("ip_current", { ip: esc(n.address) })}</p>
    ${note}
    <form id="ipf"><label>${t("new_ip")}<input name="ip" dir="ltr" required placeholder="1.2.3.4" autofocus></label>
    <div class="mfoot"><button class="btn primary">${t("apply")}</button><button type="button" class="btn ghost" id="mClose">${t("cancel")}</button></div></form>
    <p class="hint" style="margin-top:14px">${t("ip_replace_hint")}</p>`);
  $("#mClose").onclick = closeModal;
  const f = $("#ipf");
  f.onsubmit = (e) => {
    e.preventDefault();
    withBtn(f.querySelector(".btn.primary"), async () => {
      const r = await api(`/nodes/${n.id}/change-ip`, { method: "POST", body: { ip: f.ip.value.trim() } });
      closeModal();
      if (r.dns_error) toast(t("ip_dns_err") + r.dns_error, true);
      else if (!r.reachable) toast(t("ip_unreach"), true);
      else toast(r.links_changed ? t("ip_ok_changed") : t("ip_ok_same"));
      loadServers();
    });
  };
}

async function loadAdsState() { try { state.ads = await api("/ads"); } catch {} }
/* <select> of the named sponsor tags; emptyLabel === null leaves out the "none" option */
function tagSelect(name, current, emptyLabel) {
  const cur = (current || "").toLowerCase();
  const known = state.ads.tags.some((x) => x.tag === cur);
  return `<select name="${name}">` + (emptyLabel === null ? "" : `<option value="">${emptyLabel}</option>`) +
    state.ads.tags.map((x) => `<option value="${x.tag}" ${x.tag === cur ? "selected" : ""}>${esc(x.name)}</option>`).join("") +
    (cur && !known ? `<option value="${esc(cur)}" selected>${t("tag_unknown")} (${esc(cur.slice(0, 6))}…)</option>` : "") + `</select>`;
}

function parseConn(raw) {
  try {
    raw = raw.trim().replace(/^mtpnode:\/\//, "");
    return JSON.parse(atob(raw.replace(/-/g, "+").replace(/_/g, "/")));
  } catch { return null; }
}

function nodeModal(n) {
  const isNew = !n;
  openModal(`<h3>${isNew ? t("add_server") : t("edit_x", { name: esc(n.name) })}</h3>
  <form id="nf" novalidate>
    <label>${t("conn_code")} ${isNew ? "" : t("conn_code_replace")}<textarea name="conn" rows="3" dir="ltr" placeholder="mtpnode://..."></textarea></label>
    <p class="hint">${t("conn_code_hint")}</p>
    <label>${t("server_name")}<input name="name" required value="${esc(n?.name)}" placeholder="${t("server_name_ph")}"></label>
    <label>${t("link_domain")}<input name="domain" dir="ltr" value="${esc(n?.domain)}" placeholder="${t("link_domain_ph")}"></label>
    <p class="hint">${t("link_domain_hint")}</p>
    <details><summary>${t("conn_details")}</summary>
      <div class="row2"><label>${t("server_ip")}<input name="address" dir="ltr" required value="${esc(n?.address)}"></label>
        <label>${t("agent_port")}<input name="agent_port" type="number" required value="${n?.agent_port ?? 7443}"></label></div>
      <label>${t("agent_token")}<input name="token" dir="ltr" ${isNew ? "required" : ""} placeholder="${isNew ? "" : t("unchanged")}"></label>
      <label>${t("fingerprint")}<input name="fingerprint" dir="ltr" required value="${esc(n?.fingerprint)}"></label>
      <div class="row2"><label>${t("proxy_port")}<input name="public_port" type="number" value="${n?.public_port ?? ""}" placeholder="${t("auto")}"></label>
        <label>${t("tls_domain")}<input name="tls_domain" dir="ltr" value="${esc(n?.tls_domain)}" placeholder="${t("auto")}"></label></div>
      <p class="hint">${t("tls_hint")}</p>
      <label>${t("server_ad")}${tagSelect("ad_tag", n?.ad_tag, t("server_ad_default"))}</label>
      ${isNew ? "" : `<label>${t("status")}<select name="enabled"><option value="1">${t("enabled")}</option><option value="0" ${!n.enabled ? "selected" : ""}>${t("disabled")}</option></select></label>`}
    </details>
    <div class="mfoot"><button class="btn primary">${isNew ? t("connect_add") : t("save")}</button><button type="button" class="btn ghost" id="mClose">${t("cancel")}</button>
      ${isNew ? "" : `<button type="button" class="btn danger" id="nDel" style="margin-inline-start:auto">${t("delete")}</button>`}</div>
  </form>`);
  const f = $("#nf");
  $("#mClose").onclick = closeModal;
  f.conn.oninput = () => {
    const c = parseConn(f.conn.value); if (!c) return;
    f.address.value = c.a || ""; f.agent_port.value = c.p || 7443; f.token.value = c.t || ""; f.fingerprint.value = c.f || "";
    if (c.n && !f.name.value) f.name.value = c.n;
    toast(t("conn_read"));
  };
  if (!isNew) $("#nDel").onclick = () => withBtn($("#nDel"), async () => {
    if (!confirm(t("confirm_del_server"))) return;
    await api("/nodes/" + n.id, { method: "DELETE" }); closeModal(); loadServers();
  });
  f.onsubmit = (e) => {
    e.preventDefault();
    const body = {
      name: f.name.value.trim(), address: f.address.value.trim(), agent_port: +f.agent_port.value,
      token: f.token.value.trim(), fingerprint: f.fingerprint.value.trim(), domain: f.domain.value.trim(),
      public_port: f.public_port.value ? +f.public_port.value : null, tls_domain: f.tls_domain.value.trim(),
      ad_tag: f.ad_tag.value, enabled: f.enabled ? f.enabled.value === "1" : true,
    };
    if (!body.address || !body.fingerprint || (isNew && !body.token)) {
      f.querySelector("details").open = true;
      toast(f.conn.value.trim() ? t("conn_bad") : t("conn_paste"), true); return;
    }
    if (!body.name) { toast(t("need_name"), true); f.name.focus(); return; }
    withBtn(f.querySelector(".btn.primary"), async () => {
      const r = isNew ? await api("/nodes", { method: "POST", body }) : await api("/nodes/" + n.id, { method: "PATCH", body });
      closeModal();
      if (r.dns_error) toast(t("saved_dns_err") + r.dns_error, true);
      else if (r.warning) toast(t("saved_unreach") + r.warning, true);
      else toast(isNew ? t("server_added") : t("saved"));
      loadServers();
    });
  };
}

/* ---------------- links ---------------- */
async function loadLinks() {
  const [links, nodes] = await Promise.all([api("/links"), api("/nodes")]).catch((e) => (toast(e.message, true), [null]));
  if (!links) return;
  state.links = links; state.nodes = nodes; renderLinks();
}
$("#q").oninput = renderLinks;
function renderLinks() {
  const q = $("#q").value.trim().toLowerCase();
  const list = state.links.filter((l) => !q || l.name.toLowerCase().includes(q) || (l.note || "").toLowerCase().includes(q));
  if (!list.length) { $("#linkList").innerHTML = `<div class="empty">${state.links.length ? t("nothing_found") : t("no_links_yet")}</div>`; return; }
  const nodeName = (id) => (state.nodes.find((n) => n.id === id) || {}).name || "?";
  $("#linkList").innerHTML = list.map((l) => `<div class="lrow ${l.enabled ? "" : "off"}" data-id="${l.id}">
      <label class="switch" title="${t("toggle_title")}"><input type="checkbox" data-act="toggle" ${l.enabled ? "checked" : ""}><span></span></label>
      <div class="lmain"><div class="lname" dir="ltr">${esc(l.name)}</div>
        <div class="lsub">${l.node_ids == null ? t("all_servers") : l.node_ids.map(nodeName).map(esc).join(t("sep")) || t("no_server")}
          ${l.users_online ? ` · ${t("n_users", { n: num(l.users_online) })}` : ""}${l.note ? " · " + esc(l.note) : ""}</div></div>
      <div class="uact"><button class="btn sm" data-act="urls">${t("qr_copy")}</button><button class="btn sm" data-act="edit">⋯</button></div>
    </div>`).join("");
}
$("#linkList").addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]"); if (!el) return;
  const l = state.links.find((x) => x.id == el.closest(".lrow").dataset.id);
  if (el.dataset.act === "toggle") {
    withBtn(null, async () => { await api("/links/" + l.id, { method: "PATCH", body: { ...linkBody(l), enabled: el.checked } }); }).then(loadLinks);
  }
  if (el.dataset.act === "urls") urlsModal(l);
  if (el.dataset.act === "edit") linkModal(l);
});
$("#addLink").onclick = () => linkModal(null);
$("#publicBtn").onclick = () => publicModal();
const linkBody = (l) => ({ note: l.note, enabled: l.enabled, ad_tag: l.ad_tag, node_ids: l.node_ids });

function nodeChips(selected) {
  if (!state.nodes.length) return `<div class="hint">${t("no_servers_yet")}</div>`;
  const all = selected == null;
  return `<label class="chip"><input type="checkbox" name="allNodes" ${all ? "checked" : ""}> ${t("all_servers")}</label>` +
    state.nodes.map((n) => `<label class="chip"><input type="checkbox" name="node" value="${n.id}" ${all || selected.includes(n.id) ? "checked" : ""} ${all ? "disabled" : ""}> ${esc(n.name)}</label>`).join("");
}
function linkModal(l) {
  const isNew = !l;
  openModal(`<h3>${isNew ? t("new_link") : t("link_x", { name: esc(l.name) })}</h3>
  <form id="lf">
    ${isNew ? `<div class="row2"><label>${t("name_en")}<input name="name" dir="ltr" placeholder="link" pattern="[A-Za-z0-9_.\\-]*"></label>
      <label>${t("count")}<input name="count" type="number" min="1" max="200" value="1"></label></div>` : ""}
    <label>${t("note")}<input name="note" value="${esc(l?.note)}" placeholder="${t("note_ph")}"></label>
    <div class="hint">${t("which_servers")}</div><div class="chips">${nodeChips(l ? l.node_ids : null)}</div>
    <details><summary>${t("advanced")}</summary>
      <label>${t("link_ad")}${tagSelect("ad_tag", l?.ad_tag, t("link_ad_default"))}</label>
      ${isNew ? "" : `<label>${t("secret_for_bot")}<div class="mono" style="cursor:pointer" id="secCopy">${esc(l.secret)}</div></label>`}
    </details>
    <div class="mfoot"><button class="btn primary">${isNew ? t("create") : t("save")}</button><button type="button" class="btn ghost" id="mClose">${t("cancel")}</button></div>
    ${isNew ? "" : `<h3 style="margin-top:22px">${t("actions")}</h3><div class="actions">
      <button type="button" class="btn sm" data-op="rotate">${t("rotate_secret")}</button>
      <button type="button" class="btn sm danger" data-op="delete">${t("delete_link")}</button></div>`}
  </form>`);
  const f = $("#lf");
  $("#mClose").onclick = closeModal;
  if ($("#secCopy")) $("#secCopy").onclick = () => copy(l.secret);
  const all = f.querySelector("[name=allNodes]");
  if (all) all.onchange = () => $$("[name=node]", f).forEach((c) => { c.disabled = all.checked; c.checked = true; });
  f.onsubmit = (e) => {
    e.preventDefault();
    const body = { note: f.note.value, ad_tag: f.ad_tag.value, enabled: l ? l.enabled : true,
      node_ids: !all || all.checked ? null : $$("[name=node]:checked", f).map((c) => +c.value) };
    withBtn(f.querySelector(".btn.primary"), async () => {
      if (isNew) {
        const r = await api("/links", { method: "POST", body: { ...body, name: f.name.value.trim(), count: +f.count.value || 1 } });
        toast(t("n_links_created", { n: num(r.created.length) }));
      } else { await api("/links/" + l.id, { method: "PATCH", body }); toast(t("saved")); }
      closeModal(); loadLinks();
    });
  };
  f.querySelectorAll("[data-op]").forEach((b) => (b.onclick = () => withBtn(b, async () => {
    if (b.dataset.op === "delete") { if (!confirm(t("confirm_delete"))) return; await api("/links/" + l.id, { method: "DELETE" }); }
    else { if (!confirm(t("confirm_rotate"))) return; await api(`/links/${l.id}/rotate-secret`, { method: "POST" }); }
    closeModal(); toast(t("done")); loadLinks();
  })));
}

async function urlsModal(l) {
  const urls = await api(`/links/${l.id}/urls`).catch((e) => toast(e.message, true));
  if (!urls) return;
  openModal(`<h3 dir="ltr">${esc(l.name)}</h3>
    ${urls.length ? urls.map((u) => `<div class="linkbox"><b>${esc(u.node)}</b><br>
      <img src="${B}/qr?text=${encodeURIComponent(u.tg)}" alt="QR" loading="lazy">
      <div class="mono">${esc(u.https)}</div>
      <div class="actions" style="justify-content:center"><button class="btn sm primary" data-copy="${esc(u.https)}">${t("copy_link")}</button>
      <button class="btn sm" data-copy="${esc(u.tg)}">${t("copy_tg")}</button></div></div>`).join("")
      : `<div class="empty">${t("link_no_active_server")}</div>`}
    ${urls.length > 1 ? `<button class="btn block" id="copyAll">${t("copy_all")}</button>` : ""}
    <div class="mfoot"><button class="btn" id="mClose">${t("close")}</button></div>`);
  $("#mClose").onclick = closeModal;
  $$("[data-copy]", $("#modalBody")).forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
  if ($("#copyAll")) $("#copyAll").onclick = () => copy(urls.map((u) => `${u.node}:\n${u.https}`).join("\n\n"));
}

function publicModal() {
  const s = state.settings;
  openModal(`<h3>${t("public_title")}</h3>
    <p class="hint" style="margin:0 0 10px">${t("public_desc")}</p>
    ${s.public_enabled ? `<div class="mono">${esc(publicUrl())}</div>
    <div class="actions"><button class="btn sm primary" id="pc">${t("copy_address")}</button><a class="btn sm" href="${esc(publicUrl())}" target="_blank" rel="noopener">${t("open")}</a></div>`
    : `<p class="warn-box hint">${t("public_disabled")}</p>`}
    <div class="mfoot"><button class="btn" id="mClose">${t("close")}</button></div>`);
  $("#mClose").onclick = closeModal;
  if ($("#pc")) $("#pc").onclick = () => copy(publicUrl());
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  await loadSettingsState();
  const s = state.settings, f = $("#settingsForm");
  if (!(document.activeElement && f.contains(document.activeElement))) {
    f.panel_title.value = s.panel_title || "";
    f.public_enabled.checked = !!s.public_enabled;
    f.public_base_url.value = s.public_base_url || "";
    f.public_lang.value = s.public_lang === "en" ? "en" : "fa";
  }
  $("#publicUrl").innerHTML = s.public_enabled ? `${t("public_addr_is")} <span dir="ltr">${esc(publicUrl())}</span>` : "";
  $("#cfState").innerHTML = s.cf_token_set ? t("cf_set") : t("cf_unset");
  const sys = await loadSystem(), card = $("#accessCard");
  if (!(document.activeElement && card.contains(document.activeElement)) && !card.dataset.busy) {
    card.innerHTML = accessHtml(sys);
    bindAccess(card, sys);
  }
}
$("#settingsForm").onsubmit = (e) => {
  e.preventDefault(); const f = e.target;
  withBtn(f.querySelector("button"), async () => {
    await api("/settings", { method: "PUT", body: { panel_title: f.panel_title.value, public_enabled: f.public_enabled.checked,
      public_base_url: f.public_base_url.value.trim(), public_lang: f.public_lang.value } });
    toast(t("saved")); loadSettings();
  });
};
$("#rotatePublic").onclick = (e) => withBtn(e.target, async () => {
  if (!confirm(t("confirm_rotate_public"))) return;
  await api("/settings/rotate-public", { method: "POST" }); toast(t("new_address_made")); loadSettings();
});
$("#cfForm").onsubmit = (e) => {
  e.preventDefault(); const f = e.target;
  if (!f.cf_token.value.trim()) return toast(t("enter_token"), true);
  withBtn(f.querySelector("button"), async () => {
    const s = state.settings;
    await api("/settings", { method: "PUT", body: { panel_title: s.panel_title, public_enabled: s.public_enabled, cf_token: f.cf_token.value.trim() } });
    f.reset(); toast(t("token_saved")); loadSettings();
  });
};
$("#cfDel").onclick = (e) => withBtn(e.target, async () => {
  if (!confirm(t("confirm_delete_token"))) return;
  await api("/settings/cf-token", { method: "DELETE" }); loadSettings();
});
$("#pwForm").onsubmit = (e) => {
  e.preventDefault(); const f = e.target;
  withBtn(f.querySelector("button"), async () => {
    await api("/password", { method: "POST", body: { current: f.current.value, new: f.new.value, username: f.username.value.trim() || null } });
    f.reset(); toast(t("pass_changed"));
  });
};
$("#backupBtn").onclick = () => { location.href = B + "/api/backup"; };

/* ---------------- ads ---------------- */
const srcLabel = (k) => (I18N.fa["src_" + k] ? t("src_" + k) : esc(k));
function dur(sec) {
  sec = Math.max(0, sec);
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  const part = (key, n) => t(key, { n: num(n) });
  if (d) return part("dur_d", d) + (h ? t("and") + part("dur_h", h) : "");
  if (h) return part("dur_h", h) + (m ? t("and") + part("dur_m", m) : "");
  return part("dur_m", m);
}
async function loadAds() {
  await loadAdsState();
  const a = state.ads; if (!a.tags) return;
  $("#adsEnabled").checked = a.enabled;
  $("#adsNodes").innerHTML = a.nodes.length ? a.nodes.map((n) => `<div class="card adnode">
      <div class="node-h"><span class="dot ${n.enabled ? "on" : ""}"></span><b>${esc(n.name)}</b>
      ${n.has_tag ? `<span class="pill ${n.sponsor_ok ? "active" : "limited"}" title="route: ${esc(n.route_mode || "?")}">${n.sponsor_ok ? t("showing") : t("no_middle")}</span>` : ""}</div>
      <div class="big">${n.has_tag ? esc(n.tag_name || t("untitled_tag")) : t("no_ad")}</div>
      <div class="src">${t("source")}${srcLabel(n.source)}</div></div>`).join("")
    : `<div class="card muted">${t("no_servers_short")}</div>`;

  const now = Date.now() / 1000;
  const nodeName = (id) => (state.nodes.find((x) => x.id === id) || a.nodes.find((x) => x.id === id) || {}).name || "?";
  const stateLabel = { running: t("st_running"), upcoming: t("st_upcoming"), ended: t("st_ended") };
  $("#adsCamps").innerHTML = a.campaigns.length ? a.campaigns.map((c) => {
    const pct = c.state === "running" ? Math.min(100, ((now - c.start_at) / (c.end_at - c.start_at)) * 100) : 0;
    const when = c.state === "running" ? t("left", { d: dur(c.end_at - now) })
      : c.state === "upcoming" ? t("starts", { t: fmtDate(c.start_at) }) : t("ended_at", { t: fmtDate(c.end_at) });
    return `<div class="arow ${c.state}" data-id="${c.id}">
      <div class="grow"><div class="t">${esc(c.tag_name || t("deleted_tag"))} <span class="pill ${c.state === "running" ? "run" : ""}">${stateLabel[c.state]}</span></div>
        <div class="s">${c.node_ids == null ? t("all_servers") : c.node_ids.map(nodeName).map(esc).join(t("sep"))} · ${when}${c.note ? " · " + esc(c.note) : ""}</div>
        ${c.state === "running" ? `<div class="prog"><i style="width:${pct}%"></i></div>` : ""}</div>
      ${c.state !== "ended" ? `<button class="btn sm danger" data-act="stop">${t("stop")}</button>` : `<button class="btn sm" data-act="del">${t("delete")}</button>`}</div>`;
  }).join("") : `<div class="empty" style="padding:20px">${t("no_campaigns")}</div>`;

  $("#adsTags").innerHTML = a.tags.length ? a.tags.map((tg) => {
    const isDef = tg.id === a.default_tag_id;
    const uses = [tg.servers ? t("n_servers", { n: num(tg.servers) }) : "", tg.links ? t("n_links", { n: num(tg.links) }) : ""].filter(Boolean).join(t("sep"));
    return `<div class="arow" data-id="${tg.id}">
      <div class="grow"><div class="t">${esc(tg.name)} ${isDef ? `<span class="pill active">${t("default_badge")}</span>` : ""}</div>
        <div class="s"><span class="tagcode">${esc(tg.tag)}</span>${uses ? t("specific_on") + uses : ""}</div></div>
      ${isDef ? "" : `<button class="btn sm" data-act="def">${t("make_default")}</button>`}
      <button class="btn sm" data-act="edit">${t("edit")}</button>
      <button class="btn sm danger" data-act="del">${t("delete")}</button></div>`;
  }).join("") : `<div class="empty" style="padding:20px">${t("no_tags")}</div>`;
}
$("#adsEnabled").onchange = (e) => withBtn(null, async () => {
  await api("/ads/settings", { method: "PUT", body: { enabled: e.target.checked, default_tag_id: state.ads.default_tag_id } });
  toast(e.target.checked ? t("ads_on_toast") : t("ads_off_toast"));
}).then(loadAds);
$("#adsTags").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const tg = state.ads.tags.find((x) => x.id == b.closest(".arow").dataset.id);
  if (b.dataset.act === "edit") tagModal(tg);
  if (b.dataset.act === "def") withBtn(b, async () => {
    await api("/ads/settings", { method: "PUT", body: { enabled: state.ads.enabled, default_tag_id: tg.id } }); toast(t("made_default")); loadAds();
  });
  if (b.dataset.act === "del") withBtn(b, async () => {
    if (!confirm(t("confirm_del_tag", { name: tg.name }))) return;
    await api("/ads/tags/" + tg.id, { method: "DELETE" }); loadAds();
  });
});
$("#adsCamps").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const id = b.closest(".arow").dataset.id;
  withBtn(b, async () => {
    if (b.dataset.act === "stop") { if (!confirm(t("confirm_stop"))) return; await api(`/ads/campaigns/${id}/stop`, { method: "POST" }); }
    else await api(`/ads/campaigns/${id}`, { method: "DELETE" });
    loadAds();
  });
});
$("#addTag").onclick = () => tagModal(null);
$("#addCamp").onclick = async () => { if (!state.nodes.length) state.nodes = await api("/nodes").catch(() => []); campModal(); };

function tagModal(tg) {
  openModal(`<h3>${tg ? t("edit_tag") : t("add_tag")}</h3>
    <form id="tf"><label>${t("tag_name_label")}<input name="name" required value="${esc(tg?.name)}" placeholder="${t("tag_name_ph")}"></label>
    <label>${t("tag_label")}<input name="tag" dir="ltr" required maxlength="32" minlength="32" value="${esc(tg?.tag)}" placeholder="32 hex"></label>
    <div class="mfoot"><button class="btn primary">${t("save")}</button><button type="button" class="btn ghost" id="mClose">${t("cancel")}</button></div></form>`);
  $("#mClose").onclick = closeModal;
  const f = $("#tf");
  f.onsubmit = (e) => {
    e.preventDefault();
    withBtn(f.querySelector(".btn.primary"), async () => {
      const body = { name: f.name.value.trim(), tag: f.tag.value.trim() };
      if (tg) await api("/ads/tags/" + tg.id, { method: "PATCH", body }); else await api("/ads/tags", { method: "POST", body });
      closeModal(); toast(t("saved")); loadAds();
    });
  };
}
function campModal() {
  if (!state.ads.tags.length) { toast(t("need_tag_first"), true); return; }
  const pad = (n) => String(n).padStart(2, "0");
  const d = new Date(Date.now() + 3600e3);
  const local = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:00`;
  openModal(`<h3>${t("campaign_title")}</h3>
    <form id="cf">
      <label>${t("which_ad")}${tagSelect("tag", "", null)}</label>
      <div class="hint">${t("which_servers")}</div><div class="chips">${nodeChips(null)}</div>
      <label>${t("start")}<select name="when"><option value="now">${t("now_opt")}</option><option value="later">${t("later_opt")}</option></select></label>
      <label id="startWrap" class="hidden">${t("start_time")}<input name="start" type="datetime-local" value="${local}" dir="ltr"><span class="hint" id="startHint"></span></label>
      <div class="row2"><label>${t("days")}<input name="days" type="number" min="0" step="1" value="1"></label>
        <label>${t("plus_hours")}<input name="hours" type="number" min="0" max="23" step="1" value="0"></label></div>
      <label>${t("note")}<input name="note" placeholder="${t("camp_note_ph")}"></label>
      <p class="hint">${t("camp_hint")}</p>
      <div class="mfoot"><button class="btn primary">${t("submit")}</button><button type="button" class="btn ghost" id="mClose">${t("cancel")}</button></div>
    </form>`);
  const f = $("#cf");
  $("#mClose").onclick = closeModal;
  const all = f.querySelector("[name=allNodes]");
  if (all) all.onchange = () => $$("[name=node]", f).forEach((c) => { c.disabled = all.checked; c.checked = true; });
  const hint = () => { const v = new Date(f.start.value); $("#startHint").textContent = isNaN(v) ? "" : fmtDate(v / 1000); };
  f.when.onchange = () => { $("#startWrap").classList.toggle("hidden", f.when.value === "now"); hint(); };
  f.start.oninput = hint;
  f.onsubmit = (e) => {
    e.preventDefault();
    const hours = (+f.days.value || 0) * 24 + (+f.hours.value || 0);
    if (hours <= 0) return toast(t("need_duration"), true);
    const tag = state.ads.tags.find((x) => x.tag === f.tag.value);
    const body = { tag_id: tag.id, duration_hours: hours, note: f.note.value,
      start_at: f.when.value === "now" ? null : Math.floor(new Date(f.start.value) / 1000),
      node_ids: !all || all.checked ? null : $$("[name=node]:checked", f).map((c) => +c.value) };
    withBtn(f.querySelector(".btn.primary"), async () => {
      await api("/ads/campaigns", { method: "POST", body }); closeModal(); toast(t("submitted")); loadAds();
    });
  };
}

/* ---------------- panel access: domain certificate, port, path (wizard + settings) ---------------- */
async function loadSystem() { try { state.system = await api("/system"); } catch {} return state.system; }

function accessHtml(sys, title = true) {
  const head = title ? `<h3>${t("access_title")}</h3>` : "";
  if (!sys || !sys.managed) return head + `<p class="hint">${t("access_unmanaged")}</p>`;
  const domainOk = sys.https === "domain";
  const c = sys.cert || {};
  const certState = c.state === "running" ? `<div class="hint">${t("certificating")}</div>`
    : c.state === "error" ? `<div class="errline">${esc(c.message)}</div>` : "";
  return head + `
    <div class="kv"><span>${t("cur_address")}</span><span dir="ltr">${esc(sys.url)}</span></div>
    <div class="kv"><span>${t("https_cert")}</span><span>${domainOk ? t("cert_valid_for") + `<span dir="ltr">${esc(sys.host)}</span>` : t("cert_self")}</span></div>
    <div class="sec"><h4>${t("domain_cert_title")}</h4>
      <p class="hint" style="margin:0 0 8px">${t("domain_cert_hint", { ip: esc(sys.public_ip) })}</p>
      <div class="copyrow"><input data-a="domain" dir="ltr" placeholder="panel.example.com" value="${domainOk ? esc(sys.host) : ""}">
        <button class="btn" data-a="cert">${t("get_cert")}</button></div>
      <div data-a="certMsg">${certState}</div>
    </div>
    <div class="sec"><h4>${t("port_path_title")}</h4>
      <div class="row2"><label>${t("port")}<input data-a="port" type="number" min="1" max="65535" value="${sys.port}"></label>
        <label>${t("path")}<input data-a="path" dir="ltr" value="${esc(sys.path)}"></label></div>
      <p class="hint">${t("port_path_hint")}</p>
      <button class="btn" data-a="access">${t("apply_port_path")}</button>
    </div>`;
}

function bindAccess(root, sys) {
  if (!sys || !sys.managed) return;
  const q = (k) => root.querySelector(`[data-a="${k}"]`);
  q("cert").onclick = () => withBtn(q("cert"), async () => {
    const domain = q("domain").value.trim();
    if (!domain) { toast(t("enter_domain"), true); return; }
    q("certMsg").innerHTML = "";
    let r;
    try { r = await api("/system/certificate", { method: "POST", body: { domain } }); }
    catch (e) {
      // 412 = the domain does not point to this server (yet); let the admin try anyway
      if (e.status !== 412 || !confirm(e.message + "\n\n" + t("try_anyway"))) throw e;
      r = await api("/system/certificate", { method: "POST", body: { domain, force: true } });
    }
    root.dataset.busy = "1";
    q("certMsg").innerHTML = `<div class="hint">${t("cert_requesting")}</div>`;
    await pollCert(q("certMsg"), r.url);
    delete root.dataset.busy;
  });
  q("access").onclick = () => withBtn(q("access"), async () => {
    const port = +q("port").value, path = q("path").value.trim().replace(/^\/+|\/+$/g, "");
    if (!confirm(t("confirm_access", { port, path }))) return;
    const r = await api("/system/access", { method: "POST", body: { port, path } });
    showRestart(r.url, t("access_changed"));
  });
}

async function pollCert(box, newUrl) {
  for (let i = 0; i < 150; i++) {
    await new Promise((res) => setTimeout(res, 2000));
    let st;
    try { st = await api("/system/certificate"); } catch { continue; }
    if (st.state === "ok") { showRestart(newUrl, t("cert_got")); return; }
    if (st.state === "error") { box.innerHTML = `<div class="errline">${esc(st.message)}</div>`; return; }
  }
  box.innerHTML = `<div class="errline">${t("too_long")}</div>`;
}

async function showRestart(url, msg) {
  clearInterval(state.timer);
  $("#rsText").textContent = msg + t("restarting_text");
  $("#rsUrl").textContent = url;
  $("#rsGo").href = url;
  $("#restarting").classList.remove("hidden");
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // 1) wait until the current panel process has actually stopped
  for (let i = 0; i < 40; i++) {
    try { await fetch(B + "/api/me", { cache: "no-store" }); } catch { break; }
    await sleep(500);
  }
  // 2) wait until the panel answers on the new address, then go there
  for (let i = 0; i < 45; i++) {
    try { await fetch(url, { mode: "no-cors", cache: "no-store" }); break; } catch {}
    await sleep(1000);
  }
  location.href = url;
}

/* ---------------- first-run setup wizard ---------------- */
const WIZ = ["wiz_title", "wiz_server", "wiz_ads", "wiz_cf", "wiz_access"];
const wiz = { step: 0 };
function openWizard(step = 0) { wiz.step = step; $("#wizard").classList.remove("hidden"); renderWizard(); }
function closeWizard() { $("#wizard").classList.add("hidden"); refresh(); }
$("#rerunWizard").onclick = () => openWizard(0);

/* re-draw the current step in the new language, keeping what was typed */
async function rerenderWizard() {
  const keep = {};
  $$("#wizBody input[id], #wizBody textarea[id], #wizBody select[id]").forEach((el) => { keep[el.id] = el.value; });
  await renderWizard();
  for (const id in keep) { const el = document.getElementById(id); if (el) el.value = keep[id]; }
}

function wizNav({ skip = true, next = t("next") } = {}) {
  return `<div class="wiz-nav">
    ${wiz.step > 0 ? `<button class="btn ghost" data-w="back">${t("back")}</button>` : ""}
    <span class="sp"></span>
    ${skip ? `<button class="btn ghost" data-w="skip">${t("later")}</button>` : ""}
    <button class="btn primary" data-w="next">${next}</button></div>`;
}

let wizRender = 0;
async function renderWizard() {
  const body = $("#wizBody"), step = wiz.step, ticket = ++wizRender;
  $("#wizSteps").innerHTML = WIZ.map((k, i) => `<div class="${i < step ? "done" : i === step ? "cur" : ""}"><i></i>${t(k)}</div>`).join("");
  const go = (d) => { wiz.step = Math.max(0, Math.min(WIZ.length - 1, wiz.step + d)); renderWizard(); };
  const stale = () => ticket !== wizRender;   // a newer render started while we were loading data
  const bind = (onNext) => {
    body.querySelector('[data-w="back"]')?.addEventListener("click", () => go(-1));
    body.querySelector('[data-w="skip"]')?.addEventListener("click", () => go(1));
    $$("[data-copy]", body).forEach((b) => (b.onclick = () => copy(b.dataset.copy)));
    const nb = body.querySelector('[data-w="next"]');
    nb.addEventListener("click", () => withBtn(nb, async () => {
      if (onNext && (await onNext()) === false) return;
      if (wiz.step === WIZ.length - 1) {
        await api("/setup/done", { method: "POST" });
        if (state.system) state.system.setup_done = true;
        closeWizard();
        toast(t("setup_done_toast"));
      } else go(1);
    }));
  };

  if (step === 0) {
    const pl = state.settings.public_lang === "en" ? "en" : "fa";
    body.innerHTML = `<h2>${t("w0_h")}</h2>
      <p class="lead">${t("w0_lead")}</p>
      <label>${t("w0_label")}<input id="wTitle" value="${esc(state.settings.panel_title || "MTPanel")}"></label>
      <label>${t("public_lang_label")}<select id="wPubLang"><option value="fa" ${pl === "fa" ? "selected" : ""}>فارسی</option><option value="en" ${pl === "en" ? "selected" : ""}>English</option></select></label>
      ${wizNav({ skip: false })}`;
    bind(async () => {
      await api("/settings", { method: "PUT", body: { panel_title: $("#wTitle").value.trim() || "MTPanel",
        public_enabled: state.settings.public_enabled !== false, public_lang: $("#wPubLang").value } });
      await loadSettingsState();
    });
  }

  if (step === 1) {
    const nodes = await api("/nodes").catch(() => []);
    if (stale()) return;
    body.innerHTML = `<h2>${t("w1_h")}</h2>
      <p class="lead">${t("w1_lead")}</p>
      <div class="cmd">cd mtpanel && bash install.sh</div>
      <p class="hint" style="margin-top:0">${t("w1_hint")}</p>
      <textarea id="wConn" rows="3" dir="ltr" placeholder="mtpnode://..."></textarea>
      <label style="margin-top:8px">${t("w1_name")}<input id="wName" placeholder="${t("server_name_ph")}"></label>
      <button class="btn" id="wAdd">${t("add_server")}</button>
      <div id="wMsg"></div>
      <div class="added">${nodes.map((n) => `<div>✅ ${esc(n.name)} — <span dir="ltr">${esc(n.public_host)}:${n.public_port}</span></div>`).join("")}</div>
      ${wizNav({ skip: !nodes.length })}`;
    $("#wAdd").onclick = () => withBtn($("#wAdd"), async () => {
      const c = parseConn($("#wConn").value);
      if (!c || !c.a || !c.t || !c.f) { $("#wMsg").innerHTML = `<div class="errline">${t("w1_conn_bad")}</div>`; return; }
      $("#wMsg").innerHTML = `<div class="hint">${t("w1_connecting")}</div>`;
      try {
        await api("/nodes", { method: "POST", body: { name: $("#wName").value.trim() || c.n || c.a, address: c.a, agent_port: c.p || 7443, token: c.t, fingerprint: c.f } });
      } catch (e) { $("#wMsg").innerHTML = `<div class="errline">${esc(e.message)}</div>`; return; }
      const links = await api("/links").catch(() => []);
      if (!links.length) await api("/links", { method: "POST", body: { name: "link" } }).catch(() => {});
      toast(t("server_added"));
      renderWizard();
    });
    bind();
  }

  if (step === 2) {
    const [nodes, links] = await Promise.all([api("/nodes").catch(() => []), api("/links").catch(() => [])]);
    await loadAdsState();
    if (stale()) return;
    const n = nodes[0], l = links[0], has = state.ads.tags.length;
    body.innerHTML = `<h2>${t("w2_h")}</h2>
      <p class="lead">${t("w2_lead")}</p>
      <ol class="help-ol">
        <li>${t("tag_help_1")}</li>
        <li>${t("w2_2")}
          ${n && l ? `<div class="copyrow"><div class="mono">${esc(n.public_host)}:${n.public_port}</div><button class="btn sm" data-copy="${esc(n.public_host)}:${n.public_port}">${t("copy_btn")}</button></div>
          <div class="copyrow"><div class="mono">${esc(l.secret)}</div><button class="btn sm" data-copy="${esc(l.secret)}">${t("copy_btn")}</button></div>`
          : `<div class="hint">${t("w2_need_server")}</div>`}</li>
        <li>${t("w2_3")}</li>
      </ol>
      ${has ? `<div class="okline">${t("w2_has", { n: num(has) })}</div>` : ""}
      <input id="wTag" dir="ltr" maxlength="32" placeholder="${t("w2_tag_ph")}">
      <p class="hint">${t("w2_final")}</p>
      ${wizNav()}`;
    bind(async () => {
      const tag = $("#wTag").value.trim().toLowerCase();
      if (!tag || state.ads.tags.some((x) => x.tag === tag)) return;
      await api("/ads/tags", { method: "POST", body: { name: t("my_channel"), tag } });
      await loadAdsState();
    });
  }

  if (step === 3) {
    body.innerHTML = `<h2>${t("w3_h")}</h2>
      <p class="lead">${t("w3_lead")}</p>
      <p class="hint">${t("w3_hint")}</p>
      ${state.settings.cf_token_set ? `<div class="okline">${t("w3_set")}</div>` : ""}
      <input id="wCf" dir="ltr" type="password" placeholder="API Token">
      ${wizNav()}`;
    bind(async () => {
      const tok = $("#wCf").value.trim();
      if (!tok) return;
      await api("/settings", { method: "PUT", body: { panel_title: state.settings.panel_title, public_enabled: state.settings.public_enabled !== false, cf_token: tok } });
      await loadSettingsState();
    });
  }

  if (step === 4) {
    const sys = await loadSystem();
    if (stale()) return;
    body.innerHTML = `<h2>${t("w4_h")}</h2>
      <p class="lead">${t("w4_lead")}</p>
      <div id="wAccess"></div>
      ${wizNav({ skip: false, next: t("finish_setup") })}`;
    $("#wAccess").innerHTML = accessHtml(sys, false);
    bindAccess($("#wAccess"), sys);
    bind();
  }
}

applyStaticI18n();
boot();
