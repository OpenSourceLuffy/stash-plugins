// Privacy Blur - blur pictures (and optionally names/titles).
// Turned on and off with the "Blur on" switch in Settings -> Plugins ->
// Privacy Blur, or with Shift+Alt+B (which flips that same setting).
(async function () {
  "use strict";

  const PLUGIN_ID = "PrivacyBlur";
  // Last known state, cached only so pictures don't flash before the
  // settings arrive. The plugin setting is the real source of truth.
  const CACHE_KEY = "privacy-blur-on";
  const root = document.documentElement;

  try {
    if (localStorage.getItem(CACHE_KEY) === "1") root.classList.add("pb-on");
  } catch (e) {}

  async function gql(query, variables) {
    const res = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables }),
    });
    return res.json();
  }

  let settings = {};

  function apply() {
    const on = !!settings.enabled;
    root.classList.toggle("pb-on", on);
    try {
      localStorage.setItem(CACHE_KEY, on ? "1" : "0");
    } catch (e) {}

    const strength = Math.min(Math.max(Number(settings.blurStrength) || 18, 4), 40);
    root.style.setProperty("--pb-blur", strength + "px");
    root.style.setProperty("--pb-text-blur", Math.max(4, Math.round(strength / 3)) + "px");
    root.classList.toggle("pb-names", !!settings.blurNames);
    root.classList.toggle("pb-titles", !!settings.blurTitles);
    root.classList.toggle("pb-peek", !!settings.peekOnHover);
    syncTitle();
  }

  async function loadSettings() {
    try {
      const json = await gql("{ configuration { plugins } }");
      settings = (json.data && json.data.configuration.plugins[PLUGIN_ID]) || {};
      apply();
    } catch (e) {
      console.warn("[Privacy Blur] could not load settings", e);
    }
  }

  // The browser tab shows the scene/performer name; hide it too while names
  // or titles are being blurred
  let realTitle = document.title;
  function syncTitle() {
    const hide = !!(settings.blurNames || settings.blurTitles) && root.classList.contains("pb-on");
    if (hide) {
      if (document.title !== "Stash") {
        realTitle = document.title;
        document.title = "Stash";
      }
    } else if (document.title === "Stash" && realTitle && realTitle !== "Stash") {
      document.title = realTitle;
    }
  }
  const titleEl = document.querySelector("title");
  if (titleEl) new MutationObserver(syncTitle).observe(titleEl, { childList: true, characterData: true, subtree: true });

  await loadSettings();

  // Pick up changes made on the settings page without a reload: re-read the
  // settings whenever the page changes and while the plugin settings are open
  // (Stash saves each switch as soon as it's flipped).
  let lastPath = location.pathname + location.search;
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    setTimeout(() => {
      queued = false;
      const path = location.pathname + location.search;
      if (path !== lastPath) {
        lastPath = path;
        loadSettings();
      }
    }, 300);
  }).observe(document.body, { childList: true, subtree: true });

  // Also re-read after any change on the plugin settings page
  document.addEventListener(
    "change",
    () => {
      if (location.pathname.startsWith("/settings")) setTimeout(loadSettings, 500);
    },
    true
  );
  document.addEventListener(
    "click",
    () => {
      if (location.pathname.startsWith("/settings")) setTimeout(loadSettings, 500);
    },
    true
  );

  // Keyboard shortcut: Shift+Alt+B flips the "Blur on" setting
  document.addEventListener("keydown", async (e) => {
    if (!(e.shiftKey && e.altKey && !e.ctrlKey && !e.metaKey && e.code === "KeyB")) return;
    e.preventDefault();
    settings = Object.assign({}, settings, { enabled: !settings.enabled });
    apply();
    try {
      await gql(
        "mutation($id: ID!, $input: Map!) { configurePlugin(plugin_id: $id, input: $input) }",
        { id: PLUGIN_ID, input: settings }
      );
    } catch (err) {
      console.warn("[Privacy Blur] could not save setting", err);
    }
  });
})();
