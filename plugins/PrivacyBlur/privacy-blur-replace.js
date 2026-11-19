// Privacy Blur - "Replace instead of blurring": every picture and video
// shows a safe stand-in (simple drawn landscapes), and names, studios,
// titles and tags show generic ones. The same item always gets the same
// stand-in, so a page still makes sense. Only the screen changes; turning it
// off puts everything back.
(function () {
  "use strict";
  const root = document.documentElement;

  // ---- stand-in pictures: drawn here (no downloads, no licences) ----------
  const PALETTES = [
    ["#ffb26b", "#ff7b54", "#6a4c93", "#2d3047"], // sunset
    ["#a8e6ff", "#5fb8e8", "#2f6f9f", "#183a4a"], // sea
    ["#cfe8c6", "#8cc084", "#4f7942", "#2e4a2a"], // forest
    ["#fde2c8", "#f6b17a", "#c06c84", "#355c7d"], // dusk
    ["#e0f4ff", "#9bd0f5", "#5d8aa8", "#36454f"], // snow
    ["#fff1c1", "#ffd56b", "#e0a43a", "#8a5a2b"], // desert
    ["#1b1f3b", "#3a3f7a", "#7f5af0", "#2cb67d"], // night
    ["#ffe4ec", "#ffb3c6", "#9d4edd", "#3c096c"], // lavender
  ];
  const svg = (body, p) =>
    "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300" preserveAspectRatio="xMidYMid slice">` +
      `<defs><linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${p[0]}"/><stop offset="1" stop-color="${p[1]}"/></linearGradient></defs>` +
      `<rect width="400" height="300" fill="url(#s)"/>${body}</svg>`);
  const SCENES = [
    // mountains and a sun
    (p) => `<circle cx="300" cy="90" r="34" fill="#fff" opacity=".75"/><path d="M0 230 L90 120 L160 200 L240 100 L330 210 L400 150 L400 300 L0 300Z" fill="${p[2]}"/><path d="M0 260 L120 190 L220 250 L320 200 L400 240 L400 300 L0 300Z" fill="${p[3]}"/>`,
    // sea and a sailboat
    (p) => `<circle cx="90" cy="80" r="26" fill="#fff" opacity=".7"/><rect y="190" width="400" height="110" fill="${p[2]}"/><path d="M0 205 Q50 195 100 205 T200 205 T300 205 T400 205 V300 H0Z" fill="${p[3]}" opacity=".6"/><path d="M230 185 L250 120 L250 185Z" fill="#fff"/><path d="M215 188 H275 L265 198 H225Z" fill="${p[3]}"/>`,
    // forest
    (p) => `<rect y="220" width="400" height="80" fill="${p[3]}"/>` + [30, 80, 130, 185, 240, 290, 345, 385].map((x, i) =>
      `<path d="M${x} ${150 + (i % 3) * 18} L${x - 28} 240 L${x + 28} 240Z" fill="${i % 2 ? p[2] : p[3]}"/>`).join(""),
    // city skyline
    (p) => [[10, 140, 50], [65, 100, 40], [110, 160, 55], [170, 80, 45], [220, 130, 60], [285, 110, 40], [330, 150, 60]].map(([x, y, w]) =>
      `<rect x="${x}" y="${y}" width="${w}" height="${300 - y}" fill="${p[3]}"/>` +
      Array.from({ length: 6 }, (_, r) => `<rect x="${x + 8}" y="${y + 12 + r * 22}" width="${w - 16}" height="6" fill="${p[0]}" opacity=".45"/>`).join("")).join(""),
    // rolling hills
    (p) => `<circle cx="320" cy="70" r="28" fill="#fff" opacity=".7"/><path d="M0 200 Q100 150 200 200 T400 190 V300 H0Z" fill="${p[2]}"/><path d="M0 240 Q120 200 240 245 T400 235 V300 H0Z" fill="${p[3]}"/>`,
    // dunes
    (p) => `<circle cx="80" cy="70" r="30" fill="#fff" opacity=".8"/><path d="M0 210 Q150 150 260 210 T400 200 V300 H0Z" fill="${p[2]}"/><path d="M0 250 Q140 215 300 255 T400 250 V300 H0Z" fill="${p[3]}" opacity=".85"/>`,
    // lake with reflection
    (p) => `<path d="M0 170 L110 90 L190 160 L280 70 L400 170Z" fill="${p[3]}"/><rect y="170" width="400" height="130" fill="${p[2]}" opacity=".85"/><path d="M0 170 L110 250 L190 180 L280 270 L400 170Z" fill="${p[3]}" opacity=".35"/>`,
    // starry sky
    (p) => Array.from({ length: 40 }, (_, i) => `<circle cx="${(i * 97) % 400}" cy="${(i * 53) % 180}" r="${1 + (i % 3) * 0.6}" fill="#fff" opacity=".8"/>`).join("") +
      `<circle cx="300" cy="70" r="22" fill="#fff" opacity=".9"/><path d="M0 250 Q200 200 400 250 V300 H0Z" fill="${p[3]}"/>`,
    // flowers in a field
    (p) => `<rect y="200" width="400" height="100" fill="${p[2]}"/>` + Array.from({ length: 14 }, (_, i) =>
      `<line x1="${20 + i * 28}" y1="${215 + (i % 4) * 12}" x2="${20 + i * 28}" y2="300" stroke="${p[3]}" stroke-width="3"/><circle cx="${20 + i * 28}" cy="${215 + (i % 4) * 12}" r="9" fill="${["#ffd6e0", "#fff3b0", "#ffffff", "#ffb4a2"][i % 4]}"/>`).join(""),
    // aurora
    (p) => `<path d="M0 120 Q100 60 200 110 T400 90" stroke="${p[3]}" stroke-width="40" fill="none" opacity=".5"/><path d="M0 150 Q120 100 220 140 T400 120" stroke="${p[2]}" stroke-width="26" fill="none" opacity=".45"/><path d="M0 250 L150 200 L260 245 L400 210 V300 H0Z" fill="#11131a"/>`,
    // abstract shapes
    (p) => `<circle cx="120" cy="120" r="80" fill="${p[2]}" opacity=".6"/><circle cx="270" cy="170" r="100" fill="${p[3]}" opacity=".45"/><rect x="180" y="40" width="90" height="90" rx="20" fill="#fff" opacity=".35"/>`,
    // beach with palm
    (p) => `<rect y="180" width="400" height="60" fill="${p[2]}"/><path d="M0 235 Q200 215 400 235 V300 H0Z" fill="#f3d9a4"/><path d="M90 240 Q100 180 80 130" stroke="#6b4f2a" stroke-width="8" fill="none"/>` +
      [[-50, -10], [50, -5], [-35, 30], [40, 30]].map(([dx, dy]) => `<path d="M80 130 q${dx / 2} ${dy - 25} ${dx} ${dy}" stroke="#3f7d3a" stroke-width="7" fill="none" stroke-linecap="round"/>`).join(""),
  ];
  const PICTURES = [];
  SCENES.forEach((scene, i) => {
    PICTURES.push(svg(scene(PALETTES[i % PALETTES.length]), PALETTES[i % PALETTES.length]));
    PICTURES.push(svg(scene(PALETTES[(i + 3) % PALETTES.length]), PALETTES[(i + 3) % PALETTES.length]));
  });

  // ---- generic names ----------------------------------------------------
  const WORDS = {
    performer: ["Jane Doe", "Alex Rivera", "Mia Brooks", "Sam Lee", "Chloe Hart", "Nina Park", "Ruby Lane", "Zoe Kim", "Ava Stone", "Lily Chen",
      "Emma Gray", "Leah Fox", "Ivy Moore", "Kate Bell", "Jade Ross", "Tara Quinn", "Erin Wells", "Maya Cruz", "Olivia Hayes", "Grace Lin",
      "Hannah Reed", "Sofia Marsh", "Claire Dunn", "Bella Ward", "Iris Cole", "Luna Price", "Nora Blake", "Ella Shaw"],
    studio: ["Northlight", "Blue Harbor", "Golden Hour", "Pine Studio", "Lumen Works", "Copper Lane", "Silver Coast", "Maple Films",
      "Harbor Light", "Redwood", "Sunset Media", "Bright Room", "Cedar Pictures", "Lakeside"],
    tag: ["Outdoors", "Beach", "Cosplay", "Selfie", "Sunset", "Studio", "Black and White", "Close-up", "Pool", "Night", "Mirror",
      "Garden", "Retro", "Neon", "Rain", "Snow", "City", "Forest", "Kitchen", "Portrait", "Candid", "Golden Hour", "Vintage",
      "Travel", "Indoors", "Fitness", "Road Trip", "Summer", "Winter", "Autumn", "Spring", "Lake", "Mountains", "Rooftop", "Cafe", "Park"],
    title: ["Morning Light", "Weekend at the Lake", "Rooftop Sunset", "City Lights", "Behind the Scenes", "Studio Session",
      "Golden Hour", "A Day at the Beach", "Road Trip, Part 2", "Rainy Afternoon", "Garden Party", "Night Drive", "First Shoot",
      "Coffee Break", "Snow Day", "Poolside", "Summer Evening", "The Interview", "Back Home", "Late Night", "Mountain Air",
      "Harbor Walk", "Picnic", "Old Town", "Blue Hour", "Spring Bloom"],
    detail: ["A relaxed afternoon, shot on location.", "Taken over a long weekend away.", "A short set from a sunny day out.",
      "Behind-the-scenes shots from the studio.", "Pictures from a trip along the coast.", "A quiet evening at home.",
      "Out and about in the city.", "A few favourites from the summer."],
    gallery: ["Summer Trip", "Road Trip", "Beach Day", "City Walk", "Holiday", "Weekend Away", "Studio Session", "Mountain Hike",
      "Lake House", "Rooftop Set", "Autumn Walk", "Snow Day", "Garden Party", "Night Out", "Cafe Morning", "Coastal Drive"],
  };
  const hash = (s) => {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    return h >>> 0;
  };
  // real -> made-up, kept for the session: the same name always gets the
  // same stand-in, and two different ones don't share one (while there are
  // enough to go round)
  const given = {}; // kind -> Map(real -> fake)
  function pick(kind, real) {
    const list = WORDS[kind];
    const map = (given[kind] = given[kind] || new Map());
    if (map.has(real)) return map.get(real);
    const used = new Set(map.values());
    let i = hash(kind + ":" + real) % list.length;
    for (let n = 0; n < list.length && used.has(list[i]); n++) i = (i + 1) % list.length;
    const name = list[i]; // (once they've all been used, they repeat)
    map.set(real, name);
    return name;
  }
  function fake(kind, text) {
    const t = text.trim();
    if (!t || /^[\d\s.,:/()%+-]*$/.test(t)) return text; // numbers, dates, counts stay
    if (/^[A-Z][\w ]{0,20}:$/.test(t)) return text;       // field labels ("Studio:")
    return text.replace(t, pick(kind, t.toLowerCase()));
  }
  // a marker's name: "7:44 - <scene title>" keeps its time and gets the same
  // stand-in title as the scene; any other name is a made-up title
  function fakeMarker(text) {
    const m = text.match(/^(\s*\d+(?::\d\d){1,2}\s*[-–]\s*)(.+?)(\s*)$/);
    if (!m) return fake("title", text);
    const t = m[2].trim();
    if (/^[\d\s.,:/()%+-]*$/.test(t)) return text; // just times
    return m[1] + pick("title", t.toLowerCase()) + m[3];
  }

  // which text is what (the same places the blur covers)
  const TEXT = [
    ["performer", ".performer-card .card-section-title, #performer-page .performer-head h2 .performer-name, #performer-page .performer-name, " +
      ".sticky.detail-header .performer-name, a.tag-item[href^='/performers/'], .performer-tag-container .performer-name, " +
      ".mlt-perf-card .mlt-rel-title, .mui-perf-card .mui-rel-title, #performer-page .alias-head"],
    ["studio", ".studio-overlay a, .studio-card .card-section-title, .studio-logo .studio-name, #studio-page .studio-name, " +
      ".mlt-studio-card .mlt-rel-title, .mui-studio-line a, a[href^='/studios/'].tag-item"],
    ["tag", ".tag-item:not([href^='/performers/']):not([href^='/studios/']):not([href^='/galleries/']), .tag-card .card-section-title, " +
      "#tag-page .tag-name, .detail-header .tag-name, .pts-chip, .mui-gqf-chip span, .bta-zoom-chip"],
    ["gallery", ".gallery-card .card-section-title, .gallery-header, .mui-gallery-title h2, .mlt-gal-card .mlt-rel-title, a.tag-item[href^='/galleries/']"],
    ["detail", ".tab-content p.pre, .scene-details .pre, #performer-page .detail-item-value.details"],
    ["performer", "a[href*='photographer']"],
    ["title", ".scene-card .card-section-title, .image-card .card-section-title, .group-card .card-section-title, .scene-header, " +
      ".image-header, #image-page .image-header, .group-head h2, .mlt-rel-title, .mui-rel-title, .bta-zoom-title, " +
      ".scene-marker-card__scene a"],
    // markers: the Markers page, a scene's Markers tab, the AI markers panels
    ["marker", ".scene-marker-card .card-section-title, .scene-markers-panel .primary-card-body .row > .btn-link:first-child, " +
      ".wall-item-text > div, .bta-quick-prev"],
  ];

  const origText = new WeakMap(); // text node -> what it said
  const origSrc = new WeakMap();  // img / video -> {src, srcset, poster}
  const origBg = new WeakMap();   // element -> its inline background-image
  const origTitle = new WeakMap(); // element -> its tooltip {real, shown}
  let on = false;

  function textNodes(el) {
    const out = [];
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) if (n.nodeValue.trim()) out.push(n);
    return out;
  }
  function swapText(el, kind) {
    if (kind === "detail") {
      const nodes = textNodes(el).concat([...el.childNodes].filter((n) => n.nodeType === 3 && !n.nodeValue.trim() && origText.has(n)));
      nodes.forEach((n) => { if (origText.has(n) && n.nodeValue !== origText.get(n).shown) origText.delete(n); });
      // the block's own text (not what's shown now) decides its stand-in
      const real = nodes.map((n) => (origText.has(n) ? origText.get(n).real : n.nodeValue)).join("").trim().slice(0, 200);
      if (!real) return;
      const sentence = pick("detail", real);
      nodes.forEach((n, i) => {
        const base = origText.has(n) ? origText.get(n).real : n.nodeValue;
        const shown = i === 0 ? sentence : "";
        if (n.nodeValue !== shown) n.nodeValue = shown;
        origText.set(n, { real: base, shown });
      });
      return;
    }
    textNodes(el).forEach((n) => {
      const real = origText.has(n) ? origText.get(n).real : n.nodeValue;
      // React changed it since: that's the new real text
      if (origText.has(n) && n.nodeValue !== origText.get(n).shown) origText.delete(n);
      const base = origText.has(n) ? origText.get(n).real : n.nodeValue;
      const shown = kind === "marker" ? fakeMarker(base) : fake(kind, base);
      if (n.nodeValue !== shown) n.nodeValue = shown;
      origText.set(n, { real: base, shown });
      void real;
    });
    if (el.title) el.title = "";
  }

  function standIn(key) {
    return PICTURES[hash(key || "x") % PICTURES.length];
  }
  function swapImg(img) {
    if (img.closest(".top-nav, .pb-keep, .mui-bottom-nav")) return;
    const src = img.getAttribute("src") || "";
    if (src.startsWith("data:image/svg+xml") && origSrc.has(img)) return;
    origSrc.set(img, { src, srcset: img.getAttribute("srcset") });
    img.removeAttribute("srcset");
    img.setAttribute("src", standIn(src.replace(/[?&]t=\d+/, "")));
    img.alt = "";
  }
  function swapVideo(v) {
    if (origSrc.has(v)) return;
    origSrc.set(v, { poster: v.getAttribute("poster") });
    const pic = standIn(v.currentSrc || v.getAttribute("src") || location.pathname);
    v.setAttribute("poster", pic);
    // the picture covers the video (its controls stay on top)
    const host = v.parentElement;
    if (host) {
      host.classList.add("pb-standin-host");
      host.style.setProperty("--pb-standin", `url("${pic}")`);
    }
  }
  function swapBg(el) {
    const bg = el.style.backgroundImage;
    if (!bg || bg.includes("data:image/svg+xml")) return;
    origBg.set(el, bg);
    el.style.backgroundImage = `url("${standIn(bg)}")`;
  }

  function pass() {
    if (!on) return;
    document.querySelectorAll("img").forEach(swapImg);
    document.querySelectorAll("video").forEach(swapVideo);
    document.querySelectorAll(".vjs-poster, .background-image-container, .scrubber-item, .vjs-vtt-thumbnail-display, [style*='background-image']")
      .forEach((el) => !el.closest(".top-nav") && swapBg(el));
    if (root.classList.contains("pb-names")) {
      TEXT.filter(([k]) => k === "performer" || k === "studio").forEach(([k, sel]) => document.querySelectorAll(sel).forEach((el) => swapText(el, k)));
    }
    if (root.classList.contains("pb-titles")) {
      // "Studio Code: <code>": a made-up code (the label and the value can
      // be separate bits of text)
      document.querySelectorAll("h6, dd, .detail-item").forEach((el) => {
        if (!/^\s*Studio Code/.test(el.textContent) || el.querySelector("h6, dd, .detail-item")) return;
        const nodes = textNodes(el);
        const at = nodes.findIndex((n) => /Studio Code/.test(origText.has(n) ? origText.get(n).real : n.nodeValue));
        if (at < 0) return;
        const parts = nodes.slice(at).map((n) => {
          if (origText.has(n) && n.nodeValue !== origText.get(n).shown) origText.delete(n);
          return origText.has(n) ? origText.get(n).real : n.nodeValue;
        });
        const real = parts.join("");
        const code = real.replace(/^\s*Studio Code\s*:?\s*/, "").trim();
        if (!code) return;
        const shown = "Studio Code: SC-" + String(hash(code) % 90000 + 10000);
        nodes.slice(at).forEach((n, k) => {
          const want = k === 0 ? shown : "";
          if (n.nodeValue !== want) n.nodeValue = want;
          origText.set(n, { real: parts[k], shown: want });
        });
      });
      TEXT.filter(([k]) => k === "title" || k === "gallery" || k === "detail" || k === "marker").forEach(([k, sel]) => document.querySelectorAll(sel).forEach((el) => swapText(el, k)));
      // hover tooltips that name a marker (the pictures under the video, the Markers tab)
      document.querySelectorAll(".mui-detail-marker[title], .wall-item-text > div[title]").forEach((el) => {
        const o = origTitle.get(el);
        const real = o && el.title === o.shown ? o.real : el.title;
        const shown = fakeMarker(real.replace(/ - play from .*$/, "")) + (/ - play from .*$/.exec(real) || [""])[0];
        if (el.title !== shown) el.title = shown;
        origTitle.set(el, { real, shown });
      });
    }
    if (root.classList.contains("pb-tags")) {
      TEXT.filter(([k]) => k === "tag").forEach(([k, sel]) => document.querySelectorAll(sel).forEach((el) => swapText(el, k)));
    }
  }

  function restore() {
    document.querySelectorAll("img").forEach((img) => {
      const o = origSrc.get(img);
      if (!o) return;
      img.setAttribute("src", o.src);
      if (o.srcset) img.setAttribute("srcset", o.srcset);
      origSrc.delete(img);
    });
    document.querySelectorAll("video").forEach((v) => {
      const o = origSrc.get(v);
      if (!o) return;
      if (o.poster) v.setAttribute("poster", o.poster); else v.removeAttribute("poster");
      v.parentElement?.classList.remove("pb-standin-host");
      origSrc.delete(v);
    });
    document.querySelectorAll("[style*='background-image']").forEach((el) => {
      if (origBg.has(el)) { el.style.backgroundImage = origBg.get(el); origBg.delete(el); }
    });
    document.querySelectorAll("[title]").forEach((el) => {
      const o = origTitle.get(el);
      if (o && el.title === o.shown) el.title = o.real;
      origTitle.delete(el);
    });
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const o = origText.get(n);
      if (o && n.nodeValue === o.shown) n.nodeValue = o.real;
      origText.delete(n);
    }
  }

  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued || !on) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      observer.disconnect();
      pass();
      observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["src", "srcset", "style", "poster", "title"] });
    });
  });

  function sync() {
    const want = root.classList.contains("pb-on") && root.classList.contains("pb-replace");
    if (want === on) return;
    on = want;
    if (on) {
      pass();
      observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["src", "srcset", "style", "poster", "title"] });
    } else {
      observer.disconnect();
      restore();
    }
  }
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["class"] });
  if (document.body) sync();
  else document.addEventListener("DOMContentLoaded", sync);
})();
