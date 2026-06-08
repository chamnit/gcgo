"use strict";
const $ = (id) => document.getElementById(id);
const fileSel = $("file");
let ws, loaded = null;

function connect() {
  ws = new WebSocket("ws://" + location.host + "/ws");
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => { setState("offline"); setTimeout(connect, 1000); };
}

function cmd(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function setState(s) {
  const el = $("state");
  el.textContent = s;
  el.className = (s || "").split(":")[0];
}

function fmt3(a, u) {
  return a.map((v) => v.toFixed(3).padStart(9)).join(" ") + " " + (u || "");
}

function handle(m) {
  if (m.type === "status") {
    setState(m.state || "—");
    $("wpos").textContent = fmt3(m.wpos, m.units);
    $("mpos").textContent = fmt3(m.mpos, m.units);
    $("feed").textContent = m.feed.toFixed(0) + " " + (m.units === "in" ? "in/min" : "mm/min");
    $("spindle").textContent = m.spindle.toFixed(0) + " RPM";
    $("ov").textContent = "F" + m.ov[0] + "% R" + m.ov[1] + "% S" + m.ov[2] + "%";
    $("pins").textContent = m.pins || "-";
    const st = m.stream;
    $("prog").value = st.progress;
    $("progtxt").textContent = st.state === "running"
      ? st.sent + " sent  " + (st.progress * 100).toFixed(0) + "%"
      : st.state + (m.loaded ? "  (" + m.loaded + ")" : "");
    if (m.loaded && m.loaded !== loaded) { loaded = m.loaded; selectFile(loaded); }
  } else if (m.type === "files") {
    fileSel.innerHTML = "";
    m.files.forEach((f) => {
      const o = document.createElement("option");
      o.value = o.textContent = f;
      fileSel.appendChild(o);
    });
    if (loaded) selectFile(loaded);
  } else if (m.type === "msg") {
    log(m.line);
  }
}

function selectFile(f) {
  for (const o of fileSel.options) if (o.value === f) o.selected = true;
}

function log(line) {
  const el = $("log");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
}

connect();
