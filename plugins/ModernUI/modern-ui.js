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

  // Before anything is awaited: Stash shows the page once plugin scripts have
  // loaded, so a list sorted by Count can ask for its items while the
  // settings below are still loading. The Count sort has to be ready by then.
  setupCountSort();

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
  setupLongPressSelect();
  setupGalleryTitle();
  setupTagIndex();
  if (!cfg.showGalleryDetails) setupGalleryPanelClosed();

  // the full-screen viewer swipes between images too, unless that's off
  const imageNav = cfg.disableImageSwipe ? null : setupImageSwipe();
  setupImageViewer(imageNav);

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
  // ticks it, and sliding the finger on ticks the cards it passes; once
  // anything is selected, Stash itself makes a normal tap
  // add/remove cards, and its toolbar shows Edit / Delete for the selection.

  function setupLongPressSelect() {
    const HOLD_MS = 450;
    const MOVE_TOLERANCE = 10; // px
    const EDGE = 70; // px from the top / bottom where dragging scrolls the page
    let timer = null, start = null, fired = false, suppressUntil = 0;
    // After a hold, sliding the finger selects every card from the held one
    // to the one under the finger (or unselects, if the held one was
    // selected). Sliding back puts the ones left behind back as they were.
    let drag = null; // { cards, from, select, changed: Set, x, y, raf }

    const cancel = () => {
      clearTimeout(timer);
      timer = null;
    };

    const checkOf = (card) => card.querySelector(".card-check");

    function cardAt(x, y) {
      // a finger on a bar counts as just inside it
      const { top, bottom } = visibleArea();
      y = Math.min(bottom - 4, Math.max(top + 4, y));
      const el = document.elementFromPoint(x, y);
      const card = el && el.closest(".grid-card");
      return card && drag.cards.includes(card) ? card : null;
    }

    function setCard(card, on) {
      const check = checkOf(card);
      if (check && check.checked !== on) check.click();
    }

    function dragTo(x, y) {
      const card = cardAt(x, y);
      if (!card) return;
      const to = drag.cards.indexOf(card);
      const lo = Math.min(drag.from, to), hi = Math.max(drag.from, to);
      // cards no longer in the range go back to how they were
      [...drag.changed].forEach((c) => {
        const i = drag.cards.indexOf(c);
        if (i < lo || i > hi) {
          setCard(c, !drag.select);
          drag.changed.delete(c);
        }
      });
      for (let i = lo; i <= hi; i++) {
        const c = drag.cards[i];
        const check = checkOf(c);
        if (check && check.checked !== drag.select) {
          check.click();
          drag.changed.add(c);
        }
      }
    }

    // The part of the screen between the bars at the top and bottom (Stash's
    // navbar is at the bottom on phones, as is our tab bar)
    function visibleArea() {
      let top = 0, bottom = window.innerHeight;
      document.querySelectorAll(".top-nav, .mui-bottom-nav").forEach((bar) => {
        const r = bar.getBoundingClientRect();
        if (!r.height || getComputedStyle(bar).position !== "fixed") return;
        if (r.top < window.innerHeight / 2) top = Math.max(top, r.bottom);
        else bottom = Math.min(bottom, r.top);
      });
      return { top, bottom };
    }

    // Near the top or bottom edge, keep scrolling while the finger is there
    function edgeScroll() {
      if (!drag) return;
      // only once the finger has moved, so holding a card near an edge
      // doesn't scroll by itself
      if (!drag.moved) {
        drag.raf = requestAnimationFrame(edgeScroll);
        return;
      }
      const { top, bottom } = visibleArea();
      let v = 0;
      if (drag.y > bottom - EDGE) v = Math.min(1, (drag.y - (bottom - EDGE)) / EDGE);
      else if (drag.y < top + EDGE) v = -Math.min(1, (top + EDGE - drag.y) / EDGE);
      if (v) {
        window.scrollBy(0, v * 18);
        dragTo(drag.x, drag.y);
      }
      drag.raf = requestAnimationFrame(edgeScroll);
    }

    function endDrag() {
      if (!drag) return;
      cancelAnimationFrame(drag.raf);
      document.body.classList.remove("mui-drag-selecting");
      drag = null;
    }

    window.addEventListener("touchstart", (e) => {
      cancel();
      endDrag();
      fired = false;
      if (e.touches.length !== 1 || !(e.target instanceof Element)) return;
      const card = e.target.closest(".grid-card");
      if (!card || card.closest(".slick-list")) return; // home rows can't select
      if (e.target.closest("input, button, .btn, .card-popovers")) return;
      const check = checkOf(card);
      if (!check) return;
      // Once anything is selected Stash makes cards draggable (mouse
      // drag-select). On a phone, holding a draggable card picks it up for
      // the browser's drag-and-drop instead, which cancels this touch.
      [card, ...card.querySelectorAll("[draggable], img, a")].forEach((el) => {
        if (el.draggable) el.draggable = false;
      });
      start = { x: e.touches[0].clientX, y: e.touches[0].clientY };
      timer = setTimeout(() => {
        timer = null;
        fired = true;
        suppressUntil = performance.now() + 800;
        check.click(); // goes through Stash's own selection handling
        if (navigator.vibrate) navigator.vibrate(15);
        // the cards of this list, in order
        const list = card.parentElement;
        const cards = [...list.querySelectorAll(":scope > .grid-card, :scope > * > .grid-card")].filter((c) => checkOf(c));
        drag = { cards, from: cards.indexOf(card), select: check.checked, changed: new Set(), x: start.x, y: start.y };
        if (drag.from === -1) drag = null;
        else {
          document.body.classList.add("mui-drag-selecting");
          drag.raf = requestAnimationFrame(edgeScroll);
        }
      }, HOLD_MS);
    }, { capture: true, passive: true });

    window.addEventListener("touchmove", (e) => {
      if (drag) {
        // selecting: the finger picks cards instead of scrolling
        if (e.cancelable) e.preventDefault();
        const t = e.touches[0];
        drag.x = t.clientX;
        drag.y = t.clientY;
        if (Math.abs(drag.x - start.x) > 2 * MOVE_TOLERANCE || Math.abs(drag.y - start.y) > 2 * MOVE_TOLERANCE) drag.moved = true;
        dragTo(t.clientX, t.clientY);
        return;
      }
      if (!timer || !start) return;
      const t = e.touches[0];
      if (Math.abs(t.clientX - start.x) > MOVE_TOLERANCE || Math.abs(t.clientY - start.y) > MOVE_TOLERANCE) cancel();
    }, { capture: true, passive: false });

    window.addEventListener("touchend", (e) => {
      cancel();
      endDrag();
      if (fired) {
        e.preventDefault(); // don't also open the card
        fired = false;
      }
    }, { capture: true, passive: false });
    window.addEventListener("touchcancel", () => {
      cancel();
      endDrag();
    }, { capture: true, passive: true });

    // Backup: swallow the tap the browser may still send after a hold
    window.addEventListener("click", (e) => {
      if (performance.now() < suppressUntil && e.target instanceof Element &&
          e.target.closest(".grid-card") && !e.target.closest(".card-check")) {
        e.preventDefault();
        e.stopPropagation();
        suppressUntil = 0;
      }
    }, { capture: true });

    // Backup for the above: no browser drag-and-drop started by a finger
    let touching = false;
    window.addEventListener("touchstart", () => { touching = true; }, { capture: true, passive: true });
    window.addEventListener("touchend", (e) => { if (!e.touches.length) touching = false; }, { capture: true, passive: true });
    window.addEventListener("touchcancel", () => { touching = false; }, { capture: true, passive: true });
    window.addEventListener("dragstart", (e) => {
      if ((touching || drag) && e.target instanceof Element && e.target.closest(".grid-card")) {
        e.preventDefault();
        e.stopPropagation();
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
  // "Count" sort on the Tags, Performers and Studios pages
  // ------------------------------------------------------------------
  // Sorts by scenes + images + galleries together. Stash can't sort by a
  // sum, so when the list asks for sort "mui_count", the request is handled
  // here: every matching item's three counts are fetched (cached briefly),
  // added up and ranked, and then Stash is asked for just the current
  // page's items by id. Paging, filters, search and the direction arrow
  // keep working. The option is added to the Sort menu, which only offers
  // Stash's own sorts.
  function setupCountSort() {
    const SORT = "mui_count";
    const LABEL = "Count";
    const LISTS = {
      FindTags: { field: "findTags", list: "tags", filterVar: "tag_filter", filterType: "TagFilterType", idsVar: "ids", type: "FindTagsResultType" },
      // (the Tags page itself uses this one)
      FindTagsForList: { field: "findTags", list: "tags", filterVar: "tag_filter", filterType: "TagFilterType", idsVar: "ids", addIds: true, type: "FindTagsResultType" },
      FindPerformers: { field: "findPerformers", list: "performers", filterVar: "performer_filter", filterType: "PerformerFilterType", idsVar: "performer_ids", intIds: true, type: "FindPerformersResultType" },
      FindStudios: { field: "findStudios", list: "studios", filterVar: "studio_filter", filterType: "StudioFilterType", idsVar: "ids", addIds: true, type: "FindStudiosResultType" },
    };
    const realFetch = window.fetch.bind(window);
    const rankings = new Map(); // key -> { at, promise of [{ id, n: count, i: place by name }] }

    async function ranking(cfg, vars) {
      const key = cfg.field + JSON.stringify([vars.filter.q || "", vars[cfg.filterVar] || null]);
      const hit = rankings.get(key);
      if (hit && Date.now() - hit.at < 30000) return hit.promise;
      const promise = (async () => {
        const res = await realFetch("/graphql", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            query: `query ($f: FindFilterType, $o: ${cfg.filterType}) { ${cfg.field}(filter: $f, ${cfg.filterVar}: $o) { ${cfg.list} { id scene_count image_count gallery_count } } }`,
            variables: { f: { q: vars.filter.q || undefined, per_page: -1, sort: "name", direction: "ASC" }, o: vars[cfg.filterVar] || undefined },
          }),
        });
        const json = await res.json();
        if (json.errors) throw new Error(json.errors[0].message);
        return json.data[cfg.field][cfg.list]
          .map((x, i) => ({ id: String(x.id), n: (x.scene_count || 0) + (x.image_count || 0) + (x.gallery_count || 0), i }));
      })();
      promise.catch(() => rankings.delete(key));
      rankings.set(key, { at: Date.now(), promise });
      return promise;
    }

    async function countSorted(cfg, body, url, init) {
      const vars = body.variables;
      const f = vars.filter;
      // by count, then name (A-Z either way)
      const asc = String(f.direction).toUpperCase() === "ASC";
      const ids = (await ranking(cfg, vars))
        .slice()
        .sort((a, b) => (asc ? a.n - b.n : b.n - a.n) || a.i - b.i)
        .map((x) => x.id);
      const total = ids.length;
      const per = f.per_page > 0 ? f.per_page : total;
      const pageIds = ids.slice(((f.page || 1) - 1) * per, (f.page || 1) * per);
      const reply = (data) => new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
      if (!pageIds.length) return reply({ data: { [cfg.field]: { __typename: cfg.type, count: total, [cfg.list]: [] } } });

      const next = JSON.parse(JSON.stringify(body));
      next.variables.filter = { ...f, page: 1, per_page: pageIds.length, sort: "name", direction: "ASC" };
      next.variables[cfg.idsVar] = cfg.intIds ? pageIds.map(Number) : pageIds;
      if (cfg.addIds) {
        // Some of Stash's list queries have no ids argument: add it
        const q = next.query
          .replace(new RegExp(`(query\\s+${body.operationName}\\s*\\([^)]*)\\)`), "$1, $ids: [ID!])")
          .replace(new RegExp(`(${cfg.field}\\s*\\([^)]*)\\)`), "$1, ids: $ids)");
        if (!/\$ids: \[ID!\]/.test(q) || !/[(,]\s*ids: \$ids/.test(q)) throw new Error(`can't add ids to ${body.operationName}`);
        next.query = q;
      }
      const res = await realFetch(url, { ...init, body: JSON.stringify(next) });
      const json = await res.json();
      const result = json.data && json.data[cfg.field];
      if (result) {
        const order = new Map(pageIds.map((id, i) => [id, i]));
        result[cfg.list].sort((a, b) => order.get(String(a.id)) - order.get(String(b.id)));
        result.count = total;
      }
      return reply(json);
    }

    window.fetch = function (input, init) {
      try {
        const url = typeof input === "string" ? input : input && input.url;
        if (url && /\/graphql(\?|$)/.test(url) && init && typeof init.body === "string" && init.body.includes(SORT)) {
          const body = JSON.parse(init.body);
          const cfg = LISTS[body.operationName];
          if (cfg && body.variables && body.variables.filter && body.variables.filter.sort === SORT) {
            return countSorted(cfg, body, url, init).catch((e) => {
              console.warn("[Modern UI] Count sort failed, showing by name instead", e);
              body.variables.filter.sort = "name";
              return realFetch(url, { ...init, body: JSON.stringify(body) });
            });
          }
        }
      } catch (e) {}
      return realFetch(input, init);
    };

    // ---- the Sort menu ----
    const LIST_PAGE = /^\/(tags|performers|studios)\/?$/;
    const isCountMenu = (menu) => {
      const values = new Set([...menu.querySelectorAll(".dropdown-item[data-value]")].map((el) => el.dataset.value));
      return ["scenes_count", "images_count", "galleries_count"].every((v) => values.has(v));
    };
    const sortedByCount = () => new URLSearchParams(window.location.search).get("sortby") === SORT;

    function sync() {
      if (!LIST_PAGE.test(window.location.pathname)) return;
      document.querySelectorAll(".sort-by-select").forEach((select) => {
        const toggle = select.querySelector(".dropdown-toggle");
        const menu = select.querySelector(".dropdown-menu");
        // Stash shows no label for a sort it doesn't know
        if (toggle && sortedByCount() && toggle.textContent.trim() !== LABEL) toggle.textContent = LABEL;
        if (!menu || menu.querySelector(".mui-count-sort") || !isCountMenu(menu)) return;
        const item = document.createElement("a");
        item.href = "#";
        item.setAttribute("role", "button");
        item.className = "dropdown-item bg-secondary text-white mui-count-sort";
        item.dataset.value = SORT;
        item.textContent = LABEL;
        item.title = "Scenes + images + galleries";
        item.addEventListener("click", (e) => {
          e.preventDefault();
          toggle && toggle.click(); // close the menu
          const params = new URLSearchParams(window.location.search);
          if (params.get("sortby") !== SORT) params.set("sortdir", "desc");
          params.set("sortby", SORT);
          params.delete("p");
          routerNavigate(window.location.pathname + "?" + params.toString());
        });
        // keep the menu in alphabetical order, like Stash's own options
        const after = [...menu.querySelectorAll(".dropdown-item")].find((el) => el.textContent.trim().localeCompare(LABEL) > 0);
        after ? after.before(item) : menu.appendChild(item);
      });
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
      if (document.body.classList.contains("mui-viewer-open")) return null;
      if (target.closest(".grid-card, .mlt-related, .mlt-similar, .mlt-perf-similar, .top-nav, .mui-bottom-nav, .mui-sheet, .modal, " + LIGHTBOX)) return null;
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
      if (document.querySelector(".modal.show, .mui-viewer, " + LIGHTBOX)) return; // a full-screen viewer handles keys
      if (e.key === "ArrowRight" && go(1)) e.preventDefault();
      if (e.key === "ArrowLeft" && go(-1)) e.preventDefault();
    });

    return { neighbour, go };
  }

  // ------------------------------------------------------------------
  // Image page: click the picture to see it full screen and zoom in
  // ------------------------------------------------------------------
  // Zoom with the mouse wheel / trackpad pinch, pinch on a phone, or
  // double-click / double-tap; drag to move around when zoomed. Clicking or
  // tapping outside the picture, Esc, or the Close button closes it. While
  // not zoomed in, swipe (or drag, or the arrow keys) to the next / previous
  // image in the same list as the image page's swipe; the viewer stays open.

  function setupImageViewer(nav) {
    const IMAGE_PAGE = /^\/images\/\d+\/?$/;
    const MAX = 8;
    let viewer = null;

    document.addEventListener("click", (e) => {
      if (viewer || e.button !== 0 || !IMAGE_PAGE.test(location.pathname)) return;
      const img = e.target instanceof Element && e.target.closest(".image-container img.image-image");
      if (!img || !img.complete || !img.naturalWidth) return;
      e.preventDefault();
      open(img.currentSrc || img.src, img.alt);
    });

    // id -> promise of the picture's URL (null for videos / clips)
    const srcCache = new Map();
    function pictureOf(id) {
      if (!srcCache.has(id)) {
        srcCache.set(
          id,
          fetch("/graphql", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              query: "query($id: ID!) { findImage(id: $id) { paths { image } visual_files { __typename } } }",
              variables: { id },
            }),
          })
            .then((r) => r.json())
            .then((j) => {
              const im = j.data && j.data.findImage;
              if (!im || (im.visual_files[0] && im.visual_files[0].__typename === "VideoFile")) return null;
              // load it now, so it shows straight away
              return new Promise((done) => {
                const pre = new Image();
                pre.onload = pre.onerror = () => done(im.paths.image);
                pre.src = im.paths.image;
              });
            })
            .catch(() => null)
        );
      }
      return srcCache.get(id);
    }

    function open(src, alt) {
      const box = document.createElement("div");
      box.className = "mui-viewer";
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-label", "Picture, full screen");
      const pic = document.createElement("img");
      pic.className = "mui-viewer-img";
      pic.src = src;
      pic.alt = alt || "";
      pic.draggable = false;
      const close = document.createElement("button");
      close.type = "button";
      close.className = "mui-viewer-close";
      close.textContent = "✕ Close";
      box.append(pic, close);
      document.body.appendChild(box);
      document.body.classList.add("mui-viewer-open");

      let s = 1, tx = 0, ty = 0; // zoom and offset of the picture
      const pointers = new Map(); // pointerId -> {x, y}
      let gesture = null; // what the current touch / drag is doing
      // Where the press started: with pointer capture the click itself is
      // reported on the backdrop even when it was on the picture
      let moved = false, lastTap = 0, downOnPic = false, mouse = true;
      let swipeT = 0, busy = false, ownNav = false;

      // Get the neighbours' pictures ready
      const prefetch = () => nav && [1, -1].forEach((st) => { const n = nav.neighbour(st); if (n) pictureOf(n); });
      prefetch();

      // Move to the next (1) / previous (-1) image, staying full screen
      async function step(dir, fromX) {
        const id = nav && nav.neighbour(dir);
        if (!id || busy) {
          // end of the list: spring back
          tx = 0;
          apply(true);
          return;
        }
        busy = true;
        const W = box.clientWidth;
        pic.style.transition = "transform 0.18s ease-out, opacity 0.18s ease-out";
        pic.style.transform = `translate(${-dir * W}px, 0px)`;
        pic.style.opacity = "0";
        const [src] = await Promise.all([pictureOf(id), new Promise((r) => setTimeout(r, 180))]);
        ownNav = true;
        nav.go(dir);
        ownNav = false;
        if (!src) {
          // a video clip: show it on its page instead
          busy = false;
          shut();
          return;
        }
        pic.src = src;
        s = 1; tx = 0; ty = 0;
        pic.style.transition = "none";
        pic.style.transform = `translate(${dir * W * 0.3}px, 0px)`;
        void pic.offsetWidth;
        pic.style.opacity = "";
        apply(true);
        busy = false;
        prefetch();
      }

      const apply = (animate) => {
        // keep the picture from being dragged away off the screen
        const w = pic.offsetWidth * s, hgt = pic.offsetHeight * s;
        const mx = Math.max(0, (w - box.clientWidth) / 2), my = Math.max(0, (hgt - box.clientHeight) / 2);
        tx = Math.min(mx, Math.max(-mx, tx));
        ty = Math.min(my, Math.max(-my, ty));
        pic.style.transition = animate ? "transform 0.2s ease" : "none";
        pic.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
        box.classList.toggle("mui-viewer-zoomed", s > 1.01);
      };
      // Zoom to `next`, keeping the point under (x, y) where it is
      const zoomAt = (next, x, y, animate) => {
        next = Math.min(MAX, Math.max(1, next));
        const cx = x - box.clientWidth / 2, cy = y - box.clientHeight / 2;
        tx = cx - ((cx - tx) * next) / s;
        ty = cy - ((cy - ty) * next) / s;
        s = next;
        if (s === 1) tx = ty = 0;
        apply(animate);
      };
      const toggleZoom = (x, y) => zoomAt(s > 1.01 ? 1 : 2.5, x, y, true);

      function shut() {
        if (!viewer) return;
        viewer = null;
        box.remove();
        document.body.classList.remove("mui-viewer-open");
        document.removeEventListener("keydown", onKey, true);
        window.removeEventListener("popstate", onPop);
        window.removeEventListener("resize", onResize);
      }
      const onKey = (e) => {
        if (e.key === "Escape") shut();
        else if (e.key === "+" || e.key === "=") zoomAt(s * 1.5, box.clientWidth / 2, box.clientHeight / 2, true);
        else if (e.key === "-") zoomAt(s / 1.5, box.clientWidth / 2, box.clientHeight / 2, true);
        else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && s <= 1.01 && nav) step(e.key === "ArrowRight" ? 1 : -1);
        else if (e.key === "ArrowRight" || e.key === "ArrowLeft") { /* zoomed in: nothing */ }
        else return;
        e.preventDefault();
        e.stopPropagation();
      };
      const onResize = () => apply(false);
      document.addEventListener("keydown", onKey, true);
      const onPop = () => { if (!ownNav) shut(); }; // e.g. the phone's back gesture
      window.addEventListener("popstate", onPop);
      window.addEventListener("resize", onResize);
      viewer = { shut };

      close.addEventListener("click", (e) => {
        e.stopPropagation();
        shut();
      });

      box.addEventListener("wheel", (e) => {
        e.preventDefault();
        // ctrlKey: a trackpad pinch, which sends small steps
        const f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002));
        zoomAt(s * f, e.clientX, e.clientY, false);
      }, { passive: false });

      box.addEventListener("dblclick", (e) => {
        // touch screens get their own double-tap below (some browsers fire
        // dblclick for taps too, which would undo it)
        if (downOnPic && mouse) toggleZoom(e.clientX, e.clientY);
      });

      box.addEventListener("pointerdown", (e) => {
        if (e.target === close) return;
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        box.setPointerCapture(e.pointerId);
        if (pointers.size === 1) {
          moved = false;
          downOnPic = e.target === pic;
          mouse = e.pointerType === "mouse";
          swipeT = performance.now();
          gesture = { type: "pan", x: e.clientX, y: e.clientY, tx, ty, onPic: e.target === pic };
        } else if (pointers.size === 2) {
          const [a, b] = [...pointers.values()];
          moved = true;
          gesture = {
            type: "pinch",
            dist: Math.hypot(a.x - b.x, a.y - b.y) || 1,
            mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, s, tx, ty,
          };
        }
      });

      box.addEventListener("pointermove", (e) => {
        if (!pointers.has(e.pointerId) || !gesture) return;
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (gesture.type === "pinch" && pointers.size >= 2) {
          const [a, b] = [...pointers.values()];
          const g = gesture;
          const next = Math.min(MAX, Math.max(1, (g.s * Math.hypot(a.x - b.x, a.y - b.y)) / g.dist));
          const mx = (a.x + b.x) / 2 - box.clientWidth / 2, my = (a.y + b.y) / 2 - box.clientHeight / 2;
          const ox = g.mx - box.clientWidth / 2, oy = g.my - box.clientHeight / 2;
          tx = mx - ((ox - g.tx) * next) / g.s;
          ty = my - ((oy - g.ty) * next) / g.s;
          s = next;
          apply(false);
        } else if (gesture.type === "pan") {
          const dx = e.clientX - gesture.x, dy = e.clientY - gesture.y;
          if (Math.abs(dx) > 6 || Math.abs(dy) > 6) moved = true;
          if (s > 1.01) {
            tx = gesture.tx + dx;
            ty = gesture.ty + dy;
            apply(false);
          } else if (nav && !busy && moved) {
            // not zoomed: the picture follows the finger sideways
            if (!gesture.dir) gesture.dir = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
            if (gesture.dir === "x") {
              const end = !nav.neighbour(dx < 0 ? 1 : -1);
              gesture.dx = dx;
              pic.style.transition = "none";
              pic.style.transform = `translate(${end ? dx * 0.25 : dx}px, 0px)`;
            }
          }
        }
      });

      const lift = (e) => {
        if (!pointers.delete(e.pointerId)) return;
        if (pointers.size === 1 && gesture && gesture.type === "pinch") {
          // one finger left after a pinch: carry on moving with it
          const [p] = [...pointers.values()];
          gesture = { type: "pan", x: p.x, y: p.y, tx, ty, onPic: true };
          if (s < 1.01) zoomAt(1, 0, 0, true);
          return;
        }
        if (pointers.size) return;
        const g = gesture;
        gesture = null;
        if (g && g.dir === "x" && s <= 1.01) {
          const dx = g.dx || 0;
          const fast = Math.abs(dx) / Math.max(performance.now() - swipeT, 1) > 0.5;
          if (e.type !== "pointercancel" && (Math.abs(dx) > 60 || (fast && Math.abs(dx) > 25))) step(dx < 0 ? 1 : -1);
          else apply(true); // not far enough: spring back
          return;
        }
        if (e.type === "pointercancel" || moved || !g) return;
        // double-tap on a phone (a mouse uses dblclick)
        if (e.pointerType !== "mouse" && g.onPic) {
          const now = performance.now();
          if (now - lastTap < 300) {
            lastTap = 0;
            toggleZoom(e.clientX, e.clientY);
          } else lastTap = now;
        }
      };
      box.addEventListener("pointerup", lift);
      box.addEventListener("pointercancel", lift);

      // A plain click or tap outside the picture closes it. (Done on click,
      // not pointerup, so the tap doesn't also land on the page underneath.)
      box.addEventListener("click", (e) => {
        if (!downOnPic && !moved && e.target !== close) shut();
      });

      if (!pic.complete) pic.addEventListener("load", () => apply(false), { once: true });
      apply(false);
    }

    // Leaving the image page (e.g. a link elsewhere) closes it too
    window.addEventListener("popstate", () => {
      if (viewer && !IMAGE_PAGE.test(location.pathname)) viewer.shut();
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
