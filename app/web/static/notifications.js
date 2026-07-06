/**
 * Multi-row notification stack - bottom-right corner.
 *
 * API (exposed on window):
 *   mrNotify(msg)  → { done(msg), error(msg), update(msg) }
 *
 * HTMX integration: elements with data-mr-action-label="…" automatically
 * get a pending notification on request start and resolve on completion.
 */
(function () {
  function mrNotify(initialMsg) {
    var container = document.getElementById('mr-toasts');
    if (!container) return { done: function () {}, error: function () {}, update: function () {} };

    var item = document.createElement('div');
    item.className = 'mr-toast-item mr-toast-pending';
    item.innerHTML =
      '<span class="mr-toast-icon"><span class="mr-spin">↻</span></span>' +
      '<span class="mr-toast-msg"></span>';
    item.querySelector('.mr-toast-msg').textContent = initialMsg;
    container.prepend(item);

    var _resolved = false;

    function _resolve(msg, state, delayMs) {
      if (_resolved) return;
      _resolved = true;
      item.className = 'mr-toast-item mr-toast-' + state;
      var icon = item.querySelector('.mr-toast-icon');
      if (icon) icon.innerHTML = state === 'success' ? '✓' : '✗';
      var msgEl = item.querySelector('.mr-toast-msg');
      if (msgEl) msgEl.textContent = msg;
      setTimeout(function () {
        item.classList.add('mr-toast-out');
        setTimeout(function () {
          if (item.parentNode) item.parentNode.removeChild(item);
        }, 350);
      }, delayMs);
    }

    function _update(msg) {
      if (_resolved) return;
      var msgEl = item.querySelector('.mr-toast-msg');
      if (msgEl) msgEl.textContent = msg;
    }

    return {
      done: function (msg) { _resolve(msg || initialMsg + ' ✓', 'success', 3000); },
      error: function (msg) { _resolve(msg, 'error', 7000); },
      update: function (msg) { _update(msg); },
    };
  }

  // Wire up HTMX device-action buttons automatically.
  // Buttons opt in by carrying data-mr-action-label="Label text".
  document.addEventListener('htmx:beforeRequest', function (e) {
    var label = e.detail.elt && e.detail.elt.dataset && e.detail.elt.dataset.mrActionLabel;
    if (!label) return;
    var notify = mrNotify(label + '…');
    e.detail.elt._mrNotify = notify;
  });

  document.addEventListener('htmx:afterRequest', function (e) {
    var notify = e.detail.elt && e.detail.elt._mrNotify;
    if (!notify) return;
    delete e.detail.elt._mrNotify;

    if (e.detail.successful) {
      var msg = e.detail.elt.dataset.mrActionLabel + ' ✓';
      try {
        var body = JSON.parse(e.detail.xhr.responseText);
        if (body && body.message) msg = body.message;
      } catch (_) {}
      notify.done(msg);
      // Reload after the toast is visible briefly
      setTimeout(function () { window.location.reload(); }, 1600);
    } else {
      var errMsg = 'Error';
      try {
        var errBody = JSON.parse(e.detail.xhr.responseText);
        if (errBody && errBody.detail) errMsg = errBody.detail;
        else if (errBody && errBody.message) errMsg = errBody.message;
      } catch (_) {}
      if (!errMsg || errMsg === 'Error') errMsg = 'Error ' + (e.detail.xhr.status || '');
      notify.error(errMsg);
    }
  });

  window.mrNotify = mrNotify;
})();
