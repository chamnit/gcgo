"use strict";
const $ = (id) => document.getElementById(id);
let ws, curdir = "", sel = null, loaded = null;
let step = 1, running = false;

function connect() {
  ws = new WebSocket("ws://" + location.host + "/ws");
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onopen = () => log("connected", "sys");
  ws.onclose = () => { setState("offline"); setTimeout(connect, 1000); };
}
function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

function setState(s) {
  const el = $("state");
  el.textContent = s;
  el.className = "badge " + (s || "").split(":")[0];
}
const join = (d, n) => (d ? d + "/" + n : n);

function handle(m) {
  if (m.type === "status") {
    setState(m.state || "—");
    const u = m.units;
    $("unitlbl").textContent = u;
    setAxis("x", m.wpos[0], m.mpos[0]);
    setAxis("y", m.wpos[1], m.mpos[1]);
    setAxis("z", m.wpos[2], m.mpos[2]);
    $("feed").textContent = m.feed.toFixed(0) + (u === "in" ? " in/m" : " mm/m");
    $("spindle").textContent = m.spindle.toFixed(0) + " rpm";
    $("ov").textContent = m.ov[0] + "/" + m.ov[1] + "/" + m.ov[2];
    $("pins").textContent = m.pins || "-";
    const st = m.stream;
    running = st.state === "running";
    $("prog").value = st.progress;
    $("progtxt").textContent = running
      ? st.sent + " sent · " + (st.progress * 100).toFixed(0) + "%"
      : st.state + (m.loaded ? "  (" + m.loaded + ")" : "");
    loaded = m.loaded;
  } else if (m.type === "files") {
    curdir = m.dir || "";
    renderFiles(m.entries || []);
  } else if (m.type === "settings") {
    segSet("unitseg", "units", m.units);
    segSet("afterseg", "after", m.after);
    if (document.activeElement !== $("rateinp")) $("rateinp").value = m.rate;
  } else if (m.type === "msg") {
    const cls = /^(error|ALARM|\[MSG:.*rror)/i.test(m.line) ? "err" : "rx";
    log(m.line, cls);
  }
}

function setAxis(a, w, mc) {
  $("w" + a).textContent = w.toFixed(3);
  $("m" + a).textContent = "M " + mc.toFixed(3);
}

const ICON_DIR = '<svg class="ic" viewBox="0 0 16 16" fill="currentColor"><path d="M1.5 3.5A1.5 1.5 0 0 1 3 2h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 5v6.5A1.5 1.5 0 0 1 13 13H3a1.5 1.5 0 0 1-1.5-1.5z"/></svg>';
const ICON_FILE = '<svg class="ic" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><path d="M4 1.5h5L13 5v9.5H4z"/><path d="M9 1.5V5h4"/></svg>';

function renderFiles(entries) {
  $("cwd").textContent = "/" + curdir;
  const box = $("files");
  box.innerHTML = "";
  if (!entries.length) {
    const e = document.createElement("div");
    e.className = "entry"; e.style.color = "var(--mut)"; e.textContent = "(empty)";
    box.appendChild(e); return;
  }
  for (const e of entries) {
    const row = document.createElement("div");
    row.className = "entry" + (e.d ? " dir" : "");
    row.innerHTML = (e.d ? ICON_DIR : ICON_FILE) + "<span></span>";
    row.lastChild.textContent = e.n;
    const rel = join(curdir, e.n);
    if (e.d) {
      row.onclick = () => send({ cmd: "files", dir: rel });
    } else {
      if (rel === sel) row.classList.add("sel");
      row.onclick = () => {
        sel = rel;
        $("sel").textContent = rel;
        $("sel").style.color = "var(--txt)";
        for (const el of box.querySelectorAll(".sel")) el.classList.remove("sel");
        row.classList.add("sel");
      };
    }
    box.appendChild(row);
  }
}

// --- jog & zero ---
function jog(...pairs) {
  let g = "$J=G91 G21";
  for (let i = 0; i < pairs.length; i += 2) {
    const axis = pairs[i], sign = pairs[i + 1];
    g += " " + axis + (sign === "-" ? "-" : "") + step;
  }
  g += " F" + (parseFloat($("jogfeed").value) || 1000);
  mdi(g);
}
function zero(...axes) {
  mdi("G10 L20 P0 " + axes.map((a) => a + "0").join(" "));
}

// segmented controls (jog step, units, after-run)
document.addEventListener("click", (e) => {
  const b = e.target.closest(".seg button");
  if (!b) return;
  for (const x of b.parentElement.children) x.classList.toggle("on", x === b);
  if (b.dataset.step) step = parseFloat(b.dataset.step);
  else if (b.dataset.units) send({ cmd: "units", value: b.dataset.units });
  else if (b.dataset.after) send({ cmd: "after", value: b.dataset.after });
});
function segSet(id, attr, val) {
  const seg = $(id);
  if (!seg) return;
  for (const x of seg.children) x.classList.toggle("on", x.dataset[attr] === String(val));
}

// --- files ---
function upDir() {
  const parent = curdir.includes("/") ? curdir.slice(0, curdir.lastIndexOf("/")) : "";
  send({ cmd: "files", dir: parent });
}
function refresh() { send({ cmd: "files", dir: curdir }); }
function runSel() { if (sel) send({ cmd: "run", file: sel }); }
function dlSel() { if (sel) window.location = "/dl?file=" + encodeURIComponent(sel); }
function delSel() {
  if (sel && confirm("Delete " + sel + "?")) {
    send({ cmd: "delete", file: sel }); sel = null;
    $("sel").textContent = "no file selected"; $("sel").style.color = "var(--mut)";
  }
}

async function upload(input) {
  for (const f of input.files) {
    log("uploading " + f.name + " …", "sys");
    try {
      const url = "/upload?name=" + encodeURIComponent(f.name) +
                  "&dir=" + encodeURIComponent(curdir);
      const r = await fetch(url, { method: "POST", body: f });
      if (!r.ok) log("upload failed: " + (await r.text()), "err");
    } catch (err) { log("upload error: " + err, "err"); }
  }
  input.value = "";
  refresh();
}

// --- console ---
function mdi(line) {
  line = (line || "").trim();
  if (!line) return;
  log("> " + line, "tx");
  send({ cmd: "mdi", line });
}
function log(line, cls) {
  const el = $("log");
  const span = document.createElement("span");
  span.className = cls || "rx";
  span.textContent = line + "\n";
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}

connect();
