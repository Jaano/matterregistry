(function () {
  var THEME_KEY = 'mr.ui.theme';
  var LANG_KEY  = 'mr.ui.language';
  var media = window.matchMedia('(prefers-color-scheme: dark)');

  /* ── Theme ─────────────────────────────────────────────── */
  function applyTheme(value) {
    var dark = value === 'dark' || (value !== 'light' && media.matches);
    document.documentElement.classList.toggle('dark', dark);
  }

  applyTheme(localStorage.getItem(THEME_KEY) || 'system');

  media.addEventListener('change', function () {
    if ((localStorage.getItem(THEME_KEY) || 'system') === 'system') applyTheme('system');
  });

  window.mrSetTheme = function (value) {
    localStorage.setItem(THEME_KEY, value);
    applyTheme(value);
    var picker = document.querySelector('[data-theme-picker]');
    if (picker && picker.value !== value) picker.value = value;
  };

  /* ── Language ───────────────────────────────────────────── */
  function applyLang(value) {
    if (!value) return;
    document.documentElement.lang = value;
    document.cookie = 'mr_lang=' + encodeURIComponent(value) + '; path=/; SameSite=Lax';
  }

  applyLang(localStorage.getItem(LANG_KEY) || '');

  window.mrSetLanguage = function (value) {
    localStorage.setItem(LANG_KEY, value);
    applyLang(value);
    window.location.reload();
  };

  /* ── DOMContentLoaded: sync pickers ────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    var themePicker = document.querySelector('[data-theme-picker]');
    if (themePicker) themePicker.value = localStorage.getItem(THEME_KEY) || 'system';

    var langPicker = document.querySelector('[data-lang-picker]');
    if (langPicker) langPicker.value = localStorage.getItem(LANG_KEY) || 'en';
  });
})();
