(function () {
    const API_BASE = '';
    const DEFAULT_SYMBOLS = ['RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'BHARTIARTL', 'ITC'];
    const VIEW_RANGES = [365, 1095, 1825, 3650, 99999];
    const LAYOUT_BUCKETS = [1, 2, 4, 6, 8];
    const DEFAULT_METRICS = ['Sales', 'Operating Profit', 'PE'];
    const STORAGE_KEYS = {
        metrics: 'screenerMetrics',
        chartCount: 'chartCount',
        chartTheme: 'chartTheme',
        globalViewRange: 'globalViewRange',
        barType: 'screenerBarType',
        paneConfigs: 'screenerPaneConfigs',
        sync: 'screenerSync',
    };
    let metricCatalog = [];
    let metricMeta = {};
    let panes = [];
    let chartCount = parseInt(localStorage.getItem(STORAGE_KEYS.chartCount) || '4', 10);
    let chartTheme = localStorage.getItem(STORAGE_KEYS.chartTheme) || 'dark';
    let globalViewRange = parseInt(localStorage.getItem(STORAGE_KEYS.globalViewRange) || '1825', 10);
    globalViewRange = VIEW_RANGES.includes(globalViewRange) ? globalViewRange : 1825;
    let globalChartType = 'line';
    let syncState = { symbol: true };
    let selectedMetrics = (() => {
        try {
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEYS.metrics) || 'null');
            return Array.isArray(saved) && saved.length ? saved : DEFAULT_METRICS.slice();
        } catch (e) {
            return DEFAULT_METRICS.slice();
        }
    })();

    const THEME_COLORS = {
        dark: { bg: '#131722', grid: '#1e222d', text: '#d1d4dc', crossLine: '#758696', axisBg: '#2a2e39', borderCol: '#2a2e39', line: '#2962ff', areaTop: 'rgba(41, 98, 255, 0.35)', areaBottom: 'rgba(41, 98, 255, 0.05)', up: '#089981', down: '#f23645' },
        light: { bg: '#ffffff', grid: '#eef0f3', text: '#131722', crossLine: '#9098a8', axisBg: '#d6d8e0', borderCol: '#d6d8e0', line: '#1565c0', areaTop: 'rgba(21, 101, 192, 0.28)', areaBottom: 'rgba(21, 101, 192, 0.04)', up: '#089981', down: '#d32f2f' },
    };

    function fmt(value, digits = 2) {
        if (value === null || value === undefined || Number.isNaN(value)) return 'N/A';
        return new Intl.NumberFormat('en-IN', {
            maximumFractionDigits: digits,
            minimumFractionDigits: Math.abs(value % 1) > 0.0001 ? Math.min(2, digits) : 0,
        }).format(value);
    }

    function nextLayoutCount(metricCount) {
        return LAYOUT_BUCKETS.find((count) => count >= Math.max(1, metricCount)) || 8;
    }

    async function fetchJSON(url, options) {
        const response = await fetch(url, options);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Request failed');
        return data;
    }

    function saveState() {
        localStorage.setItem(STORAGE_KEYS.metrics, JSON.stringify(selectedMetrics));
        localStorage.setItem(STORAGE_KEYS.chartCount, String(chartCount));
        localStorage.setItem(STORAGE_KEYS.chartTheme, chartTheme);
        localStorage.setItem(STORAGE_KEYS.globalViewRange, String(globalViewRange));
        const paneConfigs = panes.map((pane) => ({ symbol: pane.symbol, metric: pane.metric }));
        localStorage.setItem(STORAGE_KEYS.paneConfigs, JSON.stringify(paneConfigs));
        const payload = {};
        Object.keys(STORAGE_KEYS).forEach((key) => {
            const storageKey = STORAGE_KEYS[key];
            const value = localStorage.getItem(storageKey);
            if (value !== null) payload[storageKey] = value;
        });
        fetch('/api/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), keepalive: true }).catch(() => {});
    }

    function openFlyout(button, flyout) {
        document.querySelectorAll('.sidebar-flyout').forEach((node) => node.classList.remove('open'));
        const rect = button.getBoundingClientRect();
        flyout.style.left = (rect.right + 6) + 'px';
        flyout.style.top = rect.top + 'px';
        flyout.classList.add('open');
    }

    function closeFlyouts() {
        document.querySelectorAll('.sidebar-flyout').forEach((node) => node.classList.remove('open'));
        panes.forEach((pane) => pane.el.querySelector('.symbol-dropdown').classList.remove('visible'));
    }

    function setupPassiveFlyouts() {
        [
            ['lineToolsBtn', 'lineToolsFlyout'],
            ['shapeToolsBtn', 'shapeToolsFlyout'],
        ].forEach(([btnId, flyoutId]) => {
            const btn = document.getElementById(btnId);
            const flyout = document.getElementById(flyoutId);
            if (!btn || !flyout) return;
            btn.addEventListener('click', (e) => { e.stopPropagation(); openFlyout(btn, flyout); });
        });
    }

    function setupControls() {
        const timeBtn = document.getElementById('timeMenuBtn');
        const chartBtn = document.getElementById('chartMenuBtn');
        const valuesBtn = document.getElementById('valuesMenuBtn');
        const themeBtn = document.getElementById('themeToggleBtn');
        const timeLabels = { '365': '1Y', '1095': '3Y', '1825': '5Y', '3650': '10Y', '99999': 'MAX' };

        timeBtn.textContent = timeLabels[String(globalViewRange)] || '1Y';
        chartBtn.textContent = String(chartCount);
        valuesBtn.textContent = String(selectedMetrics.length);
        valuesBtn.classList.toggle('has-active', selectedMetrics.length > 0);

        timeBtn.addEventListener('click', (e) => { e.stopPropagation(); openFlyout(timeBtn, document.getElementById('timeFlyout')); });
        chartBtn.addEventListener('click', (e) => { e.stopPropagation(); openFlyout(chartBtn, document.getElementById('chartFlyout')); });
        valuesBtn.addEventListener('click', (e) => { e.stopPropagation(); openFlyout(valuesBtn, document.getElementById('valuesFlyout')); });
        themeBtn.addEventListener('click', () => { applyTheme(chartTheme === 'dark' ? 'light' : 'dark'); saveState(); });

        document.getElementById('timeFlyout').querySelectorAll('.sidebar-flyout-row').forEach((row) => {
            row.classList.toggle('selected', row.dataset.val === String(globalViewRange));
            row.addEventListener('click', (e) => {
                e.stopPropagation();
                globalViewRange = parseInt(row.dataset.val, 10);
                timeBtn.textContent = timeLabels[row.dataset.val] || '1Y';
                document.getElementById('timeFlyout').querySelectorAll('.sidebar-flyout-row').forEach((item) => item.classList.toggle('selected', item === row));
                closeFlyouts();
                panes.forEach((pane) => applyVisibleRange(pane));
                saveState();
            });
        });

        document.getElementById('chartFlyout').querySelectorAll('.sidebar-flyout-row').forEach((row) => {
            row.classList.toggle('selected', row.dataset.val === String(chartCount));
            row.addEventListener('click', (e) => {
                e.stopPropagation();
                setChartCount(parseInt(row.dataset.val, 10), false);
                closeFlyouts();
                saveState();
            });
        });


        document.getElementById('pageFullscreen').addEventListener('click', togglePageFullscreen);
        document.getElementById('pageScreenshot').addEventListener('click', takePageScreenshot);
        document.addEventListener('click', (event) => {
            if (!event.target.closest('.drawing-sidebar') && !event.target.closest('.sidebar-flyout') && !event.target.closest('.symbol-input-wrap')) {
                closeFlyouts();
            }
        });
    }

    async function loadMetricCatalog() {
        const data = await fetchJSON('/api/metrics');
        metricCatalog = data.metrics || [];
        metricMeta = data.metricMeta || {};
        selectedMetrics = selectedMetrics.filter((metric) => metricCatalog.includes(metric));
        if (!selectedMetrics.length) {
            selectedMetrics = DEFAULT_METRICS.filter((metric) => metricCatalog.includes(metric));
        }
        if (!selectedMetrics.length && metricCatalog.length) {
            selectedMetrics = metricCatalog.slice(0, Math.min(3, metricCatalog.length));
        }
        renderValuesFlyout();
        applyFreqTimeframe();
    }

    function getEffectiveMetrics() {
        const out = [];
        selectedMetrics.forEach((metric) => {
            if (metricCatalog.includes(metric) && !out.includes(metric)) {
                out.push(metric);
            }
        });
        return out;
    }

    // Determine whether the current metric selection should be charted on a
    // quarterly or annual timeframe.
    //   - every selected metric supports quarterly (Q/A) -> 'quarterly'
    //   - any selected metric is annual-only (A)         -> 'annual'
    function computeSelectionFreq() {
        const effective = getEffectiveMetrics();
        if (!effective.length) return 'annual';
        const allQuarterly = effective.every((m) => ((metricMeta[m] || {}).freq || 'A') === 'Q/A');
        return allQuarterly ? 'quarterly' : 'annual';
    }

    // Auto-select a sensible time range based on selection frequency.
    // Quarterly selections get a denser/shorter default window; annual or
    // mixed selections fall back to the wider annual window.
    function applyFreqTimeframe() {
        // Both quarterly (annual+TTM) and annual metrics carry full ~12y history
        // from Screener, so default to MAX so data from 2015 is visible.
        const target = 99999;
        if (globalViewRange === target) return;
        globalViewRange = target;
        const timeLabels = { '365': '1Y', '1095': '3Y', '1825': '5Y', '3650': '10Y', '99999': 'MAX' };
        const timeBtn = document.getElementById('timeMenuBtn');
        if (timeBtn) timeBtn.textContent = timeLabels[String(target)] || '5Y';
        const flyout = document.getElementById('timeFlyout');
        if (flyout) {
            flyout.querySelectorAll('.sidebar-flyout-row').forEach((row) => row.classList.toggle('selected', row.dataset.val === String(target)));
        }
    }

    function renderValuesFlyout() {
        const list = document.getElementById('valuesList');
        const meta = document.getElementById('valuesMeta');
        const btn = document.getElementById('valuesMenuBtn');
        const effective = getEffectiveMetrics();
        btn.textContent = String(selectedMetrics.length);
        btn.classList.toggle('has-active', selectedMetrics.length > 0);
        meta.textContent = `${selectedMetrics.length} selected (${effective.length} plotted)`;
        list.innerHTML = '';
        metricCatalog.forEach((metric) => {
            const row = document.createElement('div');
            row.className = 'sidebar-flyout-row';
            row.dataset.metric = metric;
            row.classList.toggle('selected', selectedMetrics.includes(metric));
            const freq = (metricMeta[metric] || {}).freq || '';
            const freqBadge = freq ? ` <span style="font-size:10px;opacity:0.55;font-weight:400">(${freq})</span>` : '';
            row.innerHTML = `<span>${metric}${freqBadge}</span>`;
            row.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectedMetrics.includes(metric)) {
                    if (selectedMetrics.length === 1) return;
                    selectedMetrics = selectedMetrics.filter((item) => item !== metric);
                } else {
                    selectedMetrics = selectedMetrics.concat(metric);
                }
                btn.textContent = String(selectedMetrics.length);
                btn.classList.toggle('has-active', selectedMetrics.length > 0);
                renderValuesFlyout();
                applyFreqTimeframe();
                panes.forEach((pane) => loadPaneData(pane));
                saveState();
            });
            list.appendChild(row);
        });
    }

    function applyTheme(theme) {
        chartTheme = theme;
        document.body.classList.toggle('theme-light', theme === 'light');
        const colors = THEME_COLORS[theme] || THEME_COLORS.dark;
        panes.forEach((pane) => {
            pane.chart.applyOptions({
                layout: { background: { type: 'solid', color: colors.bg }, textColor: colors.text },
                grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
                crosshair: {
                    vertLine: { color: colors.crossLine, labelBackgroundColor: colors.axisBg },
                    horzLine: { color: colors.crossLine, labelBackgroundColor: colors.axisBg },
                },
                rightPriceScale: { borderColor: colors.borderCol },
                timeScale: { borderColor: colors.borderCol },
            });
        });
    }

    function loadSavedPaneConfigs() {
        try {
            const raw = JSON.parse(localStorage.getItem(STORAGE_KEYS.paneConfigs) || '[]');
            return Array.isArray(raw) ? raw : [];
        } catch (e) {
            return [];
        }
    }

    function autoAssignMetrics() {
        panes.forEach((pane) => {
            pane.metric = selectedMetrics[0] || '';
        });
    }

    function setChartCount(count, autoRefresh) {
        chartCount = LAYOUT_BUCKETS.includes(count) ? count : 4;
        document.getElementById('chartMenuBtn').textContent = String(chartCount);
        document.getElementById('chartFlyout').querySelectorAll('.sidebar-flyout-row').forEach((row) => row.classList.toggle('selected', row.dataset.val === String(chartCount)));
        const grid = document.getElementById('chartGrid');
        grid.setAttribute('data-count', String(chartCount));
        panes.forEach((pane) => {
            if (pane.resizeObserver) {
                try { pane.resizeObserver.disconnect(); } catch (e) {}
            }
            try { pane.chart.remove(); } catch (e) {}
        });
        panes = [];
        grid.innerHTML = '';
        const savedConfigs = loadSavedPaneConfigs();
        for (let i = 0; i < chartCount; i++) {
            const saved = savedConfigs[i] || {};
            createPane(grid, i, saved.symbol || DEFAULT_SYMBOLS[i] || 'RELIANCE', selectedMetrics[0] || saved.metric || '');
        }
        if (autoRefresh !== false) panes.forEach((pane) => loadPaneData(pane));
        applyTheme(chartTheme);
    }

    function createPane(container, idx, symbol, metric) {
        const paneEl = document.createElement('div');
        paneEl.className = 'chart-pane';
        paneEl.id = `pane-${idx}`;
        paneEl.innerHTML = `
            <div class="chart-container">
                <div class="loading-overlay active"><div class="loading-spinner"></div></div>
                <div class="chart-watermark">${metric || 'No Metric'}</div>
                <div class="chart-overlay-top">
                    <div class="symbol-input-wrap">
                        <input type="text" class="symbol-input" value="${symbol}" placeholder="Symbol..." autocomplete="off" spellcheck="false">
                        <div class="symbol-dropdown"></div>
                    </div>
                    <div class="ohlc-legend">Waiting for data...</div>
                    <div class="ticker-bar"><span class="ticker-ltp">--</span><span class="ticker-change">--</span></div>
                </div>
                <div class="chart-overlay-right">
                    <button class="pane-btn btn-screenshot" title="Screenshot"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="12" cy="13" r="3"/><path d="M5 7h.01M19 7h.01"/></svg></button>
                    <button class="pane-btn btn-fullscreen" title="Fullscreen"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg></button>
                </div>
                <div class="pane-empty-note"></div>
            </div>`;
        container.appendChild(paneEl);
        const chartContainer = paneEl.querySelector('.chart-container');
        const colors = THEME_COLORS[chartTheme] || THEME_COLORS.dark;
        const chart = LightweightCharts.createChart(chartContainer, {
            layout: { background: { type: 'solid', color: colors.bg }, textColor: colors.text, fontSize: 11 },
            grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: { color: colors.crossLine, width: 1, style: 3, labelBackgroundColor: colors.axisBg },
                horzLine: { color: colors.crossLine, width: 1, style: 3, labelBackgroundColor: colors.axisBg },
            },
            rightPriceScale: { borderColor: colors.borderCol },
            timeScale: {
                borderColor: colors.borderCol,
                timeVisible: true,
                secondsVisible: false,
                tickMarkFormatter: (time) => {
                    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
                    let y, m;
                    if (time && typeof time === 'object' && 'year' in time) {
                        y = time.year; m = time.month;
                    } else if (typeof time === 'string') {
                        const parts = time.split('-');
                        y = parseInt(parts[0], 10); m = parseInt(parts[1], 10);
                    }
                    if (y && m) return `${months[m - 1]} '${String(y).slice(-2)}`;
                    return String(time);
                },
            },
        });
        const pane = { id: idx, el: paneEl, chart, series: null, metricSeries: [], symbol, metric, metricData: null, stockData: null, allMetrics: [], resizeObserver: null };
        panes.push(pane);
        setupSymbolInput(pane);
        setupPaneActions(pane);
        const ro = new ResizeObserver(() => chart.applyOptions({ width: chartContainer.clientWidth, height: chartContainer.clientHeight }));
        ro.observe(chartContainer);
        pane.resizeObserver = ro;
    }

    function setupSymbolInput(pane) {
        const input = pane.el.querySelector('.symbol-input');
        const dropdown = pane.el.querySelector('.symbol-dropdown');
        let timer = null;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => showDropdown(pane, input.value), 150);
        });
        input.addEventListener('focus', () => {
            if (input.value.trim().length >= 2) showDropdown(pane, input.value);
        });
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                dropdown.classList.remove('visible');
                changeSymbol(pane, input.value.trim().toUpperCase());
            }
            if (event.key === 'Escape') dropdown.classList.remove('visible');
        });
    }

    async function showDropdown(pane, query) {
        const dropdown = pane.el.querySelector('.symbol-dropdown');
        const q = query.trim();
        if (q.length < 2) {
            dropdown.classList.remove('visible');
            return;
        }
        try {
            const data = await fetchJSON(`${API_BASE}/api/search?q=${encodeURIComponent(q)}`);
            const results = (data.results || []).slice(0, 12);
            if (!results.length) {
                dropdown.classList.remove('visible');
                return;
            }
            dropdown.innerHTML = results.map((item) => `<div class="symbol-option" data-symbol="${item.ticker}"><strong>${item.ticker}</strong><span>${item.name}</span></div>`).join('');
            dropdown.classList.add('visible');
            dropdown.querySelectorAll('.symbol-option').forEach((node) => {
                node.addEventListener('click', () => {
                    dropdown.classList.remove('visible');
                    changeSymbol(pane, node.dataset.symbol);
                });
            });
        } catch (e) {
            dropdown.classList.remove('visible');
        }
    }

    function changeSymbol(pane, symbol) {
        if (!symbol) return;
        const oldSymbol = pane.symbol;
        pane.symbol = symbol;
        pane.el.querySelector('.symbol-input').value = symbol;
        loadPaneData(pane);
        if (true) {
            panes.forEach((target) => {
                if (target !== pane && target.symbol === oldSymbol) {
                    target.symbol = symbol;
                    target.el.querySelector('.symbol-input').value = symbol;
                    loadPaneData(target);
                }
            });
        }
        saveState();
    }

    function setupPaneActions(pane) {
        pane.el.querySelector('.btn-fullscreen').addEventListener('click', () => togglePaneFullscreen(pane));
        pane.el.querySelector('.btn-screenshot').addEventListener('click', () => takePaneScreenshot(pane));
    }

    function togglePaneFullscreen(pane) {
        pane.el.classList.toggle('fullscreen');
        setTimeout(() => {
            const container = pane.el.querySelector('.chart-container');
            pane.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
        }, 50);
    }

    async function loadPaneData(pane) {
        const loading = pane.el.querySelector('.loading-overlay');
        const note = pane.el.querySelector('.pane-empty-note');
        if (!pane.metric) {
            note.textContent = 'Choose at least one value metric';
            loading.classList.remove('active');
            return;
        }
        loading.classList.add('active');
        note.textContent = '';
        try {
            const effective = getEffectiveMetrics();
            const metricsParam = effective.join(',');
            const data = await fetchJSON(`${API_BASE}/api/stock?symbol=${encodeURIComponent(pane.symbol)}&metrics=${encodeURIComponent(metricsParam)}`);
            pane.stockData = data.stock;
            pane.allMetrics = data.metrics || [];
            pane.metricData = pane.allMetrics[0] || null;
            pane.dataSource = data.fallbackProvider || 'screener';
            pane.isCached = !!data.cached;
            updatePaneHeader(pane);
            renderPaneSeries(pane);
        } catch (error) {
            pane.metricData = null;
            pane.dataSource = null;
            pane.isCached = false;
            pane.el.querySelector('.ohlc-legend').textContent = error.message || 'Unable to load metric';
            pane.el.querySelector('.chart-watermark').textContent = pane.metric || 'Error';
            note.textContent = 'Data unavailable for this symbol/metric';
        } finally {
            loading.classList.remove('active');
        }
    }

    function updatePaneHeader(pane) {
        const stock = pane.stockData || {};
        const ltp = pane.el.querySelector('.ticker-ltp');
        const change = pane.el.querySelector('.ticker-change');
        const legend = pane.el.querySelector('.ohlc-legend');
        const watermark = pane.el.querySelector('.chart-watermark');
        ltp.textContent = stock.price == null ? '--' : `₹${fmt(stock.price)}`;
        change.textContent = stock.changePct == null ? '--' : `${stock.changePct >= 0 ? '+' : ''}${fmt(stock.changePct)}%`;
        change.style.color = stock.changePct >= 0 ? 'var(--green)' : 'var(--red)';
        const src = (pane.dataSource === 'yahoo_finance') ? 'Yahoo fallback' : 'Screener';
        legend.textContent = pane.dataSource ? `Source: ${src}${pane.isCached ? ' (cached)' : ''}` : '';
        if (!pane.dataSource) {
            legend.style.color = 'var(--text-secondary)';
        } else if (pane.isCached) {
            legend.style.color = 'var(--text-secondary)';
        } else if (pane.dataSource === 'yahoo_finance') {
            legend.style.color = '#f59e0b';
        } else {
            legend.style.color = 'var(--green)';
        }
        watermark.textContent = `${stock.ticker || pane.symbol}`;
    }

    function ensureSeries(pane) {
        if (pane.metricSeries && pane.metricSeries.length) {
            pane.metricSeries.forEach((series) => {
                try { pane.chart.removeSeries(series); } catch (e) {}
            });
        }
        pane.metricSeries = [];
    }

    function renderPaneSeries(pane) {
        ensureSeries(pane);
        const metrics = pane.allMetrics || [];
        if (!metrics.length) {
            return;
        }

        const multiPointSeries = metrics.filter((metric) => Array.isArray(metric.points));
        let timeline = [];
        multiPointSeries.forEach((metric) => {
            (metric.points || []).forEach((point) => {
                if (point && point.date) timeline.push(point.date.slice(0, 10));
            });
        });
        timeline = Array.from(new Set(timeline)).sort();
        if (!timeline.length) {
            const now = new Date();
            for (let i = 7; i >= 0; i--) {
                const dt = new Date(now);
                dt.setMonth(now.getMonth() - (i * 3));
                timeline.push(dt.toISOString().slice(0, 10));
            }
        }
        pane._timeline = timeline;

        const palette = ['#2962ff', '#f23645', '#089981', '#f59e0b', '#8b5cf6', '#14b8a6', '#ef4444', '#22c55e'];
        pane._seriesData = [];
        metrics.forEach((metric, idx) => {
            let data = [];
            if (Array.isArray(metric.points) && metric.points.length) {
                const byDate = new Map((metric.points || []).filter((p) => p && p.date && p.value != null).map((p) => [p.date.slice(0, 10), p.value]));
                data = timeline.map((time) => ({ time, value: byDate.has(time) ? byDate.get(time) : null })).filter((p) => p.value != null);
            } else {
                const value = metric.value == null ? null : Number(metric.value);
                if (value != null && Number.isFinite(value)) {
                    data = timeline.map((time) => ({ time, value }));
                }
            }
            if (!data.length) return;

            const base = data[0].value || 1;
            const normalized = data.map((p) => ({ time: p.time, value: (p.value / base) * 100 }));
            const series = pane.chart.addLineSeries({ color: palette[idx % palette.length], lineWidth: 2, title: metric.label, priceLineVisible: false, lastValueVisible: false });
            series.setData(normalized);
            pane.metricSeries.push(series);
            pane._seriesData = pane._seriesData.concat(normalized);
        });

        if (pane._seriesData.length) pane.chart.timeScale().fitContent();
        applyVisibleRange(pane);
    }

    function applyVisibleRange(pane) {
        const timeline = pane._timeline || [];
        if (!timeline.length) return;

        const last = timeline[timeline.length - 1];

        if (globalViewRange >= 99999) {
            pane.chart.timeScale().setVisibleRange({ from: timeline[0], to: last });
            return;
        }

        // Date-based cutoff: go back globalViewRange days from the latest data point
        const lastDate = new Date(last + 'T00:00:00Z');
        const cutoffMs = lastDate.getTime() - globalViewRange * 24 * 60 * 60 * 1000;
        const cutoffStr = new Date(cutoffMs).toISOString().slice(0, 10);

        const visible = timeline.filter(t => t >= cutoffStr);
        const from = visible.length >= 1 ? visible[0] : timeline[0];

        if (from >= last) {
            pane.chart.timeScale().fitContent();
            return;
        }

        pane.chart.timeScale().setVisibleRange({ from, to: last });
    }

    function togglePageFullscreen() {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen();
        } else {
            document.exitFullscreen();
        }
    }

    function saveCanvas(canvas, filename) {
        const link = document.createElement('a');
        link.href = canvas.toDataURL('image/png');
        link.download = filename;
        link.click();
    }

    function composePaneCanvas(pane) {
        if (!pane.chart || typeof pane.chart.takeScreenshot !== 'function') return null;
        const base = pane.chart.takeScreenshot();
        if (!base) return null;
        const out = document.createElement('canvas');
        out.width = base.width;
        out.height = base.height;
        const ctx = out.getContext('2d');
        ctx.drawImage(base, 0, 0);
        ctx.font = '600 26px -apple-system, system-ui, sans-serif';
        ctx.fillStyle = 'rgba(209, 212, 220, 0.18)';
        ctx.fillText(`${pane.symbol}  ${pane.metric}`, 12, 30);
        return out;
    }

    function takePaneScreenshot(pane) {
        const canvas = composePaneCanvas(pane);
        if (!canvas) return;
        saveCanvas(canvas, `${pane.symbol}_${pane.metric.replace(/[^A-Za-z0-9]+/g, '_')}.png`);
    }

    function takePageScreenshot() {
        const grid = document.getElementById('chartGrid');
        const tiles = panes.map((pane) => composePaneCanvas(pane)).filter(Boolean);
        if (!tiles.length) return;
        const cols = (getComputedStyle(grid).gridTemplateColumns || '').split(' ').filter(Boolean).length || 1;
        const rows = Math.ceil(tiles.length / cols);
        const width = Math.max(...tiles.map((tile) => tile.width));
        const height = Math.max(...tiles.map((tile) => tile.height));
        const canvas = document.createElement('canvas');
        canvas.width = width * cols;
        canvas.height = height * rows;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#131722';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        tiles.forEach((tile, index) => {
            const col = index % cols;
            const row = Math.floor(index / cols);
            ctx.drawImage(tile, col * width, row * height);
        });
        saveCanvas(canvas, `screener_${new Date().toISOString().slice(0, 10)}.png`);
    }

    async function init() {
        setupPassiveFlyouts();
        setupControls();
        await loadMetricCatalog();
        chartCount = LAYOUT_BUCKETS.includes(chartCount) ? chartCount : 4;
        setChartCount(chartCount, true);
        applyTheme(chartTheme);
        saveState();
    }

    document.addEventListener('DOMContentLoaded', init);
})();