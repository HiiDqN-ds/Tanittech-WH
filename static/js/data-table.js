/**
 * Shared Data Table Enhancer
 * ---------------------------------------------------------------
 * Progressively enhances any <table class="data-table"> with:
 *   - client-side column sorting (click a header)
 *   - an instant search/filter box
 *   - pagination
 *   - sticky headers (via CSS, see data-table.css)
 *   - a responsive stacked-card layout on small screens
 *   - consistent row hover styling
 *
 * Zero markup required beyond class="data-table" on the <table>.
 * Purely a presentation layer: it only toggles row visibility and
 * re-orders <tr> elements inside the existing <tbody> — it never
 * touches form actions, hrefs, or any server-rendered data, so it
 * cannot affect business logic.
 *
 * Optional per-table opt-ins (all via data-* attributes on the
 * <table> itself):
 *   data-page-size="15"            rows per page (default 10)
 *   data-search-placeholder="..."  placeholder text for the search box
 *   data-empty-text="..."          text shown when a filter matches nothing
 * Optional per-column opt-out:
 *   <th data-no-sort>...</th>      disables sorting for that column
 *
 * For tables whose rows are injected dynamically after page load
 * (e.g. a modal populated via fetch()), call:
 *   window.DataTableEnhancer.refresh(tableEl)
 * after the new rows are in the DOM, and the engine will pick them
 * up (re-apply labels, re-run the current sort/filter/page).
 */
(function () {
  'use strict';

  var DEFAULT_PAGE_SIZE = 10;
  var COMPACT_THRESHOLD = 6; // hide the search box for very small tables

  function init(table) {
    if (!table || table.dataset.dtInit) return;
    table.dataset.dtInit = '1';

    var thead = table.querySelector('thead');
    var tbody = table.querySelector('tbody');
    if (!thead || !tbody) return;

    var headerCells = Array.prototype.slice.call(thead.querySelectorAll('th'));
    var pageSize = parseInt(table.dataset.pageSize || DEFAULT_PAGE_SIZE, 10) || DEFAULT_PAGE_SIZE;

    // ---- wrap the table: toolbar + scroll container + pager ----
    var wrapper = document.createElement('div');
    wrapper.className = 'dt-wrapper';
    table.parentNode.insertBefore(wrapper, table);

    var toolbar = document.createElement('div');
    toolbar.className = 'dt-toolbar';

    var searchWrap = document.createElement('div');
    searchWrap.className = 'dt-search';
    var searchIcon = document.createElement('i');
    searchIcon.className = 'bi bi-search';
    searchIcon.setAttribute('aria-hidden', 'true');
    var searchInput = document.createElement('input');
    searchInput.type = 'text';
    searchInput.className = 'dt-search-input form-control';
    searchInput.placeholder = table.dataset.searchPlaceholder || 'Filter…';
    searchInput.setAttribute('aria-label', searchInput.placeholder);
    searchWrap.appendChild(searchIcon);
    searchWrap.appendChild(searchInput);

    var info = document.createElement('div');
    info.className = 'dt-info';

    toolbar.appendChild(searchWrap);
    toolbar.appendChild(info);
    wrapper.appendChild(toolbar);

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'dt-scroll';
    wrapper.appendChild(scrollWrap);
    scrollWrap.appendChild(table);
    table.classList.add('dt-table');

    var pager = document.createElement('div');
    pager.className = 'dt-pager';
    wrapper.appendChild(pager);

    // Keep references for refresh() / state
    table._dt = {
      wrapper: wrapper, toolbar: toolbar, searchInput: searchInput,
      info: info, pager: pager, headerCells: headerCells,
      pageSize: pageSize, page: 1, filterText: '', sortIdx: null, sortDir: 1
    };

    tagRows(table);
    setupSorting(table);

    searchInput.addEventListener('input', function () {
      table._dt.filterText = searchInput.value.trim().toLowerCase();
      table._dt.page = 1;
      render(table);
    });

    render(table);
  }

  // Assign data-label (for the responsive card view) and a shared
  // class to every body row so the engine can find/manage them,
  // without touching any existing classes, attributes, or content.
  function tagRows(table) {
    var headerCells = table._dt ? table._dt.headerCells
      : Array.prototype.slice.call(table.querySelectorAll('thead th'));
    var tbody = table.querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.forEach(function (row) {
      if (row.classList.contains('dt-empty-row')) return;
      row.classList.add('dt-row');
      var cells = Array.prototype.slice.call(row.children);
      cells.forEach(function (cell, i) {
        if (!cell.hasAttribute('data-label') && headerCells[i]) {
          cell.setAttribute('data-label', headerCells[i].textContent.trim());
        }
      });
    });

    // Toggle compact mode (hides the search box) for tiny tables.
    var count = tbody.querySelectorAll('tr.dt-row').length;
    if (table._dt) {
      table._dt.wrapper.classList.toggle('dt-compact', count <= COMPACT_THRESHOLD);
    }
  }

  function setupSorting(table) {
    var state = table._dt;
    state.headerCells.forEach(function (th, idx) {
      if (th.hasAttribute('data-no-sort')) return;
      th.classList.add('dt-sortable');
      th.setAttribute('tabindex', '0');
      th.setAttribute('role', 'button');
      var icon = document.createElement('span');
      icon.className = 'dt-sort-icon';
      icon.setAttribute('aria-hidden', 'true');
      th.appendChild(icon);

      function activate() { sortBy(table, idx); }
      th.addEventListener('click', activate);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
      });
    });
  }

  function cellValue(cell) {
    if (!cell) return '';
    if (cell.hasAttribute('data-sort-value')) return cell.getAttribute('data-sort-value');
    return (cell.textContent || '').trim();
  }

  function sortBy(table, idx) {
    var state = table._dt;
    var tbody = table.querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.dt-row'));
    if (!rows.length) return;

    state.sortDir = (state.sortIdx === idx) ? -state.sortDir : 1;
    state.sortIdx = idx;

    state.headerCells.forEach(function (th) {
      th.classList.remove('dt-sort-asc', 'dt-sort-desc');
    });
    state.headerCells[idx].classList.add(state.sortDir === 1 ? 'dt-sort-asc' : 'dt-sort-desc');

    rows.sort(function (a, b) {
      var av = cellValue(a.children[idx]);
      var bv = cellValue(b.children[idx]);
      var an = parseFloat(av.replace(/[^0-9.\-]/g, ''));
      var bn = parseFloat(bv.replace(/[^0-9.\-]/g, ''));
      var looksNumeric = av !== '' && bv !== '' && !isNaN(an) && !isNaN(bn);
      var cmp = looksNumeric ? (an - bn) : av.localeCompare(bv, undefined, { numeric: true, sensitivity: 'base' });
      return cmp * state.sortDir;
    });

    rows.forEach(function (r) { tbody.appendChild(r); });
    state.page = 1;
    render(table);
  }

  function getFilteredRows(table) {
    var state = table._dt;
    var tbody = table.querySelector('tbody');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.dt-row'));
    if (!state.filterText) return rows;
    return rows.filter(function (r) {
      return r.textContent.toLowerCase().indexOf(state.filterText) !== -1;
    });
  }

  function render(table) {
    var state = table._dt;
    var tbody = table.querySelector('tbody');
    var allRows = Array.prototype.slice.call(tbody.querySelectorAll('tr.dt-row'));
    var filtered = getFilteredRows(table);

    allRows.forEach(function (r) { r.style.display = 'none'; });

    var totalPages = Math.max(1, Math.ceil(filtered.length / state.pageSize));
    if (state.page > totalPages) state.page = totalPages;
    var start = (state.page - 1) * state.pageSize;
    var pageRows = filtered.slice(start, start + state.pageSize);
    pageRows.forEach(function (r) { r.style.display = ''; });

    var emptyRow = tbody.querySelector('tr.dt-empty-row');
    if (!filtered.length) {
      if (!emptyRow) {
        emptyRow = document.createElement('tr');
        emptyRow.className = 'dt-empty-row';
        var td = document.createElement('td');
        td.colSpan = state.headerCells.length || 1;
        td.textContent = table.dataset.emptyText || 'No matching results';
        emptyRow.appendChild(td);
        tbody.appendChild(emptyRow);
      }
      emptyRow.style.display = '';
    } else if (emptyRow) {
      emptyRow.style.display = 'none';
    }

    state.info.textContent = filtered.length
      ? (start + 1) + '\u2013' + Math.min(start + pageRows.length, filtered.length) + ' / ' + filtered.length
      : '';

    renderPager(table, totalPages);
  }

  function renderPager(table, totalPages) {
    var state = table._dt;
    var pager = state.pager;
    pager.innerHTML = '';
    if (totalPages <= 1) return;

    function mkBtn(label, page, disabled, active, ariaLabel) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'dt-pager-btn' + (active ? ' active' : '');
      b.textContent = label;
      b.disabled = !!disabled;
      if (ariaLabel) b.setAttribute('aria-label', ariaLabel);
      b.addEventListener('click', function () {
        state.page = page;
        render(table);
        state.wrapper.scrollIntoView({ block: 'nearest' });
      });
      return b;
    }

    pager.appendChild(mkBtn('\u2039', state.page - 1, state.page <= 1, false, 'Previous page'));

    var maxButtons = 5;
    var startP = Math.max(1, state.page - 2);
    var endP = Math.min(totalPages, startP + maxButtons - 1);
    startP = Math.max(1, endP - maxButtons + 1);
    for (var p = startP; p <= endP; p++) {
      pager.appendChild(mkBtn(String(p), p, false, p === state.page));
    }

    pager.appendChild(mkBtn('\u203a', state.page + 1, state.page >= totalPages, false, 'Next page'));
  }

  function refresh(table) {
    if (!table) return;
    if (!table.dataset.dtInit) { init(table); return; }
    tagRows(table);
    render(table);
  }

  function initAll(root) {
    var scope = root || document;
    var tables = scope.querySelectorAll ? scope.querySelectorAll('table.data-table') : [];
    Array.prototype.forEach.call(tables, init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { initAll(); });
  } else {
    initAll();
  }

  window.DataTableEnhancer = { init: init, initAll: initAll, refresh: refresh };
})();
