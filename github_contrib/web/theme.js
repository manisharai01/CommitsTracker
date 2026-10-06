// Applies the saved theme before first paint (app.js is a deferred module).
try {
  const theme = localStorage.getItem("commitstracker.theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch {
  // Storage unavailable: follow the system theme.
}
