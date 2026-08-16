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

  const resultsSection = document.getElementById('results-section');
  const resultsBody = document.getElementById('results-body');
  const resultsSummary = document.getElementById('results-summary');
  const exportBtn = document.getElementById('export-btn');

  const filterMin = document.getElementById('filter-min');
  const filterMax = document.getElementById('filter-max');

  // --- State ---
  const API_URL = '/api/dr';
  let results = [];
  let sortColumn = null;
  let sortDirection = 'asc';
  let isRunning = false;

  // --- Helpers ---

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

  async function fetchDR(domain) {
    const url = `${API_URL}?target=${encodeURIComponent(domain)}`;
    const response = await fetch(url);

    if (!response.ok) {
      const text = await response.text().catch(() => '');
      throw new Error(`HTTP ${response.status}${text ? ': ' + text.substring(0, 100) : ''}`);
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
    results = [];

    // Init results array
    domains.forEach((domain, i) => {
      results.push({
        index: i + 1,
        domain: domain,
        dr: null,
        status: 'pending',
        error: null,
      });
    });

    // Show progress
    progressSection.classList.remove('hidden');
    resultsSection.classList.remove('hidden');
    updateProgress(0, domains.length);
    renderTable();

    // Fire all requests in parallel
    let completed = 0;

    const promises = domains.map((domain, i) => {
      return fetchDR(domain)
        .then(data => {
          results[i].dr = data.dr;
          results[i].status = 'ok';
        })
        .catch(err => {
          results[i].status = 'error';
          results[i].error = err.message || 'Неизвестная ошибка';
        })
        .finally(() => {
          completed++;
          updateProgress(completed, domains.length);
          renderTable();
        });
    });

    await Promise.allSettled(promises);

    // Done
    progressLabel.textContent = 'Готово!';
    isRunning = false;
    checkBtn.disabled = false;
    renderSummary();
  }

  function updateProgress(done, total) {
    const pct = total > 0 ? (done / total) * 100 : 0;
    progressBar.style.width = `${pct}%`;
    progressCount.textContent = `${done} / ${total}`;
    if (done < total) {
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
    const drValues = results.filter(r => r.dr !== null).map(r => r.dr);
    const avgDr = drValues.length > 0 ? (drValues.reduce((a, b) => a + b, 0) / drValues.length).toFixed(1) : '—';
    const maxDr = drValues.length > 0 ? Math.max(...drValues) : '—';
    const minDr = drValues.length > 0 ? Math.min(...drValues) : '—';

    resultsSummary.innerHTML = `
      <span class="stat">Всего: <span class="stat-value">${total}</span></span>
      <span class="stat">Успешно: <span class="stat-value">${ok}</span></span>
      <span class="stat">Ошибок: <span class="stat-value">${errors}</span></span>
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
      r.status === 'ok' ? 'OK' : r.status === 'error' ? `Error: ${r.error || ''}` : 'Pending',
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
