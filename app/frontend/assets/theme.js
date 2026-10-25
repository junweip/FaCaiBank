// Applies the saved colour theme before the page paints, so there is no flash.
(function () {
  try {
    var saved = window.localStorage.getItem("facai.theme");
    if (saved === "light" || saved === "dark") document.documentElement.setAttribute("data-theme", saved);
  } catch (error) {
    /* storage unavailable: follow the system setting */
  }
})();
