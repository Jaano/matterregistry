(function () {
  "use strict";
  var search = document.getElementById("new-device-product-search");
  var select = document.getElementById("new-device-product");
  if (!search || !select) return;
  search.addEventListener("input", function () {
    var query = search.value.trim().toLocaleLowerCase();
    Array.prototype.forEach.call(select.options, function (option) {
      option.hidden = Boolean(option.value && query && !option.textContent.toLocaleLowerCase().includes(query));
    });
  });
})();
