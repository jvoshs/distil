// Allowance card. Embed it in Notion (or anywhere) to see how much free usage is left.
// GET /widget?k=WIDGET_KEY         -> the card (Notion embeds this)
// GET /widget/data?k=WIDGET_KEY    -> the numbers as JSON (the card refreshes itself)
// POST /api/usage                  -> the pipeline saves its latest usage (needs INBOX_TOKEN)
//
// Only counts are shown here. No links, titles or notes.

export async function ensureKv(env) {
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)").run();
}

export async function saveUsage(request, env) {
  await ensureKv(env);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object") {
    return new Response(JSON.stringify({ ok: false, error: "Bad body" }), { status: 400 });
  }
  await env.DB.prepare("INSERT INTO kv (k, v) VALUES ('usage', ?1) ON CONFLICT(k) DO UPDATE SET v = excluded.v")
    .bind(JSON.stringify(body).slice(0, 5000)).run();
  return new Response(JSON.stringify({ ok: true }), { headers: { "content-type": "application/json" } });
}

async function widgetData(env) {
  await ensureKv(env);
  const row = await env.DB.prepare("SELECT v FROM kv WHERE k = 'usage'").first();
  const counts = await env.DB.prepare("SELECT status, COUNT(*) AS n FROM links GROUP BY status").all();
  const queue = Object.fromEntries((counts.results || []).map((r) => [r.status, r.n]));
  return { usage: row ? JSON.parse(row.v) : null, queue };
}

function keyOk(url, env) {
  const k = url.searchParams.get("k") || "";
  return !!env.WIDGET_KEY && k === env.WIDGET_KEY;
}

export async function handleWidget(url, env) {
  if (!keyOk(url, env)) return new Response("Not found", { status: 404 });
  const data = await widgetData(env);
  const headers = { "cache-control": "no-store", "x-robots-tag": "noindex" };
  if (url.pathname === "/widget/data") {
    return new Response(JSON.stringify(data), { headers: { ...headers, "content-type": "application/json" } });
  }
  const config = { tz: env.WIDGET_TZ || "UTC", noun: env.ITEM_NOUN || "posts", title: env.APP_NAME || "Share to Notes" };
  const safe = (o) => JSON.stringify(o).replace(/</g, "\\u003c");
  const html = WIDGET_HTML.replace("__DATA__", safe(data)).replace("__CONFIG__", safe(config));
  return new Response(html, { headers: { ...headers, "content-type": "text/html; charset=utf-8" } });
}

const WIDGET_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Allowance</title>
<style>
  :root {
    --bg: transparent; --card: #ffffff; --card2: #f7f6f3; --ink: #1f1f1f; --muted: #787774;
    --line: #e9e9e7; --accent: #2f9e6f; --accent2: #7bd3a8; --warn: #d9730d; --bad: #e03e3e;
    --shadow: 0 1px 2px rgba(15,15,15,.06), 0 8px 24px rgba(15,15,15,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root { --card: #252525; --card2: #2f2f2f; --ink: #ececec; --muted: #9b9a97; --line: #373737;
      --accent: #4dc792; --accent2: #2c7a57; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35); }
  }
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; background: var(--bg); }
  body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif; color: var(--ink);
    display: flex; align-items: center; justify-content: center; padding: 10px 14px; -webkit-font-smoothing: antialiased; }
  .scene { width: 100%; max-width: 760px; height: 216px; perspective: 1400px; cursor: pointer; }
  .card { position: relative; width: 100%; height: 100%; transform-style: preserve-3d;
    transition: transform .9s cubic-bezier(.2,.75,.2,1); }
  .card.flipped { transform: rotateY(180deg); }
  .face { position: absolute; inset: 0; border-radius: 18px; background: var(--card); box-shadow: var(--shadow);
    border: 1px solid var(--line); backface-visibility: hidden; -webkit-backface-visibility: hidden;
    padding: 20px 22px; display: flex; gap: 22px; overflow: hidden; }
  .back { transform: rotateY(180deg); flex-direction: column; gap: 12px; }
  .face::before { content: ""; position: absolute; inset: -40% -10% auto auto; width: 280px; height: 280px;
    border-radius: 50%; background: radial-gradient(circle, color-mix(in srgb, var(--accent) 16%, transparent), transparent 70%);
    pointer-events: none; }
  .ring { flex: 0 0 auto; width: 160px; height: 160px; align-self: center; position: relative; }
  .ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
  .ring .track { stroke: var(--card2); }
  .ring .bar { stroke: url(#g); stroke-linecap: round; transition: stroke-dashoffset 1.4s cubic-bezier(.2,.75,.2,1); }
  .ring .mid { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
  .big { font-size: 34px; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
  .small { font-size: 11.5px; color: var(--muted); }
  .main { display: flex; flex-direction: column; justify-content: center; gap: 6px; min-width: 0; position: relative; }
  .eyebrow { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); font-weight: 600; }
  .headline { font-size: 28px; font-weight: 700; letter-spacing: -.02em; line-height: 1.15; }
  .headline em { font-style: normal; color: var(--accent); }
  .sub { color: var(--muted); }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 6px; }
  .chip { font-size: 12px; padding: 3px 9px; border-radius: 99px; background: var(--card2); border: 1px solid var(--line); white-space: nowrap; }
  .chip b { font-variant-numeric: tabular-nums; }
  .chip.bad { color: var(--bad); }
  .hint { position: absolute; right: 16px; bottom: 12px; font-size: 11px; color: var(--muted); opacity: .8; }
  .rows { display: flex; flex-direction: column; gap: 11px; position: relative; }
  .row .top { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
  .row .top span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .meter { height: 8px; border-radius: 99px; background: var(--card2); overflow: hidden; margin-top: 5px; }
  .meter i { display: block; height: 100%; width: 0; border-radius: 99px; background: linear-gradient(90deg, var(--accent2), var(--accent));
    transition: width 1.2s cubic-bezier(.2,.75,.2,1); }
  .meter i.warn { background: var(--warn); } .meter i.bad { background: var(--bad); }
  .foot { display: flex; justify-content: space-between; align-items: center; margin-top: auto; font-size: 11.5px; color: var(--muted); position: relative; }
  @media (max-width: 520px) {
    .scene { height: 250px; } .ring { width: 116px; height: 116px; } .big { font-size: 26px; } .headline { font-size: 21px; }
    .face { padding: 16px; gap: 14px; }
  }
  @media (prefers-reduced-motion: reduce) { .card, .ring .bar, .meter i { transition: none; } }
</style>
</head>
<body>
<div class="scene" id="scene" title="Tap to flip">
  <div class="card" id="card">
    <section class="face front">
      <div class="ring">
        <svg viewBox="0 0 120 120" aria-hidden="true">
          <defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="var(--accent2)"/><stop offset="1" stop-color="var(--accent)"/></linearGradient></defs>
          <circle class="track" cx="60" cy="60" r="50" fill="none" stroke-width="11"/>
          <circle class="bar" id="ringBar" cx="60" cy="60" r="50" fill="none" stroke-width="11" stroke-dasharray="314.16" stroke-dashoffset="314.16"/>
        </svg>
        <div class="mid"><div class="big" id="monthLeft">–</div><div class="small" id="ringLabel">left this month</div></div>
      </div>
      <div class="main">
        <div class="eyebrow" id="eyebrow">📟 Allowance</div>
        <div class="headline" id="headline">About <em id="perDay">–</em> a day</div>
        <div class="sub" id="perDaySub">to last until the end of the month</div>
        <div class="chips">
          <span class="chip">Today's cap <b id="todayCap">–</b></span>
          <span class="chip">Done today <b id="doneToday">–</b></span>
          <span class="chip">Waiting <b id="waiting">–</b></span>
          <span class="chip bad" id="failedChip" hidden>Failed <b id="failed">0</b></span>
        </div>
      </div>
      <div class="hint">tap to flip ↺</div>
    </section>
    <section class="face back">
      <div class="eyebrow">Free limits</div>
      <div class="rows">
        <div class="row"><div class="top"><span id="ghLabel">GitHub minutes · main limit</span><span id="ghText">–</span></div><div class="meter"><i id="ghBar"></i></div></div>
        <div class="row"><div class="top"><span id="gemLabel">Gemini notes today</span><span id="gemText">–</span></div><div class="meter"><i id="gemBar"></i></div></div>
        <div class="row"><div class="top"><span>Groq audio today</span><span id="groqText">–</span></div><div class="meter"><i id="groqBar"></i></div></div>
      </div>
      <div class="foot"><span id="updated">Waiting for the first run</span><span>tap to flip ↺</span></div>
    </section>
  </div>
</div>
<script>
  let data = __DATA__;
  const CONFIG = __CONFIG__;
  const TZ = (() => { try { new Intl.DateTimeFormat("en", { timeZone: CONFIG.tz }); return CONFIG.tz; } catch (e) { return "UTC"; } })();
  const $ = (id) => document.getElementById(id);
  const fmt = (n) => Math.max(0, Math.round(n)).toLocaleString();
  const dayIn = (tz) => new Intl.DateTimeFormat("en-CA", { timeZone: tz }).format(new Date());
  const localTime = (d) => d.toLocaleTimeString("en-GB", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true }).replace(" ", "").toLowerCase();

  function nextPacificMidnightLocal() {
    // Gemini resets at midnight US Pacific time. Find that moment in your time zone.
    const now = new Date();
    for (let h = 1; h <= 26; h++) {
      const t = new Date(now.getTime() + h * 3600e3);
      const hr = Number(new Intl.DateTimeFormat("en-US", { timeZone: "America/Los_Angeles", hour: "numeric", hourCycle: "h23" }).format(t));
      if (hr === 0) { t.setMinutes(0, 0, 0); return t.toLocaleTimeString("en-GB", { timeZone: TZ, hour: "numeric", hour12: true }).replace(" ", "").toLowerCase(); }
    }
    return "midnight US Pacific";
  }

  function setMeter(id, frac) {
    const el = $(id);
    el.style.width = Math.max(2, Math.min(100, frac * 100)) + "%";
    el.className = frac < 0.1 ? "bad" : frac < 0.25 ? "warn" : "";
  }

  function render() {
    const u = data.usage || {};
    const q = data.queue || {};
    const utcDay = new Date().toISOString().slice(0, 10);
    const utcMonth = utcDay.slice(0, 7);
    const pacDay = dayIn("America/Los_Angeles");

    const ghTotal = u.gh_total || 2000;
    const ghUsed = u.gh_month === utcMonth ? (u.gh_used || 0) : 0;
    const ghLeft = Math.max(0, ghTotal - ghUsed);
    const gemTotal = u.gemini_total || 500;
    const gemUsed = u.gemini_day === pacDay ? (u.gemini_used || 0) : 0;
    const gemLeft = Math.max(0, gemTotal - gemUsed);
    const groqTotal = u.groq_total_seconds || 28800;
    const groqUsed = u.groq_day === utcDay ? (u.groq_seconds || 0) : 0;
    const groqLeft = Math.max(0, groqTotal - groqUsed);
    const done = u.posts_day === utcDay ? (u.posts_today || 0) : 0;

    const local = new Date(new Date().toLocaleString("en-US", { timeZone: TZ }));
    const firstNext = new Date(local.getFullYear(), local.getMonth() + 1, 1);
    const daysLeft = Math.max(1, Math.ceil((firstNext - local) / 864e5));
    const perDay = Math.floor(ghLeft / daysLeft);
    const monthName = firstNext.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    const resets = nextPacificMidnightLocal();
    const noun = CONFIG.noun;

    if (u.gh_unlimited) {
      // Public repo: GitHub minutes are unlimited, so Gemini's daily cap is the limit that matters.
      $("monthLeft").textContent = fmt(gemLeft);
      $("ringLabel").textContent = "left today";
      $("ringBar").style.strokeDashoffset = String(314.16 * (1 - gemLeft / gemTotal));
      $("headline").innerHTML = 'About <em id="perDay">' + fmt(gemLeft) + "</em> " + noun + " left today";
      $("perDaySub").textContent = "Gemini's daily cap is the main limit · resets " + resets;
      $("todayCap").textContent = fmt(gemLeft);
      $("ghLabel").textContent = "GitHub minutes";
      $("ghText").textContent = "Unlimited (public repo)";
      $("gemLabel").textContent = "Gemini notes today · main limit";
      setMeter("ghBar", 1);
    } else {
      $("monthLeft").textContent = fmt(ghLeft);
      $("ringLabel").textContent = "minutes left this month";
      $("ringBar").style.strokeDashoffset = String(314.16 * (1 - ghLeft / ghTotal));
      $("headline").innerHTML = 'About <em id="perDay">' + fmt(Math.min(perDay, gemTotal)) + "</em> " + noun + " a day";
      $("perDaySub").textContent = "to last until " + monthName + " · " + daysLeft + " day" + (daysLeft === 1 ? "" : "s") + " to go";
      $("todayCap").textContent = fmt(Math.min(gemLeft, ghLeft));
      $("ghText").textContent = fmt(ghLeft) + " of " + fmt(ghTotal) + " left this month";
      setMeter("ghBar", ghLeft / ghTotal);
    }
    $("eyebrow").textContent = "📟 " + CONFIG.title + " allowance";
    $("doneToday").textContent = fmt(done);
    $("waiting").textContent = fmt((q.pending || 0) + (q.processing || 0));
    $("failed").textContent = fmt(q.failed || 0);
    $("failedChip").hidden = !(q.failed > 0);

    $("gemText").textContent = fmt(gemLeft) + " of " + fmt(gemTotal) + " left · resets " + resets;
    $("groqText").textContent = fmt(groqLeft / 60) + " of " + fmt(groqTotal / 60) + " min left";
    setMeter("gemBar", gemLeft / gemTotal);
    setMeter("groqBar", groqLeft / groqTotal);
    $("updated").textContent = u.updated ? "Last run " + localTime(new Date(u.updated)) + " · queue is live" : "Waiting for the first run";
  }

  // Flip on tap, and every 7 seconds unless you're hovering over it.
  const card = $("card"), scene = $("scene");
  let hovering = false;
  scene.addEventListener("click", () => card.classList.toggle("flipped"));
  scene.addEventListener("mouseenter", () => (hovering = true));
  scene.addEventListener("mouseleave", () => (hovering = false));
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    setInterval(() => { if (!hovering && !document.hidden) card.classList.toggle("flipped"); }, 7000);
  }

  async function refresh() {
    try {
      const res = await fetch("/widget/data" + location.search, { cache: "no-store" });
      if (res.ok) { data = await res.json(); render(); }
    } catch (e) {}
  }
  requestAnimationFrame(() => setTimeout(render, 60));
  setInterval(refresh, 60000);
</script>
</body>
</html>`;
