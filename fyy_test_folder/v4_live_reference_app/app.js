const PARAMS = {
  interval: 10,
  hold: 10,
  stopLossPct: -8,
  bullCount: 5,
  neutralCount: 3,
  bearCount: 2,
  initialCapital: 50000,
};
let data = emptyState();
const automation = { status: null, screening: null };
const liveQuotes = { quotes: {}, fetchedAt: null, error: null };
let syncText = "服务端状态加载中...";
let quoteText = "实时行情未获取";
let saveTimer = null;
let quotesTimer = null;

const $ = (id) => document.getElementById(id);

init();

async function init() {
  startClock();
  bindGlobal();
  await refreshAll();
  startQuotesPolling();
}

function startClock() {
  tick();
  setInterval(tick, 10000);
}

function tick() {
  const now = new Date();
  $("clockDisplay").textContent = now.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
    weekday: "short",
  });
  const session = getSession(now);
  const badge = $("sessionBadge");
  badge.textContent = session.label;
  badge.className = `badge ${session.cls}`;
}

function getSession(now) {
  const h = now.getHours(), m = now.getMinutes();
  const t = h * 60 + m;
  const day = now.getDay();
  if (day === 0 || day === 6) return { label: "休市", cls: "closed" };
  if (t < 9 * 60 + 15) return { label: "盘前", cls: "pre-market" };
  if (t < 9 * 60 + 30) return { label: "集合竞价", cls: "pre-market" };
  if (t < 15 * 60) return { label: "盘中", cls: "in-session" };
  return { label: "盘后", cls: "post-market" };
}

function isQuotePollingSession(now = new Date()) {
  return getSession(now).cls === "in-session";
}

function getToday() {
  return fmt(new Date());
}

function fmt(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function minutesOfDay(d) {
  return d.getHours() * 60 + d.getMinutes();
}

function isAfterAutoRunTime(d) {
  return minutesOfDay(d) >= 16 * 60 + 30;
}

async function refreshAll() {
  try {
    const payload = await fetchJson("/api/bootstrap");
    if (!payload) throw new Error("服务端没有返回数据");
    data = normalizeState(payload.state || emptyState());
    automation.status = payload.automation_status || null;
    automation.screening = payload.latest_screening || null;
    syncText = `已同步 ${fmtSyncTime(new Date())}`;
  } catch (err) {
    syncText = `同步失败：${err.message}`;
  }
  render();
}

async function refreshRealtimeQuotes(force = false) {
  try {
    const payload = await fetchJson(`/api/realtime_quotes${force ? `?force=${Date.now()}` : ""}`);
    if (!payload) throw new Error("行情接口无返回");
    liveQuotes.quotes = payload.quotes || {};
    liveQuotes.fetchedAt = payload.fetched_at || null;
    liveQuotes.error = payload.error || null;
    quoteText = liveQuotes.error
      ? liveQuotes.error
      : liveQuotes.fetchedAt
        ? `实时行情更新于 ${formatTimeText(liveQuotes.fetchedAt)}`
        : "暂无实时行情";
  } catch (err) {
    liveQuotes.error = err.message;
    quoteText = "实时行情获取失败，请稍后重试";
  }
  render();
}

function startQuotesPolling() {
  clearInterval(quotesTimer);
  refreshRealtimeQuotes();
  quotesTimer = setInterval(() => {
    if (!isQuotePollingSession()) return;
    refreshRealtimeQuotes();
  }, 60000);
}

async function fetchJson(path) {
  try {
    const sep = path.includes("?") ? "&" : "?";
    const res = await fetch(`${path}${sep}t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

async function postJson(path, payload) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return await res.json();
}

function emptyState() {
  return { firstSelectionDate: "", tradingDays: [], rounds: [], logs: [] };
}

function dedupeLogs(logs) {
  const map = new Map();
  (logs || []).forEach((log) => {
    const key = `${log.date || ""}__${log.actionId || ""}`;
    map.set(key, log);
  });
  return Array.from(map.values());
}

function normalizeState(state) {
  const merged = { ...emptyState(), ...state };
  merged.logs = dedupeLogs(merged.logs);
  if (merged.tradingDays.length) {
    const map = new Map((merged.rounds || []).map((r) => [r.selDate, r]));
    const rebuilt = [];
    const first = merged.tradingDays.indexOf(merged.firstSelectionDate);
    if (first >= 0) {
      for (let si = first, n = 1; si < merged.tradingDays.length; si += PARAMS.interval, n++) {
        const selDate = merged.tradingDays[si];
        const buyDate = merged.tradingDays[si + 1];
        if (!buyDate) break;
        const sellDate = merged.tradingDays[Math.min(si + 1 + PARAMS.hold, merged.tradingDays.length - 1)];
        const prev = map.get(selDate);
        rebuilt.push({
          id: prev?.id || `r${n}-${selDate}`,
          num: n,
          selDate,
          buyDate,
          sellDate,
          regime: prev?.regime || "",
          picks: (prev?.picks || []).map((p) => ({ ...emptyPick(), ...p })),
        });
      }
      merged.rounds = rebuilt;
    }
  }
  indexPicksOn(merged);
  return merged;
}

function fmtSyncTime(d) {
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatTimeText(value) {
  try {
    return new Date(value).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return String(value || "");
  }
}

function getLiveQuote(symbol) {
  return liveQuotes.quotes?.[symbol] || null;
}

function getEffectiveLastPrice(pick) {
  const live = getLiveQuote(pick.symbol);
  return live?.close ?? pick.lastPrice ?? null;
}

function getAutoScreeningForRound(round, today) {
  if (!round || !automation.screening) return null;
  if (automation.screening.selection_date === round.selDate) return automation.screening;
  if (automation.screening.next_buy_date === (today || getToday())) return automation.screening;
  if (automation.screening.next_buy_date === round.buyDate) return automation.screening;
  return null;
}

function getCandidatePicks(round, today) {
  const localPicks = (round?.picks || []).filter((p) => p.symbol);
  if (localPicks.length) return localPicks;
  return getAutoScreeningForRound(round, today)?.selected_picks || [];
}

function forEachPick(mutator) {
  data.rounds.forEach((round) => {
    round.picks.forEach((pick) => mutator(pick, round));
  });
}

function findPickBySymbol(symbol) {
  for (const round of data.rounds) {
    for (const pick of round.picks) {
      if (pick.symbol === symbol) return pick;
    }
  }
  return null;
}

function applyActionState(result, actionId) {
  if (result !== "done") return;

  if (actionId === "set-conditional") {
    forEachPick((pick) => {
      if (pick.status === "active" && pick.buyPrice) {
        pick.conditionalSet = true;
      }
    });
    return;
  }

  if (actionId === "mark-sold") {
    forEachPick((pick) => {
      if (pick.status === "active") {
        pick.status = "sold";
      }
    });
    return;
  }

  if (actionId === "buy-all") {
    forEachPick((pick) => {
      if (pick.symbol && (pick.status === "watch" || pick.status === "skipped")) {
        pick.status = "active";
      }
    });
    return;
  }

  if (actionId.startsWith("buy-")) {
    const symbol = actionId.slice(4);
    const pick = findPickBySymbol(symbol);
    if (pick) pick.status = "active";
    return;
  }

  if (actionId.startsWith("sell-")) {
    const symbol = actionId.slice(5);
    const pick = findPickBySymbol(symbol);
    if (pick) pick.status = "sold";
  }
}

function render() {
  const syncEl = $("syncStatus");
  if (syncEl) syncEl.textContent = syncText;
  const quoteEl = $("quoteStatus");
  if (quoteEl) quoteEl.textContent = quoteText;
  renderBriefing();
  renderActions();
  renderAutomation();
  renderHoldings();
  renderLogs();
}

function renderBriefing() {
  const today = getToday();
  const now = new Date();
  const session = getSession(now);
  const round = findRoundForToday(today);
  const stage = round ? getStage(round, today) : null;
  const autoScreening = getAutoScreeningForRound(round, today);

  const activeRound = findActiveHoldingRound(today);
  const activePicks = activeRound
    ? activeRound.picks.filter((p) => p.status === "active")
    : [];

  let invested = 0, mktVal = 0;
  activePicks.forEach((p) => {
    if (p.buyPrice && p.quantity) {
      invested += p.buyPrice * p.quantity;
      mktVal += (getEffectiveLastPrice(p) || p.buyPrice) * p.quantity;
    }
  });
  const cash = PARAMS.initialCapital - invested;
  const pnl = invested > 0 ? ((mktVal - invested) / invested * 100) : 0;

  let roundLabel = "-";
  let dayLabel = "-";
  let stageLabel = "-";
  let regimeLabel = "-";

  if (round) {
    roundLabel = `第 ${round.num} 轮`;
    regimeLabel = round.regime || autoScreening?.regime || "未设置";
  }
  if (activeRound && stage) {
    dayLabel = stage.day ? `第 ${stage.day} 天` : "-";
    stageLabel = stage.label;
  } else if (stage) {
    stageLabel = stage.label;
  }

  let message = "";
  if (!data.tradingDays.length) {
    message = "交易日日历尚未加载。";
  } else if (!round && !activeRound) {
    message = `今天 (${today}) 不在任何轮次范围内，没有需要操作的事项。`;
  } else {
    message = buildTodayMessage(today, now, session, round, stage, activeRound, activePicks, autoScreening);
  }

  $("briefingContent").innerHTML = `
    <div class="briefing-grid">
      <div class="briefing-item">
        <div class="label">当前轮次</div>
        <div class="value">${roundLabel}</div>
        <div class="sub">${stageLabel}</div>
      </div>
      <div class="briefing-item">
        <div class="label">持有天数</div>
        <div class="value">${dayLabel}</div>
        <div class="sub">共 ${PARAMS.hold} 交易日</div>
      </div>
      <div class="briefing-item">
        <div class="label">大盘状态</div>
        <div class="value">${regimeLabel}</div>
        <div class="sub">目标 ${targetCount(round?.regime || autoScreening?.regime)} 只</div>
      </div>
      <div class="briefing-item">
        <div class="label">持仓盈亏</div>
        <div class="value ${pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${pnl ? (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + "%" : "-"}</div>
        <div class="sub">现金 ¥${cash.toLocaleString()}</div>
      </div>
    </div>
    <div class="briefing-message">${message}</div>
  `;
}

function buildTodayMessage(today, now, session, round, stage, activeRound, activePicks, autoScreening) {
  const lines = [];
  const s = session.cls;
  const afterAutoRun = isAfterAutoRunTime(now);
  const candidates = getCandidatePicks(round, today);

  if (stage?.type === "selection") {
    if (s === "pre-market" || s === "in-session") {
      lines.push("今天是<b>选股日</b>。系统会在今晚 <b>16:30</b> 自动执行 v4_fast 选股，为明天买入提供名单。");
      lines.push("白天你不需要操作，等晚上自动结果即可。");
    } else if (!afterAutoRun) {
      lines.push("今天是<b>选股日</b>，刚收盘。系统会在 <b>16:30</b> 自动执行选股，你现在先不用动。");
    } else if (automation.status?.status === "ran" && automation.status?.selection_date === today) {
      lines.push("今天是<b>选股日</b>，16:30 自动选股已完成。");
      if (autoScreening?.selected_picks?.length) {
        lines.push("明日买入名单：" + autoScreening.selected_picks.map((p) => `${p.symbol}(${p.name})`).join("、"));
      }
      lines.push("今晚只需要确认结果，明天开盘执行买入。");
    } else if (automation.status?.status === "error" && automation.status?.selection_date === today) {
      lines.push("今天是<b>选股日</b>，但 16:30 自动选股执行失败了，请检查远端日志。");
    } else {
      lines.push("今天是<b>选股日</b>。后台会在 16:30 后自动执行；如果下方暂时还没结果，等后台线程写入即可。");
    }
  } else if (stage?.type === "buy") {
    const unset = activePicks.filter((p) => p.buyPrice && !p.conditionalSet);
    if (s === "pre-market") {
      lines.push("今天是<b>买入日</b>，即将开盘。");
      lines.push(`按昨晚 16:30 的选股结果，开盘后买入 ${targetCount(round?.regime || autoScreening?.regime)} 只，等权分配资金。`);
      if (candidates.length) {
        lines.push("候选名单：" + candidates.map((p) => `${p.symbol}(${p.name})`).join("、"));
      }
    } else if (s === "in-session") {
      lines.push("今天是<b>买入日</b>，盘中。");
      lines.push("如果还没买入，尽快执行。买完后在下方持仓表填上实际买入价和股数。");
      if (unset.length) {
        lines.push(`<b>还有 ${unset.length} 只未设条件单</b>，买入后立刻设置 -8% 止损条件单。`);
      }
    } else {
      lines.push("今天是<b>买入日</b>，已收盘。");
      if (activePicks.length) {
        lines.push("确认持仓表中买入价和股数已填写。");
        if (unset.length) {
          lines.push(`<b>有 ${unset.length} 只还没设条件单</b>，明天开盘前务必设好！`);
        }
      } else {
        lines.push("持仓表还没有数据，请补录今天的买入情况。");
      }
    }
  } else if (stage?.type === "sell") {
    if (s === "pre-market") {
      lines.push("今天是<b>到期卖出日</b>。开盘后将所有 active 持仓卖出。");
      lines.push("如果今天同时也是下一轮的买入日，先卖完旧仓再买新仓。");
    } else if (s === "in-session") {
      const stillActive = activePicks.filter((p) => p.status === "active");
      if (stillActive.length) {
        lines.push(`今天是<b>到期卖出日</b>，还有 ${stillActive.length} 只未卖出，请尽快操作。`);
      } else {
        lines.push("今天是到期卖出日，所有持仓已标记为 sold，做得好。");
      }
    } else {
      lines.push("今天是<b>到期卖出日</b>，已收盘。确认所有持仓状态已改为 sold。");
    }
  } else if (stage?.type === "holding") {
    const danger = activePicks.filter((p) => {
      const marketPrice = getEffectiveLastPrice(p);
      if (!p.buyPrice || !marketPrice) return false;
      const sl = p.buyPrice * (1 + PARAMS.stopLossPct / 100);
      return marketPrice <= sl * 1.02;
    });
    if (s === "pre-market") {
      lines.push(`持有第 ${stage.day} 天，今天无主动操作。开盘后留意条件单是否正常。`);
    } else if (s === "in-session") {
      lines.push(`持有第 ${stage.day} 天，盘中观察。`);
      if (danger.length) {
        lines.push(`<b>注意：${danger.map((p) => p.symbol).join("、")} 接近止损线，重点盯盘。</b>`);
      } else {
        lines.push("目前无个股接近止损线，条件单保持即可。");
      }
    } else {
      lines.push(`持有第 ${stage.day} 天，已收盘。更新下方最新价，检查明日是否有关键节点。`);
    }
  } else {
    lines.push(`今天不在关键操作节点。如有持仓，盘后可更新最新价。`);
  }

  return lines.join("<br>");
}

function renderActions() {
  const today = getToday();
  const now = new Date();
  const session = getSession(now);
  const actions = generateActions(today, now, session).filter((a) => {
    const rec = getLogForAction(today, a.id);
    return rec?.result !== "done";
  });

  if (!actions.length) {
    $("actionList").innerHTML = `<div class="empty">今天没有待办操作</div>`;
    return;
  }

  $("actionList").innerHTML = actions.map((a) => {
    const rec = getLogForAction(today, a.id);
    const cls = rec ? rec.result : "";
    return `
      <div class="action-item ${cls}" data-aid="${a.id}">
        <div class="action-body">
          <strong>${esc(a.title)}</strong>
          <p>${esc(a.detail)}</p>
        </div>
        <div class="action-buttons">
          <button class="primary" data-do="done" data-aid="${a.id}">已执行</button>
          <button data-do="skipped" data-aid="${a.id}">跳过</button>
          <button data-do="failed" data-aid="${a.id}">失败</button>
        </div>
      </div>
    `;
  }).join("");
}

function generateActions(today, now, session) {
  const actions = [];
  const round = findRoundForToday(today);
  const stage = round ? getStage(round, today) : null;
  const activeRound = findActiveHoldingRound(today);
  const s = session.cls;
  const candidates = getCandidatePicks(round, today);

  if (stage?.type === "selection" && (s === "post-market" || s === "closed")) {
    if (!isAfterAutoRunTime(now)) {
      actions.push({ id: "wait-auto-screening", title: "等待 16:30 自动选股", detail: "今晚系统会自动执行 v4_fast，你现在不用手动跑脚本" });
    } else if (automation.status?.status === "ran" && automation.status?.selection_date === today) {
      actions.push({ id: "review-auto-screening", title: "查看自动选股结果", detail: "确认今晚 16:30 的自动选股结果和大盘状态" });
      actions.push({ id: "prepare-next-buy", title: "准备明日买入", detail: "按自动选股结果，明早开盘执行买入" });
    } else {
      actions.push({ id: "wait-background-run", title: "等待后台自动选股", detail: "16:30 后由后端自动执行，点刷新只会同步显示结果" });
    }
  }

  if (stage?.type === "buy") {
    if (s === "pre-market" || s === "in-session") {
      if (candidates.length) {
        candidates.forEach((p) => {
          actions.push({ id: `buy-${p.symbol}`, title: `买入 ${p.symbol} ${p.name}`, detail: `目标价 ${p.plannedPrice || p.close || "开盘价"}，${p.quantity || "待定"} 股` });
        });
      } else {
        actions.push({ id: "buy-all", title: "执行买入", detail: "按昨晚 16:30 自动选股名单买入" });
      }
    }
    if (s === "in-session" || s === "post-market") {
      actions.push({ id: "fill-buy-price", title: "填写实际买入价", detail: "在持仓表补上实际成交价和股数" });
      actions.push({ id: "set-conditional", title: "设置 -8% 条件单", detail: "为每只 active 持仓挂好止损条件单" });
    }
  }

  if (stage?.type === "sell") {
    const activePicks = activeRound?.picks?.filter((p) => p.status === "active") || [];
    activePicks.forEach((p) => {
      actions.push({ id: `sell-${p.symbol}`, title: `卖出 ${p.symbol} ${p.name}`, detail: `${p.quantity || ""} 股，到期卖出` });
    });
    if (activePicks.length) {
      actions.push({ id: "mark-sold", title: "标记为 sold", detail: "卖出后把持仓状态改成 sold" });
    }
  }

  if (stage?.type === "holding" && (s === "post-market")) {
    actions.push({ id: "update-prices", title: "更新最新价", detail: "填入今天收盘价，检查止损距离" });
  }

  return actions;
}

function renderAutomation() {
  const el = $("autoScreeningContent");
  if (!el) return;

  const status = automation.status;
  const screening = automation.screening;

  if (!status && !screening) {
    el.innerHTML = `<div class="empty">自动选股结果尚未生成</div>`;
    return;
  }

  const metaHtml = status ? `
    <div class="auto-meta">
      <div class="auto-meta-item">
        <strong>触发方式</strong>
        <span>后端后台线程自动判断</span>
      </div>
      <div class="auto-meta-item">
        <strong>实际执行规则</strong>
        <span>仅到 16:30 且命中第 10 个交易日轮次时执行</span>
      </div>
      <div class="auto-meta-item">
        <strong>最近状态</strong>
        <span>${esc(status.status || "-")}</span>
      </div>
      <div class="auto-meta-item">
        <strong>最近检查日</strong>
        <span>${esc(status.today || "-")}</span>
      </div>
    </div>
    <div class="auto-message">${esc(status.message || "后端会在后台自动判断并落文件。")}</div>
  ` : "";

  const picks = screening?.selected_picks || [];
  const screeningHtml = screening ? `
    <div class="auto-meta">
      <div class="auto-meta-item">
        <strong>选股日期</strong>
        <span>${esc(screening.selection_date || "-")}</span>
      </div>
      <div class="auto-meta-item">
        <strong>下次买入日</strong>
        <span>${esc(screening.next_buy_date || "-")}</span>
      </div>
      <div class="auto-meta-item">
        <strong>大盘状态</strong>
        <span>${esc(screening.regime || "-")}</span>
      </div>
      <div class="auto-meta-item">
        <strong>建议买入只数</strong>
        <span>${esc(String(screening.recommended_count || 0))}</span>
      </div>
    </div>
    <div class="screening-list">
      ${picks.length ? picks.map((p) => `
        <div class="screening-item">
          <div>
            <strong>${esc(p.symbol)} ${esc(p.name)}</strong>
            <div class="meta">${esc(p.industry || "-")} / ${esc(p.market || "-")} / 现价 ${esc(String(p.close ?? "-"))}</div>
          </div>
          <div class="score">综合分 ${esc(String(p.composite_score ?? "-"))}</div>
        </div>
      `).join("") : `<div class="empty">最近一次自动选股没有返回候选股</div>`}
    </div>
  ` : "";

  el.innerHTML = `<div class="auto-grid">${metaHtml}${screeningHtml}</div>`;
}

function renderHoldings() {
  const body = $("holdingBody");
  const allPicks = [];
  data.rounds.forEach((r) => {
    r.picks.forEach((p) => {
      if (p.status === "active" || p.status === "watch") {
        allPicks.push({ ...p, roundNum: r.num });
      }
    });
  });

  if (!allPicks.length) {
    body.innerHTML = `<tr><td colspan="10" class="empty">暂无持仓</td></tr>`;
    $("portfolioSummary").textContent = "";
    return;
  }

  body.innerHTML = allPicks.map((p, i) => {
    const live = getLiveQuote(p.symbol);
    const marketPrice = getEffectiveLastPrice(p);
    const sl = p.buyPrice ? +(p.buyPrice * (1 + PARAMS.stopLossPct / 100)).toFixed(2) : "";
    const pnl = p.buyPrice && marketPrice ? ((marketPrice - p.buyPrice) / p.buyPrice * 100) : null;
    const distSl = marketPrice && sl ? ((marketPrice - sl) / sl * 100) : null;
    const pnlCls = pnl === null ? "" : pnl >= 0 ? "pnl-pos" : (distSl !== null && distSl <= 2 ? "pnl-danger" : "pnl-neg");

    return `<tr>
      <td>${esc(p.symbol)}</td>
      <td>${esc(p.name)}</td>
      <td><input type="number" step="100" value="${p.quantity || ""}" data-ri="${p._ri}" data-pi="${p._pi}" data-f="quantity"></td>
      <td><input type="number" step="0.01" value="${p.buyPrice || ""}" data-ri="${p._ri}" data-pi="${p._pi}" data-f="buyPrice"></td>
      <td>${live?.close != null
        ? `<div class="quote-box"><span>${live.close.toFixed(2)}</span><span class="quote-tag">实时</span></div>`
        : `<input type="number" step="0.01" value="${p.lastPrice || ""}" data-ri="${p._ri}" data-pi="${p._pi}" data-f="lastPrice">`
      }</td>
      <td class="${pnlCls}">${pnl !== null ? (pnl >= 0 ? "+" : "") + pnl.toFixed(2) + "%" : "-"}</td>
      <td>${sl || "-"}</td>
      <td class="${distSl !== null && distSl <= 2 ? "pnl-danger" : ""}">${distSl !== null ? distSl.toFixed(1) + "%" : "-"}</td>
      <td>${p.conditionalSet ? "✅" : "❌"}<button class="small-btn" style="margin-left:4px" data-toggle-cond data-ri="${p._ri}" data-pi="${p._pi}">${p.conditionalSet ? "撤" : "设"}</button></td>
      <td><select data-ri="${p._ri}" data-pi="${p._pi}" data-f="status">
        ${["watch", "active", "stopped", "sold", "skipped"].map((s) => `<option ${s === p.status ? "selected" : ""}>${s}</option>`).join("")}
      </select></td>
    </tr>`;
  }).join("");

  let totalInvested = 0, totalMkt = 0, count = 0;
  allPicks.forEach((p) => {
    if (p.status === "active" && p.buyPrice && p.quantity) {
      totalInvested += p.buyPrice * p.quantity;
      totalMkt += (getEffectiveLastPrice(p) || p.buyPrice) * p.quantity;
      count++;
    }
  });
  const totalPnl = totalInvested ? ((totalMkt - totalInvested) / totalInvested * 100).toFixed(2) : 0;
  $("portfolioSummary").innerHTML =
    `${count} 只 active | 投入 ¥${totalInvested.toLocaleString()} | 市值 ¥${totalMkt.toLocaleString()} | 盈亏 <span class="${totalPnl >= 0 ? "pnl-pos" : "pnl-neg"}">${totalPnl >= 0 ? "+" : ""}${totalPnl}%</span> | 现金 ¥${(PARAMS.initialCapital - totalInvested).toLocaleString()}`;
}

function renderLogs() {
  const el = $("operationLog");
  if (!data.logs.length) {
    el.innerHTML = `<div class="empty">暂无操作记录</div>`;
    return;
  }
  el.innerHTML = data.logs.slice(-50).reverse().map((l) => `
    <div class="log-entry">
      <span class="log-time">${l.time}</span>
      <span class="log-badge ${l.result}">${l.result === "done" ? "已执行" : l.result === "skipped" ? "跳过" : "失败"}</span>
      <span>${esc(l.title)}</span>
    </div>
  `).join("");
}

function findRoundForToday(today) {
  return data.rounds.find((r) => {
    return today === r.selDate || today === r.buyDate || today === r.sellDate ||
      (today > r.buyDate && today < r.sellDate && data.tradingDays.includes(today));
  });
}

function findActiveHoldingRound(today) {
  return data.rounds.find((r) => {
    return today >= r.buyDate && today <= r.sellDate;
  });
}

function getStage(round, today) {
  if (today === round.selDate) return { type: "selection", label: "选股日", day: 0 };
  if (today === round.buyDate) return { type: "buy", label: "买入日", day: 1 };
  if (today === round.sellDate) {
    const d = daysBetween(round.buyDate, today);
    return { type: "sell", label: "到期卖出日", day: d + 1 };
  }
  if (today > round.buyDate && today < round.sellDate) {
    const d = daysBetween(round.buyDate, today);
    return { type: "holding", label: `持有第 ${d + 1} 天`, day: d + 1 };
  }
  return null;
}

function daysBetween(a, b) {
  const ia = data.tradingDays.indexOf(a);
  const ib = data.tradingDays.indexOf(b);
  return (ia >= 0 && ib >= 0) ? ib - ia : 0;
}

function targetCount(regime) {
  if (regime === "neutral") return PARAMS.neutralCount;
  if (regime === "bear") return PARAMS.bearCount;
  return PARAMS.bullCount;
}

function rebuildRounds() {
  const days = data.tradingDays;
  const first = days.indexOf(data.firstSelectionDate);
  if (first < 0) return;

  const existingMap = new Map(data.rounds.map((r) => [r.selDate, r]));
  const rounds = [];

  for (let si = first, n = 1; si < days.length; si += PARAMS.interval, n++) {
    const selDate = days[si];
    const buyDate = days[si + 1];
    if (!buyDate) break;
    const sellDate = days[Math.min(si + 1 + PARAMS.hold, days.length - 1)];
    const prev = existingMap.get(selDate);

    rounds.push({
      id: prev?.id || `r${n}-${selDate}`,
      num: n,
      selDate,
      buyDate,
      sellDate,
      regime: prev?.regime || "",
      picks: (prev?.picks || []).map((p, pi) => ({ ...emptyPick(), ...p, _ri: rounds.length, _pi: pi })),
    });
  }

  data.rounds = rounds;
  indexPicks();
}

function indexPicks() {
  data.rounds.forEach((r, ri) => {
    r.picks.forEach((p, pi) => { p._ri = ri; p._pi = pi; });
  });
}

function emptyPick() {
  return { symbol: "", name: "", quantity: "", buyPrice: "", lastPrice: "", plannedPrice: "", status: "watch", conditionalSet: false };
}

function getLogForAction(today, actionId) {
  for (let i = data.logs.length - 1; i >= 0; i--) {
    const log = data.logs[i];
    if (log.date === today && log.actionId === actionId) return log;
  }
  return null;
}

function addLog(actionId, title, result) {
  const now = new Date();
  data.logs.push({
    date: getToday(),
    time: now.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }),
    actionId,
    title,
    result,
  });
}

function bindGlobal() {
  $("refreshBtn").addEventListener("click", async () => {
    syncText = "服务端判断中...";
    render();
    await refreshAll();
  });

  $("refreshQuotesBtn").addEventListener("click", async () => {
    quoteText = "正在刷新实时行情...";
    render();
    await refreshRealtimeQuotes(true);
  });

  $("actionList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-do]");
    if (!btn) return;
    const result = btn.dataset.do;
    const aid = btn.dataset.aid;
    const item = btn.closest(".action-item");
    const title = item?.querySelector("strong")?.textContent || aid;

    const existing = getLogForAction(getToday(), aid);
    if (existing?.result === "done") {
      render();
      return;
    }
    if (existing?.result === result) {
      return;
    }
    if (existing) {
      existing.result = result;
      existing.time = new Date().toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
    } else {
      addLog(aid, title, result);
    }
    applyActionState(result, aid);
    queueSave();
    render();
  });

  $("holdingBody").addEventListener("input", (e) => {
    const t = e.target;
    const ri = Number(t.dataset.ri);
    const pi = Number(t.dataset.pi);
    const f = t.dataset.f;
    if (f == null || isNaN(ri) || isNaN(pi)) return;
    const pick = data.rounds[ri]?.picks[pi];
    if (!pick) return;

    if (["quantity", "buyPrice", "lastPrice"].includes(f)) {
      pick[f] = t.value === "" ? "" : Number(t.value);
    } else {
      pick[f] = t.value;
    }
    queueSave();
    renderBriefing();
    renderActions();
    renderHoldings();
  });

  $("holdingBody").addEventListener("change", (e) => {
    const t = e.target;
    const ri = Number(t.dataset.ri);
    const pi = Number(t.dataset.pi);
    const f = t.dataset.f;
    if (f == null || isNaN(ri) || isNaN(pi)) return;
    const pick = data.rounds[ri]?.picks[pi];
    if (!pick) return;
    pick[f] = t.value;
    queueSave();
    render();
  });

  $("holdingBody").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-toggle-cond]");
    if (!btn) return;
    const ri = Number(btn.dataset.ri);
    const pi = Number(btn.dataset.pi);
    const pick = data.rounds[ri]?.picks[pi];
    if (!pick) return;
    pick.conditionalSet = !pick.conditionalSet;
    queueSave();
    render();
  });

  $("exportBtn").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `v4-live-${getToday()}.json`;
    a.click();
  });
}

function indexPicksOn(d) {
  (d.rounds || []).forEach((r, ri) => {
    (r.picks || []).forEach((p, pi) => { p._ri = ri; p._pi = pi; });
  });
}

function queueSave() {
  syncText = "正在保存到服务端...";
  render();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 300);
}

async function saveNow() {
  try {
    data = normalizeState(data);
    const payload = await postJson("/api/state", data);
    data = normalizeState(payload.state || data);
    syncText = `已保存 ${fmtSyncTime(new Date())}`;
  } catch (err) {
    syncText = `保存失败：${err.message}`;
  }
  render();
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
