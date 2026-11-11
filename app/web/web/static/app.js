/* gitops-watcher web UI
   - buttons that change things are plain <form method="post">, so they work without JS
   - this file adds: dialogs, the file editor, toasts, auto-refresh, countdown, rain */

const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
const CSRF = document.querySelector('meta[name="csrf-token"]').content;

const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- toasts ---------- */
function toast(html, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.innerHTML = html;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4000);
}
$$("#toasts .toast").forEach(t => setTimeout(() => t.remove(), 4500));   // flashed from Flask

/* ---------- dialogs ---------- */
let lastFocus = null;
function openModal(sel, focusSel) {
  lastFocus = document.activeElement;
  $(sel).classList.add("open");
  if (focusSel) setTimeout(() => $(focusSel)?.focus(), 0);
}
function closeModals() {
  if ($("#files-drawer").classList.contains("open") && isDirty() &&
      !confirm("discard unsaved changes?")) return;
  $$(".backdrop.open").forEach(m => m.classList.remove("open"));
  lastFocus?.focus();
}
const anyOpen = () => !!document.querySelector(".backdrop.open");

document.addEventListener("click", e => {
  const opener = e.target.closest("[data-open]");
  if (opener) { openModal(opener.dataset.open, opener.dataset.focus); return; }
  if (e.target.closest("[data-close]")) { closeModals(); return; }
  if (e.target.classList.contains("backdrop")) { closeModals(); return; }

  const rm = e.target.closest("[data-remove]");
  if (rm) { openRemove(rm.dataset.remove); return; }

  const files = e.target.closest("[data-files]");
  if (files) { openFiles(files.dataset.files); return; }
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && anyOpen()) closeModals(); });

/* ---------- remove: type the name to enable the button ---------- */
let removing = null;
function openRemove(name) {
  removing = name;
  $$(".rm-name").forEach(el => el.textContent = name);
  $("#remove-form").action = `/repos/${encodeURIComponent(name)}/remove`;
  $("#rm-confirm").value = "";
  $("#rm-wipe").checked = false;
  $("#rm-submit").disabled = true;
  $("#rm-submit").textContent = "remove";
  openModal("#remove-modal", "#rm-confirm");
}
$("#rm-confirm")?.addEventListener("input", e => { $("#rm-submit").disabled = e.target.value !== removing; });
$("#rm-wipe")?.addEventListener("change", e => {
  $("#rm-submit").textContent = e.target.checked ? "remove + delete data" : "remove";
});

/* =========================================================
   FILE EDITOR
   ========================================================= */
let fd = { repo: null, kind: null, file: null, original: "", secret: false, revealed: false,
           list: { local: [], git: [], missing_env: [] } };

const isSecret = f => f === ".env" || f.endsWith(".env");
const api = (path, opts = {}) => fetch(path, {
  ...opts,
  headers: { "X-CSRF-Token": CSRF, "Content-Type": "application/json", ...(opts.headers || {}) },
}).then(async res => {
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
});
const repoUrl = () => `/api/repos/${encodeURIComponent(fd.repo)}/files`;

async function openFiles(name) {
  fd = { repo: name, kind: null, file: null, original: "", secret: false, revealed: false,
         list: { local: [], git: [], missing_env: [] } };
  $("#fd-repo").textContent = name;
  $("#fd-text").value = "";
  openModal("#files-drawer");
  await refreshList();
  if (fd.list.local.includes(".env")) selectFile("local", ".env");
  else if (fd.list.git.length) selectFile("git", fd.list.git[0]);
  else showEmpty();
}

async function refreshList() {
  try { fd.list = await api(repoUrl()); }
  catch (err) { toast(esc(err.message), "error"); }
  renderList();
}

function renderList() {
  const { local, git, missing_env } = fd.list;
  const btn = (kind, file) => {
    const active = fd.kind === kind && fd.file === file ? "active" : "";
    let tag = "";
    if (active && isDirty()) tag = `<span class="dirty">●</span>`;
    else if (kind === "local" && file === ".env" && missing_env.length) tag = `<span class="warnmark">⚠ ${missing_env.length} missing</span>`;
    else if (kind === "git") tag = `<span class="lock">read-only</span>`;
    return `<button class="file ${active}" data-kind="${kind}" data-file="${esc(file)}" title="${esc(file)}"><span class="fn">${esc(file)}</span>${tag}</button>`;
  };
  const hasEnv = local.includes(".env");
  const hasExample = git.includes(".env.example");
  const hasOverride = local.includes("docker-compose.override.yml");
  $("#fd-list").innerHTML = `
    <div class="file-group">LOCAL<small>yours · kept across pulls</small></div>
    ${local.map(f => btn("local", f)).join("")}
    ${!hasEnv && hasExample ? `<button class="file ghost" data-new=".env" data-from-example>+ .env from example</button>` : ""}
    ${!hasEnv && !hasExample ? `<button class="file ghost" data-new=".env">+ .env</button>` : ""}
    ${hasOverride ? "" : `<button class="file ghost" data-new="docker-compose.override.yml">+ compose override</button>`}
    <button class="file ghost" data-new="?">+ new file</button>
    <div class="file-group">FROM GIT<small>overwritten on every pull</small></div>
    ${git.length ? git.map(f => btn("git", f)).join("") : `<div class="file-group"><small>not cloned yet</small></div>`}
  `;
}

function showEmpty() {
  Object.assign(fd, { kind: null, file: null, original: "" });
  $("#fd-fname").textContent = "";
  $("#fd-badges").innerHTML = "";
  $("#fd-banner").innerHTML = `<div class="editor-loading">no files yet · create one from the list</div>`;
  $("#fd-warn").innerHTML = "";
  $("#fd-text").value = "";
  $("#fd-text").readOnly = true;
  updateGutter(); updateFoot(); renderList();
}

async function selectFile(kind, file, { reveal = false } = {}) {
  if (isDirty() && !confirm("discard unsaved changes?")) return;
  let data;
  try {
    data = await api(`${repoUrl()}/${kind}/${encodeURIComponent(file)}${reveal ? "?reveal=1" : ""}`);
  } catch (err) { toast(esc(err.message), "error"); return; }

  Object.assign(fd, { kind, file, original: data.content, secret: data.secret, revealed: !data.masked });
  $("#fd-fname").textContent = file;
  $("#fd-badges").innerHTML =
    (kind === "git" ? `<span class="badge git">from git</span>` : `<span class="badge local">local</span>`) +
    (data.secret ? ` <span class="badge secret">secrets</span>` : "");

  const ta = $("#fd-text");
  ta.value = data.content;
  ta.readOnly = kind === "git" || data.masked;

  if (kind === "git") {
    const isCompose = file === fd.list.compose_file || /^docker-compose.*\.ya?ml$|^compose.*\.ya?ml$/.test(file);
    $("#fd-banner").innerHTML = `
      <div class="banner info">
        <span>this file comes from the repo. edits here would be wiped by the next pull.
        change it in git, or override it locally.</span>
        ${isCompose ? `<button class="btn icon" data-new="docker-compose.override.yml">+ create override</button>` : ""}
      </div>`;
  } else if (data.masked) {
    $("#fd-banner").innerHTML = `
      <div class="banner secret">
        <span>values are hidden. only reveal them on a screen nobody else can see.</span>
        <button class="btn icon" id="fd-reveal">reveal + edit</button>
      </div>`;
    $("#fd-reveal").addEventListener("click", () => selectFile(kind, file, { reveal: true }).then(() => ta.focus()));
  } else if (data.secret) {
    $("#fd-banner").innerHTML = `
      <div class="banner secret"><span>values visible · editing</span>
      <button class="btn icon" id="fd-hide">hide</button></div>`;
    $("#fd-hide").addEventListener("click", () => selectFile(kind, file));
  } else {
    $("#fd-banner").innerHTML = "";
  }
  updateGutter(); updateFoot(); renderList(); renderWarn();
}

/* ---------- missing .env keys (names only, never values) ---------- */
function envKeys(text) {
  const keys = [];
  for (let line of text.split("\n")) {
    line = line.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    keys.push(line.replace(/^export\s+/, "").split("=")[0].trim());
  }
  return keys;
}
let exampleCache = { repo: null, text: null };
async function exampleText() {
  if (exampleCache.repo === fd.repo) return exampleCache.text;
  let text = null;
  if (fd.list.git.includes(".env.example")) {
    try { text = (await api(`${repoUrl()}/git/.env.example`)).content; } catch { /* ignore */ }
  }
  exampleCache = { repo: fd.repo, text };
  return text;
}
async function renderWarn() {
  const box = $("#fd-warn");
  if (fd.kind !== "local" || fd.file !== ".env") { box.innerHTML = ""; return; }
  const example = await exampleText();
  if (!example) { box.innerHTML = ""; return; }
  const have = new Set(envKeys(fd.revealed ? $("#fd-text").value : fd.original));
  const miss = envKeys(example).filter(k => !have.has(k));
  box.innerHTML = miss.length ? `
    <div class="banner warn">
      <span>missing from .env (found in .env.example): ${miss.map(k => `<code>${esc(k)}</code>`).join(", ")}</span>
      <button class="btn icon" id="fd-add-missing">+ add missing keys</button>
    </div>` : "";
  if (miss.length) $("#fd-add-missing").addEventListener("click", () => addMissing(miss, example));
}
async function addMissing(miss, example) {
  if (!fd.revealed) await selectFile("local", ".env", { reveal: true });
  const want = new Set(miss);
  const lines = example.split("\n").filter(l => {
    const t = l.trim();
    return t && !t.startsWith("#") && t.includes("=") && want.has(t.replace(/^export\s+/, "").split("=")[0].trim());
  });
  const ta = $("#fd-text");
  ta.value = ta.value.replace(/\n*$/, "\n") + `\n# added from .env.example: fill these in\n${lines.join("\n")}\n`;
  ta.focus(); ta.scrollTop = ta.scrollHeight;
  updateGutter(); updateFoot(); renderList(); renderWarn();
  toast(`added ${miss.length} key${miss.length > 1 ? "s" : ""} with example values <span class="muted">· fill in, then save</span>`);
}

/* ---------- editor state ---------- */
function isDirty() {
  if (!fd.file || fd.kind === "git" || !fd.revealed) return false;
  return $("#fd-text").value !== fd.original;
}
function updateGutter() {
  const n = $("#fd-text").value.split("\n").length;
  $("#fd-gutter").textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
}
function updateFoot() {
  const dirty = isDirty();
  $("#fd-btns").style.display = fd.kind === "local" ? "" : "none";
  $("#fd-state").className = "state" + (dirty ? " dirty" : "");
  $("#fd-state").textContent =
    !fd.file ? "" :
    fd.kind === "git" ? "read-only · edit this in the repo" :
    !fd.revealed ? "hidden · reveal to edit" :
    dirty ? "● unsaved changes" : "no changes";
  ["#fd-discard", "#fd-save", "#fd-save-deploy"].forEach(s => $(s).disabled = !dirty);
}

const ta = $("#fd-text");
if (ta) {
  ta.addEventListener("scroll", () => { $("#fd-gutter").scrollTop = ta.scrollTop; });
  ta.addEventListener("input", () => { updateGutter(); updateFoot(); renderList(); renderWarn(); });
  ta.addEventListener("keydown", e => {
    if (e.key === "Tab" && !ta.readOnly) {
      e.preventDefault();
      ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
      updateFoot();
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (isDirty()) save(false); }
  });
}

async function save(redeploy) {
  const content = $("#fd-text").value;
  try {
    const res = await api(`${repoUrl()}/${encodeURIComponent(fd.file)}`, {
      method: "POST", body: JSON.stringify({ content, redeploy }),
    });
    fd.original = content;
    fd.list.missing_env = res.missing_env;
    toast(redeploy
      ? `saved ${esc(fd.file)} <span class="muted">· redeploy queued</span>`
      : `saved ${esc(fd.file)} <span class="muted">· takes effect on next deploy</span>`, "ok");
    updateFoot(); renderList(); renderWarn();
  } catch (err) { toast(esc(err.message), "error"); }
}
$("#fd-save")?.addEventListener("click", () => save(false));
$("#fd-save-deploy")?.addEventListener("click", () => save(true));
$("#fd-discard")?.addEventListener("click", () => {
  $("#fd-text").value = fd.original;
  updateGutter(); updateFoot(); renderList(); renderWarn();
});

/* ---------- file list clicks + creating files ---------- */
const OVERRIDE_TEMPLATE = `# merged on top of the repo's compose file
# only list what you want to change, for example:
services:
  web:
    ports:
      - "8080:80"
`;
document.addEventListener("click", async e => {
  const f = e.target.closest(".file[data-kind]");
  if (f) { selectFile(f.dataset.kind, f.dataset.file); return; }

  const n = e.target.closest("[data-new]");
  if (!n) return;
  let file = n.dataset.new;
  if (file === "?") {
    file = (prompt("file name (e.g. app.env, nginx.conf)") || "").trim();
    if (!file) return;
  }
  if (!/^[A-Za-z0-9._-]{1,100}$/.test(file) || file === "." || file === "..") {
    toast("use letters, numbers, dot, dash or underscore only", "error"); return;
  }
  if (fd.list.local.includes(file)) { selectFile("local", file); return; }

  let content = "";
  if (file === "docker-compose.override.yml") content = OVERRIDE_TEMPLATE;
  if ("fromExample" in n.dataset) content = (await exampleText()) || "";

  try {
    const res = await api(`${repoUrl()}/${encodeURIComponent(file)}`, {
      method: "POST", body: JSON.stringify({ content }),
    });
    toast(`created ${esc(file)} <span class="muted">· local</span>` +
      (res.shadows_git ? `<br><span class="muted">this replaces the repo's ${esc(file)} on every deploy</span>` : ""), "ok");
    await refreshList();
    exampleCache = { repo: null, text: null };
    selectFile("local", file, { reveal: isSecret(file) });
  } catch (err) { toast(esc(err.message), "error"); }
});

/* ---------- auto-refresh (skipped while a dialog or the editor is open) ---------- */
setInterval(() => { if (!anyOpen()) location.reload(); }, 30000);

/* ---------- countdown ---------- */
(function () {
  const el = $("#countdown");
  if (!el) return;
  let secs = parseInt(el.dataset.seconds, 10) || 0;
  const show = () => {
    el.textContent = secs > 0 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}` : "any moment";
  };
  show();
  setInterval(() => { if (secs > 0) secs--; show(); }, 1000);
})();

/* ---------- rain ---------- */
(function () {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const canvas = $("#rain"), ctx = canvas.getContext("2d");
  let drops = [];
  function resize() {
    canvas.width = innerWidth * devicePixelRatio;
    canvas.height = innerHeight * devicePixelRatio;
    ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
    drops = Array.from({ length: Math.floor(innerWidth / 9) }, () => ({
      x: Math.random() * innerWidth, y: Math.random() * innerHeight,
      len: 8 + Math.random() * 14, speed: 2.5 + Math.random() * 4, alpha: 0.04 + Math.random() * 0.09,
    }));
  }
  function tick() {
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    for (const d of drops) {
      ctx.strokeStyle = `rgba(124,183,232,${d.alpha})`;
      ctx.beginPath(); ctx.moveTo(d.x, d.y); ctx.lineTo(d.x - 1, d.y + d.len); ctx.stroke();
      d.y += d.speed;
      if (d.y > innerHeight) { d.y = -d.len; d.x = Math.random() * innerWidth; }
    }
    requestAnimationFrame(tick);
  }
  addEventListener("resize", resize);
  resize(); tick();
})();
