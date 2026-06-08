"use strict";
const $ = (id) => document.getElementById(id);
let ws, curdir = "", sel = null, loaded = null;

function connect() {
  ws = new WebSocket("ws://" + location.host + "/ws");
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => { setState("offline"); setTimeout(connect, 1000); };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function setState(s) {
  const el = $("state");
  el.textContent = s;
  el.className = (s || "").split(":")[0];
}
const join = (d, n) => (d ? d + "/" + n : n);

function handle(m) {
  if (m.type === "status") {
    setState(m.state || "—");
    const u = m.units;
    $("wpos").textContent = fmt(m.wpos, u);
    $("mpos").textContent = fmt(m.mpos, u);
    $("feed").textContent = m.feed.toFixed(0) + " " + (u === "in" ? "in/min" : "mm/min");
    $("spindle").textContent = m.spindle.toFixed(0) + " RPM";
    $("ov").textContent = "F" + m.ov[0] + "% R" + m.ov[1] + "% S" + m.ov[2] + "%";
    $("pins").textContent = m.pins || "-";
    const st = m.stream;
    $("prog").value = st.progress;
    $("progtxt").textContent = st.state === "running"
      ? st.sent + " sent  " + (st.progress * 100).toFixed(0) + "%"
      : st.state + (m.loaded ? "  (" + m.loaded + ")" : "");
    loaded = m.loaded;
  } else if (m.type === "files") {
    curdir = m.dir || "";
    renderFiles(m.entries || []);
  } else if (m.type === "msg") {
    log(m.line);
  }
}

function fmt(a, u) { return a.map((v) => v.toFixed(3).padStart(9)).join(" ") + " " + (u || ""); }

function renderFiles(entries) {
  $("cwd").textContent = "/" + curdir;
  const box = $("files");
  box.innerHTML = "";
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "entry" + (e.d ? " dir" : "");
    row.textContent = (e.d ? "📁 " : "📄 ") + e.n;
    const rel = join(curdir, e.n);
    if (e.d) {
      row.onclick = () => send({ cmd: "files", dir: rel });
    } else {
      if (rel === sel) row.classList.add("sel");
      row.onclick = () => {
        sel = rel;
        $("sel").textContent = rel;
        for (const el of box.querySelectorAll(".sel")) el.classList.remove("sel");
        row.classList.add("sel");
      };
    }
    box.appendChild(row);
  }
}

function upDir() {
  const parent = curdir.includes("/") ? curdir.slice(0, curdir.lastIndexOf("/")) : "";
  send({ cmd: "files", dir: parent });
}
function refresh() { send({ cmd: "files", dir: curdir }); }
function runSel() { if (sel) send({ cmd: "run", file: sel }); }
function dlSel() { if (sel) window.location = "/dl?file=" + encodeURIComponent(sel); }
function delSel() {
  if (sel && confirm("Delete " + sel + "?")) { send({ cmd: "delete", file: sel }); sel = null; $("sel").textContent = "no file selected"; }
}

async function upload(input) {
  for (const f of input.files) {
    log("uploading " + f.name + " …");
    try {
      const url = "/upload?name=" + encodeURIComponent(f.name) +
                  "&dir=" + encodeURIComponent(curdir);
      const r = await fetch(url, { method: "POST", body: f });
      if (!r.ok) log("upload failed: " + (await r.text()));
    } catch (err) { log("upload error: " + err); }
  }
  input.value = "";
  refresh();
}

function log(line) {
  const el = $("log");
  el.textContent += line + "\n";
  el.scrollTop = el.scrollHeight;
}

connect();
