"use strict";

const PRESETS = [[18, 6], [20, 4], [24, 0], [12, 12], [11, 13], [4, 4]];
const KIND_LABEL = { veg: "Veg", bloom: "Bloom", other: "Other" };
const KIND_ORDER = ["veg", "bloom", "other"];
const LOGIN_NEEDED = "log in first";
const REFRESH_MS = 15000;

const $ = (selector) => document.querySelector(selector);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node[key] = value;
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error(LOGIN_NEEDED);
  }
  if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
  return data;
}

const duration = (seconds) => {
  const minutes = Math.max(0, Math.round(seconds / 60));
  const h = Math.floor(minutes / 60);
  if (h >= 48) return `${Math.round(h / 24)} days`;
  return h ? `${h} h ${minutes % 60} min` : `${minutes} min`;
};
const when = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
const date = (ts) => new Date(ts * 1000).toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const hours = (minutes) => String(+(minutes / 60).toFixed(2));
const scheduleText = (phase) => `${hours(phase.on_minutes)}/${hours(phase.off_minutes)}`;
const localInput = (date_) => new Date(date_.getTime() - date_.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

let state = null;
let tab = "plugs";
let installPrompt = null;
const openRuns = new Set();

function showOffline() {
  if (state) {
    const box = $("#box-status");
    box.textContent = "Not connected";
    box.classList.add("bad");
    return;
  }
  $("#login").hidden = true;
  $("#offline").hidden = false;
}

function showLogin() {
  $("#app").hidden = true;
  $("#offline").hidden = true;
  $("#login").hidden = false;
  $("#login-password").focus();
}

function selectTab(name) {
  tab = name;
  for (const button of document.querySelectorAll(".tab")) button.classList.toggle("current", button.dataset.tab === name);
  for (const id of ["plugs", "history", "log"]) $(`#tab-${id}`).hidden = id !== name;
  refresh();
}

function act(promise) {
  promise.then(refresh).catch((error) => flash(error.message));
}

function flash(message) {
  const banner = $("#banner");
  banner.textContent = message;
  banner.hidden = false;
  setTimeout(() => { banner.hidden = true; }, 6000);
}

function cycleBar(zone) {
  if (zone.cycle_position === null) return null;
  const total = zone.phase.on_minutes + zone.phase.off_minutes;
  const on = el("div", { class: "cycle-on" });
  on.style.width = `${(zone.phase.on_minutes / total) * 100}%`;
  const now = el("div", { class: "cycle-now" });
  now.style.left = `calc(${(zone.cycle_position / total) * 100}% - 1px)`;
  return el("div", { class: "cycle", role: "img", ariaLabel: "Position in the light cycle" }, on, now);
}

function readings(unit) {
  if (!unit) return null;
  const parts = [];
  if (unit.power_mw !== null) parts.push(`${(unit.power_mw / 1000).toFixed(unit.power_mw < 10000 ? 1 : 0)} W`);
  if (unit.celsius !== null) parts.push(`${unit.celsius} °C at the plug`);
  return parts.length ? el("p", { class: "muted" }, parts.join(" · ")) : null;
}

function statusPill(zone) {
  const active = zone.unit ? zone.unit.active : null;
  if (active === null) return el("span", { class: "pill unknown" }, "Unknown");
  if (zone.desired !== null && zone.desired !== active) return el("span", { class: "pill unknown" }, "Switching…");
  return el("span", { class: `pill ${active ? "on" : "off"}` }, active ? "Light on" : "Light off");
}

function nextChangeText(zone, now) {
  if (zone.reason === "hold") return `Held until ${when(zone.hold_until)}, then back to the schedule`;
  if (zone.next_change === null) return zone.desired ? "Always on" : zone.desired === false ? "Always off" : "";
  const verb = zone.pending && zone.pending.started_at === zone.next_change ? "New schedule" : zone.desired ? "Off" : "On";
  return `${verb} in ${duration(zone.next_change - now)} (${when(zone.next_change)})`;
}

function totalsText(run) {
  return Object.entries(run.totals).map(([kind, t]) => `${KIND_LABEL[kind]} ${t.days} d`).join(" · ");
}

function zoneCard(zone, now) {
  const run = zone.run;
  const held = zone.reason === "hold";
  const active = zone.unit ? zone.unit.active : null;
  const buttons = el("div", { class: "buttons" });
  if (run) {
    buttons.append(el("button", { class: "primary", onclick: () => openSchedule(zone) }, "Change schedule"));
    if (!held) {
      buttons.append(el("button", { class: "quiet", onclick: () => act(api("POST", `/api/zones/${zone.id}/hold`, { state: !active })) }, active ? "Off until next change" : "On until next change"));
    } else {
      buttons.append(el("button", { class: "quiet", onclick: () => act(api("POST", `/api/zones/${zone.id}/hold`, { state: null })) }, "Back to schedule"));
    }
    buttons.append(el("button", { class: "quiet", onclick: () => {
      if (confirm(`End the grow "${run.name}"? The plug will then be left as it is.`)) act(api("POST", `/api/runs/${run.id}/end`, {}));
    } }, "End grow"));
  } else {
    buttons.append(el("button", { class: "primary", onclick: () => openSchedule(zone) }, "Start grow"));
  }
  const current = run && run.current;
  return el("article", { class: "card" },
    el("div", { class: "card-head" }, el("h3", {}, zone.name), statusPill(zone)),
    zone.problem ? el("p", { class: "problem" }, zone.problem) : null,
    current ? el("div", { class: "stage" }, `${KIND_LABEL[current.kind]} · day ${current.day} · week ${current.week}`) : null,
    run && !current ? el("p", { class: "line" }, `Starts ${date(run.started_at)}`) : null,
    !run ? el("p", { class: "muted" }, "No grow running, so this plug is left alone.") : null,
    zone.phase ? el("p", { class: "line" }, `${scheduleText(zone.phase)} · ${run.name}`) : null,
    cycleBar(zone),
    el("p", { class: "line" }, nextChangeText(zone, now)),
    zone.pending ? el("p", { class: "muted" }, `Then ${KIND_LABEL[zone.pending.kind]} ${scheduleText(zone.pending)} from ${when(zone.pending.started_at)}`) : null,
    readings(zone.unit),
    run && Object.keys(run.totals).length ? el("p", { class: "muted" }, totalsText(run)) : null,
    buttons,
  );
}

function renderPlugs() {
  const now = state.now;
  const zones = $("#zones");
  zones.replaceChildren(...state.zones.filter((z) => z.enabled).map((z) => zoneCard(z, now)));
  if (!state.zones.length) zones.append(el("p", { class: "empty" }, "No plugs yet. Add your first one."));
}

async function renderHistory() {
  const runs = await api("GET", "/api/runs");
  const root = $("#tab-history");
  if (!runs.length) {
    root.replaceChildren(el("p", { class: "empty" }, "No grows recorded yet."));
    return;
  }
  root.replaceChildren(el("div", { class: "history" }, ...runs.map(growCard)));
}

const shortDate = (ts) => new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
const dayCount = (days) => `${days} ${days === 1 ? "day" : "days"}`;
const kwhText = (kwh, estimated) => (kwh === null ? null : `${estimated ? "~" : ""}${kwh} kWh`);

function weeksAndDays(seconds) {
  const days = Math.floor(seconds / 86400);
  return `${Math.floor(days / 7)} wk ${days % 7} d`;
}

function splitBar(phases, totalSeconds) {
  const parts = phases.filter((p) => p.seconds > 0).map((p) => {
    const part = el("span", { class: `kind-${p.kind}`, title: `${KIND_LABEL[p.kind]} ${scheduleText(p)}: ${dayCount(p.days)}` });
    part.style.flexGrow = String(p.seconds / totalSeconds);
    part.style.flexBasis = "0";
    return part;
  });
  const summary = phases.map((p) => `${KIND_LABEL[p.kind]} ${scheduleText(p)} ${dayCount(p.days)}`).join(", ");
  return el("div", { class: "split", role: "img", ariaLabel: summary }, ...parts);
}

function statTile(kind, total) {
  return el("div", { class: "tile" },
    el("div", { class: "tile-label" }, el("span", { class: `swatch kind-${kind}` }), KIND_LABEL[kind]),
    el("div", { class: "tile-value" }, dayCount(total.days)),
    el("div", { class: "tile-sub" }, weeksAndDays(total.seconds)),
    el("div", { class: "tile-sub" }, `${total.light_hours} h light`));
}

function phaseRow(phase) {
  const until = phase.open ? "now" : shortDate(phase.ended_at);
  const facts = [`${shortDate(phase.started_at)} – ${until}`, `${phase.light_hours} h light`, kwhText(phase.kwh, phase.kwh_estimated)];
  return el("li", {},
    el("span", { class: `swatch kind-${phase.kind}` }),
    el("span", {}, `${KIND_LABEL[phase.kind]} ${scheduleText(phase)}`),
    el("span", { class: "phase-days" }, `${phase.days} d`),
    el("span", { class: "phase-detail" }, facts.filter(Boolean).join(" · ")));
}

function growCard(run) {
  const kinds = KIND_ORDER.filter((kind) => run.totals[kind]);
  const totals = kinds.map((kind) => run.totals[kind]);
  const seconds = totals.reduce((sum, t) => sum + t.seconds, 0);
  const light = totals.reduce((sum, t) => sum + t.light_hours, 0);
  const metered = totals.filter((t) => t.kwh !== null);
  const kwh = metered.length ? metered.reduce((sum, t) => sum + t.kwh, 0).toFixed(2) : null;
  const estimated = metered.some((t) => t.kwh_estimated);
  const footer = [kinds.length > 1 ? `${light.toFixed(1)} h light in total` : null, kwhText(kwh, estimated)].filter(Boolean).join(" · ");
  const until = run.ended_at ? shortDate(run.ended_at) : "now";
  const count = run.phases.length;
  const details = el("details", {
    open: openRuns.has(run.id),
    ontoggle: (event) => { if (event.target.open) openRuns.add(run.id); else openRuns.delete(run.id); },
  },
    el("summary", {}, `${count} schedule ${count === 1 ? "phase" : "phases"}`),
    el("ul", { class: "phases" }, ...run.phases.map(phaseRow)));
  return el("article", { class: "panel grow" },
    el("div", { class: "grow-head" }, el("h3", {}, run.name), el("span", { class: "grow-total" }, `${(seconds / 86400).toFixed(1)} days`)),
    el("p", { class: "muted" }, `${run.zone_name} · ${shortDate(run.started_at)} – ${until}`),
    seconds > 0 ? splitBar(run.phases, seconds) : el("p", { class: "muted" }, "Not started yet"),
    kinds.length ? el("div", { class: "tiles" }, ...kinds.map((kind) => statTile(kind, run.totals[kind]))) : null,
    footer ? el("p", { class: "muted" }, footer) : null,
    count ? details : null);
}

const EVENT_TEXT = {
  switch: (e) => `Switched ${e.active ? "on" : "off"} (${e.detail})`,
  switch_failed: (e) => `Switching ${e.active ? "on" : "off"} failed: ${e.detail}`,
  problem: (e) => `Problem: ${e.detail}`,
  problem_cleared: (e) => `Resolved: ${e.detail}`,
  hold: (e) => `Held ${e.active ? "on" : "off"} ${e.detail}`,
  hold_ended: () => "Back to the schedule",
  grow_started: (e) => `Grow started: ${e.detail}`,
  schedule_changed: (e) => `Schedule set: ${e.detail}`,
  box_error: (e) => `FRITZ!Box: ${e.detail}`,
  box_ok: () => "FRITZ!Box reachable again",
  box_cert: (e) => `FRITZ!Box has a new certificate, trusted automatically (${e.detail})`,
  internal_error: (e) => `Internal error: ${e.detail}`,
};

async function renderLog() {
  const events = await api("GET", "/api/events");
  const names = Object.fromEntries(state.zones.map((z) => [z.id, z.name]));
  const root = $("#tab-log");
  if (!events.length) {
    root.replaceChildren(el("p", { class: "empty" }, "Nothing has happened yet."));
    return;
  }
  root.replaceChildren(el("ul", { class: "log" }, ...events.map((e) => el("li", {},
    el("time", {}, date(e.ts)),
    el("b", {}, names[e.zone_id] || ""),
    el("span", {}, (EVENT_TEXT[e.kind] || (() => e.kind))(e))))));
}

function renderBox() {
  const box = $("#box-status");
  const ok = state.box.ok;
  box.textContent = ok ? "FRITZ!Box connected" : state.box.error ? `FRITZ!Box: ${state.box.error}` : "Waiting for the FRITZ!Box";
  box.classList.toggle("bad", !ok);
}

async function refresh() {
  try {
    state = await api("GET", "/api/state");
  } catch (error) {
    if (error.message !== LOGIN_NEEDED) showOffline();
    return;
  }
  $("#offline").hidden = true;
  $("#login").hidden = true;
  $("#app").hidden = false;
  renderBox();
  if (tab === "plugs") renderPlugs();
  else (tab === "history" ? renderHistory() : renderLog()).catch((error) => flash(error.message));
}

// the schedule dialog: a new grow when the zone has none, otherwise the next phase

let dialogZone = null;

function markPreset() {
  const on = Number($("#on-hours").value), off = Number($("#off-hours").value);
  for (const chip of document.querySelectorAll(".chip")) chip.classList.toggle("current", Number(chip.dataset.on) === on && Number(chip.dataset.off) === off);
}

function setHours(on, off) {
  $("#on-hours").value = on;
  $("#off-hours").value = off;
  markPreset();
}

function syncStart() {
  const growing = Boolean(dialogZone && dialogZone.run);
  $("#start-at-row").hidden = growing && $("#start-mode").value !== "at";
}

function openSchedule(zone) {
  dialogZone = zone;
  const growing = Boolean(zone.run);
  $("#schedule-title").textContent = growing ? `Change schedule: ${zone.name}` : `Start grow: ${zone.name}`;
  $("#grow-name-row").hidden = growing;
  $("#start-mode-row").hidden = !growing;
  $("#schedule-error").hidden = true;
  $("#grow-name").value = "";
  $("#grow-name").required = !growing;
  $("#start-mode").value = "next_on";
  $("#start-at").value = localInput(new Date());
  $("#kind").value = zone.phase ? zone.phase.kind : "veg";
  if (zone.phase) setHours(+hours(zone.phase.on_minutes), +hours(zone.phase.off_minutes));
  else setHours(18, 6);
  syncStart();
  $("#schedule-dialog").showModal();
}

$("#presets").append(...PRESETS.map(([on, off]) => el("button", {
  type: "button", class: "chip", onclick: () => setHours(on, off),
}, `${on}/${off}`)));
document.querySelectorAll(".chip").forEach((chip, index) => {
  chip.dataset.on = PRESETS[index][0];
  chip.dataset.off = PRESETS[index][1];
});

$("#on-hours").addEventListener("input", markPreset);
$("#off-hours").addEventListener("input", markPreset);
$("#start-mode").addEventListener("change", syncStart);

$("#schedule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {
    kind: $("#kind").value,
    on_minutes: Math.round(Number($("#on-hours").value) * 60),
    off_minutes: Math.round(Number($("#off-hours").value) * 60),
  };
  const at = $("#start-at").value ? new Date($("#start-at").value).getTime() / 1000 : null;
  try {
    if (dialogZone.run) {
      const mode = $("#start-mode").value;
      body.start = mode === "at" ? at : mode;
      await api("POST", `/api/runs/${dialogZone.run.id}/phases`, body);
    } else {
      body.name = $("#grow-name").value;
      body.first_light_on = at;
      await api("POST", `/api/zones/${dialogZone.id}/runs`, body);
    }
    $("#schedule-dialog").close();
    refresh();
  } catch (error) {
    $("#schedule-error").textContent = error.message;
    $("#schedule-error").hidden = false;
  }
});

$("#add-plug").addEventListener("click", async () => {
  const units = (await api("GET", "/api/units")).filter((u) => !u.taken);
  const select = $("#plug-unit");
  select.replaceChildren(...units.map((u) => el("option", { value: u.uid }, `${u.name}${u.power_mw === null ? " (no meter)" : ""}`)));
  const fill = () => {
    const unit = units.find((u) => u.uid === select.value);
    $("#plug-name").value = unit ? unit.name : "";
  };
  select.onchange = fill;
  fill();
  $("#plug-watts").value = "";
  $("#plug-error").textContent = units.length ? "" : "The box lists no other switchable plugs.";
  $("#plug-error").hidden = Boolean(units.length);
  $("#plug-dialog").showModal();
});

$("#plug-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const watts = $("#plug-watts").value;
  try {
    await api("POST", "/api/zones", { unit_uid: $("#plug-unit").value, name: $("#plug-name").value, lamp_watts: watts ? Number(watts) : null });
    $("#plug-dialog").close();
    refresh();
  } catch (error) {
    $("#plug-error").textContent = error.message;
    $("#plug-error").hidden = false;
  }
});

for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", () => button.closest("dialog").close());
for (const button of document.querySelectorAll(".tab")) button.addEventListener("click", () => selectTab(button.dataset.tab));

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("POST", "/api/login", { password: $("#login-password").value });
    $("#login-password").value = "";
    $("#login-error").hidden = true;
    refresh();
  } catch (error) {
    $("#login-error").textContent = error.message;
    $("#login-error").hidden = false;
  }
});

$("#retry").addEventListener("click", refresh);

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  $("#install").hidden = false;
});
window.addEventListener("appinstalled", () => { $("#install").hidden = true; });
$("#install").addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  await installPrompt.userChoice;
  installPrompt = null;
  $("#install").hidden = true;
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});

const refreshIfIdle = () => { if (!document.hidden && !document.querySelector("dialog[open]")) refresh(); };
document.addEventListener("visibilitychange", refreshIfIdle);

selectTab("plugs");
setInterval(refreshIfIdle, REFRESH_MS);
