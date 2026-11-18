(function () {
  var mobile = window.matchMedia('(max-width: 720px)');

  function syncDefaultView() {
    var viewInput = document.querySelector('.mr-filters input[name="view"]');
    var results = document.querySelector('.mr-device-results, .mr-product-results');
    if (!viewInput || viewInput.value || !results) return;

    var view = mobile.matches ? 'cards' : 'table';
    results.dataset.view = view;
    results.querySelectorAll('[data-view-option]').forEach(function (option) {
      if (option.dataset.viewOption === view) option.setAttribute('aria-current', 'true');
      else option.removeAttribute('aria-current');
    });
  }

  syncDefaultView();
  mobile.addEventListener('change', syncDefaultView);
  document.addEventListener('htmx:afterSwap', syncDefaultView);
})();
