(async function () {
  const PLUGIN_ID = "ModernUI";
  const MOBILE_QUERY = window.matchMedia("(max-width: 576px)");
  const body = document.body.classList;
  body.add("mui");

  // ------------------------------------------------------------------
  // Light / dark theme
  // ------------------------------------------------------------------
  // The last choice is remembered so the page doesn't flash dark while
  // the settings load.
  const THEME_KEY = "mui-theme";
  const DARK_QUERY = window.matchMedia("(prefers-color-scheme: dark)");
  let themeSetting = localStorage.getItem(THEME_KEY) || "dark";

  function applyTheme() {
    const light = themeSetting === "light" || (themeSetting === "auto" && !DARK_QUERY.matches);
    body.toggle("mui-light", light);
    document.documentElement.style.colorScheme = light ? "light" : "dark";
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "theme-color";
      document.head.appendChild(meta);
    }
    meta.content = light ? "#f4f6f8" : "#202b33";
  }
  applyTheme();
  DARK_QUERY.addEventListener("change", applyTheme);

  let cfg = {};
  let cfgLoaded = false;
  try {
    const res = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: "{ configuration { plugins } }" }),
    });
    const json = await res.json();
    cfg = (json.data.configuration.plugins || {})[PLUGIN_ID] || {};
    cfgLoaded = true;
  } catch (e) {
    console.warn("[Modern UI] could not load settings, using defaults", e);
  }

  if (cfgLoaded) {
    const theme = String(cfg.colorTheme || "").trim().toLowerCase();
    themeSetting = ["light", "auto"].includes(theme) ? theme : "dark";
    localStorage.setItem(THEME_KEY, themeSetting);
    applyTheme();
  }

  body.toggle("mui-show-details", !!cfg.showDetails);
  body.toggle("mui-show-popovers", !!cfg.showPopovers);
  body.toggle("mui-show-overlays", !!cfg.showOverlays);
  body.toggle("mui-hide-title", !!cfg.hideTitle);
  body.toggle("mui-mobile", !cfg.disableMobile);
  body.toggle("mui-fit-contain", !!cfg.showWholePictures);
  body.toggle("mui-scene-stack", !cfg.sceneDetailsBeside);
  body.toggle("mui-image-stack", !cfg.imageDetailsBeside);

  const RATIOS = { portrait: "3 / 4", square: "1 / 1", landscape: "4 / 3", wide: "16 / 9" };
  const shape = String(cfg.pictureShape || "").trim().toLowerCase();
  if (RATIOS[shape]) {
    document.documentElement.style.setProperty("--mui-picture-ratio", RATIOS[shape]);
  }

  const cols = Number(cfg.mobileColumns) === 1 ? 1 : 2;
  document.documentElement.style.setProperty("--mui-mobile-cols", cols);
  document.documentElement.style.setProperty("--mui-mobile-image-cols", cols + 1);

  if (!cfg.disableMobile && !cfg.disableBottomNav) {
    setupBottomNav(cfg);
  }

  setupNativeCarouselScroll();
  setupSidebarButton();
  setupHomeFlag();
  setupEditingFlag();
  setupLongPressSelect();
  setupGalleryTitle();
  setupTagIndex();
  setupSuggested();
  setupSimilarImages();
  setupSimilarPerformers();
  if (!cfg.showGalleryDetails) setupGalleryPanelClosed();

  if (!cfg.disableImageSwipe) {
    setupImageSwipe();
  }

  // Stash's filter sidebar toggle is a half-transparent tab stuck to the left
  // edge. Put a proper "Filters" button at the start of the toolbar instead;
  // it just clicks Stash's own (now hidden) toggle.
  function setupSidebarButton() {
    const ICON =
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
      '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/>' +
      '<circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/></svg>';

    function sync() {
      document.querySelectorAll(".sidebar-pane-content").forEach((content) => {
        const original = content.querySelector(":scope > .sidebar-toggle-button-container .sidebar-toggle-button");
        const toolbar = content.querySelector(":scope > .filtered-list-toolbar");
        if (!original || !toolbar) return;
        let btn = toolbar.querySelector(":scope > .mui-sidebar-btn");
        if (!btn) {
          btn = document.createElement("button");
          btn.type = "button";
          btn.className = "btn btn-secondary mui-sidebar-btn ignore-sidebar-outside-click";
          btn.title = "Show or hide filters";
          btn.innerHTML = ICON + '<span class="mui-sidebar-btn-label">Filters</span>';
          btn.addEventListener("click", () => {
            const current = btn.closest(".sidebar-pane-content")
              ?.querySelector(":scope > .sidebar-toggle-button-container .sidebar-toggle-button");
            current?.click();
          });
          toolbar.insertBefore(btn, toolbar.firstChild);
        }
        const pane = content.closest(".sidebar-pane");
        const open = !!pane && !pane.classList.contains("hide-sidebar");
        if (btn.classList.contains("active") !== open) btn.classList.toggle("active", open);
        btn.setAttribute("aria-pressed", String(open));
      });
    }

    sync();
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
  }

  // ------------------------------------------------------------------
  // Touch screens: press and hold a card to select it
  // ------------------------------------------------------------------
  // Stash's select checkbox only appears on mouse hover. Holding a card
  // ticks it; once anything is selected, Stash itself makes a normal tap
  // add/remove cards, and its toolbar shows Edit / Delete for the selection.

  function setupLongPressSelect() {
    const HOLD_MS = 450;
    const MOVE_TOLERANCE = 10; // px
    let timer = null, start = null, fired = false, suppressUntil = 0;

    const cancel = () => {
      clearTimeout(timer);
      timer = null;
    };

    window.addEventListener("touchstart", (e) => {
      cancel();
      fired = false;
      if (e.touches.length !== 1 || !(e.target instanceof Element)) return;
      const card = e.target.closest(".grid-card");
      if (!card || card.closest(".slick-list")) return; // home rows can't select
      if (e.target.closest("input, button, .btn, .card-popovers")) return;
      const check = card.querySelector(".card-check");
      if (!check) return;
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      timer = setTimeout(() => {
        timer = null;
        fired = true;
        suppressUntil = performance.now() + 800;
        check.click(); // goes through Stash's own selection handling
        if (navigator.vibrate) navigator.vibrate(15);
      }, HOLD_MS);
    }, { capture: true, passive: true });

    window.addEventListener("touchmove", (e) => {
      if (!timer || !start) return;
      const t = e.touches[0];
      if (Math.abs(t.clientX - start.x) > MOVE_TOLERANCE || Math.abs(t.clientY - start.y) > MOVE_TOLERANCE) cancel();
    }, { capture: true, passive: true });

    window.addEventListener("touchend", (e) => {
      cancel();
      if (fired) {
        e.preventDefault(); // don't also open the card
        fired = false;
      }
    }, { capture: true, passive: false });
    window.addEventListener("touchcancel", cancel, { capture: true, passive: true });

    // Backup: swallow the tap the browser may still send after a hold
    window.addEventListener("click", (e) => {
      if (performance.now() < suppressUntil && e.target instanceof Element &&
          e.target.closest(".grid-card") && !e.target.closest(".card-check")) {
        e.preventDefault();
        e.stopPropagation();
        suppressUntil = 0;
      }
    }, { capture: true });

    // No "open link / save image" menu when holding a card
    window.addEventListener("contextmenu", (e) => {
      if (e.target instanceof Element && e.target.closest(".grid-card") &&
          window.matchMedia("(hover: none)").matches) e.preventDefault();
    }, { capture: true });
  }

  // ------------------------------------------------------------------
  // Gallery page: show the gallery's name above its pictures
  // ------------------------------------------------------------------
  // Stash only shows it in the details panel on the left, which can be
  // collapsed. We copy it into a heading at the top of the picture area.

  function setupGalleryTitle() {
    function sync() {
      const container = document.querySelector(".gallery-container");
      let heading = document.querySelector(".mui-gallery-title");
      if (!container) {
        heading?.remove();
        return;
      }
      const source = document.querySelector(".gallery-tabs .gallery-header");
      const name = (source?.textContent || "").trim();
      if (!name) return;
      if (!heading || heading.parentElement !== container) {
        heading?.remove();
        heading = document.createElement("h2");
        heading.className = "mui-gallery-title";
        container.insertBefore(heading, container.firstChild);
      }
      if (heading.textContent !== name) {
        heading.textContent = name;
        heading.title = name;
      }
    }

    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
    sync();
  }

  // Gallery page: start with the details panel closed (wide screens).
  // Clicks Stash's own collapse button once per gallery, so the blue bar
  // still opens it and it stays open until you go to another gallery.
  function setupGalleryPanelClosed() {
    const GALLERY_PAGE = /^\/galleries\/(\d+)/;
    let handledId = null;

    function check() {
      const id = (window.location.pathname.match(GALLERY_PAGE) || [])[1] || null;
      if (!id) {
        handledId = null;
        return;
      }
      if (id === handledId) return;
      const tabs = document.querySelector(".gallery-tabs");
      const button = document.querySelector(".gallery-divider button");
      if (!tabs || !button || !button.offsetParent) return; // not loaded yet / narrow screen
      handledId = id;
      if (!tabs.classList.contains("collapsed")) button.click();
    }

    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        check();
      });
    }).observe(document.body, { childList: true, subtree: true });
    check();
  }

  // ------------------------------------------------------------------
  // Tags page: "A-Z" view
  // ------------------------------------------------------------------
  // An extra button next to Stash's Grid / List buttons. It shows every tag
  // that's used on at least one scene or image as an alphabetical index: big letter headings, names in columns, and a
  // row of letters to jump around. The toolbar's search box still filters
  // it. The choice is remembered on this device.

  function setupTagIndex() {
    const STORE_KEY = "mui-tags-az";
    const onTagsPage = () => /^\/tags\/?$/.test(window.location.pathname);
    let active = localStorage.getItem(STORE_KEY) === "1";
    let tags = null; // [{ id, name }]
    let loading = false;
    let renderedFor = null;

    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const letterOf = (name) => {
      const c = String(name).trim().normalize("NFD").replace(/[̀-ͯ]/g, "").charAt(0).toUpperCase();
      return c >= "A" && c <= "Z" ? c : "#";
    };

    async function load() {
      if (loading) return;
      loading = true;
      try {
        const res = await fetch("/graphql", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: '{ findTags(filter: { per_page: -1, sort: "name", direction: ASC }) { tags { id name scene_count image_count } } }',
          }),
        });
        const json = await res.json();
        // Only tags that are actually used on a scene or an image
        tags = json.data.findTags.tags.filter((t) => (t.scene_count || 0) > 0 || (t.image_count || 0) > 0);
      } catch (e) {
        console.warn("[Modern UI] could not load tags", e);
        tags = [];
      }
      loading = false;
      renderedFor = null;
      sync();
    }

    function searchTerm() {
      const fromUrl = new URLSearchParams(window.location.search).get("q");
      if (fromUrl) return fromUrl;
      const box = document.querySelector('.tag-list .filtered-list-toolbar input[placeholder^="Search"], .tag-list .filtered-list-toolbar .search-term-input input');
      return box ? box.value : "";
    }

    function render(view) {
      if (!tags) {
        view.innerHTML = '<div class="mui-az-empty">Loading tags…</div>';
        return;
      }
      const q = searchTerm().trim().toLowerCase();
      const key = q + "|" + tags.length;
      if (renderedFor === key && view.childElementCount) return;
      renderedFor = key;

      const list = (q ? tags.filter((t) => t.name.toLowerCase().includes(q)) : tags.slice())
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true }));
      if (!list.length) {
        view.innerHTML = '<div class="mui-az-empty">No tags match.</div>';
        return;
      }

      const groups = new Map();
      for (const t of list) {
        const l = letterOf(t.name);
        if (!groups.has(l)) groups.set(l, []);
        groups.get(l).push(t);
      }
      const letters = [...groups.keys()].sort((a, b) => (a === "#" ? -1 : b === "#" ? 1 : a.localeCompare(b)));
      const item = (t) => `<a class="mui-az-tag" href="/tags/${esc(t.id)}">${esc(t.name)}</a>`;

      view.innerHTML =
        `<nav class="mui-az-jump" aria-label="Jump to letter">${letters
          .map((l) => `<button type="button" data-letter="${esc(l)}">${esc(l)}</button>`).join("")}</nav>` +
        `<div class="mui-az-count">${list.length} tag${list.length === 1 ? "" : "s"}</div>` +
        '<div class="mui-az-columns">' +
        letters.map((l) => {
          const items = groups.get(l);
          // Heading + first few names stay together so a letter never sits
          // alone at the bottom of a column
          return `<section class="mui-az-group" data-letter="${esc(l)}">` +
            `<div class="mui-az-keep"><h3 class="mui-az-letter">${esc(l)}</h3>${items.slice(0, 3).map(item).join("")}</div>` +
            items.slice(3).map(item).join("") +
            "</section>";
        }).join("") +
        "</div>";
    }

    function findDisplayGroup(toolbar) {
      const zoom = toolbar.querySelector(".zoom-slider-container");
      const prev = zoom && zoom.previousElementSibling;
      if (prev && prev.classList.contains("btn-group")) return prev;
      const groups = [...toolbar.querySelectorAll(".btn-group")].filter((g) => g.querySelector(".btn svg") && !g.textContent.trim());
      return groups[groups.length - 1] || null;
    }

    function setActive(on) {
      active = on;
      localStorage.setItem(STORE_KEY, on ? "1" : "0");
      if (on) load(); // (re)load the tag names each time it's opened
      renderedFor = null;
      sync();
    }

    function sync() {
      const content = onTagsPage() && document.querySelector(".tag-list .sidebar-pane-content");
      const toolbar = content && content.querySelector(":scope > .filtered-list-toolbar");
      if (!content || !toolbar) {
        body.remove("mui-az-active");
        return;
      }

      const group = findDisplayGroup(toolbar);
      let btn = toolbar.querySelector(".mui-az-btn");
      if (group && (!btn || btn.parentElement !== group)) {
        btn?.remove();
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-secondary mui-az-btn";
        btn.title = "A–Z index";
        btn.textContent = "A–Z";
        btn.addEventListener("click", () => setActive(true));
        group.appendChild(btn);
        // Picking one of Stash's own views turns the index off
        group.addEventListener("click", (e) => {
          if (!e.target.closest(".mui-az-btn") && e.target.closest(".btn")) setActive(false);
        });
      }
      if (btn && btn.classList.contains("active") !== active) btn.classList.toggle("active", active);
      if (group) group.classList.toggle("mui-az-group-on", active);

      let view = content.querySelector(":scope > .mui-az");
      if (!active) {
        view?.remove();
        body.remove("mui-az-active");
        return;
      }
      body.add("mui-az-active");
      if (!view) {
        view = document.createElement("div");
        view.className = "mui-az";
        const tagsRow = content.querySelector(":scope > .filter-tags");
        (tagsRow || toolbar).after(view);
        view.addEventListener("click", (e) => {
          const jump = e.target.closest("[data-letter]");
          if (jump && jump.tagName === "BUTTON") {
            view.querySelector(`.mui-az-group[data-letter="${CSS.escape(jump.dataset.letter)}"]`)
              ?.scrollIntoView({ behavior: "smooth", block: "start" });
            return;
          }
          const a = e.target.closest("a.mui-az-tag");
          if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          routerNavigate(a.getAttribute("href"));
        });
        renderedFor = null;
      }
      if (!tags) load();
      render(view);
    }

    let queued = false;
    new MutationObserver((mutations) => {
      if (queued || mutations.every((m) => m.target.closest?.(".mui-az"))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true });
    document.addEventListener("input", (e) => {
      if (active && e.target instanceof Element && e.target.closest(".tag-list .filtered-list-toolbar")) {
        clearTimeout(setupTagIndex.t);
        setupTagIndex.t = setTimeout(sync, 200);
      }
    }, true);
    window.addEventListener("popstate", sync);
    sync();
  }

  // ------------------------------------------------------------------
  // Scene page: "Suggested" and "More with <performer>" rows at the bottom
  // ------------------------------------------------------------------
  // Suggested: scenes with shared tags, the same studio or a similar length
  // and shape, scored (tags, studio, look-alike performers, length,
  // orientation) and shown best first; topped up if there are few. Scenes
  // already in the More with row are left out. More with: scenes with the
  // same performer(s), random order.

  // How alike two performers look, from their profile details. Used by
  // Similar Performers and the Suggested scenes row.
  // Ethnicity 3, hair 2, eyes 1, country 1, breasts 1, height within 5cm 1,
  // age within 5 years 1 (max 10).
  // (function declarations, not consts: the setup functions run before
  // this point in the file)
  function sameText(a, b) {
    return !!a && !!b && String(a).trim().toLowerCase() === String(b).trim().toLowerCase();
  }
  function birthYear(d) {
    return d ? Number(String(d).slice(0, 4)) || null : null;
  }
  function lookFields() {
    return "gender ethnicity hair_color eye_color country fake_tits height_cm birthdate";
  }
  function lookAlikePoints(p, x) {
    let k = 0;
    if (sameText(x.ethnicity, p.ethnicity)) k += 3;
    if (sameText(x.hair_color, p.hair_color)) k += 2;
    if (sameText(x.eye_color, p.eye_color)) k += 1;
    if (sameText(x.country, p.country)) k += 1;
    if (sameText(x.fake_tits, p.fake_tits)) k += 1;
    if (p.height_cm && x.height_cm && Math.abs(p.height_cm - x.height_cm) <= 5) k += 1;
    const pb = birthYear(p.birthdate), xb = birthYear(x.birthdate);
    if (pb && xb && Math.abs(pb - xb) <= 5) k += 1;
    return k;
  }

  // Random order (Fisher-Yates), returns a new array
  function shuffle(list) {
    const a = list.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }

  function setupSuggested() {
    const SCENE_PAGE = /^\/scenes\/(\d+)\/?$/;
    const MAX = 12;
    const CARD_FIELDS =
      `id title date paths { screenshot preview } files { duration basename width height } studio { id name } performers { id name ${lookFields()} } tags { id }`;

    const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const gql = async (query, variables) => {
      const res = await fetch("/graphql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, variables }),
      });
      const json = await res.json();
      if (json.errors) throw new Error(json.errors[0].message);
      return json.data;
    };
    const findScenes = async (sceneFilter, perPage) =>
      (await gql(
        `query ($f: SceneFilterType, $n: Int) { findScenes(scene_filter: $f, filter: { per_page: $n, sort: "random" }) { scenes { ${CARD_FIELDS} } } }`,
        { f: sceneFilter, n: perPage }
      )).findScenes.scenes;

    const orientationOf = (file) => {
      if (!file || !file.width || !file.height) return null;
      const r = file.width / file.height;
      return r > 1.05 ? "LANDSCAPE" : r < 0.95 ? "PORTRAIT" : "SQUARE";
    };

    // Returns [{ title, scenes }] rows to show
    async function rowsFor(id) {
      const scene = (await gql(
        `query ($id: ID!) { findScene(id: $id) { id tags { id } studio { id } performers { id name ${lookFields()} } files { duration width height } } }`,
        { id }
      )).findScene;
      if (!scene) return [];
      const perf = new Set(scene.performers.map((p) => p.id));
      const tagIds = new Set(scene.tags.map((t) => t.id));
      const notThis = (list) => list.filter((s) => s.id !== scene.id);
      // Best matches first; a little shuffle between equal matches
      const rank = (list, score) => list
        .map((s) => ({ s, k: score(s) + Math.random() * 0.5 }))
        .sort((a, b) => b.k - a.k)
        .slice(0, MAX)
        .map((x) => x.s);

      // Fill-up for Suggested, loosening step by step until there are
      // enough: similar length + same shape, then wider length, then same
      // shape only, then anything.
      async function fillUp(have, skip) {
        const file = scene.files[0];
        const o = orientationOf(file);
        const len = file && file.duration;
        const between = (lo, hi) => ({
          modifier: "BETWEEN",
          value: Math.max(0, Math.floor(len * lo)),
          value2: Math.ceil(len * hi),
        });
        const steps = [];
        if (len && o) steps.push({ duration: between(0.75, 1.25), orientation: { value: [o] } });
        if (len && o) steps.push({ duration: between(0.5, 1.5), orientation: { value: [o] } });
        if (o) steps.push({ orientation: { value: [o] } });
        if (len) steps.push({ duration: between(0.5, 1.5) });
        steps.push({});

        const result = have.slice();
        const seen = new Set(result.map((s) => s.id).concat(scene.id, ...skip));
        for (const filter of steps) {
          if (result.length >= MAX) break;
          const found = await findScenes(filter, MAX * 2);
          for (const s of found) {
            if (result.length >= MAX) break;
            if (!seen.has(s.id)) {
              seen.add(s.id);
              result.push(s);
            }
          }
        }
        return result;
      }

      const file = scene.files[0];
      const shape = orientationOf(file);
      const len = file && file.duration;
      const studioId = scene.studio && scene.studio.id;

      const [byTags, byStudio, byShape, byPerformers] = await Promise.all([
        tagIds.size
          ? findScenes({ tags: { value: [...tagIds], modifier: "INCLUDES", depth: 0 } }, 150).then(notThis)
          : Promise.resolve([]),
        studioId
          ? findScenes({ studios: { value: [studioId], modifier: "INCLUDES", depth: 0 } }, 60).then(notThis)
          : Promise.resolve([]),
        len && shape
          ? findScenes({
              duration: { modifier: "BETWEEN", value: Math.floor(len * 0.75), value2: Math.ceil(len * 1.25) },
              orientation: { value: [shape] },
            }, 40).then(notThis)
          : Promise.resolve([]),
        perf.size
          ? findScenes({ performers: { value: [...perf], modifier: "INCLUDES" } }, 60).then(notThis)
          : Promise.resolve([]),
      ]);

      const rows = [];

      // More with <performer> (worked out first so Suggested doesn't repeat it)
      const performerScenes = rank(byPerformers, (s) => s.performers.filter((p) => perf.has(p.id)).length);
      const inPerformerRow = new Set(performerScenes.map((s) => s.id));

      // Suggested: every candidate scored on
      //   each shared tag 2, same studio 3, performers who look alike
      //   (best pair, half of lookAlikePoints: up to 5), similar length 1,
      //   same orientation 1
      const score = (s) => {
        let k = 2 * s.tags.filter((t) => tagIds.has(t.id)).length;
        if (studioId && s.studio && s.studio.id === studioId) k += 3;
        let look = 0;
        for (const a of scene.performers) for (const b of s.performers) look = Math.max(look, lookAlikePoints(a, b));
        k += look / 2;
        const f = s.files[0];
        if (len && f && f.duration && Math.abs(f.duration - len) <= len * 0.25) k += 1;
        if (shape && orientationOf(f) === shape) k += 1;
        return k;
      };
      const pool = new Map();
      for (const s of [...byTags, ...byStudio, ...byShape]) {
        if (!inPerformerRow.has(s.id)) pool.set(s.id, s);
      }
      let suggested = rank([...pool.values()], score); // closest match first
      // Still short: top up with similar scenes, loosening as it goes
      if (suggested.length < MAX) suggested = await fillUp(suggested, inPerformerRow);
      if (suggested.length) rows.push({ title: "Suggested", scenes: suggested });

      if (performerScenes.length) {
        const names = scene.performers.map((p) => p.name);
        const who = names.length === 1 ? names[0]
          : names.length === 2 ? names.join(" & ")
          : "these performers";
        rows.push({ title: "More with " + who, scenes: shuffle(performerScenes) });
      }
      return rows;
    }

    const duration = (sec) => {
      if (!sec) return "";
      sec = Math.round(sec);
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = sec % 60;
      return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
    };

    function card(s) {
      const file = s.files[0] || {};
      const title = s.title || file.basename || "Untitled";
      const meta = [s.studio && s.studio.name, s.performers.slice(0, 2).map((p) => p.name).join(", ")]
        .filter(Boolean).join(" · ");
      return (
        `<a class="mui-rel-card" href="/scenes/${esc(s.id)}" data-preview="${esc(s.paths.preview || "")}">` +
        `<div class="mui-rel-thumb">` +
        (s.paths.screenshot ? `<img src="${esc(s.paths.screenshot)}" alt="" loading="lazy" decoding="async">` : "") +
        (file.duration ? `<span class="mui-rel-duration">${duration(file.duration)}</span>` : "") +
        `</div>` +
        `<div class="mui-rel-title" title="${esc(title)}">${esc(title)}</div>` +
        (meta ? `<div class="mui-rel-meta">${esc(meta)}</div>` : "") +
        `</a>`
      );
    }

    function rowHtml(row) {
      return (
        '<div class="mui-related-row">' +
        `<div class="mui-related-head"><h3>${esc(row.title)}</h3>` +
        '<div class="mui-related-arrows">' +
        '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
        '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div></div>' +
        `<div class="mui-related-strip">${row.scenes.map(card).join("")}</div>` +
        "</div>"
      );
    }

    function build(id) {
      const section = document.createElement("section");
      section.className = "mui-related";
      section.dataset.scene = id;
      section.innerHTML = '<div class="mui-related-loading">Finding suggestions…</div>';

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = arrow.closest(".mui-related-row").querySelector(".mui-related-strip");
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mui-rel-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        routerNavigate(a.getAttribute("href"));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      // Desktop: play the preview clip while hovering a card
      section.addEventListener("mouseover", (e) => {
        if (!window.matchMedia("(hover: hover)").matches) return;
        const a = e.target.closest("a.mui-rel-card");
        if (!a || !a.dataset.preview || a.querySelector("video")) return;
        const v = document.createElement("video");
        v.src = a.dataset.preview;
        v.muted = true;
        v.loop = true;
        v.playsInline = true;
        v.autoplay = true;
        a.querySelector(".mui-rel-thumb").appendChild(v);
        a.addEventListener("mouseleave", () => v.remove(), { once: true });
      });

      rowsFor(id).then((rows) => {
        if (section.dataset.scene !== id || !section.isConnected) return;
        section.innerHTML = rows.length
          ? rows.map(rowHtml).join("")
          : '<div class="mui-related-loading">No suggestions yet.</div>';
      }).catch((e) => {
        console.warn("[Modern UI] could not load suggestions", e);
        section.innerHTML = '<div class="mui-related-loading">Couldn’t load suggestions.</div>';
      });
      return section;
    }

    function sync() {
      const id = (window.location.pathname.match(SCENE_PAGE) || [])[1];
      const existing = document.querySelector(".mui-related");
      if (!id) {
        existing?.remove();
        return;
      }
      const row = document.querySelector(".scene-player-container")?.parentElement;
      if (!row) return;
      if (existing && existing.dataset.scene === id && existing.parentElement === row) return;
      existing?.remove();
      row.appendChild(build(id));
    }

    let queued = false;
    new MutationObserver((mutations) => {
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mui-related"))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("popstate", sync);
    sync();
  }

  // ------------------------------------------------------------------
  // Image page: "Similar photos" grid at the bottom
  // ------------------------------------------------------------------
  // Photos of the same performers are left out here (they have their own
  // "More with <performer>" grid). Everything else that shares a tag, a
  // gallery, or has a look-alike performer gets a score: 2 points per
  // shared tag or gallery, plus up to 5 for the closest look-alike
  // (half their Similar Performers score: looks + shared performer tags).
  // The highest scores come first (ties in random order). If there are
  // few or none, it's topped up with images that have no performers, tags
  // or galleries yet (same orientation first), then random ones. A Load
  // more card at the end of each row adds the next 12. Opening one of
  // them makes swiping move through this list. If you opened the image
  // from a gallery's page, a "More from <gallery>" row comes first; from a
  // row on the home page, "More from <row>" using that row's filter.

  function setupSimilarImages() {
    const IMAGE_PAGE = /^\/images\/(\d+)\/?$/;
    const MAX = 12;
    const TAG_POINTS = 2;
    const GALLERY_POINTS = 2;
    const LOOKALIKE_MAX_POINTS = 5;
    const LOOKALIKE_MIN = 4;       // look-alike score (out of ~10+) to count at all
    const LOOKALIKES = 15;         // closest look-alike performers to pull photos from
    const CANDIDATES = 500;        // images fetched per match type before scoring
    const FIELDS = "id title paths { thumbnail } tags { id } performers { id } galleries { id }";
    const PERF_FIELDS = `id name image_count tags { id } ${lookFields()}`;

    const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const gql = async (query, variables) => {
      const res = await fetch("/graphql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, variables }),
      });
      const json = await res.json();
      if (json.errors) throw new Error(json.errors[0].message);
      return json.data;
    };
    const findImages = async (imageFilter, perPage, page = 1, sort = "random") =>
      (await gql(
        `query ($f: ImageFilterType, $n: Int, $p: Int, $s: String) { findImages(image_filter: $f, filter: { per_page: $n, page: $p, sort: $s }) { images { ${FIELDS} } } }`,
        { f: imageFilter, n: perPage, p: page, s: sort }
      )).findImages.images;
    // Random order that stays the same from page to page (for Load more)
    const seededSort = () => "random_" + Math.floor(Math.random() * 1e8);

    const findPerformers = async (filter, perPage) =>
      (await gql(
        `query ($f: PerformerFilterType, $n: Int) { findPerformers(performer_filter: $f, filter: { per_page: $n, sort: "random" }) { performers { ${PERF_FIELDS} } } }`,
        { f: filter, n: perPage }
      )).findPerformers.performers;

    // Performers who look like the ones in the photo (and have photos):
    // Map performer id -> { name, k } where k is their best look-alike score
    async function lookAlikesOf(sources, skip) {
      const out = new Map();
      await Promise.all(sources.map(async (p) => {
        const base = { image_count: { value: 0, modifier: "GREATER_THAN" } };
        if (p.gender) base.gender = { value: p.gender, modifier: "EQUALS" };
        const with_ = (extra) => Object.assign({}, base, extra);
        const queries = [findPerformers(base, 200)];
        if (p.tags.length) queries.push(findPerformers(with_({ tags: { value: p.tags.map((t) => t.id), modifier: "INCLUDES", depth: 0 } }), 150));
        if (p.hair_color) queries.push(findPerformers(with_({ hair_color: { value: p.hair_color, modifier: "EQUALS" } }), 100));
        if (p.ethnicity) queries.push(findPerformers(with_({ ethnicity: { value: p.ethnicity, modifier: "EQUALS" } }), 100));
        const tagIds = new Set(p.tags.map((t) => t.id));
        for (const list of await Promise.all(queries)) {
          for (const x of list) {
            if (skip.has(x.id)) continue;
            const k = lookAlikePoints(p, x) + x.tags.filter((t) => tagIds.has(t.id)).length;
            if (k < LOOKALIKE_MIN) continue;
            const prev = out.get(x.id);
            if (!prev || k > prev.k) out.set(x.id, { name: x.name, k });
          }
        }
      }));
      return new Map([...out].sort((a, b) => b[1].k - a[1].k).slice(0, LOOKALIKES));
    }

    // Returns next(n): the next n photos by score, then the fill-ups.
    // Resolves to { items, done }.
    async function similarSource(id) {
      const image = (await gql(
        `query ($id: ID!) { findImage(id: $id) { id tags { id } galleries { id } performers { ${PERF_FIELDS} }
          visual_files { ... on ImageFile { width height } ... on VideoFile { width height } } } }`,
        { id }
      )).findImage;
      if (!image) return async () => ({ items: [], done: true });
      const tagIds = new Set(image.tags.map((t) => t.id));
      const perfIds = new Set(image.performers.map((p) => p.id));
      const galleryIds = new Set(image.galleries.map((g) => g.id));
      const seen = new Set([image.id]);
      // Same performer: that's what "More with <performer>" is for
      const samePerformer = (x) => x.performers.some((p) => perfIds.has(p.id));

      const lookAlikes = perfIds.size ? await lookAlikesOf(image.performers.slice(0, 3), perfIds) : new Map();

      // Everything sharing a tag or gallery, or with a look-alike in it
      const candidates = new Map();
      const lookups = [];
      if (tagIds.size) lookups.push(findImages({ tags: { value: [...tagIds], modifier: "INCLUDES", depth: 0 } }, CANDIDATES));
      if (galleryIds.size) lookups.push(findImages({ galleries: { value: [...galleryIds], modifier: "INCLUDES" } }, CANDIDATES));
      if (lookAlikes.size) lookups.push(findImages({ performers: { value: [...lookAlikes.keys()], modifier: "INCLUDES" } }, CANDIDATES));
      for (const list of await Promise.all(lookups)) {
        for (const x of list) if (x.id !== image.id && !samePerformer(x)) candidates.set(x.id, x);
      }

      // Every match, best first
      const scored = [...candidates.values()]
        .map((x) => {
          const tags = x.tags.filter((t) => tagIds.has(t.id)).length;
          const galleries = x.galleries.filter((g) => galleryIds.has(g.id)).length;
          let lookAlike = 0;
          for (const p of x.performers) lookAlike = Math.max(lookAlike, lookAlikes.get(p.id)?.k || 0);
          const lookPoints = Math.min(LOOKALIKE_MAX_POINTS, Math.round(lookAlike / 2));
          return { x, score: tags * TAG_POINTS + galleries * GALLERY_POINTS + lookPoints };
        })
        .filter((r) => r.score > 0)
        .map((r) => ({ x: r.x, k: r.score + Math.random() * 0.5 }))
        .sort((a, b) => b.k - a.k)
        .map((r) => r.x);
      scored.forEach((x) => seen.add(x.id));
      let at = 0;

      // After the matches: photos that haven't been organised yet (no
      // performers, tags or galleries), same shape first, then any photos
      const f = image.visual_files[0];
      const o = f && f.width && f.height
        ? (f.width / f.height > 1.05 ? "LANDSCAPE" : f.width / f.height < 0.95 ? "PORTRAIT" : "SQUARE")
        : null;
      const bare = {
        performer_count: { value: 0, modifier: "EQUALS" },
        tag_count: { value: 0, modifier: "EQUALS" },
        galleries: { modifier: "IS_NULL" },
      };
      const steps = [];
      if (o) steps.push({ ...bare, orientation: { value: [o] } });
      steps.push(bare);
      if (o) steps.push({ orientation: { value: [o] } });
      steps.push({});
      const sort = seededSort();
      const FILL_PAGE = 40;
      let step = 0, page = 1;
      const buffer = [];

      return async function next(n) {
        const items = scored.slice(at, at + n);
        at += items.length;
        while (items.length < n) {
          if (!buffer.length) {
            if (step >= steps.length) break;
            const list = await findImages(steps[step], FILL_PAGE, page, sort);
            if (list.length < FILL_PAGE) { step++; page = 1; } else page++;
            for (const x of list) {
              if (seen.has(x.id) || samePerformer(x)) continue;
              seen.add(x.id);
              buffer.push(x);
            }
            continue;
          }
          items.push(buffer.shift());
        }
        return { items, done: at >= scored.length && !buffer.length && step >= steps.length };
      };
    }

    // Remember the gallery you opened an image from. Opened from a row on
    // the home page: "home", and which row (its title, place and link).
    const FROM_HOME = "home";
    const FROM_KEY = "mui-from-gallery";
    const ROW_KEY = "mui-from-home-row";
    document.addEventListener("click", (e) => {
      const a = e.target instanceof Element && e.target.closest('a[href*="/images/"]');
      const row = a && a.closest(".recommendation-row");
      if (!row || window.location.pathname !== "/") return;
      const rows = [...document.querySelectorAll(".recommendation-row")];
      const info = {
        title: row.querySelector(".recommendation-row-head h2")?.textContent.trim() || "",
        index: rows.indexOf(row),
        imageRowIndex: rows.filter((r) => r.querySelector('a[href*="/images/"]')).indexOf(row),
        link: row.querySelector(".recommendation-row-head a")?.getAttribute("href") || "",
      };
      try { sessionStorage.setItem(ROW_KEY, JSON.stringify(info)); } catch (err) {}
    }, true);
    const fromRow = () => {
      try { return JSON.parse(sessionStorage.getItem(ROW_KEY) || "null"); } catch (e) { return null; }
    };
    const GALLERY_PAGE = /^\/galleries\/(\d+)/;
    let lastPath = window.location.pathname;
    function trackOrigin() {
      const path = window.location.pathname;
      if (path === lastPath) return;
      const from = lastPath.match(GALLERY_PAGE);
      try {
        if (IMAGE_PAGE.test(path)) {
          if (from) sessionStorage.setItem(FROM_KEY, from[1]);
          else if (lastPath === "/") sessionStorage.setItem(FROM_KEY, FROM_HOME);
        } else if (!GALLERY_PAGE.test(path)) {
          sessionStorage.removeItem(FROM_KEY);
          sessionStorage.removeItem(ROW_KEY);
        }
      } catch (e) {}
      lastPath = path;
    }
    const fromGallery = () => {
      try { return sessionStorage.getItem(FROM_KEY); } catch (e) { return null; }
    };

    // "More with <performer>": 12 random photos with the same performers
    async function performerBlock(imageId) {
      const image = (await gql(
        "query ($id: ID!) { findImage(id: $id) { id performers { id name } } }",
        { id: imageId }
      )).findImage;
      if (!image || !image.performers.length) return null;
      // Pages of 12 in a random order that stays put, for Load more
      const filter = { performers: { value: image.performers.map((p) => p.id), modifier: "INCLUDES" } };
      const sort = seededSort();
      let page = 0;
      const more = async () => {
        page++;
        const list = await findImages(filter, GALLERY_MAX, page, sort);
        return { items: list.filter((x) => x.id !== image.id), done: list.length < GALLERY_MAX };
      };
      const first = await more();
      const images = first.items;
      if (!images.length) return null;
      const names = image.performers.map((p) => p.name);
      const who = names.length === 1 ? names[0]
        : names.length === 2 ? names.join(" & ")
        : "these performers";
      return { title: "More with " + who, images, performers: image.performers, more: first.done ? null : more };
    }

    const GALLERY_MAX = 12;    // photos after the current one in the gallery row
    const GALLERY_BEFORE = 12; // photos before it (scroll left to see them)
    const GALLERY_SHOWN_BEFORE = 2; // ...of which this many are in view at first
    async function galleryBlock(imageId, galleryId) {
      const data = await gql(
        `query ($g: ID!, $f: ImageFilterType) {
          findGallery(id: $g) { id title files { basename } folder { path } }
          findImages(image_filter: $f, filter: { per_page: -1, sort: "path", direction: ASC }) { count images { id title paths { thumbnail } } }
        }`,
        { g: galleryId, f: { galleries: { value: [galleryId], modifier: "INCLUDES" } } }
      );
      const g = data.findGallery;
      const all = data.findImages.images;
      const at = all.findIndex((x) => x.id === imageId);
      if (!g || at === -1 || all.length < 2) return null; // not in that gallery, or nothing else in it
      const name = g.title || (g.files[0] && g.files[0].basename) ||
        (g.folder && g.folder.path.split(/[\\/]/).filter(Boolean).pop()) || "this gallery";
      // Photos before this one, this one, then the ones after it
      // (in gallery order, no wrapping round)
      let end = at + 1 + GALLERY_MAX;
      const images = all.slice(Math.max(0, at - GALLERY_BEFORE), end);
      // Load more: the next 12 in gallery order
      const more = async () => {
        const items = all.slice(end, end + GALLERY_MAX);
        end += items.length;
        return { items, done: end >= all.length };
      };
      return { title: "More from " + name, link: `/galleries/${g.id}`, linkText: `View gallery (${data.findImages.count})`,
        images, ids: all.map((x) => x.id), more: end < all.length ? more : null };
    }

    // ---- Home page rows: run the row's own filter ----
    // Saved filters keep their criteria in the format Stash's filter screen
    // uses; turn them into what the image search expects.
    const RESOLUTIONS = { "144p": "VERY_LOW", "240p": "LOW", "360p": "R360P", "480p": "STANDARD", "540p": "WEB_HD",
      "720p": "STANDARD_HD", "1080p": "FULL_HD", "1440p": "QUAD_HD", "4k": "FOUR_K", "5k": "FIVE_K", "6k": "SIX_K",
      "7k": "SEVEN_K", "8k": "EIGHT_K", huge: "HUGE" };
    const BOOLEANS = new Set(["organized", "performer_favorite"]);
    function toImageFilter(saved) {
      const out = {};
      for (const [key, c] of Object.entries(saved || {})) {
        if (!c || typeof c !== "object") continue;
        const { modifier, value: v } = c;
        if (BOOLEANS.has(key)) out[key] = String(v) === "true";
        else if (key === "is_missing") out[key] = v;
        else if (key === "orientation") out[key] = { value: [].concat(v || []).map((o) => String(o).toUpperCase()) };
        else if (key === "resolution") out[key] = { value: RESOLUTIONS[String(v).toLowerCase()] || v, modifier };
        else if (v && typeof v === "object" && Array.isArray(v.items)) {
          out[key] = { value: v.items.map((i) => i.id), modifier, depth: modifier === "EQUALS" ? 0 : v.depth || 0 };
          if (v.excluded && v.excluded.length) out[key].excludes = v.excluded.map((i) => i.id);
        } else if (Array.isArray(v) && v.every((i) => i && typeof i === "object" && "id" in i)) {
          out[key] = { value: v.map((i) => i.id), modifier };
        } else if (v && typeof v === "object" && "value" in v) {
          out[key] = { value: v.value, modifier };
          if (v.value2 !== undefined && v.value2 !== null) out[key].value2 = v.value2;
        } else {
          out[key] = v === undefined || v === null ? { value: "", modifier } : { value: v, modifier };
        }
      }
      return out;
    }

    // Which filter a home page row uses: { name, filter, find }
    let homeRowsCache = null;
    async function homeRowFilter(row) {
      if (!homeRowsCache) {
        homeRowsCache = (async () => {
          const ui = (await gql("{ configuration { ui } }")).configuration.ui || {};
          // No home page set up yet: Stash's default has one image row,
          // "Recently Added Images"
          const content = ui.frontPageContent ||
            [{ __typename: "CustomFilter", mode: "IMAGES", sortBy: "created_at", direction: "DESC" }];
          return Promise.all(content.map(async (c) => {
            if (c.__typename === "SavedFilter") {
              const f = (await gql(
                `query ($id: ID!) { findSavedFilter(id: $id) { name mode find_filter { q sort direction per_page } object_filter } }`,
                { id: String(c.savedFilterId) }
              )).findSavedFilter;
              return f && f.mode === "IMAGES"
                ? { name: f.name, filter: toImageFilter(f.object_filter), find: f.find_filter || {} } : null;
            }
            if (c.__typename === "CustomFilter" && c.mode === "IMAGES") {
              return { name: null, filter: {}, find: { sort: c.sortBy, direction: c.direction } };
            }
            return null;
          }));
        })().catch((e) => { homeRowsCache = null; throw e; });
      }
      const rows = await homeRowsCache;
      // By title first (saved filters), then by its place among image rows
      return rows.find((r) => r && r.name && r.name === row.title) ||
        rows.filter(Boolean)[row.imageRowIndex] || null;
    }

    const ROW_PAGE = 100;
    async function homeRowBlock(imageId, row) {
      if (!row) return null;
      const r = await homeRowFilter(row);
      if (!r) return null;
      // Same order as the row: its View All link has the exact sort (and
      // for random rows, the random seed the home page used)
      const q = new URLSearchParams((row.link || "").split("?")[1] || "");
      let sort = q.get("sortby") || r.find.sort || "created_at";
      if (sort === "random") sort = seededSort();
      const direction = (q.get("sortdir") || r.find.direction || "DESC").toUpperCase();
      const findPage = async (page) => (await gql(
        `query ($f: ImageFilterType, $ff: FindFilterType) { findImages(image_filter: $f, filter: $ff) { count images { id title paths { thumbnail } } } }`,
        { f: r.filter, ff: { q: r.find.q || undefined, sort, direction, per_page: ROW_PAGE, page } }
      )).findImages;

      // Load pages until this photo turns up (up to 5 pages)
      const all = [];
      let page = 0, total = 0, at = -1;
      while (at === -1 && page < 5) {
        const res = await findPage(++page);
        total = res.count;
        all.push(...res.images);
        at = all.findIndex((x) => x.id === imageId);
        if (res.images.length < ROW_PAGE) break;
      }
      if (all.length < 2) return null;
      const start = at === -1 ? 0 : Math.max(0, at - GALLERY_BEFORE);
      let end = (at === -1 ? 0 : at + 1) + GALLERY_MAX;
      const images = all.slice(start, end);
      const ids = all.map((x) => x.id);
      // Load more: the next 12 in the row's order, fetching more pages as needed
      const more = async () => {
        if (end + GALLERY_MAX > all.length && all.length < total) {
          const res = await findPage(++page);
          all.push(...res.images);
          ids.push(...res.images.map((x) => x.id));
        }
        const items = all.slice(end, end + GALLERY_MAX);
        end += items.length;
        return { items, done: end >= total || !items.length };
      };
      return { title: "More from " + (row.title || "this row"), link: row.link, linkText: `View all (${total})`,
        images, ids, more: end < total ? more : null };
    }

    const cardHtml = (x, currentId) =>
      `<a class="mui-similar-card${x.id === currentId ? " mui-current" : ""}" href="/images/${esc(x.id)}" data-id="${esc(x.id)}" title="${esc(x.title || "")}">` +
      `<img src="${esc(x.paths.thumbnail)}" alt="" loading="lazy" decoding="async"></a>`;

    const ARROWS = '<div class="mui-related-arrows">' +
      '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
      '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div>';

    // "Load more" card at the end of a row/grid; more() resolves to { items, done }
    function addLoadMore(grid, more, currentId) {
      if (!more) return;
      grid.insertAdjacentHTML("beforeend",
        '<button type="button" class="mui-load-more"><span>Load more</span></button>');
      const btn = grid.lastElementChild;
      const label = btn.querySelector("span");
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (btn.disabled) return;
        btn.disabled = true;
        label.textContent = "Loading…";
        try {
          const { items, done } = await more();
          btn.insertAdjacentHTML("beforebegin", items.map((x) => cardHtml(x, currentId)).join(""));
          // Keep the swipe order up to date with what's been loaded
          if (grid.dataset.ids) {
            const ids = JSON.parse(grid.dataset.ids);
            for (const x of items) if (!ids.includes(x.id)) ids.push(x.id);
            grid.dataset.ids = JSON.stringify(ids);
          }
          if (done || !items.length) {
            btn.remove();
          } else {
            btn.disabled = false;
            label.textContent = "Load more";
          }
        } catch (err) {
          console.warn("[Modern UI] could not load more photos", err);
          btn.disabled = false;
          label.textContent = "Try again";
        }
      });
    }

    function build(id) {
      const section = document.createElement("section");
      section.className = "mui-similar";
      section.dataset.image = id;
      const galleryId = fromGallery();
      section.dataset.gallery = galleryId || "";
      section.innerHTML =
        '<div class="mui-from-gallery"></div>' +
        `<div class="mui-similar-head mui-similar-tags-head"><h3>Similar photos</h3><div class="mui-similar-actions">${ARROWS}</div></div>` +
        '<div class="mui-similar-grid mui-similar-tags mui-similar-strip"><div class="mui-related-loading">Finding similar photos…</div></div>' +
        '<div class="mui-from-performer"></div>';
      const grid = section.querySelector(".mui-similar-tags");
      const galleryBox = section.querySelector(".mui-from-gallery");

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = arrow.closest(".mui-similar-head").nextElementSibling;
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mui-similar-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        // Swiping on the next page moves through the grid you picked from
        // (the gallery grid is already in gallery order)
        // (the gallery grid swipes through the whole gallery, not just the 12 shown)
        const fromGrid = a.closest(".mui-similar-grid");
        const ids = fromGrid.dataset.ids
          ? JSON.parse(fromGrid.dataset.ids)
          : [...fromGrid.querySelectorAll("a.mui-similar-card")].map((c) => c.dataset.id);
        if (!ids.includes(id)) ids.unshift(id);
        try { sessionStorage.setItem("mui-image-list", JSON.stringify(ids)); } catch (err) {}
        routerNavigate(a.getAttribute("href"));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      // From a gallery: that gallery. From the home page: the same filter
      // as the row you picked the photo from.
      if (galleryId) {
        (galleryId === FROM_HOME ? homeRowBlock(id, fromRow()) : galleryBlock(id, galleryId)).then((block) => {
          if (!block || section.dataset.image !== id || !section.isConnected) return;
          galleryBox.innerHTML =
            `<div class="mui-similar-head"><h3>${esc(block.title)}</h3>` +
            '<div class="mui-similar-actions">' +
            (block.link ? `<a class="mui-similar-all" href="${esc(block.link)}">${esc(block.linkText)}</a>` : "") +
            ARROWS + '</div></div>' +
            `<div class="mui-similar-grid mui-similar-gallery mui-similar-strip">${block.images.map((x) => cardHtml(x, id)).join("")}</div>`;
          const strip = galleryBox.querySelector(".mui-similar-gallery");
          strip.dataset.ids = JSON.stringify(block.ids);
          addLoadMore(strip, block.more, id);
          // Start scrolled so only the 2 photos before this one are in view
          const cards = [...strip.querySelectorAll("a.mui-similar-card")];
          const cur = cards.findIndex((c) => c.dataset.id === id);
          const first = cards[Math.max(0, cur - GALLERY_SHOWN_BEFORE)];
          if (cur > GALLERY_SHOWN_BEFORE && first) {
            strip.scrollLeft += first.getBoundingClientRect().left - strip.getBoundingClientRect().left;
          }
          galleryBox.querySelector(".mui-similar-all")?.addEventListener("click", (e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            routerNavigate(e.currentTarget.getAttribute("href"));
          });
        }).catch((e) => console.warn("[Modern UI] could not load the gallery / home row photos", e));
      }

      performerBlock(id).then((block) => {
        if (!block || section.dataset.image !== id || !section.isConnected) return;
        const box = section.querySelector(".mui-from-performer");
        // Link to the performer's page (only when there's just one)
        const link = block.performers.length === 1
          ? `<a class="mui-similar-all" href="/performers/${esc(block.performers[0].id)}">View performer</a>`
          : "";
        box.innerHTML =
          `<div class="mui-similar-head"><h3>${esc(block.title)}</h3><div class="mui-similar-actions">${link}${ARROWS}</div></div>` +
          `<div class="mui-similar-grid mui-similar-performer mui-similar-strip">${block.images.map((x) => cardHtml(x, null)).join("")}</div>`;
        addLoadMore(box.querySelector(".mui-similar-performer"), block.more, null);
        const all = box.querySelector(".mui-similar-all");
        if (all) all.addEventListener("click", (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          routerNavigate(e.currentTarget.getAttribute("href"));
        });
      }).catch((e) => console.warn("[Modern UI] could not load performer photos", e));

      similarSource(id).then(async (next) => {
        const { items, done } = await next(MAX);
        if (section.dataset.image !== id || !section.isConnected) return;
        grid.innerHTML = items.length
          ? items.map((x) => cardHtml(x, null)).join("")
          : '<div class="mui-related-loading">No similar photos yet.</div>';
        if (items.length && !done) addLoadMore(grid, () => next(MAX), null);
      }).catch((e) => {
        console.warn("[Modern UI] could not load similar photos", e);
        grid.innerHTML = '<div class="mui-related-loading">Couldn’t load similar photos.</div>';
      });
      return section;
    }

    function sync() {
      trackOrigin();
      const id = (window.location.pathname.match(IMAGE_PAGE) || [])[1];
      const existing = document.querySelector(".mui-similar");
      if (!id) {
        existing?.remove();
        return;
      }
      const row = document.querySelector(".image-container")?.parentElement;
      if (!row) return;
      if (existing && existing.dataset.image === id && existing.parentElement === row &&
          existing.dataset.gallery === (fromGallery() || "")) return;
      existing?.remove();
      row.appendChild(build(id));
    }

    let queued = false;
    new MutationObserver((mutations) => {
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mui-similar"))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("popstate", sync);
    sync();
  }

  // ------------------------------------------------------------------
  // "Similar Performers" row at the bottom of performer, scene and image
  // pages
  // ------------------------------------------------------------------
  // Same gender, then scored on shared tags, hair colour, ethnicity, eye
  // colour, country, breast type, height (within 5cm) and age (within 5
  // years). The 12 best are shown, closest match first. On a scene or
  // image it's based on the performers in it (up to 3), and leaves them out.

  function setupSimilarPerformers() {
    const PAGES = [
      { re: /^\/performers\/(\d+)/, kind: "performer", row: () => document.getElementById("performer-page") },
      { re: /^\/scenes\/(\d+)\/?$/, kind: "scene", row: () => document.querySelector(".scene-player-container")?.parentElement },
      { re: /^\/images\/(\d+)\/?$/, kind: "image", row: () => document.querySelector(".image-container")?.parentElement },
    ];
    const MAX = 12;
    const MAX_SOURCES = 3;
    const FIELDS =
      "id name disambiguation gender image_path hair_color eye_color ethnicity country height_cm birthdate fake_tits scene_count image_count gallery_count tags { id }";

    const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const gql = async (query, variables) => {
      const res = await fetch("/graphql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, variables }),
      });
      const json = await res.json();
      if (json.errors) throw new Error(json.errors[0].message);
      return json.data;
    };
    const findPerformers = async (filter, perPage) =>
      (await gql(
        `query ($f: PerformerFilterType, $n: Int) { findPerformers(performer_filter: $f, filter: { per_page: $n, sort: "random" }) { performers { ${FIELDS} } } }`,
        { f: filter, n: perPage }
      )).findPerformers.performers;

    // The performers to base it on
    async function sourcesFor(kind, id) {
      if (kind === "performer") return [id];
      const q = kind === "scene"
        ? "query ($id: ID!) { item: findScene(id: $id) { performers { id } } }"
        : "query ($id: ID!) { item: findImage(id: $id) { performers { id } } }";
      const item = (await gql(q, { id })).item;
      return item ? item.performers.map((p) => p.id) : [];
    }

    // Scores every candidate against one performer; returns Map id -> {x, k}
    async function scoresFor(p) {
      const base = p.gender ? { gender: { value: p.gender, modifier: "EQUALS" } } : {};
      const with_ = (extra) => Object.assign({}, base, extra);
      const queries = [findPerformers(base, 100)];
      if (p.tags.length) queries.push(findPerformers(with_({ tags: { value: p.tags.map((t) => t.id), modifier: "INCLUDES", depth: 0 } }), 150));
      if (p.hair_color) queries.push(findPerformers(with_({ hair_color: { value: p.hair_color, modifier: "EQUALS" } }), 100));
      if (p.ethnicity) queries.push(findPerformers(with_({ ethnicity: { value: p.ethnicity, modifier: "EQUALS" } }), 100));

      const tagIds = new Set(p.tags.map((t) => t.id));
      const score = (x) => {
        let k = 2 * x.tags.filter((t) => tagIds.has(t.id)).length;
        k += lookAlikePoints(p, x);
        if (x.gender === "FEMALE") k += 2;
        return k;
      };

      const out = new Map();
      for (const list of await Promise.all(queries)) {
        for (const x of list) {
          if (out.has(x.id)) continue;
          if (!x.scene_count && !x.image_count && !x.gallery_count) continue; // nothing to see
          out.set(x.id, { x, k: score(x) });
        }
      }
      return out;
    }

    async function similarTo(kind, id) {
      const ids = (await sourcesFor(kind, id)).slice(0, MAX_SOURCES);
      if (!ids.length) return [];
      const sources = (await gql(
        `query ($ids: [ID!]) { findPerformers(ids: $ids, filter: { per_page: -1 }) { performers { ${FIELDS} } } }`,
        { ids }
      )).findPerformers.performers;

      // Best score against any of the source performers
      const best = new Map();
      for (const scores of await Promise.all(sources.map(scoresFor))) {
        for (const [pid, r] of scores) {
          const prev = best.get(pid);
          if (!prev || r.k > prev.k) best.set(pid, r);
        }
      }
      const skip = new Set(ids);
      return [...best.values()]
        .filter((r) => !skip.has(r.x.id))
        .map((r) => ({ x: r.x, k: r.k + Math.random() * 0.5 })) // mix up equal matches
        .sort((a, b) => b.k - a.k)
        .slice(0, MAX)
        .map((r) => r.x)
        // Women first, then everyone else; closest match first within each
        .sort((a, b) => (b.gender === "FEMALE") - (a.gender === "FEMALE"));
    }

    function card(x) {
      const meta = [
        x.scene_count ? `${x.scene_count} scene${x.scene_count === 1 ? "" : "s"}` : "",
        x.image_count ? `${x.image_count} image${x.image_count === 1 ? "" : "s"}` : "",
      ].filter(Boolean).join(" · ");
      const name = x.name + (x.disambiguation ? ` (${x.disambiguation})` : "");
      return (
        `<a class="mui-rel-card mui-perf-card" href="/performers/${esc(x.id)}">` +
        `<div class="mui-rel-thumb">` +
        (x.image_path ? `<img src="${esc(x.image_path)}" alt="" loading="lazy" decoding="async">` : "") +
        `</div>` +
        `<div class="mui-rel-title" title="${esc(name)}">${esc(name)}</div>` +
        (meta ? `<div class="mui-rel-meta">${esc(meta)}</div>` : "") +
        `</a>`
      );
    }

    function build(kind, id) {
      const key = kind + ":" + id;
      const section = document.createElement("section");
      section.className = "mui-perf-similar mui-perf-similar-" + kind;
      section.dataset.key = key;

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = section.querySelector(".mui-related-strip");
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mui-perf-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        routerNavigate(a.getAttribute("href"));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      similarTo(kind, id).then((list) => {
        if (section.dataset.key !== key || !section.isConnected || !list.length) return;
        section.innerHTML =
          '<div class="mui-related-row">' +
          '<div class="mui-related-head"><h3>Similar Performers</h3>' +
          '<div class="mui-related-arrows">' +
          '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
          '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div></div>' +
          `<div class="mui-related-strip">${list.map(card).join("")}</div>` +
          "</div>";
      }).catch((e) => console.warn("[Modern UI] could not load similar performers", e));
      return section;
    }

    function sync() {
      const path = window.location.pathname;
      const page = PAGES.find((pg) => pg.re.test(path));
      const existing = document.querySelector(".mui-perf-similar");
      const row = page && page.row();
      if (!page || !row) {
        existing?.remove();
        return;
      }
      const id = path.match(page.re)[1];
      if (existing && existing.dataset.key === page.kind + ":" + id && existing.parentElement === row) return;
      existing?.remove();
      row.appendChild(build(page.kind, id));
    }

    let queued = false;
    new MutationObserver((mutations) => {
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mui-perf-similar"))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("popstate", sync);
    sync();
  }

  // Mark when an edit form is open (the Edit tab on scene, image and gallery
  // pages, or edit mode on a performer page) so the Suggested / Similar
  // sections can be hidden and don't get mixed in with the form
  function setupEditingFlag() {
    const EDITING = '.nav-link.active[data-rb-event-key$="-edit-panel"], #performer-page .detail-header.edit';
    const update = () => body.toggle("mui-editing", !!document.querySelector(EDITING));
    let queued = false;
    new MutationObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        update();
      });
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
    update();
  }

  // Mark the home page so the "Stash" logo button only lights up there
  function setupHomeFlag() {
    const update = () => body.toggle("mui-on-home", window.location.pathname === "/");
    update();
    for (const fn of ["pushState", "replaceState"]) {
      const orig = history[fn];
      history[fn] = function () {
        const r = orig.apply(this, arguments);
        update();
        return r;
      };
    }
    window.addEventListener("popstate", update);
  }

  // Go to a page through Stash's router (no full page reload)
  function routerNavigate(path) {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  // ------------------------------------------------------------------
  // Image page: swipe (or arrow keys) to the next / previous image
  // ------------------------------------------------------------------
  // While you browse a list of images (Images page, a gallery, a filtered
  // list...), we remember the order of the images shown. On a single
  // image's page, swiping moves through that list.

  function setupImageSwipe() {
    const STORE_KEY = "mui-image-list";
    const IMAGE_PAGE = /^\/images\/(\d+)\/?$/;
    const SWIPE_MIN = 60; // px

    const currentImageId = () => (window.location.pathname.match(IMAGE_PAGE) || [])[1] || null;

    function rememberList() {
      if (currentImageId()) return; // don't overwrite while viewing an image
      const ids = [];
      document.querySelectorAll('.image-card a[href*="/images/"]').forEach((a) => {
        const m = a.getAttribute("href").match(/\/images\/(\d+)/);
        if (m && !ids.includes(m[1])) ids.push(m[1]);
      });
      if (ids.length) sessionStorage.setItem(STORE_KEY, JSON.stringify(ids));
    }

    function neighbour(step) {
      const id = currentImageId();
      if (!id) return null;
      let ids = [];
      try {
        ids = JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]");
      } catch (e) {}
      const i = ids.indexOf(id);
      if (i === -1) return null;
      return ids[i + step] || null;
    }

    function go(step) {
      const next = neighbour(step);
      if (next) routerNavigate(`/images/${next}`);
      return !!next;
    }

    let timer = null;
    new MutationObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(rememberList, 300);
    }).observe(document.body, { childList: true, subtree: true });

    // The big image/video on the image page (not thumbnails or icons)
    function mainMedia(target) {
      if (!currentImageId() || !(target instanceof Element)) return null;
      if (target.closest(".grid-card, .mui-related, .mui-similar, .mui-perf-similar, .top-nav, .mui-bottom-nav, .mui-sheet, .modal, " + LIGHTBOX)) return null;
      const media = target.closest("img, video") || target.querySelector?.("img, video");
      if (!media || media.getBoundingClientRect().width < 150) return null;
      return media;
    }

    let media = null, startX = 0, startY = 0, startT = 0, dx = 0, dir = null;

    const reset = (el, animate) => {
      if (!el) return;
      el.style.transition = animate ? "transform 0.2s ease, opacity 0.2s ease" : "";
      el.style.transform = "";
      el.style.opacity = "";
    };

    window.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) { media = null; return; } // pinch-zoom: leave alone
      media = mainMedia(e.target);
      if (!media) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      startT = performance.now();
      dx = 0;
      dir = null;
      media.style.transition = "";
    }, { capture: true, passive: true });

    window.addEventListener("touchmove", (e) => {
      if (!media) return;
      if (e.touches.length !== 1) { reset(media, true); media = null; return; }
      const t = e.touches[0];
      dx = t.clientX - startX;
      const dy = t.clientY - startY;
      if (!dir) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        dir = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      }
      if (dir !== "x") return;
      // Image is zoomed in: let the user pan it instead
      if (window.visualViewport && window.visualViewport.scale > 1.05) return;
      e.preventDefault();
      const edge = !neighbour(dx < 0 ? 1 : -1);
      const shown = edge ? dx * 0.25 : dx; // rubber-band at the ends of the list
      media.style.transform = `translateX(${shown}px)`;
      media.style.opacity = String(1 - Math.min(Math.abs(shown) / 600, 0.4));
    }, { capture: true, passive: false });

    const end = () => {
      if (!media) return;
      const el = media;
      media = null;
      const fast = Math.abs(dx) / Math.max(performance.now() - startT, 1) > 0.5;
      if (dir === "x" && (Math.abs(dx) > SWIPE_MIN || (fast && Math.abs(dx) > 25))) {
        const step = dx < 0 ? 1 : -1;
        if (neighbour(step)) {
          el.style.transition = "transform 0.15s ease, opacity 0.15s ease";
          el.style.transform = `translateX(${dx < 0 ? -window.innerWidth : window.innerWidth}px)`;
          el.style.opacity = "0";
          setTimeout(() => {
            go(step);
            setTimeout(() => reset(el, false), 50);
          }, 150);
          return;
        }
      }
      reset(el, true);
    };
    window.addEventListener("touchend", end, { capture: true, passive: true });
    window.addEventListener("touchcancel", end, { capture: true, passive: true });

    // ---- Stash's full-screen viewer (Wall view, galleries) ----
    // The viewer already knows next/previous; we just trigger it on swipe.
    const LIGHTBOX = ".Lightbox, [class*='Lightbox']";

    function lightboxStep(step) {
      const box = document.querySelector(LIGHTBOX);
      if (!box) return;
      // 1. The viewer's own arrow buttons (hidden on phones, but still present)
      const buttons = [...box.querySelectorAll("button, .btn")].filter((b) =>
        /navbutton|nav-button|Lightbox-nav|left|right|prev|next/i.test(b.className)
      );
      const prev = buttons.find((b) => /left|prev/i.test(b.className)) || buttons[0];
      const next = buttons.find((b) => /right|next/i.test(b.className)) || buttons[buttons.length - 1];
      const btn = step > 0 ? next : prev;
      if (buttons.length >= 2 && btn) {
        btn.click();
        return;
      }
      // 2. Fall back to the viewer's keyboard shortcuts
      const key = step > 0 ? "ArrowRight" : "ArrowLeft";
      const code = step > 0 ? 39 : 37;
      for (const target of [document, window]) {
        const ev = new KeyboardEvent("keydown", { key, code: key, bubbles: true, cancelable: true });
        Object.defineProperty(ev, "keyCode", { get: () => code });
        Object.defineProperty(ev, "which", { get: () => code });
        target.dispatchEvent(ev);
        if (ev.defaultPrevented) break;
      }
    }

    let lb = null; // { x, y, t }
    window.addEventListener("touchstart", (e) => {
      lb = null;
      if (e.touches.length !== 1) return; // pinch-zoom: leave alone
      if (!(e.target instanceof Element) || !e.target.closest(LIGHTBOX)) return;
      // Ignore the viewer's toolbar / footer buttons
      if (e.target.closest("button, .btn, input, a, [class*='footer'], [class*='header']")) return;
      lb = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: performance.now() };
    }, { capture: true, passive: true });

    window.addEventListener("touchmove", (e) => {
      if (lb && e.touches.length !== 1) lb = null;
    }, { capture: true, passive: true });

    window.addEventListener("touchend", (e) => {
      if (!lb) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - lb.x;
      const dy = t.clientY - lb.y;
      const fast = Math.abs(dx) / Math.max(performance.now() - lb.t, 1) > 0.5;
      lb = null;
      if (window.visualViewport && window.visualViewport.scale > 1.05) return;
      if (Math.abs(dx) < Math.abs(dy) * 1.5) return; // mostly vertical
      if (Math.abs(dx) > SWIPE_MIN || (fast && Math.abs(dx) > 25)) {
        lightboxStep(dx < 0 ? 1 : -1);
      }
    }, { capture: true, passive: true });

    // Desktop: left/right arrow keys
    window.addEventListener("keydown", (e) => {
      if (!currentImageId() || e.altKey || e.ctrlKey || e.metaKey) return;
      const el = document.activeElement;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      if (document.querySelector(".modal.show, " + LIGHTBOX)) return; // Stash's own viewer handles keys
      if (e.key === "ArrowRight" && go(1)) e.preventDefault();
      if (e.key === "ArrowLeft" && go(-1)) e.preventDefault();
    });
  }

  // ------------------------------------------------------------------
  // Home page rows: native scrolling
  // ------------------------------------------------------------------
  // Stash's home page rows use a carousel library that moves one card per
  // swipe with a fixed animation. CSS turns each row into a plain scrollable
  // strip, and here we just keep the carousel's own touch handling out of the
  // way. The phone's browser then does the dragging and the momentum glide
  // itself, which is smoother (and more reliable on iPhone) than doing it in
  // JavaScript.

  function setupNativeCarouselScroll() {
    const SELECTOR = ".slick-slider .slick-list";
    const inList = (e) => e.target instanceof Element && e.target.closest(SELECTOR);

    for (const type of ["touchstart", "touchmove", "touchend", "touchcancel"]) {
      window.addEventListener(type, (e) => {
        if (inList(e)) e.stopPropagation(); // carousel never sees the swipe
      }, { capture: true, passive: true });
    }

    window.addEventListener("mousedown", (e) => {
      if (inList(e)) e.stopPropagation();
    }, { capture: true, passive: true });

    // Row arrows: scroll the strip ourselves instead of the carousel library
    window.addEventListener("click", (e) => {
      const arrow = e.target instanceof Element && e.target.closest(".slick-slider .slick-arrow");
      if (!arrow) return;
      e.preventDefault();
      e.stopPropagation();
      const slider = arrow.closest(".slick-slider");
      const el = slider && slider.querySelector(".slick-list");
      if (!el) return;
      const dir = arrow.classList.contains("slick-prev") ? -1 : 1;
      el.scrollBy({ left: dir * el.clientWidth * 0.8, behavior: "smooth" });
    }, { capture: true });

    // Fade an arrow out when its end of the row is reached
    function updateEdges(el) {
      const slider = el.closest(".slick-slider");
      if (!slider) return;
      const max = el.scrollWidth - el.clientWidth;
      slider.classList.toggle("mui-at-start", el.scrollLeft <= 2);
      slider.classList.toggle("mui-at-end", el.scrollLeft >= max - 2);
    }
    document.addEventListener("scroll", (e) => {
      if (e.target instanceof Element && e.target.matches(SELECTOR)) updateEdges(e.target);
    }, { capture: true, passive: true });

    const refreshAll = () =>
      document.querySelectorAll(SELECTOR).forEach((el) => {
        updateEdges(el);
        // Decode pictures off the main thread so scrolling doesn't hitch
        el.querySelectorAll("img:not([decoding])").forEach((img) => (img.decoding = "async"));
      });
    let pending = false;
    new MutationObserver(() => {
      if (pending) return;
      pending = true;
      setTimeout(() => { pending = false; refreshAll(); }, 250);
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", refreshAll);
    // Images load after the row appears and change its width
    window.addEventListener("load", refreshAll, true);
  }

  // ------------------------------------------------------------------
  // Mobile bottom tab bar
  // ------------------------------------------------------------------
  // The pages come from Stash's own menu, so pages added by other plugins
  // (e.g. Stash TV) show up too, and pages hidden in Stash's settings stay
  // hidden. Home and Settings are always available. The order can be
  // changed from the More panel's Edit button; it's saved to this plugin's
  // settings (bottomNavItems / moreNavOrder).

  function setupBottomNav(settings) {
    const ICONS = {
      home: '<path d="M12 3 2.5 11H5v9a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1v-9h2.5L12 3Z"/>',
      scenes: '<path d="M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Zm6 4v6l5-3-5-3Z"/>',
      images: '<path d="M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm1 13h14l-4.5-6-3.5 4.5-2.5-3L5 17Zm3-8.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z"/>',
      performers: '<path d="M12 12a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9Zm0 2c-4.4 0-8 2.2-8 5v1h16v-1c0-2.8-3.6-5-8-5Z"/>',
      galleries: '<path d="M7 3h13a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm1 12h11l-3.5-4.5-2.5 3-2-2.5L8 15ZM3 7h1.5v12.5H17V21H4a1 1 0 0 1-1-1V7Z"/>',
      studios: '<path d="M3 21V8l6-3v4l6-3v4l6-3v14H3Zm4-3h2v-3H7v3Zm4 0h2v-3h-2v3Zm4 0h2v-3h-2v3Z"/>',
      groups: '<path d="M4 4h16v4H4V4Zm0 6h16v10H4V10Zm2 2v2h2v-2H6Zm0 4v2h2v-2H6Zm10-4v2h2v-2h-2Zm0 4v2h2v-2h-2Z"/>',
      tags: '<path d="M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9-9-9Zm4.5-3.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z"/>',
      markers: '<path d="M6 3h12v18l-6-4-6 4V3Z"/>',
      settings: '<path d="M19.4 13a7.6 7.6 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3h-4l-.4 2.9a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.6 7.6 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1L11 21h4l.4-2.9a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5-2.2-1.6ZM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z"/>',
      more: '<path d="M5 10a2 2 0 1 1 0 4 2 2 0 0 1 0-4Zm7 0a2 2 0 1 1 0 4 2 2 0 0 1 0-4Zm7 0a2 2 0 1 1 0 4 2 2 0 0 1 0-4Z"/>',
      page: '<path d="M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z"/>',
      up: '<path d="m12 7-6 6 1.4 1.4L12 9.8l4.6 4.6L18 13l-6-6Z"/>',
      down: '<path d="m12 17 6-6-1.4-1.4-4.6 4.6-4.6-4.6L6 11l6 6Z"/>',
      add: '<path d="M11 5h2v6h6v2h-6v6h-2v-6H5v-2h6V5Z"/>',
      remove: '<path d="M5 11h14v2H5v-2Z"/>',
    };
    const DEFAULT_TABS = ["home", "scenes", "images", "performers"];
    const MAX_TABS = 4;

    const parseList = (v) =>
      String(v || "").split(",").map((x) => x.trim().toLowerCase()).filter(Boolean);
    let tabOrder = parseList(settings.bottomNavItems);
    if (!tabOrder.length) tabOrder = DEFAULT_TABS.slice();
    let moreOrder = parseList(settings.moreNavOrder);

    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const slug = (t) => String(t).trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    const icon = (key) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">${ICONS[key]}</svg>`;

    // Every page we can link to, in Stash's menu order
    function scanPages() {
      const pages = [{ key: "home", label: "Home", href: "/", svg: icon("home") }];
      const seen = new Set(["home", "settings"]);
      document.querySelectorAll(".top-nav .navbar-collapse .navbar-nav .nav-link").forEach((item) => {
        const target = item.querySelector("a[href]") || (item.matches("a[href]") ? item : null) || item.querySelector("button, .btn");
        if (!target) return;
        const label = (target.textContent || item.textContent || "").trim();
        if (!label) return;
        const href = target.getAttribute("href") || null;
        const m = href && href.match(/^\/([a-z]+)\/?$/);
        let key = m && ICONS[m[1]] ? m[1] : slug(label);
        if (!key || seen.has(key)) return;
        seen.add(key);
        const theirIcon = item.querySelector("svg");
        let svg = ICONS[key] ? icon(key) : null;
        if (!svg && theirIcon) {
          const copy = theirIcon.cloneNode(true);
          copy.removeAttribute("class");
          copy.setAttribute("aria-hidden", "true");
          svg = copy.outerHTML;
        }
        pages.push({
          key, label, href, svg: svg || icon("page"),
          external: target.getAttribute("target") === "_blank" || (href && !href.startsWith("/")),
        });
      });
      pages.push({ key: "settings", label: "Settings", href: "/settings", svg: icon("settings") });
      return pages;
    }

    // Split the pages into tab bar + More panel using the saved order
    function arrange(pages, tabs, more) {
      const byKey = new Map(pages.map((p) => [p.key, p]));
      const tab = tabs.filter((k) => byKey.has(k)).slice(0, MAX_TABS).map((k) => byKey.get(k));
      const used = new Set(tab.map((p) => p.key));
      const rest = more.filter((k) => byKey.has(k) && !used.has(k)).map((k) => byKey.get(k));
      rest.forEach((p) => used.add(p.key));
      // Pages not mentioned anywhere (new plugins etc.) go to the end, Settings last
      const extra = pages.filter((p) => !used.has(p.key));
      const settingsPage = extra.find((p) => p.key === "settings");
      const others = extra.filter((p) => p.key !== "settings");
      return { tab, more: rest.concat(others, settingsPage ? [settingsPage] : []) };
    }

    const nav = document.createElement("nav");
    nav.className = "mui-bottom-nav";
    nav.setAttribute("aria-label", "Main");
    const backdrop = document.createElement("div");
    backdrop.className = "mui-sheet-backdrop";
    const sheet = document.createElement("div");
    sheet.className = "mui-sheet";
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-label", "More pages");
    document.body.append(nav, backdrop, sheet);
    body.add("mui-has-bottom-nav");

    let pages = [];
    let current = { tab: [], more: [] };
    let draft = null; // { tab: [keys], more: [keys] } while editing
    let saving = false;
    let lastSignature = "";

    const link = (p) =>
      `<a href="${esc(p.href || "#")}" data-key="${esc(p.key)}">${p.svg}<span>${esc(p.label)}</span></a>`;

    function renderNav() {
      nav.innerHTML =
        current.tab.map(link).join("") +
        `<button type="button" data-key="more">${icon("more")}<span>More</span></button>`;
    }

    function renderSheet() {
      if (!draft) {
        sheet.innerHTML =
          '<div class="mui-sheet-handle"></div>' +
          `<div class="mui-sheet-grid">${current.more.map(link).join("")}</div>` +
          '<button type="button" class="mui-sheet-edit">Edit tab bar</button>';
        return;
      }
      const byKey = new Map(pages.map((p) => [p.key, p]));
      const row = (key, i, list, inTab) => {
        const p = byKey.get(key);
        const full = !inTab && draft.tab.length >= MAX_TABS;
        return (
          `<div class="mui-edit-row" data-key="${esc(key)}">` +
          `<span class="mui-edit-icon">${p.svg}</span><span class="mui-edit-label">${esc(p.label)}</span>` +
          `<button type="button" data-act="up" aria-label="Move up" ${i === 0 ? "disabled" : ""}>${icon("up")}</button>` +
          `<button type="button" data-act="down" aria-label="Move down" ${i === list.length - 1 ? "disabled" : ""}>${icon("down")}</button>` +
          `<button type="button" data-act="${inTab ? "remove" : "add"}" class="mui-edit-toggle" ` +
          `aria-label="${inTab ? "Move to More" : "Add to tab bar"}" ${full ? "disabled" : ""}>${icon(inTab ? "remove" : "add")}</button>` +
          `</div>`
        );
      };
      sheet.innerHTML =
        '<div class="mui-sheet-handle"></div>' +
        `<div class="mui-edit-heading">Tab bar <small>${draft.tab.length} of ${MAX_TABS}</small></div>` +
        `<div class="mui-edit-list">${draft.tab.map((k, i, l) => row(k, i, l, true)).join("") ||
          '<div class="mui-edit-empty">Nothing here: only More will show</div>'}</div>` +
        '<div class="mui-edit-heading">More</div>' +
        `<div class="mui-edit-list">${draft.more.map((k, i, l) => row(k, i, l, false)).join("")}</div>` +
        '<div class="mui-edit-actions">' +
        '<button type="button" data-act="reset">Reset</button>' +
        `<button type="button" data-act="done" class="mui-edit-done">${saving ? "Saving…" : "Done"}</button>` +
        "</div>";
    }

    function refresh(force) {
      const scanned = scanPages();
      const signature = scanned.map((p) => p.key + ":" + p.label).join("|");
      if (!force && signature === lastSignature) return;
      lastSignature = signature;
      pages = scanned;
      current = arrange(pages, tabOrder, moreOrder);
      renderNav();
      if (!draft) renderSheet();
      updateActive();
    }

    function sheetOpen() {
      return body.contains("mui-sheet-open");
    }
    function setSheet(open) {
      body.toggle("mui-sheet-open", open);
      if (!open && draft) {
        draft = null; // closing discards unsaved changes
        renderSheet();
      }
      updateActive();
    }

    // Open a page: through Stash's own menu item when there is one, so
    // plugin pages behave exactly as they do on desktop.
    function open(key) {
      setSheet(false);
      const page = scanPages().find((p) => p.key === key);
      if (!page) return;
      if (key !== "home" && key !== "settings") {
        for (const item of document.querySelectorAll(".top-nav .navbar-collapse .navbar-nav .nav-link")) {
          const target = item.querySelector("a[href]") || item.querySelector("button, .btn") || item;
          const label = (target.textContent || "").trim();
          if (label === page.label) {
            target.click();
            return;
          }
        }
      }
      if (page.external && page.href) {
        window.open(page.href, "_blank");
      } else if (page.href) {
        const existing = document.querySelector(`.top-nav a[href="${page.href}"]`);
        if (existing) existing.click();
        else routerNavigate(page.href);
      }
    }

    async function save() {
      saving = true;
      renderSheet();
      tabOrder = draft.tab.slice();
      moreOrder = draft.more.slice();
      try {
        const gql = async (query, variables) => {
          const res = await fetch("/graphql", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, variables }),
          });
          const json = await res.json();
          if (json.errors) throw new Error(json.errors[0].message);
          return json.data;
        };
        // Re-read first: Stash replaces the whole settings object on save
        const latest = ((await gql("{ configuration { plugins } }")).configuration.plugins || {})[PLUGIN_ID] || {};
        const input = { ...latest, bottomNavItems: tabOrder.join(","), moreNavOrder: moreOrder.join(",") };
        await gql("mutation ($input: Map!) { configurePlugin(plugin_id: \"" + PLUGIN_ID + "\", input: $input) }", { input });
      } catch (e) {
        console.warn("[Modern UI] could not save the tab bar order", e);
        alert("Couldn't save the tab bar order. It will apply until you reload the page.");
      }
      saving = false;
      draft = null;
      refresh(true);
    }

    nav.addEventListener("click", (e) => {
      const el = e.target.closest("[data-key]");
      if (!el) return;
      e.preventDefault();
      if (el.dataset.key === "more") setSheet(!sheetOpen());
      else open(el.dataset.key);
    });

    sheet.addEventListener("click", (e) => {
      if (e.target.closest(".mui-sheet-edit")) {
        draft = { tab: current.tab.map((p) => p.key), more: current.more.map((p) => p.key) };
        renderSheet();
        return;
      }
      const actBtn = e.target.closest("button[data-act]");
      if (actBtn && draft) {
        if (saving) return;
        const act = actBtn.dataset.act;
        if (act === "done") return void save();
        if (act === "reset") {
          const def = arrange(pages, DEFAULT_TABS, []);
          draft = { tab: def.tab.map((p) => p.key), more: def.more.map((p) => p.key) };
          return void renderSheet();
        }
        const key = actBtn.closest(".mui-edit-row").dataset.key;
        const list = draft.tab.includes(key) ? draft.tab : draft.more;
        const i = list.indexOf(key);
        if (act === "up" && i > 0) [list[i - 1], list[i]] = [list[i], list[i - 1]];
        if (act === "down" && i < list.length - 1) [list[i + 1], list[i]] = [list[i], list[i + 1]];
        if (act === "remove") {
          draft.tab.splice(i, 1);
          draft.more.unshift(key);
        }
        if (act === "add" && draft.tab.length < MAX_TABS) {
          draft.more.splice(i, 1);
          draft.tab.push(key);
        }
        renderSheet();
        return;
      }
      const el = e.target.closest("a[data-key]");
      if (!el || draft) return;
      e.preventDefault();
      open(el.dataset.key);
    });

    backdrop.addEventListener("click", () => setSheet(false));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && sheetOpen()) setSheet(false);
    });

    // Swipe the sheet down to close it (only when it's scrolled to the top)
    let startY = null;
    sheet.addEventListener("touchstart", (e) => {
      startY = sheet.scrollTop <= 0 ? e.touches[0].clientY : null;
    }, { passive: true });
    sheet.addEventListener("touchend", (e) => {
      if (startY !== null && e.changedTouches[0].clientY - startY > 60) setSheet(false);
      startY = null;
    });

    function updateActive() {
      const path = window.location.pathname;
      const byKey = new Map(pages.map((p) => [p.key, p]));
      [...nav.querySelectorAll("a[data-key]"), ...sheet.querySelectorAll("a[data-key]")].forEach((a) => {
        const p = byKey.get(a.dataset.key);
        const href = p && !p.external ? p.href : null;
        const on = !!href && (href === "/" ? path === "/" : path === href || path.startsWith(href.replace(/\/$/, "") + "/"));
        a.classList.toggle("active", on);
      });
      nav.querySelector('[data-key="more"]')?.classList.toggle("active", sheetOpen());
    }

    // Stash is a single-page app: watch for route changes.
    for (const fn of ["pushState", "replaceState"]) {
      const orig = history[fn];
      history[fn] = function () {
        const r = orig.apply(this, arguments);
        window.dispatchEvent(new Event("mui:locationchange"));
        return r;
      };
    }
    window.addEventListener("popstate", updateActive);
    window.addEventListener("mui:locationchange", updateActive);

    // Lift the Floating Scene Player button above the tab bar on phones.
    const FSP_IDS = ["fsp-floating-scene-player-launch-btn", "fsp-floating-scene-player-container"];
    function liftFloatingPlayer() {
      for (const id of FSP_IDS) {
        const el = document.getElementById(id);
        if (!el) continue;
        if (MOBILE_QUERY.matches) {
          el.style.setProperty("bottom", "calc(76px + env(safe-area-inset-bottom))", "important");
        } else if (el.style.getPropertyValue("bottom").startsWith("calc(76px")) {
          el.style.setProperty("bottom", "20px", id.endsWith("btn") ? "important" : "");
        }
      }
    }

    // Plugins add their menu items after the page loads: keep re-checking
    let queued = false;
    new MutationObserver((mutations) => {
      if (queued || mutations.every((m) => nav.contains(m.target) || sheet.contains(m.target))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        refresh(false);
        updateActive();
        liftFloatingPlayer();
      });
    }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });

    MOBILE_QUERY.addEventListener("change", liftFloatingPlayer);
    refresh(true);
    liftFloatingPlayer();
  }
})();
