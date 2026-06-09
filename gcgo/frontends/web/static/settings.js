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
  if (m.type === "status") {
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

connect();
