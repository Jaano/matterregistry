/**
 * Shared syncAll() helper - used by both the device-list header button and
 * (if ever needed again) the integrations settings page.
 *
 * The button element must carry:
 *   id="sync-all-btn"
 *   data-api-base="<url-to-/api/integrations>"
 */
(function () {
  async function syncAll() {
    const btn = document.getElementById('sync-all-btn');
    if (!btn) return;
    const icon = btn.querySelector('.icon-sync-circle');
    const baseUrl = btn.dataset.apiBase || '/api/integrations';

    btn.disabled = true;
    if (icon) icon.classList.add('mr-spin');

    const steps = [
      {label: btn.dataset.labelMs   || 'Matter Server',              url: baseUrl + '/matter-server/import/apply'},
      {label: btn.dataset.labelOtbr || 'OpenThread Border Router',   url: baseUrl + '/otbr/poll/apply'},
      {label: btn.dataset.labelHa   || 'Home Assistant',             url: baseUrl + '/ha-core/sync'},
      {label: btn.dataset.labelMdns || 'mDNS Discovery',             url: baseUrl + '/mdns/sync'},
    ];
    for (const step of steps) {
      const notify = window.mrNotify ? window.mrNotify(step.label + '\u2026') : null;
      try {
        const r = await fetch(step.url, {method: 'POST'});
        if (!r.ok) throw new Error('HTTP ' + r.status);
        if (notify) notify.done(step.label + ' \u2713');
      } catch (e) {
        if (notify) notify.error(step.label + ' \u2717 (' + e.message + ')');
      }
    }

    if (icon) icon.classList.remove('mr-spin');
    btn.disabled = false;

    // Refresh device list if on the devices page
    var rows = document.getElementById('device-rows');
    if (rows && typeof htmx !== 'undefined') {
      var params = new URLSearchParams();
      var q = document.getElementById('search-q');
      if (q && q.value) params.set('q', q.value);
      var fs = document.getElementById('filter-status');
      if (fs && fs.value) params.set('status', fs.value);
      var fv = document.getElementById('filter-vendor');
      if (fv && fv.value) params.set('vendor', fv.value);
      var fr = document.getElementById('filter-room');
      if (fr && fr.value) params.set('room', fr.value);
      var qs = params.toString();
      var devicesUrl = btn.dataset.apiBase.replace('/api/integrations', '/devices') + (qs ? '?' + qs : '');
      htmx.ajax('GET', devicesUrl, {target: '#device-rows', swap: 'outerHTML'});
    }
  }

  window.syncAll = syncAll;
})();
