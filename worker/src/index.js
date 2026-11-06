// Share to Notes inbox: a private queue for links you share, plus a place to read the finished notes.
//
// Flow: a share-sheet shortcut or this web page adds a link -> this Worker starts a GitHub Actions job ->
// the job makes the note and saves it in your notes repo (and Notion, if set up).
//
// Every /api/* call needs the header:  Authorization: Bearer <INBOX_TOKEN>
// Secrets: INBOX_TOKEN, GITHUB_TOKEN.  Vars: GITHUB_REPO ("username/share-to-notes"), WIDGET_KEY, APP_NAME.

import { handleWidget, saveUsage } from "./widget.js";

const CLAIM_TIMEOUT_MINUTES = 45; // a link stuck in "processing" this long goes back to the queue
const MAX_ATTEMPTS = 3;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
      return new Response(PAGE_HTML.replaceAll("{{APP_NAME}}", escapeHtml(env.APP_NAME || "Share to Notes")), {
        headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
      });
    }

    if (request.method === "GET" && (url.pathname === "/widget" || url.pathname === "/widget/data")) {
      return await handleWidget(url, env);
    }

    if (!url.pathname.startsWith("/api/")) {
      return json({ error: "Not found" }, 404);
    }

    if (!authorised(request, env)) {
      return json({ error: "Unauthorised" }, 401);
    }

    try {
      const route = `${request.method} ${url.pathname}`;
      switch (route) {
        case "POST /api/add": return await addLink(request, env, ctx);
        case "GET /api/pending": return await claimPending(url, env);
        case "POST /api/done": return await markDone(request, env);
        case "POST /api/release": return await releaseLink(request, env);
        case "POST /api/retry": return await retryLink(request, env, ctx);
        case "GET /api/list": return await listRecent(url, env);
        case "GET /api/note": return await getNote(url, env);
        case "POST /api/usage": return await saveUsage(request, env);
        case "POST /api/run": {
          const started = await triggerGitHub(env, "manual");
          return json({ ok: started, message: started ? "Processing started" : "Couldn't start GitHub. Check GITHUB_TOKEN and GITHUB_REPO." });
        }
        default: return json({ error: "Not found" }, 404);
      }
    } catch (err) {
      return json({ error: String(err && err.message ? err.message : err) }, 500);
    }
  },

  // Safety net: every few hours, start a job if anything is still waiting.
  async scheduled(event, env, ctx) {
    const row = await env.DB.prepare(
      `SELECT COUNT(*) AS n FROM links WHERE status IN ('pending') AND attempts < ?1`
    ).bind(MAX_ATTEMPTS).first();
    if (row && row.n > 0) ctx.waitUntil(triggerGitHub(env, "cron"));
  },
};

function authorised(request, env) {
  const header = request.headers.get("authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!env.INBOX_TOKEN || !token) return false;
  return timingSafeEqual(token, env.INBOX_TOKEN);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

// Starts the GitHub Actions workflow ("repository_dispatch" event).
export async function triggerGitHub(env, reason) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) return false;
  const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "user-agent": "share-to-notes-worker",
      "x-github-api-version": "2022-11-28",
      "content-type": "application/json",
    },
    body: JSON.stringify({ event_type: "new-link", client_payload: { reason } }),
  });
  return res.status === 204;
}

// Tracking parameters removed from every link, so the same post is never queued twice.
// Add your own with the STRIP_PARAMS var in wrangler.toml (comma separated).
const TRACKING_PARAMS = ["fbclid", "gclid", "si", "ref", "ref_src", "feature", "mibextid", "share_id"];

// Pulls the first http(s) link out of whatever the share sheet sends and tidies it.
// The rules match normalise_url() in pipeline/notes_pipeline.py, so both sides make the same key.
export async function normaliseLink(input, extraParams = "") {
  const strip = new Set([...TRACKING_PARAMS, ...String(extraParams).split(",").map((p) => p.trim().toLowerCase()).filter(Boolean)]);
  if (!input || typeof input !== "string") return null;
  const match = input.match(/https?:\/\/[^\s"'<>]+/i);
  if (!match) return null;
  let parsed;
  try {
    parsed = new URL(match[0]);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
  let host = parsed.hostname.toLowerCase();
  for (const prefix of ["www.", "m."]) if (host.startsWith(prefix)) host = host.slice(prefix.length);
  const params = [...parsed.searchParams.entries()]
    .filter(([k]) => !k.toLowerCase().startsWith("utm_") && !strip.has(k.toLowerCase()))
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  const path = parsed.pathname.replace(/\/+$/, "");
  const query = new URLSearchParams(params).toString();
  const url = `https://${host}${path}${query ? "?" + query : ""}`;
  const digest = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(url));
  const key = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 12);
  return { url, shortcode: key, kind: host };
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function readBody(request) {
  const type = request.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    const body = await request.json().catch(() => ({}));
    return body || {};
  }
  if (type.includes("form")) {
    const form = await request.formData();
    return Object.fromEntries(form.entries());
  }
  const text = await request.text();
  return { url: text };
}

async function addLink(request, env, ctx) {
  const body = await readBody(request);
  const raw = String(body.url || body.text || body.input || "");
  const link = await normaliseLink(raw, env.STRIP_PARAMS);
  if (!link) {
    return json({ ok: false, error: "That doesn't look like a link." }, 400);
  }
  const now = new Date().toISOString();
  const result = await env.DB.prepare(
    `INSERT INTO links (url, shortcode, kind, status, added_at, attempts)
     VALUES (?1, ?2, ?3, 'pending', ?4, 0)
     ON CONFLICT(shortcode) DO NOTHING`
  )
    .bind(link.url, link.shortcode, link.kind, now)
    .run();

  const added = (result.meta && result.meta.changes) > 0;
  // Bulk imports pass start=false and trigger once at the end.
  if (added && body.start !== false) ctx.waitUntil(triggerGitHub(env, "new-link"));
  return json({
    ok: true,
    added,
    message: added ? "Saved to your inbox" : "Already in your inbox",
    url: link.url,
  });
}

async function claimPending(url, env) {
  const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "5", 10) || 5, 1), 20);
  const now = new Date();
  const stale = new Date(now.getTime() - CLAIM_TIMEOUT_MINUTES * 60 * 1000).toISOString();

  // Put back anything a job started but never finished.
  await env.DB.prepare(
    `UPDATE links SET status = 'pending' WHERE status = 'processing' AND claimed_at < ?1`
  ).bind(stale).run();

  const { results } = await env.DB.prepare(
    `SELECT id, url, shortcode, kind, added_at, attempts FROM links
     WHERE status = 'pending' AND attempts < ?1
     ORDER BY added_at ASC LIMIT ?2`
  ).bind(MAX_ATTEMPTS, limit).all();

  if (results.length) {
    const stamp = now.toISOString();
    const stmt = env.DB.prepare(
      `UPDATE links SET status = 'processing', claimed_at = ?1, attempts = attempts + 1 WHERE id = ?2`
    );
    await env.DB.batch(results.map((r) => stmt.bind(stamp, r.id)));
  }
  return json({ ok: true, items: results });
}

async function markDone(request, env) {
  const body = await readBody(request);
  const id = parseInt(body.id, 10);
  const status = body.status === "failed" ? "failed" : "done";
  if (!id) return json({ ok: false, error: "Missing id" }, 400);

  const row = await env.DB.prepare(`SELECT attempts FROM links WHERE id = ?1`).bind(id).first();
  if (!row) return json({ ok: false, error: "Unknown id" }, 404);
  // A failed link goes back to the queue until it has used up its attempts.
  const finalStatus = status === "failed" && row.attempts < MAX_ATTEMPTS ? "pending" : status;

  await env.DB.prepare(
    `UPDATE links SET status = ?1, done_at = ?2, note = ?3, title = COALESCE(?4, title),
       note_md = COALESCE(?5, note_md), note_path = COALESCE(?6, note_path), topic = COALESCE(?7, topic)
     WHERE id = ?8`
  ).bind(
    finalStatus,
    new Date().toISOString(),
    String(body.note || "").slice(0, 500),
    body.title || null,
    body.note_md ? String(body.note_md).slice(0, 200000) : null,
    body.note_path || null,
    body.topic || null,
    id
  ).run();
  return json({ ok: true, status: finalStatus });
}

// Put a link back without using up an attempt (e.g. the daily AI allowance ran out).
async function releaseLink(request, env) {
  const body = await readBody(request);
  const id = parseInt(body.id, 10);
  if (!id) return json({ ok: false, error: "Missing id" }, 400);
  await env.DB.prepare(
    `UPDATE links SET status = 'pending', attempts = MAX(attempts - 1, 0), note = ?1 WHERE id = ?2`
  ).bind(String(body.note || "").slice(0, 500), id).run();
  return json({ ok: true });
}

async function retryLink(request, env, ctx) {
  const body = await readBody(request);
  const id = parseInt(body.id, 10);
  if (!id) return json({ ok: false, error: "Missing id" }, 400);
  await env.DB.prepare(`UPDATE links SET status = 'pending', attempts = 0, note = NULL WHERE id = ?1`).bind(id).run();
  ctx.waitUntil(triggerGitHub(env, "retry"));
  return json({ ok: true });
}

async function listRecent(url, env) {
  const q = (url.searchParams.get("q") || "").trim();
  let stmt;
  if (q) {
    const like = `%${q.replace(/[%_]/g, "")}%`;
    stmt = env.DB.prepare(
      `SELECT id, url, kind, status, added_at, done_at, note, title, topic, note_path,
              (note_md IS NOT NULL) AS has_note
       FROM links WHERE title LIKE ?1 OR note_md LIKE ?1 OR topic LIKE ?1
       ORDER BY added_at DESC LIMIT 100`
    ).bind(like);
  } else {
    stmt = env.DB.prepare(
      `SELECT id, url, kind, status, added_at, done_at, note, title, topic, note_path,
              (note_md IS NOT NULL) AS has_note
       FROM links ORDER BY added_at DESC LIMIT 100`
    );
  }
  const { results } = await stmt.all();
  const counts = await env.DB.prepare(`SELECT status, COUNT(*) AS n FROM links GROUP BY status`).all();
  return json({ ok: true, items: results, counts: counts.results, repo: env.GITHUB_REPO || "" });
}

async function getNote(url, env) {
  const id = parseInt(url.searchParams.get("id") || "", 10);
  if (!id) return json({ ok: false, error: "Missing id" }, 400);
  const row = await env.DB.prepare(
    `SELECT id, url, title, note_md, note_path FROM links WHERE id = ?1`
  ).bind(id).first();
  if (!row) return json({ ok: false, error: "Not found" }, 404);
  return json({ ok: true, ...row, repo: env.GITHUB_REPO || "" });
}

const PAGE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{{APP_NAME}}</title>
<style>
  :root { --bg:#f6f5f2; --card:#fff; --ink:#1b1b1b; --muted:#6b6b6b; --line:#e4e2dc; --accent:#1f5f4a; --bad:#a33; --soft:#eef3f0; }
  @media (prefers-color-scheme: dark) { :root { --bg:#131313; --card:#1d1d1d; --ink:#eee; --muted:#9a9a9a; --line:#2c2c2c; --accent:#6fc3a1; --bad:#e77; --soft:#1f2a25; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.55 -apple-system, system-ui, sans-serif; }
  main { max-width:720px; margin:0 auto; padding:28px 16px 64px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p.sub { color:var(--muted); margin:0 0 20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  textarea, input { width:100%; font:inherit; color:inherit; background:transparent; border:1px solid var(--line); border-radius:8px; padding:10px; }
  textarea { min-height:80px; resize:vertical; }
  button { font:inherit; border:0; border-radius:8px; padding:9px 14px; background:var(--accent); color:#fff; cursor:pointer; }
  button.link { background:none; color:var(--accent); padding:0; }
  .row { display:flex; gap:8px; margin-top:10px; align-items:center; flex-wrap:wrap; }
  #msg { color:var(--muted); font-size:14px; }
  ul { list-style:none; padding:0; margin:0; }
  li { padding:12px 0; border-top:1px solid var(--line); display:flex; gap:10px; justify-content:space-between; align-items:flex-start; }
  li:first-child { border-top:0; }
  .title { font-weight:600; cursor:pointer; color:var(--ink); background:none; padding:0; text-align:left; }
  .meta { color:var(--muted); font-size:13px; word-break:break-all; }
  .meta a { color:inherit; }
  .pill { font-size:12px; padding:2px 8px; border-radius:99px; border:1px solid var(--line); white-space:nowrap; }
  .pill.done { background:var(--soft); }
  .failed { color:var(--bad); font-size:13px; }
  .counts { color:var(--muted); font-size:14px; }
  #login, #app, #reader { display:none; }
  #reader h1 { font-size:21px; margin:6px 0 10px; }
  #reader h2 { font-size:17px; margin:22px 0 6px; }
  #reader ul { list-style:disc; padding-left:22px; }
  #reader li { display:list-item; border:0; padding:2px 0; }
  #reader details { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:8px 0; }
  #reader summary { cursor:pointer; color:var(--muted); }
  #reader .fm { color:var(--muted); font-size:13px; }
  .tag { display:inline-block; font-size:12px; background:var(--soft); border-radius:99px; padding:1px 8px; margin:2px 4px 2px 0; }
</style>
</head>
<body>
<main>
  <h1>{{APP_NAME}}</h1>
  <p class="sub">Share links here. Notes appear a few minutes later.</p>

  <div class="card" id="login">
    <label for="token">Inbox key</label>
    <input id="token" type="password" autocomplete="current-password">
    <div class="row"><button id="saveToken">Unlock</button></div>
  </div>

  <div id="app">
    <div class="card">
      <label for="links">Add links (one per line)</label>
      <textarea id="links" placeholder="https://..."></textarea>
      <div class="row"><button id="add">Add to inbox</button><button class="link" id="run">Process now</button><span id="msg"></span></div>
    </div>
    <div class="card">
      <input id="search" type="search" placeholder="Search your notes">
      <div class="row counts" id="counts"></div>
      <ul id="list"></ul>
      <div class="row"><button class="link" id="logout">Lock this device</button></div>
    </div>
  </div>

  <div class="card" id="reader">
    <button class="link" id="back">&larr; Back</button>
    <div id="noteBody"></div>
  </div>
</main>
<script>
  let memToken = "";
  const store = {
    get() { try { return localStorage.getItem("inboxToken") || memToken; } catch (e) { return memToken; } },
    set(v) { memToken = v; try { localStorage.setItem("inboxToken", v); } catch (e) {} },
    clear() { memToken = ""; try { localStorage.removeItem("inboxToken"); } catch (e) {} },
  };
  const $ = (id) => document.getElementById(id);

  async function api(path, opts) {
    opts = opts || {};
    const res = await fetch(path, Object.assign({}, opts, { headers: { "authorization": "Bearer " + store.get(), "content-type": "application/json" } }));
    if (res.status === 401) { store.clear(); show(); throw new Error("Wrong key"); }
    return res.json();
  }

  function view(name) {
    const has = !!store.get();
    $("login").style.display = !has ? "block" : "none";
    $("app").style.display = has && name === "app" ? "block" : "none";
    $("reader").style.display = has && name === "reader" ? "block" : "none";
  }
  function show() { view("app"); if (store.get()) refresh(); }

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); }

  let searchTimer;
  async function refresh() {
    const q = $("search").value.trim();
    const data = await api("/api/list" + (q ? "?q=" + encodeURIComponent(q) : ""));
    const c = {}; (data.counts || []).forEach(function (r) { c[r.status] = r.n; });
    $("counts").textContent = (c.pending || 0) + " waiting · " + (c.processing || 0) + " processing · " + (c.done || 0) + " done · " + (c.failed || 0) + " failed";
    $("list").innerHTML = (data.items || []).map(function (r) {
      const name = r.title || r.url;
      const head = r.has_note ? '<button class="title" data-open="' + r.id + '">' + esc(name) + '</button>' : '<div class="title">' + esc(name) + '</div>';
      const meta = '<div class="meta">' + (r.topic ? esc(r.topic) + ' · ' : '') + '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">original</a></div>';
      const fail = r.status === "failed" ? '<div class="failed">' + esc(r.note) + ' <button class="link" data-retry="' + r.id + '">Retry</button></div>'
                 : (r.status === "pending" && r.note ? '<div class="meta">' + esc(r.note) + '</div>' : '');
      return '<li><div>' + head + meta + fail + '</div><span class="pill ' + esc(r.status) + '">' + esc(r.status) + '</span></li>';
    }).join("") || '<li><div class="meta">Nothing here yet.</div></li>';
  }

  // Small Markdown renderer, enough for these notes.
  function inline(s) {
    return esc(s)
      .replace(/\\*\\*(.+?)\\*\\*/g, "<strong>$1</strong>")
      .replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
  }
  function renderNote(md) {
    let out = "", list = false, quote = null, fm = [];
    const lines = md.replace(/\\r/g, "").split("\\n");
    let i = 0;
    if (lines[0] === "---") { i = 1; while (i < lines.length && lines[i] !== "---") { fm.push(lines[i]); i++; } i++; }
    const tags = fm.filter(function (l) { return /^\\s+- /.test(l); }).map(function (l) { return l.replace(/^\\s+- /, ""); });
    const meta = fm.filter(function (l) { return /^(creator|posted|processed|type|topic):/.test(l); }).map(function (l) { return esc(l.replace(/"/g, "")); });
    if (meta.length) out += '<div class="fm">' + meta.join(" · ") + '</div>';
    if (tags.length) out += '<div>' + tags.map(function (t) { return '<span class="tag">#' + esc(t) + '</span>'; }).join("") + '</div>';
    function closeList() { if (list) { out += "</ul>"; list = false; } }
    function closeQuote() { if (quote !== null) { out += "<details><summary>" + esc(quote.title) + "</summary><p>" + quote.body.join("<br>") + "</p></details>"; quote = null; } }
    for (; i < lines.length; i++) {
      const l = lines[i];
      const callout = l.match(/^> \\[!\\w+\\]-? (.*)$/);
      if (callout) { closeList(); closeQuote(); quote = { title: callout[1], body: [] }; continue; }
      if (quote !== null && l.indexOf(">") === 0) { quote.body.push(inline(l.replace(/^> ?/, ""))); continue; }
      closeQuote();
      if (/^# /.test(l)) { closeList(); out += "<h1>" + inline(l.slice(2)) + "</h1>"; }
      else if (/^## /.test(l)) { closeList(); out += "<h2>" + inline(l.slice(3)) + "</h2>"; }
      else if (/^- \\[ \\] /.test(l)) { if (!list) { out += "<ul>"; list = true; } out += "<li>☐ " + inline(l.slice(6)) + "</li>"; }
      else if (/^- /.test(l)) { if (!list) { out += "<ul>"; list = true; } out += "<li>" + inline(l.slice(2)) + "</li>"; }
      else if (l.trim() === "") { closeList(); }
      else { closeList(); out += "<p>" + inline(l) + "</p>"; }
    }
    closeList(); closeQuote();
    return out;
  }

  async function openNote(id) {
    const n = await api("/api/note?id=" + id);
    let html = renderNote(n.note_md || "");
    if (n.repo && n.note_path) {
      html += '<p class="meta"><a href="https://github.com/' + esc(n.repo) + '/blob/main/' + n.note_path.split("/").map(encodeURIComponent).join("/") + '" target="_blank" rel="noopener">Open in GitHub</a></p>';
    }
    $("noteBody").innerHTML = html;
    view("reader");
    window.scrollTo(0, 0);
  }

  $("saveToken").onclick = function () { store.set($("token").value.trim()); show(); };
  $("logout").onclick = function () { store.clear(); show(); };
  $("back").onclick = function () { view("app"); };
  $("search").oninput = function () { clearTimeout(searchTimer); searchTimer = setTimeout(refresh, 250); };
  $("list").onclick = async function (e) {
    const open = e.target.getAttribute("data-open");
    if (open) return openNote(open);
    const id = e.target.getAttribute("data-retry");
    if (id) { await api("/api/retry", { method: "POST", body: JSON.stringify({ id: id }) }); refresh(); }
  };
  $("run").onclick = async function () { const r = await api("/api/run", { method: "POST" }); $("msg").textContent = r.message; };
  $("add").onclick = async function () {
    const lines = $("links").value.split(/\\s+/).filter(Boolean);
    let added = 0, dupes = 0, bad = 0;
    for (const line of lines) {
      const r = await api("/api/add", { method: "POST", body: JSON.stringify({ url: line }) });
      if (!r.ok) bad++; else if (r.added) added++; else dupes++;
    }
    $("msg").textContent = added + " added, " + dupes + " already there" + (bad ? ", " + bad + " not links" : "") + ".";
    $("links").value = "";
    refresh();
  };
  show();
  setInterval(function () { if ($("app").style.display === "block" && !$("search").value) refresh(); }, 20000);
</script>
</body>
</html>`;
