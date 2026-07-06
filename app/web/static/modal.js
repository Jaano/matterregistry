(function () {
  var _overlay, _title, _body, _cancel, _confirm, _resolve, _returnFocus;

  // Localized default labels come from data-* attributes on #mr-modal
  // (rendered by the server via the translation dict). Falls back to English
  // if the element or attributes are missing.
  function _labels() {
    var el = document.getElementById('mr-modal');
    var d = (el && el.dataset) || {};
    return {
      confirm: d.labelConfirm || 'Confirm',
      cancel: d.labelCancel || 'Cancel',
      delete: d.labelDelete || 'Delete',
    };
  }

  function _init() {
    _overlay = document.getElementById('mr-modal');
    _title   = document.getElementById('mr-modal-title');
    _body    = document.getElementById('mr-modal-body');
    _cancel  = document.getElementById('mr-modal-cancel');
    _confirm = document.getElementById('mr-modal-confirm');

    _cancel.addEventListener('click', function () { _close(false); });
    _confirm.addEventListener('click', function () { _close(true); });
    _overlay.addEventListener('click', function (e) {
      if (e.target === _overlay) _close(false);
    });
    document.addEventListener('keydown', function (e) {
      if (_overlay.classList.contains('hidden')) return;
      if (e.key === 'Escape') { e.preventDefault(); _close(false); }
      if (e.key === 'Tab') _trapTab(e);
    });
  }

  function _trapTab(e) {
    var focusable = Array.from(_overlay.querySelectorAll('button:not([disabled])'));
    var first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault(); last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault(); first.focus();
    }
  }

  function _close(confirmed) {
    _overlay.classList.add('hidden');
    var r = _resolve; _resolve = null;
    if (_returnFocus) { _returnFocus.focus(); _returnFocus = null; }
    if (r) r(confirmed);
  }

  window.openModal = function (opts) {
    if (!_overlay) _init();
    var labels = _labels();
    _title.textContent = opts.title || labels.confirm;
    if (opts.bodyHtml !== undefined) {
      _body.innerHTML = opts.bodyHtml;
    } else {
      _body.textContent = opts.body || '';
    }
    _confirm.textContent = opts.confirmLabel || labels.confirm;
    if (opts.destructive !== false) {
      _title.className = 'text-lg font-semibold mb-3 text-red-700';
      _confirm.className = 'px-4 py-2 text-sm rounded bg-red-600 text-white hover:bg-red-700';
    } else {
      _title.className = 'text-lg font-semibold mb-3';
      _confirm.className = 'px-4 py-2 text-sm rounded bg-blue-600 text-white hover:bg-blue-700';
    }
    _returnFocus = document.activeElement;
    _overlay.classList.remove('hidden');
    _cancel.focus();
    return new Promise(function (resolve) { _resolve = resolve; });
  };

  // Intercept hx-confirm dialogs globally. htmx fires this event on every
  // request; detail.question is null unless an hx-confirm attribute is set,
  // so bail out (letting the request proceed) when there's nothing to confirm.
  document.body.addEventListener('htmx:confirm', function (evt) {
    if (!evt.detail.question) return;
    evt.preventDefault();
    var labels = _labels();
    openModal({
      title: labels.confirm,
      body: evt.detail.question,
      confirmLabel: labels.delete,
      destructive: true,
    }).then(function (confirmed) {
      if (confirmed) evt.detail.issueRequest(true);
    });
  });
})();
