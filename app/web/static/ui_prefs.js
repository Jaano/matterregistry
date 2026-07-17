(function () {
  var THEME_KEY   = 'mr.ui.theme';
  var LANG_KEY    = 'mr.ui.language';
  var STICKER_KEY = 'mr.ui.sticker_format';
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
    document.cookie = 'mr_lang=' + encodeURIComponent(value) + '; path=/; max-age=31536000; SameSite=Lax';
  }

  applyLang(localStorage.getItem(LANG_KEY) || '');

  window.mrSetLanguage = function (value) {
    localStorage.setItem(LANG_KEY, value);
    applyLang(value);
    window.location.reload();
  };

  /* ── QR sticker label format ───────────────────────────── */
  /* The print dialog's paper choice can't be detected from CSS/JS, so the
     label-sheet format is a stored preference. Pages that print stickers
     carry one <style data-sticker-format="..."> block per format; exactly
     the selected one stays enabled. */
  function applyStickerFormat(value) {
    var styles = document.querySelectorAll('style[data-sticker-format]');
    for (var i = 0; i < styles.length; i++) {
      styles[i].disabled = styles[i].getAttribute('data-sticker-format') !== value;
    }
  }

  window.mrSetStickerFormat = function (value) {
    localStorage.setItem(STICKER_KEY, value);
    applyStickerFormat(value);
    var picker = document.querySelector('[data-sticker-picker]');
    if (picker && picker.value !== value) picker.value = value;
  };

  /* ── DOMContentLoaded: sync pickers, apply body-level prefs ── */
  document.addEventListener('DOMContentLoaded', function () {
    var themePicker = document.querySelector('[data-theme-picker]');
    if (themePicker) themePicker.value = localStorage.getItem(THEME_KEY) || 'system';

    var langPicker = document.querySelector('[data-lang-picker]');
    var storedLang = localStorage.getItem(LANG_KEY);
    if (langPicker && storedLang) langPicker.value = storedLang;

    applyStickerFormat(localStorage.getItem(STICKER_KEY) || 'l7162');
    var stickerPicker = document.querySelector('[data-sticker-picker]');
    if (stickerPicker) stickerPicker.value = localStorage.getItem(STICKER_KEY) || 'l7162';
  });
})();
