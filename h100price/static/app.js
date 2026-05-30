const state = {
  data: null,
  model: "H100",
  source: "all",
  chartMode: "median",
  loading: false,
  chartPoints: [],
  chartHoverId: null,
};

const AUTO_REFRESH_MS = 60 * 1000;

const els = {
  generatedAt: document.getElementById("generatedAt"),
  refreshBtn: document.getElementById("refreshBtn"),
  brandMark: document.getElementById("brandMark"),
  modelTabs: document.getElementById("modelTabs"),
  sourceFilter: document.getElementById("sourceFilter"),
  kpis: document.getElementById("kpis"),
  trendChart: document.getElementById("trendChart"),
  chartTooltip: document.getElementById("chartTooltip"),
  chartModeTabs: document.getElementById("chartModeTabs"),
  tableCount: document.getElementById("tableCount"),
  priceRows: document.getElementById("priceRows"),
  sourceHealth: document.getElementById("sourceHealth"),
  setupList: document.getElementById("setupList"),
  setupCount: document.getElementById("setupCount"),
  signals: document.getElementById("signals"),
  coverage: document.getElementById("coverage"),
  chartSubtitle: document.getElementById("chartSubtitle"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtMoney(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  const numeric = Number(value);
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: numeric >= 1000 ? 0 : digits,
    maximumFractionDigits: numeric >= 1000 ? 0 : digits,
  }).format(numeric);
}

function fmtNumber(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "n/a";
  return Number(value).toFixed(digits);
}

function fmtObserved(value) {
  if (!value) return "n/a";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtDay(value) {
  if (!value) return "n/a";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function fmtMetricPrice(metric) {
  if (metric.unit === "instance_hour") {
    return `${fmtMoney(metric.price)}/inst-hr`;
  }
  return `${fmtMoney(metric.price)}/GPU-hr`;
}

function fmtNormalized(metric) {
  return `${fmtMoney(metric.normalizedPrice)}/GPU-hr`;
}

function scenarioValue(row) {
  if (!row || row.score === null || row.score === undefined) return "n/a";
  if (row.unit === "USD/GPU-hr") return `${fmtMoney(row.score)}/hr`;
  return fmtNumber(row.score, 2);
}

function statisticLabel(mode = state.chartMode) {
  return mode === "average" ? "Average" : "Median";
}

function monitorApiPath(refresh) {
  const params = new URLSearchParams();
  if (refresh) params.set("refresh", "1");
  if (state.model) params.set("model", state.model);
  return `/api/monitor?${params.toString()}`;
}

function staticMonitorPaths() {
  const model = encodeURIComponent(String(state.model || "H100").toLowerCase());
  return [`/data/monitor_${model}.json`, "/data/monitor.json"];
}

async function fetchJson(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} HTTP ${res.status}`);
  return res.json();
}

async function loadData(refresh = false) {
  state.loading = true;
  els.refreshBtn.classList.add("loading");
  els.refreshBtn.disabled = true;
  try {
    try {
      state.data = await fetchJson(monitorApiPath(refresh));
    } catch (apiErr) {
      let staticErr = apiErr;
      for (const path of staticMonitorPaths()) {
        try {
          state.data = await fetchJson(path);
          staticErr = null;
          break;
        } catch (err) {
          staticErr = err;
        }
      }
      if (staticErr) throw staticErr;
    }
    render();
  } catch (err) {
    renderError(err);
  } finally {
    state.loading = false;
    els.refreshBtn.classList.remove("loading");
    els.refreshBtn.disabled = false;
  }
}

function renderError(err) {
  els.generatedAt.textContent = `Load failed: ${err.message}`;
  els.kpis.innerHTML = `<div class="empty">Could not load monitor data.</div>`;
}

function render() {
  const data = state.data;
  if (!data) return;
  const runLabel = data.staticSnapshot ? "Latest data pull" : "Live run";
  els.generatedAt.textContent = `${runLabel} - ${fmtObserved(data.generatedAt)}`;
  els.coverage.textContent = data.coverage || "0/0";
  if (els.brandMark) els.brandMark.textContent = data.model || state.model;
  populateModels(data);
  populateSources(data);
  renderKpis(data);
  renderRows(data);
  renderHealth(data);
  renderSetup(data);
  renderSignals(data);
  renderChart(data);
}

function populateModels(data) {
  if (!els.modelTabs) return;
  const models = (data.models && data.models.length ? data.models : [state.model]);
  const counts = data.modelCounts || {};
  if (!models.includes(state.model)) state.model = data.model || models[0];
  els.modelTabs.innerHTML = models
    .map((model) => {
      const count = counts[model];
      const countLabel = count === undefined ? "" : `<span class="tab-count">${count}</span>`;
      const active = model === state.model ? " active" : "";
      return `<button type="button" role="tab" data-model="${escapeHtml(model)}" class="${active.trim()}" aria-selected="${model === state.model}">${escapeHtml(model)}${countLabel}</button>`;
    })
    .join("");
}

function populateSources(data) {
  const current = state.source;
  const sources = Array.from(new Set((data.metrics || []).map((m) => m.source).concat((data.statuses || []).map((s) => s.source)))).sort();
  const options = [`<option value="all">All sources</option>`]
    .concat(sources.map((source) => `<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`))
    .join("");
  if (els.sourceFilter.innerHTML !== options) {
    els.sourceFilter.innerHTML = options;
    els.sourceFilter.value = sources.includes(current) ? current : "all";
    state.source = els.sourceFilter.value;
  }
}

function filteredMetrics(data) {
  return (data.metrics || [])
    .filter((m) => state.source === "all" || m.source === state.source)
    .sort((a, b) => a.normalizedPrice - b.normalizedPrice);
}

function renderKpis(data) {
  const summary = data.summary || {};
  const sourceCounts = data.sourceCounts || {};
  const modelLabel = data.model || state.model || "GPU";
  const cards = [
    {
      label: "Lowest rental",
      value: summary.rentalMin === null || summary.rentalMin === undefined ? "n/a" : `${fmtMoney(summary.rentalMin)}/hr`,
      note: `normalized per ${modelLabel} GPU`,
    },
    {
      label: "Rental median",
      value: summary.rentalMedian === null || summary.rentalMedian === undefined ? "n/a" : `${fmtMoney(summary.rentalMedian)}/hr`,
      note: `${fmtMoney(summary.rentalP25)} to ${fmtMoney(summary.rentalP75)} middle range`,
    },
    {
      label: "Rental average",
      value: summary.rentalAverage === null || summary.rentalAverage === undefined ? "n/a" : `${fmtMoney(summary.rentalAverage)}/hr`,
      note: "mean across latest run",
    },
    {
      label: "Coverage",
      value: data.coverage || "0/0",
      note: `${sourceCounts.observations || 0} observations stored`,
    },
  ];
  els.kpis.innerHTML = cards
    .map(
      (card) => `
        <article class="kpi">
          <label>${escapeHtml(card.label)}</label>
          <strong>${escapeHtml(card.value)}</strong>
          <span>${escapeHtml(card.note)}</span>
        </article>
      `,
    )
    .join("");
}

function renderRows(data) {
  const rows = filteredMetrics(data);
  els.tableCount.textContent = `${rows.length} matching observations`;
  if (!rows.length) {
    els.priceRows.innerHTML = `<tr><td colspan="6"><div class="empty">No prices match the current filters.</div></td></tr>`;
    return;
  }
  els.priceRows.innerHTML = rows
    .map((m) => {
      const href = m.link ? escapeHtml(m.link) : "#";
      return `
        <tr>
          <td><strong>${escapeHtml(m.source)}</strong></td>
          <td>
            <a class="item-link" href="${href}" target="_blank" rel="noreferrer">${escapeHtml(m.title)}</a>
            <div class="health-meta">${m.gpuCount} GPU${m.gpuCount === 1 ? "" : "s"}</div>
          </td>
          <td>${escapeHtml(m.condition || "n/a")}<div class="health-meta">${escapeHtml(m.availability || "")}</div></td>
          <td><strong>${escapeHtml(fmtMetricPrice(m))}</strong></td>
          <td><strong>${escapeHtml(fmtNormalized(m))}</strong></td>
          <td>${escapeHtml(fmtObserved(m.observedAt))}</td>
        </tr>
      `;
    })
    .join("");
}

function renderHealth(data) {
  const statuses = data.statuses || [];
  if (!statuses.length) {
    els.sourceHealth.innerHTML = `<div class="empty">No source runs yet.</div>`;
    return;
  }
  els.sourceHealth.innerHTML = statuses
    .map((s) => {
      const detail = s.ok
        ? `${s.count} rows, ${s.latencyMs} ms`
        : `${s.error || "not connected"}${s.requires ? ` - requires ${s.requires}` : ""}`;
      return `
        <div class="health-row">
          <div class="health-title">
            <a href="${escapeHtml(s.link || "#")}" target="_blank" rel="noreferrer">${escapeHtml(s.source)}</a>
            <span class="status-dot ${s.ok ? "ok" : ""}"></span>
          </div>
          <div class="health-meta">${escapeHtml(detail)}</div>
        </div>
      `;
    })
    .join("");
}

function renderSetup(data) {
  const missing = (data.statuses || []).filter((s) => !s.ok && s.requires);
  els.setupCount.textContent = `${missing.length} missing`;
  if (!missing.length) {
    els.setupList.innerHTML = `<div class="setup-row">Credentialed sources are configured for this server process.</div>`;
    return;
  }
  els.setupList.innerHTML = missing
    .map((s) => {
      const command = setupCommand(s.source);
      return `
        <div class="setup-row">
          <strong>${escapeHtml(s.source)}</strong>
          <span>${escapeHtml(s.error || s.requires)}</span>
          ${command ? `<code>${escapeHtml(command)}</code>` : ""}
        </div>
      `;
    })
    .join("");
}

function setupCommand(source) {
  if (source === "Vast.ai") return "export VAST_API_KEY=...";
  return "";
}

function renderSignals(data) {
  const scenarios = data.scenarios || [];
  const tracker = ((data.config || {}).tracker) || {};
  const rows = scenarios
    .map(
      (s) => `
        <div class="signal-row">
          <strong>${escapeHtml(s.name)}</strong>
          <span>${escapeHtml(scenarioValue(s))} ${s.unit ? escapeHtml(s.unit.replace("USD/", "")) : ""}</span>
        </div>
      `,
    )
    .join("");
  const interval = Number(tracker.intervalHours || 0);
  const samplesPerDay = interval ? 24 / interval : null;
  const tracking = data.staticSnapshot
    ? `<div class="signal-row"><strong>Latest data pull</strong><span>Data pulled ${escapeHtml(fmtObserved(data.generatedAt))}.</span></div>`
    : tracker.enabled && tracker.mode === "interval"
    ? `<div class="signal-row"><strong>Intraday tracking</strong><span>Every ${escapeHtml(interval)} hours (${escapeHtml(fmtNumber(samplesPerDay, samplesPerDay % 1 === 0 ? 0 : 1))} samples/day). Next run ${escapeHtml(fmtObserved(tracker.nextRunAt))}.</span></div>`
    : tracker.enabled
      ? `<div class="signal-row"><strong>Daily tracking</strong><span>On at ${escapeHtml(tracker.runAt || "09:00")} local. Next run ${escapeHtml(fmtObserved(tracker.nextRunAt))}.</span></div>`
      : `<div class="signal-row"><strong>Tracking</strong><span>Off. Start with python3 server.py --track-interval --track-every-hours 1.</span></div>`;
  const lastRun = tracker.lastFinishedAt
    ? `<div class="signal-row"><strong>Last auto run</strong><span>${escapeHtml(fmtObserved(tracker.lastFinishedAt))}${tracker.lastError ? ` - ${escapeHtml(tracker.lastError)}` : ""}</span></div>`
    : "";
  els.signals.innerHTML = rows + tracking + lastRun || `<div class="empty">No signals yet.</div>`;
}

function median(values) {
  if (!values.length) return null;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function average(values) {
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function aggregateHistory(history, mode) {
  const buckets = new Map();
  for (const row of history || []) {
    if (row.market && row.market !== "rental") continue;
    const date = new Date(row.observedAt);
    if (Number.isNaN(date.getTime())) continue;
    const key = row.observedAt.slice(0, 10);
    if (!buckets.has(key)) buckets.set(key, []);
    const value = Number(row.normalizedPrice);
    if (!Number.isNaN(value)) {
      buckets.get(key).push({
        value,
        source: row.source || "Unknown",
        title: row.title || "",
      });
    }
  }
  return Array.from(buckets.entries())
    .map(([key, rows]) => {
      const values = rows.map((row) => row.value);
      const sources = Array.from(new Set(rows.map((row) => row.source))).sort();
      return {
        key,
        value: mode === "average" ? average(values) : median(values),
        median: median(values),
        average: average(values),
        min: values.length ? Math.min(...values) : null,
        max: values.length ? Math.max(...values) : null,
        count: values.length,
        sources,
      };
    })
    .filter((row) => row.value !== null)
    .sort((a, b) => a.key.localeCompare(b.key));
}

function renderChart(data) {
  const canvas = els.trendChart;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const width = rect.width;
  const height = rect.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);
  state.chartPoints = [];

  const modeLabel = statisticLabel();
  const rental = aggregateHistory(data.history || [], state.chartMode);
  const modelLabel = data.model || state.model;
  els.chartSubtitle.textContent = `Daily ${modeLabel.toLowerCase()} rental price per ${modelLabel} GPU-hour`;

  if (!rental.length) {
    ctx.fillStyle = "#67706c";
    ctx.font = "13px system-ui";
    ctx.fillText("No history yet. Run a refresh or start interval tracking.", 18, 36);
    hideChartTooltip();
    return;
  }

  const margin = { left: 56, right: 22, top: 18, bottom: 34 };
  const plot = {
    x: margin.left,
    y: margin.top,
    w: width - margin.left - margin.right,
    h: height - margin.top - margin.bottom,
  };

  ctx.strokeStyle = "#dfe4df";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i <= 4; i += 1) {
    const y = plot.y + (plot.h * i) / 4;
    ctx.moveTo(plot.x, y);
    ctx.lineTo(plot.x + plot.w, y);
  }
  ctx.stroke();

  const allKeys = rental.map((p) => p.key);
  const xFor = (key) => {
    if (allKeys.length <= 1) return plot.x + plot.w / 2;
    return plot.x + (plot.w * allKeys.indexOf(key)) / (allKeys.length - 1);
  };

  function scaleFor(series) {
    const values = series.map((p) => p.value);
    if (!values.length) return { min: 0, max: 1 };
    let min = Math.min(...values);
    let max = Math.max(...values);
    if (min === max) {
      const pad = Math.max(1, min * 0.1);
      min -= pad;
      max += pad;
    } else {
      const pad = (max - min) * 0.16;
      min -= pad;
      max += pad;
    }
    return { min, max };
  }

  const rentalScale = scaleFor(rental);

  const yFor = (value, scale) => plot.y + plot.h - ((value - scale.min) / (scale.max - scale.min)) * plot.h;

  function drawAxis(scale, side, formatter, color) {
    ctx.fillStyle = color;
    ctx.font = "11px system-ui";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i += 1) {
      const value = scale.max - ((scale.max - scale.min) * i) / 4;
      const y = plot.y + (plot.h * i) / 4;
      const text = formatter(value);
      if (side === "left") {
        ctx.textAlign = "right";
        ctx.fillText(text, plot.x - 10, y);
      } else {
        ctx.textAlign = "left";
        ctx.fillText(text, plot.x + plot.w + 10, y);
      }
    }
  }

  function drawSeries(series, scale, color, market, label) {
    if (!series.length) return;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    series.forEach((point, index) => {
      const x = xFor(point.key);
      const y = yFor(point.value, scale);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    for (const point of series) {
      const x = xFor(point.key);
      const y = yFor(point.value, scale);
      const id = `${market}:${point.key}`;
      state.chartPoints.push({
        id: `${state.chartMode}:${id}`,
        market,
        label,
        methodLabel: modeLabel,
        color,
        x,
        y,
        key: point.key,
        value: point.value,
        median: point.median,
        average: point.average,
        min: point.min,
        max: point.max,
        count: point.count,
        sources: point.sources,
      });
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = color;
      ctx.lineWidth = state.chartHoverId === `${state.chartMode}:${id}` ? 3 : 2;
      ctx.beginPath();
      ctx.arc(x, y, state.chartHoverId === `${state.chartMode}:${id}` ? 6 : 4, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
    }
  }

  drawAxis(rentalScale, "left", (v) => `$${v.toFixed(1)}`, "#087f70");
  drawSeries(rental, rentalScale, "#087f70", "rental", "Rental");

  ctx.fillStyle = "#67706c";
  ctx.font = "11px system-ui";
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  if (allKeys.length) ctx.fillText(allKeys[0], plot.x, plot.y + plot.h + 12);
  if (allKeys.length > 1) {
    ctx.textAlign = "right";
    ctx.fillText(allKeys[allKeys.length - 1], plot.x + plot.w, plot.y + plot.h + 12);
  }
}

function tooltipPrice(point) {
  return `${fmtMoney(point.value)}/GPU-hr`;
}

function tooltipRange(point) {
  if (point.min === null || point.max === null || point.min === point.max) return "";
  return `${fmtMoney(point.min)} - ${fmtMoney(point.max)}/GPU-hr`;
}

function showChartTooltip(point, canvasX, canvasY) {
  if (!els.chartTooltip) return;
  const range = tooltipRange(point);
  const sources = (point.sources || []).slice(0, 3).join(", ");
  els.chartTooltip.innerHTML = `
    <strong>${escapeHtml(point.label)} ${escapeHtml(point.methodLabel.toLowerCase())} ${escapeHtml(tooltipPrice(point))}</strong>
    <span>${escapeHtml(fmtDay(point.key))}</span>
    <span>${escapeHtml(point.count)} observation${point.count === 1 ? "" : "s"}${sources ? ` from ${escapeHtml(sources)}` : ""}</span>
    ${range ? `<span>Range ${escapeHtml(range)}</span>` : ""}
  `;
  const canvasRect = els.trendChart.getBoundingClientRect();
  const sectionRect = els.chartTooltip.parentElement.getBoundingClientRect();
  const tooltipWidth = 260;
  const maxLeft = Math.max(8, sectionRect.width - tooltipWidth - 8);
  const x = Math.min(Math.max(canvasRect.left - sectionRect.left + canvasX + 14, 8), maxLeft);
  const y = Math.max(canvasRect.top - sectionRect.top + canvasY - 44, 8);
  els.chartTooltip.style.left = `${x}px`;
  els.chartTooltip.style.top = `${y}px`;
  els.chartTooltip.classList.add("visible");
}

function hideChartTooltip() {
  state.chartHoverId = null;
  if (els.chartTooltip) els.chartTooltip.classList.remove("visible");
}

function nearestChartPoint(canvasX, canvasY) {
  let nearest = null;
  let nearestDistance = Infinity;
  for (const point of state.chartPoints) {
    const dx = point.x - canvasX;
    const dy = point.y - canvasY;
    const distance = Math.sqrt(dx * dx + dy * dy);
    if (distance < nearestDistance) {
      nearest = point;
      nearestDistance = distance;
    }
  }
  return nearestDistance <= 18 ? nearest : null;
}

function handleChartMove(event) {
  if (!state.data) return;
  const rect = els.trendChart.getBoundingClientRect();
  const canvasX = event.clientX - rect.left;
  const canvasY = event.clientY - rect.top;
  const point = nearestChartPoint(canvasX, canvasY);
  els.trendChart.style.cursor = point ? "crosshair" : "default";
  if (!point) {
    if (state.chartHoverId) {
      hideChartTooltip();
      renderChart(state.data);
    }
    return;
  }
  const previous = state.chartHoverId;
  state.chartHoverId = point.id;
  if (previous !== point.id) renderChart(state.data);
  showChartTooltip(point, point.x, point.y);
}

els.refreshBtn.addEventListener("click", () => loadData(true));

els.chartModeTabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mode]");
  if (!button) return;
  state.chartMode = button.dataset.mode === "average" ? "average" : "median";
  state.chartHoverId = null;
  hideChartTooltip();
  for (const item of els.chartModeTabs.querySelectorAll("button")) {
    item.classList.toggle("active", item === button);
  }
  if (state.data) renderChart(state.data);
});

els.modelTabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-model]");
  if (!button) return;
  const model = button.dataset.model;
  if (!model || model === state.model) return;
  state.model = model;
  state.source = "all";
  state.chartHoverId = null;
  hideChartTooltip();
  loadData(false);
});

els.sourceFilter.addEventListener("change", (event) => {
  state.source = event.target.value;
  render();
});

window.addEventListener("resize", () => {
  if (state.data) renderChart(state.data);
});

els.trendChart.addEventListener("mousemove", handleChartMove);
els.trendChart.addEventListener("mouseleave", () => {
  if (!state.chartHoverId) return;
  hideChartTooltip();
  if (state.data) renderChart(state.data);
});

loadData(false);

window.setInterval(() => {
  if (!state.loading) loadData(false);
}, AUTO_REFRESH_MS);
