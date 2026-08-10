/**
 * sales-refund.js — Refund modal UI for admin_sales.html
 *
 * Handles:
 *   - Opening a refund modal with line-item selection
 *   - Quantity toggles per item (quantity refunded <= buyable qty)
 *   - Live preview of the refund total
 *   - Cash / Card method selection
 *   - Stripe Terminal integration for card refunds
 *   - Submitting to POST /admin/sales/refund/<sale_id>
 *   - Displaying refund history
 */

(function () {
  'use strict';

  // ---- state ----
  let currentSale = null;
  let currentSaleId = null;
  let stripeRefundClient = null;

  // ---- i18n ----
  const i18n = window.i18nRefund || {};
  const T = {
    refundTitle: i18n.refundTitle || 'Rückerstattung bearbeiten',
    refundBtn: i18n.refundBtn || 'Rückerstatten',
    refundSubmit: i18n.refundSubmit || 'Rückerstattung ausführen',
    refundProcessing: i18n.refundProcessing || 'Rückerstattung wird bearbeitet...',
    refundSuccess: i18n.refundSuccess || '✅ Rückerstattung erfolgreich bearbeitet.',
    refundError: i18n.refundError || '❌ Rückerstattung fehlgeschlagen.',
    refundCash: i18n.refundCash || 'Bar-Rückerstattung',
    refundCard: i18n.refundCard || 'Karten-Rückerstattung',
    refundReason: i18n.refundReason || 'Grund für Rückerstattung',
    refundReasonPh: i18n.refundReasonPh || 'z. B. Kunde zurückgegeben, beschädigt, falscher Artikel',
    refundHistory: i18n.refundHistory || 'Rückerstattungsverlauf',
    refundTotalRefunded: i18n.refundTotalRefunded || 'Bisher erstatteter Betrag',
    refundNoRefunds: i18n.refundNoRefunds || 'Keine Rückerstattungen für diesen Verkauf.',
    refundAmountRefunded: i18n.refundAmountRefunded || 'Erstatteter Betrag',
    refundSelectItems: i18n.refundSelectItems || 'Artikel zum Rückerstatten auswählen',
    refundQtyToRefund: i18n.refundQtyToRefund || 'Menge',
    refundMethod: i18n.refundMethod || 'Rückerstattungsmethode',
    refundItem: i18n.refundItem || 'Artikel',
    refundBarcode: i18n.refundBarcode || 'Barcode',
    refundStockRestored: i18n.refund_stock_restored || 'Der Lagerbestand wurde wiederhergestellt.',
    stripeNotConfigured: i18n.stripeNotConfigured || 'Kartenzahlungen sind nicht eingerichtet.',
    stripeReaderDisconnected: i18n.stripeReaderDisconnected || 'Kartenlesegerät nicht verbunden.',
  };

  // ---- helpers ----
  function esc(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '<')
      .replace(/>/g, '>')
      .replace(/"/g, '"')
      .replace(/'/g, '&#039;');
  }

  function fmt(amount) {
    return '\u20AC' + Number(amount).toFixed(2);
  }

  // ---- build modal DOM ----
  function buildRefundModal() {
    var existing = document.getElementById('refundModal');
    if (existing) existing.remove();

    var modal = document.createElement('div');
    modal.id = 'refundModal';
    modal.className = 'modal fade';
    modal.tabIndex = -1;
    modal.setAttribute('aria-labelledby', 'refundModalLabel');
    modal.setAttribute('aria-hidden', 'true');

    var cardDisabled = !window.stripeTerminal || !window.stripeTerminal.configured;
    var cardHelp = cardDisabled ? ' <small class="text-muted">(' + esc(T.stripeNotConfigured) + ')</small>' : '';

    modal.innerHTML =
      '<div class="modal-dialog modal-lg modal-dialog-scrollable">' +
        '<div class="modal-content">' +
          '<div class="modal-header">' +
            '<h5 class="modal-title" id="refundModalLabel">' + esc(T.refundTitle) + '</h5>' +
            '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
          '</div>' +
          '<div class="modal-body">' +
            '<div id="refundItemsContainer">' +
              '<p class="text-muted">' + esc(T.refundSelectItems) + '</p>' +
              '<table class="table table-bordered align-middle" id="refundItemsTable">' +
                '<thead class="table-light">' +
                  '<tr>' +
                    '<th style="width:40px"><input type="checkbox" id="refundSelectAll" checked></th>' +
                    '<th>' + esc(T.refundItem) + '</th>' +
                    '<th>' + esc(T.refundBarcode) + '</th>' +
                    '<th style="width:80px">' + esc(T.refundQtyToRefund) + '</th>' +
                    '<th style="width:100px">St\u00fcckpreis</th>' +
                    '<th style="width:100px">Betrag</th>' +
                  '</tr>' +
                '</thead>' +
                '<tbody id="refundItemsBody"></tbody>' +
              '</table>' +
            '</div>' +
            '<div class="mb-3">' +
              '<label class="form-label fw-bold">' + esc(T.refundMethod) + '</label>' +
              '<div class="d-flex gap-3">' +
                '<div class="form-check">' +
                  '<input class="form-check-input" type="radio" name="refundMethod" id="refundMethodCash" value="cash" checked>' +
                  '<label class="form-check-label" for="refundMethodCash">' + esc(T.refundCash) + '</label>' +
                '</div>' +
                '<div class="form-check">' +
                  '<input class="form-check-input" type="radio" name="refundMethod" id="refundMethodCard" value="card"' +
                    (cardDisabled ? ' disabled' : '') + '>' +
                  '<label class="form-check-label" for="refundMethodCard">' +
                    esc(T.refundCard) + (cardHelp) +
                  '</label>' +
                '</div>' +
              '</div>' +
            '</div>' +
            '<div class="mb-3">' +
              '<label for="refundReason" class="form-label fw-bold">' + esc(T.refundReason) + '</label>' +
              '<textarea class="form-control" id="refundReason" rows="2" placeholder="' + esc(T.refundReasonPh) + '"></textarea>' +
            '</div>' +
            '<div class="alert alert-info d-flex justify-content-between align-items-center">' +
              '<span>' + esc(T.refundTotalRefunded) + ':</span>' +
              '<span class="fw-bold fs-5" id="refundTotalAmount">' + fmt(0) + '</span>' +
            '</div>' +
          '</div>' +
          '<div class="modal-footer d-flex justify-content-between">' +
            '<div>' +
              '<button type="button" class="btn btn-outline-secondary btn-sm" id="refundShowHistoryBtn">' +
                esc(T.refundHistory) +
              '</button>' +
            '</div>' +
            '<div>' +
              '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">' +
                (window.i18nRefund?.commonCancel || 'Abbrechen') +
              '</button>' +
              '<button type="button" class="btn btn-warning ms-2" id="refundSubmitBtn">' +
                esc(T.refundSubmit) +
              '</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';

    // History panel (hidden by default, toggled by the history button)
    var historyPanel = document.createElement('div');
    historyPanel.id = 'refundHistoryPanel';
    historyPanel.style.display = 'none';
    historyPanel.className = 'card mb-3 p-3 border-secondary';
    historyPanel.innerHTML =
      '<h6>' + esc(T.refundHistory) + '</h6>' +
      '<div id="refundHistoryBody"><p class="text-muted"><small>' + esc(T.refundNoRefunds) + '</small></p></div>';

    modal.querySelector('.modal-body').insertBefore(historyPanel, modal.querySelector('.modal-body').firstChild);

    document.body.appendChild(modal);
  }

  // ---- populate items into the modal ----
  function populateRefundItems(sale) {
    var tbody = document.getElementById('refundItemsBody');
    if (!tbody) return;

    tbody.innerHTML = '';
    var items = sale.items || [];
    var hasItems = false;

    items.forEach(function (item) {
      var boughtQty = parseInt(item.quantity, 10) || 0;
      var alreadyRefunded = parseInt(item.refunded_qty, 10) || 0;
      var refundable = Math.max(0, boughtQty - alreadyRefunded);

      if (refundable <= 0) return; // fully refunded, skip

      hasItems = true;
      var price = parseFloat(item.sale_price) || 0;
      var row = document.createElement('tr');
      row.dataset.saleItemId = item.id;
      row.dataset.barcode = item.barcode || '';
      row.dataset.productName = item.product_name || '';
      row.dataset.unitPrice = price;
      row.dataset.maxQty = refundable;

      row.innerHTML =
        '<td><input type="checkbox" class="refund-item-checkbox" checked></td>' +
        '<td>' + esc(item.product_name) + '</td>' +
        '<td><code>' + esc(item.barcode) + '</code></td>' +
        '<td>' +
          '<input type="number" class="form-control form-control-sm refund-item-qty" ' +
            'value="' + refundable + '" min="1" max="' + refundable + '" style="width:70px">' +
        '</td>' +
        '<td>' + fmt(price) + '</td>' +
        '<td class="refund-item-line-total">' + fmt(price * refundable) + '</td>';

      tbody.appendChild(row);
    });

    if (!hasItems) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-3">' +
        'Alle Artikel dieses Verkaufs wurden bereits vollst\u00e4ndig zur\u00fcckerstattet.' +
        '</td></tr>';
    }

    // Show/hide the select-all hint
    var hint = document.getElementById('refundSelectItemsHint');
    if (hint) {
      hint.style.display = hasItems ? '' : 'none';
    }

    updateRefundTotal();
  }

  // ---- compute & display the refund total ----
  function updateRefundTotal() {
    var totalEl = document.getElementById('refundTotalAmount');
    if (!totalEl) return;

    var sum = 0;
    var rows = document.querySelectorAll('#refundItemsBody tr');
    rows.forEach(function (row) {
      var cb = row.querySelector('.refund-item-checkbox');
      if (!cb || !cb.checked) return;

      var qty = parseInt(row.querySelector('.refund-item-qty').value, 10) || 0;
      var price = parseFloat(row.dataset.unitPrice) || 0;
      var lineTotal = price * qty;
      sum += lineTotal;

      var lineTotalEl = row.querySelector('.refund-item-line-total');
      if (lineTotalEl) {
        lineTotalEl.textContent = fmt(lineTotal);
      }
    });

    totalEl.textContent = fmt(sum);
  }

  // ---- attach event listeners inside the modal ----
  function attachModalEvents(saleId) {
    // Select all / deselect all
    var selectAll = document.getElementById('refundSelectAll');
    if (selectAll) {
      selectAll.addEventListener('change', function () {
        var checked = selectAll.checked;
        document.querySelectorAll('.refund-item-checkbox').forEach(function (cb) {
          cb.checked = checked;
        });
        updateRefundTotal();
      });
    }

    // Individual checkboxes
    document.querySelectorAll('.refund-item-checkbox').forEach(function (cb) {
      cb.addEventListener('change', updateRefundTotal);
    });

    // Quantity changes
    document.querySelectorAll('.refund-item-qty').forEach(function (input) {
      input.addEventListener('input', updateRefundTotal);
      input.addEventListener('change', function () {
        var max = parseInt(this.max, 10);
        var val = parseInt(this.value, 10);
        if (isNaN(val) || val < 1) this.value = 1;
        if (val > max) this.value = max;
        updateRefundTotal();
      });
    });

    // History button
    var historyBtn = document.getElementById('refundShowHistoryBtn');
    if (historyBtn) {
      historyBtn.addEventListener('click', function () {
        toggleRefundHistory(saleId);
      });
    }

    // Submit button
    var submitBtn = document.getElementById('refundSubmitBtn');
    if (submitBtn) {
      submitBtn.addEventListener('click', function () {
        submitRefund(saleId);
      });
    }
  }

  // ---- toggle refund history panel ----
  function toggleRefundHistory(saleId) {
    var panel = document.getElementById('refundHistoryPanel');
    if (!panel) return;

    if (panel.style.display === 'none') {
      panel.style.display = 'block';
      loadRefundHistory(saleId);
    } else {
      panel.style.display = 'none';
    }
  }

  // ---- load refund history from server ----
  function loadRefundHistory(saleId) {
    var body = document.getElementById('refundHistoryBody');
    if (!body) return;

    body.innerHTML = '<p class="text-muted"><small>Lade...</small></p>';

    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/admin/sales/' + encodeURIComponent(saleId) + '/refunds', true);
    xhr.setRequestHeader('Accept', 'application/json');

    xhr.onload = function () {
      if (xhr.status !== 200) {
        body.innerHTML = '<p class="text-danger"><small>Fehler beim Laden.</small></p>';
        return;
      }
      try {
        var data = JSON.parse(xhr.responseText);
        renderRefundHistory(body, data);
      } catch (e) {
        body.innerHTML = '<p class="text-danger"><small>Ung\u00fcltige Antwort.</small></p>';
      }
    };

    xhr.onerror = function () {
      body.innerHTML = '<p class="text-danger"><small>Netzwerkfehler.</small></p>';
    };

    xhr.send();
  }

  // ---- render refund history rows ----
  function renderRefundHistory(container, data) {
    var refunds = data.refunds || [];
    var totalRefunded = parseFloat(data.total_refunded) || 0;

    if (refunds.length === 0) {
      container.innerHTML = '<p class="text-muted"><small>' + esc(T.refundNoRefunds) + '</small></p>';
      return;
    }

    var html =
      '<p class="mb-1"><strong>' + esc(T.refundTotalRefunded) + ': ' + fmt(totalRefunded) + '</strong></p>' +
      '<div style="max-height:200px;overflow-y:auto">' +
        '<table class="table table-sm table-striped mb-0">' +
          '<thead>' +
            '<tr>' +
              '<th>' + esc(T.refundItem) + '</th>' +
              '<th>Menge</th>' +
              '<th>' + esc(T.refundAmountRefunded) + '</th>' +
              '<th>Methode</th>' +
              '<th>Grund</th>' +
              '<th>Datum</th>' +
              '<th>Von</th>' +
            '</tr>' +
          '</thead>' +
          '<tbody>';

    refunds.forEach(function (r) {
      html +=
        '<tr>' +
          '<td>' + esc(r.product_name) + '</td>' +
          '<td>' + r.quantity + '</td>' +
          '<td>' + fmt(r.total_refund_amount) + '</td>' +
          '<td>' + esc(r.refund_method) + '</td>' +
          '<td><small>' + esc(r.reason || '-') + '</small></td>' +
          '<td><small>' + esc(r.refunded_at || '') + '</small></td>' +
          '<td><small>' + esc(r.refunded_by || '-') + '</small></td>' +
        '</tr>';
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;
  }

  // ---- submit refund to server ----
  function submitRefund(saleId) {
    var submitBtn = document.getElementById('refundSubmitBtn');
    if (!submitBtn) return;

    // Gather selected items
    var items = [];
    var rows = document.querySelectorAll('#refundItemsBody tr');
    rows.forEach(function (row) {
      var cb = row.querySelector('.refund-item-checkbox');
      if (!cb || !cb.checked) return;

      var saleItemId = row.dataset.saleItemId;
      var qty = parseInt(row.querySelector('.refund-item-qty').value, 10) || 1;

      items.push({
        sale_item_id: parseInt(saleItemId, 10),
        qty: qty,
      });
    });

    if (items.length === 0) {
      alert('Bitte w\u00e4hlen Sie mindestens einen Artikel aus.');
      return;
    }

    var refundMethod = document.querySelector('input[name="refundMethod"]:checked');
    var method = refundMethod ? refundMethod.value : 'cash';
    var reason = document.getElementById('refundReason') ? document.getElementById('refundReason').value.trim() : '';

    // For card refunds: check Stripe Terminal is available
    var stripePaymentIntentId = null;
    if (method === 'card') {
      if (!window.stripeTerminal || !window.stripeTerminal.configured) {
        alert(T.stripeNotConfigured);
        return;
      }
      // Try to get a connected reader
      if (window.StripeTerminal && stripeRefundClient) {
        try {
          // We need to process card refund through server side since the
          // original PaymentIntent must be refunded via the Stripe API.
          // The JS will ask server to process the Stripe refund.
          alert('Karten-R\u00fcckerstattung wird \u00fcber den Server verarbeitet.');
        } catch (e) {
          alert(T.stripeReaderDisconnected);
          return;
        }
      }
    }

    // Disable button, show processing
    var originalText = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = T.refundProcessing;

    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/admin/sales/refund/' + encodeURIComponent(saleId), true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.setRequestHeader('Accept', 'application/json');

    xhr.onload = function () {
      submitBtn.disabled = false;
      submitBtn.textContent = originalText;

      if (xhr.status !== 200) {
        try {
          var err = JSON.parse(xhr.responseText);
          alert(err.message || T.refundError);
        } catch (e) {
          alert(T.refundError);
        }
        return;
      }

      try {
        var data = JSON.parse(xhr.responseText);
        if (data.success) {
          alert(data.message || T.refundSuccess);
          // Close modal
          var modalEl = document.getElementById('refundModal');
          if (modalEl && window.bootstrap && window.bootstrap.Modal) {
            var bsModal = window.bootstrap.Modal.getInstance(modalEl);
            if (bsModal) bsModal.hide();
            else modalEl.querySelector('.btn-close').click();
          } else if (modalEl) {
            modalEl.querySelector('.btn-close').click();
          }
          // Reload page to reflect updated refund data
          location.reload();
        } else {
          alert(data.message || T.refundError);
        }
      } catch (e) {
        alert(T.refundError);
      }
    };

    xhr.onerror = function () {
      submitBtn.disabled = false;
      submitBtn.textContent = originalText;
      alert('Netzwerkfehler beim Senden der R\u00fcckerstattung.');
    };

    xhr.send(JSON.stringify({
      items: items,
      refund_method: method,
      reason: reason,
      stripe_payment_intent_id: stripePaymentIntentId,
    }));
  }

  // ---- openRefundModal ----
  function openRefundModal(sale) {
    if (!sale || !sale.order_id) {
      console.error('openRefundModal: invalid sale object', sale);
      return;
    }

    currentSale = sale;
    currentSaleId = sale.order_id;

    buildRefundModal();
    populateRefundItems(sale);
    attachModalEvents(sale.order_id);

    // Show modal
    var modalEl = document.getElementById('refundModal');
    if (window.bootstrap && window.bootstrap.Modal) {
      var bsModal = new window.bootstrap.Modal(modalEl);
      bsModal.show();
    } else {
      modalEl.classList.add('show');
      modalEl.style.display = 'block';
      document.body.classList.add('modal-open');
      // Add backdrop
      var backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop fade show';
      backdrop.id = 'refundModalBackdrop';
      document.body.appendChild(backdrop);
    }
  }

  // ---- expose globally ----
  window.openRefundModal = openRefundModal;
  window.refundHelpers = {
    getTotalRefunded: function () {
      var el = document.getElementById('refundTotalAmount');
      return el ? el.textContent : '\u20AC0.00';
    },
  };

})();

