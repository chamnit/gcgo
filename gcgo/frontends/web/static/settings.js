"use strict";
const $ = (id) => document.getElementById(id);
let ws;

function connect() {
  ws = new WebSocket("ws://" + location.host + "/ws");
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onopen = () => setState("online");
  ws.onclose = () => { setState("offline"); setTimeout(connect, 1000); };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function setState(s) {
  const el = $("state");
  el.textContent = s;
  el.className = "badge " + (s || "").split(":")[0];
}

function handle(m) {
  if (m.type.startsWith("ctl_")) {
    ctlHandle(m);
  } else if (m.type === "status") {
    setState(m.state || "—");
  } else if (m.type === "settings") {
    segSet("unitseg", "units", m.units);
    segSet("afterseg", "after", m.after);
    if (document.activeElement !== $("rateinp")) $("rateinp").value = m.rate;
    const ov = m.overrides || {};
    for (const g of ["feed", "rapid", "spindle", "toggles"]) {
      const btn = document.querySelector('#ovshow button[data-ov="' + g + '"]');
      if (btn) btn.classList.toggle("on", ov[g] !== false);
    }
  }
}

function segSet(id, attr, val) {
  const seg = $(id);
  if (!seg) return;
  for (const x of seg.children) x.classList.toggle("on", x.dataset[attr] === String(val));
}

document.addEventListener("click", (e) => {
  const b = e.target.closest(".seg button");
  if (b) {
    for (const x of b.parentElement.children) x.classList.toggle("on", x === b);
    if (b.dataset.units) send({ cmd: "units", value: b.dataset.units });
    else if (b.dataset.after) send({ cmd: "after", value: b.dataset.after });
    return;
  }
  const t = e.target.closest("#ovshow button");
  if (t) {
    const on = !t.classList.contains("on");
    t.classList.toggle("on", on);
    send({ cmd: "ovconfig", group: t.dataset.ov, value: on });
  }
});

// --- the controller's own settings ---
// Values come from the controller (the server reads caPy's $settings.all or
// GRBL's $$); what each one means -- kind, range, default, text -- comes from
// settings.json, generated from caPy's settings table by tools/sync_capy.py.
let schema = null, ctl = null, codes = { errors: {} };
const loadSchema = fetch("/settings.json").then((r) => r.json())
  .then((j) => { schema = j; }).catch(() => { schema = { capy: [], grbl: {} }; });
fetch("/codes.json").then((r) => r.json()).then((j) => { codes = j; }).catch(() => {});

function ctlLoad() {
  $("ctlload").disabled = true;
  $("ctlrows").textContent = "reading…";
  send({ cmd: "ctl_settings" });
}

// what a setting is: {kind, range, def, meaning, mark} or null
function info(fw, key) {
  if (!schema) return null;
  if (fw === "grbl") {
    const g = schema.grbl[key];
    if (!g) return null;
    return g[1] === "bool" ? { kind: "enum", range: "0,1", meaning: g[0], labels: ["disabled", "enabled"] }
                           : { kind: "num", range: "-inf..inf", meaning: g[0] + (g[1] ? " (" + g[1] + ")" : "") };
  }
  if (!schema._capy) schema._capy = Object.fromEntries(schema.capy.map((r) => [r[0], r]));
  const r = schema._capy[key];
  if (!r) return null;
  const mk = /^(\*\*?) /.exec(r[5]);
  return { kind: r[1], range: r[2], def: r[3], grbl: r[4], mark: mk ? mk[1] : "",
           meaning: mk ? r[5].slice(mk[0].length) : r[5] };
}

// is v allowed by a range such as "0..inf", "positive,0", "10..2000,0"?
function inRange(kind, range, v) {
  if (kind === "enum" || kind === "role") return range.split(",").includes(v);
  const n = Number(v);
  if (v === "" || !isFinite(n)) return false;
  if (kind === "int" && !Number.isInteger(n)) return false;
  return range.split(",").some((p) => {
    if (p === "positive") return n > 0;
    const ab = p.split("..");
    if (ab.length === 1) return n === Number(p);
    const lo = ab[0] === "-inf" ? -Infinity : Number(ab[0]);
    const hi = ab[1] === "inf" ? Infinity : Number(ab[1]);
    return n >= lo && n <= hi;
  });
}

// "10..2000,0" -> "10..2000 or 0", "positive" -> "> 0"
const showRange = (r) => r === "-inf..inf" ? "any number" : r.split(",").map((p) => p === "positive" ? "> 0" :
  p.replace("-inf..", "≤ ").replace(/\.\.inf$/, " and up")).join(" or ");
const MARKS = { "*": "applies at the next reset", "**": "applies at the next boot, after Save to flash" };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function ctlRender() {
  if (!ctl) return;
  const f = $("ctlfilter").value.trim().toLowerCase();
  const out = [], groups = {};
  for (const [key, val, grp] of ctl.rows) {
    const i = info(ctl.fw, key) || { kind: "", range: "", meaning: "" };
    if (f && !(key.toLowerCase().includes(f) || i.meaning.toLowerCase().includes(f))) continue;
    const name = ctl.fw === "grbl" ? "$" + key : key;
    let field;
    if (i.kind === "enum" || i.kind === "role") {
      const opts = i.range.split(",");
      if (!opts.includes(val)) opts.unshift(val);
      field = '<select data-k="' + esc(key) + '">' + opts.map((o, n) =>
        '<option' + (o === val ? " selected" : "") + ' value="' + esc(o) + '">' +
        esc(i.labels ? i.labels[n] || o : o) + "</option>").join("") + "</select>";
    } else {
      field = '<input type="text" inputmode="decimal" data-k="' + esc(key) + '" value="' + esc(val) + '">';
    }
    const st = ctl.state[key];
    const meta = [i.meaning];
    if (i.def !== undefined && i.def !== "") meta.push("default " + i.def);
    if (i.range && i.kind !== "enum" && i.kind !== "role" && i.range !== "-inf..inf") meta.push(showRange(i.range));
    if (i.grbl) meta.push(i.grbl);
    const row = '<div class="ctlrow"><span class="k">' + esc(name) +
      (i.mark ? ' <b class="mark" title="' + MARKS[i.mark] + '">' + i.mark + "</b>" : "") +
      "</span>" + field + '<span class="m">' + esc(meta.filter(Boolean).join(" · ")) + "</span>" +
      (st ? '<span class="r ' + st[0] + '">' + esc(st[1]) + "</span>" : "") + "</div>";
    const g = grp || (ctl.fw === "grbl" ? "GRBL $$" : "settings");
    (groups[g] = groups[g] || []).push(row);
  }
  for (const g in groups)
    out.push("<details" + (f || ctl.open.has(g) ? " open" : "") + ' data-g="' + esc(g) + '"><summary>' +
             esc(g) + " (" + groups[g].length + ")</summary>" + groups[g].join("") + "</details>");
  $("ctlrows").innerHTML = out.join("") || '<div class="hint">nothing matches</div>';
}

function ctlSend(el) {
  const key = el.dataset.k, v = el.value.trim();
  const row = ctl.rows.find((r) => r[0] === key);
  if (!row || v === row[1]) return;
  const i = info(ctl.fw, key);
  if (i && i.kind && !inRange(i.kind, i.range, v)) {
    el.classList.add("bad");
    ctl.state[key] = ["err", "not sent: allowed " + showRange(i.range) + (i.kind === "int" ? " (whole number)" : "")];
    ctlRender();
    return;
  }
  ctl.state[key] = ["wait", "sending " + v + "…"];
  send({ cmd: "ctl_set", key, value: v });
  ctlRender();
}

function explainErr(r) {
  const m = /^error:(\d+)/.exec(r);
  return m && codes.errors[m[1]] ? r + " — " + codes.errors[m[1]] : r;
}

function ctlHandle(m) {
  if (m.type === "ctl_settings") {
    $("ctlload").disabled = false;
    if (m.error) { $("ctlrows").textContent = explainErr(m.error); return; }
    loadSchema.then(() => {
      const open = ctl ? ctl.open : new Set();
      ctl = { fw: m.fw, rows: m.rows, state: {}, open };
      $("ctlfw").textContent = m.fw === "capy" ? "caPy · " + m.rows.length + " settings"
                                               : "GRBL · " + m.rows.length + " settings";
      $("ctlsave").style.display = m.fw === "capy" ? "" : "none";
      ctlRender();
    });
  } else if (m.type === "ctl_set" && ctl) {
    const row = ctl.rows.find((r) => r[0] === m.key);
    if (m.reply.startsWith("ok")) {
      if (row) row[1] = m.value;
      const i = info(ctl.fw, m.key);
      ctl.state[m.key] = ["ok", "set" + (i && i.mark ? " — " + MARKS[i.mark] : "") +
                          (ctl.fw === "capy" ? "; Save to flash to keep it" : " (GRBL keeps it at once)")];
    } else {
      ctl.state[m.key] = ["err", explainErr(m.reply)];
    }
    ctlRender();
  } else if (m.type === "ctl_saved") {
    $("ctlnote").textContent = m.reply.startsWith("ok") ? "Saved to flash." : explainErr(m.reply);
  }
}

$("ctlrows").addEventListener("change", (e) => { if (e.target.dataset.k) ctlSend(e.target); });
$("ctlrows").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset.k) e.target.blur();
});
$("ctlrows").addEventListener("toggle", (e) => {
  const g = e.target.dataset && e.target.dataset.g;
  if (g && ctl && !$("ctlfilter").value.trim())   // (a filter opens every group it shows)
    (e.target.open ? ctl.open.add(g) : ctl.open.delete(g));
}, true);

connect();
