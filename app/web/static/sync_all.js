/** Run the configured integration syncs from the shared header. */
(function () {
  const resultKey = 'mr-sync-all-results';

  function showSavedResults() {
    let saved;
    try {
      saved = sessionStorage.getItem(resultKey);
      sessionStorage.removeItem(resultKey);
    } catch (_) {
      return;
    }
    if (!saved || !window.mrNotify) return;
    try {
      JSON.parse(saved).forEach(function (result) {
        const notify = window.mrNotify(result.label);
        if (result.ok) notify.done(result.label);
        else notify.error(result.label + ': ' + result.error);
      });
    } catch (_) {
      // Ignore stale session data.
    }
  }

  async function syncAll() {
    const btn = document.getElementById('sync-all-btn');
    if (!btn || btn.disabled || !btn.dataset.apiBase) return;
    const icon = btn.querySelector('.icon-sync-circle');
    const baseUrl = btn.dataset.apiBase;

    btn.disabled = true;
    if (icon) icon.classList.add('mr-spin');

    const steps = [
      {enabled: btn.dataset.syncMs === 'true', label: 'Matter Server', url: baseUrl + '/matter-server/import/apply'},
      {enabled: btn.dataset.syncOtbr === 'true', label: 'OpenThread Border Router', url: baseUrl + '/otbr/poll/apply'},
      {enabled: btn.dataset.syncHa === 'true', label: 'Home Assistant', url: baseUrl + '/ha-core/sync'},
      {enabled: btn.dataset.syncMdns === 'true', label: 'mDNS Discovery', url: baseUrl + '/mdns/sync'},
    ];
    const results = [];
    for (const step of steps.filter(function (item) { return item.enabled; })) {
      const notify = window.mrNotify ? window.mrNotify(step.label) : null;
      try {
        const response = await fetch(step.url, {method: 'POST'});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        results.push({label: step.label, ok: true});
        if (notify) notify.done(step.label);
      } catch (error) {
        results.push({label: step.label, ok: false, error: error.message});
        if (notify) notify.error(step.label + ': ' + error.message);
      }
    }

    if (icon) icon.classList.remove('mr-spin');
    btn.disabled = false;
    try {
      sessionStorage.setItem(resultKey, JSON.stringify(results));
    } catch (_) {
      // The page can still refresh when session storage is unavailable.
    }
    window.location.reload();
  }

  showSavedResults();
  window.syncAll = syncAll;
})();
