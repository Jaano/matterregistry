(function () {
  "use strict";
  var dialog = document.getElementById("mr-search-dialog");
  if (!dialog || !dialog.showModal) return;
  var input = document.getElementById("mr-search-input");
  var results = document.getElementById("mr-search-results");
  var timer;
  var controller;
  function links() { return Array.prototype.slice.call(results.querySelectorAll("a")); }
  function select(index) {
    var items = links();
    if (!items.length) return;
    items.forEach(function (item, i) { item.setAttribute("aria-selected", i === index ? "true" : "false"); });
    items[index].scrollIntoView({ block: "nearest" });
  }
  function load() {
    if (controller) controller.abort();
    controller = new AbortController();
    var url = dialog.dataset.searchUrl + "?q=" + encodeURIComponent(input.value);
    fetch(url, { signal: controller.signal, credentials: "same-origin" })
      .then(function (response) { if (!response.ok) throw new Error("Search failed"); return response.text(); })
      .then(function (html) { results.innerHTML = html; select(0); })
      .catch(function (error) { if (error.name !== "AbortError") results.textContent = error.message; });
  }
  function open() { dialog.showModal(); input.value = ""; input.focus(); load(); }
  dialog.querySelector("[data-mr-search-close]").addEventListener("click", function () { dialog.close(); });
  input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(load, 160); });
  dialog.addEventListener("keydown", function (event) {
    var items = links();
    var current = items.findIndex(function (item) { return item.getAttribute("aria-selected") === "true"; });
    if (event.key === "ArrowDown" && items.length) { event.preventDefault(); select((current + 1) % items.length); }
    if (event.key === "ArrowUp" && items.length) { event.preventDefault(); select((current - 1 + items.length) % items.length); }
    if (event.key === "Enter" && items.length && document.activeElement === input) { event.preventDefault(); items[Math.max(0, current)].click(); }
  });
  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (dialog.open) dialog.close(); else open();
    }
  });
})();
