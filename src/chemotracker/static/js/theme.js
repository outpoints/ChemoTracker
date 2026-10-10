// Applies the saved light/dark choice before the page paints (loaded without defer on purpose).
try { document.documentElement.dataset.theme = localStorage.getItem("ct-theme") || "auto"; } catch (e) {}
