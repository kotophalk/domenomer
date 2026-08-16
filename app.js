// ============================================
// Bulk DR Checker — App Logic
// ============================================

(function () {
  'use strict';

  // --- DOM Elements ---
  const domainsInput = document.getElementById('domains-input');
  const fileUpload = document.getElementById('file-upload');
  const domainCountEl = document.getElementById('domain-count');
  const checkBtn = document.getElementById('check-btn');

  const progressSection = document.getElementById('progress-section');
  const progressLabel = document.getElementById('progress-label');
  const progressCount = document.getElementById('progress-count');
  const progressBar = document.getElementById('progress-bar');
  const stopBtn = document.getElementById('stop-btn');

  const resultsSection = document.getElementById('results-section');
  const resultsBody = document.getElementById('results-body');
  const resultsSummary = document.getElementById('results-summary');
  const exportBtn = document.getElementById('export-btn');

  const filterMin = document.getElementById('filter-min');
  const filterMax = document.getElementById('filter-max');

  // --- Лимиты API ---
  // Ahrefs API: 60 запросов в минуту, при превышении — 429 (плюс редкие 429 из-за троттлинга).
  const RATE_LIMIT_PER_MIN = 60;
  const MIN_INTERVAL_MS = Math.ceil(60000 / RATE_LIMIT_PER_MIN); // пауза между стартами запросов
  const MAX_CONCURRENCY = 4;   // одновременных запросов в полёте
  const MAX_RETRIES = 3;       // повторов одного домена после 429
  const BACKOFF_BASE_MS = 2000; // если Ahrefs не прислал Retry-After: 2с, 4с, 8с...
  const BACKOFF_MAX_MS = 60000;

  // --- State ---
  const API_URL = '/api/dr';
  let results = [];
  let sortColumn = null;
  let sortDirection = 'asc';
  let isRunning = false;
  let pausedUntil = 0;   // глобальная пауза очереди после 429 (timestamp)
  let pauseTimer = null; // таймер обратного отсчёта паузы в UI
  let abortController = null; // «Стоп»: обрывает запросы в полёте и сон воркеров

  // --- Helpers ---

  // Сон, который можно прервать через AbortSignal (для кнопки «Стоп»)
  function sleep(ms, signal) {
    return new Promise(resolve => {
      if (signal?.aborted) { resolve(); return; }
      const timer = setTimeout(done, ms);
      function done() {
        clearTimeout(timer);
        signal?.removeEventListener('abort', done);
        resolve();
      }
      signal?.addEventListener('abort', done, { once: true });
    });
  }

  function parseRetryAfter(value) {
    if (!value) return null;
    const seconds = Number(value);
    if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
    const date = Date.parse(value);
    return Number.isNaN(date) ? null : Math.max(0, date - Date.now());
  }

  function parseDomains(text) {
    return text
      .split(/[\n\r,;]+/)
      .map(d => d.trim().toLowerCase())
      .map(d => d.replace(/^https?:\/\//, '').replace(/\/.*$/, '').replace(/^www\./, ''))
      .filter(d => d.length > 0 && d.includes('.'));
  }

  function uniqueDomains(domains) {
    return [...new Set(domains)];
  }

  function getDrClass(dr) {
    if (dr === null || dr === undefined) return '';
    if (dr >= 51) return 'dr-high';
    if (dr >= 21) return 'dr-mid';
    return 'dr-low';
  }



  function updateDomainCount() {
    const domains = uniqueDomains(parseDomains(domainsInput.value));
    const count = domains.length;
    if (count > 0) {
      domainCountEl.textContent = `${count} ${pluralize(count, 'домен', 'домена', 'доменов')}`;
    } else {
      domainCountEl.textContent = '';
    }
  }

  function pluralize(n, one, few, many) {
    const mod10 = n % 10;
    const mod100 = n % 100;
    if (mod100 >= 11 && mod100 <= 19) return many;
    if (mod10 === 1) return one;
    if (mod10 >= 2 && mod10 <= 4) return few;
    return many;
  }

  // --- API ---

  async function fetchDR(domain, signal) {
    const url = `${API_URL}?target=${encodeURIComponent(domain)}`;
    const response = await fetch(url, { signal });

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      let detail = text;
      try {
        const parsed = JSON.parse(text);
        if (typeof parsed.error === 'string') detail = parsed.error;
      } catch (_) { /* не JSON — оставляем как есть */ }
      const err = new Error(`HTTP ${response.status}${detail ? ': ' + detail.substring(0, 100) : ''}`);
      err.status = response.status;
      err.retryAfterMs = parseRetryAfter(response.headers.get('Retry-After'));
      throw err;
    }

    const data = await response.json();
    return {
      dr: data.domain_rating?.domain_rating ?? null,
    };
  }

  // --- Check flow ---

  async function runCheck() {
    const domains = uniqueDomains(parseDomains(domainsInput.value));

    if (domains.length === 0) {
      domainsInput.focus();
      return;
    }

    isRunning = true;
    checkBtn.disabled = true;
    stopBtn.disabled = false;
    stopBtn.classList.remove('hidden');
    results = [];
    pausedUntil = 0;
    clearInterval(pauseTimer);
    pauseTimer = null;
    abortController = new AbortController();
    const signal = abortController.signal;

    // Init results array
    domains.forEach((domain, i) => {
      results.push({
        index: i + 1,
        domain: domain,
        dr: null,
        status: 'pending',
        error: null,
        retries: 0,
      });
    });

    // Show progress
    progressSection.classList.remove('hidden');
    resultsSection.classList.remove('hidden');
    updateProgress(0, domains.length);
    renderTable();

    // Очередь: не более MAX_CONCURRENCY в полёте, старты не чаще MIN_INTERVAL_MS,
    // при 429 — общая пауза для всех воркеров и повтор домена.
    let completed = 0;
    let nextIndex = 0;
    let lastStartAt = 0;

    async function waitForSlot() {
      while (!signal.aborted) {
        const now = Date.now();
        const wait = Math.max(lastStartAt + MIN_INTERVAL_MS, pausedUntil) - now;
        if (wait <= 0) break;
        await sleep(wait, signal);
      }
      lastStartAt = Date.now();
    }

    // При остановке возвращается, оставив r.status === 'pending'
    async function checkOne(r) {
      for (let attempt = 0; ; attempt++) {
        await waitForSlot();
        if (signal.aborted) return;
        try {
          const data = await fetchDR(r.domain, signal);
          r.dr = data.dr;
          r.status = 'ok';
          return;
        } catch (err) {
          if (signal.aborted) return;
          if (err.status === 429 && attempt < MAX_RETRIES) {
            const backoff = err.retryAfterMs != null
              ? Math.max(err.retryAfterMs, 1000)
              : Math.min(BACKOFF_BASE_MS * 2 ** attempt, BACKOFF_MAX_MS);
            pauseAll(backoff);
            r.retries = attempt + 1;
            renderTable();
            continue;
          }
          r.status = 'error';
          r.error = (err.message || 'Неизвестная ошибка') + (attempt > 0 ? ` (повторов: ${attempt})` : '');
          return;
        }
      }
    }

    async function worker() {
      while (nextIndex < domains.length && !signal.aborted) {
        const r = results[nextIndex++];
        await checkOne(r);
        if (r.status === 'pending') break; // прервано кнопкой «Стоп»
        completed++;
        updateProgress(completed, domains.length);
        renderTable();
      }
    }

    const workerCount = Math.min(MAX_CONCURRENCY, domains.length);
    await Promise.all(Array.from({ length: workerCount }, worker));

    // Done (или остановлено)
    const stopped = signal.aborted;
    results.forEach(r => {
      if (r.status === 'pending') r.status = 'skipped';
    });
    clearInterval(pauseTimer);
    pauseTimer = null;
    pausedUntil = 0;
    abortController = null;
    progressLabel.textContent = stopped ? 'Остановлено' : 'Готово!';
    isRunning = false;
    checkBtn.disabled = false;
    stopBtn.classList.add('hidden');
    renderTable();
    renderSummary();
  }

  function stopCheck() {
    if (!isRunning || !abortController) return;
    stopBtn.disabled = true;
    progressLabel.textContent = 'Останавливаю...';
    abortController.abort();
  }

  function pauseAll(ms) {
    pausedUntil = Math.max(pausedUntil, Date.now() + ms);
    if (pauseTimer) return; // отсчёт уже идёт, он подхватит новый pausedUntil

    const tick = () => {
      const left = Math.ceil((pausedUntil - Date.now()) / 1000);
      if (left > 0 && isRunning) {
        progressLabel.textContent = `Лимит API (429) — пауза ${left} с`;
      } else {
        clearInterval(pauseTimer);
        pauseTimer = null;
        if (isRunning) progressLabel.textContent = 'Проверка...';
      }
    };
    tick();
    pauseTimer = setInterval(tick, 250);
  }

  function updateProgress(done, total) {
    const pct = total > 0 ? (done / total) * 100 : 0;
    progressBar.style.width = `${pct}%`;
    progressCount.textContent = `${done} / ${total}`;
    if (done < total && Date.now() >= pausedUntil && !abortController?.signal.aborted) {
      progressLabel.textContent = 'Проверка...';
    }
  }

  // --- Rendering ---

  function getFilteredResults() {
    const minDr = filterMin.value !== '' ? parseFloat(filterMin.value) : null;
    const maxDr = filterMax.value !== '' ? parseFloat(filterMax.value) : null;

    return results.filter(r => {
      if (r.dr === null) return true; // always show errors/pending
      if (minDr !== null && r.dr < minDr) return false;
      if (maxDr !== null && r.dr > maxDr) return false;
      return true;
    });
  }

  function getSortedResults(data) {
    if (!sortColumn) return data;

    const sorted = [...data];
    const dir = sortDirection === 'asc' ? 1 : -1;

    sorted.sort((a, b) => {
      let va, vb;

      switch (sortColumn) {
        case 'index':
          va = a.index; vb = b.index;
          break;
        case 'domain':
          va = a.domain; vb = b.domain;
          return dir * va.localeCompare(vb);
        case 'dr':
          va = a.dr ?? -1; vb = b.dr ?? -1;
          break;
        case 'status':
          va = a.status; vb = b.status;
          return dir * va.localeCompare(vb);
        default:
          return 0;
      }

      return dir * (va - vb);
    });

    return sorted;
  }

  function renderTable() {
    const filtered = getFilteredResults();
    const sorted = getSortedResults(filtered);

    resultsBody.innerHTML = '';

    sorted.forEach((r, i) => {
      const tr = document.createElement('tr');
      tr.style.animationDelay = `${Math.min(i * 0.02, 0.5)}s`;

      // #
      const tdNum = document.createElement('td');
      tdNum.className = 'col-num';
      tdNum.textContent = r.index;

      // Domain
      const tdDomain = document.createElement('td');
      tdDomain.innerHTML = `<span class="domain-name">${escapeHtml(r.domain)}</span>`;

      // DR
      const tdDr = document.createElement('td');
      if (r.status === 'pending') {
        tdDr.innerHTML = '<span class="spinner"></span>';
      } else if (r.dr !== null) {
        const drClass = getDrClass(r.dr);
        tdDr.innerHTML = `<span class="dr-badge ${drClass}">${r.dr}</span>`;
      } else {
        tdDr.textContent = '—';
      }



      // Status
      const tdStatus = document.createElement('td');
      if (r.status === 'ok') {
        tdStatus.innerHTML = '<span class="status-ok">✓ OK</span>';
      } else if (r.status === 'error') {
        tdStatus.innerHTML = `<span class="status-error" title="${escapeHtml(r.error || '')}">✗ Ошибка</span>`;
      } else if (r.status === 'skipped') {
        tdStatus.innerHTML = '<span class="status-skipped" title="Проверка остановлена">— Не проверен</span>';
      } else if (r.retries > 0) {
        tdStatus.innerHTML = `<span class="status-pending" title="Повтор после 429">⏳ повтор ${r.retries}</span>`;
      } else {
        tdStatus.innerHTML = '<span class="status-pending">⏳</span>';
      }

      tr.append(tdNum, tdDomain, tdDr, tdStatus);
      resultsBody.appendChild(tr);
    });

    // Update sort indicators
    document.querySelectorAll('#results-table thead th').forEach(th => {
      th.classList.remove('sorted-asc', 'sorted-desc');
      if (th.dataset.sort === sortColumn) {
        th.classList.add(sortDirection === 'asc' ? 'sorted-asc' : 'sorted-desc');
      }
    });
  }

  function renderSummary() {
    const total = results.length;
    const ok = results.filter(r => r.status === 'ok').length;
    const errors = results.filter(r => r.status === 'error').length;
    const skipped = results.filter(r => r.status === 'skipped').length;
    const drValues = results.filter(r => r.dr !== null).map(r => r.dr);
    const avgDr = drValues.length > 0 ? (drValues.reduce((a, b) => a + b, 0) / drValues.length).toFixed(1) : '—';
    const maxDr = drValues.length > 0 ? Math.max(...drValues) : '—';
    const minDr = drValues.length > 0 ? Math.min(...drValues) : '—';

    resultsSummary.innerHTML = `
      <span class="stat">Всего: <span class="stat-value">${total}</span></span>
      <span class="stat">Успешно: <span class="stat-value">${ok}</span></span>
      <span class="stat">Ошибок: <span class="stat-value">${errors}</span></span>
      ${skipped > 0 ? `<span class="stat">Не проверено: <span class="stat-value">${skipped}</span></span>` : ''}
      <span class="stat">Средний DR: <span class="stat-value">${avgDr}</span></span>
      <span class="stat">Мин / Макс: <span class="stat-value">${minDr} / ${maxDr}</span></span>
    `;
  }

  function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // --- Sorting ---

  function handleSort(column) {
    if (sortColumn === column) {
      sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
      sortColumn = column;
      sortDirection = 'asc';
    }
    renderTable();
  }

  // --- Export CSV ---

  function exportCsv() {
    const filtered = getFilteredResults();
    const sorted = getSortedResults(filtered);

    const headers = ['#', 'Domain', 'DR', 'Status'];
    const rows = sorted.map(r => [
      r.index,
      r.domain,
      r.dr !== null ? r.dr : '',
      r.status === 'ok' ? 'OK'
        : r.status === 'error' ? `Error: ${r.error || ''}`
        : r.status === 'skipped' ? 'Skipped'
        : 'Pending',
    ]);

    const csv = [headers, ...rows]
      .map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
      .join('\n');

    const bom = '\uFEFF'; // UTF-8 BOM for Excel
    const blob = new Blob([bom + csv], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);

    const a = document.createElement('a');
    a.href = url;
    a.download = `dr-check-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();

    URL.revokeObjectURL(url);
  }

  // --- File Upload ---

  function handleFileUpload(e) {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = function (evt) {
      const text = evt.target.result;
      const existing = domainsInput.value.trim();
      domainsInput.value = existing ? existing + '\n' + text : text;
      updateDomainCount();
    };
    reader.readAsText(file);

    // Reset input so the same file can be selected again
    fileUpload.value = '';
  }

  // --- Event Listeners ---

  domainsInput.addEventListener('input', updateDomainCount);

  fileUpload.addEventListener('change', handleFileUpload);

  checkBtn.addEventListener('click', () => {
    if (!isRunning) runCheck();
  });

  stopBtn.addEventListener('click', stopCheck);

  // Esc — остановить проверку
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isRunning) stopCheck();
  });

  // Sort headers
  document.querySelectorAll('#results-table thead th[data-sort]').forEach(th => {
    th.addEventListener('click', () => handleSort(th.dataset.sort));
  });

  // Filters
  filterMin.addEventListener('input', () => {
    renderTable();
    renderSummary();
  });

  filterMax.addEventListener('input', () => {
    renderTable();
    renderSummary();
  });

  exportBtn.addEventListener('click', exportCsv);

  // Keyboard shortcut: Ctrl+Enter to check
  domainsInput.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      if (!isRunning) runCheck();
    }
  });

})();
