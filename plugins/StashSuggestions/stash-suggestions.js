// Stash Suggestions - "Suggested" scenes, "Similar photos" and "Similar
// Performers" rows at the bottom of scene, image and performer pages.
// Works on its own; with the Modern UI plugin installed it picks up that
// theme's look (light mode, picture shape, phone columns...).
//
// The image page rows save the order of the photos you pick from in
// sessionStorage "mui-image-list", which Modern UI's swipe-between-images
// reads (the key keeps its old name so the two keep working together).
(function () {
  "use strict";

  const body = document.body.classList;

  // Mark when an edit form is open (the Edit tab on scene, image and gallery
  // pages, or edit mode on a performer page) so the Suggested / Similar
  // sections can be hidden and don't get mixed in with the form
  function setupEditingFlag() {
    const EDITING = '.nav-link.active[data-rb-event-key$="-edit-panel"], #performer-page .detail-header.edit';
    const update = () => body.toggle("mlt-editing", !!document.querySelector(EDITING));
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

  // Go to a page through Stash's router (no full page reload)
  function routerNavigate(path) {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
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
        `<a class="mlt-rel-card" href="/scenes/${esc(s.id)}" data-preview="${esc(s.paths.preview || "")}">` +
        `<div class="mlt-rel-thumb">` +
        (s.paths.screenshot ? `<img src="${esc(s.paths.screenshot)}" alt="" loading="lazy" decoding="async">` : "") +
        (file.duration ? `<span class="mlt-rel-duration">${duration(file.duration)}</span>` : "") +
        `</div>` +
        `<div class="mlt-rel-title" title="${esc(title)}">${esc(title)}</div>` +
        (meta ? `<div class="mlt-rel-meta">${esc(meta)}</div>` : "") +
        `</a>`
      );
    }

    function rowHtml(row) {
      return (
        '<div class="mlt-related-row">' +
        `<div class="mlt-related-head"><h3>${esc(row.title)}</h3>` +
        '<div class="mlt-related-arrows">' +
        '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
        '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div></div>' +
        `<div class="mlt-related-strip">${row.scenes.map(card).join("")}</div>` +
        "</div>"
      );
    }

    function build(id) {
      const section = document.createElement("section");
      section.className = "mlt-related";
      section.dataset.scene = id;
      section.innerHTML = '<div class="mlt-related-loading">Finding suggestions…</div>';

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = arrow.closest(".mlt-related-row").querySelector(".mlt-related-strip");
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mlt-rel-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        routerNavigate(a.getAttribute("href"));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      // Desktop: play the preview clip while hovering a card
      section.addEventListener("mouseover", (e) => {
        if (!window.matchMedia("(hover: hover)").matches) return;
        const a = e.target.closest("a.mlt-rel-card");
        if (!a || !a.dataset.preview || a.querySelector("video")) return;
        const v = document.createElement("video");
        v.src = a.dataset.preview;
        v.muted = true;
        v.loop = true;
        v.playsInline = true;
        v.autoplay = true;
        a.querySelector(".mlt-rel-thumb").appendChild(v);
        a.addEventListener("mouseleave", () => v.remove(), { once: true });
      });

      rowsFor(id).then((rows) => {
        if (section.dataset.scene !== id || !section.isConnected) return;
        section.innerHTML = rows.length
          ? rows.map(rowHtml).join("")
          : '<div class="mlt-related-loading">No suggestions yet.</div>';
      }).catch((e) => {
        console.warn("[Stash Suggestions] could not load suggestions", e);
        section.innerHTML = '<div class="mlt-related-loading">Couldn’t load suggestions.</div>';
      });
      return section;
    }

    function sync() {
      const id = (window.location.pathname.match(SCENE_PAGE) || [])[1];
      const existing = document.querySelector(".mlt-related");
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
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mlt-related"))) return;
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
    const FROM_KEY = "mlt-from-gallery";
    const ROW_KEY = "mlt-from-home-row";
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
    // ...of which this many are in view at first: 2 on wider screens; on
    // phones the row starts at the current photo (swipe left for earlier ones)
    const shownBefore = () => (window.matchMedia("(max-width: 576px)").matches ? 0 : 2);
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
      `<a class="mlt-similar-card${x.id === currentId ? " mlt-current" : ""}" href="/images/${esc(x.id)}" data-id="${esc(x.id)}" title="${esc(x.title || "")}">` +
      `<img src="${esc(x.paths.thumbnail)}" alt="" loading="lazy" decoding="async"></a>`;

    const ARROWS = '<div class="mlt-related-arrows">' +
      '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
      '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div>';

    // "Load more" card at the end of a row/grid; more() resolves to { items, done }
    function addLoadMore(grid, more, currentId) {
      if (!more) return;
      grid.insertAdjacentHTML("beforeend",
        '<button type="button" class="mlt-load-more"><span>Load more</span></button>');
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
          console.warn("[Stash Suggestions] could not load more photos", err);
          btn.disabled = false;
          label.textContent = "Try again";
        }
      });
    }

    function build(id) {
      const section = document.createElement("section");
      section.className = "mlt-similar";
      section.dataset.image = id;
      const galleryId = fromGallery();
      section.dataset.gallery = galleryId || "";
      section.innerHTML =
        '<div class="mlt-from-gallery"></div>' +
        `<div class="mlt-similar-head mlt-similar-tags-head"><h3>Similar photos</h3><div class="mlt-similar-actions">${ARROWS}</div></div>` +
        '<div class="mlt-similar-grid mlt-similar-tags mlt-similar-strip"><div class="mlt-related-loading">Finding similar photos…</div></div>' +
        '<div class="mlt-from-performer"></div>';
      const grid = section.querySelector(".mlt-similar-tags");
      const galleryBox = section.querySelector(".mlt-from-gallery");

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = arrow.closest(".mlt-similar-head").nextElementSibling;
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mlt-similar-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        // Swiping on the next page moves through the grid you picked from
        // (the gallery grid is already in gallery order)
        // (the gallery grid swipes through the whole gallery, not just the 12 shown)
        const fromGrid = a.closest(".mlt-similar-grid");
        const ids = fromGrid.dataset.ids
          ? JSON.parse(fromGrid.dataset.ids)
          : [...fromGrid.querySelectorAll("a.mlt-similar-card")].map((c) => c.dataset.id);
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
            `<div class="mlt-similar-head"><h3>${esc(block.title)}</h3>` +
            '<div class="mlt-similar-actions">' +
            (block.link ? `<a class="mlt-similar-all" href="${esc(block.link)}">${esc(block.linkText)}</a>` : "") +
            ARROWS + '</div></div>' +
            `<div class="mlt-similar-grid mlt-similar-gallery mlt-similar-strip">${block.images.map((x) => cardHtml(x, id)).join("")}</div>`;
          const strip = galleryBox.querySelector(".mlt-similar-gallery");
          strip.dataset.ids = JSON.stringify(block.ids);
          addLoadMore(strip, block.more, id);
          // Start scrolled so only the photos before this one that should
          // show are in view (2, or none on phones)
          const before = shownBefore();
          const cards = [...strip.querySelectorAll("a.mlt-similar-card")];
          const cur = cards.findIndex((c) => c.dataset.id === id);
          const first = cards[Math.max(0, cur - before)];
          if (cur > before && first) {
            strip.scrollLeft += first.getBoundingClientRect().left - strip.getBoundingClientRect().left;
          }
          galleryBox.querySelector(".mlt-similar-all")?.addEventListener("click", (e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
            e.preventDefault();
            routerNavigate(e.currentTarget.getAttribute("href"));
          });
        }).catch((e) => console.warn("[Stash Suggestions] could not load the gallery / home row photos", e));
      }

      performerBlock(id).then((block) => {
        if (!block || section.dataset.image !== id || !section.isConnected) return;
        const box = section.querySelector(".mlt-from-performer");
        // Link to the performer's page (only when there's just one)
        const link = block.performers.length === 1
          ? `<a class="mlt-similar-all" href="/performers/${esc(block.performers[0].id)}">View performer</a>`
          : "";
        box.innerHTML =
          `<div class="mlt-similar-head"><h3>${esc(block.title)}</h3><div class="mlt-similar-actions">${link}${ARROWS}</div></div>` +
          `<div class="mlt-similar-grid mlt-similar-performer mlt-similar-strip">${block.images.map((x) => cardHtml(x, null)).join("")}</div>`;
        addLoadMore(box.querySelector(".mlt-similar-performer"), block.more, null);
        const all = box.querySelector(".mlt-similar-all");
        if (all) all.addEventListener("click", (e) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
          e.preventDefault();
          routerNavigate(e.currentTarget.getAttribute("href"));
        });
      }).catch((e) => console.warn("[Stash Suggestions] could not load performer photos", e));

      similarSource(id).then(async (next) => {
        const { items, done } = await next(MAX);
        if (section.dataset.image !== id || !section.isConnected) return;
        grid.innerHTML = items.length
          ? items.map((x) => cardHtml(x, null)).join("")
          : '<div class="mlt-related-loading">No similar photos yet.</div>';
        if (items.length && !done) addLoadMore(grid, () => next(MAX), null);
      }).catch((e) => {
        console.warn("[Stash Suggestions] could not load similar photos", e);
        grid.innerHTML = '<div class="mlt-related-loading">Couldn’t load similar photos.</div>';
      });
      return section;
    }

    function sync() {
      trackOrigin();
      const id = (window.location.pathname.match(IMAGE_PAGE) || [])[1];
      const existing = document.querySelector(".mlt-similar");
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
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mlt-similar"))) return;
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
        `<a class="mlt-rel-card mlt-perf-card" href="/performers/${esc(x.id)}">` +
        `<div class="mlt-rel-thumb">` +
        (x.image_path ? `<img src="${esc(x.image_path)}" alt="" loading="lazy" decoding="async">` : "") +
        `</div>` +
        `<div class="mlt-rel-title" title="${esc(name)}">${esc(name)}</div>` +
        (meta ? `<div class="mlt-rel-meta">${esc(meta)}</div>` : "") +
        `</a>`
      );
    }

    function build(kind, id) {
      const key = kind + ":" + id;
      const section = document.createElement("section");
      section.className = "mlt-perf-similar mlt-perf-similar-" + kind;
      section.dataset.key = key;

      section.addEventListener("click", (e) => {
        const arrow = e.target.closest("button[data-dir]");
        if (arrow) {
          const strip = section.querySelector(".mlt-related-strip");
          strip.scrollBy({ left: Number(arrow.dataset.dir) * strip.clientWidth * 0.8, behavior: "smooth" });
          return;
        }
        const a = e.target.closest("a.mlt-perf-card");
        if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        routerNavigate(a.getAttribute("href"));
        window.scrollTo({ top: 0, behavior: "smooth" });
      });

      similarTo(kind, id).then((list) => {
        if (section.dataset.key !== key || !section.isConnected || !list.length) return;
        section.innerHTML =
          '<div class="mlt-related-row">' +
          '<div class="mlt-related-head"><h3>Similar Performers</h3>' +
          '<div class="mlt-related-arrows">' +
          '<button type="button" data-dir="-1" aria-label="Scroll left">&#8249;</button>' +
          '<button type="button" data-dir="1" aria-label="Scroll right">&#8250;</button></div></div>' +
          `<div class="mlt-related-strip">${list.map(card).join("")}</div>` +
          "</div>";
      }).catch((e) => console.warn("[Stash Suggestions] could not load similar performers", e));
      return section;
    }

    function sync() {
      const path = window.location.pathname;
      const page = PAGES.find((pg) => pg.re.test(path));
      const existing = document.querySelector(".mlt-perf-similar");
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
      if (queued || mutations.every((m) => m.target instanceof Element && m.target.closest(".mlt-perf-similar"))) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        sync();
      });
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("popstate", sync);
    sync();
  }


  setupEditingFlag();
  setupSuggested();
  setupSimilarImages();
  setupSimilarPerformers();
})();
