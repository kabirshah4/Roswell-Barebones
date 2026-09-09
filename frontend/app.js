const REFRESH_MS = 5000;
const SPARKLINE_TTL_MS = 60000; // sparklines only change on the poller's
// ~5.25-minute cadence, so refetching every 5s wastes ~98% of requests.

const state = {
  activeTicker: null,
  prices: [],
  sparklines: {},
  fundamentals: {},
  news: {},
  aiEnabled: null,
  aiProvider: null,
  warming: new Set(),
};

const $ = (id) => document.getElementById(id);

const ESCAPE_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// Sparkline points are cached per ticker and only refetched when missing or
// stale (see SPARKLINE_TTL_MS above). The backend restricts stored tickers to
// [A-Z0-9.-] (backend/routes/watchlist.py), so this cache can't grow past the
// current watchlist size in practice; it is still pruned on every refresh so
// a ticker removed mid-session doesn't linger.
async function getSparkline(ticker) {
  const cached = state.sparklines[ticker];
  if (cached && Date.now() - cached.fetchedAt < SPARKLINE_TTL_MS) {
    return cached.points;
  }
  try {
    const { points } = await api(`/api/prices/${ticker}/sparkline`);
    state.sparklines[ticker] = { points, fetchedAt: Date.now() };
    return points;
  } catch (_) {
    return cached ? cached.points : [];
  }
}

function pruneSparklineCache() {
  const live = new Set(state.prices.map((p) => p.ticker));
  for (const ticker of Object.keys(state.sparklines)) {
    if (!live.has(ticker)) delete state.sparklines[ticker];
  }
}

/* --- loading states -------------------------------------------------------
 *
 * Every panel here refreshes on a poller. Painting a skeleton on each refresh
 * would make the terminal strobe every forty-five seconds, so these only fire
 * while a container has never held anything — the first paint, which is the
 * only time the user is actually waiting.
 */

function skelBar(width) {
  return `<span class="skel" style="width:${width}%"></span>`;
}

/* Rows of bars at varied widths. Uniform widths read as a loading graphic;
   ragged ones read as text about to arrive. */
function skeletonRows(count = 5, widths = [30, 55, 20]) {
  return Array.from({ length: count }, (_, i) =>
    `<li class="skel-row">${widths
      .map((w) => skelBar(Math.max(12, w - (i % 3) * 5)))
      .join("")}</li>`
  ).join("");
}

function skeletonTable(count = 5, cols = 3) {
  const widths = [70, 45, 35, 55];
  return Array.from({ length: count }, (_, i) =>
    `<tr class="skel-row">${Array.from({ length: cols }, (_, c) =>
      `<td>${skelBar(Math.max(20, widths[c % widths.length] - (i % 3) * 8))}</td>`
    ).join("")}</tr>`
  ).join("");
}

function skeletonScreen() {
  return `<div class="skel-screen">
    ${skelBar(34)}
    <div style="height:12px"></div>
    ${[0, 1].map(() => `<div class="skel-block">
      ${[60, 40, 52, 30].map((w) => `<div style="padding:3px 0">${skelBar(w)}</div>`).join("")}
    </div>`).join("")}
  </div>`;
}

/* Paint a skeleton only if this container has never been filled.
 *
 * The timeout is a safety net, not the mechanism. Every renderer is supposed
 * to paint on all of its paths, but two of them did not — one swallowed its
 * error, one returned early — and those panels pulsed forever, promising data
 * that was never coming. A skeleton that cannot resolve is worse than no
 * skeleton, so it degrades into a plain statement.
 */
const SKELETON_GIVE_UP_MS = 15000;

function showSkeleton(el, html, fallback = "Unavailable.") {
  if (!el || el.dataset.filled === "1") return;
  el.innerHTML = html;
  clearTimeout(Number(el.dataset.skelTimer));
  el.dataset.skelTimer = String(
    setTimeout(() => {
      if (el.querySelector(".skel")) {
        markFilled(el);
        el.innerHTML = `<li class="empty">${escapeHtml(fallback)}</li>`;
      }
    }, SKELETON_GIVE_UP_MS)
  );
}

function markFilled(el) {
  if (!el) return;
  el.dataset.filled = "1";
  clearTimeout(Number(el.dataset.skelTimer));
}

function sparklineSVG(points) {
  if (!points || points.length < 2) return '<span class="dim">—</span>';
  const w = 60, h = 16;
  const min = Math.min(...points), max = Math.max(...points);
  const span = max - min || 1;
  const step = w / (points.length - 1);
  const d = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${(i * step).toFixed(1)},${(h - ((p - min) / span) * h).toFixed(1)}`)
    .join(" ");
  const rising = points[points.length - 1] >= points[0];
  return `<svg width="${w}" height="${h}"><path d="${d}" fill="none"
    stroke="${rising ? "#26a65b" : "#e0483e"}" stroke-width="1.2"/></svg>`;
}

function fmt(value, digits = 2) {
  return value === null || value === undefined ? "—" : value.toFixed(digits);
}

function fmtBig(n) {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(2) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  return n.toFixed(2);
}

// Percentages arrive as fractions (0.403 == 40.3%). Rendering them raw is the
// kind of thing nobody notices until a 40% margin reads as 0.40.
function pct(v) {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;
}

function upside(f) {
  if (!f.target_mean || !f.week52_high) return "—";
  return fmt(f.target_mean);
}

const FUNDAMENTAL_ROWS = [
  ["P/E", (f) => fmt(f.pe_ratio)],
  ["FWD P/E", (f) => fmt(f.forward_pe)],
  ["MKT CAP", (f) => fmtBig(f.market_cap)],
  ["EPS", (f) => fmt(f.eps)],
  ["REVENUE", (f) => fmtBig(f.revenue)],
  ["BETA", (f) => fmt(f.beta)],
  ["DIV YIELD", (f) => (f.dividend_yield === null ? "—" : fmt(f.dividend_yield) + "%")],
  ["52W HIGH", (f) => fmt(f.week52_high)],
  ["52W LOW", (f) => fmt(f.week52_low)],
  ["SECTOR", (f) => escapeHtml(f.sector ?? "—")],
  ["INDUSTRY", (f) => escapeHtml(f.industry ?? "—")],

  ["—— VALUATION ——", () => ""],
  ["PEG", (f) => fmt(f.peg_ratio)],
  ["P/B", (f) => fmt(f.price_to_book)],
  ["PAYOUT", (f) => pct(f.payout_ratio)],

  ["—— PROFITABILITY ——", () => ""],
  ["GROSS MARGIN", (f) => pct(f.gross_margin)],
  ["OPER MARGIN", (f) => pct(f.operating_margin)],
  ["NET MARGIN", (f) => pct(f.profit_margin)],
  ["ROE", (f) => pct(f.return_on_equity)],
  ["FREE CASHFLOW", (f) => fmtBig(f.free_cashflow)],
  ["DEBT/EQUITY", (f) => fmt(f.debt_to_equity)],

  ["—— GROWTH ——", () => ""],
  ["REVENUE GROWTH", (f) => pct(f.revenue_growth)],
  ["EARNINGS GROWTH", (f) => pct(f.earnings_growth)],

  ["—— THE STREET ——", () => ""],
  ["RATING", (f) => escapeHtml((f.recommendation ?? "—").replace(/_/g, " ").toUpperCase())],
  ["ANALYSTS", (f) => (f.analyst_count === null ? "—" : String(f.analyst_count))],
  ["TARGET MEAN", (f) => upside(f)],
  ["TARGET RANGE", (f) =>
    f.target_low && f.target_high ? `${fmt(f.target_low)} – ${fmt(f.target_high)}` : "—"],

  ["—— POSITIONING ——", () => ""],
  ["SHORT % FLOAT", (f) => pct(f.short_pct_float)],
  ["INSTITUTIONAL", (f) => pct(f.held_by_institutions)],
  ["AVG VOLUME", (f) => fmtBig(f.avg_volume)],
  ["50D MA", (f) => fmt(f.ma50)],
  ["200D MA", (f) => fmt(f.ma200)],
];

async function renderFundamentals(ticker) {
  const body = $("fundamentals-body");
  if (!ticker) {
    body.innerHTML = '<tr class="empty"><td colspan="2">Select a ticker.</td></tr>';
    return;
  }
  let data = null;
  try {
    data = (await api(`/api/fundamentals/${encodeURIComponent(ticker)}`)).fundamentals;
  } catch (_) {}
  if (!data) {
    body.innerHTML = state.warming.has(ticker)
      ? '<tr class="empty"><td colspan="2">Fetching fundamentals…</td></tr>'
      : '<tr class="empty"><td colspan="2">Awaiting first fetch…</td></tr>';
    return;
  }
  body.innerHTML = FUNDAMENTAL_ROWS.map(
    ([label, get]) =>
      label.startsWith("——")
        ? `<tr class="section"><td colspan="2">${escapeHtml(
            label.replace(/—/g, "").trim()
          )}</td></tr>`
        : `<tr><td>${label}</td><td>${get(data)}</td></tr>`
  ).join("");
}

// Only statuses worth interrupting the reader for. "ok" and "unchecked" render
// nothing: a clean link needs no badge, and an unverified one must not be
// dressed up as either verdict.
const LINK_FLAGS = {
  dead: ' · <span class="link-flag bad">[DEAD LINK]</span>',
  blocked: ' · <span class="link-flag warn">[PAYWALL/BLOCKED]</span>',
  error: ' · <span class="link-flag bad">[NO RESPONSE]</span>',
};

async function renderNews(ticker) {
  const list = $("news-list");
  if (!ticker) {
    markFilled(list), list.innerHTML = '<li class="empty">Select a ticker.</li>';
    return;
  }
  let articles = [];
  try {
    articles = (await api(`/api/news/${encodeURIComponent(ticker)}`)).articles;
  } catch (_) {}
  if (articles.length === 0) {
    // "No recent news" reads as a fact about the ticker. While a warm is in
    // flight it is a fact about us.
    markFilled(list), list.innerHTML = state.warming.has(ticker)
      ? '<li class="empty">Fetching news…</li>'
      : '<li class="empty">No recent news.</li>';
    return;
  }
  markFilled(list), list.innerHTML = articles
    .map((a) => {
      const when = a.published_at
        ? new Date(a.published_at).toLocaleDateString()
        : "";
      const sentiment = escapeHtml(a.sentiment ?? "neutral");
      const ai = a.ai_summary
        ? `<div class="news-ai"><span class="tag ${sentiment}">${sentiment.toUpperCase()}</span>${escapeHtml(a.ai_summary)}</div>`
        : "";
      const href = a.url ? escapeHtml(a.url) : "#";
      const link = LINK_FLAGS[a.link_status] ?? "";
      const dead = a.link_status === "dead" ? " dead-link" : "";
      return `<li>
        <a class="news-headline${dead}" href="${href}" target="_blank" rel="noopener">${escapeHtml(
        a.title ?? ""
      )}</a>
        <div class="news-meta">${escapeHtml(a.publisher ?? "")} · ${when}${link}</div>
        ${ai}
      </li>`;
    })
    .join("");
}

function renderAiStatus() {
  const el = $("ai-status");
  if (state.aiEnabled === false) {
    // Name the key the app is actually trying to use, not whichever provider
    // happened to be written into this string first.
    const key =
      state.aiProvider === "gemini" ? "GEMINI_API_KEY" : "ANTHROPIC_API_KEY";
    el.textContent = `AI off — set ${key} for summaries`;
  } else {
    el.textContent = "";
  }
}

async function renderWatchlist() {
  const body = $("watchlist-body");

  if (state.prices.length === 0) {
    body.innerHTML = '<tr class="empty"><td colspan="4">No tickers. Add one above.</td></tr>';
    return;
  }

  const rows = sortedPrices(state.prices);
  const sparklines = await Promise.all(rows.map((p) => getSparkline(p.ticker)));

  body.innerHTML = rows
    .map((p, i) => {
      const dir = (p.change_pct ?? 0) >= 0 ? "up" : "down";
      const sign = (p.change_pct ?? 0) >= 0 ? "+" : "";
      const ticker = escapeHtml(p.ticker);
      return `<tr data-ticker="${ticker}"
        class="${p.ticker === state.activeTicker ? "active" : ""} ${p.is_stale ? "is-stale" : ""}">
        <td class="sym">${ticker}<button class="remove" data-remove="${ticker}"
          title="Remove">×</button></td>
        <td class="num">${fmt(p.price)}</td>
        <td class="num ${dir}">${p.change_pct === null ? "—" : sign + fmt(p.change_pct)}</td>
        <td>${sparklineSVG(sparklines[i])}</td>
      </tr>`;
    })
    .join("");
}

function loadChart(ticker) {
  $("active-ticker").textContent = ticker ?? "—";
  $("tv-link").href = ticker
    ? `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(ticker)}`
    : "https://www.tradingview.com/chart/";

  const container = $("chart-container");
  if (!ticker) {
    container.innerHTML = '<p class="empty-chart">Select a ticker to load its chart.</p>';
    return;
  }
  // The public embed cannot render private Pine Script indicators at any
  // subscription tier. The "Open in TradingView" link above is the route to
  // those; Phase 4 webhooks bring their signals back into this app.
  const params = new URLSearchParams({
    symbol: ticker, interval: "D", theme: "dark", style: "1",
    timezone: "Etc/UTC", withdateranges: "1", hide_side_toolbar: "0",
    allow_symbol_change: "0", save_image: "0", locale: "en",
  });
  container.innerHTML =
    `<iframe src="https://s.tradingview.com/widgetembed/?${params}"
       title="TradingView chart for ${escapeHtml(ticker)}" allowtransparency="true"
       scrolling="no"></iframe>`;
}

function bnum(id) {
  const raw = $(id).value.trim();
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

function showScreenerError(message) {
  const err = $("screener-error");
  err.textContent = message;
  err.hidden = false;
}

async function runScreener() {
  const err = $("screener-error");
  const body = $("screener-body");
  err.hidden = true;

  const mcapB = bnum("f-mcap-min");
  const payload = {
    pe_max: bnum("f-pe-max"),
    market_cap_min: mcapB === null ? null : mcapB * 1e9,
    dividend_yield_min: bnum("f-div-min"),
    sector: $("f-sector").value.trim() || null,
    limit: 50,
  };
  Object.keys(payload).forEach((k) => payload[k] === null && delete payload[k]);

  let data;
  try {
    data = await api("/api/screener", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    showScreenerError(e.message);
    return;
  }

  const cov = data.coverage;
  $("screener-coverage").textContent = cov.warming
    ? `screening ${cov.screened} of ${cov.universe} — still loading`
    : `${cov.screened} of ${cov.universe} covered`;

  if (data.matches.length === 0) {
    body.innerHTML = cov.screened === 0
      ? '<tr class="empty"><td colspan="4">Universe still loading — no data yet.</td></tr>'
      : '<tr class="empty"><td colspan="4">No matches.</td></tr>';
    return;
  }

  body.innerHTML = data.matches
    .map((m) => {
      const sym = escapeHtml(m.ticker);
      return `<tr data-ticker="${sym}">
        <td class="sym">${sym}</td>
        <td class="num">${fmt(m.pe_ratio)}</td>
        <td class="num">${fmtBig(m.market_cap)}</td>
        <td>${escapeHtml(m.sector ?? "—")}</td>
      </tr>`;
    })
    .join("");
}

async function renderMacro() {
  const list = $("macro-list");
  showSkeleton(list, skeletonRows(4));
  let data;
  try {
    data = await api("/api/macro/calendar");
  } catch (_) {
    markFilled(list), list.innerHTML = '<li class="empty">Calendar unavailable.</li>';
    return;
  }
  if (data.events.length === 0) {
    markFilled(list), list.innerHTML = data.fred_enabled
      ? '<li class="empty">No upcoming releases cached yet.</li>'
      : '<li class="empty">Set FRED_API_KEY in .env for the macro calendar.</li>';
    return;
  }
  markFilled(list), list.innerHTML = data.events
    .map((e) => {
      const impact = escapeHtml(e.impact ?? "low");
      const when = e.event_date
        ? new Date(e.event_date + "T00:00:00Z").toLocaleDateString()
        : "";
      return `<li>
        <span class="tag ${impact}">${impact.toUpperCase()}</span>
        ${escapeHtml(e.release_name ?? "")}
        <div class="macro-date">${escapeHtml(when)}</div>
      </li>`;
    })
    .join("");
}

function setActive(ticker) {
  state.activeTicker = ticker;
  loadChart(ticker);
  document.querySelectorAll(".active-sym").forEach((el) => {
    el.textContent = ticker ?? "—";
  });
  renderFundamentals(ticker);
  renderNews(ticker);
  renderWatchlist();
}

// Re-entrancy guard: setInterval(refresh, REFRESH_MS) can fire again before
// a slow cycle finishes, and the add/remove handlers call refresh() directly
// too. Without this, overlapping cycles could race on state.prices and the
// DOM. A caller that arrives mid-cycle awaits the cycle already in flight
// instead of starting a redundant one.
let refreshPromise = null;

function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = runRefresh().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function runRefresh() {
  try {
    const data = await api("/api/prices");
    state.prices = data.prices;
    pruneSparklineCache();

    if (!state.activeTicker && state.prices.length > 0) {
      setActive(state.prices[0].ticker);
      return;
    }
    if (state.activeTicker && !state.prices.some((p) => p.ticker === state.activeTicker)) {
      setActive(state.prices[0]?.ticker ?? null);
      return;
    }

    await renderWatchlist();

    const health = await api("/api/health");
    state.aiEnabled = health.ai_enabled ?? null;
    state.aiProvider = health.ai_provider ?? null;
    const wasWarming = state.warming;
    state.warming = new Set(health.warming || []);
    // A ticker that just finished warming has data the panels have not drawn.
    for (const ticker of wasWarming) {
      if (!state.warming.has(ticker) && ticker === state.activeTicker) {
        renderNews(ticker);
        renderFundamentals(ticker);
      }
    }
    $("chat-status").textContent =
      health.chat_enabled === false
        ? `set ${
            health.chat_provider === "gemini"
              ? "GEMINI_API_KEY"
              : "ANTHROPIC_API_KEY"
          } to enable`
        : "";
    renderAiStatus();
    renderAlertsStatus(health);
    const anyStale = state.prices.some((p) => p.is_stale);
    const el = $("status");
    el.classList.toggle("stale", anyStale);
    el.textContent = anyStale
      ? `stale — retrying every ${Math.round(health.interval_seconds)}s`
      : health.last_success_at
        ? `live — updated ${new Date(health.last_success_at).toLocaleTimeString()}`
        : "waiting for first poll…";
  } catch (err) {
    const el = $("status");
    el.classList.add("stale");
    el.textContent = `backend unreachable: ${err.message}`;
  }
}

// Adding happens in the modal now; the form exists only so Enter inside the
// watchlist header does not reload the page.
$("add-form").addEventListener("submit", (e) => e.preventDefault());

$("watchlist-body").addEventListener("click", async (e) => {
  const removeBtn = e.target.closest("[data-remove]");
  if (removeBtn) {
    e.stopPropagation();
    await api(`/api/watchlist/${removeBtn.dataset.remove}`, { method: "DELETE" });
    await refresh();
    return;
  }
  const row = e.target.closest("tr[data-ticker]");
  if (row) setActive(row.dataset.ticker);
});

$("screener-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runScreener();
});

$("screener-body").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-ticker]");
  if (row) setActive(row.dataset.ticker);
});

refresh();
renderMacro();
renderAlerts();
// Macro releases change on a ~3h backend cadence, so a slow refresh is enough;
// putting this on the 5s price interval would be pure waste.
setInterval(renderMacro, 10 * 60 * 1000);
// Setups fire on the backend's bar cycle (~30 min), so polling faster than
// this only re-renders the same rows.
setInterval(renderAlerts, 60 * 1000);
setInterval(refresh, REFRESH_MS);

// --- Analyst chat -----------------------------------------------------------
//
// The model answers in Markdown, and rendering it as plain text left the panel
// full of literal ### and ** — technically safe, practically unreadable.
//
// So: escape the whole reply FIRST, then apply formatting to the escaped text.
// Every tag in the output is one this function wrote; nothing from the model
// can become markup, because its angle brackets stopped being angle brackets
// before any pattern ran. That ordering is the entire safety argument.
// Kept in step with cfg.chat_history_turns; the backend trims to its own
// ceiling anyway, so sending more than it wants is harmless.
const CHAT_HISTORY_TURNS = 60;
state.chatHistory = [];

// Inline formatting, applied to already-escaped text.
function inlineMarkdown(text) {
  // Code spans are lifted out before anything else runs and put back at the
  // end. Wrapping them in <code> first is not enough: the bold and italic
  // patterns happily reach inside the tag, so `**not bold**` came out bold.
  const codes = [];
  const withoutCode = text.replace(/`([^`]+)`/g, (_, body) => {
    codes.push(body);
    return `\u0000CODE${codes.length - 1}\u0000`;
  });

  return withoutCode
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
    // Links: http(s) only. A model-supplied javascript: or data: URL is
    // precisely what must not pass through, and the scheme is the only thing
    // standing between a rendered link and an executable one.
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)"']+)\)/g,
      (_, label, href) =>
        `<a href="${href}" target="_blank" rel="noopener">${label}</a>`
    )
    .replace(/\u0000CODE(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

function renderMarkdown(raw) {
  const lines = escapeHtml(String(raw ?? "")).split("\n");
  const out = [];
  let list = null;          // "ul" | "ol" | null
  let table = null;         // collected rows while inside a table
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${inlineMarkdown(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  const flushList = () => {
    if (list) {
      out.push(`</${list}>`);
      list = null;
    }
  };
  const flushTable = () => {
    if (!table) return;
    const [head, ...body] = table;
    out.push(
      `<div class="md-table-wrap"><table class="md-table"><thead><tr>${head
        .map((c) => `<th>${inlineMarkdown(c)}</th>`)
        .join("")}</tr></thead><tbody>${body
        .map(
          (row) =>
            `<tr>${row.map((c) => `<td>${inlineMarkdown(c)}</td>`).join("")}</tr>`
        )
        .join("")}</tbody></table></div>`
    );
    table = null;
  };
  const flushAll = () => {
    flushParagraph();
    flushList();
    flushTable();
  };

  const cells = (line) =>
    line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  for (const line of lines) {
    const trimmed = line.trim();

    if (!trimmed) {
      flushAll();
      continue;
    }

    // Table rows. The |---|---| separator only confirms the header.
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      flushParagraph();
      flushList();
      if (/^\|[\s:|-]+\|$/.test(trimmed)) continue;
      (table = table || []).push(cells(trimmed));
      continue;
    }
    flushTable();

    const heading = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushAll();
      // Clamped to h3-h6: h1 and h2 belong to the page and the panel titles,
      // and a reply heading outranking its own panel reads as a mistake.
      const level = Math.min(Math.max(heading[1].length, 3), 6);
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
      flushAll();
      out.push("<hr>");
      continue;
    }

    const bullet = trimmed.match(/^[-*+]\s+(.*)$/);
    const numbered = trimmed.match(/^\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushParagraph();
      const wanted = bullet ? "ul" : "ol";
      if (list !== wanted) {
        flushList();
        out.push(`<${wanted}>`);
        list = wanted;
      }
      out.push(`<li>${inlineMarkdown((bullet || numbered)[1])}</li>`);
      continue;
    }
    flushList();

    if (trimmed.startsWith("&gt;")) {
      flushParagraph();
      out.push(`<blockquote>${inlineMarkdown(trimmed.slice(4).trim())}</blockquote>`);
      continue;
    }

    paragraph.push(trimmed);
  }
  flushAll();
  return out.join("");
}

function appendChat(cls, text) {
  const log = $("chat-log");
  const div = document.createElement("div");
  div.className = `chat-msg ${cls}`;
  if (cls === "bot") {
    // Safe by construction: renderMarkdown escapes before it formats.
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

$("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("chat-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendChat("user", `> ${question}`);
  const pending = appendChat("pending", "thinking…");

  try {
    const data = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: question,
        history: state.chatHistory.slice(-CHAT_HISTORY_TURNS),
      }),
    });
    pending.remove();
    appendChat("bot", data.reply);
    if (data.tools_used?.length) {
      appendChat("tools", `used: ${data.tools_used.join(", ")}`);
    }
    state.chatHistory.push({ role: "user", content: question });
    state.chatHistory.push({ role: "assistant", content: data.reply });
  } catch (err) {
    pending.remove();
    appendChat("tools", `error: ${err.message}`);
  }
});

// --- Alerts -----------------------------------------------------------------

function fmtLevel(v) {
  return v === null || v === undefined ? "—" : Number(v).toFixed(2);
}

async function renderAlerts() {
  const list = $("alerts-list");
  showSkeleton(list, skeletonRows(3));
  let signals = [];
  let measured = {};
  try {
    [signals, measured] = await Promise.all([
      api("/api/signals?limit=25").then((d) => d.signals),
      api("/api/backtest").then((d) => d.stats),
    ]);
  } catch (_) {}

  if (signals.length === 0) {
    markFilled(list), list.innerHTML =
      '<li class="empty">No setups yet. The engine scans on its bar cycle.</li>';
    return;
  }

  markFilled(list), list.innerHTML = signals
    .map((s) => {
      const dir = s.direction === "short" ? "short" : "long";
      const when = s.created_at
        ? new Date(s.created_at).toLocaleString()
        : "";
      // Earnings inside the window is the one thing the stop cannot protect
      // against, so it is called out rather than left in the payload.
      const earnings = s.earnings_at
        ? ` · <span class="signal-warn">EARNINGS ${escapeHtml(
            String(s.earnings_at).slice(0, 10)
          )}</span>`
        : "";
      const sent = s.delivered_at ? " · SENT" : "";
      return `<li class="signal ${dir}">
        <div class="signal-head">
          <span class="signal-sym">${escapeHtml(s.ticker)}</span>
          <span class="signal-dir ${dir}">${dir.toUpperCase()}</span>
          <span class="signal-grade">${escapeHtml(s.grade)} · ${Number(
        s.risk_reward
      ).toFixed(2)}R · ${(s.factors || []).length}/${
        (s.factors || []).length + (s.factors_failed || []).length
      } confluence</span>
        </div>
        ${levelsRow(s)}
        <div class="signal-meta">${escapeHtml(
          (s.factors || []).join(", ") || "—"
        )} · ${escapeHtml(s.timeframes || "")}</div>
        <div class="signal-meta">${gradeRecord(measured, s.ticker, s.grade)}</div>
        <div class="signal-meta">${when}${sent}${earnings}</div>
      </li>`;
    })
    .join("");
}

// A grade is a claim about expected performance. Measurement showed A+ did
// not reliably outperform B, so no grade is ever shown in this app without
// the numbers behind it -- a bare letter reads as a ranking it has not earned.
// Delegates to measuredLine so the fired setups, the playbook and the scanner
// all word the record identically — three copies drifted apart once already.
function gradeRecord(measured, ticker, grade) {
  return measuredLine(measured?.[ticker]?.[grade], grade);
}

function renderAlertsStatus(health) {
  const el = $("alerts-status");
  if (!el) return;
  // Measurement showed A+ did not reliably beat B, so the panel says what the
  // grades are rather than letting a letter imply a ranking it has not earned.
  el.textContent =
    health.alerts_enabled === false
      ? "set DISCORD_WEBHOOK_URL to push"
      : "";
}

// --- Panel arranging: drag, resize, snap ------------------------------------
//
// Every panel is positioned absolutely and moved or resized on its own. The
// previous model shared four grid tracks between all nine panels, so dragging
// one edge moved several panels at once — and its splitters drew lines at
// fixed gutters straight through any panel that happened to span one.
//
// Geometry is stored in SNAP units (a 24 x 24 lattice), not pixels, so a saved
// layout survives a resized window and every edge lands on the same grid as
// its neighbours.

const SNAP_X = 24;
const SNAP_Y = 24;
const MIN_W = 3;   // below this a panel's title is unreadable
const MIN_H = 3;
const CANVAS_PAD = 6;

const grid = document.querySelector(".grid");

// Live geometry of the panels on screen, keyed by id: {x, y, w, h} in units.
let geometry = {};

// The version suffix is load-bearing. Earlier builds wrote a different shape
// under the unversioned key; read back, those entries produced NaN width and
// height, which the browser silently ignores — leaving every panel shrink-
// wrapped to its content. Bumping the key retires that data outright instead
// of hoping validation catches every variant of it.
const GEOMETRY_VERSION = "v2";

function layoutStorageKey() {
  return typeof activeWorkspace === "number" && typeof WORKSPACES !== "undefined"
    ? `diy-terminal-geometry.${GEOMETRY_VERSION}:${WORKSPACES[activeWorkspace].key}`
    : `diy-terminal-geometry.${GEOMETRY_VERSION}`;
}

// A stored entry is only usable if it is four finite numbers that fit the
// lattice. Anything else is discarded in favour of the tab's default, because
// a panel that silently renders at its content size looks like the app is
// broken rather than like a stale preference.
function isUsableGeometry(g) {
  if (!g || typeof g !== "object") return false;
  return ["x", "y", "w", "h"].every((k) => {
    const v = g[k];
    return typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= SNAP_X;
  }) && g.w >= MIN_W && g.h >= MIN_H;
}

function saveGeometry() {
  try {
    localStorage.setItem(layoutStorageKey(), JSON.stringify(geometry));
  } catch (_) {}
}

// One-time sweep of the pre-versioned keys, so a browser that used an earlier
// build does not carry unusable geometry around forever.
(function discardLegacyGeometry() {
  try {
    Object.keys(localStorage)
      .filter(
        (k) =>
          k.startsWith("diy-terminal-geometry") &&
          !k.startsWith(`diy-terminal-geometry.${GEOMETRY_VERSION}`)
      )
      .forEach((k) => localStorage.removeItem(k));
    // The track-based layout this replaced is gone too.
    Object.keys(localStorage)
      .filter((k) => k.startsWith("diy-terminal-layout"))
      .forEach((k) => localStorage.removeItem(k));
  } catch (_) {}
})();

function readGeometry() {
  try {
    const raw = localStorage.getItem(layoutStorageKey());
    return raw ? JSON.parse(raw) : null;
  } catch (_) {
    return null;
  }
}

function clampGeometry(g) {
  // Coerce first: a non-numeric value would propagate NaN through every
  // Math.min below and out into an inline style the browser then ignores.
  ["x", "y", "w", "h"].forEach((k) => {
    const v = Number(g[k]);
    g[k] = Number.isFinite(v) ? v : 0;
  });
  g.w = Math.max(MIN_W, Math.min(g.w, SNAP_X));
  g.h = Math.max(MIN_H, Math.min(g.h, SNAP_Y));
  g.x = Math.max(0, Math.min(g.x, SNAP_X - g.w));
  g.y = Math.max(0, Math.min(g.y, SNAP_Y - g.h));
  return g;
}

function paint(id) {
  const el = $(id);
  const g = geometry[id];
  if (!el || !g) return;

  const pct = (value, span) => (Number(value) / span) * 100;
  const box = {
    left: pct(g.x, SNAP_X), top: pct(g.y, SNAP_Y),
    width: pct(g.w, SNAP_X), height: pct(g.h, SNAP_Y),
  };

  // Last line of defence. A non-finite percentage is not an error the browser
  // reports — it silently drops the declaration, and an absolutely positioned
  // panel with no width falls back to its content size. That failure mode
  // reached a user once; it must not be reachable from here again.
  if (!Object.values(box).every(Number.isFinite)) {
    console.warn(`Refusing to paint ${id} with`, g);
    return;
  }
  Object.entries(box).forEach(([prop, value]) => {
    el.style[prop] = `${value}%`;
  });
}

function paintAll() {
  Object.keys(geometry).forEach(paint);
}

function ensureHandles(el) {
  if (el.querySelector(".rz")) return;
  // Edges first, corners last: a later sibling wins the hit test where they
  // overlap, which is what makes corner-dragging reachable at all.
  ["n", "s", "w", "e", "nw", "ne", "sw", "se"].forEach((edge) => {
    const handle = document.createElement("div");
    handle.className = `rz rz-${edge}`;
    handle.dataset.edge = edge;
    el.appendChild(handle);
  });
}

function unitsPerPixel() {
  const box = grid.getBoundingClientRect();
  return {
    x: SNAP_X / Math.max(box.width - CANVAS_PAD * 2, 1),
    y: SNAP_Y / Math.max(box.height - CANVAS_PAD * 2, 1),
  };
}

function beginArrange(el, event, edge) {
  if (!el || el.classList.contains("maximised") || !geometry[el.id]) return;
  const id = el.id;
  const startGeom = { ...geometry[id] };
  const scale = unitsPerPixel();
  const startX = event.clientX;
  const startY = event.clientY;

  el.classList.add(edge ? "resizing" : "dragging");
  grid.classList.add("arranging");
  document.body.classList.add("arranging");

  const onMove = (e) => {
    // Rounded to the lattice, so an edge lands where a neighbour's edge lands.
    const dx = Math.round((e.clientX - startX) * scale.x);
    const dy = Math.round((e.clientY - startY) * scale.y);
    const g = { ...startGeom };

    if (!edge) {
      g.x = startGeom.x + dx;
      g.y = startGeom.y + dy;
    } else {
      if (edge.includes("e")) g.w = startGeom.w + dx;
      if (edge.includes("s")) g.h = startGeom.h + dy;
      if (edge.includes("w")) {
        // The left edge changes x and w together; clamping w alone would let
        // the panel creep sideways once it hits its minimum width.
        const width = Math.max(MIN_W, startGeom.w - dx);
        g.x = startGeom.x + (startGeom.w - width);
        g.w = width;
      }
      if (edge.includes("n")) {
        const height = Math.max(MIN_H, startGeom.h - dy);
        g.y = startGeom.y + (startGeom.h - height);
        g.h = height;
      }
    }
    geometry[id] = clampGeometry(g);
    paint(id);
  };

  const onUp = () => {
    el.classList.remove("dragging", "resizing");
    grid.classList.remove("arranging");
    document.body.classList.remove("arranging");
    saveGeometry();
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("pointercancel", onUp);
  event.preventDefault();
}

grid.addEventListener("pointerdown", (e) => {
  const handle = e.target.closest(".rz");
  if (handle) {
    beginArrange(handle.closest(".panel"), e, handle.dataset.edge);
    return;
  }
  const title = e.target.closest(".panel-title");
  // Buttons and inputs inside a title bar keep working.
  if (title && !e.target.closest("button, a, input, select")) {
    beginArrange(title.closest(".panel"), e, null);
  }
});

// Keyboard arranging: the title bar is focusable, so this works without a
// pointer. Alt resizes instead of moving; Shift takes a coarser step.
grid.addEventListener("keydown", (e) => {
  const title = e.target.closest(".panel-title");
  if (!title) return;
  const el = title.closest(".panel");
  const g = geometry[el.id];
  if (!g) return;
  const step = e.shiftKey ? 3 : 1;
  const moves = e.altKey
    ? { ArrowLeft: { w: -step }, ArrowRight: { w: step },
        ArrowUp: { h: -step }, ArrowDown: { h: step } }
    : { ArrowLeft: { x: -step }, ArrowRight: { x: step },
        ArrowUp: { y: -step }, ArrowDown: { y: step } };
  const delta = moves[e.key];
  if (!delta) return;
  Object.entries(delta).forEach(([k, v]) => {
    g[k] += v;
  });
  geometry[el.id] = clampGeometry(g);
  paint(el.id);
  saveGeometry();
  e.preventDefault();
});

// Geometry is in snap units, so a resized window needs only a repaint — the
// percentages are derived from the same units.
window.addEventListener("resize", paintAll);

$("reset-layout").addEventListener("click", () => {
  try {
    localStorage.removeItem(layoutStorageKey());
  } catch (_) {}
  applyWorkspace(activeWorkspace);
});

// --- Screener presets --------------------------------------------------------
//
// The filter engine already accepted saved presets from Phase 3; there was
// simply no way to reach them from the interface.

const PRESET_FIELDS = {
  pe_max: "f-pe-max",
  market_cap_min: "f-mcap-min",
  dividend_yield_min: "f-div-min",
  sector: "f-sector",
};

// The market-cap box is in billions for readability, but `market_cap_min`
// means raw dollars everywhere else in the API. Presets are stored in API
// units so the saved filters mean the same thing to any other reader.
const MCAP_SCALE = 1e9;

function currentFilters() {
  const filters = {};
  for (const [key, id] of Object.entries(PRESET_FIELDS)) {
    const raw = $(id).value.trim();
    if (raw === "") continue;
    if (key === "sector") filters[key] = raw;
    else if (key === "market_cap_min") filters[key] = Number(raw) * MCAP_SCALE;
    else filters[key] = Number(raw);
  }
  return filters;
}

function applyFilters(filters) {
  for (const [key, id] of Object.entries(PRESET_FIELDS)) {
    // Blank rather than leave a stale value: a preset that omits a field
    // means "no constraint", not "keep whatever was there".
    const value = filters[key];
    if (value === undefined || value === null) {
      $(id).value = "";
    } else if (key === "market_cap_min") {
      $(id).value = Number(value) / MCAP_SCALE;
    } else {
      $(id).value = value;
    }
  }
}

async function loadPresets(selected) {
  const select = $("preset-select");
  let presets = [];
  try {
    presets = (await api("/api/screener/presets")).presets;
  } catch (_) {}

  select.innerHTML =
    '<option value="">SAVED SCREENS…</option>' +
    presets
      .map(
        (p) =>
          `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)}</option>`
      )
      .join("");
  if (selected) select.value = selected;
  $("preset-delete").disabled = !select.value;
  state.presets = Object.fromEntries(
    presets.map((p) => {
      let filters = {};
      try {
        filters = JSON.parse(p.filter_json);
      } catch (_) {}
      return [p.name, filters];
    })
  );
}

$("preset-select").addEventListener("change", () => {
  const name = $("preset-select").value;
  $("preset-delete").disabled = !name;
  if (!name) return;
  applyFilters(state.presets?.[name] ?? {});
  runScreener();
});

$("preset-save").addEventListener("click", async () => {
  const name = prompt("Name this screen:");
  if (!name || !name.trim()) return;
  try {
    await api("/api/screener/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), filters: currentFilters() }),
    });
    await loadPresets(name.trim());
  } catch (err) {
    showScreenerError(err.message);
  }
});

$("preset-delete").addEventListener("click", async () => {
  const name = $("preset-select").value;
  if (!name) return;
  try {
    await api(`/api/screener/presets/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
    await loadPresets();
  } catch (err) {
    showScreenerError(err.message);
  }
});

loadPresets();

// --- Add-symbol modal --------------------------------------------------------
//
// Clicking the box opens a browser over the whole catalogue rather than a
// dropdown you have to type into first. Categories are only offered for asset
// classes that map to something yfinance can actually quote: a CRYPTO tab that
// adds an unpriceable symbol is worse than no tab.

const MODAL_PAGE = 60;
let modalKind = "";
let modalQuery = "";
let modalOffset = 0;
let modalRows = [];
let modalIndex = -1;
let modalLoading = false;
let modalExhausted = false;
let modalTimer = null;

function fmtCap(v) {
  if (!v) return "";
  if (v >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(0)}M`;
  return String(Math.round(v));
}

const KIND_LABEL = {
  stock: "STOCK", fund: "FUND/ETF", dr: "ADR",
  crypto: "CRYPTO", forex: "FOREX", index: "INDEX",
};

function openSymbolModal() {
  $("symbol-modal").hidden = false;
  $("modal-search").value = "";
  modalQuery = "";
  $("modal-search").focus();
  loadModal(true);
}

function closeSymbolModal() {
  $("symbol-modal").hidden = true;
  modalIndex = -1;
}

function renderModalRows(rows, append) {
  const list = $("modal-results");
  const watchlist = new Set(state.prices.map((p) => p.ticker));
  const html = rows
    .map((r, i) => {
      const owned = watchlist.has(r.symbol);
      return `<li role="option" data-symbol="${escapeHtml(r.symbol)}"
                 class="${owned ? "on-watchlist" : ""}">
        <span class="sym">${escapeHtml(r.symbol)}</span>
        <span class="nm">${escapeHtml(r.name)}</span>
        <span class="kind">${escapeHtml(KIND_LABEL[r.kind] || r.kind)}</span>
        <span class="ex">${escapeHtml(r.exchange)}${
        r.market_cap ? " · " + fmtCap(r.market_cap) : ""
      }</span>
        <button class="add" data-add="${escapeHtml(r.symbol)}"
                title="${owned ? "Already on your watchlist" : "Add"}"
                >${owned ? "✓" : "+"}</button>
      </li>`;
    })
    .join("");
  if (append) list.insertAdjacentHTML("beforeend", html);
  else {
    markFilled(list), list.innerHTML = html;
    list.scrollTop = 0;
  }
}

async function loadModal(reset) {
  if (modalLoading || (modalExhausted && !reset)) return;
  modalLoading = true;
  if (reset) {
    modalOffset = 0;
    modalExhausted = false;
    modalRows = [];
    modalIndex = -1;
  }
  const kind = modalKind;
  const query = modalQuery;
  try {
    const path = query
      ? `/api/symbols/search?q=${encodeURIComponent(query)}&limit=50` +
        (kind ? `&kind=${kind}` : "")
      : `/api/symbols/browse?offset=${modalOffset}&limit=${MODAL_PAGE}` +
        (kind ? `&kind=${kind}` : "");
    const data = await api(path);

    // A newer query or chip won this race; discard the stale page.
    if (kind !== modalKind || query !== modalQuery) return;

    const rows = data.results;
    if (rows.length === 0 && modalRows.length === 0) {
      $("modal-results").innerHTML =
        '<li class="empty">No match. Refresh the catalogue from Settings if it is empty.</li>';
      $("modal-foot").textContent = "";
      return;
    }
    renderModalRows(rows, !reset);
    modalRows = modalRows.concat(rows);
    modalOffset += rows.length;
    modalExhausted = query ? true : !data.has_more;
    $("modal-foot").textContent = query
      ? `${modalRows.length} match${modalRows.length === 1 ? "" : "es"} · Enter adds the highlighted row`
      : `${modalRows.length.toLocaleString()} of ${(data.total || 0).toLocaleString()} · scroll for more`;
  } catch (_) {
    modalExhausted = true;
  } finally {
    modalLoading = false;
  }
}

function highlightModal(next) {
  const items = $("modal-results").querySelectorAll("li[data-symbol]");
  if (items.length === 0) return;
  modalIndex = (next + items.length) % items.length;
  items.forEach((el, i) =>
    el.setAttribute("aria-selected", i === modalIndex ? "true" : "false")
  );
  items[modalIndex].scrollIntoView({ block: "nearest" });
}

// The backend warms a new ticker in the background; poll a little faster than
// the normal interval until it lands, so the panels fill in seconds rather
// than on the next cycle.
function followWarm(ticker) {
  let attempts = 0;
  const timer = setInterval(async () => {
    attempts += 1;
    await refresh();
    if (ticker === state.activeTicker) {
      renderNews(ticker);
      renderFundamentals(ticker);
    }
    // The warm caches the bars the playbook needs, so recompute as they land.
    renderPlans();
    // Give up after ~40s: by then the warm has either landed or failed, and
    // the ordinary cadence takes over.
    if (!state.warming.has(ticker) && attempts > 2) clearInterval(timer);
    if (attempts >= 20) clearInterval(timer);
  }, 2000);
}

async function addSymbol(symbol) {
  $("add-error").hidden = true;
  try {
    const result = await api("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: symbol }),
    });
    state.warming.add(symbol);
    await refresh();
    setActive(symbol);
    renderPlans();
    if (result && result.warming) followWarm(symbol);
    // Re-render so the row shows as owned without closing the browser.
    renderModalRows(modalRows, false);
    return true;
  } catch (err) {
    $("modal-foot").textContent = `Could not add ${symbol}: ${err.message}`;
    return false;
  }
}

$("add-input").addEventListener("focus", openSymbolModal);
$("add-input").addEventListener("click", openSymbolModal);
$("add-open").addEventListener("click", openSymbolModal);
$("symbol-modal-close").addEventListener("click", closeSymbolModal);

$("symbol-modal").addEventListener("click", (e) => {
  // Only the backdrop closes; a click inside the card must not.
  if (e.target === $("symbol-modal")) closeSymbolModal();
});

$("modal-search").addEventListener("input", () => {
  modalQuery = $("modal-search").value.trim();
  clearTimeout(modalTimer);
  modalTimer = setTimeout(() => loadModal(true), 130);
});

$("symbol-chips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  modalKind = chip.dataset.kind;
  document.querySelectorAll("#symbol-chips .chip").forEach((c) =>
    c.setAttribute("aria-selected", String(c === chip))
  );
  loadModal(true);
});

$("modal-results").addEventListener("scroll", () => {
  const list = $("modal-results");
  if (list.scrollTop + list.clientHeight >= list.scrollHeight - 60) {
    if (!modalQuery) loadModal(false);
  }
});

$("modal-results").addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-add]");
  if (button) {
    e.stopPropagation();
    await addSymbol(button.dataset.add);
    return;
  }
  const row = e.target.closest("li[data-symbol]");
  if (row && (await addSymbol(row.dataset.symbol))) closeSymbolModal();
});

$("modal-search").addEventListener("keydown", async (e) => {
  if (e.key === "ArrowDown") {
    highlightModal(modalIndex + 1);
    e.preventDefault();
  } else if (e.key === "ArrowUp") {
    highlightModal(modalIndex - 1);
    e.preventDefault();
  } else if (e.key === "Enter") {
    e.preventDefault();
    const chosen =
      modalIndex >= 0 ? modalRows[modalIndex]?.symbol : modalQuery.toUpperCase();
    if (chosen && (await addSymbol(chosen))) closeSymbolModal();
  } else if (e.key === "Escape") {
    closeSymbolModal();
  }
});

// --- User price alerts -------------------------------------------------------

function showAlertError(message) {
  const el = $("alert-error");
  el.textContent = message;
  el.hidden = false;
}

async function renderMyAlerts() {
  const list = $("my-alerts");
  showSkeleton(list, skeletonRows(2, [26, 40]));
  let alerts = [];
  try {
    alerts = (await api("/api/alerts")).alerts;
  } catch (_) {}

  if (alerts.length === 0) {
    markFilled(list), list.innerHTML = "";
    return;
  }
  markFilled(list), list.innerHTML = alerts
    .map((a) => {
      const fired = Boolean(a.triggered_at);
      const cls = !fired ? "armed" : a.direction === "above" ? "fired-up" : "fired-down";
      const state = fired
        ? `TRIGGERED ${new Date(a.triggered_at).toLocaleString()}`
        : "ARMED";
      return `<li class="my-alert">
        <span class="sym">${escapeHtml(a.ticker)}</span>
        <span>${escapeHtml(a.direction)} ${Number(a.price).toFixed(2)}</span>
        <span class="${cls}">${state}</span>
        ${a.note ? `<span class="nm">${escapeHtml(a.note)}</span>` : ""}
        <button class="remove-alert" data-alert="${a.id}" title="Delete">×</button>
      </li>`;
    })
    .join("");
}

$("alert-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("alert-error").hidden = true;
  const ticker = $("alert-ticker").value.trim().toUpperCase();
  const price = Number($("alert-price").value);
  if (!ticker || !Number.isFinite(price) || price <= 0) {
    showAlertError("Enter a ticker and a price above zero.");
    return;
  }
  try {
    await api("/api/alerts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker,
        direction: $("alert-direction").value,
        price,
        note: $("alert-note").value.trim() || null,
      }),
    });
    $("alert-ticker").value = "";
    $("alert-price").value = "";
    $("alert-note").value = "";
    await renderMyAlerts();
  } catch (err) {
    showAlertError(err.message);
  }
});

$("my-alerts").addEventListener("click", async (e) => {
  const button = e.target.closest("button[data-alert]");
  if (!button) return;
  try {
    await api(`/api/alerts/${button.dataset.alert}`, { method: "DELETE" });
    await renderMyAlerts();
  } catch (err) {
    showAlertError(err.message);
  }
});

// --- Settings ----------------------------------------------------------------

$("settings-toggle").addEventListener("click", () => {
  const panel = $("settings-drawer");
  const open = panel.hidden;
  panel.hidden = !open;
  $("settings-toggle").setAttribute("aria-expanded", String(open));
  if (open) {
    renderSettings();
    renderSecurity();
    renderTotpState();
    renderAccount();
  }
});

async function renderSettings() {
  try {
    const s = await api("/api/settings");
    const state = $("webhook-state");
    if (s.discord_webhook_source === "database") {
      state.textContent = `Configured (${s.discord_webhook_hint}). Saving replaces it.`;
    } else if (s.discord_webhook_source === "environment") {
      // Saying "configured" without saying where would make an unchanged
      // env var look like a failed save.
      state.textContent =
        "Set by the DISCORD_WEBHOOK_URL environment variable. Saving here overrides it.";
    } else {
      state.textContent = "Not configured — setups and price alerts are not pushed.";
    }
  } catch (_) {}
  try {
    const { count } = await api("/api/symbols/status");
    $("symbol-state").textContent =
      count > 0
        ? `${count.toLocaleString()} symbols cached and searchable.`
        : "Empty — set SCREENER_SCAN_URL, then refresh to pull the catalogue.";
  } catch (_) {}
}

$("webhook-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("webhook-error").hidden = true;
  const url = $("webhook-input").value.trim();
  if (!url) return;
  try {
    await api("/api/settings/discord-webhook", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    // Never leave a credential sitting in the box.
    $("webhook-input").value = "";
    await renderSettings();
    refresh();
  } catch (err) {
    $("webhook-error").textContent = err.message;
    $("webhook-error").hidden = false;
  }
});

$("webhook-clear").addEventListener("click", async () => {
  try {
    await api("/api/settings/discord-webhook", { method: "DELETE" });
    $("webhook-input").value = "";
    await renderSettings();
  } catch (_) {}
});

$("symbol-refresh").addEventListener("click", async () => {
  const button = $("symbol-refresh");
  button.disabled = true;
  button.textContent = "PULLING…";
  try {
    const { count, refreshed, reason } =
      await api("/api/symbols/refresh", { method: "POST" });
    if (refreshed) {
      $("symbol-state").textContent =
        `${count.toLocaleString()} symbols cached and searchable.`;
    } else if (reason === "no_screener_source") {
      // Not a failure: the app ships without a screener endpoint. Say what to
      // set rather than implying the network ate the request.
      $("symbol-state").textContent =
        "No screener source configured — set SCREENER_SCAN_URL in .env to enable search.";
    } else {
      $("symbol-state").textContent =
        "The screener did not respond — the previous catalogue is unchanged.";
    }
  } catch (_) {
    $("symbol-state").textContent = "Refresh failed — the previous catalogue is unchanged.";
  } finally {
    button.disabled = false;
    button.textContent = "REFRESH CATALOGUE";
  }
});

renderMyAlerts();
setInterval(renderMyAlerts, 30 * 1000);

// --- Workspaces, fullscreen and keyboard -------------------------------------
//
// Eight panels on one screen is cramped. A workspace shows a subset, which is
// the two-page split the layout was always missing — but panels are HIDDEN
// rather than re-placed, so the track sizes the user dragged survive a switch.

// --- maximise ---------------------------------------------------------------

function maximisePanel(panel) {
  const grid = document.querySelector(".grid");
  const already = panel.classList.contains("maximised");
  restorePanels();
  if (already) return;
  // A hidden panel cannot be maximised into view without contradicting the
  // workspace the user chose, so un-hide it for the duration.
  panel.dataset.wasHidden = String(panel.hidden);
  panel.hidden = false;
  panel.classList.add("maximised");
  // Inline: the panel's own inline geometry is what has to be overridden.
  panel.style.left = "0";
  panel.style.top = "0";
  panel.style.width = "100%";
  panel.style.height = "100%";
  grid.classList.add("has-maximised");
}

function restorePanels() {
  const grid = document.querySelector(".grid");
  grid.classList.remove("has-maximised");
  document.querySelectorAll(".panel.maximised").forEach((el) => {
    el.classList.remove("maximised");
    paint(el.id);
    if (el.dataset.wasHidden === "true") el.hidden = true;
    delete el.dataset.wasHidden;
  });
}

document.querySelectorAll("[data-maximise]").forEach((button) =>
  button.addEventListener("click", (e) => {
    e.stopPropagation();
    maximisePanel(button.closest(".panel"));
  })
);

// --- fullscreen -------------------------------------------------------------

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch (_) {
    // Denied (no user gesture, or unsupported). Nothing useful to say.
  }
}

$("fullscreen-toggle").addEventListener("click", toggleFullscreen);
document.addEventListener("fullscreenchange", () => {
  $("fullscreen-toggle").textContent = document.fullscreenElement
    ? "⤡ EXIT"
    : "⤢ FULL";
});

// --- help -------------------------------------------------------------------

function toggleHelp(force) {
  const overlay = $("help-overlay");
  overlay.hidden = force !== undefined ? !force : !overlay.hidden;
}

$("help-toggle").addEventListener("click", () => toggleHelp());
$("help-overlay").addEventListener("click", () => toggleHelp(false));

// --- keyboard ---------------------------------------------------------------

function isTyping(target) {
  // Single-letter shortcuts must never fire while someone is typing a ticker.
  const tag = (target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" ||
    target.isContentEditable;
}

let hoveredPanel = null;
document.querySelectorAll(".grid > .panel").forEach((panel) => {
  panel.addEventListener("mouseenter", () => {
    hoveredPanel = panel;
  });
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (!$("help-overlay").hidden) return toggleHelp(false);
    if (!$("newtab-modal").hidden) {
      $("newtab-modal").hidden = true;
      return;
    }
    if (overlayScreen) return applyWorkspace(activeWorkspace);
    if (document.querySelector(".panel.maximised")) return restorePanels();
    if (!$("settings-drawer").hidden) {
      $("settings-drawer").hidden = true;
      $("settings-toggle").setAttribute("aria-expanded", "false");
      return;
    }
    closeSymbolModal();
    return;
  }

  if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;

  switch (e.key) {
    case "1": case "2": case "3": case "4": case "5":
      applyWorkspace(Number(e.key) - 1);
      break;
    case "/":
      $("add-input").focus();
      e.preventDefault();
      break;
    case "a": $("alert-ticker").focus(); e.preventDefault(); break;
    case "c": $("chat-input").focus(); e.preventDefault(); break;
    case "f": toggleFullscreen(); break;
    case "x":
      if (hoveredPanel) maximisePanel(hoveredPanel);
      break;
    case "?": toggleHelp(); break;
    default: break;
  }
});

// --- Watchlist sorting -------------------------------------------------------
//
// Sorting is applied at render time and never mutates state.prices, because the
// poller replaces that array wholesale on every cycle.

state.sort = { key: null, dir: "asc" };

function sortedPrices(rows) {
  const { key, dir } = state.sort;
  if (!key) return rows;
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = a[key];
    const y = b[key];
    // Missing values sort last in both directions -- a stale row should never
    // take the top of the list just because its price is null.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (typeof x === "string") return sign * x.localeCompare(y);
    return sign * (x - y);
  });
}

document.querySelectorAll(".watchlist th[data-sort]").forEach((th) =>
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    state.sort =
      state.sort.key === key
        ? { key, dir: state.sort.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "ticker" ? "asc" : "desc" };
    document.querySelectorAll(".watchlist th[data-sort]").forEach((other) =>
      other.setAttribute(
        "aria-sort",
        other.dataset.sort === key
          ? state.sort.dir === "asc" ? "ascending" : "descending"
          : "none"
      )
    );
    renderWatchlist();
  })
);

// --- Playbook ----------------------------------------------------------------
//
// /api/signals lists setups that actually fired, which on a normal day is none
// of them — and "no signals" says nothing about what you hold. The playbook
// shows the levels for every ticker with an explicit verdict, so a 2-of-7 chart
// appears as the weak thing it is instead of being silently omitted.

const VERDICT_LABEL = {
  tradeable: "TRADEABLE",
  weak: "NO SETUP",
  blackout: "EARNINGS BLACKOUT",
  insufficient_data: "NO DATA YET",
};

// Entry, stop and both take-profits, each with the move it represents. "T1
// 504.62" alone does not say what you would make; "+1.70%" does, and the R
// multiple says how that compares to what you are risking.
/* The share count, computed here rather than fetched: it is division, the
   inputs are already on the page, and a request per plan card to divide two
   numbers would be silly. Mirrors backend/services/sizing.py, including the
   floor — rounding up risks more than the number the user typed. */
function sizeLine(p) {
  const { account_size: account, risk_pct: risk } = accountSettings;
  if (!Number.isFinite(p.entry) || !Number.isFinite(p.stop)) return "";
  const perShare = Math.abs(p.entry - p.stop);
  if (!(perShare > 0) || !(account > 0) || !(risk > 0)) return "";

  let shares = Math.floor((account * risk / 100) / perShare);
  let capped = "";
  if (shares * p.entry > account) {
    shares = Math.floor(account / p.entry);
    capped = " · cash-capped";
  }
  if (shares < 1) {
    return `<div class="plan-size">Too far to the stop for ${risk}% of ` +
      `${money(account)} — one share risks ${money(perShare)}.</div>`;
  }
  const atRisk = shares * perShare;
  return `<div class="plan-size">SIZE <b>${shares}</b> sh · ` +
    `${money(shares * p.entry)} · risks <b>${money(atRisk)}</b> ` +
    `(${(atRisk / account * 100).toFixed(2)}% of account)${capped}</div>`;
}

function levelsRow(p) {
  if (p.entry === null || p.entry === undefined) return "";
  const move = (target) =>
    p.entry ? ((target - p.entry) / p.entry) * 100 : 0;
  const risk = p.entry - p.stop;
  const rMultiple = (target) => (risk > 0 ? (target - p.entry) / risk : 0);

  const cell = (label, value, extra, cls) =>
    `<span class="${cls}">${label} <b>${fmtLevel(value)}</b>${
      extra ? `<i>${extra}</i>` : ""
    }</span>`;

  return `<div class="signal-levels">
    ${cell("ENTRY", p.entry, "", "")}
    ${cell("STOP", p.stop, `${move(p.stop).toFixed(2)}%`, "stop")}
    ${cell("TP1", p.target1,
           `+${move(p.target1).toFixed(2)}% · ${rMultiple(p.target1).toFixed(1)}R`,
           "target")}
    ${cell("TP2", p.target2,
           `+${move(p.target2).toFixed(2)}% · ${rMultiple(p.target2).toFixed(1)}R`,
           "target")}
  </div>`;
}

function measuredLine(m, grade) {
  if (!grade) {
    // Rendering nothing here reads as a missing number rather than as "there
    // is no claim to check", which is what an ungraded setup actually means.
    return '<span class="unmeasured">no grade — below the confluence minimum, '
      + "nothing measured</span>";
  }
  if (!m) {
    return `<span class="unmeasured">grade ${escapeHtml(grade)} unvalidated — no backtest yet</span>`;
  }
  const rate = (m.win_rate * 100).toFixed(0);
  const r = m.avg_r >= 0 ? `+${m.avg_r.toFixed(2)}` : m.avg_r.toFixed(2);
  // The win rate leads, because it is the number people actually look for —
  // but never without the sample size behind it. 67% of three trades is noise.
  return `<b class="${m.avg_r >= 0 ? "up" : "down"}">${rate}% win rate</b>
    · ${r}R average · measured over ${m.signals} past grade-${escapeHtml(grade)}
    signals`;
}

// The horizon decides which timeframes the engine reads and therefore what the
// levels mean — a stop sized from 1-minute ATR is a scalp stop; the same
// formula on daily bars is a swing stop. It also sets how often to recompute,
// because a 1-day horizon does not change between two glances.
let planHorizon = null;
let planTimer = null;

async function renderHorizons() {
  const bar = $("horizon-bar");
  let data;
  try {
    data = await api("/api/plans/horizons");
  } catch (_) {
    return;
  }
  planHorizon = planHorizon || data.selected;
  bar.innerHTML = data.horizons
    .map(
      (h) => `<button type="button" class="horizon" role="tab"
                 data-horizon="${escapeHtml(h.key)}"
                 aria-selected="${h.key === planHorizon}"
                 title="Hold ${escapeHtml(h.hold)} · recomputes every ${
        h.refresh_seconds
      }s">${escapeHtml(h.label)}<span class="hz-frames">${escapeHtml(
        h.frames.join(" · ")
      )}</span></button>`
    )
    .join("");
}

$("horizon-bar").addEventListener("click", (e) => {
  const button = e.target.closest("[data-horizon]");
  if (!button || button.dataset.horizon === planHorizon) return;
  planHorizon = button.dataset.horizon;
  document.querySelectorAll("#horizon-bar .horizon").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.horizon === planHorizon))
  );
  renderPlans();
});

function schedulePlans(seconds) {
  clearInterval(planTimer);
  // Clamped: a 15s scalp cadence is the floor, and nothing should sit longer
  // than an hour without recomputing even on the position horizon.
  const every = Math.max(15, Math.min(seconds || 300, 3600));
  planTimer = setInterval(renderPlans, every * 1000);
}

function stampPlans(asOf, horizon) {
  const when = asOf ? new Date(asOf) : new Date();
  const every = horizon ? `${horizon.refresh_seconds}s` : "—";
  $("plans-stamp").textContent =
    `${horizon ? horizon.frames.join("/") : ""} · ${when.toLocaleTimeString()} · every ${every}`;
}

async function renderPlans() {
  const list = $("plans-list");
  showSkeleton(list, skeletonRows(4, [24, 62, 38]));
  let payload;
  try {
    payload = await api(
      "/api/plans" + (planHorizon ? `?horizon=${encodeURIComponent(planHorizon)}` : "")
    );
  } catch (_) {
    markFilled(list), list.innerHTML = '<li class="empty">Could not load the playbook.</li>';
    return;
  }
  const plans = payload.plans;
  planHorizon = payload.horizon.key;
  stampPlans(payload.as_of, payload.horizon);
  schedulePlans(payload.horizon.refresh_seconds);
  if (plans.length === 0) {
    markFilled(list), list.innerHTML = '<li class="empty">Add a ticker to see its levels.</li>';
    return;
  }

  markFilled(list), list.innerHTML = plans
    .map((p) => {
      const total = (p.factors || []).length + (p.factors_failed || []).length;
      const levels = levelsRow(p);
      const earnings = p.earnings_at
        ? ` · <span class="signal-warn">EARNINGS ${escapeHtml(
            String(p.earnings_at).slice(0, 10)
          )}</span>`
        : "";
      const measured = measuredLine(p.measured, p.grade);
      return `<li class="signal plan ${p.verdict}" data-plan-ticker="${escapeHtml(p.ticker)}">
        <div class="signal-head">
          <span class="signal-sym">${escapeHtml(p.ticker)}</span>
          <span class="signal-dir long">${escapeHtml(p.direction.toUpperCase())}</span>
          <span class="verdict">${VERDICT_LABEL[p.verdict] || p.verdict}</span>
          <span class="signal-grade">${
            p.grade ? escapeHtml(p.grade) + " · " : ""
          }${p.risk_reward ? Number(p.risk_reward).toFixed(2) + "R · " : ""}${
        total ? `${p.score}/${total} confluence` : ""
      }</span>
        </div>
        ${levels}
        ${sizeLine(p)}
        <div class="signal-meta">${escapeHtml(
          (p.factors || []).join(", ") || "—"
        )} · ${escapeHtml(p.timeframes || "")}</div>
        ${measured ? `<div class="signal-meta">${measured}</div>` : ""}
        <div class="plan-reason">${escapeHtml(p.reason)}${earnings}</div>
      </li>`;
    })
    .join("");
}

$("plans-list").addEventListener("click", (e) => {
  const row = e.target.closest("li[data-plan-ticker]");
  if (row) setActive(row.dataset.planTicker);
});

renderHorizons().then(renderPlans);
// Loaded before the plans render so the share counts are right first time.
renderPositions().then(renderPlans);

// --- Market scanner ----------------------------------------------------------
//
// Stage 1 filters the whole US market server-side on price and 10-day average
// volume. Stage 2 runs the same signal engine over the best of them, so the
// levels here mean exactly what they mean in the playbook.

function fmtVol(v) {
  if (!v) return "—";
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`;
  return String(Math.round(v));
}

function renderScan(payload) {
  const list = $("scanner-list");
  const session = payload.session || {};
  const when = payload.as_of ? new Date(payload.as_of).toLocaleTimeString() : "never";
  $("scanner-status").textContent = `${session.label || ""} · scanned ${when}`;

  // Show the field this session actually ranks on: `premarket_change` is
  // yesterday's gap once the market opens, and displaying it then is a stale
  // number dressed as today's move.
  const moveField = session.ranks_on === "premarket_change"
    ? "premarket_change" : "change";
  const rows = payload.candidates || [];
  if (rows.length === 0) {
    markFilled(list), list.innerHTML =
      '<li class="empty">No stocks match. Loosen the filters or scan again.</li>';
    return;
  }

  markFilled(list), list.innerHTML = rows
    .map((c, i) => {
      const plan = c.plan;
      const move = c[moveField] ?? 0;
      const dir = move >= 0 ? "up" : "down";
      const levels = plan ? levelsRow(plan) : "";
      const measured = plan
        ? `<div class="signal-meta">${measuredLine(c.measured, plan.grade)}</div>`
        : "";
      const verdict = plan
        ? `<span class="verdict">${
            VERDICT_LABEL[plan.verdict] || plan.verdict
          }${plan.grade ? " " + escapeHtml(plan.grade) : ""} · ${plan.score}/7</span>`
        : '<span class="verdict">not graded</span>';
      return `<li class="signal cand ${
        plan && plan.verdict === "tradeable" ? "graded-tradeable" : ""
      }" data-scan-ticker="${escapeHtml(c.ticker)}">
        <div class="signal-head">
          <span class="rank">${i + 1}</span>
          <span class="signal-sym">${escapeHtml(c.ticker)}</span>
          <span class="${dir}">${move >= 0 ? "+" : ""}${Number(move).toFixed(2)}%</span>
          ${verdict}
        </div>
        ${levels}
        ${measured}
        <div class="stats">$${Number(c.price).toFixed(2)} · 10d vol ${fmtVol(
        c.avg_volume_10d
      )}${c.relative_volume ? ` · ${Number(c.relative_volume).toFixed(1)}x rvol` : ""}${
        c.rsi ? ` · RSI ${Number(c.rsi).toFixed(0)}` : ""
      }</div>
        <div class="why">${escapeHtml((c.why || []).join(" · "))}</div>
      </li>`;
    })
    .join("");
}

async function loadScan() {
  showSkeleton($("scanner-list"), skeletonRows(4, [24, 50, 32]),
               "Scanner unavailable.");
  try {
    renderScan(await api("/api/scanner"));
  } catch (_) {
    const list = $("scanner-list");
    if (list && list.querySelector(".skel")) {
      markFilled(list);
      list.innerHTML = '<li class="empty">Scanner unavailable.</li>';
    }
  }
}

async function runScan() {
  const button = $("scan-run");
  button.disabled = true;
  button.textContent = "SCANNING…";
  const price = Number($("scan-price").value) || 15;
  const volume = (Number($("scan-volume").value) || 10) * 1e6;
  try {
    renderScan(
      await api(
        `/api/scanner/refresh?min_price=${price}&min_avg_volume=${volume}`,
        { method: "POST" }
      )
    );
  } catch (_) {
    $("scanner-status").textContent = "scan failed — try again";
  } finally {
    button.disabled = false;
    button.textContent = "SCAN NOW";
  }
}

$("scan-run").addEventListener("click", runScan);
$("scanner-list").addEventListener("click", (e) => {
  const row = e.target.closest("li[data-scan-ticker]");
  if (row) setActive(row.dataset.scanTicker);
});

loadScan();
// The backend rescans every 15 minutes; this just picks up whatever it cached.
setInterval(loadScan, 60 * 1000);

// --- Workspaces: five fixed tabs -------------------------------------------
//
// Nine panels and a dozen function screens on one canvas is more than anyone
// reads at once. Each tab is a job — what is moving, studying one name, placing
// a trade, what you hold, asking questions — and shows only the panels that job
// needs, arranged for it.
//
// Placement is written INLINE because every panel is positioned by an ID
// selector (specificity 1,0,0) that no class-based per-tab rule can outrank.

// Placement is [x, y, w, h] on the 24 x 24 snap lattice. Every layout below
// tiles the canvas completely — no gaps, because an empty region is wasted
// screen, and no overlaps.
const WORKSPACES = [
  {
    key: "market", code: "MARKET", label: "what is moving",
    layout: {
      "watchlist-panel": [0, 0, 6, 16],
      "chart-panel": [6, 0, 18, 16],
      "macro-panel": [0, 16, 6, 8],
      "scanner-panel": [6, 16, 18, 8],
    },
  },
  {
    key: "research", code: "RESEARCH", label: "study one name",
    layout: {
      "watchlist-panel": [0, 0, 5, 12],
      "chart-panel": [5, 0, 12, 14],
      "fundamentals-panel": [17, 0, 7, 24],
      "screener-panel": [0, 12, 5, 12],
      "news-panel": [5, 14, 12, 10],
    },
  },
  {
    key: "trade", code: "TRADE", label: "entries, stops, targets",
    layout: {
      "watchlist-panel": [0, 0, 5, 24],
      "chart-panel": [5, 0, 19, 10],
      "playbook-panel": [5, 10, 12, 14],
      // The plan and what was actually done about it, side by side. That
      // adjacency is the whole argument for the positions panel existing.
      "positions-panel": [17, 10, 7, 10],
      "alerts-panel": [17, 20, 7, 4],
    },
  },
  { key: "portfolio", code: "PORTFOLIO", label: "what you hold", screen: "PORTVIEW" },
  {
    key: "analyst", code: "ANALYST", label: "ask anything",
    layout: {
      "chat-panel": [0, 0, 16, 24],
      "news-panel": [16, 0, 8, 15],
      "watchlist-panel": [16, 15, 8, 9],
    },
  },
];

// Everything past this index is a user-built tab, appended at load.
const BUILTIN_WORKSPACE_COUNT = WORKSPACES.length;

const WORKSPACE_KEY = "diy-terminal-workspace";
const ALL_PANEL_IDS = Array.from(document.querySelectorAll(".grid > .panel")).map(
  (el) => el.id
);

let activeWorkspace = 0;
let overlayScreen = null;   // a command-line screen shown over the workspace

function renderTabs() {
  $("tabstrip-tabs").innerHTML =
    WORKSPACES.map(
      (w, i) => `<div class="tab${w.custom ? " custom" : ""}" role="tab"
           data-tab="${i}"
           aria-selected="${i === activeWorkspace && !overlayScreen}"
           title="${escapeHtml(w.code)} — ${escapeHtml(w.label)}">
        <span class="tab-code">${escapeHtml(w.code)}</span>
        <span class="tab-label">${escapeHtml(w.label)}</span>
        ${
          w.custom
            ? `<button class="tab-close" data-delete-tab="${escapeHtml(
                w.key
              )}" aria-label="Delete this tab" title="Delete this tab">×</button>`
            : ""
        }
      </div>`
    ).join("") +
    (overlayScreen
      ? `<div class="tab" role="tab" data-tab="screen" aria-selected="true"
             title="${escapeHtml(overlayScreen.code)}">
          <span class="tab-code">${escapeHtml(
            overlayScreen.ticker
              ? `${overlayScreen.ticker} ${overlayScreen.code}`
              : overlayScreen.code
          )}</span>
          <span class="tab-label">${escapeHtml(overlayScreen.label || "")}</span>
          <button class="tab-close" data-close-screen aria-label="Close">×</button>
        </div>`
      : "");
}

function applyWorkspace(index, { skipSave = false } = {}) {
  // The boot frames have done their job the moment a real layout exists, and
  // the panels can be shown now that they have somewhere to be.
  const canvas = document.querySelector(".grid");
  if (canvas) canvas.classList.remove("booting");
  const boot = document.getElementById("boot");
  if (boot) boot.remove();

  activeWorkspace = Math.max(0, Math.min(index, WORKSPACES.length - 1));
  overlayScreen = null;
  const workspace = WORKSPACES[activeWorkspace];
  const grid = document.querySelector(".grid");

  if (workspace.screen) {
    grid.hidden = true;
    $("screen").hidden = false;
    renderScreen({ code: workspace.screen, ticker: null, label: workspace.label });
  } else {
    grid.hidden = false;
    $("screen").hidden = true;
    restorePanels();

    // A saved arrangement wins, but only for panels this tab shows: a stale
    // entry for a hidden panel would otherwise resurrect it.
    const saved = readGeometry() || {};
    geometry = {};
    ALL_PANEL_IDS.forEach((id) => {
      const el = $(id);
      const place = workspace.layout[id];
      el.hidden = !place;
      if (!place) {
        el.style.left = el.style.top = el.style.width = el.style.height = "";
        return;
      }
      ensureHandles(el);
      const [x, y, w, h] = place;
      const stored = saved[id];
      geometry[id] = clampGeometry(
        isUsableGeometry(stored) ? { ...stored } : { x, y, w, h }
      );
    });
    paintAll();
  }

  renderTabs();
  if (!skipSave) {
    try {
      localStorage.setItem(WORKSPACE_KEY, workspace.key);
    } catch (_) {}
  }
}

$("tabstrip-tabs").addEventListener("click", (e) => {
  const remove = e.target.closest("[data-delete-tab]");
  if (remove) {
    e.stopPropagation();
    deleteCustomTab(remove.dataset.deleteTab);
    return;
  }
  if (e.target.closest("[data-close-screen]")) {
    e.stopPropagation();
    applyWorkspace(activeWorkspace);
    return;
  }
  const tab = e.target.closest("[data-tab]");
  if (!tab) return;
  if (tab.dataset.tab === "screen") return;
  applyWorkspace(Number(tab.dataset.tab));
});

$("tab-new").addEventListener("click", () => openNewTab());

// --- command-line screens over a workspace ----------------------------------

/* Which security a screen is about.
 *
 * The directory lists functions, not securities, so clicking HDS or OMON there
 * passes no ticker. /api/functions/company/null answers 200 with every field
 * empty, so the screen used to render "null — HOLDERS" over an empty table and
 * look broken. Falling back to whatever is already in focus is what clicking it
 * means.
 */
function resolveTicker(known, ticker, activeTicker) {
  if (ticker) return ticker;
  if (!known || !known.needs_ticker) return null;
  return activeTicker || null;
}

function openScreen(code, ticker) {
  const known = FUNCTION_INDEX[code];
  ticker = resolveTicker(known, ticker, state.activeTicker);
  if (code === "BLP") {
    // The launchpad *is* the panel canvas. Rendering a screen for it would be
    // a screen apologising for not being the thing it stands in front of.
    // Return to the tab already open, unless that tab is itself a screen.
    const here = WORKSPACES[activeWorkspace];
    const canvas = here && here.layout
      ? activeWorkspace
      : WORKSPACES.findIndex((w) => w.layout);
    applyWorkspace(Math.max(canvas, 0));
    if (ticker) setActive(ticker);
    renderTabs();
    return;
  }
  const target = CANVAS_FUNCTIONS[code];
  if (target) {
    // These already exist as panels. Jump to a workspace that shows the panel
    // rather than building a second version of it.
    const home = WORKSPACES.findIndex((w) => w.layout && w.layout[target]);
    if (home >= 0) {
      applyWorkspace(home);
      if (ticker) setActive(ticker);
      maximisePanel($(target));
      return;
    }
  }
  overlayScreen = { code, ticker: ticker || null, label: known ? known.name : "" };
  recordRecent(overlayScreen);
  document.querySelector(".grid").hidden = true;
  $("screen").hidden = false;
  renderTabs();
  renderScreen(overlayScreen);
}

// --- recent screens (LAST) ---------------------------------------------------

const RECENT_KEY = "diy-terminal-recent";

function recordRecent(entry) {
  try {
    const seen = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    const key = `${entry.ticker || ""}|${entry.code}`;
    const next = [entry, ...seen.filter((e) => `${e.ticker || ""}|${e.code}` !== key)];
    localStorage.setItem(RECENT_KEY, JSON.stringify(next.slice(0, 8)));
  } catch (_) {}
}

function recentScreens() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch (_) {
    return [];
  }
}

// --- command line ------------------------------------------------------------

let FUNCTION_INDEX = {};
let cmdSuggestions = [];
let cmdIndex = -1;

async function loadFunctionIndex() {
  try {
    const data = await api("/api/functions");
    data.categories.forEach((c) =>
      c.functions.forEach((f) => {
        FUNCTION_INDEX[f.code] = f;
      })
    );
  } catch (_) {}
}

function closeCmdSuggest() {
  $("cmd-suggest").hidden = true;
  cmdIndex = -1;
}

async function updateCmdSuggest() {
  const value = $("cmd-input").value;
  if (!value.trim()) {
    closeCmdSuggest();
    return;
  }
  let parsed;
  try {
    parsed = await api(`/api/functions/parse?q=${encodeURIComponent(value)}`);
  } catch (_) {
    return;
  }
  cmdSuggestions = parsed.suggestions;
  const list = $("cmd-suggest");
  markFilled(list), list.innerHTML = cmdSuggestions
    .map(
      (f, i) => `<li role="option" data-code="${escapeHtml(f.code)}" id="cmd-opt-${i}">
        <span class="code">${escapeHtml(f.code)}</span>
        <span class="label">${escapeHtml(f.name)}</span>
        <span class="desc">${f.needs_ticker ? "needs a ticker" : ""}</span>
      </li>`
    )
    .join("");
  list.hidden = cmdSuggestions.length === 0;
  cmdIndex = -1;
}

$("cmd-input").addEventListener("input", () => {
  clearTimeout(Number($("cmd-input").dataset.timer));
  $("cmd-input").dataset.timer = String(setTimeout(updateCmdSuggest, 110));
});

$("cmd-input").addEventListener("keydown", (e) => {
  const items = $("cmd-suggest").querySelectorAll("li[data-code]");
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (items.length === 0) return;
    cmdIndex =
      (cmdIndex + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items.forEach((el, i) => el.setAttribute("aria-selected", String(i === cmdIndex)));
    e.preventDefault();
  } else if (e.key === "Escape") {
    closeCmdSuggest();
    $("cmd-input").blur();
  }
});

$("cmd-suggest").addEventListener("click", (e) => {
  const row = e.target.closest("li[data-code]");
  if (!row) return;
  const input = $("cmd-input");
  const words = input.value.trim().split(/\s+/);
  const ticker = words.find((w) => !FUNCTION_INDEX[w.toUpperCase()]);
  input.value = ticker ? `${ticker.toUpperCase()} ${row.dataset.code}` : row.dataset.code;
  closeCmdSuggest();
  $("cmd-form").requestSubmit();
});

$("cmd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const value = $("cmd-input").value.trim();
  if (!value) return;
  const chosen = cmdIndex >= 0 ? cmdSuggestions[cmdIndex].code : null;
  let parsed;
  try {
    parsed = await api(`/api/functions/parse?q=${encodeURIComponent(value)}`);
  } catch (_) {
    return;
  }
  const code = chosen || parsed.code;
  const known = FUNCTION_INDEX[code];
  $("cmd-input").value = "";
  closeCmdSuggest();
  $("cmd-input").blur();
  if (!known || (known.needs_ticker && !parsed.ticker)) {
    openScreen("MAIN", null);
    return;
  }
  openScreen(code, parsed.ticker);
});

document.addEventListener("click", (e) => {
  if (!e.target.closest("#cmd-suggest") && e.target !== $("cmd-input")) {
    closeCmdSuggest();
  }
});

// --- screen renderers --------------------------------------------------------

function money(v) {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if (a >= 1e12) return `${sign}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(0)}k`;
  return `${sign}$${a.toFixed(2)}`;
}

function statementTable(block, title) {
  if (!block || !block.rows || block.rows.length === 0) {
    return `<section><h3>${title}</h3><p class="prose">Not reported for this security.</p></section>`;
  }
  return `<section><h3>${title}</h3>
    <div style="overflow-x:auto"><table>
      <thead><tr><th>Line item</th>${block.periods
        .map((p) => `<th>${escapeHtml(p)}</th>`)
        .join("")}</tr></thead>
      <tbody>${block.rows
        .map(
          (r) => `<tr><td class="label">${escapeHtml(r.label)}</td>${r.values
            .map((v) => `<td>${money(v)}</td>`)
            .join("")}</tr>`
        )
        .join("")}</tbody>
    </table></div></section>`;
}

const SCREENS = {
  async MAIN() {
    const data = await api("/api/functions");
    const recents = recentScreens();
    return `<h2>MAIN — FUNCTION DIRECTORY</h2>
      <p class="screen-sub">Type a code in the command line, or click one below.</p>
      ${
        recents.length
          ? `<section><h3>RECENT (LAST)</h3>${recents
              .map(
                (r) => `<div class="func-row" data-open="${escapeHtml(r.code)}"
                    data-ticker="${escapeHtml(r.ticker || "")}">
                  <span class="code">${escapeHtml(r.code)}</span>
                  <span>${escapeHtml(r.label)}</span>
                  <span class="desc">${escapeHtml(r.ticker || "")}</span>
                </div>`
              )
              .join("")}</section>`
          : ""
      }
      ${data.categories
        .map(
          (c) => `<section><h3>${escapeHtml(c.name.toUpperCase())}</h3>${c.functions
            .map(
              (f) => `<div class="func-row" data-open="${escapeHtml(f.code)}"${
                f.needs_ticker && state.activeTicker
                  ? ` data-ticker="${escapeHtml(state.activeTicker)}"`
                  : ""
              }>
                  <span class="code">${escapeHtml(f.code)}</span>
                  <span>${escapeHtml(f.name)}</span>
                  <span class="desc">${escapeHtml(f.summary)}</span>
                </div>`
            )
            .join("")}</section>`
        )
        .join("")}
      <section><h3>NOT AVAILABLE HERE</h3>
        <p class="prose">These are core Bloomberg functions this terminal
        deliberately does not implement. Each needs a licensed data feed, a
        brokerage connection, or a private network — a code that opens an empty
        screen is worse than one that does not exist.</p>
        ${data.unavailable
          .map(
            (u) => `<div class="func-unavailable"><b>${escapeHtml(
              u.code
            )}</b> — ${escapeHtml(u.reason)}</div>`
          )
          .join("")}
      </section>`;
  },

  async DES(tab) {
    const d = await api(`/api/functions/company/${encodeURIComponent(tab.ticker)}`);
    const f = (await api(`/api/fundamentals/${encodeURIComponent(tab.ticker)}`).catch(
      () => ({ fundamentals: null })
    )).fundamentals;
    const p = d.profile || {};
    const kv = (rows) =>
      `<dl class="kv">${rows
        .map(([k, v]) => `<dt>${k}</dt><dd>${v ?? "—"}</dd>`)
        .join("")}</dl>`;
    return `<h2>${escapeHtml(tab.ticker)} — SECURITY DESCRIPTION</h2>
      <p class="screen-sub">${escapeHtml(p.name || "")}${
      p.exchange ? " · " + escapeHtml(p.exchange) : ""
    }</p>
      <div class="screen-grid">
        <section><h3>PROFILE</h3>${kv([
          ["Country", escapeHtml(p.country || "")],
          ["City", escapeHtml(p.city || "")],
          ["Employees", p.employees ? Number(p.employees).toLocaleString() : "—"],
          ["Currency", escapeHtml(p.currency || "")],
          ["Sector", escapeHtml(f?.sector || "")],
          ["Industry", escapeHtml(f?.industry || "")],
        ])}</section>
        <section><h3>VALUATION</h3>${kv([
          ["Market cap", money(f?.market_cap)],
          ["P/E", f?.pe_ratio?.toFixed?.(2) ?? "—"],
          ["Forward P/E", f?.forward_pe?.toFixed?.(2) ?? "—"],
          ["P/B", f?.price_to_book?.toFixed?.(2) ?? "—"],
          ["EPS", f?.eps?.toFixed?.(2) ?? "—"],
          ["Beta", f?.beta?.toFixed?.(2) ?? "—"],
        ])}</section>
        <section><h3>52 WEEK</h3>${kv([
          ["High", f?.week52_high?.toFixed?.(2) ?? "—"],
          ["Low", f?.week52_low?.toFixed?.(2) ?? "—"],
          ["50d avg", f?.ma50?.toFixed?.(2) ?? "—"],
          ["200d avg", f?.ma200?.toFixed?.(2) ?? "—"],
          ["Avg volume", f?.avg_volume ? fmtVol(f.avg_volume) : "—"],
        ])}</section>
      </div>
      ${
        p.summary
          ? `<section><h3>BUSINESS</h3><p class="prose">${escapeHtml(
              p.summary
            )}</p></section>`
          : ""
      }`;
  },

  async FA(tab) {
    const d = await api(`/api/functions/company/${encodeURIComponent(tab.ticker)}`);
    return `<h2>${escapeHtml(tab.ticker)} — FINANCIAL ANALYSIS</h2>
      <p class="screen-sub">Annual statements as reported, most recent first.</p>
      ${statementTable(d.income_statement, "INCOME STATEMENT")}
      ${statementTable(d.balance_sheet, "BALANCE SHEET")}
      ${statementTable(d.cashflow, "CASH FLOW")}`;
  },

  async EE(tab) {
    const d = await api(`/api/functions/company/${encodeURIComponent(tab.ticker)}`);
    return `<h2>${escapeHtml(tab.ticker)} — EARNINGS ESTIMATES</h2>
      <p class="screen-sub">Analyst consensus. These are forecasts, not results.</p>
      ${statementTable(d.earnings_estimate, "EPS ESTIMATE")}
      ${statementTable(d.revenue_estimate, "REVENUE ESTIMATE")}`;
  },

  async ANR(tab) {
    const d = await api(`/api/functions/company/${encodeURIComponent(tab.ticker)}`);
    const recs = d.recommendations || [];
    const t = d.price_targets || {};
    const bar = (r) => {
      const total =
        r.strong_buy + r.buy + r.hold + r.sell + r.strong_sell || 1;
      const bulls = r.strong_buy + r.buy;
      const bears = r.sell + r.strong_sell;
      return `${((bulls / total) * 100).toFixed(0)}% bull / ${(
        (bears / total) *
        100
      ).toFixed(0)}% bear`;
    };
    return `<h2>${escapeHtml(tab.ticker)} — ANALYST RECOMMENDATIONS</h2>
      <p class="screen-sub">Ratings by month, newest first.</p>
      <section><h3>PRICE TARGETS</h3>
        <dl class="kv">
          <dt>Current</dt><dd>${t.current?.toFixed?.(2) ?? "—"}</dd>
          <dt>Mean target</dt><dd>${t.mean?.toFixed?.(2) ?? "—"}</dd>
          <dt>Median target</dt><dd>${t.median?.toFixed?.(2) ?? "—"}</dd>
          <dt>High / Low</dt><dd>${t.high?.toFixed?.(2) ?? "—"} / ${
      t.low?.toFixed?.(2) ?? "—"
    }</dd>
        </dl>
      </section>
      <section><h3>RATINGS</h3>
        <table><thead><tr><th>Period</th><th>Strong buy</th><th>Buy</th>
          <th>Hold</th><th>Sell</th><th>Strong sell</th><th>Split</th></tr></thead>
        <tbody>${recs
          .map(
            (r) => `<tr><td class="label">${escapeHtml(r.period)}</td>
              <td>${r.strong_buy}</td><td>${r.buy}</td><td>${r.hold}</td>
              <td>${r.sell}</td><td>${r.strong_sell}</td>
              <td>${bar(r)}</td></tr>`
          )
          .join("")}</tbody></table>
      </section>`;
  },

  async HDS(tab) {
    const d = await api(`/api/functions/company/${encodeURIComponent(tab.ticker)}`);
    const holders = d.institutional_holders || [];
    if (holders.length === 0)
      return `<h2>${escapeHtml(tab.ticker)} — HOLDERS</h2>
        <p class="prose">No institutional holder data for this security.</p>`;
    return `<h2>${escapeHtml(tab.ticker)} — HOLDERS</h2>
      <p class="screen-sub">Largest institutional positions as last reported.</p>
      <table><thead><tr><th>Holder</th><th>Shares</th><th>% held</th>
        <th>Value</th></tr></thead>
      <tbody>${holders
        .map(
          (h) => `<tr><td class="label">${escapeHtml(h.holder)}</td>
            <td>${Number(h.shares).toLocaleString()}</td>
            <td>${(h.pct_held * 100).toFixed(2)}%</td>
            <td>${money(h.value)}</td></tr>`
        )
        .join("")}</tbody></table>`;
  },

  async IMAP() {
    let d = await api("/api/functions/sectors");
    if (!d.sectors.length) d = await api("/api/functions/sectors/refresh", { method: "POST" });
    const widest = Math.max(...d.sectors.map((s) => Math.abs(s.change)), 0.5);
    return `<h2>IMAP — SECTOR MAP</h2>
      <p class="screen-sub">Cap-weighted move across ${
        d.constituents ? d.constituents.toLocaleString() : "—"
      } companies over $2B. Weighted, not averaged: an equal-weight mean lets
      micro caps swing a sector that trillions barely moved.</p>
      ${d.sectors
        .map((s) => {
          const pct = (Math.abs(s.change) / widest) * 50;
          return `<div class="sector-row">
            <span>${escapeHtml(s.sector)}</span>
            <span class="sector-bar"><span class="${
              s.change >= 0 ? "up" : "down"
            }" style="width:${pct.toFixed(1)}%"></span></span>
            <span class="${s.change >= 0 ? "up" : "down"}">${
            s.change >= 0 ? "+" : ""
          }${s.change.toFixed(2)}%</span>
          </div>`;
        })
        .join("")}`;
  },

  async FACT() {
    let d = await api("/api/factors");
    if (!d.factors.length) {
      $("screen").innerHTML =
        '<h2>FACT — FACTOR STUDY</h2><p class="prose">Replaying every cached '
        + 'ticker. This takes a minute or two.</p>';
      d = await api("/api/factors/refresh", { method: "POST" });
    }
    if (!d.factors.length) {
      return `<h2>FACT — FACTOR STUDY</h2>
        <p class="prose">Not enough cached daily bars yet. Add tickers and let
        the poller fill them in.</p>`;
    }

    const pct = (v) => `${(v * 100).toFixed(1)}%`;
    const signed = (v) => (v >= 0 ? `+${v.toFixed(3)}` : v.toFixed(3));
    const bestScore = d.by_score.reduce(
      (a, b) => (b.avg_r > a.avg_r ? b : a), d.by_score[0]
    );

    return `<h2>FACT — FACTOR STUDY</h2>
      <p class="screen-sub">Every factor's record with it present versus absent,
      replayed over ${d.resolved.toLocaleString()} resolved setups across
      ${d.tickers} tickers. Computed ${
      d.as_of ? new Date(d.as_of).toLocaleString() : "—"
    }.</p>

      <section><h3>MARGINAL CONTRIBUTION</h3>
      <p class="screen-sub" style="margin-bottom:6px">The engine scores all
      seven as if they weigh the same. Sorted by the R they actually add.</p>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Factor</th>
          <th>With — win</th><th>With — R</th><th>n</th>
          <th>Without — win</th><th>Without — R</th><th>n</th>
          <th>Win edge</th><th>R edge</th></tr></thead>
        <tbody>${d.factors
          .map(
            (f) => `<tr>
            <td class="label">${escapeHtml(f.factor)}</td>
            <td>${pct(f.with.win_rate)}</td><td>${signed(f.with.avg_r)}R</td>
            <td>${f.with.n.toLocaleString()}</td>
            <td>${pct(f.without.win_rate)}</td><td>${signed(f.without.avg_r)}R</td>
            <td>${f.without.n.toLocaleString()}</td>
            <td class="${f.win_edge_pp >= 0 ? "up" : "down"}">${
              f.win_edge_pp >= 0 ? "+" : ""
            }${f.win_edge_pp.toFixed(1)}pp</td>
            <td class="${f.r_edge >= 0 ? "up" : "down"}">${signed(f.r_edge)}R</td>
          </tr>`
          )
          .join("")}</tbody>
      </table></div>
      <p class="settings-hint" style="padding:6px 0 0">${escapeHtml(d.caveat || "")}</p>
      </section>

      <section><h3>DOES MORE CONFLUENCE HELP?</h3>
      <p class="screen-sub" style="margin-bottom:6px">If the seven-factor score
      meant what the grades imply, this column would rise all the way down.</p>
      <table>
        <thead><tr><th>Score</th><th>Setups</th><th>Win rate</th>
          <th>Average R</th></tr></thead>
        <tbody>${d.by_score
          .map(
            (s) => `<tr>
            <td class="label">${s.score} of 7</td>
            <td>${s.n.toLocaleString()}</td>
            <td>${pct(s.win_rate)}</td>
            <td class="${s.avg_r >= 0 ? "up" : "down"}">${signed(s.avg_r)}R</td>
          </tr>`
          )
          .join("")}</tbody>
      </table>
      <p class="prose" style="margin-top:8px">Best measured score:
      <b>${bestScore.score} of 7</b> at ${signed(bestScore.avg_r)}R. A 2:1
      target breaks even at 33.3%, so a positive average R across every score
      is the payoff structure earning it, not the selection.</p>
      </section>

      <section><h3>WHAT THIS DOES NOT DO</h3>
      <p class="prose">Nothing here has been fed back into the engine. Dropping
      the factors that measure badly would be fitting the thresholds to this
      sample — 19 correlated large-cap names over two years — which is the
      mistake the backtest exists to catch. Treat it as a question to
      investigate, not a verdict to act on.</p>
      </section>`;
  },

  async OMON(tab) {
    const url = (expiry) =>
      `/api/options/${encodeURIComponent(tab.ticker)}` +
      (expiry ? `?expiry=${encodeURIComponent(expiry)}` : "");
    const d = await api(url(tab.expiry));

    if (!d.expirations.length) {
      return `<h2>${escapeHtml(tab.ticker)} — OPTION MONITOR</h2>
        <p class="prose">${escapeHtml(d.reason || "No listed options.")}</p>`;
    }

    const s = d.summary || {};
    const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
    const num = (v, dp = 2) =>
      v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined,
        { minimumFractionDigits: dp, maximumFractionDigits: dp });

    // Strikes shared by both sides, centred on spot: a full ladder is mostly
    // untraded wings, and the rows near the money are the ones anyone reads.
    const strikes = [...new Set([...d.calls, ...d.puts].map((r) => r.strike))]
      .sort((a, b) => a - b);
    const spot = s.spot;
    let window = strikes;
    if (spot) {
      const nearest = strikes.reduce(
        (best, k) => (Math.abs(k - spot) < Math.abs(best - spot) ? k : best),
        strikes[0]
      );
      const at = strikes.indexOf(nearest);
      window = strikes.slice(Math.max(0, at - 12), at + 13);
    }
    const callBy = Object.fromEntries(d.calls.map((r) => [r.strike, r]));
    const putBy = Object.fromEntries(d.puts.map((r) => [r.strike, r]));

    const side = (r, key, dp) => (r ? num(r[key], dp) : "—");
    const rows = window
      .map((k) => {
        const c = callBy[k];
        const p = putBy[k];
        const atm = spot && k === window.reduce(
          (b, x) => (Math.abs(x - spot) < Math.abs(b - spot) ? x : b), window[0]);
        return `<tr class="${atm ? "atm" : ""}">
          <td>${side(c, "volume", 0)}</td>
          <td>${side(c, "open_interest", 0)}</td>
          <td>${c && c.iv ? pct(c.iv) : "—"}</td>
          <td>${side(c, "bid")}</td><td>${side(c, "ask")}</td>
          <td class="strike">${num(k)}</td>
          <td>${side(p, "bid")}</td><td>${side(p, "ask")}</td>
          <td>${p && p.iv ? pct(p.iv) : "—"}</td>
          <td>${side(p, "open_interest", 0)}</td>
          <td>${side(p, "volume", 0)}</td>
        </tr>`;
      })
      .join("");

    const pcRatio = s.put_call_volume;
    return `<h2>${escapeHtml(tab.ticker)} — OPTION MONITOR</h2>
      <p class="screen-sub">Expiring ${escapeHtml(d.expiry)} ·
        ${d.days_to_expiry} day${d.days_to_expiry === 1 ? "" : "s"} ·
        ${d.calls.length} calls, ${d.puts.length} puts</p>

      <div class="chips" role="tablist" aria-label="Expiry"
           style="padding:0 0 10px">${d.expirations
        .slice(0, 12)
        .map(
          (e) => `<button type="button" class="chip" role="tab"
             data-expiry="${escapeHtml(e)}"
             aria-selected="${e === d.expiry}">${escapeHtml(e.slice(5))}</button>`
        )
        .join("")}</div>

      <div class="screen-grid">
        <section><h3>WHAT THE MARKET EXPECTS</h3><dl class="kv">
          <dt>Spot</dt><dd>${num(s.spot)}</dd>
          <dt>ATM implied volatility</dt><dd>${pct(s.atm_iv)}</dd>
          <dt>Implied move by expiry</dt><dd>${
            s.implied_move_pct ? `±${s.implied_move_pct}% (±${num(s.implied_move_abs)})` : "—"
          }</dd>
          <dt>Max pain</dt><dd>${num(s.max_pain)}</dd>
        </dl>
        <p class="settings-hint" style="padding:6px 0 0">The implied move is the
        at-the-money straddle: what the market charges for a move in either
        direction. Max pain is a folk measure — it is here because every options
        screen has it, not because price is shown to gravitate to it.</p>
        </section>

        <section><h3>POSITIONING</h3><dl class="kv">
          <dt>Call volume</dt><dd>${num(s.call_volume, 0)}</dd>
          <dt>Put volume</dt><dd>${num(s.put_volume, 0)}</dd>
          <dt>Put/call — volume</dt><dd class="${
            pcRatio === null ? "" : pcRatio > 1 ? "down" : "up"
          }">${pcRatio === null ? "—" : pcRatio}</dd>
          <dt>Call open interest</dt><dd>${num(s.call_open_interest, 0)}</dd>
          <dt>Put open interest</dt><dd>${num(s.put_open_interest, 0)}</dd>
          <dt>Put/call — open interest</dt><dd>${
            s.put_call_open_interest === null ? "—" : s.put_call_open_interest
          }</dd>
        </dl></section>
      </div>

      <section><h3>CHAIN</h3>
      <div style="overflow-x:auto"><table class="chain">
        <thead>
          <tr><th colspan="5" class="calls-head">CALLS</th>
              <th></th>
              <th colspan="5" class="puts-head">PUTS</th></tr>
          <tr><th>Vol</th><th>OI</th><th>IV</th><th>Bid</th><th>Ask</th>
              <th class="strike">Strike</th>
              <th>Bid</th><th>Ask</th><th>IV</th><th>OI</th><th>Vol</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="settings-hint" style="padding:6px 0 0">Strikes around the money.
      Nothing here is a fair value: pricing an option needs a rate, a dividend
      assumption and a view on volatility, and a number built on those guesses
      would look more authoritative than it is.</p>
      </section>`;
  },

  async NSE() {
    const d = await api("/api/functions/news-search?limit=60");
    return `<h2>NSE — NEWS SEARCH</h2>
      <p class="screen-sub">Every cached headline across all tickers.</p>
      <input id="nse-q" class="modal-search" style="margin:0 0 10px" type="text"
             placeholder="Filter headlines…">
      <div id="nse-results">${newsRows(d.articles)}</div>`;
  },

  async PORT() {
    const d = await api("/api/functions/portfolio");
    return `<h2>PORT — PORTFOLIO</h2>
      <p class="screen-sub">The terminal tracks a watchlist, not positions, so
      weights are equal by construction — ${d.equal_weight_pct}% each. A weight
      column implying real allocation would be a fiction.</p>
      <div class="screen-grid">
        <section><h3>AGGREGATE</h3><dl class="kv">
          <dt>Holdings</dt><dd>${d.holdings.length}</dd>
          <dt>Average beta</dt><dd>${d.portfolio_beta ?? "—"}</dd>
          <dt>Day change (equal weight)</dt><dd class="${
            (d.day_change_pct ?? 0) >= 0 ? "up" : "down"
          }">${d.day_change_pct ?? "—"}%</dd>
        </dl></section>
        <section><h3>SECTOR EXPOSURE</h3><dl class="kv">${d.sector_exposure
          .map(
            (s) => `<dt>${escapeHtml(s.sector)}</dt><dd>${s.weight_pct}%</dd>`
          )
          .join("")}</dl></section>
      </div>
      <section><h3>HOLDINGS</h3>
        <table><thead><tr><th>Ticker</th><th>Price</th><th>Change</th>
          <th>Sector</th><th>Beta</th><th>Market cap</th></tr></thead>
        <tbody>${d.holdings
          .map(
            (h) => `<tr><td class="label">${escapeHtml(h.ticker)}</td>
              <td>${h.price?.toFixed?.(2) ?? "—"}</td>
              <td class="${(h.change_pct ?? 0) >= 0 ? "up" : "down"}">${
              h.change_pct?.toFixed?.(2) ?? "—"
            }%</td>
              <td>${escapeHtml(h.sector || "—")}</td>
              <td>${h.beta?.toFixed?.(2) ?? "—"}</td>
              <td>${money(h.market_cap)}</td></tr>`
          )
          .join("")}</tbody></table>
      </section>`;
  },

  async HELP() {
    return `<h2>HELP — COMMANDS & KEYBOARD</h2>
      <p class="screen-sub">The command line takes a ticker, a function code, or
      both in either order: <b>AAPL DES</b> and <b>DES AAPL</b> are the same. A
      bare ticker opens DES; a bare code opens that screen.</p>
      <div class="screen-grid">
        <section><h3>KEYBOARD</h3><dl class="kv">
          <dt>/</dt><dd>Search symbols</dd>
          <dt>a</dt><dd>New price alert</dd>
          <dt>c</dt><dd>Ask the analyst</dd>
          <dt>f</dt><dd>Fullscreen</dd>
          <dt>x</dt><dd>Maximise the panel under the cursor</dd>
          <dt>Esc</dt><dd>Close / un-maximise</dd>
        </dl></section>
        <section><h3>TABS</h3><p class="prose">Every function opens in its own
        tab. The first tab is the panel canvas and cannot be closed;
        middle-click or × closes any other. <b>+</b> opens the directory.</p>
        </section>
      </div>`;
  },
};

SCREENS.LAST = async function () {
  const recent = recentScreens();
  if (!recent.length) {
    return `<h2>LAST — RECENT SCREENS</h2>
      <p class="screen-sub">Nothing opened yet. Every screen you open is listed
      here, most recent first.</p>`;
  }
  const rows = recent
    .map(
      (e) => `<tr data-open="${escapeHtml(e.code)}"${
        e.ticker ? ` data-ticker="${escapeHtml(e.ticker)}"` : ""
      }>
        <td>${escapeHtml(e.code)}</td>
        <td>${escapeHtml(e.ticker || "—")}</td>
        <td>${escapeHtml(e.label || "")}</td>
      </tr>`
    )
    .join("");
  return `<h2>LAST — RECENT SCREENS</h2>
    <p class="screen-sub">The screens you have opened, most recent first. Click
    one to go back to it.</p>
    <table class="chain fn-table"><thead><tr>
      <th>CODE</th><th>TICKER</th><th>SCREEN</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
};

// The PORTFOLIO tab: holdings and where the market moved today, together.
SCREENS.PORTVIEW = async function () {
  const [port, map] = await Promise.all([
    api("/api/functions/portfolio"),
    api("/api/functions/sectors").then((d) =>
      d.sectors.length ? d : api("/api/functions/sectors/refresh", { method: "POST" })
    ).catch(() => ({ sectors: [] })),
  ]);
  const widest = Math.max(...map.sectors.map((s) => Math.abs(s.change)), 0.5);
  const held = new Set(port.sector_exposure.map((s) => s.sector));
  return `<h2>PORTFOLIO</h2>
    <p class="screen-sub">The terminal tracks a watchlist, not positions, so
    weights are equal by construction — ${port.equal_weight_pct}% each. A weight
    column implying real allocation would be a fiction.</p>
    <div class="screen-grid">
      <section><h3>AGGREGATE</h3><dl class="kv">
        <dt>Holdings</dt><dd>${port.holdings.length}</dd>
        <dt>Average beta</dt><dd>${port.portfolio_beta ?? "—"}</dd>
        <dt>Day change (equal weight)</dt><dd class="${
          (port.day_change_pct ?? 0) >= 0 ? "up" : "down"
        }">${port.day_change_pct ?? "—"}%</dd>
      </dl></section>
      <section><h3>SECTOR EXPOSURE</h3><dl class="kv">${port.sector_exposure
        .map((s) => `<dt>${escapeHtml(s.sector)}</dt><dd>${s.weight_pct}%</dd>`)
        .join("")}</dl></section>
    </div>
    <section><h3>HOLDINGS</h3>
      <table><thead><tr><th>Ticker</th><th>Price</th><th>Change</th>
        <th>Sector</th><th>Beta</th><th>Market cap</th></tr></thead>
      <tbody>${port.holdings
        .map(
          (h) => `<tr><td class="label">${escapeHtml(h.ticker)}</td>
            <td>${h.price?.toFixed?.(2) ?? "—"}</td>
            <td class="${(h.change_pct ?? 0) >= 0 ? "up" : "down"}">${
            h.change_pct?.toFixed?.(2) ?? "—"
          }%</td>
            <td>${escapeHtml(h.sector || "—")}</td>
            <td>${h.beta?.toFixed?.(2) ?? "—"}</td>
            <td>${money(h.market_cap)}</td></tr>`
        )
        .join("")}</tbody></table>
    </section>
    <section><h3>SECTOR MAP — WHERE THE MARKET MOVED</h3>
      <p class="screen-sub" style="margin-bottom:6px">Sectors you hold are
      marked. Cap-weighted across ${
        map.constituents ? map.constituents.toLocaleString() : "—"
      } companies over $2B.</p>
      ${map.sectors
        .map((s) => {
          const pct = (Math.abs(s.change) / widest) * 50;
          return `<div class="sector-row">
            <span>${held.has(s.sector) ? "▸ " : ""}${escapeHtml(s.sector)}</span>
            <span class="sector-bar"><span class="${
              s.change >= 0 ? "up" : "down"
            }" style="width:${pct.toFixed(1)}%"></span></span>
            <span class="${s.change >= 0 ? "up" : "down"}">${
            s.change >= 0 ? "+" : ""
          }${s.change.toFixed(2)}%</span>
          </div>`;
        })
        .join("")}
    </section>`;
};

function newsRows(articles) {
  if (!articles.length) return '<p class="prose">No headlines cached yet.</p>';
  return articles
    .map((a) => {
      const dead = a.link_status === "dead" ? " dead-link" : "";
      return `<div style="padding:3px 0;border-bottom:var(--hairline) solid var(--border)">
        <a class="news-headline${dead}" href="${escapeHtml(
        a.url || "#"
      )}" target="_blank" rel="noopener">${escapeHtml(a.title || "")}</a>
        <div class="news-meta">${escapeHtml(a.ticker)} · ${escapeHtml(
        a.publisher || ""
      )} · ${a.published_at ? new Date(a.published_at).toLocaleDateString() : ""}</div>
        ${
          a.ai_summary
            ? `<div class="news-ai"><span class="tag ${escapeHtml(
                a.sentiment || "neutral"
              )}">${escapeHtml(
                (a.sentiment || "neutral").toUpperCase()
              )}</span>${escapeHtml(a.ai_summary)}</div>`
            : ""
        }
      </div>`;
    })
    .join("");
}

// Functions that live on the canvas rather than a screen of their own: opening
// them jumps to the canvas and maximises the panel that already does the job.
const CANVAS_FUNCTIONS = {
  EQS: "screener-panel", SCAN: "scanner-panel", GP: "chart-panel",
  N: "news-panel", PLAY: "playbook-panel", SIG: "alerts-panel",
  ECO: "macro-panel", ALRT: "alerts-panel", CHAT: "chat-panel",
};

async function renderScreen(tab) {
  const screen = $("screen");
  // The overlay is replaced wholesale each time, so unlike the panels it
  // has nothing to preserve and always shows the skeleton.
  screen.innerHTML = skeletonScreen();
  const known = FUNCTION_INDEX[tab.code];
  if (known && known.needs_ticker && !tab.ticker) {
    // /api/functions/company/null answers 200 with every field empty, so this
    // used to render "null — HOLDERS" over an empty table and look broken.
    screen.innerHTML = `<h2>${escapeHtml(tab.code)} — ${escapeHtml(
      known.name.toUpperCase()
    )}</h2>
      <p class="screen-sub">${escapeHtml(known.summary)}</p>
      <p class="prose">This screen is about one security, and none is
      selected. Pick a ticker in the watchlist, or type
      <span class="code">AAPL ${escapeHtml(tab.code)}</span> in the command
      line.</p>`;
    return;
  }

  const render = SCREENS[tab.code];
  if (!render) {
    screen.innerHTML = `<h2>${escapeHtml(tab.code)}</h2>
      <p class="prose">This function has no screen yet. Type MAIN for the
      directory.</p>`;
    return;
  }
  try {
    screen.innerHTML = await render(tab);
  } catch (err) {
    screen.innerHTML = `<h2>${escapeHtml(tab.code)}</h2>
      <p class="prose">Could not load this screen: ${escapeHtml(
        err.message || "unknown error"
      )}</p>`;
  }
}

$("screen").addEventListener("click", (e) => {
  const expiry = e.target.closest("[data-expiry]");
  if (expiry && overlayScreen) {
    overlayScreen.expiry = expiry.dataset.expiry;
    renderScreen(overlayScreen);
    return;
  }
  const row = e.target.closest("[data-open]");
  if (row) openScreen(row.dataset.open, row.dataset.ticker || null);
});

$("screen").addEventListener("input", async (e) => {
  if (e.target.id !== "nse-q") return;
  const d = await api(
    `/api/functions/news-search?limit=60&q=${encodeURIComponent(e.target.value)}`
  );
  $("nse-results").innerHTML = newsRows(d.articles);
});

// Restore custom tabs and the last workspace.
loadFunctionIndex().then(() => {
  refreshWorkspaces();
  let index = 0;
  try {
    const saved = localStorage.getItem(WORKSPACE_KEY);
    const found = WORKSPACES.findIndex((w) => w.key === saved);
    if (found >= 0) index = found;
  } catch (_) {}
  applyWorkspace(index);
});

// --- Custom tabs -------------------------------------------------------------
//
// The five built-in tabs cover the jobs I could anticipate. This is for the
// ones I could not: pick a template, pick panels, name it.
//
// A template is a list of slots on the same 24x24 lattice everything else
// uses, so a custom tab is dragged and resized exactly like a built-in one.

const PANEL_CATALOGUE = [
  { id: "chart-panel", name: "Chart", what: "price" },
  { id: "watchlist-panel", name: "Watchlist", what: "tickers" },
  { id: "fundamentals-panel", name: "Fundamentals", what: "ratios" },
  { id: "news-panel", name: "News", what: "headlines" },
  { id: "screener-panel", name: "Screener", what: "filter" },
  { id: "scanner-panel", name: "Scanner", what: "movers" },
  { id: "alerts-panel", name: "Alerts", what: "price alerts" },
  { id: "playbook-panel", name: "Playbook", what: "entry/stop/targets" },
  { id: "positions-panel", name: "Positions", what: "what you hold" },
  { id: "macro-panel", name: "Macro Calendar", what: "releases" },
  { id: "chat-panel", name: "Analyst", what: "chat" },
];

// Slots are [x, y, w, h]. Every template tiles the full lattice, so a custom
// tab cannot ship with the dead space the built-ins are careful to avoid.
const TEMPLATES = [
  { key: "focus", name: "FOCUS + RAIL", cols: "2fr 1fr", rows: "1fr",
    slots: [[0, 0, 16, 24], [16, 0, 8, 24]] },
  { key: "quad", name: "QUARTERS", cols: "1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 12, 12], [12, 0, 12, 12], [0, 12, 12, 12], [12, 12, 12, 12]] },
  { key: "thirds", name: "THREE COLUMNS", cols: "1fr 1fr 1fr", rows: "1fr",
    slots: [[0, 0, 8, 24], [8, 0, 8, 24], [16, 0, 8, 24]] },
  { key: "main-rail", name: "MAIN + 2 RAIL", cols: "2fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 16, 24], [16, 0, 8, 12], [16, 12, 8, 12]] },
  { key: "stack", name: "TOP + BOTTOM", cols: "1fr", rows: "1fr 1fr",
    slots: [[0, 0, 24, 12], [0, 12, 24, 12]] },
  { key: "rail-main-rail", name: "RAIL + MAIN + RAIL", cols: "1fr 2fr 1fr",
    rows: "1fr", slots: [[0, 0, 6, 24], [6, 0, 12, 24], [18, 0, 6, 24]] },
  { key: "single", name: "SINGLE", cols: "1fr", rows: "1fr",
    slots: [[0, 0, 24, 24]] },

  { key: "main-3rail", name: "MAIN + 3 RAIL", cols: "2fr 1fr", rows: "1fr 1fr 1fr",
    slots: [[0, 0, 16, 24], [16, 0, 8, 8], [16, 8, 8, 8], [16, 16, 8, 8]] },
  { key: "top-2bottom", name: "TOP + 2 BELOW", cols: "1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 24, 12], [0, 12, 12, 12], [12, 12, 12, 12]] },
  { key: "2top-bottom", name: "2 ABOVE + BASE", cols: "1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 12, 12], [12, 0, 12, 12], [0, 12, 24, 12]] },
  { key: "six", name: "SIX (3x2)", cols: "1fr 1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 8, 12], [8, 0, 8, 12], [16, 0, 8, 12],
            [0, 12, 8, 12], [8, 12, 8, 12], [16, 12, 8, 12]] },
  { key: "half-2", name: "HALF + 2", cols: "1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 12, 24], [12, 0, 12, 12], [12, 12, 12, 12]] },
  { key: "l-shape", name: "L-SHAPE", cols: "2fr 1fr", rows: "2fr 1fr",
    slots: [[0, 0, 16, 16], [16, 0, 8, 16], [0, 16, 24, 8]] },
  { key: "header-3", name: "HEADER + 3", cols: "1fr 1fr 1fr", rows: "1fr 2fr",
    slots: [[0, 0, 24, 8], [0, 8, 8, 16], [8, 8, 8, 16], [16, 8, 8, 16]] },
  { key: "3-footer", name: "3 + FOOTER", cols: "1fr 1fr 1fr", rows: "2fr 1fr",
    slots: [[0, 0, 8, 16], [8, 0, 8, 16], [16, 0, 8, 16], [0, 16, 24, 8]] },
  { key: "center-stage", name: "CENTRE STAGE", cols: "1fr 2fr 1fr", rows: "2fr 1fr",
    slots: [[0, 0, 6, 16], [6, 0, 12, 16], [18, 0, 6, 16], [0, 16, 24, 8]] },
  { key: "eight", name: "EIGHT (4x2)", cols: "1fr 1fr 1fr 1fr", rows: "1fr 1fr",
    slots: [[0, 0, 6, 12], [6, 0, 6, 12], [12, 0, 6, 12], [18, 0, 6, 12],
            [0, 12, 6, 12], [6, 12, 6, 12], [12, 12, 6, 12], [18, 12, 6, 12]] },
  { key: "nine", name: "NINE (3x3)", cols: "1fr 1fr 1fr", rows: "1fr 1fr 1fr",
    slots: [[0, 0, 8, 8], [8, 0, 8, 8], [16, 0, 8, 8],
            [0, 8, 8, 8], [8, 8, 8, 8], [16, 8, 8, 8],
            [0, 16, 8, 8], [8, 16, 8, 8], [16, 16, 8, 8]] },
  { key: "four-rows", name: "FOUR ROWS", cols: "1fr", rows: "1fr 1fr 1fr 1fr",
    slots: [[0, 0, 24, 6], [0, 6, 24, 6], [0, 12, 24, 6], [0, 18, 24, 6]] },
];

// A custom template is generated rather than listed: pick columns and rows and
// it tiles evenly. 24 is divisible by 1, 2, 3, 4 and 6, so every combination
// lands on whole lattice units with nothing left over — which is exactly why
// the lattice is 24 and not 20 or 25.
const CUSTOM_DIVISORS = [1, 2, 3, 4, 6];

function buildCustomTemplate(columns, rows) {
  const cols = CUSTOM_DIVISORS.includes(columns) ? columns : 2;
  const rws = CUSTOM_DIVISORS.includes(rows) ? rows : 2;
  const w = SNAP_X / cols;
  const h = SNAP_Y / rws;
  const slots = [];
  for (let r = 0; r < rws; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      slots.push([c * w, r * h, w, h]);
    }
  }
  return {
    key: "custom-grid",
    name: `CUSTOM ${cols}x${rws}`,
    cols: `repeat(${cols}, 1fr)`,
    rows: `repeat(${rws}, 1fr)`,
    slots,
    generated: true,
  };
}

const CUSTOM_TABS_KEY = "diy-terminal-custom-tabs.v1";

function readCustomTabs() {
  try {
    const raw = JSON.parse(localStorage.getItem(CUSTOM_TABS_KEY) || "[]");
    // Only keep entries that still name real panels and a known template: a
    // tab referring to something that no longer exists renders empty.
    return raw.filter(
      (t) =>
        t && t.key && t.code && Array.isArray(t.panels) &&
        t.panels.every((id) => PANEL_CATALOGUE.some((p) => p.id === id))
    );
  } catch (_) {
    return [];
  }
}

function saveCustomTabs(list) {
  try {
    localStorage.setItem(CUSTOM_TABS_KEY, JSON.stringify(list));
  } catch (_) {}
}

function customToWorkspace(tab) {
  // A generated grid is rebuilt from the stored dimensions rather than stored
  // as slots, so a saved tab cannot drift from what the generator produces.
  const template = tab.template === "custom-grid"
    ? buildCustomTemplate(tab.cols || 2, tab.rows || 2)
    : TEMPLATES.find((t) => t.key === tab.template) || TEMPLATES[0];
  const layout = {};
  tab.panels.forEach((id, i) => {
    const slot = template.slots[i];
    if (slot) layout[id] = slot;
  });
  return {
    key: tab.key, code: tab.code, label: tab.label || "custom",
    layout, custom: true,
  };
}

function refreshWorkspaces() {
  WORKSPACES.length = BUILTIN_WORKSPACE_COUNT;
  readCustomTabs().forEach((t) => WORKSPACES.push(customToWorkspace(t)));
}

// --- the builder -------------------------------------------------------------

let draftTemplate = TEMPLATES[0];
let draftPanels = [];
let customCols = 2;
let customRows = 2;

function allTemplates() {
  return [...TEMPLATES, buildCustomTemplate(customCols, customRows)];
}

function renderTemplates() {
  const custom = buildCustomTemplate(customCols, customRows);
  $("template-grid").innerHTML =
    allTemplates()
      .map(
        (t) => `<button type="button" class="template${
          t.generated ? " generated" : ""
        }" role="radio" data-template="${escapeHtml(t.key)}"
         aria-checked="${t.key === draftTemplate.key}">
      <span class="template-preview" style="grid-template-columns:${t.cols};
        grid-template-rows:${t.rows}">${t.slots
          .map(() => "<span></span>")
          .join("")}</span>
      <span class="template-name">${escapeHtml(t.name)} · ${t.slots.length}</span>
    </button>`
      )
      .join("") +
    `<div class="custom-dims${
      draftTemplate.generated ? " active" : ""
    }" aria-label="Custom grid size">
       <span class="template-name">CUSTOM GRID</span>
       <div class="dim-row">
         <span>COLS</span>${CUSTOM_DIVISORS.map(
           (n) => `<button type="button" data-cols="${n}"
             aria-pressed="${n === customCols}">${n}</button>`
         ).join("")}
       </div>
       <div class="dim-row">
         <span>ROWS</span>${CUSTOM_DIVISORS.map(
           (n) => `<button type="button" data-rows="${n}"
             aria-pressed="${n === customRows}">${n}</button>`
         ).join("")}
       </div>
       <span class="template-name">${custom.slots.length} panels</span>
     </div>`;
}

function renderPicker() {
  const max = draftTemplate.slots.length;
  $("panel-picker").innerHTML = PANEL_CATALOGUE.map((p) => {
    const at = draftPanels.indexOf(p.id);
    return `<button type="button" class="pick" data-pick="${escapeHtml(p.id)}"
         aria-pressed="${at >= 0}">
      <span class="slot">${at >= 0 ? at + 1 : "+"}</span>
      <span>${escapeHtml(p.name)}</span>
      <span class="what">${escapeHtml(p.what)}</span>
    </button>`;
  }).join("");
  $("newtab-count").textContent = `${draftPanels.length} of ${max} slots filled`;
  $("newtab-hint").textContent =
    draftPanels.length < max
      ? `Pick ${max - draftPanels.length} more — panels fill the slots in the order you click them.`
      : "All slots filled. Clicking another panel replaces the last one.";
}

function openNewTab() {
  draftTemplate = TEMPLATES[0];
  draftPanels = [];
  $("newtab-name").value = "";
  $("newtab-error").hidden = true;
  renderTemplates();
  renderPicker();
  $("newtab-modal").hidden = false;
  $("newtab-name").focus();
}

$("newtab-close").addEventListener("click", () => {
  $("newtab-modal").hidden = true;
});
$("newtab-modal").addEventListener("click", (e) => {
  if (e.target === $("newtab-modal")) $("newtab-modal").hidden = true;
});

$("template-grid").addEventListener("click", (e) => {
  const cols = e.target.closest("[data-cols]");
  const rows = e.target.closest("[data-rows]");
  if (cols || rows) {
    if (cols) customCols = Number(cols.dataset.cols);
    if (rows) customRows = Number(rows.dataset.rows);
    // Changing the size selects the custom grid: adjusting dimensions while a
    // preset stays selected would look like nothing happened.
    draftTemplate = buildCustomTemplate(customCols, customRows);
    draftPanels = draftPanels.slice(0, draftTemplate.slots.length);
    renderTemplates();
    renderPicker();
    return;
  }

  const button = e.target.closest("[data-template]");
  if (!button) return;
  draftTemplate = allTemplates().find((t) => t.key === button.dataset.template);
  // Slots may have shrunk; drop anything that no longer has one rather than
  // silently keeping a panel the layout cannot place.
  draftPanels = draftPanels.slice(0, draftTemplate.slots.length);
  renderTemplates();
  renderPicker();
});

$("panel-picker").addEventListener("click", (e) => {
  const button = e.target.closest("[data-pick]");
  if (!button) return;
  const id = button.dataset.pick;
  const at = draftPanels.indexOf(id);
  if (at >= 0) draftPanels.splice(at, 1);
  else if (draftPanels.length < draftTemplate.slots.length) draftPanels.push(id);
  else draftPanels[draftPanels.length - 1] = id;
  renderPicker();
});

$("newtab-create").addEventListener("click", () => {
  const error = $("newtab-error");
  error.hidden = true;
  const name = $("newtab-name").value.trim().toUpperCase() || "CUSTOM";
  if (draftPanels.length === 0) {
    error.textContent = "Pick at least one panel.";
    error.hidden = false;
    return;
  }
  const list = readCustomTabs();
  const key = `custom-${Date.now()}`;
  list.push({
    key, code: name.slice(0, 16),
    label: `${draftPanels.length} panel${draftPanels.length === 1 ? "" : "s"}`,
    template: draftTemplate.key, panels: [...draftPanels],
    ...(draftTemplate.generated ? { cols: customCols, rows: customRows } : {}),
  });
  saveCustomTabs(list);
  refreshWorkspaces();
  $("newtab-modal").hidden = true;
  applyWorkspace(WORKSPACES.findIndex((w) => w.key === key));
});

function deleteCustomTab(key) {
  saveCustomTabs(readCustomTabs().filter((t) => t.key !== key));
  try {
    localStorage.removeItem(`diy-terminal-geometry.v2:${key}`);
  } catch (_) {}
  refreshWorkspaces();
  applyWorkspace(0);
}

// --- Lock screen -------------------------------------------------------------
//
// The terminal binds to loopback, which keeps the network out but not whoever
// is sitting at the machine — and it holds API keys and a webhook that can post
// to a channel. One password, optionally Touch ID.

let authState = { configured: false, authenticated: false };

function b64urlToBytes(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

function bytesToB64url(buffer) {
  return btoa(String.fromCharCode(...new Uint8Array(buffer)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}


/* --- boot ------------------------------------------------------------------
 *
 * Warm everything before showing the terminal, so no panel is a skeleton and
 * no screen is a spinner once you are in.
 *
 * The progress bar counts real completed requests. A bar that animates on a
 * timer while the work happens somewhere else is a decoration, and it lies
 * whenever the work is slower than the animation.
 */

const BOOT_DONE_KEY = "roswell-booted";
let bootSkipped = false;

/* Four at a time. Serial is needlessly slow when most of these are SQLite
   reads; unbounded would put twenty simultaneous requests through Yahoo and
   earn a rate limit. */
const BOOT_CONCURRENCY = 4;

function bootTasks(tickers) {
  const tasks = [
    ["directory", () => api("/api/functions")],
    ["settings", () => api("/api/settings")],
    ["watchlist", () => api("/api/prices")],
    ["playbook", () => api("/api/plans")],
    ["scanner", () => api("/api/scanner")],
    ["macro calendar", () => api("/api/macro/calendar")],
    ["treasury curve", () => api("/api/macro/yields")],
    ["alerts", () => api("/api/alerts")],
    ["positions", () => api("/api/positions")],
    ["forward test", () => api("/api/paper")],
    ["sector map", () => api("/api/functions/sectors")],
    ["news index", () => api("/api/functions/news-search?limit=60")],
  ];
  // Per ticker, the things a screen would otherwise fetch when opened. The
  // company call is what DES, FA, EE, ANR and HDS all read, and the chain is
  // what OMON reads; both go out to the network, which is exactly why they
  // are worth doing now rather than while someone is waiting on a screen.
  for (const ticker of tickers) {
    tasks.push([`${ticker} · quotes`, () => api(`/api/prices/${ticker}/sparkline`)]);
    tasks.push([`${ticker} · fundamentals`, () => api(`/api/fundamentals/${ticker}`)]);
    tasks.push([`${ticker} · news`, () => api(`/api/news/${ticker}`)]);
    tasks.push([`${ticker} · company`, () => api(`/api/functions/company/${ticker}`)]);
    tasks.push([`${ticker} · options`, () => api(`/api/options/${ticker}`)]);
  }
  return tasks;
}

function bootPaint(done, total, label) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  $("boot-fill").style.width = `${pct}%`;
  $("boot-bar").setAttribute("aria-valuenow", String(pct));
  $("boot-status").textContent = label;
  $("boot-count").textContent = `${done} / ${total}`;
}

async function bootSequence() {
  const screen = $("boot-screen");
  screen.hidden = false;
  bootSkipped = false;

  const skip = (e) => {
    if (e.key === "Escape") bootSkipped = true;
  };
  document.addEventListener("keydown", skip);

  let tickers = [];
  try {
    tickers = (await api("/api/watchlist")).tickers || [];
  } catch (_) {}

  const tasks = bootTasks(tickers);
  const started = Date.now();
  let done = 0;
  bootPaint(0, tasks.length, "reading the watchlist");

  const queue = tasks.slice();
  async function worker() {
    while (queue.length && !bootSkipped) {
      const [label, run] = queue.shift();
      bootPaint(done, tasks.length, label);
      try {
        await run();
      } catch (_) {
        // A screen that cannot warm is not a reason to refuse to start. It
        // will show its own error when opened.
      }
      done += 1;
      bootPaint(done, tasks.length, label);
    }
  }
  await Promise.all(
    Array.from({ length: BOOT_CONCURRENCY }, () => worker())
  );

  bootPaint(tasks.length, tasks.length,
            bootSkipped ? "skipped" : `ready in ${((Date.now() - started) / 1000).toFixed(1)}s`);
  await new Promise((r) => setTimeout(r, bootSkipped ? 120 : 420));

  document.removeEventListener("keydown", skip);
  screen.hidden = true;
  try {
    sessionStorage.setItem(BOOT_DONE_KEY, "1");
  } catch (_) {}

  // Everything is warm; repaint the panels from it.
  renderWatchlist();
  renderPlans();
  renderPositions();
  renderMacro();
  renderAlerts();
  loadScan();
}

/* Deep link: ?t=AAPL[&f=DES]

   Roswell is driven by its command line and had no URL entry point, so nothing
   outside it could point at a specific security. Roswell Brain lists hundreds
   of tickers and wants to hand one over; without this the handoff could only
   open the terminal's front door and leave you to retype the symbol.

   Deliberately narrow: it reads the parameter, opens that screen, and strips
   the query so a reload does not reopen it. It does not touch the command line,
   the tab strip, or any existing behaviour. An unknown code falls back to DES,
   which is what a bare ticker already does. */
function openDeepLink() {
  let params;
  try {
    params = new URLSearchParams(location.search);
  } catch (_) {
    return;
  }
  const ticker = (params.get("t") || "").trim().toUpperCase();
  if (!ticker) return;

  const requested = (params.get("f") || "DES").trim().toUpperCase();
  const code = FUNCTION_INDEX[requested] ? requested : "DES";

  try {
    history.replaceState(null, "", location.pathname);
  } catch (_) {}

  openScreen(code, ticker);
}


/* Once per session, not once per reload of an already-open terminal. */
function shouldBoot() {
  try {
    return sessionStorage.getItem(BOOT_DONE_KEY) !== "1";
  } catch (_) {
    return true;
  }
}

function lockError(message) {
  const el = $("lock-error");
  el.textContent = message;
  el.hidden = !message;
}

async function refreshAuth() {
  try {
    authState = await api("/api/auth/status");
  } catch (_) {
    return;
  }
  const locked = authState.configured && !authState.authenticated;
  const firstRun = !authState.configured;
  $("lock-screen").hidden = !(locked || firstRun);

  // Unlocked and not yet warmed: boot before the terminal is shown.
  if (!locked && !firstRun && shouldBoot()) {
    bootSequence();
  }

  if (!locked && !firstRun) {
    openDeepLink();
  }

  $("lock-sub").textContent = firstRun
    ? "Set a password to lock this terminal"
    : "Locked";
  $("lock-confirm").hidden = !firstRun;
  $("lock-go").textContent = firstRun ? "SET PASSWORD" : "UNLOCK";
  $("lock-password").setAttribute(
    "autocomplete", firstRun ? "new-password" : "current-password"
  );

  const canBio =
    !firstRun && authState.biometric_registered && authState.biometric_possible &&
    window.PublicKeyCredential !== undefined;
  $("lock-bio").hidden = !canBio;

  $("lock-hint").textContent = authState.locked
    ? `Too many attempts. Locked for ${authState.seconds_remaining}s.`
    : firstRun
      ? `At least ${authState.min_password_length} characters. It is hashed, never stored as typed.`
      : authState.biometric_hint || "";
  $("lock-go").disabled = authState.locked;

  if (!$("lock-screen").hidden) $("lock-password").focus();
}

// Set once the password has been accepted and only the code is outstanding.
// Not a session — the server holds a two-minute token that opens nothing.
let awaitingTotp = false;

$("lock-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  lockError("");
  const password = $("lock-password").value;
  const firstRun = !authState.configured;

  if (awaitingTotp) {
    try {
      await api("/api/auth/login/totp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: $("lock-totp").value }),
      });
    } catch (err) {
      lockError(err.message);
      return;
    }
    location.reload();
    return;
  }

  if (firstRun) {
    if (password.length < (authState.min_password_length || 6)) {
      lockError(`Use at least ${authState.min_password_length || 6} characters.`);
      return;
    }
    if (password !== $("lock-confirm").value) {
      lockError("The two passwords do not match.");
      return;
    }
  }

  let result;
  try {
    result = await api(firstRun ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
  } catch (err) {
    lockError(err.message);
    await refreshAuth();
    return;
  }

  if (result && result.totp_required) {
    // Swap the card over to the code rather than reloading: reloading here
    // would drop the pending state and send them back to the password.
    awaitingTotp = true;
    $("lock-password").value = "";
    $("lock-password").hidden = true;
    $("lock-bio").hidden = true;
    $("lock-totp").hidden = false;
    $("lock-totp").focus();
    $("lock-sub").textContent = "Code from your authenticator";
    $("lock-go").textContent = "VERIFY";
    return;
  }
  $("lock-password").value = "";
  $("lock-confirm").value = "";
  await refreshAuth();
  // The panels loaded against a 401; now that there is a session, fill them.
  location.reload();
});

$("lock-bio").addEventListener("click", async () => {
  lockError("");
  try {
    const options = await api("/api/auth/biometric/login/begin", { method: "POST" });
    const assertion = await navigator.credentials.get({
      publicKey: {
        challenge: b64urlToBytes(options.challenge),
        rpId: options.rpId,
        userVerification: "required",
        allowCredentials: (options.allowCredentials || []).map((c) => ({
          id: b64urlToBytes(c.id), type: "public-key",
        })),
      },
    });
    await api("/api/auth/biometric/login/finish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: assertion.id,
        rawId: bytesToB64url(assertion.rawId),
        type: assertion.type,
        response: {
          clientDataJSON: bytesToB64url(assertion.response.clientDataJSON),
          authenticatorData: bytesToB64url(assertion.response.authenticatorData),
          signature: bytesToB64url(assertion.response.signature),
          userHandle: assertion.response.userHandle
            ? bytesToB64url(assertion.response.userHandle)
            : null,
        },
      }),
    });
    location.reload();
  } catch (err) {
    // A cancelled Touch ID prompt is not a failure worth shouting about.
    if (err && err.name === "NotAllowedError") return;
    lockError(err.message || "Touch ID failed.");
  }
});

async function registerBiometric() {
  const options = await api("/api/auth/biometric/register/begin", { method: "POST" });
  const credential = await navigator.credentials.create({
    publicKey: {
      challenge: b64urlToBytes(options.challenge),
      rp: options.rp,
      user: {
        id: b64urlToBytes(options.user.id),
        name: options.user.name,
        displayName: options.user.displayName,
      },
      pubKeyCredParams: options.pubKeyCredParams,
      authenticatorSelection: options.authenticatorSelection,
      timeout: options.timeout,
    },
  });
  await api("/api/auth/biometric/register/finish", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: credential.id,
      rawId: bytesToB64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bytesToB64url(credential.response.clientDataJSON),
        attestationObject: bytesToB64url(credential.response.attestationObject),
      },
    }),
  });
}

refreshAuth();

// --- Lock / logout and security settings -------------------------------------

$("lock-now").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {}
  // Reload rather than just showing the overlay: it drops every panel's loaded
  // data from the page, so locking does not leave the numbers on screen behind
  // a translucent layer.
  location.reload();
});

async function renderSecurity() {
  let s;
  try {
    s = await api("/api/auth/status");
  } catch (_) {
    return;
  }
  $("security-state").textContent = s.configured
    ? "Password set. Changing it signs out every other session."
    : "No password yet — the terminal is unlocked for anyone at this machine.";
  $("pw-current").hidden = !s.configured;

  const supported = window.PublicKeyCredential !== undefined;
  // Deliberately NOT disabled when the origin cannot support it. A disabled
  // button that does nothing on click is indistinguishable from a broken one;
  // clicking it should explain the problem and offer the fix.
  $("bio-register").disabled = !supported;
  $("bio-forget").hidden = !s.biometric_registered;

  const state = $("bio-state");
  if (!supported) {
    state.textContent = "This browser has no platform authenticator.";
  } else if (!s.biometric_possible) {
    // A link, not a sentence to retype: same server, different host string.
    const url = `http://localhost:${location.port || 8000}${location.pathname}`;
    state.innerHTML =
      `Touch ID needs a hostname, not an IP address — a browser will not accept
       <code>${escapeHtml(location.hostname)}</code> as a WebAuthn relying party.
       Open <a href="${escapeHtml(url)}">${escapeHtml(url)}</a> instead; it is
       the same terminal.`;
  } else {
    state.textContent = s.biometric_registered
      ? "Touch ID is registered. You can use it on the lock screen."
      : "Not set up yet. Click above and confirm with your fingerprint.";
  }
}

$("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = $("pw-error");
  error.hidden = true;
  try {
    await api("/api/auth/password", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current: $("pw-current").value,
        password: $("pw-new").value,
      }),
    });
    $("pw-current").value = "";
    $("pw-new").value = "";
    await renderSecurity();
    $("security-state").textContent = "Password changed.";
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  }
});

$("bio-register").addEventListener("click", async () => {
  const error = $("pw-error");
  error.hidden = true;

  // Check before calling the browser API: navigator.credentials.create would
  // reject with a SecurityError that says nothing about how to fix it.
  let s;
  try {
    s = await api("/api/auth/status");
  } catch (_) {
    s = {};
  }
  if (s.biometric_possible === false) {
    const url = `http://localhost:${location.port || 8000}${location.pathname}`;
    error.innerHTML =
      `Touch ID cannot be set up on <code>${escapeHtml(location.hostname)}</code>.
       Open <a href="${escapeHtml(url)}">${escapeHtml(url)}</a> and try again.`;
    error.hidden = false;
    return;
  }

  try {
    await registerBiometric();
    await renderSecurity();
  } catch (err) {
    if (err && err.name === "NotAllowedError") return;
    error.textContent = err.message || "Touch ID setup failed.";
    error.hidden = false;
  }
});

$("bio-forget").addEventListener("click", async () => {
  try {
    await api("/api/auth/biometric", { method: "DELETE" });
    await renderSecurity();
  } catch (_) {}
});


// --- two-factor --------------------------------------------------------------

function totpError(message) {
  const el = $("totp-error");
  el.textContent = message || "";
  el.hidden = !message;
}

async function renderTotpState() {
  let status;
  try {
    status = await api("/api/auth/status");
  } catch (_) {
    return;
  }
  const on = status.totp_enabled;
  $("totp-state").textContent = on
    ? `On. ${status.backup_codes_left} backup code${
        status.backup_codes_left === 1 ? "" : "s"
      } left.`
    : "Off. A stolen password alone would open the terminal.";
  $("totp-begin").hidden = on;
  $("totp-off").hidden = !on;
  if (on) $("totp-setup").hidden = true;
}

$("totp-begin").addEventListener("click", async () => {
  totpError("");
  try {
    const begun = await api("/api/auth/totp/begin", { method: "POST" });
    $("totp-secret").textContent = begun.formatted;
    $("totp-setup").hidden = false;
    $("totp-codes").hidden = true;
    $("totp-code").focus();
  } catch (err) {
    totpError(err.message);
  }
});

$("totp-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  totpError("");
  try {
    const done = await api("/api/auth/totp/enable", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: $("totp-code").value }),
    });
    $("totp-backup").textContent = done.backup_codes.join("\n");
    $("totp-codes").hidden = false;
    $("totp-setup").hidden = true;
    $("totp-code").value = "";
  } catch (err) {
    totpError(err.message);
  }
  await renderTotpState();
});

$("totp-off").addEventListener("click", async () => {
  totpError("");
  // Turning a factor off is exactly what someone at an unattended session
  // would do, so the server asks for the password and so do we.
  const password = prompt("Password, to turn two-factor off:");
  if (!password) return;
  try {
    await api("/api/auth/totp", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    $("totp-codes").hidden = true;
  } catch (err) {
    totpError(err.message);
  }
  await renderTotpState();
});


// --- positions ---------------------------------------------------------------
//
// The watchlist is a list of things to look at. This is the list of things
// actually held, which is what lets the terminal answer "how am I doing" with
// a measurement rather than a feeling.

let accountSettings = { account_size: 10000, risk_pct: 1 };

function money(v) {
  return Number.isFinite(v)
    ? (v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "—";
}

function signed(v, suffix = "") {
  if (!Number.isFinite(v)) return '<span>—</span>';
  const cls = v > 0 ? "up" : v < 0 ? "down" : "";
  return `<span class="${cls}">${v > 0 ? "+" : ""}${v.toFixed(2)}${suffix}</span>`;
}

function agg(label, value) {
  return `<span class="agg"><span class="agg-label">${escapeHtml(label)}</span>` +
         `<span class="agg-value">${value}</span></span>`;
}

function positionError(message) {
  const el = $("position-error");
  el.textContent = message || "";
  el.hidden = !message;
}

/* The journal's own honesty rules, rendered: the win rate never appears
   without its count, and an empty record says so rather than showing 0%. */
function journalLine(j) {
  if (!j || !j.closed) {
    return '<span class="thin">No closed trades yet. The record fills in as ' +
           'positions are closed.</span>';
  }
  const parts = [
    agg("closed", j.closed),
    agg("win rate", j.win_rate === null ? "—" : `${j.win_rate}% of ${j.closed}`),
    agg("expectancy", signed(j.expectancy_r, "R")),
    agg("total", signed(j.total_r, "R")),
    agg("realised", money(j.realised_pnl)),
  ];
  if (Number.isFinite(j.avg_win_r)) parts.push(agg("avg win", signed(j.avg_win_r, "R")));
  if (Number.isFinite(j.avg_loss_r)) parts.push(agg("avg loss", signed(j.avg_loss_r, "R")));
  if (j.overruns) {
    // The failure mode that quietly turns a positive expectancy negative.
    parts.push(agg("past the stop", `${j.overruns}`));
  }
  if (Number.isFinite(j.left_on_table)) {
    parts.push(agg("left on table", `${j.left_on_table}R`));
  }
  if (j.thin) {
    parts.push('<span class="thin">Thin sample — under ' +
      'twenty closed trades, none of these numbers should change what you do.</span>');
  }
  return parts.join("");
}

function renderOpenPosition(p) {
  const rr = Number.isFinite(p.planned_r) ? `${p.planned_r.toFixed(2)}R` : "—";
  return `<li class="position" data-position="${p.id}">
    <div class="pos-head">
      <span class="pos-sym">${escapeHtml(p.ticker)}</span>
      <span class="pos-dir">${escapeHtml(p.direction.toUpperCase())}</span>
      ${p.plan_grade ? `<span class="pos-dir">${escapeHtml(p.plan_grade)}</span>` : ""}
      <span class="pos-size">${p.shares} @ ${Number(p.entry_price).toFixed(2)}</span>
    </div>
    <div class="pos-levels">
      <span>STOP <b>${p.stop === null ? "none" : Number(p.stop).toFixed(2)}</b></span>
      <span>TARGET <b>${p.target1 === null ? "—" : Number(p.target1).toFixed(2)}</b></span>
      <span>PLANNED <b>${rr}</b></span>
      <span>AT RISK <b>${p.risk_remaining === null ? "—" : money(p.risk_remaining)}</b></span>
      <span>OPEN ${signed(p.open_pnl)}</span>
    </div>
    <div class="pos-actions">
      <input type="number" step="any" placeholder="EXIT"
             data-exit="${p.id}" aria-label="Exit price">
      <button type="button" data-close="${p.id}">CLOSE</button>
      <input type="number" step="any" placeholder="NEW STOP"
             data-newstop="${p.id}" aria-label="New stop">
      <button type="button" data-movestop="${p.id}">MOVE STOP</button>
      <button type="button" data-forget="${p.id}">DELETE</button>
    </div>
  </li>`;
}

function renderClosedPosition(p) {
  return `<li class="position">
    <div class="pos-head">
      <span class="pos-sym">${escapeHtml(p.ticker)}</span>
      <span class="pos-dir">${escapeHtml(p.direction.toUpperCase())}</span>
      ${p.exit_reason ? `<span class="pos-dir">${escapeHtml(p.exit_reason)}</span>` : ""}
      <span class="pos-size">${signed(p.realised_r, "R")} · ${money(p.pnl)}</span>
    </div>
    <div class="pos-levels">
      <span>${Number(p.entry_price).toFixed(2)} → ${Number(p.exit_price).toFixed(2)}</span>
      ${Number.isFinite(p.hold_days) ? `<span>held ${p.hold_days.toFixed(1)}d</span>` : ""}
      ${p.plan_grade ? `<span>grade ${escapeHtml(p.plan_grade)}</span>` : ""}
    </div>
  </li>`;
}

async function renderPositions() {
  showSkeleton($("positions-list"), skeletonRows(2, [22, 48, 30]));
  showSkeleton($("positions-closed"), skeletonRows(2, [22, 40]));
  let data;
  try {
    data = await api("/api/positions");
  } catch (_) {
    markFilled($("positions-list")), $("positions-list").innerHTML =
      '<li class="position empty">Could not load positions.</li>';
    markFilled($("positions-closed")), $("positions-closed").innerHTML =
      '<li class="position empty">Could not load the record.</li>';
    return;
  }
  accountSettings = {
    account_size: data.account_size, risk_pct: data.risk_pct,
  };

  $("positions-aggregate").innerHTML = data.open.length
    ? [
        agg("open", data.open.length),
        agg("exposure", `${money(data.exposure)} (${data.exposure_pct}%)`),
        agg("unrealised", signed(data.unrealised_pnl)),
        // The number that decides whether to take one more.
        agg("risk if every stop hits",
            `${money(data.open_risk)} (${data.open_risk_pct}% of account)`),
      ].join("")
    : '<span class="thin">Nothing open.</span>';

  markFilled($("positions-list")), $("positions-list").innerHTML = data.open.length
    ? data.open.map(renderOpenPosition).join("")
    : '<li class="position empty">No open positions.</li>';

  $("journal-summary").innerHTML = journalLine(data.journal);
  markFilled($("positions-closed")), $("positions-closed").innerHTML = data.closed.length
    ? data.closed.slice(0, 40).map(renderClosedPosition).join("")
    : '<li class="position empty">Nothing closed yet.</li>';

  $("positions-stamp").textContent =
    `${money(accountSettings.account_size)} · ${accountSettings.risk_pct}% per trade`;
}

$("position-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  positionError("");
  const body = {
    ticker: $("pos-ticker").value.trim().toUpperCase(),
    direction: $("pos-direction").value,
    shares: Number($("pos-shares").value),
    entry_price: Number($("pos-entry").value),
    stop: $("pos-stop").value ? Number($("pos-stop").value) : null,
    target1: $("pos-target").value ? Number($("pos-target").value) : null,
  };
  if (!body.ticker || !(body.shares > 0) || !(body.entry_price > 0)) {
    positionError("Ticker, shares and entry are required.");
    return;
  }
  try {
    await api("/api/positions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    positionError(err.message);
    return;
  }
  ["pos-ticker", "pos-shares", "pos-entry", "pos-stop", "pos-target"]
    .forEach((id) => ($(id).value = ""));
  renderPositions();
});

$("positions-panel").addEventListener("click", async (e) => {
  const closeBtn = e.target.closest("[data-close]");
  const moveBtn = e.target.closest("[data-movestop]");
  const forgetBtn = e.target.closest("[data-forget]");
  positionError("");

  try {
    if (closeBtn) {
      const id = closeBtn.dataset.close;
      const field = document.querySelector(`[data-exit="${id}"]`);
      const price = Number(field.value);
      if (!(price > 0)) {
        positionError("Type the exit price first.");
        return;
      }
      await api(`/api/positions/${id}/close`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ exit_price: price, exit_reason: "manual" }),
      });
    } else if (moveBtn) {
      const id = moveBtn.dataset.movestop;
      const field = document.querySelector(`[data-newstop="${id}"]`);
      const stop = Number(field.value);
      if (!(stop > 0)) {
        positionError("Type the new stop first.");
        return;
      }
      await api(`/api/positions/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stop }),
      });
    } else if (forgetBtn) {
      // Deleting erases it from the record, which is not the same as closing
      // it — a deleted loss would flatter the journal for good.
      if (!confirm("Delete this position? It leaves the record entirely — " +
                   "close it instead if it was a real trade.")) return;
      await api(`/api/positions/${forgetBtn.dataset.forget}`, { method: "DELETE" });
    } else {
      return;
    }
  } catch (err) {
    positionError(err.message);
  }
  renderPositions();
});


async function renderAccount() {
  try {
    const s = await api("/api/settings");
    accountSettings = { account_size: s.account_size, risk_pct: s.risk_pct };
    $("account-size").value = s.account_size;
    $("account-risk").value = s.risk_pct;
    $("account-state").textContent = s.account_configured
      ? `${money(s.account_size)}, risking ${s.risk_pct}% per trade.`
      : `Not set — assuming ${money(s.account_size)} at ${s.risk_pct}%.`;
  } catch (_) {
    $("account-state").textContent = "Could not read the account settings.";
  }
}

$("account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const el = $("account-error");
  el.hidden = true;
  try {
    await api("/api/settings/account", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_size: Number($("account-size").value),
        risk_pct: Number($("account-risk").value),
      }),
    });
  } catch (err) {
    el.textContent = err.message;
    el.hidden = false;
    return;
  }
  await renderAccount();
  // The share counts on every plan card are computed from these.
  renderPlans();
  renderPositions();
});


// --- FWD: the forward test ---------------------------------------------------
//
// Every other measurement in this app is in-sample. This one is not, which is
// the entire reason it exists and the reason its caveats are printed next to
// its numbers rather than buried.

SCREENS.FWD = async function () {
  const d = await api("/api/paper");
  const j = d.journal;
  const c = d.counts;

  const row = (t) => `<tr>
    <td>${escapeHtml(t.ticker)}</td>
    <td>${escapeHtml(t.plan_grade || "—")}</td>
    <td class="num">${t.planned_entry.toFixed(2)}</td>
    <td class="num">${t.stop.toFixed(2)}</td>
    <td class="num">${t.target1.toFixed(2)}</td>
    <td>${escapeHtml(String(t.armed_at).slice(0, 10))}</td>
  </tr>`;

  const closedRow = (t) => `<tr>
    <td>${escapeHtml(t.ticker)}</td>
    <td>${escapeHtml(t.plan_grade || "—")}</td>
    <td class="num">${t.entry_price === null ? "—" : t.entry_price.toFixed(2)}</td>
    <td class="num">${t.exit_price === null ? "—" : t.exit_price.toFixed(2)}</td>
    <td>${escapeHtml(t.exit_reason || "")}${t.exit_gapped ? " (gap)" : ""}</td>
    <td class="num ${t.realised_r > 0 ? "up" : t.realised_r < 0 ? "down" : ""}">${
      t.realised_r === null ? "—" : (t.realised_r > 0 ? "+" : "") + t.realised_r.toFixed(2) + "R"
    }</td>
  </tr>`;

  const grades = Object.entries(j.by_grade || {})
    .map(([g, v]) => `<tr><td>${escapeHtml(g)}</td><td class="num">${v.n}</td>` +
      `<td class="num">${v.win_rate}%</td>` +
      `<td class="num ${v.expectancy_r > 0 ? "up" : "down"}">${
        v.expectancy_r > 0 ? "+" : ""}${v.expectancy_r}R</td></tr>`)
    .join("") || '<tr><td colspan="4">Nothing closed yet.</td></tr>';

  return `<h2>FWD — FORWARD TEST</h2>
    <p class="screen-sub">The engine's own plans, frozen when computed and
    judged by bars that arrived afterwards. Every other measurement in this
    terminal is in-sample; this one is not, which is why it is here and why it
    will take months to say anything.</p>

    <div class="screen-grid">
      <section><h3>STATE</h3><dl class="kv">
        <dt>Armed, waiting to fill</dt><dd>${c.armed}</dd>
        <dt>Open</dt><dd>${c.open}</dd>
        <dt>Closed</dt><dd>${c.closed}</dd>
        <dt>Never filled or unmodellable</dt><dd>${c.dropped}</dd>
        <dt>Fill rate</dt><dd>${d.fill_rate === null ? "—" : d.fill_rate + "%"}</dd>
      </dl>
      <p class="screen-note">A strategy whose entries rarely trade is not the
      strategy the backtest measured, so the fill rate sits beside the
      result rather than behind it.</p></section>

      <section><h3>RESULT</h3><dl class="kv">
        <dt>Closed trades</dt><dd>${j.closed}</dd>
        <dt>Win rate</dt><dd>${j.win_rate === null ? "—" : j.win_rate + "%"}</dd>
        <dt>Expectancy</dt><dd class="${j.expectancy_r > 0 ? "up" : "down"}">${
          j.expectancy_r === null ? "—" : (j.expectancy_r > 0 ? "+" : "") + j.expectancy_r + "R"
        }</dd>
        <dt>Average win</dt><dd>${j.avg_win_r === null ? "—" : "+" + j.avg_win_r + "R"}</dd>
        <dt>Average loss</dt><dd>${j.avg_loss_r === null ? "—" : j.avg_loss_r + "R"}</dd>
        <dt>Worse than −1R</dt><dd>${j.overruns}</dd>
        <dt>Exits through a gap</dt><dd>${d.gapped_exits}</dd>
      </dl>
      ${j.thin ? '<p class="screen-note">Fewer than twenty closed trades. ' +
        'These numbers are shown because hiding them invites guessing, not ' +
        'because they mean anything yet.</p>' : ""}</section>

      <section><h3>BY GRADE</h3>
        <table class="chain"><thead><tr><th>GRADE</th><th class="num">N</th>
          <th class="num">WIN</th><th class="num">EXPECTANCY</th></tr></thead>
          <tbody>${grades}</tbody></table>
        <p class="screen-note">The backtest already showed A+ does not reliably
        beat B. This is the same question asked forward.</p></section>
    </div>

    <h3>ARMED</h3>
    <table class="chain"><thead><tr><th>SYM</th><th>GRADE</th>
      <th class="num">ENTRY</th><th class="num">STOP</th><th class="num">TARGET</th>
      <th>ARMED</th></tr></thead>
      <tbody>${d.armed.map(row).join("") ||
        '<tr><td colspan="6">Nothing armed. The engine arms a plan when it calls a setup tradeable.</td></tr>'}</tbody></table>

    <h3>CLOSED</h3>
    <table class="chain"><thead><tr><th>SYM</th><th>GRADE</th>
      <th class="num">IN</th><th class="num">OUT</th><th>WHY</th>
      <th class="num">R</th></tr></thead>
      <tbody>${d.closed.map(closedRow).join("") ||
        '<tr><td colspan="6">Nothing closed yet.</td></tr>'}</tbody></table>

    <h3>WHERE THIS MODEL IS KIND</h3>
    <ul class="caveats">${d.caveats.map(
      (c) => `<li>${escapeHtml(c)}</li>`).join("")}</ul>`;
};


// --- WB: the Treasury curve --------------------------------------------------

SCREENS.WB = async function () {
  const d = await api("/api/macro/yields");
  if (!d.points.length) {
    return `<h2>WB — WORLD YIELDS</h2>
      <p class="screen-sub">${escapeHtml(d.reason || "No curve cached.")}</p>`;
  }

  const pcts = d.points.map((p) => p.percent);
  const lo = Math.min(...pcts);
  const span = Math.max(...pcts) - lo || 1;

  const rows = d.points
    .map((p) => {
      const change = p.month_ago === null ? null : p.percent - p.month_ago;
      // A bar per maturity: the curve's shape is the only reason to open this
      // screen, and a column of numbers hides it.
      const width = 8 + ((p.percent - lo) / span) * 88;
      return `<tr>
        <td>${escapeHtml(p.label)}</td>
        <td class="num">${p.percent.toFixed(2)}%</td>
        <td class="num ${change > 0 ? "up" : change < 0 ? "down" : ""}">${
          change === null ? "—" : (change > 0 ? "+" : "") + change.toFixed(2)
        }</td>
        <td class="curve-cell"><span class="curve-bar" style="width:${width.toFixed(
          1
        )}%"></span></td>
      </tr>`;
    })
    .join("");

  // Not a <dl>: a label, a number and a sentence of context do not fit the
  // two-column key/value grid, which wrapped "10Y minus 2Y" onto three lines
  // and stretched the note across the whole panel.
  const spread = (label, value, note) => {
    const inverted = value !== null && value < 0;
    return `<div class="spread">
      <div class="spread-head">
        <span class="spread-label">${escapeHtml(label)}</span>
        <span class="spread-value ${inverted ? "down" : ""}">${
          value === null ? "—" : (value > 0 ? "+" : "") + value.toFixed(2) + "%"
        }${inverted ? " · INVERTED" : ""}</span>
      </div>
      <p class="screen-note">${escapeHtml(note)}</p>
    </div>`;
  };

  return `<h2>WB — WORLD YIELDS</h2>
    <p class="screen-sub">US Treasury constant-maturity closes from FRED, as of
    ${escapeHtml(d.points[0].observed)}. The change column compares with roughly
    a month earlier.</p>

    <div class="screen-grid">
      <section><h3>THE CURVE</h3>
        <table class="chain"><thead><tr>
          <th>TERM</th><th class="num">YIELD</th><th class="num">1M</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table></section>

      <section><h3>SPREADS</h3>
        ${spread("10Y minus 2Y", d.spread_10y_2y,
                 "The one people quote. It has inverted before most recessions, " +
                 "which is not the same as predicting them — the sample is a " +
                 "handful of events and the lead time has ranged over years.")}
        ${spread("10Y minus 3M", d.spread_10y_3m,
                 "The version with the better research record, and the one the " +
                 "New York Fed publishes its recession probability from.")}
      </section>
    </div>

    <p class="screen-note">Only US Treasuries. The name says world yields
    because that is the Bloomberg function code; this terminal has one free
    macro source and it is American.</p>`;
};
