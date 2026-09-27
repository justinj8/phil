// Runs before first paint: restore the viewer's theme choice (if any) so the
// page never flashes the wrong mode. Storage can be unavailable; that is fine.
(function () {
  try {
    var t = localStorage.getItem("phil-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* no stored preference */ }
})();
