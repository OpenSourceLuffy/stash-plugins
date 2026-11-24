// Better Organizing AI - the page side: an "AI" row under the Tags box, a
// "Faces" row under the Performers box and an "AI titles" row under the
// Title box when editing a scene or image (titles also on galleries), and
// "Ask AI for details" when editing a performer, asking the local AI model (Ollama) through this
// plugin. Works with or without Better Organizing Tools; if that's installed,
// its suggestion rows and these sit one after another.
(function () {
  "use strict";

  const PLUGIN_ID = "BetterTaggingAI";
  const api = window.PluginApi;
  if (!api || !api.patch) return;
  const React = api.React;
  const h = React.createElement;

  async function gql(query, variables) {
    const res = await fetch("/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, variables }),
    });
    const json = await res.json();
    if (json.errors) throw new Error(pythonWhy(json.errors[0].message));
    return json.data;
  }

  // Windows: Stash ran the Microsoft Store's stand-in for Python (error 9009)
  // instead of a real one - say how to point Stash at the real one
  function pythonWhy(m) {
    return /exit status (9009|9003)\b|Python was not found/i.test(m || "")
      ? "Stash couldn't start Python: on Windows it found the Microsoft Store's stand-in for it (error 9009). "
        + "In Stash, open Settings → System → Python executable path and enter your Python's full path "
        + "(run  py -c \"import sys; print(sys.executable)\"  in PowerShell to see it), then try again."
      : m;
  }

  function ask(args) {
    return gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", {
      p: PLUGIN_ID,
      a: args,
    }).then((d) => d.runPluginOperation);
  }

  // ---- what's installed: a feature shows only when what it needs is there ----
  // (the face helper and its models, Apple's Vision, CLIP's text model, the AI
  // model in Ollama, Auto Seek's model, a look-alike scan). The last answer is
  // kept, so pages don't wait; before the very first answer nothing that needs
  // a model shows.
  const CAPS_KEY = "bta-caps";
  const caps = { data: null, listeners: new Set(), at: 0 };
  try { caps.data = JSON.parse(localStorage.getItem(CAPS_KEY) || "null"); } catch (e) { /* fine */ }
  function refreshCaps(force) {
    if (!force && Date.now() - caps.at < 5 * 60 * 1000) return;
    caps.at = Date.now();
    ask({ mode: "capabilities" }).then((d) => {
      caps.data = d || {};
      try { localStorage.setItem(CAPS_KEY, JSON.stringify(caps.data)); } catch (e) { /* fine */ }
      caps.listeners.forEach((fn) => fn());
    }).catch(() => {});
  }
  const capsNow = () => caps.data || {};
  function useCaps() {
    const [, bump] = React.useState(0);
    React.useEffect(() => {
      const fn = () => bump((n) => n + 1);
      caps.listeners.add(fn);
      refreshCaps();
      return () => caps.listeners.delete(fn);
    }, []);
    return capsNow();
  }
  // what each feature needs
  const NEEDS = {
    none: () => true,
    ollama: (c) => !!c.ollama,
    looks: (c) => !!(c.embed && c.looks),
    tags: (c) => !!(c.ollama || (c.embed && c.looks)),
    faces: (c) => !!c.faces,
    search: (c) => !!(c.text && c.embed && c.looks),
    markers: (c) => !!c.auto_seek,
    body: (c) => !!c.body,
    apple: (c) => !!c.apple,
    helper: (c) => !!c.helper,
  };
  const can = (need, c) => (NEEDS[need] || NEEDS.none)(c || capsNow());
  // a page part that shows only when it can work
  function Gate({ need, children }) {
    const c = useCaps();
    return can(need, c) ? children : null;
  }
  refreshCaps();

  // ---- "Ask AI for tags" under the Tags box ---------------------------------
  const ITEM_PAGE = /^\/(scenes|images)\/(\d+)/;

  function lookList(out) {
    return (out || []).map((t) => ({ id: String(t.id), name: t.name, aliases: [], score: t.score, sure: t.sure }));
  }

  function AiTags({ values, onSelect }) {
    const cz = useCaps();
    const m = location.pathname.match(ITEM_PAGE);
    const [ai, setAi] = React.useState({ state: "idle" });
    const [look, setLook] = React.useState({ state: "idle" });
    const key = m ? m[1] + m[2] : "";
    const kind = m && (m[1] === "scenes" ? "scene" : "image");
    // look-alike tags are quick: load them as soon as the edit form opens
    // (if they can't be had, e.g. not scanned yet, the button stays instead)
    React.useEffect(() => {
      setAi({ state: "idle" });
      if (!m) return undefined;
      let live = true;
      setLook({ state: "loading", auto: true });
      ask({ mode: "look_suggest", kind, id: m[2] })
        .then((out) => live && setLook({ state: "done", tags: lookList(out) }))
        .catch(() => live && setLook({ state: "idle" }));
      return () => {
        live = false;
      };
    }, [key]);
    if (!m) return null;

    async function go() {
      setAi({ state: "loading" });
      try {
        const out = await ask({ mode: "ai_suggest", kind, id: m[2] });
        const list = out || [];
        setAi({
          state: "done",
          tags: list.filter((t) => t.source !== "new").map((t) => ({ id: String(t.id), name: t.name, aliases: [], source: t.source || "ai", why: t.why })),
          ideas: list.filter((t) => t.source === "new").map((t) => t.name),
        });
      } catch (e) {
        setAi({ state: "error", message: e.message });
      }
    }

    async function goLook() {
      setLook({ state: "loading" });
      try {
        const out = await ask({ mode: "look_suggest", kind, id: m[2] });
        setLook({ state: "done", tags: lookList(out) });
      } catch (e) {
        setLook({ state: "error", message: e.message });
      }
    }

    const have = new Set((values || []).map((t) => String(t.id)));
    const haveNames = new Set((values || []).map((t) => String(t.name || "").toLowerCase()));
    // tags you don't have yet (from "Ask AI for tags"): tapping one makes it
    const ideas = ai.state === "done" ? (ai.ideas || []).filter((n) => !haveNames.has(n.toLowerCase())) : [];
    async function addNew(name) {
      try {
        let tag;
        try {
          tag = (await gql("mutation ($i: TagCreateInput!) { tagCreate(input: $i) { id name } }", { i: { name } })).tagCreate;
        } catch (e) {
          // made in the meantime (or differently capitalised): use that one
          const d = await gql("query ($n: String!) { findTags(tag_filter: { name: { value: $n, modifier: EQUALS } }) { tags { id name } } }", { n: name });
          tag = d.findTags.tags[0];
          if (!tag) throw e;
        }
        onSelect([...(values || []), { id: String(tag.id), name: tag.name, aliases: [] }]);
      } catch (e) {
        setAi((st) => ({ ...st, newError: "Couldn't make \"" + name + "\": " + e.message }));
      }
    }
    const aiTags = ai.state === "done" ? ai.tags.filter((t) => !have.has(t.id)) : [];
    const shown = new Set(aiTags.map((t) => t.id));
    const lookTags = look.state === "done" ? look.tags.filter((t) => !have.has(t.id) && !shown.has(t.id)) : [];
    const strip = (t) => ({ id: t.id, name: t.name, aliases: [] });
    const add = (list) => onSelect([...(values || []), ...list.map(strip)]);
    const all = [...aiTags, ...lookTags];
    const SOURCE = {
      both: "Picked by the AI and backed up by look-alikes you've tagged",
      looks: "Most look-alikes you've tagged have it",
      ai: "Picked by the AI model (look-alikes not scanned yet)",
      body: "Shown in it (body-part detector); sizes from the performer",
    };
    const SURE = new Set(["both", "looks", "body"]);
    const busy = (st) => st.state === "loading";
    if (!can("tags", cz)) return null;
    return h(
      "div",
      { className: "pts-suggest pts-ai bta-tags" },
      h("span", { className: "pts-label" }, "AI"),
      ai.state === "idle" && can("ollama", cz) &&
        h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: go, title: "Ask the local AI model (Ollama) which of your tags fit, checked against look-alikes" }, "Ask AI for tags"),
      look.state === "idle" && can("looks", cz) &&
        h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: goLook, title: "Tags of the scenes and images you've tagged that look most like this one (quick, no AI model)" }, "Tags from look-alikes"),
      (busy(ai) || busy(look)) && h("span", { className: "pts-ai-note" }, "Looking at it…"),
      ai.state === "error" &&
        h("span", { className: "pts-ai-note" }, "Couldn't ask the AI: " + ai.message + " ",
          h("button", { type: "button", className: "pts-chip", onClick: go }, "Try again")),
      look.state === "error" &&
        h("span", { className: "pts-ai-note" }, "Look-alikes: " + look.message + " ",
          h("button", { type: "button", className: "pts-chip", onClick: goLook }, "Try again")),
      (ai.state === "done" || look.state === "done") && !busy(ai) && !busy(look) && !all.length && !ideas.length &&
        h("span", { className: "pts-ai-note" }, (ai.tags || []).length + (look.tags || []).length ? "All suggestions are already added" : "No suggestions"),
      aiTags.map((t) =>
        h("button", { key: "a" + t.id, type: "button", className: "pts-chip" + (SURE.has(t.source) ? " bta-sure" : ""), title: (SOURCE[t.source] || SOURCE.ai) + (t.why ? " - " + t.why : ""), onClick: () => add([t]) },
          "+ " + t.name + (SURE.has(t.source) ? " ✓" : ""))
      ),
      lookTags.map((t) =>
        h("button", { key: "l" + t.id, type: "button", className: "pts-chip" + (t.sure ? " bta-sure" : ""),
          title: Math.round(t.score * 100) + "% of the closest look-alikes you've tagged have it" + (t.sure ? " (sure)" : ""), onClick: () => add([t]) },
          "+ " + t.name + " " + Math.round(t.score * 100) + "%" + (t.sure ? " ✓" : ""))
      ),
      all.length > 1 && h("button", { type: "button", className: "pts-chip pts-all", onClick: () => add(all) }, "Add all"),
      ideas.length > 0 && h("span", { className: "pts-ai-note bta-new-label" }, "Not tags yet:"),
      ideas.map((n) =>
        h("button", { key: "n" + n, type: "button", className: "pts-chip bta-new",
          title: "You don't have this tag yet - tap to make it and add it", onClick: () => addNew(n) },
          "+ " + n + " (new)")
      ),
      ai.newError && h("span", { className: "pts-ai-note" }, ai.newError)
    );
  }

  // ---- the bulk Edit window (several scenes / images selected) -------------
  // Look-alike tags for the selected items: the ones that suit at least a
  // quarter of them, with how many ("+ Beach 4/6"). Quick, no AI model.
  function BulkAiTags({ values, onSelect }) {
    const ref = React.useRef(null);
    const [sel, setSel] = React.useState(null); // {kind, ids}
    const [st, setSt] = React.useState({ state: "idle" });
    React.useEffect(() => {
      const modal = ref.current && ref.current.closest(".modal");
      const head = modal && (modal.querySelector(".modal-header, .modal-title") || {}).textContent || "";
      if (!modal || /filter/i.test(head)) return;
      const cards = [...document.querySelectorAll(".card-check:checked")].map((c) => c.closest(".scene-card, .image-card")).filter(Boolean);
      const kinds = new Set(cards.map((c) => (c.classList.contains("scene-card") ? "scene" : "image")));
      if (!cards.length || kinds.size !== 1) return;
      const kind = [...kinds][0];
      const re = kind === "scene" ? /\/scenes\/(\d+)/ : /\/images\/(\d+)/;
      const ids = [...new Set(cards.map((c) => ((c.querySelector(`a[href*="/${kind}s/"]`) || {}).getAttribute?.("href") || "").match(re)).filter(Boolean).map((m) => m[1]))];
      if (!ids.length) return;
      setSel({ kind, ids });
      let live = true;
      setSt({ state: "loading" });
      ask({ mode: "look_suggest_many", kind, ids })
        .then((out) => live && setSt({ state: "done", ...out }))
        .catch((e) => live && setSt({ state: "error", message: e.message }));
      return () => { live = false; };
    }, []);
    const have = new Set((values || []).map((t) => String(t.id)));
    const tags = st.state === "done" ? (st.tags || []).filter((t) => !have.has(String(t.id))) : [];
    const strip = (t) => ({ id: String(t.id), name: t.name, aliases: [] });
    return h("span", { ref, style: sel ? null : { display: "none" } },
      sel && h("div", { className: "pts-suggest pts-ai bta-tags bta-bulk-tags" },
        h("span", { className: "pts-label" }, "AI"),
        st.state === "loading" && h("span", { className: "pts-ai-note" }, `Looking at the ${sel.ids.length} selected…`),
        st.state === "error" && h("span", { className: "pts-ai-note" }, st.message),
        st.state === "done" && !tags.length && h("span", { className: "pts-ai-note" }, "No look-alike tags for these"),
        tags.map((t) => h("button", {
          key: t.id, type: "button", className: "pts-chip",
          title: `${t.count} of the ${t.of} selected look like items you've tagged ${t.name}`,
          onClick: () => onSelect([...(values || []), strip(t)]),
        }, `+ ${t.name} `, h("small", null, `${t.count}/${t.of}`))),
        tags.length > 1 && h("button", { type: "button", className: "pts-chip pts-all",
          onClick: () => onSelect([...(values || []), ...tags.filter((t) => t.count * 2 >= t.of).map(strip)]),
          title: "Add the ones that suit half or more of them" }, "Add common")));
  }

  api.patch.after("TagSelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    // the edit form's Tags box (not filters, and not Better Organizing Tools'
    // own bulk windows)
    if (!props.isMulti || !props.onSelect || props.isDisabled || props.ptsNoSuggest) return result;
    if (!ITEM_PAGE.test(location.pathname)) {
      // a list page: only in the bulk Edit window (BulkAiTags checks)
      return h(React.Fragment, null, result, h(Gate, { need: "looks" }, h(BulkAiTags, { values: props.values, onSelect: props.onSelect })));
    }
    return h(React.Fragment, null, result, h(AiTags, { values: props.values, onSelect: props.onSelect }));
  });

  // ---- "Look-alikes are in" under the Galleries box (images) ---------------
  // The 20 photos that look most like this one (picture fingerprints) vote
  // for the galleries they're in, closer ones counting more (the same way
  // look-alike tags are scored). Only galleries made in Stash: a photo can't
  // be put in a zip or folder gallery.
  const IMAGE_EDIT = /^\/images\/(\d+)/;
  const LOOK_K = 20, LOOK_TEMP = 0.02, GALLERY_CUT = 0.2;
  const galleryGuesses = new Map(); // image id -> promise

  async function lookGalleries(imageId) {
    const out = await ask({ mode: "look_similar", kind: "image", id: imageId });
    if (!out || !out.known) return { known: false, list: [] };
    const near = (out.items || []).slice(0, LOOK_K);
    if (!near.length) return { known: true, list: [] };
    // one alias per photo, so a deleted photo doesn't sink the whole query
    const q = near.map(([id], i) => `i${i}: findImage(id: "${String(id).replace(/\D/g, "")}") { galleries { id title files { path } folder { path } } }`).join(" ");
    const d = await gql(`{ ${q} }`);
    const top = near[0][1] / 100;
    const votes = new Map();
    let total = 0;
    near.forEach(([, sc], i) => {
      const img = d["i" + i];
      if (!img) return;
      const w = Math.exp((sc / 100 - top) / LOOK_TEMP);
      total += w;
      img.galleries.forEach((g) => {
        if (g.files.length || g.folder || !(g.title || "").trim()) return;
        const v = votes.get(g.id) || { gallery: { id: g.id, title: g.title }, w: 0, n: 0 };
        v.w += w;
        v.n += 1;
        votes.set(g.id, v);
      });
    });
    const list = [...votes.values()]
      .map((v) => ({ ...v, share: total ? v.w / total : 0 }))
      .filter((v) => v.share >= GALLERY_CUT)
      .sort((x, y) => y.share - x.share)
      .slice(0, 5);
    return { known: true, list };
  }

  function LookGalleries({ values, onSelect }) {
    const m = location.pathname.match(IMAGE_EDIT);
    const [st, setSt] = React.useState({ state: "loading" });
    React.useEffect(() => {
      if (!m) return undefined;
      let live = true;
      setSt({ state: "loading" });
      if (!galleryGuesses.has(m[1])) {
        const p = lookGalleries(m[1]);
        p.catch(() => galleryGuesses.delete(m[1]));
        galleryGuesses.set(m[1], p);
      }
      galleryGuesses.get(m[1])
        .then((r) => live && setSt({ state: "done", ...r }))
        .catch(() => live && setSt({ state: "error" }));
      return () => (live = false);
    }, [m && m[1]]);
    if (!m || st.state !== "done") return null;
    const have = new Set((values || []).map((g) => String(g.id)));
    const shown = st.list.filter((v) => !have.has(String(v.gallery.id)));
    if (!shown.length) return null;
    const asOption = (g) => ({ id: g.id, title: g.title, files: [], folder: null });
    return h(
      "div",
      { className: "pts-suggest pts-ai bta-galleries" },
      h("span", { className: "pts-label" }, "AI"),
      shown.map((v) =>
        h("button", {
          key: v.gallery.id, type: "button", className: "pts-chip",
          title: `${v.n} of the ${LOOK_K} photos that look most like this one are in it (${Math.round(v.share * 100)}% of the look-alike vote)`,
          onClick: () => onSelect([...(values || []), asOption(v.gallery)]),
        }, "+ " + v.gallery.title + " " + Math.round(v.share * 100) + "%")
      )
    );
  }

  api.patch.after("GallerySelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    if (!props.isMulti || !props.onSelect || props.isDisabled || !IMAGE_EDIT.test(location.pathname)) return result;
    return h(React.Fragment, null, result, h(Gate, { need: "looks" }, h(LookGalleries, { values: props.values, onSelect: props.onSelect })));
  });

  // ---- "Find performers by face" under the Performers box ------------------
  function FaceRow({ values, onSelect }) {
    const m = location.pathname.match(ITEM_PAGE);
    const [st, setSt] = React.useState({ state: "idle" });
    // the performer whose bigger picture is showing: on hover with a mouse,
    // or after a tap on a phone (where a tap shows it, and "+ Add" adds)
    const [peek, setPeek] = React.useState(null);
    const [hovering, setHovering] = React.useState(false); // peek came from a mouse hover
    const lastPointer = React.useRef("mouse");
    const lastTouch = React.useRef(0);
    // phones send made-up mouse events after a tap (and when the page
    // scrolls under a finger): only a real mouse may hover
    const realMouse = (e) =>
      e.pointerType === "mouse" && Date.now() - lastTouch.current > 1500 &&
      !(window.matchMedia && window.matchMedia("(hover: none)").matches);
    const peekRef = React.useRef(null);
    // a tapped card can open under the phone's bottom bar: bring it into view
    React.useEffect(() => {
      if (peek && !hovering && peekRef.current) {
        const bar = document.querySelector(".mui-bottom-nav");
        const r = peekRef.current.getBoundingClientRect();
        const bottom = window.innerHeight - (bar ? bar.offsetHeight : 0) - 8;
        if (r.bottom > bottom) window.scrollBy({ top: r.bottom - bottom, behavior: "smooth" });
      }
    }, [peek, hovering]);
    const key = m ? m[1] + m[2] : "";
    React.useEffect(() => {
      setSt({ state: "idle" });
      setPeek(null);
    }, [key]);
    if (!m) return null;

    async function go() {
      setPeek(null);
      setSt({ state: "loading" });
      try {
        const out = await ask({ mode: "face_suggest", kind: m[1] === "scenes" ? "scene" : "image", id: m[2] });
        setSt({ state: "done", cands: out || [] });
      } catch (e) {
        setSt({ state: "error", message: e.message });
      }
    }

    const have = new Set((values || []).map((p) => String(p.id)));
    const cands = st.state === "done" ? st.cands.filter((c) => !have.has(String(c.id))) : [];
    const add = (c) => {
      setPeek(null);
      onSelect([...(values || []), { id: String(c.id), name: c.name, disambiguation: null, alias_list: [], image_path: null }]);
      // you've said it's them: remember that face as theirs too
      ask({ mode: "face_confirm", performer: c.id, emb: c.emb }).catch(() => {});
    };
    return h(
      "div",
      { className: "pts-suggest pts-ai bta-faces" },
      h("span", { className: "pts-label" }, "Faces"),
      st.state === "idle" &&
        h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: go,
          title: "Compare the faces in it with your performers' (needs the face helper)" }, "Find performers by face"),
      st.state === "loading" && h("span", { className: "pts-ai-note" }, "Looking at the faces…"),
      st.state === "error" &&
        h("span", { className: "pts-ai-note" }, "Couldn't check faces: " + st.message + " ",
          h("button", { type: "button", className: "pts-chip", onClick: go }, "Try again")),
      st.state === "done" && !cands.length &&
        h("span", { className: "pts-ai-note" }, st.cands.length ? "Everyone it found is already added" : "No one it knows"),
      cands.map((c) =>
        h("button", {
          key: c.id, type: "button",
          className: "pts-chip bta-face-chip" + (c.sure ? " bta-sure" : "") + (peek && peek.id === c.id ? " bta-peeking" : ""),
          onPointerDown: (e) => {
            lastPointer.current = e.pointerType;
            if (e.pointerType !== "mouse") lastTouch.current = Date.now();
          },
          onPointerEnter: (e) => {
            if (e.pointerType !== "mouse") lastTouch.current = Date.now();
            if (!realMouse(e)) return;
            lastPointer.current = "mouse";
            setHovering(true);
            setPeek(c);
          },
          onPointerLeave: (e) => {
            if (realMouse(e) && hovering) setPeek((p) => (p && p.id === c.id ? null : p));
          },
          // a mouse click adds; a tap (finger / pen) shows the bigger picture first
          onClick: () => {
            if (lastPointer.current === "mouse") return add(c);
            setHovering(false);
            setPeek((p) => (p && p.id === c.id ? null : c));
          },
        },
          h("img", { className: "bta-face-pic", src: `/performer/${c.id}/image`, alt: "", loading: "lazy" }),
          h("span", null, "+ " + c.name + " " + c.score + "%" + (c.sure ? " ✓" : "")))
      ),
      // put the suggestions away again (the button comes back)
      (st.state === "done" || st.state === "error") &&
        h("button", { type: "button", className: "pts-chip bta-hide", onClick: () => { setPeek(null); setSt({ state: "idle" }); },
          title: "Hide these suggestions" }, "✕ Hide"),
      peek && cands.some((c) => c.id === peek.id) &&
        h("div", { ref: peekRef, className: "bta-face-peek" + (hovering ? " bta-hover" : "") },
          h("img", { src: `/performer/${peek.id}/image`, alt: peek.name }),
          h("div", { className: "bta-peek-text" },
            h("strong", null, peek.name),
            h("span", null, `${peek.score}% alike` + (m[1] === "scenes" ? `, closest in ${peek.pictures} of the frames` : "")),
            h("span", null, peek.sure ? "✓ A confident match" : "Check it's really them"),
            hovering
              ? h("span", { className: "bta-peek-hint" }, "Click to add")
              : h("div", { className: "bta-peek-buttons" },
                  h("button", { type: "button", className: "pts-chip bta-sure", onClick: () => add(peek) }, "+ Add " + peek.name),
                  h("button", { type: "button", className: "pts-chip", onClick: () => setPeek(null) }, "✕ Close"))))
    );
  }

  api.patch.after("PerformerSelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    if (!props.isMulti || !props.onSelect || props.isDisabled || !ITEM_PAGE.test(location.pathname)) return result;
    return h(React.Fragment, null, result, h(Gate, { need: "faces" }, h(FaceRow, { values: props.values, onSelect: props.onSelect })));
  });

  // ---- performer page: "Might also be in" ----------------------------------
  // Scanned scenes and images without this performer where a face looks like
  // theirs. Tick the right ones and add them in one go.
  function mountLookalikes(anchor, id) {
    const box = document.createElement("div");
    box.className = "bta-lookalikes";
    box.dataset.performer = id;
    const head = document.createElement("div");
    head.className = "pts-suggest";
    const label = document.createElement("span");
    label.className = "pts-label";
    label.textContent = "Faces";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pts-chip pts-ai-ask";
    btn.textContent = "Might also be in…";
    btn.title = "Scenes and images without them where someone looks like them (from the face scan)";
    const note = document.createElement("span");
    note.className = "pts-ai-note";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "pts-chip bta-la-close";
    close.textContent = "✕ Close";
    close.title = "Put the list away";
    close.hidden = true;
    const review = document.createElement("button");
    review.type = "button";
    review.className = "pts-chip";
    review.textContent = "Review one by one";
    review.title = "Swipe through their strong face matches (Review page)";
    review.addEventListener("click", () => goTo(`${REVIEW_PATH}?performer=${id}`));
    head.append(label, btn, review, note, close);
    const list = document.createElement("div");
    list.className = "bta-la-list";
    const bar = document.createElement("div");
    bar.className = "bta-la-bar";
    box.append(head, list, bar);
    anchor.insertAdjacentElement("beforebegin", box);

    let items = [];
    close.addEventListener("click", () => {
      items = [];
      note.textContent = "";
      btn.textContent = "Might also be in…";
      render();
    });
    function render() {
      list.textContent = "";
      bar.textContent = "";
      close.hidden = !items.length;
      items.forEach((it) => {
        const card = document.createElement("label");
        card.className = "bta-la-card" + (it.sure ? " bta-sure" : "");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = it.checked;
        cb.addEventListener("change", () => {
          it.checked = cb.checked;
          count();
        });
        const img = document.createElement("img");
        img.loading = "lazy";
        img.alt = "";
        img.src = it.kind === "scene" ? `/scene/${it.id}/screenshot` : `/image/${it.id}/thumbnail`;
        const info = document.createElement("span");
        info.className = "bta-la-info";
        const link = document.createElement("a");
        link.href = `/${it.kind}s/${it.id}`;
        link.target = "_blank";
        link.textContent = it.title || `${it.kind} ${it.id}`;
        const score = document.createElement("span");
        score.className = "bta-la-score";
        score.textContent = `${it.score}% alike` + (it.kind === "scene" ? ` · ${it.pictures} frame${it.pictures === 1 ? "" : "s"}` : "") +
          (it.sure ? " ✓" : it.rival >= it.score ? ` · looks more like someone else (${it.rival}%)` : "");
        const nope = document.createElement("button");
        nope.type = "button";
        nope.className = "bta-la-not";
        nope.textContent = "✕ Not them";
        nope.title = "Remove it from the list and don't suggest it for them again";
        nope.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          ask({ mode: "face_reject", performer: id, key: `${it.kind}:${it.id}` }).catch(() => {});
          items = items.filter((x) => x !== it);
          if (!items.length) note.textContent = "That's all of them.";
          render();
        });
        info.append(link, score, nope);
        card.append(cb, img, info);
        list.appendChild(card);
      });
      if (!items.length) return;
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "btn btn-primary btn-sm";
      const all = document.createElement("button");
      all.type = "button";
      all.className = "btn btn-secondary btn-sm";
      all.textContent = "Select all";
      all.addEventListener("click", () => {
        items.forEach((it) => (it.checked = true));
        render();
      });
      const none = document.createElement("button");
      none.type = "button";
      none.className = "btn btn-secondary btn-sm";
      none.textContent = "Select none";
      none.addEventListener("click", () => {
        items.forEach((it) => (it.checked = false));
        render();
      });
      function count() {
        const n = items.filter((it) => it.checked).length;
        addBtn.textContent = `Add them to ${n} selected`;
        addBtn.disabled = !n;
      }
      addBtn.addEventListener("click", async () => {
        const picked = items.filter((it) => it.checked);
        addBtn.disabled = true;
        addBtn.textContent = "Adding…";
        try {
          for (const [kind, mutation, type] of [["scene", "bulkSceneUpdate", "BulkSceneUpdateInput"], ["image", "bulkImageUpdate", "BulkImageUpdateInput"]]) {
            const ids = picked.filter((it) => it.kind === kind).map((it) => it.id);
            if (ids.length) {
              await gql(`mutation($i: ${type}!) { ${mutation}(input: $i) { id } }`, {
                i: { ids, performer_ids: { ids: [id], mode: "ADD" } },
              });
            }
          }
          // learn from the faces you've said are them
          picked.slice(0, 12).forEach((it) => ask({ mode: "face_confirm", performer: id, emb: it.emb }).catch(() => {}));
          items = items.filter((it) => !it.checked);
          note.textContent = `Added to ${picked.length}. Reload the page to see them in the tabs below.`;
          render();
        } catch (e) {
          note.textContent = "Couldn't add them: " + e.message;
          count();
        }
      });
      bar.append(addBtn, all, none);
      count();
    }

    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "Looking…";
      note.textContent = "";
      ask({ mode: "face_lookalikes", id })
        .then((r) => {
          items = (r || []).map((it) => ({ ...it, checked: !!it.sure }));
          note.textContent = items.length
            ? `${items.length} found. Confident matches (✓) are ticked; check the others.`
            : "No one else looks like them in what's been scanned";
          render();
          btn.textContent = "Look again";
        })
        .catch((e) => {
          note.textContent = "Couldn't look: " + e.message;
          btn.textContent = "Try again";
        })
        .finally(() => (btn.disabled = false));
    });
  }

  function checkPerformerPage() {
    const m = location.pathname.match(PERF_PAGE);
    if (!m || !can("faces")) return;
    const tabs = document.querySelector("#performer-page .performer-tabs");
    if (!tabs) return;
    const old = document.querySelector(".bta-lookalikes");
    if (old && old.dataset.performer === m[1] && old.nextElementSibling === tabs) return;
    if (old) old.remove();
    mountLookalikes(tabs, m[1]);
  }

  // ---- performer page: "Ask AI for details" ---------------------------------
  // Hair / eye colour, tattoos and piercings from their pictures; each
  // suggestion can be put into its (empty) box in the edit form.
  const PERF_PAGE = /^\/performers\/(\d+)/;
  const DETAIL_LABELS = { hair_color: "Hair colour", eye_color: "Eye colour", tattoos: "Tattoos", piercings: "Piercings" };

  function setFieldValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function mountDetails(form, id) {
    const box = document.createElement("div");
    box.className = "pts-suggest bta-details";
    box.dataset.performer = id;
    const label = document.createElement("span");
    label.className = "pts-label";
    label.textContent = "AI";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pts-chip pts-ai-ask";
    btn.textContent = "Ask AI for details";
    btn.title = "Look at their pictures and suggest hair colour, eye colour, tattoos and piercings";
    const out = document.createElement("span");
    out.className = "bta-chips";
    box.append(label, btn, out);
    const anchor = form.querySelector("#hair_color");
    const row = anchor && anchor.closest(".form-group");
    (row || form.firstElementChild).insertAdjacentElement("beforebegin", box);

    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "Looking at their pictures…";
      out.textContent = "";
      ask({ mode: "perf_details_one", id })
        .then((r) => {
          const keys = Object.keys(DETAIL_LABELS).filter((k) => r && r[k]);
          if (!keys.length) out.textContent = "Nothing clear to suggest";
          keys.forEach((k) => {
            const field = form.querySelector("#" + k);
            const b = document.createElement("button");
            b.type = "button";
            b.className = "pts-chip";
            const filled = field && field.value.trim();
            b.textContent = `${DETAIL_LABELS[k]}: ${r[k]}` + (filled ? " (has one)" : "");
            b.title = filled ? `Replace "${field.value}" with this` : "Put this in the box (then Save)";
            b.addEventListener("click", () => {
              if (field) {
                setFieldValue(field, r[k]);
                b.classList.add("bta-used");
              }
            });
            out.appendChild(b);
          });
          btn.textContent = "Ask again";
        })
        .catch((e) => {
          out.textContent = "Couldn't ask the AI: " + e.message;
          btn.textContent = "Try again";
        })
        .finally(() => (btn.disabled = false));
    });
  }

  function checkPerformerForm() {
    const m = location.pathname.match(PERF_PAGE);
    if (!m || !can("ollama")) return;
    const field = document.querySelector("form #hair_color");
    if (!field) return;
    const form = field.closest("form");
    const old = form.querySelector(".bta-details");
    if (old && old.dataset.performer === m[1]) return;
    if (old) old.remove();
    mountDetails(form, m[1]);
  }

  // ---- "AI titles" under the Title box --------------------------------------
  const TITLE_PAGE = /^\/(scenes|images|galleries)\/(\d+)/;
  const KIND_OF = { scenes: "scene", images: "image", galleries: "gallery" };

  function setInputValue(input, value) {
    // React keeps its own copy of the value: use the native setter and fire
    // "input" so the form sees the change
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
  }

  function mountAiTitles(input, item) {
    const [kind, id] = item.split(":");
    const row = document.createElement("div");
    row.className = "pts-suggest bta-titles";
    row.dataset.item = item;
    const label = document.createElement("span");
    label.className = "pts-label";
    label.textContent = "AI titles";
    const chips = document.createElement("span");
    chips.className = "bta-chips";
    const note = document.createElement("span");
    note.className = "pts-ai-note";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "pts-chip pts-ai-ask";
    btn.textContent = "Ask AI for titles";
    btn.title = "Ask the local AI model (Ollama) to look at it and suggest titles";
    row.append(label, chips, note, btn);
    placeTitleRow(input, row);

    const shown = new Set();
    btn.addEventListener("click", () => {
      btn.disabled = true;
      btn.textContent = "Looking at it…";
      note.textContent = "";
      ask({ mode: "title_ideas", kind, id })
        .then((r) => {
          const titles = (r && r.ai) || [];
          titles.forEach((t) => {
            if (!t || shown.has(t.toLowerCase())) return;
            shown.add(t.toLowerCase());
            const b = document.createElement("button");
            b.type = "button";
            b.className = "pts-chip bta-title";
            b.textContent = t;
            b.title = "Suggested by the local AI model - click to use";
            b.addEventListener("click", () => setInputValue(input, t));
            chips.appendChild(b);
          });
          if (!titles.length) note.textContent = "No ideas";
          btn.textContent = "Ask AI again";
        })
        .catch((e) => {
          note.textContent = "Couldn't ask the AI: " + e.message;
          btn.textContent = "Try again";
        })
        .finally(() => (btn.disabled = false));
    });
  }

  // Right under the Title box, or under Better Organizing Tools' "Title ideas"
  // row if that's there
  function placeTitleRow(input, row) {
    const after = input.nextElementSibling && input.nextElementSibling.classList.contains("bt-title-ideas")
      ? input.nextElementSibling
      : input;
    if (after.nextElementSibling !== row) after.insertAdjacentElement("afterend", row);
  }

  let titleTimer = null;
  function checkTitles() {
    const m = location.pathname.match(TITLE_PAGE);
    if (!m) return;
    const item = KIND_OF[m[1]] + ":" + m[2];
    document.querySelectorAll("form input#title").forEach((input) => {
      const box = input.parentElement;
      const row = box.querySelector(":scope > .bta-titles");
      if (row && row.dataset.item !== item) row.remove();
      if (row && row.dataset.item === item) placeTitleRow(input, row);
      else mountAiTitles(input, item);
    });
  }

  // ---- Scene edit: "Suggest covers" under Cover Image --------------------
  // Frames from across the video, each with Apple's score (0-100) where the
  // face helper runs on a Mac - best first - or, without it (e.g. Windows),
  // the same frames in video order. "Use this cover" saves it straight away;
  // the old cover is kept, so Undo (or the "put back" task) restores it.
  const COVER_KIND = { stash: "made by Stash", scraped: "scraped or uploaded", picked: "picked from the video", stitched: "made by Vertical Stitcher" };
  const coverClock = (t) => { t = Math.max(0, Math.round(t)); const m = Math.floor(t / 60), sec = t % 60, hr = Math.floor(m / 60);
    return (hr ? hr + ":" + String(m % 60).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0"); };
  const scoreClass = (v) => (v == null ? "" : v >= 65 ? " good" : v >= 50 ? " ok" : " low");

  // The best covers, but not several of the same shot (as the add-on picks
  // them each round): each one has to differ from those before it by
  // COVER_APART (tiny grey pictures, "sig"), lowered when there aren't enough.
  const COVER_APART = 22;
  const COVER_SHOW_MAX = 36;  // suggestions shown at most (9 more each "Compare more frames")
  const sigBytes = (b64) => { try { return Uint8Array.from(atob(b64 || ""), (c) => c.charCodeAt(0)); } catch (e) { return null; } };
  const COVER_WINDOW = 25;  // variety among covers scoring this close to the best
  function variedCovers(list, n) {
    const sigs = list.map((x) => sigBytes(x.sig));
    const apart = (a, b) => { let t = 0; for (let i = 0; i < a.length; i++) t += Math.abs(a[i] - b[i]); return t / a.length; };
    const top = Math.max(-1, ...list.map((x) => x.score ?? -1));
    const idx = list.map((_, i) => i);
    const near = idx.filter((i) => list[i].score == null || list[i].score >= top - COVER_WINDOW);
    const mid = idx.filter((i) => !near.includes(i) && list[i].score >= top - 1.4 * COVER_WINDOW);
    const far = idx.filter((i) => !near.includes(i) && !mid.includes(i));
    const picked = [];
    const fill = (pool, bar) => {
      for (const i of pool) {
        if (picked.length >= n) return;
        if (picked.includes(i)) continue;
        const g = sigs[i];
        if (g && g.length && picked.some((j) => sigs[j] && sigs[j].length === g.length && apart(g, sigs[j]) < bar)) continue;
        picked.push(i);
      }
    };
    for (const bar of [COVER_APART, COVER_APART * 0.7, COVER_APART * 0.4]) { fill(near, bar); fill(mid, bar); }
    [near, mid, far].forEach((pool) => fill(pool, 0));
    return picked.sort((a, b) => a - b).map((i) => list[i]);
  }


  function mountCoverIdeas(group, id) {
    const wrap = document.createElement("div");
    wrap.className = "bta-covers";
    wrap.dataset.scene = id;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary bta-covers-go";
    btn.textContent = "✨ Suggest covers";
    btn.title = "Frames from across the video to use as the cover" + "";
    const panel = document.createElement("div");
    panel.className = "bta-covers-panel";
    wrap.append(btn, panel);
    group.appendChild(wrap);
    let round = 0;
    let kept = [];     // the frames shown: the best so far (with scores), else the latest round
    let compared = 0;  // frames compared in all
    const preview = () => group.querySelector("img.scene-cover");
    // (Stash's cover address only changes once a second: make sure the new picture shows)
    const fresh = (url) => url + (url.includes("?") ? "&" : "?") + "bta=" + Date.now();
    // the new cover in the form: shown at once, and Stash's own copy of the
    // scene refreshed, so the form keeps showing it when it redraws
    async function showCover(url) {
      if (preview()) preview().src = fresh(url);
      try { await api.utils.StashService.getClient().refetchQueries({ include: "active" }); } catch (e) { /* shown already */ }
      const img = preview();
      if (img && !img.src.includes((url.split("?")[1] || "").split("&")[0])) img.src = fresh(url);
    }
    const status = (text, extra) => {
      let st = panel.querySelector(".bta-covers-status");
      if (!st) { st = document.createElement("div"); st.className = "bta-covers-status"; panel.prepend(st); }
      st.textContent = text;
      if (extra) st.appendChild(extra);
    };
    function undoButton() {
      const u = document.createElement("button");
      u.type = "button";
      u.className = "btn btn-link btn-sm";
      u.textContent = "Undo - put the old cover back";
      u.addEventListener("click", () => {
        u.disabled = true;
        ask({ mode: "cover_undo", id }).then((r) => {
          showCover(r.screenshot);
          status("✓ The old cover is back.");
          panel.querySelectorAll(".bta-cover.chosen").forEach((c) => c.classList.remove("chosen"));
        }).catch((e) => { status("Couldn't undo: " + e.message); u.disabled = false; });
      });
      return u;
    }
    function show(r) {
      panel.innerHTML = "";
      const cur = r.current || {};
      compared = (round ? compared : 0) + (r.looked || r.frames.length);
      let fresh = 0;
      // each "Compare more frames" shows 9 more (up to 36): the best so far,
      // varied; without scores, the new frames join the ones shown
      const showing = Math.min(COVER_SHOW_MAX, 9 * (round + 1));
      if (r.scored && round) {
        const all = kept.map((x) => ({ ...x, isNew: false }));
        r.frames.forEach((fr) => { if (!all.some((x) => Math.abs(x.at - fr.at) < 1)) all.push({ ...fr, isNew: true }); });
        all.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
        kept = variedCovers(all, showing);
        fresh = kept.filter((x) => x.isNew).length;
      } else if (round) {
        const all = kept.map((x) => ({ ...x, isNew: false }));
        r.frames.forEach((fr) => { if (!all.some((x) => Math.abs(x.at - fr.at) < 1)) all.push({ ...fr, isNew: true }); });
        kept = all.slice(-COVER_SHOW_MAX).sort((a, b) => a.at - b.at);
        fresh = kept.filter((x) => x.isNew).length;
      } else {
        kept = r.frames.map((x) => ({ ...x, isNew: false }));
      }
      r = { ...r, frames: kept };
      status((cur.score != null ? `Current cover: ${cur.score}/100` : "Current cover") +
        (cur.kind ? ` · ${COVER_KIND[cur.kind] || cur.kind}` : "") +
        (r.scored ? ` · the best ${r.frames.length} of ${compared} frames compared` +
            (round ? (fresh ? ` (${fresh} new)` : " (none of the new ones were better)") : "")
          : " · scores need the face helper on a Mac, so frames are in video order") +
        (r.vertical ? " · a vertical video: frames cut to wide covers around the people" : ""));
      const grid = document.createElement("div");
      grid.className = "bta-covers-grid";
      // with Vertical Stitcher: stitched covers first, in a row of their own
      const sgrid = document.createElement("div");
      sgrid.className = "bta-covers-grid bta-covers-stitched";
      const grids = () => [grid, sgrid];
      const makeCard = (fr, i, into) => {
        const card = document.createElement("div");
        card.className = "bta-cover";
        const img = document.createElement("img");
        img.src = "data:image/jpeg;base64," + fr.image;
        img.alt = "Frame at " + coverClock(fr.at);
        img.loading = "lazy";
        const meta = document.createElement("div");
        meta.className = "bta-cover-meta";
        const t = document.createElement("span");
        t.textContent = fr.stitch ? fr.stitch.map(coverClock).join(" + ") : coverClock(fr.at);
        meta.appendChild(t);
        if (fr.score != null) {
          const sc = document.createElement("span");
          sc.className = "bta-cover-score" + scoreClass(fr.score);
          sc.textContent = (i === 0 && r.scored && !fr.stitch ? "Best · " : "") + fr.score + "/100";
          if (fr.isNew) {
            const nw = document.createElement("span");
            nw.className = "bta-cover-new";
            nw.textContent = "New";
            meta.appendChild(nw);
          }
          if (cur.score != null && fr.score > cur.score) sc.title = `${fr.score - cur.score} more than the current cover`;
          meta.appendChild(sc);
        }
        const use = document.createElement("button");
        use.type = "button";
        use.className = "btn btn-primary btn-sm";
        use.textContent = fr.stitch ? "Use this stitched cover" : "Use this cover";
        use.addEventListener("click", () => {
          grids().forEach((g) => g.querySelectorAll("button").forEach((b) => (b.disabled = true)));
          use.textContent = "Saving…";
          ask({ mode: "cover_use", id, at: fr.at, ...(fr.stitch ? { stitch: fr.stitch } : {}) }).then((res) => {
            showCover(res.screenshot);
            panel.querySelectorAll(".bta-cover.chosen").forEach((c) => c.classList.remove("chosen"));
            card.classList.add("chosen");
            // the suggestions fold away; they can be opened again
            const shown = [grid, sgrid, more, panel.querySelector(".bta-covers-head")].filter(Boolean);
            shown.forEach((el) => (el.style.display = "none"));
            status(fr.stitch ? `✓ Stitched cover saved (${fr.stitch.map(coverClock).join(" + ")}). `
              : `✓ Cover saved (the frame at ${coverClock(fr.at)}). `, res.backup ? undoButton() : null);
            const again = document.createElement("button");
            again.type = "button";
            again.className = "btn btn-link btn-sm";
            again.textContent = "Show the suggestions again";
            again.addEventListener("click", () => { shown.forEach((el) => (el.style.display = "")); again.remove(); });
            panel.querySelector(".bta-covers-status").appendChild(again);
          }).catch((e) => status("Couldn't save it: " + e.message))
            .finally(() => { grids().forEach((g) => g.querySelectorAll("button").forEach((b) => (b.disabled = false))); use.textContent = fr.stitch ? "Use this stitched cover" : "Use this cover"; });
        });
        card.append(img, meta, use);
        into.appendChild(card);
      };
      if ((r.stitched || []).length) {
        const head = document.createElement("div");
        head.className = "bta-covers-head";
        head.textContent = "Stitched like Vertical Stitcher" + (r.scored ? " - from the best frames, and evenly spaced" : " - evenly spaced") + ":";
        r.stitched.forEach((fr, i) => makeCard(fr, i, sgrid));
        panel.append(head, sgrid);
      }
      r.frames.forEach((fr, i) => makeCard(fr, i, grid));
      panel.appendChild(grid);
      if (!r.frames.length) status("No frames could be taken from this video.");
      const more = document.createElement("button");
      more.type = "button";
      more.className = "btn btn-secondary btn-sm bta-covers-more";
      more.textContent = r.scored ? "Compare more frames" : "More frames";
      more.addEventListener("click", () => { round += 1; load(); });
      panel.appendChild(more);
    }
    function load() {
      btn.disabled = true;
      btn.textContent = "Looking through the video…";
      panel.querySelectorAll("button").forEach((b) => (b.disabled = true));
      ask({ mode: "cover_ideas", id, more: round })
        .then(show)
        .catch((e) => { panel.innerHTML = ""; status("Couldn't get frames: " + e.message); })
        .finally(() => { btn.disabled = false; btn.textContent = "✨ Suggest covers again"; });
    }
    btn.addEventListener("click", () => { round = 0; load(); });
  }

  function checkCovers() {
    const m = location.pathname.match(TITLE_PAGE);
    if (!m || m[1] !== "scenes") return;
    const label = document.querySelector('#scene-edit-details label[for="cover_image"]');
    const group = label && label.closest(".form-group");
    if (!group) return;
    const have = group.querySelector(":scope > .bta-covers");
    if (have && have.dataset.scene === m[2]) return;
    if (have) have.remove();
    mountCoverIdeas(group, m[2]);
  }

  // ---- Performer edit: "Suggest profile pictures" ---------------------------
  // Their photos and scene frames cut to a portrait around their face (known
  // faces only, else pictures with one face), best Apple score first on a
  // Mac, else closest face. "Use this picture" puts it in Stash's own form
  // through its Set image file picker, so it shows at once, Save keeps it and
  // Cancel drops it.
  const PERFORMER_PAGE = /^\/performers\/(\d+)/;

  async function putInPerformerForm(form, b64) {
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const file = new File([bytes], "profile.jpg", { type: "image/jpeg" });
    const scope = form.parentElement || document;
    const setBtn = [...scope.querySelectorAll("button")].find((b) => /set image/i.test(b.textContent || ""));
    if (!setBtn) throw new Error("Stash's Set image button wasn't found");
    setBtn.click();
    let input = null;
    for (let i = 0; i < 30 && !input; i++) {
      await new Promise((r) => setTimeout(r, 50));
      input = document.querySelector('#set-image-popover input[type="file"]');
    }
    if (!input) throw new Error("Stash's Set image menu didn't open");
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 50));
    if (document.querySelector("#set-image-popover")) setBtn.click(); // (close the menu)
  }

  function mountProfileIdeas(form, id) {
    const wrap = document.createElement("div");
    wrap.className = "bta-covers bta-profiles";
    wrap.dataset.performer = id;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary bta-covers-go";
    btn.textContent = "✨ Suggest profile pictures";
    btn.title = "Their photos and scene frames, cut to a portrait around their face";
    const panel = document.createElement("div");
    panel.className = "bta-covers-panel";
    wrap.append(btn, panel);
    form.parentElement.insertBefore(wrap, form);
    let round = 0, kept = [], compared = 0;
    const status = (text, extra) => {
      let st = panel.querySelector(".bta-covers-status");
      if (!st) { st = document.createElement("div"); st.className = "bta-covers-status"; panel.prepend(st); }
      st.textContent = text;
      if (extra) st.appendChild(extra);
    };
    function show(r) {
      panel.innerHTML = "";
      compared = (round ? compared : 0) + (r.looked || 0);
      const all = (round ? kept.map((x) => ({ ...x, isNew: false })) : []);
      r.pictures.forEach((x) => { if (!all.some((y) => y.key === x.key)) all.push({ ...x, isNew: !!round }); });
      if (r.scored) all.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
      kept = r.scored ? all.slice(0, 9) : all.slice(-9);
      const fresh = kept.filter((x) => x.isNew).length;
      const cur = r.current || {};
      status((cur.score != null ? `Current picture: ${cur.score}/100 · ` : "") +
        (kept.length ? (r.scored ? `the best ${kept.length} of ${compared} pictures looked at` : `${kept.length} pictures, closest face first (scores need the face helper on a Mac)`) +
          (round && r.scored ? (fresh ? ` (${fresh} new)` : " (none of the new ones were better)") : "")
          : `no pictures with ${r.known ? "their face" : "one face (their face isn't known yet: run \"Faces: learn performers' faces\")"} in ${compared} looked at`));
      const grid = document.createElement("div");
      grid.className = "bta-covers-grid";
      kept.forEach((x, i) => {
        const card = document.createElement("div");
        card.className = "bta-cover";
        const img = document.createElement("img");
        img.src = "data:image/jpeg;base64," + x.image;
        img.alt = "From " + x.from;
        img.loading = "lazy";
        const meta = document.createElement("div");
        meta.className = "bta-cover-meta";
        const from = document.createElement("a");
        from.href = x.link;
        from.target = "_blank";
        from.textContent = x.from.length > 28 ? x.from.slice(0, 27) + "…" : x.from;
        from.title = x.from;
        meta.appendChild(from);
        if (x.isNew) { const nw = document.createElement("span"); nw.className = "bta-cover-new"; nw.textContent = "New"; meta.appendChild(nw); }
        if (x.score != null) {
          const sc = document.createElement("span");
          sc.className = "bta-cover-score" + scoreClass(x.score);
          sc.textContent = (i === 0 && r.scored ? "Best · " : "") + x.score + "/100";
          if (x.match != null) sc.title = `face ${x.match}% like theirs`;
          meta.appendChild(sc);
        } else if (x.match != null) {
          const m = document.createElement("span");
          m.className = "bta-cover-score";
          m.textContent = `face ${x.match}%`;
          meta.appendChild(m);
        }
        const use = document.createElement("button");
        use.type = "button";
        use.className = "btn btn-primary btn-sm";
        use.textContent = "Use this picture";
        use.addEventListener("click", () => {
          const f = document.querySelector("form#performer-edit");
          if (!f) { status("The edit form is closed - open Edit again."); return; }
          putInPerformerForm(f, x.image).then(() => {
            grid.querySelectorAll(".bta-cover.chosen").forEach((c) => c.classList.remove("chosen"));
            card.classList.add("chosen");
            grid.style.display = "none";
            more.style.display = "none";
            status("✓ In the form - press Save to keep it (Cancel drops it). ");
            const again = document.createElement("button");
            again.type = "button";
            again.className = "btn btn-link btn-sm";
            again.textContent = "Show the suggestions again";
            again.addEventListener("click", () => { grid.style.display = ""; more.style.display = ""; again.remove(); });
            panel.querySelector(".bta-covers-status").appendChild(again);
          }).catch((e) => status("Couldn't put it in the form: " + e.message));
        });
        card.append(img, meta, use);
        grid.appendChild(card);
      });
      panel.appendChild(grid);
      const more = document.createElement("button");
      more.type = "button";
      more.className = "btn btn-secondary btn-sm bta-covers-more";
      more.textContent = "Look at more pictures";
      more.style.display = r.more ? "" : "none";
      more.addEventListener("click", () => { round += 1; load(); });
      panel.appendChild(more);
    }
    function load() {
      btn.disabled = true;
      btn.textContent = "Looking for their face…";
      panel.querySelectorAll("button").forEach((b) => (b.disabled = true));
      ask({ mode: "profile_ideas", id, more: round })
        .then(show)
        .catch((e) => { panel.innerHTML = ""; status("Couldn't get pictures: " + e.message); })
        .finally(() => { btn.disabled = false; btn.textContent = "✨ Suggest profile pictures again"; });
    }
    btn.addEventListener("click", () => { round = 0; load(); });
  }

  function checkProfiles() {
    const m = location.pathname.match(PERFORMER_PAGE);
    const form = m && document.querySelector("form#performer-edit");
    const have = document.querySelector(".bta-profiles");
    if (!form) { if (have) have.remove(); return; }
    if (have && have.dataset.performer === m[1] && have.nextElementSibling === form) return;
    if (have) have.remove();
    mountProfileIdeas(form, m[1]);
  }

  // ---- Settings → Tasks: this plugin's tasks in sections --------------------
  // Same idea as Better Organizing Tools: a heading above each group of tasks
  // that shows / hides them (closed to start with; remembered per browser).
  const TASK_GROUPS = [
    ["ai-setup", "Setup & schedules", "The setup check, and your nightly and after-scan jobs, run now",
      ["Setup: check everything and write a report",
       "Schedules: run the nightly jobs now",
       "Schedules: run the after-scan jobs now",
       "Schedules: run tonight's waiting steps for new items now"]],
    ["ai-tags", "Tags", "Tags from the AI model, look-alikes and the body detector, and poses (scan look-alikes first)",
      ["Tags: scan scenes and images for look-alikes (do this first)",
       "Tags: tag scenes and images with no tags (AI model)",
       "Tags: tag all scenes and images not done yet (AI model)",
       "Tags: tag scenes and images with no tags (look-alikes)",
       "Tags: add body tags to scenes and images not done yet",
       "Tags: add clothing tags from the look (quick)",
       "Tags: redo body tags on all scenes and images",
       "Tags: scan images' body poses for pose suggestions (Mac)",
       "Tags: preview on 10 (AI model, no changes)",
       "Tags: preview on 10 (look-alikes, no changes)",
       "Tags: preview body tags on 10 (no changes)",
       "Tags: preview clothing tags from the look (no changes)"]],
    ["ai-faces", "Faces", "Who's in scenes and images, from your performers' faces",
      ["Faces: scan scenes and images",
       "Faces: learn performers' faces",
       "Faces: learn more from what you've tagged",
       "Faces: find performers in scenes without any",
       "Faces: find performers in images without any",
       "Faces: find performers who may be the same person",
       "Faces: preview on 10 (no changes)"]],
    ["ai-markers", "Markers & positions", "AI markers where scenes shift, marker tags, and positions in videos",
      ["Markers: download the video model (needed first)",
       "Markers: add AI markers to scenes without markers",
       "Markers: redo AI markers with the newest method",
       "Markers: add their tags to their scenes",
       "Markers: scan scenes for positions (for Review)",
       "Markers: preview AI markers on 5 scenes (no changes)",
       "Markers: preview adding their tags to scenes (no changes)"]],
    ["ai-titles", "Titles", "Titles from the AI model for scenes and images without one",
      ["Titles: title scenes without one (AI model)",
       "Titles: title images without one (AI model)",
       "Titles: preview on 10 (AI model, no changes)"]],
    ["ai-studios", "Studios & links", "Studios, performers and links from accounts and watermarks",
      ["Studios: find scenes' studios from accounts and watermarks",
       "Studios: find images' studios from watermarks (not in galleries)",
       "Studios: find images' studios from watermarks (galleries too)",
       "Links: add account and written links to scenes and images",
       "Studios: preview on 10 scenes (no changes)",
       "Studios: preview on 10 images (no changes)",
       "Links: preview (no changes)"]],
    ["ai-pictures", "Pictures", "Tag and marker pictures, and images' aesthetic scores",
      ["Tag pictures: pick the most typical for tags without one",
       "Tag pictures: pick the most typical for every tag (replaces yours)",
       "Tag pictures: replace blurry or dark ones (Mac)",
       "Marker pictures: replace blurry, dark or empty ones (Mac)",
       "Image scores: add aesthetic scores and labels (Mac)",
       "Tag pictures: preview the most typical (no changes)",
       "Tag & marker pictures: preview replacing blurry ones (Mac, no changes)"]],
    ["ai-covers", "Scene covers", "Better-looking scene covers (Apple's scores, on a Mac), and putting the old ones back",
      ["Scene covers: replace low-scoring ones (Mac)",
       "Scene covers: put back the originals",
       "Scene covers: preview on 10 (Mac, no changes)"]],
    ["ai-screens", "Screenshots", "Cut phone screenshots of posts down to the photo",
      ["Screenshots: crop to the photo",
       "Screenshots: put back the originals",
       "Screenshots: preview cropping (no changes)"]],
    ["ai-perf", "Performer details", "Hair and eye colour from performers' pictures",
      ["Performer details: fill in empty hair and eye colour",
       "Performer details: preview on 10 (no changes)"]],
  ];
  const OPEN_KEY = "bta-task-groups-open";
  const GROUP_NAMES = new Set(TASK_GROUPS.flatMap((g) => g[3]));

  function openGroups() {
    try {
      return new Set(JSON.parse(localStorage.getItem(OPEN_KEY) || "[]"));
    } catch (e) {
      return new Set();
    }
  }

  function groupTasks() {
    if (!location.pathname.startsWith("/settings")) return;
    const rows = [...document.querySelectorAll(".setting > div > h3")]
      .filter((h3) => GROUP_NAMES.has(h3.textContent.trim()))
      .map((h3) => h3.closest(".setting"));
    if (rows.length < 2) return;
    const section = rows[0].parentElement;
    const open = openGroups();
    for (const [key, title, blurb, names] of TASK_GROUPS) {
      const mine = rows.filter((r) => r.parentElement === section && names.includes(r.querySelector("h3").textContent.trim()));
      if (!mine.length) continue;
      let head = section.querySelector(`.bt-task-group[data-group="${key}"]`);
      if (!head) {
        head = document.createElement("button");
        head.type = "button";
        head.className = "bt-task-group";
        head.dataset.group = key;
        head.innerHTML = '<span class="bt-tg-arrow" aria-hidden="true"></span><span class="bt-tg-title"></span>' +
          '<span class="bt-tg-count"></span><span class="bt-tg-blurb"></span>';
        head.querySelector(".bt-tg-title").textContent = title;
        head.querySelector(".bt-tg-blurb").textContent = blurb;
        head.addEventListener("click", () => {
          const now = openGroups();
          now.has(key) ? now.delete(key) : now.add(key);
          try {
            localStorage.setItem(OPEN_KEY, JSON.stringify([...now]));
          } catch (e) {}
          groupTasks();
        });
      }
      if (head.nextElementSibling !== mine[0]) section.insertBefore(head, mine[0]);
      const isOpen = open.has(key);
      head.classList.toggle("open", isOpen);
      head.setAttribute("aria-expanded", String(isOpen));
      // only when it changes: writing it counts as a page change, which
      // would run this again (and again)
      const count = `${mine.length} task${mine.length > 1 ? "s" : ""}`;
      const countEl = head.querySelector(".bt-tg-count");
      if (countEl.textContent !== count) countEl.textContent = count;
      mine.forEach((r) => r.classList.toggle("bt-task-hidden", !isOpen));
    }
  }

  // ---- Settings → Tasks: a check before a task starts ----------------------
  // Pressing one of this add-on's tasks first asks the add-on whether what it
  // needs is set up (the AI model, the face helper, Apple's scores, the video
  // model, the look-alike scan) - if not, it points to the setup page - and
  // roughly how long it will take. A long one (10 minutes or more) gets a
  // warning: Run now, Add to the nightly run, Don't show this again (for that
  // task, in this browser), or Close. Quick, ready tasks just start.
  const NOWARN_KEY = "bta-task-nowarn";
  // (the tasks were renamed in 1.8: choices saved with the old names still count)
  const TASK_RENAMES = {"Setup check: write a report to the log": "Setup: check everything and write a report", "Nightly jobs: run now": "Schedules: run the nightly jobs now", "After-scan jobs: run now": "Schedules: run the after-scan jobs now", "AI: run tonight's new items now": "Schedules: run tonight's waiting steps for new items now", "Look-alike tags: scan scenes and images": "Tags: scan scenes and images for look-alikes (do this first)", "AI tagging: items with no tags": "Tags: tag scenes and images with no tags (AI model)", "AI tagging: everything not done yet": "Tags: tag all scenes and images not done yet (AI model)", "Look-alike tags: items with no tags": "Tags: tag scenes and images with no tags (look-alikes)", "Body tags: scenes and images": "Tags: add body tags to scenes and images not done yet", "Body tags: clothing from the look (quick)": "Tags: add clothing tags from the look (quick)", "Body tags: check everything again": "Tags: redo body tags on all scenes and images", "Poses: read body poses": "Tags: scan images' body poses for pose suggestions (Mac)", "AI tagging: preview on 10 items (no changes)": "Tags: preview on 10 (AI model, no changes)", "Look-alike tags: preview on 10 items (no changes)": "Tags: preview on 10 (look-alikes, no changes)", "Body tags: preview on 10 items (no changes)": "Tags: preview body tags on 10 (no changes)", "Body tags: preview clothing from the look (no changes)": "Tags: preview clothing tags from the look (no changes)", "Faces: learn from what you've tagged": "Faces: learn more from what you've tagged", "Faces: scenes without performers": "Faces: find performers in scenes without any", "Faces: images without performers": "Faces: find performers in images without any", "Faces: preview on 10 items (no changes)": "Faces: preview on 10 (no changes)", "Video model: download it (for AI markers and positions)": "Markers: download the video model (needed first)", "AI markers: scenes without markers": "Markers: add AI markers to scenes without markers", "AI markers: redo with the newest method": "Markers: redo AI markers with the newest method", "Positions in videos: look at scenes": "Markers: scan scenes for positions (for Review)", "AI markers: preview on 5 scenes (no changes)": "Markers: preview AI markers on 5 scenes (no changes)", "Markers: preview adding their tags to their scenes (no changes)": "Markers: preview adding their tags to scenes (no changes)", "AI titles: scenes without a title": "Titles: title scenes without one (AI model)", "AI titles: images without a title": "Titles: title images without one (AI model)", "AI titles: preview on 10 items (no changes)": "Titles: preview on 10 (AI model, no changes)", "Studios from accounts: also read watermarks with the AI": "Studios: find scenes' studios from accounts and watermarks", "Studios from watermarks: photos without a studio, not in a gallery": "Studios: find images' studios from watermarks (not in galleries)", "Studios from watermarks: photos without a studio, galleries too": "Studios: find images' studios from watermarks (galleries too)", "Links: add account and written links to scenes and photos": "Links: add account and written links to scenes and images", "Studios from accounts: preview on 10 scenes (no changes)": "Studios: preview on 10 scenes (no changes)", "Studios from watermarks: preview on 10 photos without a studio (no changes)": "Studios: preview on 10 images (no changes)", "Tag pictures: most typical one for tags without a picture": "Tag pictures: pick the most typical for tags without one", "Tag pictures: most typical one for every tag (replaces existing ones)": "Tag pictures: pick the most typical for every tag (replaces yours)", "Pictures: replace blurry or dark tag pictures": "Tag pictures: replace blurry or dark ones (Mac)", "Pictures: replace blurry, dark or empty marker pictures": "Marker pictures: replace blurry, dark or empty ones (Mac)", "Pictures: aesthetic score and labels": "Image scores: add aesthetic scores and labels (Mac)", "Tag pictures: preview the most typical ones (no changes)": "Tag pictures: preview the most typical (no changes)", "Pictures: preview better tag and marker pictures (no changes)": "Tag & marker pictures: preview replacing blurry ones (Mac, no changes)", "Scene covers: replace low-scoring ones": "Scene covers: replace low-scoring ones (Mac)", "Scene covers: preview on 10 scenes (no changes)": "Scene covers: preview on 10 (Mac, no changes)", "Screenshots: preview cropping to the photo (no changes)": "Screenshots: preview cropping (no changes)", "Performer details: preview on 10 performers (no changes)": "Performer details: preview on 10 (no changes)"};
  const noWarn = () => {
    try { return new Set(JSON.parse(localStorage.getItem(NOWARN_KEY) || "[]").map((t) => TASK_RENAMES[t] || t)); } catch (e) { return new Set(); }
  };
  const setNoWarn = (task) => { try { const n = noWarn(); n.add(task); localStorage.setItem(NOWARN_KEY, JSON.stringify([...n])); } catch (e) { /* this time only */ } };
  const howLong = (sec) => {
    const m = Math.max(1, Math.round(sec / 60));
    if (m < 60) return `about ${m} minute${m === 1 ? "" : "s"}`;
    const hr = Math.floor(m / 60), rest = m % 60;
    if (hr >= 24) return `about ${Math.round(hr / 24 * 10) / 10} days`;
    return `about ${hr} hour${hr === 1 ? "" : "s"}` + (rest >= 5 && hr < 10 ? ` ${rest} min` : "");
  };
  const n0 = (n) => Number(n || 0).toLocaleString();
  let taskBypass = false;

  function gateDialog() {
    let wrap = document.getElementById("bta-gate");
    if (wrap) wrap.remove();
    wrap = document.createElement("div");
    wrap.id = "bta-gate";
    wrap.className = "bta-gate";
    wrap.innerHTML = '<div class="bta-gate-box" role="dialog" aria-modal="true" aria-labelledby="bta-gate-title">' +
      '<button type="button" class="bta-gate-x" aria-label="Close">✕</button><h4 id="bta-gate-title"></h4><div class="bta-gate-body"></div>' +
      '<div class="bta-gate-actions"></div></div>';
    const close = () => { wrap.remove(); document.removeEventListener("keydown", esc, true); };
    const esc = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", esc, true);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) close(); });
    wrap.querySelector(".bta-gate-x").addEventListener("click", close);
    document.body.appendChild(wrap);
    const button = (label, cls, onClick) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn btn-sm " + cls;
      b.textContent = label;
      b.addEventListener("click", onClick);
      wrap.querySelector(".bta-gate-actions").appendChild(b);
      return b;
    };
    return { wrap, close, button, title: wrap.querySelector("h4"), body: wrap.querySelector(".bta-gate-body") };
  }

  async function taskGate(task, runBtn) {
    const run = () => { taskBypass = true; try { runBtn.click(); } finally { taskBypass = false; } };
    let dlg = null;
    const slow = setTimeout(() => {
      dlg = gateDialog();
      dlg.title.textContent = task;
      dlg.body.textContent = "Checking what this task needs…";
      dlg.button("Close", "btn-secondary", dlg.close);
    }, 400);
    let r;
    try {
      r = await ask({ mode: "task_check", task });
    } catch (e) {
      clearTimeout(slow);
      if (dlg) dlg.close();
      run(); // (the check itself failed: don't stand in the way)
      return;
    }
    clearTimeout(slow);
    if (dlg && !document.body.contains(dlg.wrap)) return; // closed while checking
    const missing = (r.needs || []).filter((n) => !n.ok);
    if (!missing.length && (!r.long || noWarn().has(task))) {
      if (dlg) dlg.close();
      run();
      return;
    }
    if (dlg) dlg.close();
    dlg = gateDialog();
    dlg.title.textContent = task;
    const p = (text, cls) => { const el = document.createElement("p"); if (cls) el.className = cls; el.textContent = text; dlg.body.appendChild(el); return el; };
    if (missing.length) {
      p("This task needs something that isn't set up yet:");
      const ul = document.createElement("ul");
      missing.forEach((m) => {
        const li = document.createElement("li");
        const b = document.createElement("b");
        b.textContent = m.what;
        li.append(b, document.createTextNode(m.fix ? " - " + m.fix : ""));
        ul.appendChild(li);
      });
      dlg.body.appendChild(ul);
      p("The setup page walks you through it, step by step.", "bta-gate-note");
      dlg.button("Open the setup page", "btn-primary", () => { dlg.close(); goTo("/ai-setup"); });
      dlg.button("Close", "btn-secondary", dlg.close);
      return;
    }
    const what = [r.scenes ? `${n0(r.scenes)} scene${r.scenes === 1 ? "" : "s"}` : "",
      r.items ? `${n0(r.items)} ${r.noun || "photos"}` : ""].filter(Boolean).join(" and ");
    p(`This could take a while: ${howLong(r.seconds)}` + (what ? ` for ${what} still to do.` : "."));
    p("It runs in the background (Settings → Tasks shows its progress). You can cancel it there and run it again later - it carries on where it stopped.", "bta-gate-note");
    const chk = document.createElement("label");
    chk.className = "bta-gate-check";
    chk.innerHTML = '<input type="checkbox"> Don’t show this again for this task';
    dlg.body.appendChild(chk);
    const remember = () => { if (chk.querySelector("input").checked) setNoWarn(task); };
    dlg.button("Run now", "btn-primary", () => { remember(); dlg.close(); run(); });
    const night = dlg.button(r.nightly ? `In the nightly run (${r.night_time})` : `Add to the nightly run (${r.night_time})`, "btn-secondary", () => {
      remember();
      night.disabled = true;
      night.textContent = "Adding…";
      ask({ mode: "task_add_nightly", task })
        .then((x) => { night.classList.add("bta-gate-done"); night.textContent = `✓ Added - it runs tonight at ${x.night_time}`; setTimeout(dlg.close, 2500); })
        .catch((e) => { night.disabled = false; night.textContent = "Couldn't add it: " + e.message; });
    });
    if (r.nightly) night.disabled = true;
    dlg.button("Close", "btn-secondary", dlg.close);
  }

  document.addEventListener("click", (e) => {
    if (taskBypass || !location.pathname.startsWith("/settings")) return;
    const btn = e.target instanceof Element && e.target.closest(".setting button");
    const row = btn && btn.closest(".setting");
    const h3 = row && row.querySelector(":scope > div > h3");
    if (!h3 || !GROUP_NAMES.has(h3.textContent.trim())) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    taskGate(h3.textContent.trim(), btn);
  }, true);

  // ---- Settings → Plugins: this add-on's settings in sections --------------
  // Stash lists plugin settings alphabetically by their internal key, which
  // scatters related ones. Each setting row's id is plugin-<id>-<key>, so they
  // are put in sections here by display order alone (CSS order; Stash's own
  // elements aren't moved) under headings; the night time and job lists are
  // set on the jobs page, so they're replaced by a link to it.
  const SETTING_SECTIONS = [
    ["Setup", ["aiOllamaUrl", "aiModel", "faceHelperUrl", "welcomeOff"]],
    ["New items", ["aiAuto", "aiNewAll", "aiNewPhotoStudios", "faceAutoAssign", "aiNightly"]],
    ["Tags & titles", ["aiTagKinds", "aiTagCount", "aiMaxTags", "aiSkipTagged", "aiIncludeOrganized", "aiNoAutoTag", "aiLookSuggest", "tagSynonyms", "aiTitleKinds"]],
    ["Faces", ["faceOnlyFavourites", "faceMinItems", "faceAutoMin", "faceSuggestMin"]],
    ["Markers & poses", ["aiMarkerTag", "markerTagsStayOnMarker", "poseTags"]],
    ["Scene covers", ["coverReplaceScraped", "coverReplacePicked", "coverReplaceOrganized"]],
  ];
  const SETTINGS_ON_JOBS_PAGE = ["aiNightTime", "aiTimeZone", "nightJobs", "afterScanJobs"];
  function arrangeSettings() {
    if (!location.pathname.startsWith("/settings")) return;
    const rowOf = (k) => {
      let e = document.getElementById(`plugin-${PLUGIN_ID}-${k}`);
      while (e && e.parentElement && !e.parentElement.classList.contains("plugin-settings")) e = e.parentElement;
      return e && e.parentElement ? e : null;
    };
    const first = rowOf("aiModel");
    const box = first && first.parentElement;
    if (!box || box.dataset.btaArranged === "1") return;
    box.dataset.btaArranged = "1";
    box.classList.add("bta-settings-arranged");
    const head = (text, order, extra) => {
      const d = document.createElement("div");
      d.className = "bta-settings-head";
      d.style.order = String(order);
      d.textContent = text;
      if (extra) d.appendChild(extra);
      box.appendChild(d);
    };
    SETTING_SECTIONS.forEach(([title, keys], i) => {
      head(title, i * 100);
      keys.forEach((k, j) => {
        const row = rowOf(k);
        if (row) row.style.order = String(i * 100 + j + 1);
      });
    });
    // anything not listed (e.g. a new setting): at the end, still visible
    [...box.children].forEach((c) => { if (!c.style.order) c.style.order = "9000"; });
    SETTINGS_ON_JOBS_PAGE.forEach((k) => {
      const row = rowOf(k);
      if (row) row.style.display = "none";
    });
    const a = document.createElement("a");
    a.href = "/ai-nightly";
    a.className = "bta-settings-jobs";
    a.textContent = "🌙 Night time, time zone and what runs at night and after a scan: on the Nightly & after-scan jobs page";
    a.addEventListener("click", (e) => { e.preventDefault(); goTo("/ai-nightly"); });
    head("Schedules", SETTING_SECTIONS.length * 100, a);
  }

  // "Setup & troubleshooting" links: above this add-on's tasks (Settings →
  // Tasks) and under its settings (Settings → Plugins)
  function setupLinks() {
    if (!location.pathname.startsWith("/settings")) return;
    const make = (where) => {
      const a = document.createElement("a");
      a.href = SETUP_PAGE;
      a.className = "bta-setup-link";
      a.dataset.where = where;
      a.innerHTML = '<span aria-hidden="true">🩺</span> <b>Setup &amp; troubleshooting</b><span> - check the AI model, face helper and scans, with fixes for common problems</span>';
      a.addEventListener("click", (e) => { e.preventDefault(); goTo(SETUP_PAGE); });
      return a;
    };
    // tasks: before the first of our task groups
    const first = document.querySelector('.bt-task-group[data-group="ai-night"], .bt-task-group[data-group^="ai-"]');
    if (first && !document.querySelector('.bta-setup-link[data-where="tasks"]')) {
      first.parentElement.insertBefore(make("tasks"), first);
      const n = document.createElement("a");
      n.href = "/ai-nightly";
      n.className = "bta-setup-link";
      n.dataset.where = "tasks-night";
      n.innerHTML = '<span aria-hidden="true">🌙</span> <b>Nightly &amp; after-scan jobs</b><span> - pick which tasks (Stash\'s and plugins\') run every night and after a scan</span>';
      n.addEventListener("click", (e) => { e.preventDefault(); goTo("/ai-nightly"); });
      first.parentElement.insertBefore(n, first);
    }
    // plugins: under our plugin's name
    if (/[?&]tab=plugins/.test(location.search) && !document.querySelector('.bta-setup-link[data-where="plugins"]')) {
      // the plugin's row (its name) at the top of its collapsible group
      const head = [...document.querySelectorAll(".setting-group .setting > div > h3")]
        .find((e) => /Better (Organizing|Tagging) AI Add-on/.test(e.textContent || ""));
      const row = head && head.closest(".setting");
      if (row) row.after(make("plugins"));
    }
  }

  new MutationObserver(() => {
    if (titleTimer) return; // (at most every 200 ms, even on a page that never stops changing)
    titleTimer = setTimeout(() => {
      titleTimer = null;
      checkTitles();
      checkCovers();
      checkProfiles();
      checkPerformerForm();
      checkPerformerPage();
      groupTasks();
      setupLinks();
      arrangeSettings();
    }, 200);
  }).observe(document.body, { childList: true, subtree: true });

  // ---- Tags list: "Most typical picture (AI)" in the ••• menu --------------
  // For the selected tags: the scene or photo that looks most like everything
  // else with that tag (one no other tag has), set as the tag's picture. The
  // cards update straight away.
  const typical = { request: null, listeners: new Set() };

  function TypicalHost() {
    const Toast = api.hooks.useToast();
    React.useEffect(() => {
      const run = async (ids) => {
        let changed = 0, same = 0, few = 0, taken = 0;
        if (ids.length > 1) Toast.success(`Finding the most typical pictures for ${ids.length} tags…`);
        for (const id of ids) {
          const cards = [...document.querySelectorAll(".tag-card")].filter((c) =>
            c.querySelector(`a[href="/tags/${id}"], a[href^="/tags/${id}?"]`)
          );
          cards.forEach((c) => c.classList.add("pts-repicking"));
          try {
            const r = (await ask({ mode: "typical_one", tag_id: String(id) })) || {};
            if (r.changed && r.link) {
              changed++;
              // (the add-on's link says localhost: keep this page's address)
              let link = r.link;
              try { const u = new URL(r.link, location.origin); link = u.pathname + u.search; } catch (e) { /* as is */ }
              cards.forEach((c) => c.querySelectorAll("img.tag-card-image").forEach((img) => (img.src = link)));
            } else if (r.reason === "same") same++;
            else if (r.reason === "few") few++;
            else taken++;
          } catch (e) {
            taken++;
            console.warn("[Better Organizing AI] typical picture failed", e);
          }
          cards.forEach((c) => c.classList.remove("pts-repicking"));
        }
        const s = (n) => (n === 1 ? "" : "s");
        const notes = [];
        if (same) notes.push(`${same} already had ${same === 1 ? "it" : "theirs"}`);
        if (few) notes.push(`${few} ${few === 1 ? "is" : "are"} on too few scanned items (run "Tags: scan scenes and images for look-alikes (do this first)")`);
        if (taken) notes.push(`${taken} had no typical picture of ${taken === 1 ? "its" : "their"} own left`);
        if (changed) Toast.success(`Most typical picture set for ${changed} tag${s(changed)}` + (notes.length ? ` (${notes.join("; ")})` : ""));
        else if (same && !few && !taken) Toast.success(ids.length === 1 ? "This tag already has its most typical picture" : "These tags already have their most typical pictures");
        else Toast.error(new Error(notes.join("; ") || "No typical picture found"));
      };
      const fn = () => {
        const ids = typical.request;
        typical.request = null;
        if (ids && ids.length) run(ids);
      };
      typical.listeners.add(fn);
      return () => typical.listeners.delete(fn);
    }, []);
    return null;
  }

  // ---- Review page (/review; /faces is its Faces tab) -----------------------
  // Strong face matches across the library, one at a time: swipe right (or
  // "Yes") to add the performer, left ("Not them") to never suggest it
  // again, or skip. Undo takes the last answer back. ?performer=ID for just
  // one performer (the "Review one by one" link on their page).
  const REVIEW_PATH = "/faces";   // the Faces tab (kept for "Review one by one" links)
  const REVIEW_PAGE = "/review";  // ?mode=faces | tags
  const goTo = (path) => {
    window.history.pushState({}, "", path);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  const FACE_ICON =
    '<svg class="svg-inline--fa nav-menu-icon d-block d-xl-inline mb-2 mb-xl-0" viewBox="0 0 24 24" width="1em" height="1em" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/>' +
    '<circle cx="12" cy="10" r="3"/><path d="M7.5 17.5c1-2 2.6-3 4.5-3s3.5 1 4.5 3"/></svg>';

  // Check cards: a tag, performer or gallery ("t" / "p" / "g") on an item
  const CHECK_FIELD = { t: "tag_ids", p: "performer_ids", g: "gallery_ids" };
  const checkAvatar = (what, id) => (what === "t" ? `/tag/${id}/image` : what === "g" ? `/gallery/${id}/cover` : `/performer/${id}/image`);

  // what each tab asks for, and what Yes / Not them / Undo do
  const REVIEW_MODES = {
    faces: {
      label: "Faces",
      param: "performer",
      load: (only, skip) => ask({ mode: "face_review", performer: only || null, limit: 40, skip, favourites: (new URLSearchParams(location.search).get("fav") === "1") }),  // (never kept)
      keyOf: (it) => `${it.kind}:${it.id}:${it.performer}`,
      yes: async (it) => {
        await setOnItem(it, "performer_ids", it.performer, "ADD");
        ask({ mode: "face_confirm", performer: it.performer, emb: it.emb }).catch(() => {});
      },
      no: (it) => ask({ mode: "face_reject", performer: it.performer, key: `${it.kind}:${it.id}` }),
      undoYes: (it) => setOnItem(it, "performer_ids", it.performer, "REMOVE"),
      undoNo: (it) => ask({ mode: "face_unreject", performer: it.performer, key: `${it.kind}:${it.id}` }),
      avatar: (it) => `/performer/${it.performer}/image`,
      page: (it) => `/performers/${it.performer}`,
      question: (it) => ["Is this ", h("b", null, it.name), "?"],
      meta: (it, pct) => `${pct(it.score)} alike` +
        (it.kind === "scene"
          ? (it.times && it.times.length ? ` · seen at ${it.times.map(clock).join(", ")}`
             : ` · best match in ${it.pictures} frame${it.pictures === 1 ? "" : "s"}`)
          : "") +
        ` · next closest ${pct(it.rival)}`,
      extra: (it) => (it.others ? ` · has ${it.others} other performer${it.others === 1 ? "" : "s"}` : ""),
      noLabel: "✕ Not them", yesLabel: "✓ Yes, add", noStamp: "NOT THEM",
      empty: "No strong face matches left. Run \"Faces: scan scenes and images\" after adding new items, or \"Faces: learn performers' faces\" after adding performers.",
      filterLabel: "Just one performer",
    },
    tags: {
      label: "Tags",
      param: "tag",
      load: (only, skip, fresh) => ask({ mode: "tag_review", tag: only || null, limit: 40, skip, fresh: !!fresh }),
      keyOf: (it) => `${it.kind}:${it.id}:${it.tag}`,
      yes: (it) => setOnItem(it, "tag_ids", it.tag, "ADD"),
      no: (it) => ask({ mode: "tag_review_answer", key: `${it.kind}:${it.id}`, tag: it.tag, no: true }),
      undoYes: async (it) => {
        await setOnItem(it, "tag_ids", it.tag, "REMOVE");
        if (it.for_scene) {
          // (the scene didn't have it: Stash's hook added it, maybe a moment later)
          const off = () => setOnItem({ kind: "scene", id: it.scene }, "tag_ids", it.tag, "REMOVE");
          await off();
          setTimeout(off, 4000);
        }
      },
      undoNo: (it) => ask({ mode: "tag_review_answer", key: `${it.kind}:${it.id}`, tag: it.tag, no: false }),
      avatar: (it) => `/tag/${it.tag}/image`,
      page: (it) => `/tags/${it.tag}`,
      question: (it) => [it.kind === "marker" ? "Tag this marker " : "Tag it ", h("b", null, it.name), "?"],
      meta: (it, pct) => [it.near ? `${pct(it.score)} sure · ${it.near} of its 20 closest look-alikes have it` : "",
        it.frames ? `seen in ${it.frames} of ${it.looked} video frames` + (it.times && it.times.length ? ` · at ${it.times.map(clock).join(", ")}` : "") : "",
        it.scene_has ? "its scene has this tag" : "", it.why || "", it.aesthetic != null ? `looks ${it.aesthetic}/100` : ""]
        .filter(Boolean).join(" · "),
      tileMeta: (it, pct) => [it.near ? `${pct(it.score)} · ${it.near}/20 look-alikes` : "",
        it.frames ? `▶ ${it.frames}/${it.looked} frames` : "", it.scene_has ? "scene has it" : "", it.why ? "near-miss" : ""].filter(Boolean).join(" · "),
      extra: (it) => ` · has ${it.tags} tag${it.tags === 1 ? "" : "s"}`,
      noLabel: "✕ No", yesLabel: "✓ Yes, add", noStamp: "NO",
      empty: "No confident tags left. New ones show up after \"Tags: scan scenes and images for look-alikes (do this first)\" and as you tag more.",
      filterLabel: "Just one tag",
    },
    // what's already on items that might not belong: a tag look-alikes say
    // doesn't fit, a performer whose face isn't in it. Right = it fits
    // (keep), left = take it off.
    check: {
      label: "Check",
      param: "none",
      load: (only, skip, fresh) => ask({ mode: "check", limit: 40, skip, fresh: !!fresh, only: REVIEW_MODES._checkOnly }),
      keyOf: (it) => `${it.kind}:${it.id}:${it.what}:${it.target}`,
      yes: (it) => ask({ mode: "check_answer", key: `${it.kind}:${it.id}`, what: `${it.what}:${it.target}`, keep: true }),
      // taking a tag off also remembers it for that item, so no task puts it back
      no: async (it) => {
        await setOnItem(it, CHECK_FIELD[it.what], it.target, "REMOVE");
        if (it.what === "t") await ask({ mode: "tag_review_answer", key: `${it.kind}:${it.id}`, tag: it.target, no: true });
      },
      undoYes: (it) => ask({ mode: "check_answer", key: `${it.kind}:${it.id}`, what: `${it.what}:${it.target}`, keep: false }),
      undoNo: async (it) => {
        await setOnItem(it, CHECK_FIELD[it.what], it.target, "ADD");
        if (it.what === "t") await ask({ mode: "tag_review_answer", key: `${it.kind}:${it.id}`, tag: it.target, no: false });
      },
      avatar: (it) => checkAvatar(it.what, it.target),
      page: (it) => (it.what === "t" ? `/tags/${it.target}` : it.what === "g" ? `/galleries/${it.target}` : `/performers/${it.target}`),
      question: (it) => (it.what === "t" ? ["Does ", h("b", null, it.name), " fit?"]
        : it.what === "g" ? ["Does it belong in ", h("b", null, it.name), "?"] : ["Is ", h("b", null, it.name), " in this?"]),
      meta: (it) => (it.what === "t" ? `${it.score}% of its 20 closest look-alikes have it`
        : it.what === "g" ? `${it.score}% of its 20 closest look-alikes are in it` : "No face found in it looks like them"),
      extra: () => "",
      noLabel: "✕ Remove", yesLabel: "✓ It fits", noStamp: "REMOVE", yesStamp: "FITS",
      listNo: "✕ Remove", listYes: "✓ Fits",
      empty: "Nothing looks out of place. New ones show up as you tag (and after scans).",
      filterLabel: "",
    },
    // people seen in several items whose face matches no performer
    new: {
      label: "New",
      param: "none",
      load: (only, skip, fresh) => ask({ mode: "new_performers", limit: 30, skip, fresh: !!fresh }),
      keyOf: (it) => it.sig,
      yes: async (it) => {
        if (it._existing) {
          await setOnKeys(it.keys, "performer_ids", it._existing.id, "ADD");
          (it.faces || []).slice(0, 5).forEach((emb) =>
            ask({ mode: "face_confirm", performer: it._existing.id, emb }).catch(() => {}));
          return;
        }
        if (it.type === "assign") {
          await setOnKeys(it.missing, "performer_ids", it.performer.id, "ADD");
          (it.faces || []).slice(0, 5).forEach((emb) =>
            ask({ mode: "face_confirm", performer: it.performer.id, emb }).catch(() => {}));
          return;
        }
        const name = (it._name || "").trim();
        if (!name) throw new Error("give them a name first");
        const input = { ...(it._details || {}), name };
        if (!(input.tag_ids || []).length) delete input.tag_ids;
        // profile picture: the first one shown that wasn't left out
        const kept = new Set(it.keys);
        const pic = it.samples.find((x) => kept.has(`${x.kind}:${x.id}`)) ||
          (it.all || []).find((x) => kept.has(x.key));
        const image = await pictureData(pic);
        if (image) input.image = image;
        const d = await gql("mutation ($i: PerformerCreateInput!) { performerCreate(input: $i) { id } }", { i: input });
        it._created = d.performerCreate.id;
        await setOnKeys(it.keys, "performer_ids", it._created, "ADD");
        // learn their face now (from the new profile picture), quietly
        ask({ mode: "faces_learn" }).catch(() => {});
      },
      no: (it) => ask({ mode: "new_perf_answer", keys: it.keys, no: true }),
      undoYes: async (it) => {
        if (it._existing) return setOnKeys(it.keys, "performer_ids", it._existing.id, "REMOVE");
        if (it.type === "assign") return setOnKeys(it.missing, "performer_ids", it.performer.id, "REMOVE");
        if (!it._created) return;
        await setOnKeys(it.keys, "performer_ids", it._created, "REMOVE");
        await gql("mutation ($i: PerformerDestroyInput!) { performerDestroy(input: $i) }", { i: { id: it._created } });
        it._created = null;
      },
      undoNo: (it) => ask({ mode: "new_perf_answer", keys: it.keys, no: false }),
      empty: "No new faces seen in 3 or more items. New ones show up after \"Faces: scan scenes and images\".",
      filterLabel: "",
    },
    // studios to merge (misread names), delete (junk ones from watermarks)
    // or add (account names read once, galleries whose others have one)
    studios: {
      label: "Studios",
      param: "none",
      load: (only, skip) => ask({ mode: "studio_review", limit: 30, skip, kinds: REVIEW_MODES._studioKinds }),
      keyOf: (it) => it.sig,
      yes: async (it) => {
        const data = it.type === "merge" ? { keep: it._keep, others: it._others, name: it._name }
          : it.type === "delete" ? { id: it.studios[0].id }
          : it.type === "join" ? { studio: it.studio.id, keys: it.keys, pending: it.pending || null, alias: it._alias || null }
          : { name: it._name, platform: it.platform || null, keys: it.keys, pending: it.pending || null };
        it._undo = await ask({ mode: "studio_apply", action: it.type, data });
      },
      no: (it) => ask({ mode: "studio_apply", action: "no", data: { sig: it.sig } }),
      undoYes: (it) => ask({ mode: "studio_undo", undo: it._undo }),
      undoNo: (it) => ask({ mode: "studio_undo", undo: { action: "no", sig: it.sig } }),
      empty: "Nothing to tidy up. New ones show up after \"Studios from watermarks\" runs.",
      loading: "Looking for studios to tidy up…",
      filterLabel: "",
    },
    // tags: pictures that look alike with no tag yet (New), and tags to merge
    // or nest (Similar)
    tagtidy: {
      label: "Tags",
      param: "none",
      load: (only, skip, fresh) => ask({ mode: REVIEW_MODES._tidyMode, limit: 30, skip, fresh: !!fresh }),
      keyOf: (it) => it.sig,
      yes: async (it) => {
        const data = it.type === "tagmerge" || it.type === "perfmerge" ? { keep: it._keep, others: it._others, name: it._name }
          : it.type === "tagsub" ? { child: it.child.id, parent: it.parent.id }
          : it.type === "tagjoin" ? { tag: it._tag.id, keys: it.keys }
          : { name: it._name, keys: it.keys };
        it._undo = await ask({ mode: "tag_tidy_apply", action: it.type, data });
      },
      no: (it) => ask({ mode: "tag_tidy_apply", action: "no", data: { sig: it.sig } }),
      undoYes: (it) => it.type === "perfmerge"
        ? Promise.reject(new Error("A performer merge can't be undone in Stash."))
        : ask({ mode: "tag_tidy_undo", undo: it._undo }),
      undoNo: (it) => ask({ mode: "tag_tidy_undo", undo: { action: "no", sig: it.sig } }),
      empty: "Nothing to suggest.",
      loading: "Looking through your tags…",
      filterLabel: "",
    },
    // photos to add to a gallery, details a gallery is missing, new galleries
    galleries: {
      label: "Galleries",
      param: "none",
      load: (only, skip, fresh) => ask({ mode: "gallery_review", limit: 30, skip, fresh: !!fresh, kinds: REVIEW_MODES._galleryKinds }),
      keyOf: (it) => it.sig,
      yes: async (it) => {
        const data = it.type === "gjoin" ? { gallery: it.gallery.id, keys: it.keys }
          : it.type === "gdetails" ? { gallery: it.gallery.id, performers: it._performers, studio: it._studio }
          : { name: it._name, keys: it.keys, studio: it._studio, performers: it._performers };
        it._undo = await ask({ mode: "gallery_apply", action: it.type, data });
      },
      no: (it) => ask({ mode: "gallery_apply", action: "no", data: { sig: it.sig } }),
      undoYes: (it) => ask({ mode: "gallery_undo", undo: it._undo }),
      undoNo: (it) => ask({ mode: "gallery_undo", undo: { action: "no", sig: it.sig } }),
      empty: "Nothing to suggest.",
      loading: "Looking through your galleries…",
      filterLabel: "",
    },
  };
  // The tabs: what's reviewed (Faces, Tags, Studios), and under each what to
  // do - Suggested (add what's likely missing), Check (what's on items or
  // studios that may be wrong), New (performers / studios you don't have yet).
  // Each action is one of the modes above, set up for that tab.
  const ACTIONS = {
    faces: { ...REVIEW_MODES.faces, base: "faces", label: "Suggested" },
    faces_check: { ...REVIEW_MODES.check, base: "check", label: "Check", checkOnly: "p",
      empty: "No performer looks out of place. New ones show up after face scans." },
    new: { ...REVIEW_MODES.new, base: "new", label: "New", card: true },
    tags: { ...REVIEW_MODES.tags, base: "tags", label: "Suggested" },
    tags_check: { ...REVIEW_MODES.check, base: "check", label: "Check", checkOnly: "t",
      empty: "No tag looks out of place. New ones show up as you tag (and after look-alike scans)." },
    // tags for markers: their own look-alikes and the positions seen in them
    tags_markers: { ...REVIEW_MODES.tags, base: "tags", label: "Markers",
      load: (only, skip, fresh) => ask({ mode: "marker_tag_review", tag: only || null, limit: 40, skip, fresh: !!fresh }),
      batchLoad: (gid, fresh) => ask({ mode: "marker_tag_review", tag: gid, limit: 400, fresh: !!fresh }),
      question: (it) => ["Tag this marker ", h("b", null, it.name), "?"],
      empty: "No tag suggestions for markers. Markers come from \"AI markers\" (a scene's Markers tab, or the AI markers task); tagging some teaches the next suggestions." },
    // poses for photos and markers, learnt from the ones you've tagged
    tags_poses: { ...REVIEW_MODES.tags, base: "tags", label: "Poses",
      load: (only, skip, fresh) => ask({ mode: "pose_review", tag: only || null, limit: 40, skip, fresh: !!fresh }),
      batchLoad: (gid, fresh) => ask({ mode: "pose_review", tag: gid, limit: 400, fresh: !!fresh }),
      question: (it) => [it.kind === "marker" ? "Tag this marker " : "Tag it ", h("b", null, it.name), "?"],
      meta: (it) => it.why || "",
      tileMeta: (it, pct) => pct(it.score),
      loading: "Comparing body shapes and looks…",
      empty: "No pose suggestions. They come from photos and markers you tag with a pose (Face Down Ass Up, All Fours, Kneeling… - set in the add-on's settings), and need \"Tags: scan images' body poses for pose suggestions (Mac)\" (on a Mac)." },
    // performers who may be one person (by face, name, a shared StashDB entry or link)
    faces_similar: { ...REVIEW_MODES.tagtidy, base: "tagtidy", label: "Similar", card: true, tidyMode: "perf_pairs",
      loading: "Comparing performers' faces and names…",
      empty: "No performers look like the same person. Pairs come from faces (after face scans), names and aliases, and a shared StashDB entry or link." },
    tags_similar: { ...REVIEW_MODES.tagtidy, base: "tagtidy", label: "Similar", card: true, tidyMode: "tag_pairs",
      empty: "No tags to merge or nest. Tags named alike, or nested in name (\"Solo Female\" / \"Solo\"), show up here." },
    tags_new: { ...REVIEW_MODES.tagtidy, base: "tagtidy", label: "New", card: true, tidyMode: "tag_groups",
      empty: "No groups of look-alike pictures without a tag. They need \"Tags: scan scenes and images for look-alikes (do this first)\"." },
    studios_add: { ...REVIEW_MODES.studios, base: "studios", label: "Suggested", card: true, studioKinds: ["join"],
      empty: "No scenes or photos to add to a studio you have. New ones show up as galleries and folders get studios." },
    studios_check: { ...REVIEW_MODES.studios, base: "studios", label: "Check", card: true, studioKinds: ["merge", "delete"],
      empty: "No studios to merge or delete." },
    studios_new: { ...REVIEW_MODES.studios, base: "studios", label: "New", card: true, studioKinds: ["add"],
      empty: "No account names waiting. New ones show up after \"Studios from watermarks\" runs." },
    galleries_add: { ...REVIEW_MODES.galleries, base: "galleries", label: "Suggested", card: true, galleryKinds: ["gjoin", "gdetails"],
      empty: "No photos or details to add to a gallery. Look-alike suggestions need \"Tags: scan scenes and images for look-alikes (do this first)\"." },
    galleries_check: { ...REVIEW_MODES.check, base: "check", label: "Check", checkOnly: "g",
      empty: "No photo looks out of place in its gallery." },
    galleries_new: { ...REVIEW_MODES.galleries, base: "galleries", label: "New", card: true, galleryKinds: ["gnew"],
      empty: "No folders of photos without a gallery." },
  };
  const REVIEW_GROUPS = [
    { key: "faces", label: "Faces", actions: ["faces", "faces_check", "faces_similar", "new"] },
    { key: "tags", label: "Tags", actions: ["tags", "tags_markers", "tags_check", "tags_similar", "tags_new"] },
    { key: "studios", label: "Studios", actions: ["studios_add", "studios_check", "studios_new"] },
    { key: "galleries", label: "Galleries", actions: ["galleries_add", "galleries_check", "galleries_new"] },
  ];
  const OLD_MODES = { check: "tags_check", studios: "studios_check", tags_video: "tags", tags_poses: "tags" }; // links from before
  const groupOf = (k) => REVIEW_GROUPS.find((g) => g.actions.includes(k)) || REVIEW_GROUPS[0];
  // the action last used in each tab, so tapping Faces / Tags / Studios goes back to it
  // what each Review action needs (the rest need no model)
  const ACTION_NEEDS = { faces: "faces", faces_check: "faces", new: "faces", tags: "looks", tags_markers: "looks",
    tags_check: "looks", tags_new: "looks", galleries_add: "looks", galleries_check: "looks" };
  const reviewGroups = (c) => REVIEW_GROUPS.map((g) => ({ ...g, actions: g.actions.filter((k) => can(ACTION_NEEDS[k] || "none", c)) }))
    .filter((g) => g.actions.length);
  const lastAction = (g) => {
    try { const k = localStorage.getItem("bta-review-" + g.key); if (g.actions.includes(k)) return k; } catch (e) { /* fine */ }
    return g.actions[0];
  };

  async function setOnKeys(keys, field, id, mode) {
    const markers = (keys || []).filter((k) => k.startsWith("marker:")).map((k) => k.split(":")[1]);
    if (markers.length && field === "tag_ids") {
      await gql("mutation($i: BulkSceneMarkerUpdateInput!) { bulkSceneMarkerUpdate(input: $i) { id } }",
        { i: { ids: markers, tag_ids: { ids: [id], mode } } });
    }
    for (const kind of ["scene", "image"]) {
      const ids = (keys || []).filter((k) => k.startsWith(kind + ":")).map((k) => k.split(":")[1]);
      if (!ids.length) continue;
      const [mutation, type] = kind === "scene" ? ["bulkSceneUpdate", "BulkSceneUpdateInput"] : ["bulkImageUpdate", "BulkImageUpdateInput"];
      await gql(`mutation($i: ${type}!) { ${mutation}(input: $i) { id } }`, { i: { ids, [field]: { ids: [id], mode } } });
    }
  }

  // a picture as a data: link, for a new performer's profile picture (Stash
  // can't fetch its own pages with a login on)
  async function pictureData(sample) {
    if (!sample) return null;
    try {
      const url = sample.kind === "image" ? `/image/${sample.id}/image` : sample.picture;
      const blob = await (await fetch(url)).blob();
      if (!blob.type.startsWith("image/") || blob.size > 12 * 1024 * 1024) return null;
      return await new Promise((ok) => {
        const r = new FileReader();
        r.onload = () => ok(r.result);
        r.onerror = () => ok(null);
        r.readAsDataURL(blob);
      });
    } catch (e) {
      return null;
    }
  }

  async function setOnItem(it, field, id, mode) {
    if (it.kind === "marker") return setOnKeys([`marker:${it.id}`], field, id, mode);
    const [mutation, type] = it.kind === "scene" ? ["bulkSceneUpdate", "BulkSceneUpdateInput"] : ["bulkImageUpdate", "BulkImageUpdateInput"];
    await gql(`mutation($i: ${type}!) { ${mutation}(input: $i) { id } }`, { i: { ids: [it.id], [field]: { ids: [id], mode } } });
  }

  // a marker card is looked at as its scene, from the marker's start
  const asScene = (it) => (it && it.kind === "marker"
    ? { ...it, kind: "scene", id: it.scene, marker: it.id, times: it.times || [Math.round(it.start || 0)],
        best_at: Math.round(it.start || 0) + 2 } : it); // (the video starts 2 s before best_at)
  const clock = (sec) => {
    sec = Math.max(0, Math.round(sec || 0));
    const hh = Math.floor(sec / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    return (hh ? hh + ":" + String(mm).padStart(2, "0") : mm) + ":" + String(ss).padStart(2, "0");
  };

  // Where you were in the review, kept while you look at a performer's or
  // tag's page (tap their picture), so swiping down / Back from that page
  // lands on the same card without loading again. Kept for 30 minutes.
  let reviewMemory = null;

  // ✕ in the top right of a performer's / tag's page opened from Review:
  // straight back to the card you were on
  let reviewReturn = null; // the page path opened from Review
  function syncReviewReturn() {
    const on = reviewReturn && location.pathname.startsWith(reviewReturn);
    let btn = document.getElementById("bta-review-return");
    if (!on) {
      if (btn) btn.remove();
      if (reviewReturn && ![REVIEW_PATH, REVIEW_PAGE].includes(location.pathname)) reviewReturn = null;
      return;
    }
    if (btn) return;
    btn = document.createElement("button");
    btn.id = "bta-review-return";
    btn.type = "button";
    btn.title = "Back to Review";
    btn.setAttribute("aria-label", "Back to Review");
    btn.textContent = "✕";
    btn.addEventListener("click", () => window.history.back());
    document.body.appendChild(btn);
  }
  window.addEventListener("popstate", () => setTimeout(syncReviewReturn, 0));
  // links inside Stash change the page without "popstate": check on page changes
  let returnQueued = false, lastPath = location.pathname;
  new MutationObserver(() => {
    if (returnQueued || location.pathname === lastPath) return;
    returnQueued = true;
    requestAnimationFrame(() => {
      returnQueued = false;
      lastPath = location.pathname;
      syncReviewReturn();
    });
  }).observe(document.body, { childList: true, subtree: true });

  // The picture, big, with what's on it
  const ZOOM_FIELDS = {
    scene: "title date organized details studio { id name } performers { id name } tags { id name } galleries { id title } files { duration width height } paths { stream }",
    image: "title date organized details studio { id name } performers { id name } tags { id name } galleries { id title } visual_files { ... on ImageFile { width height } ... on VideoFile { width height } }",
  };
  // list + onNav: the pictures it's one of (a New-tab suggestion's), to swipe
  // left / right through
  // ---- Edit a scene / photo by hand, from Review's big view ------------------
  const PICK_KINDS = {
    tags: { q: "findTags", f: "tags", label: (x) => x.name },
    performers: { q: "findPerformers", f: "performers", label: (x) => x.name },
    studios: { q: "findStudios", f: "studios", label: (x) => x.name },
    galleries: { q: "findGalleries", f: "galleries", label: (x) => x.title || `Gallery ${x.id}`, fields: "id title" },
  };

  // chips of what it has (✕ takes one off) and a search box to add more
  function EditPicker({ kind, values, onChange, single, placeholder }) {
    const K = PICK_KINDS[kind];
    const [q, setQ] = React.useState("");
    const [hits, setHits] = React.useState([]);
    React.useEffect(() => {
      const text = q.trim();
      if (!text) { setHits([]); return undefined; }
      let live = true;
      const t = setTimeout(() => {
        gql(`query ($q: String) { r: ${K.q}(filter: {q: $q, per_page: 8, sort: "${kind === "galleries" ? "title" : "name"}"}) { ${K.f} { ${K.fields || "id name"} } } }`, { q: text })
          .then((d) => live && setHits(d.r[K.f] || []))
          .catch(() => live && setHits([]));
      }, 200);
      return () => { live = false; clearTimeout(t); };
    }, [q]);
    const have = new Set(values.map((v) => String(v.id)));
    const add = (x) => {
      onChange(single ? [x] : [...values, x]);
      setQ("");
      setHits([]);
    };
    return h("div", { className: "bta-edit-pick" },
      values.length > 0 && h("div", { className: "bta-zoom-chips" }, values.map((v) =>
        h("button", { key: v.id, type: "button", className: "bta-zoom-chip bta-edit-chip", title: "Take it off",
          onClick: () => onChange(values.filter((x) => String(x.id) !== String(v.id))) }, K.label(v), h("span", { "aria-hidden": true }, " ✕")))),
      h("input", { className: "bta-edit-input", type: "search", value: q, placeholder: placeholder || "Search to add…",
        autoComplete: "off", onChange: (e) => setQ(e.target.value),
        onKeyDown: (e) => { if (e.key === "Enter" && hits[0]) { e.preventDefault(); if (!have.has(String(hits[0].id))) add(hits[0]); } } }),
      hits.length > 0 && h("div", { className: "bta-edit-hits" }, hits.map((x) => {
        const on = have.has(String(x.id));
        return h("button", { key: x.id, type: "button", className: "bta-edit-hit" + (on ? " on" : ""), disabled: on, onClick: () => add(x) },
          (on ? "✓ " : "+ ") + K.label(x));
      })));
  }

  // the whole form: title, tags, performers, studio, galleries, organized
  function EditItem({ it, info, onCancel, onSaved }) {
    const [form, setForm] = React.useState(() => ({
      title: info.title || "",
      tags: info.tags || [], performers: info.performers || [],
      studio: info.studio ? [info.studio] : [], galleries: info.galleries || [],
      organized: !!info.organized,
    }));
    const [saving, setSaving] = React.useState(false);
    const [err, setErr] = React.useState("");
    const set = (k) => (v) => setForm((f0) => ({ ...f0, [k]: v }));
    async function save() {
      setSaving(true);
      setErr("");
      try {
        const [mut, typ] = it.kind === "scene" ? ["sceneUpdate", "SceneUpdateInput"] : ["imageUpdate", "ImageUpdateInput"];
        await gql(`mutation ($i: ${typ}!) { ${mut}(input: $i) { id } }`, { i: {
          id: String(it.id), title: form.title.trim(),
          tag_ids: form.tags.map((x) => x.id), performer_ids: form.performers.map((x) => x.id),
          studio_id: form.studio[0] ? form.studio[0].id : null, gallery_ids: form.galleries.map((x) => x.id),
          organized: form.organized,
        } });
        onSaved();
      } catch (e) {
        setErr("Couldn't save: " + e.message);
        setSaving(false);
      }
    }
    return h("div", { className: "bta-edit" },
      h("div", { className: "bta-zoom-head" }, "Title"),
      h("input", { className: "bta-edit-input", type: "text", value: form.title, placeholder: "No title",
        onChange: (e) => set("title")(e.target.value) }),
      h("div", { className: "bta-zoom-head" }, `Tags (${form.tags.length})`),
      h(EditPicker, { kind: "tags", values: form.tags, onChange: set("tags"), placeholder: "Add a tag…" }),
      h("div", { className: "bta-zoom-head" }, "Performers"),
      h(EditPicker, { kind: "performers", values: form.performers, onChange: set("performers"), placeholder: "Add a performer…" }),
      h("div", { className: "bta-zoom-head" }, "Studio"),
      h(EditPicker, { kind: "studios", values: form.studio, onChange: set("studio"), single: true,
        placeholder: form.studio.length ? "Change the studio…" : "Set a studio…" }),
      h("div", { className: "bta-zoom-head" }, "Galleries"),
      h(EditPicker, { kind: "galleries", values: form.galleries, onChange: set("galleries"), placeholder: "Add to a gallery…" }),
      h("label", { className: "bta-edit-org" },
        h("input", { type: "checkbox", checked: form.organized, onChange: (e) => set("organized")(e.target.checked) }),
        " Organized"),
      err && h("div", { className: "bta-edit-err" }, err),
      h("div", { className: "bta-edit-actions" },
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: saving, onClick: onCancel }, "Cancel"),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: saving, onClick: save }, saving ? "Saving…" : "✓ Save")));
  }

  function ZoomView({ it, onClose, list, onNav, pick }) {
    const [info, setInfo] = React.useState(null);
    const [editing, setEditing] = React.useState(false);
    const [saved, setSaved] = React.useState(0); // reload what's on it after a save
    const at = list ? list.findIndex((x) => x.kind === it.kind && String(x.id) === String(it.id)) : -1;
    // past the first / last one (or with nothing to step through): close,
    // like swiping down
    const step = (d, swiped) => {
      const n = at + d;
      if (list && at >= 0 && n >= 0 && n < list.length) onNav(list[n]);
      else if (swiped) closeRef.current();
    };
    const stepRef = React.useRef(step);
    stepRef.current = step;
    const [dx, setDx] = React.useState(0);
    const video = React.useRef(null);
    const jump = (t) => {
      const v = video.current;
      if (!v) return;
      v.currentTime = t;
      v.play().catch(() => {});
    };
    const [dy, setDy] = React.useState(0);
    const drag = React.useRef(null);
    const scroller = React.useRef(null);
    const closeRef = React.useRef(onClose);
    closeRef.current = onClose;
    // swipe down to close: anywhere, when it's scrolled to the top. Touch
    // events (not pointer events): on a phone a downward swipe starts the
    // page's own scrolling, which cancels pointer events.
    React.useEffect(() => {
      const el = scroller.current;
      if (!el) return undefined;
      let t0 = null, pull = 0, side = 0;
      const start = (e) => {
        if (e.touches.length !== 1) { t0 = null; return; }
        // (typing / picking in the edit form: no swiping to the next one)
        if (e.target.closest && e.target.closest(".bta-edit")) { t0 = null; return; }
        t0 = { x: e.touches[0].clientX, y: e.touches[0].clientY, top: el.scrollTop <= 0, dir: null,
          onVideo: !!(e.target.closest && e.target.closest("video")) };
        pull = 0;
        side = 0;
      };
      const move = (e) => {
        if (!t0) return;
        const dx = e.touches[0].clientX - t0.x, d = e.touches[0].clientY - t0.y;
        if (!t0.dir) {
          if (Math.abs(d) < 8 && Math.abs(dx) < 8) return;
          if (Math.abs(dx) > Math.abs(d)) t0.dir = t0.onVideo ? "other" : "side"; // (the video's own scrubbing)
          else t0.dir = d > 0 && t0.top ? "down" : "other";
        }
        if (t0.dir === "side") {
          e.preventDefault();
          side = dx;
          setDx(dx);
          return;
        }
        if (t0.dir !== "down") return;
        e.preventDefault(); // this swipe closes it, so don't scroll
        pull = Math.max(0, d);
        setDy(pull);
      };
      const end = () => {
        if (t0 && t0.dir === "down") {
          if (pull > 100) closeRef.current();
          else setDy(0);
        }
        if (t0 && t0.dir === "side") {
          if (side < -60) stepRef.current(1, true);
          else if (side > 60) stepRef.current(-1, true);
          setDx(0);
        }
        t0 = null;
      };
      el.addEventListener("touchstart", start, { passive: true });
      el.addEventListener("touchmove", move, { passive: false });
      el.addEventListener("touchend", end);
      el.addEventListener("touchcancel", end);
      return () => {
        el.removeEventListener("touchstart", start);
        el.removeEventListener("touchmove", move);
        el.removeEventListener("touchend", end);
        el.removeEventListener("touchcancel", end);
      };
    }, []);
    React.useEffect(() => {
      let live = true;
      if (!saved) {
        setInfo(null);
        setEditing(false);
        if (scroller.current) scroller.current.scrollTop = 0;
      }
      const q = it.kind === "scene" ? "findScene" : "findImage";
      gql(`query ($id: ID!) { item: ${q}(id: $id) { ${ZOOM_FIELDS[it.kind]} } }`, { id: it.id })
        .then((d) => live && setInfo(d.item || {}))
        .catch(() => live && setInfo({}));
      return () => { live = false; };
    }, [it.kind, it.id, saved]);
    React.useEffect(() => setSaved(0), [it.kind, it.id]);
    React.useEffect(() => {
      const onKey = (e) => {
        if (e.target.closest && e.target.closest("input, textarea, select")) return;
        if (e.key === "Escape") closeRef.current();
        else if (e.key === "ArrowRight") stepRef.current(1);
        else if (e.key === "ArrowLeft") stepRef.current(-1);
      };
      window.addEventListener("keydown", onKey);
      const html = document.documentElement;
      const before = html.style.overflow;
      html.style.overflow = "hidden";
      return () => {
        window.removeEventListener("keydown", onKey);
        html.style.overflow = before;
      };
    }, []);
    const open = (path) => {
      onClose();
      reviewReturn = path;
      goTo(path);
    };
    const chips = (list, path, empty) => list && list.length
      ? h("div", { className: "bta-zoom-chips" }, list.map((x) =>
          h("button", { key: x.id, type: "button", className: "bta-zoom-chip", onClick: () => open(`/${path}/${x.id}`) }, x.name || x.title || "Gallery")))
      : h("div", { className: "bta-zoom-none" }, empty);
    const f = info && ((info.files || info.visual_files || [])[0] || {});
    const size = f && f.width ? `${f.width}×${f.height}` : "";
    const len = f && f.duration ? `${Math.floor(f.duration / 60)}:${String(Math.round(f.duration % 60)).padStart(2, "0")}` : "";
    // swipe the picture down to close
    const down = (e) => { if (e.pointerType === "mouse") drag.current = { y: e.clientY, id: e.pointerId }; };
    const move = (e) => {
      if (!drag.current || drag.current.id !== e.pointerId) return;
      const d = e.clientY - drag.current.y;
      if (d > 8) setDy(d);
    };
    const up = () => {
      if (!drag.current) return;
      drag.current = null;
      if (dy > 110) onClose();
      else setDy(0);
    };
    const multi = list && list.length > 1 && at >= 0;
    return h("div", { className: "bta-zoom" + (pick ? " bta-zoom-picking" : ""), style: dy ? { background: `rgba(10,11,14,${Math.max(0.35, 1 - dy / 350)})` } : null },
      // in a batch / "See all": keep this one or leave it out, right here
      pick && h("div", { className: "bta-zoom-pickbar", role: "group", "aria-label": "Keep or leave out" },
        pick.check
          ? [h("button", { key: "r", type: "button", className: "out" + (!pick.off ? " on" : ""), "aria-pressed": !pick.off,
              onClick: () => pick.off && pick.toggle() }, "✕ Remove"),
             h("button", { key: "f", type: "button", className: "keep" + (pick.off ? " on" : ""), "aria-pressed": pick.off,
              onClick: () => !pick.off && pick.toggle() }, "✓ It fits")]
          : [h("button", { key: "k", type: "button", className: "keep" + (!pick.off ? " on" : ""), "aria-pressed": !pick.off,
              onClick: () => pick.off && pick.toggle() }, "✓ Keep"),
             h("button", { key: "o", type: "button", className: "out" + (pick.off ? " on" : ""), "aria-pressed": pick.off,
              onClick: () => !pick.off && pick.toggle() }, "✕ Leave out")]),
      h("button", { type: "button", className: "bta-zoom-close", "aria-label": "Close", onClick: onClose }, "✕"),
      multi && h("div", { className: "bta-zoom-count" }, `${at + 1} / ${list.length}`),
      multi && at > 0 && h("button", { type: "button", className: "bta-zoom-nav prev", "aria-label": "Previous", onClick: () => step(-1) }, "‹"),
      multi && at < list.length - 1 && h("button", { type: "button", className: "bta-zoom-nav next", "aria-label": "Next", onClick: () => step(1) }, "›"),
      h("div", { className: "bta-zoom-scroll", ref: scroller,
        style: dy ? { transform: `translateY(${dy}px)` } : dx ? { transform: `translateX(${dx}px)`, transition: "none" } : null },
        it.kind === "scene"
          ? h("div", { className: "bta-zoom-pic bta-zoom-video", key: "v" + it.id },
              h("video", {
                // not playing until you press play (or a ▶ time below)
                ref: video, controls: true, playsInline: true, preload: "metadata", poster: it.picture,
                // ready at where they were seen (a moment before)
                onLoadedMetadata: (e) => {
                  const at = it.best_at != null ? Math.max(0, it.best_at - 2) : 0;
                  if (at) e.target.currentTime = at;
                },
              },
                // the file as is, or Stash's MP4 version if the browser can't play it
                h("source", { src: `/scene/${it.id}/stream` }),
                h("source", { src: `/scene/${it.id}/stream.mp4`, type: "video/mp4" })))
          : h("div", {
              className: "bta-zoom-pic",
              onPointerDown: down, onPointerMove: move, onPointerUp: up, onPointerCancel: up,
            }, h("img", { key: it.id, src: `/image/${it.id}/image`, alt: "", draggable: false })),
        it.kind === "scene" && it.times && it.times.length > 0 && h("div", { className: "bta-zoom-times" },
          h("span", null, it.name ? `${it.name} seen at` : "Seen at"),
          it.times.map((t) => h("button", { key: t, type: "button", className: "bta-zoom-chip", onClick: () => jump(Math.max(0, t - 2)) },
            "▶ " + clock(t)))),
        h("div", { className: "bta-zoom-info" },
          h("div", { className: "bta-zoom-title" }, (info && info.title) || it.title || `${it.kind} ${it.id}`),
          h("div", { className: "bta-zoom-line" },
            [it.kind === "scene" ? "Scene" : "Photo", info && info.date, len, size,
             info && (info.organized ? "Organized" : "Not organized")].filter(Boolean).join(" · ")),
          !info ? h("div", { className: "bta-zoom-none" }, "Loading…") : editing ? h(EditItem, {
            it, info, onCancel: () => setEditing(false),
            onSaved: () => { setEditing(false); setSaved((n) => n + 1); },
          }) : h(React.Fragment, null,
            h("button", { type: "button", className: "bta-zoom-edit", onClick: () => setEditing(true) },
              `✎ Edit this ${it.kind === "scene" ? "scene" : "photo"}`),
            saved > 0 && h("div", { className: "bta-edit-saved" }, "✓ Saved"),
            h("div", { className: "bta-zoom-head" }, `Tags (${(info.tags || []).length})`),
            chips(info.tags, "tags", "No tags"),
            h("div", { className: "bta-zoom-head" }, "Performers"),
            chips(info.performers, "performers", "No performers"),
            info.studio && h(React.Fragment, null,
              h("div", { className: "bta-zoom-head" }, "Studio"),
              h("div", { className: "bta-zoom-text" }, info.studio.name)),
            (info.galleries || []).length > 0 && h(React.Fragment, null,
              h("div", { className: "bta-zoom-head" }, "Galleries"),
              chips(info.galleries, "galleries", "")),
            info.details && h(React.Fragment, null,
              h("div", { className: "bta-zoom-head" }, "Details"),
              h("div", { className: "bta-zoom-text" }, info.details)),
            h("button", { type: "button", className: "bta-zoom-open", onClick: () => open(`/${it.kind}s/${it.id}`) },
              `Open the ${it.kind === "scene" ? "scene" : "photo"}'s page`)))));
  }

  // Every scene / photo of a New-tab suggestion: tap one to leave it out
  // (or back in); ⤢ to see it big
  function AllItems({ it, out, onToggle, onAll, onView, onClose, onDo, doLabel }) {
    const all = it.all || [];
    const using = all.filter((x) => !out.has(x.key)).length;
    React.useEffect(() => {
      // (Escape in the big view on top closes just that)
      const onKey = (e) => e.key === "Escape" && !document.querySelector(".bta-zoom") && onClose();
      window.addEventListener("keydown", onKey);
      const html = document.documentElement;
      const before = html.style.overflow;
      html.style.overflow = "hidden";
      return () => { window.removeEventListener("keydown", onKey); html.style.overflow = before; };
    }, []);
    return h("div", { className: "bta-all" },
      h("div", { className: "bta-all-head" },
        h("div", { className: "bta-all-title" },
          it.type === "assign" ? `${it.performer.name}?` : it.type === "join" ? `${it.studio.name}?`
            : it.gallery ? `${it.gallery.name}${it.type === "gjoin" ? "?" : ""}` : it.tg ? "New tag?" : (it._name || it.name || "New performer"),
          h("span", null, ` · using ${using} of ${all.length}`)),
        h("button", { type: "button", className: "bta-zoom-close bta-all-close", "aria-label": "Close", onClick: onClose }, "✕")),
      h("div", { className: "bta-all-hint" }, "Tap one to leave it out (tap again to put it back)."),
      h("div", { className: "bta-all-scroll" },
        h("div", { className: "bta-all-grid" },
          all.map((x) => {
            const off = out.has(x.key);
            return h("div", { key: x.key, className: "bta-all-tile" + (off ? " off" : "") },
              h("button", { type: "button", className: "bta-all-pick", "aria-pressed": !off, title: off ? "Put it back" : "Leave it out",
                onClick: () => onToggle(x.key) },
                h("img", { src: x.picture, alt: "", loading: "lazy", draggable: false }),
                h("span", { className: "bta-all-mark" }, off ? "✕" : "✓"),
                x.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶ Scene")),
              h("button", { type: "button", className: "bta-all-view", title: "See it big", "aria-label": "See it big",
                onClick: () => onView(x) }, "⤢"));
          }))),
      h("div", { className: "bta-all-foot" },
        out.size > 0
          ? h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: () => onAll(true) }, "Use all")
          : h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: () => onAll(false) }, "Use none"),
        onDo
          // (Studios cards: do it straight from here - "Create", "Add 12")
          ? h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: !using, onClick: onDo }, doLabel(using))
          : h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", onClick: onClose }, `Done · using ${using}`)));
  }

  // "Create a new performer?": the name to check (or type), gender, tags and
  // more details if you like - with how their card will look
  const GENDERS = [["FEMALE", "Female"], ["MALE", "Male"], ["TRANSGENDER_FEMALE", "Trans woman"],
    ["TRANSGENDER_MALE", "Trans man"], ["NON_BINARY", "Non-binary"], ["INTERSEX", "Intersex"]];
  function ConfirmNew({ it, name, picture, onCancel, onCreate }) {
    const fromIdea = (n) => (it.names || []).find((x) => x.name === n);
    const [f, setF] = React.useState(() => ({
      name: name || "", gender: "", disambiguation: "", aliases: "",
      urls: (fromIdea(name) || {}).url || "", country: "", ethnicity: "", hair_color: "", eye_color: "",
      birthdate: "", details: "",
    }));
    const [tags, setTags] = React.useState([]); // [{id, name}]
    const [more, setMore] = React.useState(false);
    const [q, setQ] = React.useState("");
    const [found, setFound] = React.useState([]);
    const box = React.useRef(null);
    const set = (k, v) => setF((x) => ({ ...x, [k]: v }));
    const pickName = (n) => setF((x) => {
      const idea = fromIdea(n);
      const prev = fromIdea(x.name);
      // an account's link comes with its name (unless you've typed your own)
      const urls = idea && idea.url && (!x.urls || (prev && prev.url === x.urls)) ? idea.url : x.urls;
      return { ...x, name: n, urls };
    });
    React.useEffect(() => {
      const t = setTimeout(() => box.current && box.current.focus(), 50);
      const onKey = (e) => e.key === "Escape" && onCancel();
      window.addEventListener("keydown", onKey);
      return () => { clearTimeout(t); window.removeEventListener("keydown", onKey); };
    }, []);
    // tag search
    React.useEffect(() => {
      const term = q.trim();
      if (term.length < 2) { setFound([]); return undefined; }
      let live = true;
      const t = setTimeout(() => {
        gql(`query ($f: TagFilterType) { findTags(tag_filter: $f, filter: { per_page: 8, sort: "scenes_count", direction: DESC }) { tags { id name } } }`,
          { f: { name: { value: term, modifier: "INCLUDES" } } })
          .then((d) => live && setFound(d.findTags.tags))
          .catch(() => live && setFound([]));
      }, 200);
      return () => { live = false; clearTimeout(t); };
    }, [q]);
    const has = (id) => tags.some((t) => String(t.id) === String(id));
    const toggleTag = (t) => setTags((list) => (has(t.id) ? list.filter((x) => String(x.id) !== String(t.id)) : [...list, { id: String(t.id), name: t.name }]));
    const ok = f.name.trim().length > 0;
    const details = () => {
      const d = { tag_ids: tags.map((t) => t.id) };
      if (f.gender) d.gender = f.gender;
      ["disambiguation", "country", "ethnicity", "hair_color", "eye_color", "details"].forEach((k) => { if (f[k].trim()) d[k] = f[k].trim(); });
      if (/^\d{4}-\d{2}-\d{2}$/.test(f.birthdate)) d.birthdate = f.birthdate;
      const aliases = f.aliases.split(",").map((x) => x.trim()).filter(Boolean);
      if (aliases.length) d.alias_list = aliases;
      const urls = f.urls.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);
      if (urls.length) d.urls = urls;
      return d;
    };
    const create = () => ok && onCreate(f.name.trim(), details());
    const gLabel = (GENDERS.find((g) => g[0] === f.gender) || [])[1];
    const field = (label, k, props) => h("label", { className: "bta-cf-field" }, label,
      h(props && props.rows ? "textarea" : "input", { className: "bta-np-name", value: f[k], onChange: (e) => set(k, e.target.value), ...(props || {}) }));
    return h("div", { className: "bta-confirm", onClick: (e) => e.target === e.currentTarget && onCancel() },
      h("div", { className: "bta-confirm-box bta-cf", role: "dialog", "aria-modal": true, "aria-label": "Create a new performer" },
        h("div", { className: "bta-cf-scroll" },
          h("div", { className: "bta-confirm-title" }, "Create a new performer?"),
          // how their card will look
          h("div", { className: "bta-cf-preview" },
            h("div", { className: "bta-cf-card" },
              h("div", { className: "bta-cf-pic" }, picture && h("img", { src: picture, alt: "" })),
              h("div", { className: "bta-cf-name" }, f.name.trim() || "Their name",
                f.disambiguation.trim() && h("span", null, ` (${f.disambiguation.trim()})`)),
              h("div", { className: "bta-cf-sub" }, [gLabel, f.country.trim()].filter(Boolean).join(" · ") || " "),
              (() => {  // counts of what's kept (some may be left out)
                const sc = (it.keys || []).filter((k) => k.startsWith("scene:")).length;
                const ph = (it.keys || []).filter((k) => k.startsWith("image:")).length;
                return h("div", { className: "bta-cf-counts" }, [sc ? `${sc} scene${sc === 1 ? "" : "s"}` : "",
                  ph ? `${ph} photo${ph === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · "));
              })(),
              tags.length > 0 && h("div", { className: "bta-cf-tags" }, tags.slice(0, 6).map((t) => h("span", { key: t.id }, t.name)),
                tags.length > 6 && h("span", null, `+${tags.length - 6}`))),
            h("div", { className: "bta-cf-note" }, `Added to ${it.count === 1 ? "1 item" : `${it.count} scenes and photos`}. This picture becomes their profile picture.`)),
          h("label", { className: "bta-cf-field" }, "Name",
            h("input", { ref: box, className: "bta-np-name", type: "text", value: f.name, placeholder: "Their name",
              onChange: (e) => set("name", e.target.value), onKeyDown: (e) => e.key === "Enter" && create() })),
          (it.names || []).length > 0 && h("div", { className: "bta-np-ideas" },
            it.names.map((nm) => h("button", { key: nm.name, type: "button", title: nm.why,
              className: "bta-np-idea" + (f.name === nm.name ? " active" : ""), onClick: () => pickName(nm.name) }, nm.name))),
          h("div", { className: "bta-cf-label" }, "Gender"),
          h("div", { className: "bta-np-ideas" }, GENDERS.map(([v, label]) => h("button", { key: v, type: "button",
            className: "bta-np-idea" + (f.gender === v ? " active" : ""), onClick: () => set("gender", f.gender === v ? "" : v) }, label))),
          h("div", { className: "bta-cf-label" }, "Tags"),
          (it.tag_ideas || []).length > 0 && h("div", { className: "bta-np-ideas" },
            it.tag_ideas.map((t) => h("button", { key: t.id, type: "button", title: `On ${t.count} of these items`,
              className: "bta-np-idea" + (has(t.id) ? " active" : ""), onClick: () => toggleTag(t) }, (has(t.id) ? "✓ " : "+ ") + t.name))),
          tags.filter((t) => !(it.tag_ideas || []).some((x) => String(x.id) === String(t.id))).length > 0 && h("div", { className: "bta-np-ideas" },
            tags.filter((t) => !(it.tag_ideas || []).some((x) => String(x.id) === String(t.id))).map((t) =>
              h("button", { key: t.id, type: "button", className: "bta-np-idea active", title: "Tap to remove", onClick: () => toggleTag(t) }, "✓ " + t.name))),
          h("input", { className: "bta-np-name bta-cf-search", type: "search", value: q, placeholder: "Find a tag to add…",
            onChange: (e) => setQ(e.target.value) }),
          found.length > 0 && h("div", { className: "bta-np-ideas" }, found.map((t) => h("button", { key: t.id, type: "button",
            className: "bta-np-idea" + (has(t.id) ? " active" : ""), onClick: () => { toggleTag(t); setQ(""); } }, (has(t.id) ? "✓ " : "+ ") + t.name))),
          h("button", { type: "button", className: "bta-cf-more", onClick: () => setMore(!more), "aria-expanded": more },
            more ? "▾ Fewer details" : "▸ More details (aliases, links, country…)"),
          more && h("div", { className: "bta-cf-grid" },
            field("Disambiguation", "disambiguation", { placeholder: "e.g. to tell apart two with the same name" }),
            field("Aliases", "aliases", { placeholder: "Comma separated" }),
            field("Links", "urls", { placeholder: "onlyfans.com/…, instagram.com/…" }),
            field("Country", "country", { placeholder: "e.g. US" }),
            field("Ethnicity", "ethnicity"),
            field("Hair colour", "hair_color"),
            field("Eye colour", "eye_color"),
            field("Birthdate", "birthdate", { type: "date" }),
            field("Description", "details", { rows: 3 }))),
        h("div", { className: "bta-confirm-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: onCancel }, "Cancel"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: !ok, onClick: create },
            ok ? `✓ Create "${f.name.trim()}"` : "✓ Create"))));
  }

  // "Add to a performer you already have": search, pick, confirm
  function PickExisting({ it, ideas, onCancel, onPick }) {
    const [q, setQ] = React.useState("");
    const [list, setList] = React.useState([]);
    const [chosen, setChosen] = React.useState(null);
    const [alike, setAlike] = React.useState(null); // performers whose faces look closest
    const box = React.useRef(null);
    React.useEffect(() => {
      let live = true;
      ask({ mode: "new_perf_lookalikes", faces: it.faces || [] })
        .then(async (list) => {
          list = list || [];
          if (!list.length) { live && setAlike([]); return; }
          // their counts and aliases, like the search results
          const d = await gql(`query ($ids: [ID!]) { findPerformers(ids: $ids, filter: { per_page: -1 }) {
              performers { id name disambiguation alias_list scene_count image_count } } }`, { ids: list.map((x) => x.id) });
          const by = new Map(d.findPerformers.performers.map((p) => [String(p.id), p]));
          live && setAlike(list.filter((x) => by.has(String(x.id))).map((x) => ({ ...by.get(String(x.id)), score: x.score })));
        })
        .catch(() => live && setAlike([]));
      return () => { live = false; };
    }, []);
    const row = (p) => h("button", { key: p.id, type: "button", className: "bta-pick-row", onClick: () => setChosen(p) },
      h("img", { src: `/performer/${p.id}/image`, alt: "", loading: "lazy" }),
      h("span", { className: "bta-pick-text" },
        h("span", { className: "bta-pick-name" }, p.name, p.disambiguation && h("span", null, ` (${p.disambiguation})`)),
        h("span", { className: "bta-pick-sub" }, [p.score != null ? `${p.score}% alike` : "",
          p.scene_count ? `${p.scene_count} scene${p.scene_count === 1 ? "" : "s"}` : "",
          p.image_count ? `${p.image_count} photo${p.image_count === 1 ? "" : "s"}` : "",
          (p.alias_list || []).length ? `also ${p.alias_list.slice(0, 2).join(", ")}` : ""].filter(Boolean).join(" · "))));
    React.useEffect(() => {
      const t = setTimeout(() => box.current && box.current.focus(), 50);
      const onKey = (e) => e.key === "Escape" && onCancel();
      window.addEventListener("keydown", onKey);
      return () => { clearTimeout(t); window.removeEventListener("keydown", onKey); };
    }, []);
    // name ideas (accounts, tags, the AI's reads) are searched for right away
    React.useEffect(() => {
      const term = q.trim();
      let live = true;
      const t = setTimeout(async () => {
        const terms = term.length >= 2 ? [term] : (ideas || []).map((x) => x.name).slice(0, 3);
        if (!terms.length) { live && setList([]); return; }
        const seen = new Map();
        for (const one of terms) {
          try {
            const d = await gql(`query ($f: PerformerFilterType) { findPerformers(performer_filter: $f, filter: { per_page: 12, sort: "scenes_count", direction: DESC }) {
                performers { id name disambiguation alias_list scene_count image_count } } }`,
              { f: { OR: { aliases: { value: one, modifier: "INCLUDES" } }, name: { value: one, modifier: "INCLUDES" } } });
            d.findPerformers.performers.forEach((p) => seen.set(p.id, p));
          } catch (e) { /* skip */ }
        }
        if (live) setList([...seen.values()].slice(0, 12));
      }, term ? 250 : 0);
      return () => { live = false; clearTimeout(t); };
    }, [q]);
    const n = (it.keys || []).length;
    return h("div", { className: "bta-confirm", onClick: (e) => e.target === e.currentTarget && onCancel() },
      h("div", { className: "bta-confirm-box bta-cf bta-pick", role: "dialog", "aria-modal": true, "aria-label": "Add to a performer you already have" },
        h("div", { className: "bta-cf-scroll" },
          h("div", { className: "bta-confirm-title" }, chosen ? `Add ${chosen.name}?` : "Add to a performer you already have"),
          chosen
            ? h(React.Fragment, null,
                h("div", { className: "bta-pick-chosen" },
                  h("img", { src: `/performer/${chosen.id}/image`, alt: "" }),
                  h("div", null,
                    h("div", { className: "bta-pick-name" }, chosen.name, chosen.disambiguation && h("span", null, ` (${chosen.disambiguation})`)),
                    h("div", { className: "bta-confirm-text" }, `Adds ${chosen.name} to ${n === 1 ? "this item" : `these ${n} scenes and photos`} and learns this face as theirs, so they're recognised from now on.`))))
            : h(React.Fragment, null,
                h("div", { className: "bta-confirm-text" }, `Instead of making a new performer, put someone you have on ${n === 1 ? "this item" : `these ${n} scenes and photos`}.`),
                h("input", { ref: box, className: "bta-np-name", type: "search", value: q, placeholder: "Search your performers…",
                  onChange: (e) => setQ(e.target.value) }),
                q.trim().length >= 2 && h("div", { className: "bta-pick-list" },
                  list.length === 0 && h("div", { className: "bta-confirm-text" }, "No one by that name"),
                  list.map(row)),
                !q.trim() && (ideas || []).length > 0 && list.length > 0 && h(React.Fragment, null,
                  h("div", { className: "bta-cf-label" }, "Matching the name ideas"),
                  h("div", { className: "bta-pick-list" }, list.map(row))),
                h("div", { className: "bta-cf-label" }, "Looks most like"),
                h("div", { className: "bta-pick-note" }, "Closest faces you have - possibilities, not a match (no one matched well, which is why it's here)."),
                h("div", { className: "bta-pick-list" },
                  alike === null && h("div", { className: "bta-confirm-text" }, "Comparing faces…"),
                  alike && alike.length === 0 && h("div", { className: "bta-confirm-text" }, "No performer faces to compare with"),
                  (alike || []).map(row)))),
        h("div", { className: "bta-confirm-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: () => (chosen ? setChosen(null) : onCancel()) }, chosen ? "Back" : "Cancel"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: !chosen, onClick: () => onPick(chosen) },
            chosen ? `✓ Add ${chosen.name}` : "Pick someone"))));
  }

  // "New studio?" → "…or add to a studio you already have": search your
  // studios, with the ones whose name is most like it listed first
  function PickStudio({ it, using, onCancel, onPick }) {
    const [q, setQ] = React.useState("");
    const [list, setList] = React.useState([]);
    const [chosen, setChosen] = React.useState(null);
    const box = React.useRef(null);
    React.useEffect(() => {
      const t = setTimeout(() => box.current && box.current.focus(), 50);
      const onKey = (e) => e.key === "Escape" && onCancel();
      window.addEventListener("keydown", onKey);
      return () => { clearTimeout(t); window.removeEventListener("keydown", onKey); };
    }, []);
    React.useEffect(() => {
      const term = q.trim();
      if (term.length < 2) { setList([]); return undefined; }
      let live = true;
      const t = setTimeout(() => {
        gql(`query ($f: StudioFilterType) { findStudios(studio_filter: $f, filter: { per_page: 12, sort: "scenes_count", direction: DESC }) {
            studios { id name aliases scene_count image_count parent_studio { name } } } }`,
          { f: { OR: { aliases: { value: term, modifier: "INCLUDES" } }, name: { value: term, modifier: "INCLUDES" } } })
          .then((d) => live && setList(d.findStudios.studios.map((x) => ({ ...x, count: (x.scene_count || 0) + (x.image_count || 0) }))))
          .catch(() => live && setList([]));
      }, 250);
      return () => { live = false; clearTimeout(t); };
    }, [q]);
    const row = (st) => h("button", { key: st.id, type: "button", className: "bta-pick-row", onClick: () => setChosen(st) },
      h("img", { className: "bta-pick-logo", src: `/studio/${st.id}/image`, alt: "", loading: "lazy" }),
      h("span", { className: "bta-pick-text" },
        h("span", { className: "bta-pick-name" }, st.name, st.parent_studio && h("span", null, ` · ${st.parent_studio.name}`)),
        h("span", { className: "bta-pick-sub" }, [st.score != null ? `${st.score}% alike` : "", st.via ? `as "${st.via}"` : "",
          st.count ? `${st.count} item${st.count === 1 ? "" : "s"}` : "",
          (st.aliases || []).length ? `also ${st.aliases.slice(0, 2).join(", ")}` : ""].filter(Boolean).join(" · "))));
    const n = using;
    return h("div", { className: "bta-confirm", onClick: (e) => e.target === e.currentTarget && onCancel() },
      h("div", { className: "bta-confirm-box bta-cf bta-pick", role: "dialog", "aria-modal": true, "aria-label": "Add to a studio you already have" },
        h("div", { className: "bta-cf-scroll" },
          h("div", { className: "bta-confirm-title" }, chosen ? `Add to ${chosen.name}?` : "Add to a studio you already have"),
          chosen
            ? h("div", { className: "bta-pick-chosen" },
                h("img", { className: "bta-pick-logo", src: `/studio/${chosen.id}/image`, alt: "" }),
                h("div", null,
                  h("div", { className: "bta-pick-name" }, chosen.name),
                  h("div", { className: "bta-confirm-text" }, `Puts ${n === 1 ? "this item" : `these ${n} items`} in ${chosen.name}, and adds "${it.name}" to its aliases so the watermark finds it next time.`)))
            : h(React.Fragment, null,
                h("div", { className: "bta-confirm-text" }, `Instead of making "${it.name}", put ${n === 1 ? "this item" : `these ${n} items`} in a studio you have.`),
                h("input", { ref: box, className: "bta-np-name", type: "search", value: q, placeholder: "Search your studios…",
                  onChange: (e) => setQ(e.target.value) }),
                q.trim().length >= 2 && h("div", { className: "bta-pick-list" },
                  list.length === 0 && h("div", { className: "bta-confirm-text" }, "No studio by that name"),
                  list.map(row)),
                h("div", { className: "bta-cf-label" }, "Names most like it"),
                h("div", { className: "bta-pick-list" },
                  (it.near || []).length === 0 && h("div", { className: "bta-confirm-text" }, "No studio's name is close"),
                  (it.near || []).map(row)))),
        h("div", { className: "bta-confirm-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: () => (chosen ? setChosen(null) : onCancel()) }, chosen ? "Back" : "Cancel"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: !chosen, onClick: () => onPick(chosen) },
            chosen ? `✓ Add to ${chosen.name}` : "Pick a studio"))));
  }

  // Tags → Similar / New: merge two tags, nest one under another, or make a
  // tag for a group of look-alike pictures
  function TagTidyCard({ it: shownIt, next, drag, tint, busy, out, keep, setKeep, name, setName, onAll, onExisting, onPage, answer, handlers, swapped, onSwap }) {
    // a sub-tag card the other way round (⇅): the parent becomes the sub-tag
    const it = shownIt.type === "tagsub" && swapped
      ? { ...shownIt, child: shownIt.parent, parent: shownIt.child, reasons: ["Switched from the suggestion (tap ⇅ again to switch back)"] } : shownIt;
    const t = it.type;
    const tagPic = (x, big) => h("button", {
      type: "button", className: "bta-fr-avatar-btn", title: `See ${x.name}'s page`,
      onPointerDown: (e) => e.stopPropagation(), onClick: () => onPage(`/tags/${x.id}`),
    }, h("img", { className: "bta-fr-avatar bta-fr-avatar-tag" + (big ? " bta-tt-big" : ""), src: `/tag/${x.id}/image`, alt: "", draggable: false }),
      h("span", { className: "bta-fr-avatar-hint" }, "View"));
    const items = (n) => `${n} item${n === 1 ? "" : "s"}`;
    const perfPic = (x) => h("button", {
      type: "button", className: "bta-fr-avatar-btn", title: `See ${x.name}'s page`,
      onPointerDown: (e) => e.stopPropagation(), onClick: () => onPage(`/performers/${x.id}`),
    }, h("img", { className: "bta-fr-avatar bta-tt-big", src: `/performer/${x.id}/image`, alt: "", draggable: false }),
      h("span", { className: "bta-fr-avatar-hint" }, "View"));
    let top, body, labels;
    if (t === "perfmerge") {
      const keepName = ((it.perfs.find((x) => x.id === keep) || {}).name || "").replace(/ \(.*\)$/, "");
      const typed = name != null ? name : keepName;
      labels = ["✕ Not the same", "✓ Merge", "NOT SAME", "MERGE"];
      top = h("div", { className: "bta-st-list" }, it.perfs.map((x) => h("div", { key: x.id, className: "bta-st-row" + (x.id === keep ? " keep" : "") },
        perfPic(x),
        h("div", { className: "bta-st-info" },
          h("div", { className: "bta-st-name" }, (x.favorite ? "♥ " : "") + x.name),
          h("div", { className: "bta-fr-meta" }, items(x.count) + (x.aliases.length ? ` · also ${x.aliases.slice(0, 3).join(", ")}` : ""))),
        h("div", { className: "bta-st-choose" },
          h("button", { type: "button", className: "bta-st-opt" + (x.id === keep ? " on" : ""), disabled: x.id === keep, onClick: () => setKeep(x.id) },
            x.id === keep ? "✓ Keeping" : "Keep this")))));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, "The same person?"),
        h("input", { key: "n", className: "bta-np-name", type: "text", value: typed, placeholder: "Name to keep",
          onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
        h("div", { key: "m2", className: "bta-fr-meta bta-st-wide" },
          "Merging moves everything to the one you keep, adds the other's names to its aliases and deletes the other. Stash can't undo a merge."),
      ];
    } else if (t === "tagmerge") {
      const keepName = (it.tags.find((x) => x.id === keep) || {}).name || "";
      const typed = name != null ? name : keepName;
      labels = ["✕ Not the same", "✓ Merge", "NOT SAME", "MERGE"];
      top = h("div", { className: "bta-st-list" }, it.tags.map((x) => h("div", { key: x.id, className: "bta-st-row" + (x.id === keep ? " keep" : "") },
        tagPic(x),
        h("div", { className: "bta-st-info" },
          h("div", { className: "bta-st-name" }, x.name),
          h("div", { className: "bta-fr-meta" }, items(x.count) + (x.aliases.length ? ` · also ${x.aliases.slice(0, 3).join(", ")}` : ""))),
        h("div", { className: "bta-st-choose" },
          h("button", { type: "button", className: "bta-st-opt" + (x.id === keep ? " on" : ""), disabled: x.id === keep, onClick: () => setKeep(x.id) },
            x.id === keep ? "✓ Keeping" : "Keep this")))));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, "Merge these two tags?"),
        h("input", { key: "n", className: "bta-np-name", type: "text", value: typed, placeholder: "Name to keep",
          onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
        h("div", { key: "m2", className: "bta-fr-meta bta-st-wide" }, "Everything with the other moves to the one you keep; its name becomes an alias."),
      ];
    } else if (t === "tagsub") {
      labels = ["✕ No", "✓ Make it a sub-tag", "NO", "NEST"];
      top = h("div", { className: "bta-st-list" },
        h("div", { className: "bta-st-row" }, tagPic(it.parent), h("div", { className: "bta-st-info" },
          h("div", { className: "bta-st-name" }, it.parent.name), h("div", { className: "bta-fr-meta" }, items(it.parent.count)))),
        h("div", { className: "bta-st-row bta-tt-child" }, tagPic(it.child), h("div", { className: "bta-st-info" },
          h("div", { className: "bta-st-name" }, "↳ ", it.child.name), h("div", { className: "bta-fr-meta" }, items(it.child.count)))),
        h("button", { type: "button", className: "bta-st-opt bta-tt-swap", onPointerDown: (e) => e.stopPropagation(), onClick: onSwap,
          title: "Switch which one is the parent" }, "⇅ Switch parent and sub-tag"));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, "Make ", h("b", { key: "c" }, it.child.name), " a sub-tag of ", h("b", { key: "p" }, it.parent.name), "?"),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
        h("div", { key: "m2", className: "bta-fr-meta" }, `Nothing is retagged: ${it.child.name} just sits under ${it.parent.name} (and filtering for ${it.parent.name} with sub-tags finds it).`),
        h("button", { key: "x", type: "button", className: "bta-np-existing", onClick: () => answer("yes", it, { merge: true }) },
          `…or merge ${it.child.name} into ${it.parent.name} instead`),
      ];
    } else {
      const total = (it.all || []).length || it.count;
      const using = total - out.size;
      labels = ["✕ No", "✓ Create", "NO", "CREATE"];
      top = h("div", { className: "bta-fr-pic bta-np-pics n" + Math.min(it.samples.length, 6) },
        it.samples.map((smp) => h("button", { key: smp.key, type: "button", className: "bta-np-pic", title: "See all of them", onClick: onAll },
          h("img", { src: smp.picture, alt: "", draggable: false, loading: "lazy" }),
          smp.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶"))),
        h("button", { type: "button", className: "bta-np-seeall", onClick: onAll }, out.size ? `Using ${using} of ${total} · change` : `See all ${total}`));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, "New tag for these?"),
        h("input", { key: "n", className: "bta-np-name", type: "text", value: name || "", placeholder: "Tag name",
          onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
        h("button", { key: "x", type: "button", className: "bta-np-existing", onClick: onExisting }, "…or add them to a tag you already have"),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
      ];
    }
    const nextPic = next && (next.samples || [])[0];
    return h(React.Fragment, null,
      h("div", {
        className: "bta-fr-card bta-st-card" + (drag ? " bta-fr-dragging" : ""),
        style: drag ? { transform: `translateX(${drag.dx}px) rotate(${drag.dx / 25}deg)` } : null,
        ...handlers,
      },
        h("div", { className: "bta-st-top" }, top,
          h("span", { className: "bta-fr-stamp bta-fr-yes", style: { opacity: Math.max(0, tint) } }, labels[3]),
          h("span", { className: "bta-fr-stamp bta-fr-no", style: { opacity: Math.max(0, -tint) } }, labels[2])),
        h("div", { className: "bta-fr-who" },
          t === "tagnew" && h("span", { className: "bta-np-new", "aria-hidden": true }, "+"),
          h("div", { className: "bta-fr-text" }, ...body))),
      nextPic && h("link", { rel: "preload", as: "image", href: nextPic.picture }),
      h("div", { className: "bta-fr-actions" },
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy, onClick: () => answer("no") }, labels[0]),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => answer("skip") }, "Skip"),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy, onClick: () => answer("yes") }, labels[1])),
      h("div", { className: "bta-fr-hint" }, "Swipe right for yes, left for no."));
  }

  // "New tag for these?" → "…or add them to a tag you already have"
  function PickTag({ it, using, onCancel, onPick }) {
    const [q, setQ] = React.useState("");
    const [list, setList] = React.useState([]);
    const [chosen, setChosen] = React.useState(null);
    const box = React.useRef(null);
    React.useEffect(() => {
      const t = setTimeout(() => box.current && box.current.focus(), 50);
      const onKey = (e) => e.key === "Escape" && onCancel();
      window.addEventListener("keydown", onKey);
      return () => { clearTimeout(t); window.removeEventListener("keydown", onKey); };
    }, []);
    React.useEffect(() => {
      const term = q.trim();
      if (term.length < 2) { setList([]); return undefined; }
      let live = true;
      const t = setTimeout(() => {
        gql(`query ($q: String) { findTags(filter: { q: $q, per_page: 12, sort: "scenes_count", direction: DESC }) { tags { id name aliases scene_count image_count } } }`, { q: term })
          .then((d) => live && setList(d.findTags.tags.map((x) => ({ ...x, n: (x.scene_count || 0) + (x.image_count || 0) }))))
          .catch(() => live && setList([]));
      }, 250);
      return () => { live = false; clearTimeout(t); };
    }, [q]);
    const row = (x) => h("button", { key: x.id, type: "button", className: "bta-pick-row", onClick: () => setChosen(x) },
      h("img", { className: "bta-pick-logo", src: `/tag/${x.id}/image`, alt: "", loading: "lazy" }),
      h("span", { className: "bta-pick-text" },
        h("span", { className: "bta-pick-name" }, x.name),
        h("span", { className: "bta-pick-sub" }, x.count != null ? `on ${x.count} of these` : x.n ? `${x.n} items` : "")));
    return h("div", { className: "bta-confirm", onClick: (e) => e.target === e.currentTarget && onCancel() },
      h("div", { className: "bta-confirm-box bta-cf bta-pick", role: "dialog", "aria-modal": true, "aria-label": "Add them to a tag you already have" },
        h("div", { className: "bta-cf-scroll" },
          h("div", { className: "bta-confirm-title" }, chosen ? `Tag them ${chosen.name}?` : "Add them to a tag you already have"),
          chosen
            ? h("div", { className: "bta-confirm-text" }, `Adds ${chosen.name} to ${using === 1 ? "this item" : `these ${using} items`} (the ones that don't have it yet).`)
            : h(React.Fragment, null,
                h("input", { ref: box, className: "bta-np-name", type: "search", value: q, placeholder: "Search your tags…", onChange: (e) => setQ(e.target.value) }),
                q.trim().length >= 2 && h("div", { className: "bta-pick-list" },
                  list.length === 0 && h("div", { className: "bta-confirm-text" }, "No tag by that name"), list.map(row)),
                h("div", { className: "bta-cf-label" }, "Already on some of them"),
                h("div", { className: "bta-pick-list" },
                  (it.near || []).length === 0 && h("div", { className: "bta-confirm-text" }, "None"),
                  (it.near || []).map(row)))),
        h("div", { className: "bta-confirm-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", onClick: () => (chosen ? setChosen(null) : onCancel()) }, chosen ? "Back" : "Cancel"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: !chosen, onClick: () => onPick(chosen) },
            chosen ? `✓ Tag them ${chosen.name}` : "Pick a tag"))));
  }

  // Galleries tab: add photos to a gallery, add the performers / studio most
  // of its photos have, or make a new gallery from a folder
  function GalleryCard({ it, next, drag, tint, busy, out, toggle, name, setName, onAll, onPage, answer, handlers }) {
    const t = it.type;
    const total = (it.all || []).length || it.count || 0;
    const using = total - [...out].filter((k) => !k.startsWith("x:")).length;
    const typed = name != null ? name : it.name || "";
    const chips = [...(it.performers || []).map((p) => ({ key: "x:p:" + p.id, label: p.name, kind: "performer" })),
      ...(it.studio ? [{ key: "x:s:" + it.studio.id, label: it.studio.name, kind: "studio" }] : [])];
    const chipRow = chips.length > 0 && h("div", { className: "bta-np-ideas" }, chips.map((c) => h("button", {
      key: c.key, type: "button", className: "bta-np-idea" + (out.has(c.key) ? "" : " active"),
      title: out.has(c.key) ? "Left out - tap to add it" : "Tap to leave it out", onClick: () => toggle(c.key),
    }, (out.has(c.key) ? "" : "✓ ") + (c.kind === "studio" ? "Studio: " : "") + c.label)));
    const cover = it.gallery && h("button", {
      type: "button", className: "bta-fr-avatar-btn", title: `See ${it.gallery.name}`,
      onPointerDown: (e) => e.stopPropagation(), onClick: () => onPage(`/galleries/${it.gallery.id}`),
    }, h("img", { className: "bta-fr-avatar bta-st-logo bta-gal-cover", src: `/gallery/${it.gallery.id}/cover`, alt: "", draggable: false }),
      h("span", { className: "bta-fr-avatar-hint" }, "View"));
    const labels = {
      gjoin: ["✕ No", `✓ Add ${using}`, "NO", "ADD"],
      gdetails: ["✕ No", "✓ Add", "NO", "ADD"],
      gnew: ["✕ No", "✓ Create", "NO", "CREATE"],
    }[t];
    const body = t === "gjoin"
      ? [h("div", { key: "q", className: "bta-fr-q" }, "Add ", using === 1 ? "it" : `these ${using}`, " to ", h("b", { key: "b" }, it.gallery.name), "?"),
         h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · "))]
      : t === "gdetails"
      ? [h("div", { key: "q", className: "bta-fr-q" }, "Add to the gallery ", h("b", { key: "b" }, it.gallery.name), "?"),
         chipRow,
         h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ") + " (tap one to leave it out)")]
      : [h("div", { key: "q", className: "bta-fr-q" }, "New gallery?"),
         h("input", { key: "n", className: "bta-np-name", type: "text", value: typed, placeholder: "Gallery name",
           onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
         chipRow,
         h("div", { key: "m", className: "bta-fr-meta" }, it.reasons[0] + `. Adds ${using === total ? `all ${total}` : `${using} of the ${total}`}.`)];
    const nextPic = next && (next.samples || [])[0];
    return h(React.Fragment, null,
      h("div", {
        className: "bta-fr-card bta-st-card" + (drag ? " bta-fr-dragging" : ""),
        style: drag ? { transform: `translateX(${drag.dx}px) rotate(${drag.dx / 25}deg)` } : null,
        ...handlers,
      },
        h("div", { className: "bta-st-top" },
          h("div", { className: "bta-fr-pic bta-np-pics n" + Math.min(it.samples.length, 6) },
            it.samples.map((smp) => h("button", { key: smp.key, type: "button", className: "bta-np-pic", title: "See all of them", onClick: onAll },
              h("img", { src: smp.picture, alt: "", draggable: false, loading: "lazy" }))),
            h("button", { type: "button", className: "bta-np-seeall", onClick: onAll },
              t === "gdetails" ? `See all ${total}` : out.size && using < total ? `Using ${using} of ${total} · change` : `See all ${total}`)),
          h("span", { className: "bta-fr-stamp bta-fr-yes", style: { opacity: Math.max(0, tint) } }, labels[3]),
          h("span", { className: "bta-fr-stamp bta-fr-no", style: { opacity: Math.max(0, -tint) } }, labels[2])),
        h("div", { className: "bta-fr-who" },
          cover || h("span", { className: "bta-np-new", "aria-hidden": true }, "+"),
          h("div", { className: "bta-fr-text" }, ...body))),
      nextPic && h("link", { rel: "preload", as: "image", href: nextPic.picture }),
      h("div", { className: "bta-fr-actions" },
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy, onClick: () => answer("no") }, labels[0]),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => answer("skip") }, "Skip"),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy, onClick: () => answer("yes") }, labels[1])),
      h("div", { className: "bta-fr-hint" }, "Swipe right for yes, left for no."));
  }

  // Studios tab: one suggestion - merge studios, delete one, add items to one
  // (or a new one)
  function StudioCard({ it, next, drag, tint, busy, out, pick, name, setName, setPick, onAll, onExisting, onZoom, onPage, answer, handlers }) {
    const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
    const what = (s) => [s.scenes ? plural(s.scenes, "scene") : "", s.photos ? plural(s.photos, "photo") : "",
      s.count - s.scenes - s.photos > 0 ? plural(s.count - s.scenes - s.photos, "gallery").replace("gallerys", "galleries") : ""]
      .filter(Boolean).join(", ") || "nothing in it";
    const t = it.type;
    const logo = (s) => h("button", {
      type: "button", className: "bta-fr-avatar-btn", title: `See ${s.name}'s page`,
      onPointerDown: (e) => e.stopPropagation(), onClick: () => onPage(`/studios/${s.id}`),
    }, h("img", { className: "bta-fr-avatar bta-st-logo", src: `/studio/${s.id}/image`, alt: "", draggable: false }),
      h("span", { className: "bta-fr-avatar-hint" }, "View"));
    const thumbs = (list) => h("div", { className: "bta-st-thumbs" + (t === "delete" ? " big" : "") },
      list.map((x) => h("button", { key: x.key, type: "button", className: "bta-st-thumb", title: "See it big", onClick: () => onZoom(x, list) },
        h("img", { src: x.picture, alt: "", loading: "lazy", draggable: false }),
        x.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶"))));
    const total = (it.all || []).length || it.count || 0;
    const using = total - out.size;
    const keep = pick.keep || it.keep;
    const off = pick.off || new Set();
    const keepName = t === "merge" ? (it.studios.find((x) => x.id === keep) || {}).name : "";
    const typed = name != null ? name : t === "merge" ? keepName : it.name || "";
    const labels = {
      merge: ["✕ Not the same", "✓ Merge", "NOT SAME", "MERGE"],
      delete: ["✕ Keep it", "🗑 Delete", "KEEP", "DELETE"],
      join: ["✕ No", `✓ Add ${using}`, "NO", "ADD"],
      add: ["✕ No", "✓ Create", "NO", "CREATE"],
    }[t];
    let top, body;
    if (t === "merge") {
      const merging = it.studios.filter((x) => x.id === keep || !off.has(x.id)).length;
      top = h("div", { className: "bta-st-list" }, it.studios.map((s) => {
        const isKeep = s.id === keep, isOff = !isKeep && off.has(s.id);
        return h("div", { key: s.id, className: "bta-st-row" + (isKeep ? " keep" : "") + (isOff ? " off" : "") },
          logo(s),
          h("div", { className: "bta-st-info" },
            h("div", { className: "bta-st-name" }, s.name, s.parent && h("span", { className: "bta-st-parent" }, " · " + s.parent)),
            h("div", { className: "bta-fr-meta" }, what(s) + (s.aliases.length ? ` · also ${s.aliases.slice(0, 3).join(", ")}` : ""))),
          h("div", { className: "bta-st-choose" },
            h("button", { type: "button", className: "bta-st-opt" + (isKeep ? " on" : ""), disabled: isKeep,
              onClick: () => { setPick({ keep: s.id, off: new Set([...off].filter((x) => x !== s.id)) }); setName(null); } },
              isKeep ? "✓ Keeping" : "Keep this"),
            !isKeep && h("button", { type: "button", className: "bta-st-opt" + (isOff ? " on" : ""),
              onClick: () => { const o = new Set(off); if (o.has(s.id)) o.delete(s.id); else o.add(s.id); setPick({ keep, off: o }); } },
              isOff ? "Left out" : "Leave out")),
          s.samples.length > 0 && thumbs(s.samples));
      }));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, `Merge ${merging === 2 ? "these two" : `these ${merging}`} into one studio?`),
        h("input", { key: "n", className: "bta-np-name", type: "text", value: typed, placeholder: "Name to keep",
          onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
        h("div", { key: "i", className: "bta-np-ideas" }, it.studios.map((s) => h("button", {
          key: s.id, type: "button", className: "bta-np-idea" + (typed === s.name ? " active" : ""), onClick: () => setName(s.name),
        }, s.name))),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
        h("div", { key: "m2", className: "bta-fr-meta bta-st-wide" }, "Everything moves to the one you keep; the other names become its aliases."),
      ];
    } else if (t === "delete") {
      const s = it.studios[0];
      top = h("div", { className: "bta-st-list" }, h("div", { className: "bta-st-row" }, logo(s),
        h("div", { className: "bta-st-info" },
          h("div", { className: "bta-st-name" }, s.name, s.parent && h("span", { className: "bta-st-parent" }, " · " + s.parent)),
          h("div", { className: "bta-fr-meta" }, what(s))),
        s.samples.length > 0 && thumbs(s.samples)));
      body = [
        h("div", { key: "q", className: "bta-fr-q" }, "Delete ", h("b", { key: "b" }, s.name), "?"),
        h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
        h("div", { key: "m2", className: "bta-fr-meta" }, s.count ? `Its ${plural(s.count, "item")} are left without a studio (nothing else is deleted).` : "Nothing is in it."),
      ];
    } else {
      top = h("div", { className: "bta-fr-pic bta-np-pics n" + Math.min(it.samples.length, 6) },
        it.samples.map((smp) => h("button", { key: smp.key, type: "button", className: "bta-np-pic", title: "See all of them", onClick: onAll },
          h("img", { src: smp.picture, alt: "", draggable: false, loading: "lazy" }),
          smp.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶"))),
        h("button", { type: "button", className: "bta-np-seeall", onClick: onAll }, out.size ? `Using ${using} of ${total} · change` : `See all ${total}`));
      body = t === "join"
        ? [h("div", { key: "q", className: "bta-fr-q" }, "Add ", using === 1 ? "it" : "these", " to ", h("b", { key: "b" }, it.studio.name), "?"),
           h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
           it.places && it.places.length > 1 && h("div", { key: "p", className: "bta-fr-meta bta-st-places" },
             it.places.map((pl) => `${pl.name} (${pl.missing})`).join(" · "))]
        : [h("div", { key: "q", className: "bta-fr-q" }, "New studio?"),
           h("input", { key: "n", className: "bta-np-name", type: "text", value: typed, placeholder: "Studio name",
             onChange: (e) => setName(e.target.value), onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); } }),
           h("button", { key: "x", type: "button", className: "bta-np-existing", onClick: onExisting }, "…or add to a studio you already have"),
           h("div", { key: "m", className: "bta-fr-meta" }, it.reasons.join(" · ")),
           h("div", { key: "m2", className: "bta-fr-meta" }, `Makes it${it.platform ? " under " + it.platform.replace(/^./, (c) => c.toUpperCase()).replace("Onlyfans", "OnlyFans").replace("Tiktok", "TikTok") : ""} and adds ${using === 1 ? "it" : `these ${using}`}. The AI read the name: check its spelling.`)];
    }
    const nextPic = next && (next.samples || (next.studios && next.studios[0].samples) || [])[0];
    return h(React.Fragment, null,
      h("div", {
        className: "bta-fr-card bta-st-card" + (drag ? " bta-fr-dragging" : ""),
        style: drag ? { transform: `translateX(${drag.dx}px) rotate(${drag.dx / 25}deg)` } : null,
        ...handlers,
      },
        h("div", { className: "bta-st-top" }, top,
          h("span", { className: "bta-fr-stamp bta-fr-yes", style: { opacity: Math.max(0, tint) } }, labels[3]),
          h("span", { className: "bta-fr-stamp bta-fr-no", style: { opacity: Math.max(0, -tint) } }, labels[2])),
        h("div", { className: "bta-fr-who" },
          t === "join" ? logo(it.studio) : t === "add" ? h("span", { className: "bta-np-new", "aria-hidden": true }, "+") : null,
          h("div", { className: "bta-fr-text" }, ...body))),
      nextPic && h("link", { rel: "preload", as: "image", href: nextPic.picture }),
      h("div", { className: "bta-fr-actions" },
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy, onClick: () => answer("no") }, labels[0]),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => answer("skip") }, "Skip"),
        h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy, onClick: () => answer("yes") }, labels[1])),
      h("div", { className: "bta-fr-hint" }, "Swipe right for yes, left for no."));
  }

  // Review waits for the first word on what's installed (a moment, once per
  // browser), so it doesn't start loading a tab it then switches away from
  function FaceReview() {
    const cz = useCaps();
    if (!caps.data) return h("div", { className: "bta-fr" }, h("div", { className: "bta-fr-empty" }, "Loading…"));
    return h(FaceReviewPage, { cz });
  }

  function FaceReviewPage({ cz }) {
    const rGroups = reviewGroups(cz);
    const allowed = rGroups.flatMap((g) => g.actions);
    const params = new URLSearchParams(window.location.search);
    const asked0 = OLD_MODES[params.get("mode")] || params.get("mode");
    const asked = window.location.pathname === REVIEW_PATH ? "faces" : asked0;
    const tabKey = ACTIONS[asked] && allowed.includes(asked) ? asked : (allowed[0] || "studios_add");
    const M = ACTIONS[tabKey];
    const tab = M.base; // what kind of cards (faces, tags, check, new, studios)
    REVIEW_MODES._checkOnly = M.checkOnly || null;
    REVIEW_MODES._studioKinds = M.studioKinds || null;
    REVIEW_MODES._galleryKinds = M.galleryKinds || null;
    REVIEW_MODES._tidyMode = M.tidyMode || null;
    const grp = rGroups.find((g) => g.actions.includes(tabKey)) || groupOf(tabKey);
    React.useEffect(() => { try { localStorage.setItem("bta-review-" + grp.key, tabKey); } catch (e) { /* fine */ } }, [tabKey]);
    const only = params.get(M.param) || "";
    const favOnly = tab === "faces" && params.get("fav") === "1"; // Faces: favourite performers only
    const [queue, setQueue] = React.useState([]);
    const [state, setState] = React.useState({ loading: true });
    const [total, setTotal] = React.useState(0);
    const [counts, setCounts] = React.useState({ yes: 0, no: 0, skip: 0 });
    const [undo, setUndo] = React.useState([]);
    const [drag, setDrag] = React.useState(null); // { dx } while swiping
    const [busy, setBusy] = React.useState(false);
    const [zoom, setZoom] = React.useState(null);
    // Faces: each card's picture is the frame where the face was seen best,
    // cut around it (a scene's cover may not show them at all) - made by the
    // add-on for the card on top and the next ones, then kept
    const [cardPics, setCardPics] = React.useState({});
    const picAsked = React.useRef(new Set());
    // Swipe (one at a time) or List (all of them as a grid); remembered
    const [view, setViewState] = React.useState(() => {
      try { const v = localStorage.getItem("bta-review-view"); return v === "list" || v === "batch" ? v : "swipe"; } catch (e) { return "swipe"; }
    });
    const setView = (v) => {
      setViewState(v);
      try { localStorage.setItem("bta-review-view", v); } catch (e) { /* fine */ }
    };
    const [working, setWorking] = React.useState(() => new Set()); // list cards being saved
    const [names, setNames] = React.useState({}); // New tab: the name typed for each suggestion
    // Batch view: one performer's / tag's suggestions at a time
    const [groups, setGroups] = React.useState(null);  // [{id, name, count}]
    const [group, setGroup] = React.useState(null);    // the group's id
    const [batch, setBatch] = React.useState(null);    // {state, items}
    const [batchOff, setBatchOff] = React.useState(() => new Set()); // keys unticked
    const [batchKey, setBatchKey] = React.useState(0); // reload
    const inBatch = view === "batch" && !M.card;
    // Batch on a phone: the pictures scroll in a box that ends above the
    // buttons (and Modern UI's tab bar), so the buttons never cover them
    const batchBox = React.useRef(null);
    React.useLayoutEffect(() => {
      const el = batchBox.current;
      if (!el) return undefined;
      const fit = () => {
        if (window.innerWidth > 576) { el.style.height = ""; return; }
        const nav = document.querySelector(".mui-bottom-nav");
        const navH = nav && getComputedStyle(nav).display !== "none" ? nav.getBoundingClientRect().height : 0;
        const top = el.getBoundingClientRect().top + window.scrollY;
        el.style.height = Math.max(320, window.innerHeight - top - navH - 18) + "px";
      };
      fit();
      window.addEventListener("resize", fit);
      return () => window.removeEventListener("resize", fit);
    });
    const [studioPick, setStudioPick] = React.useState({}); // Studios tab: sig -> {keep, off: Set of studio ids}
    const [pickStudio, setPickStudio] = React.useState(null); // "New studio?" card: add to one you have instead
    const [pickTag, setPickTag] = React.useState(null); // "New tag?" card: add to a tag you have instead
    const batchFresh = React.useRef(false);
    const groupsFor = React.useRef(""); // which tab (and filter) the groups belong to
    const [confirmNew, setConfirmNew] = React.useState(null); // { it, name }: "Create a new performer?"
    const [pickExisting, setPickExisting] = React.useState(null); // the suggestion to add someone you have to
    const [leftOut, setLeftOut] = React.useState({}); // New tab: sig -> Set of item keys left out
    const [allOf, setAllOf] = React.useState(null);   // the suggestion whose items are being looked through
    // New tab: names the AI read in a suggestion's pictures (sig -> {state, names})
    const [aiNames, setAiNames] = React.useState({});
    const reading = React.useRef(false);
    const [readTick, setReadTick] = React.useState(0); // a read finished: the next card can start
    const ideasOf = (it) => {
      const out = [...(it.names || [])];
      ((aiNames[it.sig] || {}).names || []).forEach((n) => {
        if (!out.some((x) => x.name.toLowerCase() === n.name.toLowerCase())) out.push(n);
      });
      return out;
    };
    const seen = React.useRef(new Set());
    const loadingMore = React.useRef(false);
    const start = React.useRef(null);
    const keyOf = M.keyOf;
    // the add-on asks Stash at localhost, so its links say localhost: use
    // the same path on whatever address this page was opened with (phone)
    const here = (link, kind, id) => {
      try {
        const u = new URL(link, window.location.origin);
        return u.pathname + u.search;
      } catch (e) {
        return kind === "scene" ? `/scene/${id}/screenshot` : `/image/${id}/image`;
      }
    };

    const load = React.useCallback(async (fresh, recompute) => {
      if (loadingMore.current) return;
      loadingMore.current = true;
      setState((st) => ({ ...st, loading: true }));
      try {
        const r = await M.load(only, [...seen.current], recompute);
        const items = (r.items || []).filter((it) => !seen.current.has(keyOf(it)))
          .map((it) => ({ ...it, picture: here(it.picture, it.kind, it.id) }));
        items.forEach((it) => seen.current.add(keyOf(it)));
        setQueue((q) => (fresh ? items : [...q, ...items]));
        // r.total counts what's left apart from the ones already shown - but
        // when none of them came back (e.g. their scenes were deleted), say so
        // instead of showing "N left" over an empty page
        const stuck = !items.length && (r.total || 0) > 0;
        setTotal(stuck ? 0 : Math.max(0, (r.total || 0) - items.length));
        setState({ loading: false, note: stuck
          ? `${r.total} more suggestion${r.total === 1 ? "" : "s"} couldn't be loaded (their scenes or images may have been deleted from Stash). Leave and come back to check again.`
          : r.note || "" });
      } catch (e) {
        setState({ loading: false, error: e.message });
      } finally {
        loadingMore.current = false;
      }
    }, [only, tabKey, favOnly]);

    React.useEffect(() => {
      const mem = reviewMemory;
      if (mem && mem.tab === tabKey && mem.only === only && !!mem.fav === favOnly && Date.now() - mem.at < 30 * 60 * 1000) {
        seen.current = new Set(mem.seen);
        setQueue(mem.queue);
        setCounts(mem.counts);
        setUndo(mem.undo);
        setTotal(mem.total);
        setState({ loading: false });
        return;
      }
      seen.current = new Set();
      setQueue([]);
      setCounts({ yes: 0, no: 0, skip: 0 });
      setUndo([]);
      load(true);
    }, [only, tabKey, favOnly]);

    // remember where you are (for coming back from a performer's / tag's page)
    React.useEffect(() => {
      if (state.loading) return;
      reviewMemory = { tab: tabKey, only, fav: favOnly, queue, counts, undo, total, seen: [...seen.current], at: Date.now() };
    }, [queue, counts, undo, total, state.loading]);

    // (for a moment after switching tabs the old tab's cards are still here)
    const shown = queue.filter((it) => (tab === "tags" ? it.tag != null && (tabKey === "tags_poses" ? !!it.pose : !it.pose && (tabKey === "tags_markers") === (it.kind === "marker" && !it.for_scene)) : tab === "new" ? it.sig != null && !it.st && !it.gal && !it.tp && !it.tg
      : tab === "studios" ? !!it.st : tab === "galleries" ? !!it.gal : tab === "tagtidy" ? !!(it.tp || it.tg)
      : tab === "check" ? it.what != null : it.kind != null && it.performer != null));
    const cur = shown[0];
    const next = shown[1];
    React.useEffect(() => {
      if (!state.loading && queue.length < 6 && total > 0) load(false);
    }, [queue.length]);
    const faceCard = (it) => tab === "faces" && it && it.performer != null && (it.kind === "scene" || it.kind === "image");
    const picOf = (it) => (faceCard(it) && cardPics[keyOf(it)]) || it.picture;
    React.useEffect(() => {
      // the card on top and the next two (Swipe), or what's shown (List)
      const want = (view === "swipe" ? shown.slice(0, 3) : shown.slice(0, 40)).filter((it) => faceCard(it) && !picAsked.current.has(keyOf(it)));
      let live = true;
      (async () => {
        for (const it of want) {
          if (!live) return;
          const k = keyOf(it);
          if (picAsked.current.has(k)) continue;
          picAsked.current.add(k);
          try {
            const r = await ask({ mode: "face_card_picture", kind: it.kind, id: it.id, at: it.best_at, emb: it.emb });
            if (r && r.image) setCardPics((m) => ({ ...m, [k]: "data:image/jpeg;base64," + r.image }));
          } catch (e) { /* the card keeps its picture */ }
        }
      })();
      return () => { live = false; };
    }, [tab, view, cur && keyOf(cur), shown.length]);

    // answer the card on top (Swipe), or any card (List)
    async function answer(kind, it, confirmed) {
      let one = it || cur;
      if (!one) return;
      if (tab === "new") {
        const out = leftOut[one.sig];
        if (out && out.size) {
          one = { ...one, keys: one.keys.filter((k) => !out.has(k)), missing: (one.missing || []).filter((k) => !out.has(k)) };
          one.count = one.type === "assign" ? one.missing.length : one.keys.length;
        }
        if (kind === "yes" && one.count === 0) {
          setState((st) => ({ ...st, note: "Every item is left out - nothing to add." }));
          setDrag(null);
          return;
        }
      }
      if (tab === "studios") {
        const out = leftOut[one.sig];
        const fail = (msg) => { setState((st) => ({ ...st, note: msg })); setDrag(null); };
        if (one.type === "join" || one.type === "add") {
          if (out && out.size) one = { ...one, keys: one.keys.filter((k) => !out.has(k)) };
          if (kind === "yes" && !one.keys.some((k) => !k.startsWith("gallery:"))) return fail("Every item is left out - nothing to add.");
        }
        if (one.type === "add" && confirmed && confirmed.studio) {
          // "…or add to a studio you already have": those items go to it,
          // and the name read in the watermark becomes one of its aliases
          one = { ...one, type: "join", studio: confirmed.studio, _alias: one.name, _orig: one };
        } else if (one.type === "add") {
          one = { ...one, _name: String(names[one.sig] != null ? names[one.sig] : one.name || "").trim() };
          if (kind === "yes" && !one._name) return fail("Give the studio a name first.");
        }
        if (one.type === "merge") {
          const pk = studioPick[one.sig] || {};
          const keep = pk.keep || one.keep;
          const off = pk.off || new Set();
          const others = one.studios.filter((x) => x.id !== keep && !off.has(x.id)).map((x) => x.id);
          const keepName = (one.studios.find((x) => x.id === keep) || {}).name || "";
          one = { ...one, _keep: keep, _others: others, _name: String(names[one.sig] != null ? names[one.sig] : keepName).trim() || keepName };
          if (kind === "yes" && !others.length) return fail("Everything else is left out - nothing to merge.");
        }
      }
      if (tab === "tagtidy") {
        const fail = (msg) => { setState((st) => ({ ...st, note: msg })); setDrag(null); };
        if (one.type === "tagnew") {
          const out = leftOut[one.sig] || new Set();
          one = { ...one, keys: one.keys.filter((k) => !out.has(k)) };
          if (kind === "yes" && !one.keys.length) return fail("Every picture is left out - nothing to tag.");
          if (confirmed && confirmed.tag) one = { ...one, type: "tagjoin", _tag: confirmed.tag, _orig: one };
          else {
            one = { ...one, _name: String(names[one.sig] || "").trim() };
            if (kind === "yes" && !one._name) return fail("Give the new tag a name first (or add them to a tag you have).");
          }
        }
        if (one.type === "tagsub" && (studioPick[one.sig] || {}).swap) {
          one = { ...one, child: one.parent, parent: one.child, _orig: one._orig || one };
        }
        if (one.type === "tagsub" && confirmed && confirmed.merge) {
          // "…or merge instead": the child becomes an alias of the parent
          one = { ...one, type: "tagmerge", tags: [one.parent, one.child], keep: one.parent.id, _orig: one._orig || one };
        }
        if (one.type === "perfmerge") {
          const keep = (studioPick[one.sig] || {}).keep || one.keep;
          const keepName = ((one.perfs.find((x) => x.id === keep) || {}).name || "").replace(/ \(.*\)$/, "");
          const others = one.perfs.filter((x) => x.id !== keep);
          one = { ...one, _keep: keep, _others: others.map((x) => x.id),
                  _name: String(names[one.sig] != null ? names[one.sig] : keepName).trim() || keepName };
          // (Stash can't undo it, so it always asks)
          if (kind === "yes" && !window.confirm(`Merge ${others.map((x) => `"${x.name}"`).join(", ")} into "${one._name}"?\n\n`
              + "Their scenes, images and galleries move over, their names become aliases, and they're deleted. This can't be undone.")) {
            setDrag(null);
            return;
          }
        }
        if (one.type === "tagmerge") {
          const keep = (studioPick[one.sig] || {}).keep || one.keep;
          const keepName = (one.tags.find((t) => t.id === keep) || {}).name || "";
          one = { ...one, _keep: keep, _others: one.tags.filter((t) => t.id !== keep).map((t) => t.id),
                  _name: String(names[one.sig] != null ? names[one.sig] : keepName).trim() || keepName };
        }
      }
      if (tab === "galleries") {
        const out = leftOut[one.sig] || new Set();
        const fail = (msg) => { setState((st) => ({ ...st, note: msg })); setDrag(null); };
        if (one.type === "gjoin" || one.type === "gnew") {
          one = { ...one, keys: one.keys.filter((k) => !out.has(k)) };
          if (kind === "yes" && !one.keys.length) return fail("Every photo is left out - nothing to add.");
        }
        // details: the performers / studio not tapped off ("x:p:ID")
        const perf = (one.performers || []).filter((p) => !out.has("x:p:" + p.id)).map((p) => p.id);
        const studio = one.studio && !out.has("x:s:" + one.studio.id) ? one.studio.id : null;
        one = { ...one, _performers: perf, _studio: studio };
        if (one.type === "gdetails" && kind === "yes" && !perf.length && !studio) return fail("Everything is tapped off - nothing to add.");
        if (one.type === "gnew") {
          one = { ...one, _name: String(names[one.sig] != null ? names[one.sig] : one.name || "").trim() };
          if (kind === "yes" && !one._name) return fail("Give the gallery a name first.");
        }
      }
      if (tab === "new" && one.type === "new" && confirmed && confirmed.existing) {
        one = { ...one, _existing: confirmed.existing };
      } else if (tab === "new" && one.type === "new") {
        one = { ...one, names: ideasOf(one) };
        one = { ...one, _name: confirmed != null ? confirmed.name : names[one.sig] != null ? names[one.sig] : one.name || "",
                _details: confirmed != null ? confirmed.details : null };
        // making a performer always asks first, with the name to check
        if (kind === "yes" && confirmed == null) {
          setConfirmNew({ it: one, name: one._name });
          setDrag(null);
          return;
        }
      }
      const k = keyOf(one);
      if (it ? working.has(k) : busy) return;
      if (it) setWorking((w) => new Set(w).add(k));
      else setBusy(true);
      try {
        if (kind === "yes") await M.yes(one);
        else if (kind === "no") await M.no(one);
        setUndo((u) => [...u.slice(-19), { kind, it: one }]);
        setCounts((c) => ({ ...c, [kind]: c[kind] + 1 }));
        setQueue((q) => q.filter((x) => keyOf(x) !== k));
      } catch (e) {
        setState((st) => ({ ...st, note: "Couldn't save that: " + e.message }));
      } finally {
        if (it) setWorking((w) => { const n = new Set(w); n.delete(k); return n; });
        else setBusy(false);
        setDrag(null);
      }
    }

    // at the end: work everything out again (new scans, tags, performers),
    // or bring back what was skipped
    function startOver(recompute) {
      seen.current = new Set();
      setQueue([]);
      setCounts((c) => ({ ...c, skip: 0 }));
      if (inBatch) {
        setGroups(null);
        setGroup(null);
        batchFresh.current = !!recompute;
        setBatchKey((k) => k + 1);
      }
      load(true, recompute);
    }
    const endButtons = () => h("div", { className: "bta-fr-again" },
      h("button", { type: "button", className: "btn btn-primary", onClick: () => startOver(true) }, "↻ Check for new suggestions"),
      (counts.skip > 0 || (inBatch && (groups || []).length > 0)) &&
        h("button", { type: "button", className: "btn btn-secondary", onClick: () => startOver(false) }, "Show the skipped ones again"));

    async function takeBack() {
      const last = undo[undo.length - 1];
      if (!last || busy) return;
      setBusy(true);
      try {
        if (last.kind === "batch" && last.batch.check) {
          const b0 = last.batch;
          if (b0.removed.length) await setOnKeys(b0.removed, b0.field, b0.id, "ADD");
          if (b0.pairs.length) await ask({ mode: "check_keep_many", pairs: b0.pairs, keep: false });
          setUndo((u) => u.slice(0, -1));
          setCounts((c) => ({ ...c, no: c.no - b0.removed.length, yes: c.yes - b0.pairs.length }));
          setBatchKey((k) => k + 1);
          return;
        }
        if (last.kind === "batch") {
          const b0 = last.batch;
          if (b0.added.length) await setOnKeys(b0.added, b0.field, b0.id, "REMOVE");
          if (b0.rejected.length) {
            await ask({ mode: "review_no_many", kind: tab, no: false,
              pairs: b0.rejected.map((it) => [`${it.kind}:${it.id}`, tab === "faces" ? it.performer : it.tag]) });
          }
          setUndo((u) => u.slice(0, -1));
          setCounts((c) => ({ ...c, yes: c.yes - b0.added.length, no: c.no - b0.rejected.length }));
          setBatchKey((k) => k + 1); // load that group again
          return;
        }
        const it = last.it;
        if (last.kind === "yes") await M.undoYes(it);
        else if (last.kind === "no") await M.undoNo(it);
        setUndo((u) => u.slice(0, -1));
        setCounts((c) => ({ ...c, [last.kind]: c[last.kind] - 1 }));
        setQueue((q) => [it._orig || it, ...q]); // (the card as it was shown)
      } catch (e) {
        setState((st) => ({ ...st, note: "Couldn't undo: " + e.message }));
      } finally {
        setBusy(false);
      }
    }

    // Batch view: the groups, then the chosen group's suggestions (all of them)
    const batchLoad = (gid, fresh) => (M.batchLoad ? M.batchLoad(gid, fresh) : tab === "faces"
      ? ask({ mode: "face_review", performer: gid, limit: 400, favourites: (new URLSearchParams(location.search).get("fav") === "1") })
      : tab === "check" ? ask({ mode: "check", group: gid, limit: gid ? 400 : 1, fresh: !!fresh, only: M.checkOnly })
      : ask({ mode: "tag_review", tag: gid, limit: 400, fresh: !!fresh }));
    React.useEffect(() => {
      if (!inBatch) return undefined;
      let live = true;
      setBatch({ state: "loading" });
      (async () => {
        try {
          // the groups are per tab: Tags' groups are tags, Faces' performers
          let gs = groupsFor.current === tabKey + "|" + only ? groups : null;
          if (!gs || batchKey) {
            const r = await batchLoad(only || null, batchFresh.current);
            batchFresh.current = false;
            gs = (r.groups || []).filter((g) => !only || String(g.id) === String(only));
            if (live) { groupsFor.current = tabKey + "|" + only; setGroups(gs); }
          }
          const gid = gs.some((g) => String(g.id) === String(group)) ? group : (gs[0] || {}).id;
          if (!gid) { live && setBatch({ state: "done", items: [] }); return; }
          if (live && gid !== group) setGroup(gid);
          const r = await batchLoad(gid);
          const items = (r.items || []).map((it) => ({ ...it, picture: here(it.picture, it.kind, it.id) }));
          // (Check: everything starts as kept - you pick what to remove)
          if (live) { setBatch({ state: "done", items }); setBatchOff(tab === "check" ? new Set(items.map(keyOf)) : new Set()); }
        } catch (e) {
          live && setBatch({ state: "error", message: e.message });
        }
      })();
      return () => { live = false; };
    }, [inBatch, tabKey, group, batchKey, only]);

    React.useEffect(() => {
      if (groupsFor.current && groupsFor.current !== tabKey + "|" + only) {
        setGroups(null);
        setGroup(null);
        setBatch({ state: "loading" });
      }
    }, [tabKey, only, favOnly]);

    async function checkBatchAnswer(kind) {
      const items = batch.items;
      const field = CHECK_FIELD[String(group)[0]];
      const id = String(group).slice(2);
      // (on Check, an item in batchOff is one you marked "it fits")
      const remove = kind === "yes" ? items.filter((it) => !batchOff.has(keyOf(it))) : [];
      const fits = kind === "yes" ? items.filter((it) => batchOff.has(keyOf(it))) : kind === "none" ? items : [];
      const removed = remove.map((it) => `${it.kind}:${it.id}`);
      const pairs = fits.map((it) => [`${it.kind}:${it.id}`, String(group)]);
      if (removed.length) await setOnKeys(removed, field, id, "REMOVE");
      if (pairs.length) await ask({ mode: "check_keep_many", pairs, keep: true });
      if (removed.length || pairs.length) {
        setUndo((u) => [...u.slice(-19), { kind: "batch", batch: { check: true, field, id, removed, pairs } }]);
        setCounts((c) => ({ ...c, no: c.no + removed.length, yes: c.yes + pairs.length }));
      }
    }

    async function batchAnswer(kind) {
      if (!batch || !batch.items || busy) return;
      setBusy(true);
      const items = batch.items;
      const field = tab === "faces" ? "performer_ids" : "tag_ids";
      const yes = tab === "check" ? [] : kind === "yes" ? items.filter((it) => !batchOff.has(keyOf(it))) : [];
      const no = tab === "check" ? [] : kind === "yes" ? items.filter((it) => batchOff.has(keyOf(it))) : kind === "none" ? items : [];
      try {
        if (tab === "check") await checkBatchAnswer(kind);
        if (yes.length) {
          await setOnKeys(yes.map((it) => `${it.kind}:${it.id}`), field, group, "ADD");
          if (tab === "faces") yes.slice(0, 8).forEach((it) => ask({ mode: "face_confirm", performer: it.performer, emb: it.emb }).catch(() => {}));
        }
        if (no.length) {
          const pairs = no.map((it) => [`${it.kind}:${it.id}`, tab === "faces" ? it.performer : it.tag]);
          await ask({ mode: "review_no_many", kind: tab, pairs, no: true });
        }
        if (yes.length || no.length) {
          setUndo((u) => [...u.slice(-19), { kind: "batch", batch: { field, id: group, added: yes.map((it) => `${it.kind}:${it.id}`), rejected: no } }]);
          setCounts((c) => ({ ...c, yes: c.yes + yes.length, no: c.no + no.length }));
        }
        // on to the next group (answered ones leave the list; a skipped one stays)
        const gs = groups || [];
        const i = gs.findIndex((g) => String(g.id) === String(group));
        const rest = kind === "skip" ? gs : gs.filter((g) => String(g.id) !== String(group));
        const next = kind === "skip" ? gs[(i + 1) % gs.length] : rest[Math.min(Math.max(i, 0), rest.length - 1)];
        setGroups(rest);
        setBatch({ state: "loading" });
        setGroup(next ? next.id : null);
        if (!next) setBatch({ state: "done", items: [] });
      } catch (e) {
        setState((st) => ({ ...st, note: "Couldn't save that: " + e.message }));
      } finally {
        setBusy(false);
      }
    }

    // New tab: have the AI read the text in the current card's pictures for
    // account names - one card at a time, once it's been showing a moment
    const curSig = tab === "new" && cur && cur.type === "new" ? cur.sig : null;
    React.useEffect(() => {
      if (!curSig || aiNames[curSig] || reading.current) return undefined;
      const it = cur;
      const t = setTimeout(() => {
        if (reading.current) return;
        reading.current = true;
        setAiNames((a) => ({ ...a, [it.sig]: { state: "reading" } }));
        ask({ mode: "new_perf_read", keys: it.keys.slice(0, 12) })
          .then((r) => {
            const found = (r && r.names) || [];
            setAiNames((a) => ({ ...a, [it.sig]: { state: "done", names: found } }));
            // nothing typed or guessed yet: take the AI's best
            if (found.length && !it.name) setNames((n) => (n[it.sig] ? n : { ...n, [it.sig]: found[0].name }));
          })
          .catch((e) => setAiNames((a) => ({ ...a, [it.sig]: { state: "error", message: e.message } })))
          .finally(() => { reading.current = false; setReadTick((n) => n + 1); });
      }, 1200);
      return () => clearTimeout(t);
    }, [curSig, aiNames[curSig || ""], readTick]);

    // keyboard: right = yes, left = not them, down = skip, Z / Backspace = undo
    React.useEffect(() => {
      const onKey = (e) => {
        if (zoom || confirmNew || allOf || pickExisting || (view !== "swipe" && !M.card)) return;
        if (inBatch) return;
        if (e.target.closest && e.target.closest("input, textarea, select")) return;
        if (e.key === "ArrowRight") answer("yes");
        else if (e.key === "ArrowLeft") answer("no");
        else if (e.key === "ArrowDown") { e.preventDefault(); answer("skip"); }
        else if (e.key === "z" || e.key === "Backspace") takeBack();
        else return;
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    });

    // swipe the card sideways (up and down still scrolls the page)
    const onDown = (e) => {
      if (busy || e.button > 0) return;
      if (e.target.closest && e.target.closest("input, button, a")) return;
      start.current = { x: e.clientX, y: e.clientY, id: e.pointerId, moved: false };
    };
    const onMove = (e) => {
      const s0 = start.current;
      if (!s0 || s0.id !== e.pointerId) return;
      const dx = e.clientX - s0.x, dy = e.clientY - s0.y;
      if (!s0.moved) {
        if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx)) { start.current = null; return; }
        if (Math.abs(dx) < 10) return;
        s0.moved = true;
        e.currentTarget.setPointerCapture && e.currentTarget.setPointerCapture(e.pointerId);
      }
      setDrag({ dx });
    };
    const onUp = (e) => {
      const s0 = start.current;
      start.current = null;
      if (s0 && !s0.moved && cur && e.target.closest && e.target.closest(".bta-fr-pic")) {
        setZoom({ ...cur, picture: picOf(cur) });
        return;
      }
      if (!s0 || !s0.moved) return;
      const dx = e.clientX - s0.x;
      const w = e.currentTarget.offsetWidth || 300;
      if (dx > Math.min(120, w * 0.3)) answer("yes");
      else if (dx < -Math.min(120, w * 0.3)) answer("no");
      else setDrag(null);
    };

    const pct = (n) => Math.min(tab === "faces" ? 99 : 100, n) + "%";
    const done = counts.yes + counts.no + counts.skip;
    const left = queue.length + total; // on screen + not fetched yet
    const tint = drag ? Math.max(-1, Math.min(1, drag.dx / 140)) : 0;

    let bodyEl;
    if (state.loading && !cur) {
      bodyEl = h("div", { className: "bta-fr-empty" }, M.loading || (tab === "faces" ? "Looking for strong matches…" : "Looking for confident tags…"));
    } else if (state.error && !cur) {
      bodyEl = h("div", { className: "bta-fr-empty" }, "Couldn't load matches: " + state.error,
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => load(true) }, "Try again"));
    } else if (!cur) {
      bodyEl = h("div", { className: "bta-fr-empty" },
        h("div", { className: "bta-fr-done" }, done ? "All done 🎉" : "Nothing to review"),
        h("div", null, done
          ? `${counts.yes} added · ${counts.no} not them · ${counts.skip} skipped`
          : M.empty),
        undo.length > 0 && h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: takeBack }, "↶ Undo last"),
        endButtons());
    } else if (tab === "new") {
      const typed = names[cur.sig] != null ? names[cur.sig] : cur.name || "";
      const assign = cur.type === "assign";
      const out = leftOut[cur.sig] || new Set();
      const total = (cur.all || []).length || cur.count;
      const using = total - out.size;
      bodyEl = h(React.Fragment, null,
        h("div", {
          className: "bta-fr-card" + (drag ? " bta-fr-dragging" : ""),
          style: drag ? { transform: `translateX(${drag.dx}px) rotate(${drag.dx / 25}deg)` } : null,
          onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: () => { start.current = null; setDrag(null); },
        },
          h("div", { className: "bta-fr-pic bta-np-pics n" + Math.min(cur.samples.length, 6) },
            cur.samples.map((smp) => h("button", {
              key: smp.kind + smp.id, type: "button", className: "bta-np-pic", title: "See all of them",
              onClick: () => setAllOf(cur),
            }, h("img", { src: smp.picture, alt: "", draggable: false, loading: "lazy" }),
              smp.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶"))),
            h("button", { type: "button", className: "bta-np-seeall", onClick: () => setAllOf(cur) },
              out.size ? `Using ${using} of ${total} · change` : `See all ${total}`),
            h("span", { className: "bta-fr-stamp bta-fr-yes", style: { opacity: Math.max(0, tint) } }, assign ? "YES" : "CREATE"),
            h("span", { className: "bta-fr-stamp bta-fr-no", style: { opacity: Math.max(0, -tint) } }, "NO")),
          h("div", { className: "bta-fr-who" },
            assign
              ? h("button", { type: "button", className: "bta-fr-avatar-btn", title: `See ${cur.performer.name}'s page`,
                  onClick: () => { reviewReturn = `/performers/${cur.performer.id}`; goTo(`/performers/${cur.performer.id}`); } },
                  h("img", { className: "bta-fr-avatar", src: `/performer/${cur.performer.id}/image`, alt: "", draggable: false }),
                  h("span", { className: "bta-fr-avatar-hint" }, "View"))
              : h("span", { className: "bta-np-new", "aria-hidden": true }, "+"),
            h("div", { className: "bta-fr-text" },
              assign
                ? h("div", { className: "bta-fr-q" }, "Is this ", h("b", null, cur.performer.name), "?")
                : h("div", { className: "bta-fr-q" }, "New performer?"),
              !assign && h("input", {
                className: "bta-np-name", type: "text", value: typed, placeholder: "Their name",
                onChange: (e) => setNames((n) => ({ ...n, [cur.sig]: e.target.value })),
                onKeyDown: (e) => { if (e.key === "Enter") answer("yes"); },
              }),
              !assign && (ideasOf(cur).length > 0 || (aiNames[cur.sig] || {}).state === "reading") && h("div", { className: "bta-np-ideas" },
                ideasOf(cur).map((nm) => h("button", {
                  key: nm.name, type: "button", title: nm.why,
                  className: "bta-np-idea" + (typed === nm.name ? " active" : ""),
                  onClick: () => setNames((n) => ({ ...n, [cur.sig]: nm.name })),
                }, (/AI read/.test(nm.why) ? "🔍 " : "") + nm.name)),
                (aiNames[cur.sig] || {}).state === "reading" && h("span", { className: "bta-np-reading" }, "🔍 Reading the pictures' text…")),
              !assign && h("button", { type: "button", className: "bta-np-existing", onClick: () => setPickExisting(cur) },
                "…or add to a performer you already have"),
              h("div", { className: "bta-fr-meta" }, cur.reasons.join(" · ")),
              h("div", { className: "bta-fr-meta" }, assign
                ? `Yes adds them to ${using === total ? (total === 1 ? "the one" : `all ${total}`) : `${using} of the ${total}`} without a performer and learns this face.`
                : `Adds them to ${using === total ? `all ${total}` : `${using} of the ${total}`}; the first picture becomes their profile picture.`)))),
        next && next.samples && next.samples[0] && h("link", { rel: "preload", as: "image", href: next.samples[0].picture }),
        h("div", { className: "bta-fr-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy, onClick: () => answer("no") },
            assign ? "✕ Not them" : "✕ No"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => answer("skip") }, "Skip"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy, onClick: () => answer("yes") },
            assign ? "✓ Yes, add" : "✓ Create")));
    } else if (tab === "tagtidy") {
      bodyEl = h(TagTidyCard, {
        it: cur, next, drag, tint, busy,
        out: leftOut[cur.sig] || new Set(),
        keep: (studioPick[cur.sig] || {}).keep || cur.keep,
        swapped: !!(studioPick[cur.sig] || {}).swap,
        onSwap: () => setStudioPick((sp) => ({ ...sp, [cur.sig]: { ...(sp[cur.sig] || {}), swap: !(sp[cur.sig] || {}).swap } })),
        setKeep: (id) => { setStudioPick((sp) => ({ ...sp, [cur.sig]: { keep: id } })); setNames((n) => { const c0 = { ...n }; delete c0[cur.sig]; return c0; }); },
        name: names[cur.sig],
        setName: (v) => setNames((n) => ({ ...n, [cur.sig]: v })),
        onAll: () => setAllOf(cur),
        onExisting: () => setPickTag(cur),
        onPage: (path) => { reviewReturn = path; goTo(path); },
        answer,
        handlers: { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: () => { start.current = null; setDrag(null); } },
      });
    } else if (tab === "galleries") {
      bodyEl = h(GalleryCard, {
        it: cur, next, drag, tint, busy,
        out: leftOut[cur.sig] || new Set(),
        toggle: (key) => setLeftOut((lo) => { const s0 = new Set(lo[cur.sig] || []); if (s0.has(key)) s0.delete(key); else s0.add(key); return { ...lo, [cur.sig]: s0 }; }),
        name: names[cur.sig],
        setName: (v) => setNames((n) => ({ ...n, [cur.sig]: v })),
        onAll: () => setAllOf(cur),
        onPage: (path) => { reviewReturn = path; goTo(path); },
        answer,
        handlers: { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: () => { start.current = null; setDrag(null); } },
      });
    } else if (tab === "studios") {
      bodyEl = h(StudioCard, {
        it: cur, next, drag, tint, busy,
        out: leftOut[cur.sig] || new Set(),
        pick: studioPick[cur.sig] || {},
        name: names[cur.sig],
        setName: (v) => setNames((n) => { const c0 = { ...n }; if (v == null) delete c0[cur.sig]; else c0[cur.sig] = v; return c0; }),
        setPick: (v) => setStudioPick((sp) => ({ ...sp, [cur.sig]: v })),
        onAll: () => setAllOf(cur),
        onExisting: () => setPickStudio(cur),
        onZoom: (x, list) => setZoom({ ...x, name: "", _list: list }),
        onPage: (path) => { reviewReturn = path; goTo(path); },
        answer,
        handlers: { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: () => { start.current = null; setDrag(null); } },
      });
    } else if (inBatch) {
      const mine = groupsFor.current === tabKey + "|" + only ? groups || [] : [];
      const g = mine.find((x) => String(x.id) === String(group));
      const items = (batch && batch.items) || [];
      const using = items.length - [...batchOff].filter((k) => items.some((it) => keyOf(it) === k)).length;
      const chk = tab === "check";
      const gTag = chk ? String(group).startsWith("t:") : tab === "tags";
      const gId = chk ? String(group).slice(2) : group;
      const avatar = g && (chk ? checkAvatar(String(group)[0], gId) : gTag ? `/tag/${gId}/image` : `/performer/${gId}/image`);
      bodyEl = !g
        ? h("div", { className: "bta-fr-empty" }, batch && batch.state === "loading" ? "Loading…"
            : h(React.Fragment, null,
                h("div", { className: "bta-fr-done" }, counts.yes + counts.no ? "All done 🎉" : "Nothing to review"),
                h("div", null, counts.yes + counts.no ? `${counts.yes} added · ${counts.no} no` : M.empty),
                endButtons()))
        : h("div", { className: "bta-batch", ref: batchBox },
            h("div", { className: "bta-batch-head" },
              h("img", { className: gTag ? "bta-fr-avatar bta-fr-avatar-tag" : "bta-fr-avatar", src: avatar, alt: "" }),
              h("div", { className: "bta-batch-title" },
                h("div", { className: "bta-fr-q" }, chk ? ["Remove ", h("b", { key: "n" }, g.name), " from these?"]
                  : tab === "faces" ? ["Is this ", h("b", { key: "n" }, g.name), "?"] : ["Tag them ", h("b", { key: "n" }, g.name), "?"]),
                h("select", { className: "bta-batch-pick", value: String(group), onChange: (e) => setGroup(e.target.value), "aria-label": "Which group" },
                  (groups || []).map((x) => h("option", { key: x.id, value: String(x.id) }, `${x.name} (${x.count})`))))),
            items.length > 1 && h("div", { className: "bta-batch-all" },
              h("button", { type: "button", className: "bta-fr-chip", onClick: () => setBatchOff(chk ? new Set() : new Set()) },
                chk ? "Mark all to remove" : "Select all"),
              h("button", { type: "button", className: "bta-fr-chip", onClick: () => setBatchOff(new Set(items.map(keyOf))) },
                chk ? "Keep all" : "Deselect all")),
            h("div", { className: "bta-all-hint bta-batch-hint" }, batch && batch.state === "loading" ? "Loading them…"
              : chk ? `These look out of place, but they're all kept until you tap the ones that ${gTag ? "really don't fit" : "aren't them"} (they turn red). ⤢ to see one big.`
              : `Tap the ones that ${tab === "faces" ? "aren't them" : "don't fit"} - they'll be marked no. ⤢ to see one big.`),
            h("div", { className: "bta-all-grid bta-batch-grid" + (chk ? " chk" : "") },
              items.map((it) => {
                const k = keyOf(it);
                const off = batchOff.has(k);
                return h("div", { key: k, className: "bta-all-tile" + (off ? " off" : "") },
                  h("button", { type: "button", className: "bta-all-pick", "aria-pressed": !off,
                    title: chk ? (off ? "Kept - tap to remove it" : "Removed - tap to keep it") : off ? "Put it back" : "Leave it out",
                    onClick: () => setBatchOff((s0) => { const n0 = new Set(s0); if (n0.has(k)) n0.delete(k); else n0.add(k); return n0; }) },
                    h("img", { src: it.picture, alt: "", loading: "lazy", draggable: false }),
                    h("span", { className: "bta-all-mark" }, chk ? (off ? "✓" : "✕") : off ? "✕" : "✓"),
                    (it.kind === "scene" || it.kind === "marker") && h("span", { className: "bta-fr-tile-kind" }, it.kind === "marker" ? "▶ Marker " + clock(it.start) : "▶ Scene"),
                    h("span", { className: "bta-batch-score" }, chk ? (it.what === "p" ? "no face" : `${it.score}%`) : tab === "faces" ? `${Math.min(99, it.score)}%` : `${it.score}%`)),
                  h("button", { type: "button", className: "bta-all-view", title: "See it big", "aria-label": "See it big",
                    onClick: () => setZoom({ ...it, _list: items, _ctx: "batch" }) }, "⤢"));
              })),
            chk
              ? h("div", { className: "bta-batch-foot" },
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy || !items.length || using > 0,
                    title: "Keep them all and don't ask about them again", onClick: () => batchAnswer("none") }, "✓ All fit"),
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => batchAnswer("skip") }, "Skip"),
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy || !using, onClick: () => batchAnswer("yes") },
                    using ? `✕ Remove from ${using}` + (items.length - using ? ` · ${items.length - using} fit` : "") : "Tap the ones to remove"))
              : h("div", { className: "bta-batch-foot" },
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy || !items.length, onClick: () => batchAnswer("none") }, "✕ None"),
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => batchAnswer("skip") }, "Skip"),
                  h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy || !using, onClick: () => batchAnswer("yes") },
                    `✓ Add to ${using}` + (items.length - using ? ` · ${items.length - using} no` : ""))));
    } else if (view === "list") {
      const more = total;
      bodyEl = h(React.Fragment, null,
        h("div", { className: "bta-fr-grid" },
          shown.map((it) => {
            const k = keyOf(it);
            const saving = working.has(k);
            return h("div", { key: k, className: "bta-fr-tile" + (saving ? " saving" : "") },
              h("button", { type: "button", className: "bta-fr-tile-pic", title: "See it big", onClick: () => setZoom({ ...it, picture: picOf(it) }) },
                h("img", { src: picOf(it), alt: "", loading: "lazy", draggable: false }),
                (it.kind === "scene" || it.kind === "marker") && h("span", { className: "bta-fr-tile-kind" }, it.kind === "marker" ? "▶ Marker " + clock(it.start) : "▶ Scene")),
              h("button", { type: "button", className: "bta-fr-tile-who", title: `See ${it.name}'s page`,
                onClick: () => { reviewReturn = M.page(it); goTo(M.page(it)); } },
                h("img", { className: tab === "tags" || (tab === "check" && it.what !== "p") ? "bta-fr-avatar-tag" : "", src: M.avatar(it), alt: "", loading: "lazy" }),
                h("span", { className: "bta-fr-tile-name" }, it.name)),
              h("div", { className: "bta-fr-tile-meta" },
                M.tileMeta ? M.tileMeta(it, pct)
                : tab === "faces"
                  ? `${pct(it.score)} alike` + (it.times && it.times.length ? ` · ${it.times.map(clock).join(", ")}` : "")
                  : tab === "check" ? (it.what === "p" ? "Face not found" : `${it.score}% of look-alikes`)
                  : `${pct(it.score)} · ${it.near}/20 look-alikes`),
              h("div", { className: "bta-fr-tile-actions" },
                h("button", { type: "button", className: "bta-fr-tile-btn no", disabled: saving, onClick: () => answer("no", it) },
                  M.listNo || (tab === "faces" ? "✕ Not them" : "✕ No")),
                h("button", { type: "button", className: "bta-fr-tile-btn yes", disabled: saving, onClick: () => answer("yes", it) },
                  M.listYes || "✓ Add")));
          })),
        more > 0 && h("button", { type: "button", className: "bta-fr-more", disabled: state.loading,
          onClick: () => load(false) }, state.loading ? "Loading…" : `Show more (${more} left)`),
        more === 0 && h("div", { className: "bta-fr-empty bta-fr-endlist" }, "That's all of them for now.", endButtons()));
    } else {
      bodyEl = h(React.Fragment, null,
        h("div", {
          className: "bta-fr-card" + (drag ? " bta-fr-dragging" : ""),
          style: drag ? { transform: `translateX(${drag.dx}px) rotate(${drag.dx / 25}deg)` } : null,
          onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: () => { start.current = null; setDrag(null); },
        },
          h("div", { className: "bta-fr-pic" },
            h("img", { src: picOf(cur), alt: "", draggable: false, key: keyOf(cur) }),
            h("span", { className: "bta-fr-stamp bta-fr-yes", style: { opacity: Math.max(0, tint) } }, M.yesStamp || "YES"),
            h("span", { className: "bta-fr-stamp bta-fr-no", style: { opacity: Math.max(0, -tint) } }, M.noStamp)),
          h("div", { className: "bta-fr-who" },
            h("button", {
              type: "button", className: "bta-fr-avatar-btn", title: `See ${cur.name}'s page`,
              onPointerDown: (e) => e.stopPropagation(),
              onClick: () => { reviewReturn = M.page(cur); goTo(M.page(cur)); },
            },
              h("img", { className: "bta-fr-avatar" + (tab === "tags" || (tab === "check" && cur.what !== "p") ? " bta-fr-avatar-tag" : ""), src: M.avatar(cur), alt: "", draggable: false }),
              h("span", { className: "bta-fr-avatar-hint" }, "View")),
            h("div", { className: "bta-fr-text" },
              h("div", { className: "bta-fr-q" }, ...M.question(cur)),
              h("div", { className: "bta-fr-meta" }, M.meta(cur, pct)),
              h("div", { className: "bta-fr-meta" },
                h("a", { href: cur.kind === "marker" ? `/scenes/${cur.scene}?t=${Math.round(cur.start || 0)}` : `/${cur.kind}s/${cur.id}`,
                  target: "_blank", rel: "noopener", onPointerDown: (e) => e.stopPropagation() },
                  (cur.kind === "marker" ? "Marker" : cur.kind === "scene" ? "Scene" : "Photo") + ": " + (cur.title || cur.id) + " ↗"),
                M.extra(cur))))),
        next && h("link", { rel: "preload", as: "image", href: next.picture }),
        h("div", { className: "bta-fr-actions" },
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-no", disabled: busy, onClick: () => answer("no") }, M.noLabel),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-skip", disabled: busy, onClick: () => answer("skip") }, "Skip"),
          h("button", { type: "button", className: "bta-fr-btn bta-fr-btn-yes", disabled: busy, onClick: () => answer("yes") }, M.yesLabel)),
        h("div", { className: "bta-fr-hint" }, "Swipe right for yes, left for no.",
          h("span", { className: "bta-fr-keys" }, " Keys: → yes, ← no, ↓ skip, Z undo.")));
    }

    return h("div", { className: "bta-fr" },
      h("div", { className: "bta-fr-head" },
        h("h2", null, "Review"),
        // Faces / Tags / New, and under it Swipe / List
        h("div", { className: "bta-fr-switches" },
          h("div", { className: "bta-fr-tabs", role: "tablist", "aria-label": "What to review" },
            rGroups.map((g) =>
              h("button", { key: g.key, type: "button", role: "tab", "aria-selected": grp.key === g.key,
                className: "bta-fr-tab" + (grp.key === g.key ? " active" : ""),
                onClick: () => grp.key !== g.key && goTo(`${REVIEW_PAGE}?mode=${lastAction(g)}`) }, g.label))),
          h("div", { className: "bta-fr-actrow" },
          h("div", { className: "bta-fr-tabs bta-fr-acts", role: "tablist", "aria-label": "What to do" },
            grp.actions.map((k) =>
              h("button", { key: k, type: "button", role: "tab", "aria-selected": tabKey === k,
                className: "bta-fr-tab" + (tabKey === k ? " active" : ""),
                onClick: () => tabKey !== k && goTo(`${REVIEW_PAGE}?mode=${k}`) }, ACTIONS[k].label))),
          !M.card && h("div", { className: "bta-fr-tabs bta-fr-views", role: "tablist", "aria-label": "View" },
            [["swipe", "Swipe"], ["list", "List"], ["batch", "Batch"]].map(([v, label]) => {
              const on = view === v;
              return h("button", { key: v, type: "button", role: "tab", "aria-selected": on,
                className: "bta-fr-tab" + (on ? " active" : ""), onClick: () => setView(v) }, label);
            })))),
        h("div", { className: "bta-fr-sub" },
          only && cur ? `${cur.name}: ` : only ? M.filterLabel : "",
          inBatch ? (groups && groupsFor.current === tabKey + "|" + only ? `${groups.length} group${groups.length === 1 ? "" : "s"}` : "") : cur || done ? `${left} left` : "",
          done ? ` · ${counts.yes} added, ${counts.no} no` : ""),
        h("div", { className: "bta-fr-tools" },
          undo.length > 0 && h("button", { type: "button", className: "bta-fr-chip", disabled: busy, onClick: takeBack }, "↶ Undo"),
          tab === "faces" && h("button", {
            type: "button", className: "bta-fr-chip" + (favOnly ? " on" : ""), "aria-pressed": favOnly,
            title: favOnly ? "Showing suggestions for your favourite performers only" : "Show suggestions for your favourite performers only",
            onClick: () => {
              const q = new URLSearchParams(location.search);
              if (favOnly) q.delete("fav"); else q.set("fav", "1");
              goTo(`${location.pathname}?${q.toString()}`);
            },
          }, favOnly ? "★ Favourites only" : "☆ Favourites only"),
          only && h("button", { type: "button", className: "bta-fr-chip", onClick: () => goTo(`${REVIEW_PAGE}?mode=${tabKey}` + (favOnly ? "&fav=1" : "")) },
            tab === "faces" ? "All performers" : "All tags"))),
      state.note && h("div", { className: "bta-fr-note" }, state.note),
      bodyEl,
      allOf && h(AllItems, {
        it: allOf, out: leftOut[allOf.sig] || new Set(),
        onToggle: (key) => setLeftOut((lo) => {
          const s0 = new Set(lo[allOf.sig] || []);
          if (s0.has(key)) s0.delete(key); else s0.add(key);
          return { ...lo, [allOf.sig]: s0 };
        }),
        onAll: (keep) => setLeftOut((lo) => ({ ...lo, [allOf.sig]: keep ? new Set() : new Set((allOf.all || []).map((x) => x.key)) })),
        onView: (x) => setZoom({ ...x, name: allOf.type === "assign" ? allOf.performer.name : "", _list: allOf.all, _ctx: "all", _sig: allOf.sig }),
        onClose: () => setAllOf(null),
        ...(allOf.st || allOf.tg || (allOf.gal && allOf.type !== "gdetails") ? {
          doLabel: (n) => (allOf.type === "add" || allOf.type === "gnew" || allOf.type === "tagnew" ? `✓ Create · ${n}` : `✓ Add ${n}`),
          onDo: () => { const one = allOf; setAllOf(null); answer("yes", one); },
        } : {}),
      }),
      zoom && h(ZoomView, {
        it: asScene(zoom), onClose: () => setZoom(null), list: zoom._list && zoom._list.map(asScene),
        onNav: (x) => setZoom((z) => ({ ...x, name: z.name, _list: z._list, _ctx: z._ctx, _sig: z._sig })),
        pick: zoom._ctx === "batch"
          ? { off: batchOff.has(keyOf(zoom)), check: tab === "check",
              toggle: () => setBatchOff((s0) => { const k = keyOf(zoom); const n0 = new Set(s0); if (n0.has(k)) n0.delete(k); else n0.add(k); return n0; }) }
          : zoom._ctx === "all"
          ? { off: (leftOut[zoom._sig] || new Set()).has(zoom.key),
              toggle: () => setLeftOut((lo) => { const n0 = new Set(lo[zoom._sig] || []); if (n0.has(zoom.key)) n0.delete(zoom.key); else n0.add(zoom.key); return { ...lo, [zoom._sig]: n0 }; }) }
          : null,
      }),
      pickTag && h(PickTag, {
        it: pickTag,
        using: (() => { const o = leftOut[pickTag.sig]; return pickTag.keys.filter((k) => !(o && o.has(k))).length; })(),
        onCancel: () => setPickTag(null),
        onPick: (t) => { const one = pickTag; setPickTag(null); answer("yes", one, { tag: t }); },
      }),
      pickStudio && h(PickStudio, {
        it: pickStudio,
        using: (() => { const o = leftOut[pickStudio.sig]; return pickStudio.keys.filter((k) => !(o && o.has(k))).length; })(),
        onCancel: () => setPickStudio(null),
        onPick: (st) => { const one = pickStudio; setPickStudio(null); answer("yes", one, { studio: st }); },
      }),
      pickExisting && h(PickExisting, {
        it: (() => {  // only the ticked items
          const out0 = leftOut[pickExisting.sig];
          return out0 && out0.size ? { ...pickExisting, keys: pickExisting.keys.filter((k) => !out0.has(k)) } : pickExisting;
        })(),
        ideas: ideasOf(pickExisting),
        onCancel: () => setPickExisting(null),
        onPick: (p) => { setPickExisting(null); answer("yes", null, { existing: p }); },
      }),
      confirmNew && h(ConfirmNew, {
        it: confirmNew.it, name: confirmNew.name,
        onCancel: () => setConfirmNew(null),
        picture: (() => {
          const kept = new Set(confirmNew.it.keys);
          const p0 = confirmNew.it.samples.find((x) => kept.has(`${x.kind}:${x.id}`)) || (confirmNew.it.all || []).find((x) => kept.has(x.key));
          return p0 ? (p0.kind === "image" ? `/image/${p0.id}/image` : p0.picture) : null;
        })(),
        onCreate: (name, details) => {
          setNames((n) => ({ ...n, [confirmNew.it.sig]: name }));
          setConfirmNew(null);
          answer("yes", null, { name, details });
        },
      }));
  }

  // ---- Setup & troubleshooting page (/ai-setup) ----------------------------
  const SETUP_PAGE = "/ai-setup";
  const COMMON = [
    ["\"Can't reach Ollama\"", "Ollama isn't running, or Stash can't see it. On a Mac: brew services start ollama (or open the Ollama app); on Windows: start Ollama from the Start menu (it sits by the clock). Stash in Docker Desktop reaches it at http://host.docker.internal:11434; Stash installed directly at http://localhost:11434; Stash in Docker on Linux needs network_mode: host and http://127.0.0.1:11434. Set it under Setup: Ollama address."],
    ["Ollama or the face helper is on another computer (no SSH tunnel needed)", "By default each only answers the computer it runs on. On that computer, make Ollama listen on the network: Windows: setx OLLAMA_HOST 0.0.0.0, then quit Ollama by the clock and open it again; Mac app: launchctl setenv OLLAMA_HOST 0.0.0.0, then quit and reopen Ollama; Linux: sudo systemctl edit ollama, add [Service] and Environment=\"OLLAMA_HOST=0.0.0.0\", then sudo systemctl restart ollama. For the face helper run setup again with --listen-all (Windows: setup.ps1 -ListenAll). Let ports 11434 and 11535 through its firewall, then set Setup: Ollama address to http://<its IP>:11434 and the face helper address to http://<its IP>:11535. Neither has a password: only on a home network you trust (or a VPN like Tailscale)."],
    ["\"Ollama doesn't have the model\"", "On the computer running Ollama: ollama pull gemma3:4b (about 3.3 GB). If you set Setup: AI model (in Ollama) to something else, pull that one, and make sure it can see pictures."],
    ["\"Can't reach the face helper\" / faces and look-alikes do nothing", "Install it: from the add-on's folder run sh face-helper/setup.sh (on Windows, in PowerShell: powershell -ExecutionPolicy Bypass -File face-helper\\setup.ps1, with -ListenAll when Stash runs in Docker Desktop). It starts at login; check it with curl http://127.0.0.1:11535/health. Keep it out of Documents, Desktop and Downloads on a Mac (programs started at login can't read those)."],
    ["setup.sh / setup.ps1 fails installing onnxruntime", "Your newest Python is too new for ONNX Runtime. Install an older one (brew install python@3.13) and run PYTHON=python3.13 sh face-helper/setup.sh. On Windows: winget install Python.Python.3.12, then .\\setup.ps1 -Python \"py -3.12\"."],
    ["\"The face helper is too old\"", "A newer add-on needs a newer helper: run sh face-helper/setup.sh again (on Windows, face-helper\\setup.ps1). It updates in place and keeps nothing you'd lose."],
    ["Tasks stop working after about an hour, or say \"Done\" having skipped everything", "The task's login runs out. Generate an API key under Settings → Security; the add-on uses it from then on."],
    ["Full-size pictures or videos won't open, but thumbnails do", "Stash lost its link to your library drive (often a network drive after it disconnected or the computer slept). Reconnect the drive, then restart Stash (in Docker: docker restart stash; on Windows: quit Stash from its tray icon and start it again)."],
    ["Look-alike tags, Similar galleries or Review → Tags show nothing", "Run \"Tags: scan scenes and images for look-alikes (do this first)\" under Settings → Tasks. New items get their fingerprint as they're scanned; cancel and rerun to carry on."],
    ["No face matches", "Run \"Faces: learn performers' faces\" (it uses their profile pictures - give performers a clear, front-facing one), then \"Faces: scan scenes and images\"."],
    ["The AI is slow, or other tasks wait for hours", "Stash runs one task at a time and the AI takes seconds per item. Turn on New items: wait until night for the AI model steps so the slow steps run once at night; the first request after a while is slower while the model loads."],
    ["AI tags are wrong", "A small model guesses; the add-on only keeps its picks that your look-alikes agree with. Tag more items yourself (the add-on learns from them), use Review to accept or turn down suggestions, and use the previews before the tasks."],
    ["New items aren't done at night", "The night timer runs inside Stash and stops when Stash restarts; the next scan starts it again. Or run \"Schedules: run tonight's waiting steps for new items now\"."],
  ];
  const STATUS = { ok: ["✓", "OK"], warn: ["!", "Check"], fail: ["✕", "Problem"], info: ["i", "Info"] };
  // buttons under a common problem: what narrows it down (by the problem's
  // first words). "group:" re-checks one section, "ids:" a few checks,
  // "task:" runs a task (only previews and harmless ones), "log" opens the log.
  const FAQ_ACTIONS = [
    [/^"Can't reach Ollama/, [["Find Ollama", "findollama"], ["Test the connection", "group:AI model (Ollama)"], ["Test the AI on a picture", "ai"]]],
    [/^Ollama or the face helper is on another/, [["Test the connection", "group:AI model (Ollama)"], ["Check the face helper", "group:Face helper"]]],
    [/^"Ollama doesn't have/, [["List the installed models", "models"], ["Test the connection", "group:AI model (Ollama)"]]],
    [/^"Can't reach the face helper/, [["Check the face helper", "group:Face helper"]]],
    [/^"The face helper is too old/, [["Check what the helper can do", "group:Face helper"]]],
    [/^Tasks stop working/, [["Check picture access", "ids:pictures-can-be-fetched"], ["Open Stash's log", "log"]]],
    [/^Full-size pictures/, [["Check the library files", "files"], ["Check picture access", "ids:pictures-can-be-fetched,library-files-reachable"]]],
    [/^Look-alike tags, Similar/, [["Check the look-alike scan", "ids:look-alike-scan,picture-fingerprints-clip"],
      ["Preview look-alike tags on 10 items", "task:Tags: preview on 10 (look-alikes, no changes)"], ["Run the look-alike scan", "task:Tags: scan scenes and images for look-alikes (do this first)"]]],
    [/^No face matches/, [["Check the face scans", "group:Scans"], ["Preview faces on 10 items", "task:Faces: preview on 10 (no changes)"],
      ["Learn performers' faces", "task:Faces: learn performers' faces"]]],
    [/^The AI is slow/, [["Time the AI on a picture", "ai"], ["Show what's running", "jobs"]]],
    [/^AI tags are wrong/, [["Preview AI tags on 10 items", "task:Tags: preview on 10 (AI model, no changes)"], ["Open Stash's log", "log"]]],
    [/^New items aren't done/, [["Check tonight's run", "ids:tonight-s-run,after-a-scan,time-zone"], ["Run tonight's items now", "task:Schedules: run tonight's waiting steps for new items now"]]],
  ];

  // "Find Ollama": where Stash runs, and every usual address tried from there
  function FindOllama({ r, row, redo }) {
    const [saved, setSaved] = React.useState("");
    async function use(url) {
      const d = await gql("{ configuration { plugins } }");
      const cur = (d.configuration.plugins || {})[PLUGIN_ID] || {};
      await gql("mutation ($id: ID!, $i: Map!) { configurePlugin(plugin_id: $id, input: $i) }", { id: PLUGIN_ID, i: { ...cur, aiOllamaUrl: url } });
      setSaved(url);
      setTimeout(redo, 300);
    }
    const ok = r.tried.filter((t) => t.ok);
    return h(React.Fragment, null,
      row("info", "Stash runs " + r.env.where, (r.env.docker ? `Inside the container: ${r.env.os}` : r.env.os) +
        (r.env.docker ? (r.env.hdi ? " · host.docker.internal works" : " · host.docker.internal doesn't exist here") : "")),
      r.tried.map((t) => h("div", { key: t.url, className: "bta-setup-check " + (t.ok ? "ok" : "fail") },
        h("span", { className: "bta-setup-icon" }, t.ok ? "✓" : "✕"),
        h("div", null,
          h("div", { className: "bta-setup-title" }, t.url, t.current ? "  (your setting)" : ""),
          h("div", { className: "bta-setup-detail" }, t.ok ? `Ollama ${t.version || ""} answers here · ${t.models.length} model(s): ${t.models.join(", ") || "none"}` : t.error),
          t.ok && !t.current && h("div", { className: "bta-setup-do" },
            h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: saved === t.url, onClick: () => use(t.url) },
              saved === t.url ? "✓ Saved" : "Use this address"))))),
      !ok.length && row("fail", "Ollama didn't answer at any of these", "", r.advice),
      ok.length > 0 && !ok.some((t) => t.current) && row("warn", "Your setting is the one that doesn't work", `Setup: Ollama address is ${r.setting}.`, "Press \"Use this address\" on one that answers."));
  }

  function FaqItem({ q, a }) {
    const acts = (FAQ_ACTIONS.find(([re]) => re.test(q)) || [null, []])[1];
    const [out, setOut] = React.useState(null); // {label, state, ...}
    async function go(label, what) {
      setOut({ label, state: "loading" });
      try {
        if (what === "log") { goTo("/settings?tab=logs"); return; }
        if (what === "jobs") { setOut({ label, state: "jobs" }); return; }
        if (what === "ai") { setOut({ label, state: "ai", ...(await ask({ mode: "diagnose_ai" })) }); return; }
        if (what === "models") { setOut({ label, state: "models", ...(await ask({ mode: "diagnose_models" })) }); return; }
        if (what === "findollama") { setOut({ label, state: "findollama", ...(await ask({ mode: "find_ollama" })) }); return; }
        if (what === "files") { setOut({ label, state: "files", ...(await ask({ mode: "diagnose_files" })) }); return; }
        if (what.startsWith("task:")) { await runTask(what.slice(5)); setOut({ label, state: "task", task: what.slice(5) }); return; }
        const r = await ask({ mode: "diagnose" });
        const ids = what.startsWith("ids:") ? what.slice(4).split(",") : null;
        const group = what.startsWith("group:") ? what.slice(6) : null;
        setOut({ label, state: "checks", checks: r.checks.filter((c) => (ids ? ids.includes(c.id) : c.group === group)) });
      } catch (e) {
        setOut({ label, state: "error", message: e.message });
      }
    }
    const row = (st, title, detail, fix) => h("div", { className: "bta-setup-check " + st },
      h("span", { className: "bta-setup-icon" }, STATUS[st][0]),
      h("div", null, h("div", { className: "bta-setup-title" }, title),
        detail && h("div", { className: "bta-setup-detail" }, detail), fix && h("div", { className: "bta-setup-fix" }, fix)));
    let res = null;
    if (out) {
      if (out.state === "loading") res = h("div", { className: "bta-setup-detail" }, "Working…");
      else if (out.state === "error") res = row("fail", "That didn't work", out.message);
      else if (out.state === "checks") res = out.checks.length ? out.checks.map((c) => h(React.Fragment, { key: c.id }, row(c.status, c.title, c.detail, c.fix)))
        : h("div", { className: "bta-setup-detail" }, "Nothing to show.");
      else if (out.state === "ai") res = row(out.seconds > 60 ? "warn" : "ok", `The AI answered in ${out.seconds} s`, `"${out.answer}"`,
        out.seconds > 60 ? "Slow: try again (the first request loads the model), or check nothing else is running." : "");
      else if (out.state === "models") res = h(React.Fragment, null,
        out.models.length ? out.models.map((m) => h(React.Fragment, { key: m.name },
          row(m.vision === false ? "warn" : m.chosen ? "ok" : "info", m.name + (m.chosen ? "  (the one in use)" : ""),
            `${m.size_gb} GB · ${m.vision === false ? "text only - can't see pictures" : m.vision ? "can see pictures" : "couldn't tell if it sees pictures"}`)))
          : row("fail", "No models installed", `Ollama at ${out.url} has none.`, `Run: ollama pull ${out.setting}`),
        !out.models.some((m) => m.chosen) && row("fail", `Setup: AI model (in Ollama) is "${out.setting}"`, "That one isn't installed.",
          `Run ollama pull ${out.setting}, or set Setup: AI model (in Ollama) to one of the models above that can see pictures.`));
      else if (out.state === "files") res = h(React.Fragment, null,
        out.roots.map((r) => h(React.Fragment, { key: r.root },
          row(r.ok === r.checked ? "ok" : r.ok ? "warn" : "fail", r.root, `${r.ok} of ${r.checked} files checked could be opened.`,
            r.ok < r.checked ? (r.ok ? "Some files under here are missing - moved or deleted since the last scan? A scan cleans them up." :
              "Nothing under here can be opened: the drive isn't connected (or Stash's view of it went stale). Reconnect it, then restart Stash.") : ""))),
        out.libraries.map((l) => h(React.Fragment, { key: l.path },
          row(l.ok ? "ok" : "fail", "Library folder " + l.path, l.ok ? "Stash can see it." : "Stash can't see anything in it.",
            l.ok ? "" : "Check the drive is connected (and, in Docker, mounted into the container), then restart Stash."))));
      else if (out.state === "findollama") res = h(FindOllama, { r: out, row, redo: () => go(out.label, "findollama") });
      else if (out.state === "task") res = h(React.Fragment, null,
        h("div", { className: "bta-setup-detail" }, `Started "${out.task}". ` + (/preview/i.test(out.task) ? "Its results go to Stash's log (Settings → Logs)." : "")),
        h(Jobs, { tick: 1 }));
      else if (out.state === "jobs") res = h(Jobs, { tick: 1 });
    }
    return h("details", { className: "bta-setup-faq" },
      h("summary", null, q),
      h("p", null, a),
      acts.length > 0 && h("div", { className: "bta-setup-faq-do" },
        h("span", { className: "bta-setup-detail" }, "Narrow it down:"),
        acts.map(([label, what]) => h("button", { key: label, type: "button",
          className: "btn btn-sm " + (what.startsWith("task:") && !/preview/i.test(what) ? "btn-primary" : "btn-secondary"),
          disabled: out && out.state === "loading", onClick: () => go(label, what) },
          (what.startsWith("task:") ? "▶ " : "") + label))),
      out && h("div", { className: "bta-setup-faq-out" }, h("div", { className: "bta-setup-faq-label" }, out.label), res));
  }
  // a check that a task fixes: its button (only shown when it isn't OK)
  const FIX_TASKS = {
    "look-alike-scan": "Tags: scan scenes and images for look-alikes (do this first)",
    "face-scan": "Faces: scan scenes and images",
    "performers-faces-learned": "Faces: learn performers' faces",
    "tonight-s-run": "Schedules: run tonight's waiting steps for new items now",
  };
  const ISSUES_URL = "https://github.com/OpenSourceLuffy/stash-plugins/issues/new";

  // run one of the add-on's tasks; the page shows Stash's job list
  const runTask = (name) => gql(`mutation ($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }`, { p: PLUGIN_ID, t: name });

  function Jobs({ tick }) {
    const [jobs, setJobs] = React.useState([]);
    React.useEffect(() => {
      let live = true;
      const load = () => gql("{ jobQueue { id description status progress } }").then((d) => live && setJobs(d.jobQueue || [])).catch(() => {});
      load();
      const t = setInterval(load, 2000);
      return () => { live = false; clearInterval(t); };
    }, [tick]);
    if (!jobs.length) return h("div", { className: "bta-setup-detail" }, "Nothing running right now.");
    return h("div", null, jobs.map((j) => h("div", { key: j.id, className: "bta-setup-job" },
      h("div", { className: "bta-setup-title" }, j.description),
      h("div", { className: "bta-setup-bar" }, h("span", { style: { width: `${Math.round((j.progress || 0) * 100)}%` } })),
      h("div", { className: "bta-setup-detail" }, `${j.status === "RUNNING" ? "Running" : j.status === "READY" ? "Waiting its turn" : j.status.toLowerCase()}` +
        (j.progress != null && j.progress >= 0 ? ` · ${Math.round(j.progress * 100)}%` : ""),
        h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => gql("mutation ($id: ID!) { stopJob(job_id: $id) }", { id: j.id }) }, "Stop")))));
  }

  // "Report a problem": a GitHub issue, filled in, for you to read and send
  function Report({ checks, res, test }) {
    const [what, setWhat] = React.useState("");
    const [withChecks, setWithChecks] = React.useState(true);
    const [plugins, setPlugins] = React.useState([]);
    React.useEffect(() => { gql("{ systemStatus { appSchema } version { version } plugins { id name version enabled } }").then((d) => setPlugins(d)).catch(() => {}); }, []);
    function open() {
      const vers = (plugins.plugins || []).filter((p) => /^(BetterTagging|ModernUI|StashSuggestions|PrivacyBlur|LibraryCleanup|vertical)/i.test(p.id))
        .map((p) => `${p.name} ${p.version || ""}${p.enabled ? "" : " (off)"}`).join(", ");
      const lines = ["**What happened**", what.trim() || "(describe what you did, what you expected and what happened)", "",
        "**Setup**", `- Stash ${(plugins.version || {}).version || "?"}${res && res.docker ? " (Docker)" : ""}`, `- Plugins: ${vers || "?"}`,
        `- AI model: ${(res && res.model) || "?"}`];
      if (withChecks && checks.length) {
        lines.push("", "**Setup check**");
        // no file paths or addresses: only what each check found
        checks.forEach((c) => lines.push(`- [${STATUS[c.status][1]}] ${c.group} - ${c.title}` +
          (c.id === "library-files-reachable" || /address|reachable/i.test(c.title) ? "" : `: ${c.detail.replace(/(\/[^\s,;:]+)+/g, "…")}`)));
        if (test && test.state === "done") lines.push(`- AI test: answered in ${test.seconds} s`);
        if (test && test.state === "error") lines.push(`- AI test failed: ${test.message}`);
      }
      const title = "[Better Organizing AI] " + (what.trim().split("\n")[0].slice(0, 80) || "Problem");
      const url = ISSUES_URL + "?" + new URLSearchParams({ title, body: lines.join("\n").slice(0, 6000), labels: "bug" }).toString();
      window.open(url, "_blank", "noopener");
    }
    return h("div", { className: "bta-setup-report" },
      h("textarea", { className: "form-control", rows: 4, value: what, placeholder: "What went wrong? What did you do, and what did you expect?",
        onChange: (e) => setWhat(e.target.value) }),
      h("label", { className: "bta-setup-detail bta-setup-cb" },
        h("input", { type: "checkbox", checked: withChecks, onChange: (e) => setWithChecks(e.target.checked) }),
        " Include the setup check (versions and what each check found - no file paths, addresses, names or pictures)"),
      h("button", { type: "button", className: "btn btn-primary", disabled: !what.trim(), onClick: open }, "Open a GitHub issue →"),
      h("div", { className: "bta-setup-detail" }, "Opens GitHub's new-issue page, filled in: read it over, then press Submit there (it needs a free GitHub account). Nothing is sent until you do."));
  }

  function SetupPage() {
    const [res, setRes] = React.useState({ state: "loading" });
    const [test, setTest] = React.useState({ state: "idle" });
    const [errs, setErrs] = React.useState(null);
    const [plugins, setPlugins] = React.useState(null);
    const [copied, setCopied] = React.useState(false);
    const [tasks, setTasks] = React.useState([]);
    const [pickTask, setPickTask] = React.useState("");
    const [started, setStarted] = React.useState({}); // task name -> "started" / error
    const [jobTick, setJobTick] = React.useState(0);
    React.useEffect(() => {
      gql("{ plugins { id tasks { name description } } }").then((d) =>
        setTasks(((d.plugins || []).find((p) => p.id === PLUGIN_ID) || {}).tasks || [])).catch(() => {});
    }, []);
    const start = (name) => runTask(name)
      .then(() => { setStarted((s0) => ({ ...s0, [name]: "Started" })); setJobTick((n) => n + 1); })
      .catch((e) => setStarted((s0) => ({ ...s0, [name]: "Couldn't start: " + e.message })));
    const run = React.useCallback(() => {
      setRes({ state: "loading" });
      ask({ mode: "diagnose" }).then((r) => setRes({ state: "done", ...r })).catch((e) => setRes({ state: "error", message: e.message }));
      gql("{ logs { time level message } }").then((d) => setErrs((d.logs || [])
        .filter((l) => /error|warn/i.test(l.level) && /Better (Organizing|Tagging)|BetterTaggingAI|face helper|Ollama|Plugin returned error/i.test(l.message)
          && !/\] \[(OK  |WARN|FAIL|INFO)\] /.test(l.message)) // (the setup check's own report)
        .slice(-12).reverse())).catch(() => setErrs([]));
      gql("{ plugins { id name version enabled } }").then((d) => setPlugins(d.plugins || [])).catch(() => setPlugins([]));
    }, []);
    React.useEffect(run, []);
    const checks = res.state === "done" ? res.checks : [];
    const bad = checks.filter((c) => c.status === "fail").length;
    const warn = checks.filter((c) => c.status === "warn").length;
    const groups = [...new Set(checks.map((c) => c.group))];
    function report() {
      const lines = [`Better Organizing AI setup check - model ${res.model}, Ollama ${res.ollama}, helper ${res.helper}${res.docker ? " (Stash in Docker)" : ""}`];
      checks.forEach((c) => lines.push(`[${STATUS[c.status][1]}] ${c.group} - ${c.title}: ${c.detail}`));
      if (test.state === "done") lines.push(`AI test: ${test.seconds}s - "${test.answer}"`);
      (errs || []).forEach((l) => lines.push(`log ${l.time}: ${l.message}`));
      navigator.clipboard?.writeText(lines.join("\n")).then(() => { setCopied(true); setTimeout(() => setCopied(false), 2000); });
    }
    const other = (id) => (plugins || []).find((p) => p.id === id);
    return h("div", { className: "bta-setup" },
      h("h2", null, "AI setup & troubleshooting"),
      h("p", { className: "bta-setup-sub" }, "Checks everything the Better Organizing AI Add-on needs. Nothing is changed. ",
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { refreshCaps(true); tourBus.open && tourBus.open(); } }, "Show the welcome tour"), " ",
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { refreshCaps(true); tourBus.setup && tourBus.setup(); } }, "Setup guide"), " ",
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => goTo(NIGHTLY_PAGE) }, "🌙 Nightly & after-scan jobs")),
      res.state === "loading" && h("div", { className: "bta-setup-banner" }, "Checking…"),
      res.state === "error" && h("div", { className: "bta-setup-banner fail" }, "The checks couldn't run: " + res.message),
      res.state === "done" && h("div", { className: "bta-setup-banner " + (bad ? "fail" : warn ? "warn" : "ok") },
        bad ? `${bad} problem${bad > 1 ? "s" : ""} to fix` + (warn ? `, ${warn} to check` : "") : warn ? `All working - ${warn} thing${warn > 1 ? "s" : ""} worth a look` : "Everything's working",
        h("small", null, `Model ${res.model} · Ollama ${res.ollama} · face helper ${res.helper}`)),
      h("div", { className: "bta-setup-tools" },
        h("button", { type: "button", className: "btn btn-secondary", onClick: run, disabled: res.state === "loading" }, "↻ Check again"),
        h("button", { type: "button", className: "btn btn-secondary", disabled: test.state === "loading",
          onClick: () => { setTest({ state: "loading" }); ask({ mode: "diagnose_ai" }).then((r) => setTest({ state: "done", ...r })).catch((e) => setTest({ state: "error", message: e.message })); } },
          test.state === "loading" ? "Asking the AI…" : "Test the AI on a picture"),
        res.state === "done" && h("button", { type: "button", className: "btn btn-secondary", onClick: report }, copied ? "✓ Copied" : "Copy report")),
      test.state !== "idle" && test.state !== "loading" && h("div", { className: "bta-setup-check " + (test.state === "done" ? (test.seconds > 60 ? "warn" : "ok") : "fail") },
        h("span", { className: "bta-setup-icon" }, test.state === "done" ? (test.seconds > 60 ? "!" : "✓") : "✕"),
        h("div", null,
          h("div", { className: "bta-setup-title" }, test.state === "done" ? `The AI answered in ${test.seconds} s` : "The AI test failed"),
          h("div", { className: "bta-setup-detail" }, test.state === "done" ? `About a random photo: "${test.answer}"` : test.message),
          test.state === "done" && test.seconds > 60 && h("div", { className: "bta-setup-fix" }, "That's slow: the first request loads the model (try again), or another AI task is running. A smaller model or turning on AI: wait until night helps."))),
      groups.map((g) => h("section", { key: g, className: "bta-setup-group" },
        h("h3", null, g),
        checks.filter((c) => c.group === g).map((c) => h("div", { key: c.id, className: "bta-setup-check " + c.status },
          h("span", { className: "bta-setup-icon", title: STATUS[c.status][1] }, STATUS[c.status][0]),
          h("div", null,
            h("div", { className: "bta-setup-title" }, c.title),
            c.detail && h("div", { className: "bta-setup-detail" }, c.detail),
            c.fix && h("div", { className: "bta-setup-fix" }, c.fix),
            c.status !== "ok" && FIX_TASKS[c.id] && (c.status !== "info" || c.id === "tonight-s-run") && h("div", { className: "bta-setup-do" },
              h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !!started[FIX_TASKS[c.id]],
                onClick: () => start(FIX_TASKS[c.id]) }, "▶ " + FIX_TASKS[c.id]),
              started[FIX_TASKS[c.id]] && h("span", { className: "bta-setup-detail" }, " " + started[FIX_TASKS[c.id]] + " - progress below")),
            c.id === "ollama-is-reachable" && c.status === "fail" && h("div", { className: "bta-setup-do" },
              h("span", { className: "bta-setup-detail" }, "Open \"Can't reach Ollama\" under Common problems and press Find Ollama: it tries every usual address from where Stash runs.")),
            c.id === "model-in-memory" && c.status !== "ok" && h("div", { className: "bta-setup-do" },
              h("button", { type: "button", className: "btn btn-sm btn-secondary", disabled: test.state === "loading",
                onClick: () => { setTest({ state: "loading" }); ask({ mode: "diagnose_ai" }).then((r) => { setTest({ state: "done", ...r }); run(); }).catch((e) => setTest({ state: "error", message: e.message })); } },
                "Load it now (tests it on a picture)"))))))),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "Run a task"),
        h("div", { className: "bta-setup-run" },
          h("select", { className: "form-control", value: pickTask, onChange: (e) => setPickTask(e.target.value), "aria-label": "Task" },
            h("option", { value: "" }, "Pick one of the add-on's tasks…"),
            tasks.map((t) => h("option", { key: t.name, value: t.name }, t.name))),
          h("button", { type: "button", className: "btn btn-primary", disabled: !pickTask, onClick: () => start(pickTask) }, "▶ Run")),
        pickTask && h("div", { className: "bta-setup-detail" }, ((tasks.find((t) => t.name === pickTask) || {}).description || "") +
          (started[pickTask] ? " - " + started[pickTask] : "")),
        h("div", { className: "bta-setup-detail" }, "Previews (\"no changes\") are safe to try. Stash runs one task at a time; the rest wait their turn."),
        h("h3", null, "Running now"),
        h(Jobs, { tick: jobTick })),
      plugins && h("section", { className: "bta-setup-group" },
        h("h3", null, "Other plugins"),
        [["BetterTaggingTools", "Better Organizing Tools", "Recommended: the add-on then uses its Tags to ignore, special spellings and gallery-name tags."],
         ["StashSuggestions", "Stash Suggestions", "Optional: its rows use the add-on's faces and look-alikes."],
         ["ModernUI", "Modern UI", "Optional: puts Review in the phone's More menu."]].map(([id, name, why]) => {
          const p0 = other(id);
          const st = p0 && p0.enabled ? "ok" : "info";
          return h("div", { key: id, className: "bta-setup-check " + st },
            h("span", { className: "bta-setup-icon" }, STATUS[st][0]),
            h("div", null, h("div", { className: "bta-setup-title" }, name + (p0 ? ` ${p0.version || ""}` : "")),
              h("div", { className: "bta-setup-detail" }, p0 ? (p0.enabled ? "Installed and on." : "Installed but turned off.") + " " + why : "Not installed. " + why)));
        })),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "Recent errors in Stash's log"),
        errs === null ? h("div", { className: "bta-setup-detail" }, "Loading…")
          : errs.length === 0 ? h("div", { className: "bta-setup-detail" }, "None from the add-on in Stash's recent log.")
          : errs.map((l, i) => h("div", { key: i, className: "bta-setup-log" }, h("small", null, new Date(l.time).toLocaleString()), " ", l.message))),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "Report a problem"),
        h(Report, { checks, res, test })),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "Common problems"),
        COMMON.map(([q, a], i) => h(FaqItem, { key: i, q, a }))),
      h("p", { className: "bta-setup-sub" }, "More in the add-on's README (setup is at the end). A copy of these checks can also be written to the log with the task \"Setup: check everything and write a report\"."));
  }

  // ---- welcome and tour --------------------------------------------------
  // The first time the add-on runs: a welcome with what it does, then an
  // optional tour - one feature per slide (swipe, or Back / Next), each with a
  // screenshot. "Don't show again" is saved in the add-on's settings
  // ("Setup: hide the welcome and tour"); "Remind me later" asks again
  // the next day on this device. The Setup page can open the tour any time.
  const TOUR_IMG = (f) => `/plugin/${PLUGIN_ID}/assets/tour/${f}`;
  const LATER_KEY = "bta-welcome-later";
  const TOUR = [
    { k: "review", need: "looks", img: "review-swipe.jpg", title: "Review: tag suggestions",
      text: "Tags your photos and scenes are likely missing, one card at a time: swipe right (or ✓ Yes) to add, left (✕ No) to skip it for good. They come from the photos and scenes you've already tagged that look most alike, from what the video shows, and from body tags that were nearly sure - better-looking photos first.",
      where: "Review → Tags → Suggested", go: "/review?mode=tags" },
    { k: "batch", need: "looks", img: "review-batch.jpg", title: "Review: batch view",
      text: "The same suggestions, all at once for one tag: untick the ones that don't fit and add the rest in one go. List view shows them as rows instead.",
      where: "Review → any tab → Batch", go: "/review?mode=tags" },
    { k: "faces", need: "faces", img: "review-faces.jpg", title: "Review: faces",
      text: "Scenes and photos where a performer's face is found but they aren't added yet, and new people who turn up in several items. Faces are matched on your computer by the face helper.",
      where: "Review → Faces", go: "/review?mode=faces" },
    { k: "check", need: "looks", img: "review-check.jpg", title: "Review: check",
      text: "Tags and performers that look out of place on an item. ✓ It fits keeps it and never asks again; ✕ Remove takes it off, and no task ever adds it back to that item.",
      where: "Review → Tags → Check", go: "/review?mode=tags_check" },
    { k: "studios", need: "none", img: "review-studios.jpg", title: "Review: studios and galleries",
      text: "Studios for scenes and photos from their folders, galleries and the accounts in their names, studios to merge, and folders of photos that could be a gallery.",
      where: "Review → Studios / Galleries", go: "/review?mode=studios_add" },
    { k: "edit", need: "tags", img: "edit-tags.jpg", title: "Tags while you edit",
      text: "Under the Tags box of a scene or photo: Ask AI for tags (the local AI model) and Tags from look-alikes. Tap one to add it; ✓ marks the sure ones.",
      where: "A scene or photo → Edit → Tags", go: null },
    { k: "search", need: "search", img: "search.jpg", title: "Search by description",
      text: "Describe what you're looking for - \"red dress at the beach\", \"kneeling on a bed\", \"black and white photo\" - and it finds the photos, scenes and markers that look like it, even when no tag or title says so.",
      where: "Search (in the menu)", go: "/ai-search" },
    { k: "markers", need: "markers", img: "ai-markers.jpg", title: "AI markers",
      text: "Markers where a scene shifts, with a picture of each: tap one to watch from there, then + Add (or Add all). A task does it for every scene without markers.",
      where: "A scene → Markers → ✨ AI markers", go: null },
    { k: "quick", need: "none", img: "quick-marker.jpg", title: "Quick markers",
      text: "Create Marker starts at the video's current time, offers end times (the next marker, the next change the AI saw, +1 min) and fills in the name. The marker it falls inside can end where the new one starts - with Undo.",
      where: "A scene → Markers → Create Marker", go: null },
    { k: "chapters", need: "none", img: "chapters.jpg", title: "Gallery chapters",
      text: "Suggest chapters finds where a gallery's parts start - folders, file names, a change of look, and on a Mac the chapter headings printed in comics. Tap a chapter to pick its pages, then add them all.",
      where: "A gallery → Chapters → Suggest chapters", go: null },
    { k: "body", need: "body", img: "body-tags.jpg", title: "Body tags and clothing",
      text: "Tags for what's shown and worn - bare or covered, bras, panties, shoes, swimsuits, a butt sticking out - from a body-part detector and the picture's look. Ones it's nearly sure of wait in Review instead.",
      where: "Settings → Tasks → Body tags", go: "/settings?tab=tasks" },
    { k: "facts", need: "apple", img: "picture-facts.jpg", title: "Picture scores and labels",
      text: "Every photo gets an aesthetic score (0-100) and what's in it (beach, bedroom, swimsuit…), saved as custom fields you can filter by in Stash, and Review shows the better-looking photos first.",
      where: "Images → Filter → Custom fields", go: "/images" },
    { k: "screens", need: "helper", img: "screenshots.jpg", title: "Screenshot crop",
      text: "Phone screenshots of Instagram, OnlyFans or Reddit posts are cut down to the photo in them, so they look right and tag better. The originals are kept, and one task puts them all back.",
      where: "Settings → Tasks → Screenshots", go: "/settings?tab=tasks" },
    { k: "pictures", need: "none", img: "tag-pictures.jpg", title: "Tag pictures",
      text: "Every tag gets a picture of its own - the most typical photo for it, never one another tag has, skipping blurry or dark ones. Pick a new one from the Tags list's ••• menu.",
      where: "Tags → select → ••• → Most typical picture", go: "/tags" },
    { k: "setup", need: "none", img: "setup.jpg", title: "Setup and help",
      text: "The setup page checks the face helper, the AI model and the video model, says what to install for anything missing, and starts the tasks. Features show up as soon as what they need is installed.",
      where: "Settings → Plugins → Better Organizing AI → Setup", go: SETUP_PAGE },
  ];
  const tourBus = { open: null };

  async function welcomeOff() {
    const d = await gql("{ configuration { plugins } }");
    const cur = ((d.configuration || {}).plugins || {})[PLUGIN_ID] || {};
    await gql("mutation ($id: ID!, $i: Map!) { configurePlugin(plugin_id: $id, input: $i) }", { id: PLUGIN_ID, i: { ...cur, welcomeOff: true } });
  }

  function Tour({ onClose, slides }) {
    const [at, setAt] = React.useState(0);
    const track = React.useRef(null);
    const go = (i) => {
      const el = track.current;
      const n = Math.max(0, Math.min(slides.length - 1, i));
      if (el) el.scrollTo({ left: n * el.clientWidth, behavior: "smooth" });
      setAt(n);
    };
    React.useEffect(() => {
      const key = (e) => {
        if (e.key === "ArrowRight") go(at + 1);
        else if (e.key === "ArrowLeft") go(at - 1);
        else if (e.key === "Escape") onClose();
      };
      window.addEventListener("keydown", key);
      return () => window.removeEventListener("keydown", key);
    }, [at]);
    const onScroll = () => {
      const el = track.current;
      if (el && el.clientWidth) setAt(Math.round(el.scrollLeft / el.clientWidth));
    };
    const last = at === slides.length - 1;
    return h("div", { className: "bta-tour" },
      h("div", { className: "bta-tour-head" },
        h("span", null, `Tour · ${at + 1} of ${slides.length}`),
        h("button", { type: "button", className: "bta-tour-x", "aria-label": "Close the tour", onClick: onClose }, "✕")),
      h("div", { className: "bta-tour-track", ref: track, onScroll },
        slides.map((sl) => h("div", { className: "bta-tour-slide", key: sl.k },
          // (pb-keep: Privacy Blur leaves these alone - they're made with it on)
          h("div", { className: "bta-tour-shot pb-keep" }, h("img", { src: TOUR_IMG(sl.img), alt: sl.title, loading: "lazy" })),
          h("h3", null, sl.title),
          h("p", null, sl.text),
          h("div", { className: "bta-tour-where" }, h("b", null, "Where: "), sl.where,
            sl.go && h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => { onClose(); goTo(sl.go); } }, "Open it →"))))),
      h("div", { className: "bta-tour-foot" },
        h("button", { type: "button", className: "btn btn-secondary", disabled: at === 0, onClick: () => go(at - 1) }, "‹ Back"),
        h("div", { className: "bta-tour-dots" }, slides.map((sl, i) =>
          h("button", { key: sl.k, type: "button", className: "bta-tour-dot" + (i === at ? " on" : ""), "aria-label": sl.title, title: sl.title, onClick: () => go(i) }))),
        last ? h("button", { type: "button", className: "btn btn-primary", onClick: onClose }, "Done")
          : h("button", { type: "button", className: "btn btn-primary", onClick: () => go(at + 1) }, "Next ›")));
  }

  // ---- setup guide: what's missing, step by step (instead of the tour) -----
  // Each step: what it's for, what to do (commands to copy), a screenshot, the
  // matching common problems, and "Check again" to tick it off.
  const tip = (start) => COMMON.find(([q]) => q.startsWith(start));
  function setupSteps(c) {
    const docker = c.docker;
    // the helper runs on the computer Stash runs on: Windows when Stash does,
    // or (Stash in Docker) when this browser is on Windows - usually the same PC
    const win = c.windows || (docker && /Windows/i.test(navigator.userAgent));
    const setupCmd = win ? "powershell -ExecutionPolicy Bypass -File face-helper\\setup.ps1" + (docker ? " -ListenAll" : "") : "sh face-helper/setup.sh";
    const steps = [];
    const working = "What the setup page shows once it's working.";
    if (!c.helper) steps.push({ k: "helper", img: "setup-helper.jpg", caption: working, title: "Install the face helper", done: (x) => x.helper,
      intro: "Faces, look-alikes, body tags, search and picture scores all use the face helper - a small program that runs on your own computer" +
        (docker ? " (outside Docker, on the computer Stash's container runs on)." : "."),
      todo: [
        [(win ? "Open PowerShell" : "Open Terminal") + " in the add-on's folder (plugins/BetterTaggingAI, in Stash's config folder) and run the setup - it takes a few minutes and downloads the models:", setupCmd],
        ["Check that it answers:", win ? "Invoke-RestMethod http://127.0.0.1:11535/health" : "curl http://127.0.0.1:11535/health"],
        [`The add-on looks for it at ${c.face_url || "http://localhost:11535"}` + (docker ? " (Stash in Docker Desktop reaches your computer at host.docker.internal)." : ".") +
          " If yours is somewhere else: Settings → Plugins → Better Organizing AI → Setup: face helper address.", null],
      ],
      tips: [tip("\"Can't reach the face helper"), tip("setup.sh / setup.ps1 fails")] });
    else if (!c.embed || !c.text) steps.push({ k: "update", img: "setup-helper.jpg", caption: working, title: "Update the face helper", done: (x) => x.embed && x.text,
      intro: "The face helper is running, but it's missing " + [!c.embed && "the picture model (look-alikes, search)", !c.text && "the text model (search by description)"].filter(Boolean).join(" and ") + ".",
      todo: [["Run the setup again in the add-on's folder - it updates in place:", setupCmd]],
      tips: [tip("\"The face helper is too old"), tip("setup.sh / setup.ps1 fails")] });
    if (c.helper && c.embed && !c.looks) steps.push({ k: "scan", img: "setup-scan.jpg", caption: "Settings → Tasks → Look-alike tags.", title: "Scan your library's looks", done: (x) => x.looks,
      intro: "Look-alike tags, Review's suggestions, Check and search compare pictures by their look. Each scene and photo needs its fingerprint once (well under a second each); new ones get it as they're scanned.",
      todo: [["Run the task (Settings → Tasks → Look-alike tags), or start it here:", null]],
      task: "Tags: scan scenes and images for look-alikes (do this first)",
      tips: [tip("Look-alike tags, Similar"), tip("No face matches")] });
    if (!c.ollama) steps.push({ k: "ollama", img: "setup-ollama.jpg", caption: working, title: "Install the AI model (Ollama)", done: (x) => x.ollama,
      intro: "Ask AI for tags, AI titles and performer details use a local AI model in Ollama. Everything else works without it.",
      todo: [
        ["Install Ollama from ollama.com (" + (win ? "the Windows installer; it starts by itself and sits by the clock" : "on a Mac: brew install ollama, then start it with brew services start ollama") +
          "), then get the model (about 3.3 GB)" + (win ? " - in PowerShell:" : ":"), `ollama pull ${c.model || "gemma3:4b"}`],
        [`The add-on looks for it at ${c.ollama_url || "http://localhost:11434"}.` + (docker ? " Stash in Docker Desktop: http://host.docker.internal:11434; in Docker on Linux use network_mode: host and http://127.0.0.1:11434." : "") +
          " Change it under Setup: Ollama address.", null],
      ],
      tips: [tip("\"Can't reach Ollama"), tip("\"Ollama doesn't have")] });
    if (!c.auto_seek) steps.push({ k: "autoseek", img: "setup-scan.jpg", caption: "Settings → Tasks → Markers & positions.", title: "Get the video model (for AI markers)", done: (x) => x.auto_seek, optional: true,
      intro: "AI markers and positions in videos use Auto Seek's video model - the add-on downloads it itself; the Auto Seek plugin isn't needed. Optional - only for those.",
      todo: [
        ["Settings → Tasks → Better Organizing AI → Markers & positions: run \"Markers: download the video model (needed first)\" once (about 100 MB).", null],
        ["Or, if you have the Auto Seek plugin, the add-on uses its model and keeps its own copy.", null],
      ],
      tips: [] });
    steps.push({ k: "check", img: "setup.jpg", title: "Check it all", last: true,
      intro: "The setup page checks everything the add-on needs, says what to do about anything still missing, shows recent errors and makes a report you can copy. Features show up by themselves once what they need is installed.",
      todo: [], tips: [tip("Tasks stop working"), tip("The AI is slow")] });
    return steps;
  }

  function CopyLine({ text }) {
    const [ok, setOk] = React.useState(false);
    return h("div", { className: "bta-guide-code" }, h("code", null, text),
      h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => {
        navigator.clipboard?.writeText(text).then(() => { setOk(true); setTimeout(() => setOk(false), 1500); });
      } }, ok ? "Copied" : "Copy"));
  }

  function SetupGuide({ onClose, onTour }) {
    const cz = useCaps();
    const [steps] = React.useState(() => setupSteps(capsNow())); // (the list stays put while you work through it)
    const [at, setAt] = React.useState(0);
    const [checking, setChecking] = React.useState(false);
    const [started, setStarted] = React.useState({});
    const track = React.useRef(null);
    const go = (i) => {
      const el = track.current;
      const n = Math.max(0, Math.min(steps.length - 1, i));
      if (el) el.scrollTo({ left: n * el.clientWidth, behavior: "smooth" });
      setAt(n);
    };
    const onScroll = () => { const el = track.current; if (el && el.clientWidth) setAt(Math.round(el.scrollLeft / el.clientWidth)); };
    const recheck = () => { setChecking(true); refreshCaps(true); setTimeout(() => setChecking(false), 2500); };
    const todo = steps.filter((s) => s.done && !s.optional && !s.done(cz));
    const last = at === steps.length - 1;
    return h("div", { className: "bta-tour" },
      h("div", { className: "bta-tour-head" },
        h("span", null, `Setup · step ${at + 1} of ${steps.length}`),
        h("button", { type: "button", className: "bta-tour-x", "aria-label": "Close the setup guide", onClick: onClose }, "✕")),
      h("div", { className: "bta-tour-track", ref: track, onScroll },
        steps.map((s) => h("div", { className: "bta-tour-slide", key: s.k },
          h("div", { className: "bta-tour-shot bta-guide-shot pb-keep" }, h("img", { src: TOUR_IMG(s.img), alt: s.title, loading: "lazy" })),
          s.caption && h("div", { className: "bta-guide-caption" }, s.caption),
          h("h3", null, s.title, " ",
            s.done && h("span", { className: "bta-guide-state" + (s.done(cz) ? " ok" : "") }, s.done(cz) ? "✓ Done" : s.optional ? "Optional" : "To do")),
          h("p", null, s.intro),
          s.todo.length > 0 && h("ol", { className: "bta-guide-steps" }, s.todo.map(([say, cmd], i) =>
            h("li", { key: i }, say, cmd && h(CopyLine, { text: cmd })))),
          s.task && h("div", { className: "bta-guide-actions" },
            h("button", { type: "button", className: "btn btn-primary btn-sm", disabled: !!started[s.task],
              onClick: () => runTask(s.task).then(() => setStarted((x) => ({ ...x, [s.task]: "Started - it runs in Settings → Tasks." })))
                .catch((e) => setStarted((x) => ({ ...x, [s.task]: "Couldn't start: " + e.message }))) }, "▶ " + s.task),
            started[s.task] && h("span", { className: "bta-guide-note" }, started[s.task])),
          s.last && h("div", { className: "bta-guide-actions" },
            h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { onClose(); goTo(SETUP_PAGE); } }, "Open the setup page"),
            todo.length === 0 && h("button", { type: "button", className: "btn btn-primary btn-sm", onClick: onTour }, "All set - take the tour"),
            todo.length > 0 && h("span", { className: "bta-guide-note" }, "Still to do: " + todo.map((x) => x.title).join(", "))),
          s.tips.filter(Boolean).length > 0 && h("div", { className: "bta-guide-tips" },
            h("div", { className: "bta-guide-tips-head" }, "If it doesn't work"),
            s.tips.filter(Boolean).map(([q, a]) => h("details", { key: q }, h("summary", null, q), h("p", null, a)))),
          s.done && h("div", { className: "bta-guide-actions" },
            h("button", { type: "button", className: "btn btn-secondary btn-sm", disabled: checking, onClick: recheck }, checking ? "Checking…" : "↻ Check again"))))),
      h("div", { className: "bta-tour-foot" },
        h("button", { type: "button", className: "btn btn-secondary", disabled: at === 0, onClick: () => go(at - 1) }, "‹ Back"),
        h("div", { className: "bta-tour-dots" }, steps.map((s, i) =>
          h("button", { key: s.k, type: "button", className: "bta-tour-dot" + (i === at ? " on" : ""), "aria-label": s.title, title: s.title, onClick: () => go(i) }))),
        last ? h("button", { type: "button", className: "btn btn-primary", onClick: onClose }, "Close")
          : h("button", { type: "button", className: "btn btn-primary", onClick: () => go(at + 1) }, "Next ›")));
  }

  function Welcome() {
    const cz = useCaps();
    const [step, setStep] = React.useState(null); // null (hidden) | "hello" | "tour" | "setup"
    React.useEffect(() => {
      tourBus.open = () => setStep("tour");
      tourBus.setup = () => setStep("setup");
      if (/^\/login/.test(location.pathname)) return undefined;
      let live = true;
      const t = setTimeout(async () => {
        try { if (Number(localStorage.getItem(LATER_KEY) || 0) > Date.now()) return; } catch (e) { /* fine */ }
        try {
          const d = await gql("{ configuration { plugins } }");
          const cur = ((d.configuration || {}).plugins || {})[PLUGIN_ID] || {};
          if (cur.welcomeOff) return;
          // (after Stash's own windows, like the release notes on a new Stash, are closed)
          while (live && document.querySelector(".modal.show, .modal-backdrop")) await new Promise((r) => setTimeout(r, 1000));
          if (live) setStep((s0) => s0 || "hello");
        } catch (e) { /* fine */ }
      }, 1500);
      return () => { live = false; clearTimeout(t); };
    }, []);
    if (!step) return null;
    const known = cz.helper !== undefined;
    const ready = TOUR.filter((sl) => can(sl.need, cz));
    const missing = known ? TOUR.filter((sl) => !can(sl.need, cz)) : [];
    const needsSetup = known && setupSteps(cz).some((s) => s.done && !s.optional);
    const close = () => setStep(null);
    const off = () => { welcomeOff().catch(() => {}); close(); };
    const later = () => { try { localStorage.setItem(LATER_KEY, String(Date.now() + 24 * 3600 * 1000)); } catch (e) { /* fine */ } close(); };
    return h("div", { className: "bta-welcome-back", onClick: (e) => { if (e.target === e.currentTarget && step === "tour") off(); } },
      h("div", { className: "bta-welcome modal-content", role: "dialog", "aria-modal": "true", "aria-label": "Welcome to Better Organizing AI" },
        step === "hello" ? h(React.Fragment, null,
          h("h2", null, "Welcome to Better Organizing AI"),
          h("p", null, "It helps you tag and organize your library with AI that runs on your own computer - nothing is sent anywhere. It suggests tags, performers and studios, finds look-alikes and faces, makes markers and chapters, crops screenshots and lets you search by describing a picture. You decide: suggestions wait in Review until you say yes."),
          known && h("div", { className: "bta-welcome-list" },
            h("b", null, "Ready to use: "), ready.map((sl) => sl.title).join(" · ")),
          missing.length > 0 && h("div", { className: "bta-welcome-list bta-welcome-missing" },
            h("b", null, "Needs setting up: "), missing.map((sl) => sl.title).join(" · "), " ",
            h("a", { href: SETUP_PAGE, onClick: (e) => { e.preventDefault(); close(); goTo(SETUP_PAGE); } }, "Open the setup page")),
          h("p", { className: "bta-welcome-ask" }, needsSetup
            ? "A few things need setting up first. Want to go through them step by step?"
            : "Would you like a quick tour of what it can do?"),
          h("div", { className: "bta-welcome-btns" },
            needsSetup
              ? h("button", { type: "button", className: "btn btn-primary", onClick: () => setStep("setup") }, "Set it up with me")
              : h("button", { type: "button", className: "btn btn-primary", onClick: () => setStep("tour") }, "Show me around"),
            h("button", { type: "button", className: "btn btn-secondary", onClick: later }, "Remind me later"),
            h("button", { type: "button", className: "btn btn-link", onClick: off }, "Don't show again")))
          : step === "setup" ? h(SetupGuide, { onClose: later, onTour: () => setStep("tour") })
          : h(Tour, { slides: ready.length ? ready : TOUR, onClose: off })));
  }
  {
    const host = document.createElement("div");
    host.className = "bta-welcome-host";
    document.body.appendChild(host);
    api.ReactDOM.render(h(Welcome), host);
  }

  // ---- "Describe it" search: find photos, scenes and markers by words ------
  const SEARCH_PAGE = "/ai-search";
  const SEARCH_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/><path d="M8.5 9.5l1 1 2-2" opacity=".6"/></svg>';
  const SEARCH_KINDS = [["image", "Photos"], ["scene", "Scenes"], ["marker", "Markers"]];
  const SEARCH_LOOKS = [[0, "Any look"], [55, "Decent+"], [65, "Good+"], [75, "Great+"]];

  function AISearch() {
    const cz = useCaps();
    const winPC = cz.windows || (cz.docker && /Windows/i.test(navigator.userAgent)); // (Windows: the PowerShell commands)
    const params = new URLSearchParams(window.location.search);
    const [text, setText] = React.useState(params.get("q") || "");
    const [kinds, setKinds] = React.useState(new Set((params.get("kinds") || "image,scene,marker").split(",")));
    const [looks, setLooks] = React.useState(Number(params.get("looks") || 0));
    const [st, setSt] = React.useState({ state: "idle", items: [] });
    const Link = api.libraries && api.libraries.ReactRouterDOM && api.libraries.ReactRouterDOM.Link;

    const run = React.useCallback(async (q, ks, lk) => {
      if (!q.trim()) return;
      const u = new URL(window.location.href);
      u.searchParams.set("q", q); u.searchParams.set("kinds", [...ks].join(",")); u.searchParams.set("looks", String(lk));
      window.history.replaceState(null, "", u.pathname + u.search);
      setSt({ state: "loading", items: [] });
      try {
        const r = await ask({ mode: "ai_search", text: q, kinds: [...ks], limit: 90, min_aesthetic: lk || null });
        setSt({ state: "done", items: (r && r.items) || [], weak: !!(r && r.weak) });
      } catch (e) {
        setSt({ state: "error", items: [], message: e.message });
      }
    }, []);
    React.useEffect(() => { if (text) run(text, kinds, looks); }, []);

    const toggle = (k) => {
      const next = new Set(kinds);
      next.has(k) ? next.delete(k) : next.add(k);
      if (!next.size) return;
      setKinds(next);
      if (text.trim()) run(text, next, looks);
    };
    const card = (it) => {
      const inner = [
        h("div", { className: "bta-search-pic", key: "p" }, h("img", { src: it.picture, loading: "lazy", alt: "" }),
          h("span", { className: "bta-search-kind" }, it.kind === "image" ? "Photo" : it.kind === "scene" ? "Scene" : "Marker")),
        h("div", { className: "bta-search-meta", key: "m" },
          h("div", { className: "bta-search-title" }, it.title || ""),
          h("div", { className: "bta-search-sub" }, `match ${Math.round(it.score * 100)}` + (it.aesthetic != null ? ` · looks ${it.aesthetic}/100` : ""))),
      ];
      return Link ? h(Link, { to: it.link, className: "bta-search-card", key: it.kind + it.id }, ...inner)
        : h("a", { href: it.link, className: "bta-search-card", key: it.kind + it.id }, ...inner);
    };
    if (!can("search", cz)) {
      return h("div", { className: "bta-search" }, h("h2", null, "Search by description"),
        h("p", { className: "bta-search-help" }, cz.text === undefined ? "Checking what's installed…"
          : !cz.helper ? `Can't reach the face helper${cz.face_url ? " at " + cz.face_url : ""} - is it running? It starts at sign-in; check it with ${winPC ? "Invoke-RestMethod" : "curl"} http://127.0.0.1:11535/health, or run the setup again (${winPC ? "face-helper\\setup.ps1" : "face-helper/setup.sh"}). `
          : !cz.text || !cz.embed ? `The face helper is missing CLIP's text model, which search uses - run ${winPC ? "face-helper\\setup.ps1" : "face-helper/setup.sh"} again (it updates in place). `
          : "Search needs your library's look-alike fingerprints first: run \"Tags: scan scenes and images for look-alikes (do this first)\" under Settings → Tasks. ",
          cz.text !== undefined && h("a", { href: SETUP_PAGE }, "Open the setup page"),
          cz.text !== undefined && " · ",
          cz.text !== undefined && h("a", { href: "#", onClick: (e) => { e.preventDefault(); refreshCaps(true); } }, "Check again")));
    }
    return h("div", { className: "bta-search" },
      h("h2", null, "Search by description"),
      h("form", { className: "bta-search-form", onSubmit: (e) => { e.preventDefault(); run(text, kinds, looks); } },
        h("input", { type: "search", className: "form-control", value: text, autoFocus: true, enterKeyHint: "search",
          placeholder: "e.g. red dress at the beach, kneeling on a bed, black and white photo",
          onChange: (e) => setText(e.target.value) }),
        h("button", { type: "submit", className: "btn btn-primary" }, "Search")),
      h("div", { className: "bta-search-filters" },
        SEARCH_KINDS.map(([k, label]) => h("button", { type: "button", key: k,
          className: "bta-fr-chip" + (kinds.has(k) ? " active" : ""), onClick: () => toggle(k) }, (kinds.has(k) ? "✓ " : "") + label)),
        h("select", { className: "form-control bta-search-looks", value: looks, title: "Photos by their aesthetic score",
          onChange: (e) => { const v = Number(e.target.value); setLooks(v); if (text.trim()) run(text, kinds, v); } },
          SEARCH_LOOKS.map(([v, label]) => h("option", { key: v, value: v }, label)))),
      st.state === "loading" && h("div", { className: "bta-fr-empty" }, "Looking through your pictures… (the first search after a scan takes a little longer)"),
      st.state === "error" && h("div", { className: "bta-fr-empty" }, "Couldn't search: " + st.message),
      st.state === "done" && !st.items.length && h("div", { className: "bta-fr-empty" }, "Nothing found."),
      st.state === "idle" && h("p", { className: "bta-search-help" },
        "Describe what's in the picture - people, clothes, place, pose, colours, mood. It compares your words with how every photo, scene and AI marker looks (the look-alike fingerprints), so it finds things no tag or title says. \"Looks\" filters photos by their aesthetic score."),
      st.weak && st.items.length > 0 && h("p", { className: "bta-search-help" },
        "Nothing looks much like that - these are the closest. Describe what's in the picture in plain English (people, clothes, place, colours)."),
      st.items.length > 0 && h("div", { className: "bta-search-grid" }, st.items.map(card)));
  }

  // ---- Nightly & after-scan jobs page (/ai-nightly) ----------------------
  // Two lists of jobs - Stash's own tasks (backup, scan, clean, identify,
  // auto tag, generate...) and any plugin's tasks, in order: one runs every
  // night at the night time, the other when a scan you start finishes. Saved
  // in the add-on's settings; the night timer queues them and a tracker notes
  // how each went (shown under "Last run").
  const NIGHTLY_PAGE = "/ai-nightly";
  const NIGHT_ICON = { finished: ["✓", "ok"], failed: ["✕", "fail"], stopped: ["■", "warn"], skipped: ["–", "info"],
    queued: ["…", "info"], running: ["▶", "info"], waiting: ["·", "info"] };
  const JOB_TABS = {
    night: { label: "🌙 Every night", task: "Schedules: run the nightly jobs now", chosen: "chosen", last: "last" },
    scan: { label: "🔍 After a scan", task: "Schedules: run the after-scan jobs now", chosen: "scan_chosen", last: "scan_last" },
  };
  const jobDate = (s) => new Date(String(s).replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
  function NightlyPage() {
    const [tab, setTab] = React.useState(() => (new URLSearchParams(location.search).get("list") === "scan" ? "scan" : "night"));
    const [info, setInfo] = React.useState(null);
    const [err, setErr] = React.useState("");
    const [drafts, setDrafts] = React.useState(null); // { night: {...}, scan: {...} }
    const [dirty, setDirty] = React.useState({});
    const [msg, setMsg] = React.useState("");
    const [busy, setBusy] = React.useState(false);
    const [all, setAll] = React.useState([]);
    const [pick, setPick] = React.useState("");
    const fromInfo = (d, which) => {
      const c = d[JOB_TABS[which].chosen];
      return { order: c.order.map((i) => ({ ...i })), time: d.time, tz: d.tz, base: (d.saved_at || {})[which] || "" };
    };
    // keep: keep the lists with unsaved changes (the rest show what's saved now)
    const dirtyRef = React.useRef({});
    dirtyRef.current = dirty;
    const load = React.useCallback((keep) => ask({ mode: "nightly_info" }).then((d) => {
      setInfo(d);
      setDrafts((cur) => {
        const next = { ...(cur || {}) };
        ["night", "scan"].forEach((w) => { if (!keep || !next[w] || !dirtyRef.current[w]) next[w] = fromInfo(d, w); });
        return next;
      });
      if (!keep) setDirty({});
    }).catch((e) => setErr(e.message)), []);
    // back on the page (another app, tab or device may have changed the
    // lists): show what's saved now
    React.useEffect(() => {
      const again = () => { if (document.visibilityState === "visible") load(true); };
      document.addEventListener("visibilitychange", again);
      window.addEventListener("pageshow", again);
      window.addEventListener("focus", again);
      return () => { document.removeEventListener("visibilitychange", again); window.removeEventListener("pageshow", again); window.removeEventListener("focus", again); };
    }, []);
    React.useEffect(() => {
      load(false);
      gql("{ plugins { id name enabled tasks { name description } } }").then((d) => setAll((d.plugins || []).filter((p) => p.enabled && (p.tasks || []).length))).catch(() => {});
    }, []);
    const cfg = JOB_TABS[tab];
    const last0 = info && info[cfg.last];
    const last = last0 && last0.started && Array.isArray(last0.jobs) ? last0 : null;
    // while a run is still going: refresh its status
    React.useEffect(() => {
      if (!last || last.done) return undefined;
      const t = setInterval(() => load(true), 15000);
      return () => clearInterval(t);
    }, [tab, last && last.done, last && last.started]);
    const shell = (body) => h("div", { className: "bta-setup bta-night" }, h("h2", null, "Nightly & after-scan jobs"), body);
    if (err) return shell(h("div", { className: "bta-setup-banner fail" }, "Couldn't load: " + err));
    if (!info || !drafts) return shell(h("div", { className: "bta-setup-banner" }, "Loading…"));
    const draft = drafts[tab];
    const change = (fn) => {
      setDrafts((ds) => ({ ...ds, [tab]: fn({ ...ds[tab], order: ds[tab].order.slice() }) }));
      setDirty((x) => ({ ...x, [tab]: true })); setMsg("");
    };
    const itemKey = (i) => (i.stash ? "stash\u0001" + i.stash : i.plugin + "\u0001" + i.task);
    const taskKey = itemKey;
    const have = new Set(draft.order.map(itemKey));
    const stashJob = (id) => info.stash_jobs.find((j) => j.id === id) || { label: id, about: "" };
    const pluginName = (id) => ((all.find((p) => p.id === id) || {}).name) || id;
    const taskInfo = (t) => ((all.find((p) => p.id === t.plugin) || { tasks: [] }).tasks.find((x) => x.name === t.task));
    const installed = (t) => !all.length || !!taskInfo(t);
    const ownTask = (t) => t.plugin === PLUGIN_ID && (t.task === JOB_TABS.night.task || t.task === JOB_TABS.scan.task);
    async function save() {
      setBusy(true); setMsg("");
      try {
        const d = await ask({ mode: "nightly_save", data: { order: draft.order, time: draft.time, tz: draft.tz, which: tab, base: draft.base || "" } });
        setInfo(d);
        setDrafts((ds) => ({ ...ds, [tab]: fromInfo(d, tab) }));
        setDirty((x) => ({ ...x, [tab]: false })); setMsg("Saved.");
        return true;
      } catch (e) {
        setMsg("Couldn't save: " + e.message);
        if (/changed on another device/.test(e.message)) { setDirty((x) => ({ ...x, [tab]: false })); dirtyRef.current = { ...dirtyRef.current, [tab]: false }; load(true); }
        return false;
      } finally { setBusy(false); }
    }
    async function runNow() {
      if (dirty[tab] && !(await save())) return;
      setBusy(true);
      try {
        await gql("mutation ($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }", { p: PLUGIN_ID, t: cfg.task });
        setMsg("Started - they're queued in Stash's task list; the results show below as they finish.");
        setTimeout(() => load(true), 4000);
      } catch (e) { setMsg("Couldn't start: " + e.message); } finally { setBusy(false); }
    }
    const blocked = tab === "scan" ? new Set(info.not_after_scan || []) : new Set();
    const stashFree = info.stash_jobs.filter((j) => !blocked.has(j.id) && !have.has("stash\u0001" + j.id));
    const move = (i, by) => change((d) => { const t = d.order; const [x] = t.splice(i, 1); t.splice(i + by, 0, x); return d; });
    const add = (item) => change((d) => ({ ...d, order: [...d.order, item] }));
    const next = info.next ? new Date(info.next) : null;
    const count = draft.order.length;
    const countOf = (w) => drafts[w].order.length;
    const anything = countOf("night") + countOf("scan") + info.waiting > 0;
    return shell(h(React.Fragment, null,
      h("p", { className: "bta-setup-sub" }, "Choose jobs - Stash's own tasks and any plugin's - to run every night, and to run when a scan you start finishes. Stash runs one job at a time, so each list runs in the order shown. ",
        h("a", { href: SETUP_PAGE, onClick: (e) => { e.preventDefault(); goTo(SETUP_PAGE); } }, "Back to AI setup")),
      h("div", { className: "bta-night-tabs", role: "tablist" },
        Object.entries(JOB_TABS).map(([w, t]) => h("button", { key: w, type: "button", role: "tab", "aria-selected": tab === w,
          className: "btn " + (tab === w ? "btn-primary" : "btn-secondary"),
          onClick: () => { setTab(w); setPick(""); setMsg(""); history.replaceState(null, "", NIGHTLY_PAGE + (w === "scan" ? "?list=scan" : "")); } },
          t.label + (countOf(w) ? ` (${countOf(w)})` : "") + (dirty[w] ? " •" : "")))),
      h("div", { className: "bta-setup-banner " + (count ? (info.runner || !anything ? "ok" : "warn") : "") },
        count ? `${count} job${count > 1 ? "s" : ""} chosen` : "Nothing chosen yet",
        h("small", null, tab === "night"
          ? (next ? `Next run: ${next.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })} (${info.time} ${info.tz || "on Stash's computer's clock"})` : `At ${info.time} (${info.tz || "on Stash's computer's clock"})`) +
            (info.waiting ? ` · ${info.waiting} new item(s) waiting for the AI` : "")
          : "When a scan you start finishes (Stash → Tasks → Scan, or a scan button). Scans the nightly run starts don't count - its own list runs after them. Several scans close together: once, after the last.",
          anything ? (info.runner ? " · the timer is running" : " · the timer starts when you save") : "")),
      tab === "night" && h("section", { className: "bta-setup-group" },
        h("h3", null, "When"),
        h("div", { className: "bta-night-when" },
          h("label", null, h("span", { className: "bta-setup-title" }, "Time"),
            h("input", { type: "time", className: "form-control", value: draft.time, onChange: (e) => change((d) => ({ ...d, time: e.target.value })) })),
          h("label", null, h("span", { className: "bta-setup-title" }, "Time zone"),
            h("input", { type: "text", className: "form-control", value: draft.tz, placeholder: "e.g. America/New_York (empty: Stash's computer's clock)", spellCheck: false,
              onChange: (e) => change((d) => ({ ...d, tz: e.target.value })) })))),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "The jobs, in the order they run"),
        h("div", { className: "bta-setup-detail" }, "Move any job up or down. Usually Scan first (so the rest see new files), then Identify and the tagging, with Generate and Optimise last. " +
          (tab === "scan" ? "Scan isn't offered here - it would start this list again. " : "") +
          "Scan, Identify, Auto Tag and Generate use the options saved on ", h("a", { href: "/settings?tab=tasks" }, "Stash's Tasks page"), "."),
        tab === "night" && info.nightly_items && h("div", { className: "bta-setup-detail" }, "\"New items: wait until night for the AI model steps\" is on: new items saved for tonight are always done, even if that task isn't in the list."),
        draft.order.length === 0 && h("div", { className: "bta-setup-detail bta-night-empty" }, "Nothing yet - add jobs below."),
        draft.order.map((it, i) => {
          const sj = it.stash && stashJob(it.stash);
          const ok = it.stash ? true : installed(it);
          return h("div", { key: itemKey(it), className: "bta-night-task" + (ok ? "" : " missing") + (it.stash ? " stash" : "") },
            h("span", { className: "bta-night-n" }, i + 1),
            h("span", { className: "bta-night-name" },
              h("span", { className: "bta-setup-title" }, it.stash ? sj.label : it.task),
              h("span", { className: "bta-setup-detail" }, it.stash ? "Stash · " + sj.about : pluginName(it.plugin) + (ok ? "" : " - not installed or turned off: skipped"))),
            h("span", { className: "bta-night-btns" },
              h("button", { type: "button", className: "btn btn-secondary btn-sm", disabled: i === 0, onClick: () => move(i, -1) }, "↑ Up"),
              h("button", { type: "button", className: "btn btn-secondary btn-sm", disabled: i === draft.order.length - 1, onClick: () => move(i, 1) }, "↓ Down"),
              h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => change((d) => ({ ...d, order: d.order.filter((_, k) => k !== i) })) }, "✕ Remove")));
        }),
        h("div", { className: "bta-setup-run" },
          h("select", { className: "form-control", value: pick, onChange: (e) => setPick(e.target.value), "aria-label": "Job to add" },
            h("option", { value: "" }, "Add a job - Stash's or any plugin's…"),
            stashFree.length > 0 && h("optgroup", { label: "Stash" }, stashFree.map((j) => h("option", { key: j.id, value: "stash\u0001" + j.id }, j.label))),
            all.map((p) => h("optgroup", { key: p.id, label: p.name },
              p.tasks.filter((t) => !have.has(p.id + "\u0001" + t.name) && !ownTask({ plugin: p.id, task: t.name }))
                .map((t) => h("option", { key: t.name, value: p.id + "\u0001" + t.name }, t.name))))),
          h("button", { type: "button", className: "btn btn-primary", disabled: !pick, onClick: () => {
            const [a1, a2] = pick.split("\u0001");
            add(a1 === "stash" ? { stash: a2 } : { plugin: a1, task: a2 }); setPick("");
          } }, "+ Add")),
        pick && h("div", { className: "bta-setup-detail" }, pick.startsWith("stash\u0001") ? stashJob(pick.split("\u0001")[1]).about
          : (taskInfo({ plugin: pick.split("\u0001")[0], task: pick.split("\u0001")[1] }) || {}).description || ""),
        h("div", { className: "bta-night-sug" }, h("span", { className: "bta-setup-detail" }, "Quick add: "),
          stashFree.filter((j) => ["scan", "identify", "autotag", "generate"].includes(j.id)).map((j) => h("button", { key: j.id, type: "button", className: "btn btn-secondary btn-sm",
            onClick: () => add({ stash: j.id }) }, "+ " + j.label)),
          info.suggested.filter((t) => !have.has(taskKey(t)) && installed(t) && !(tab === "scan" && t.task === "Schedules: run tonight's waiting steps for new items now"))
            .map((t) => h("button", { key: t.task, type: "button", className: "btn btn-secondary btn-sm", onClick: () => add(t) }, "+ " + t.task)))),
      h("div", { className: "bta-setup-tools bta-night-save" },
        h("button", { type: "button", className: "btn btn-primary", disabled: busy || !dirty[tab], onClick: save }, dirty[tab] ? "Save" : "✓ Saved"),
        h("button", { type: "button", className: "btn btn-secondary", disabled: busy || !count, onClick: runNow }, "▶ Run them now"),
        dirty[tab] && h("button", { type: "button", className: "btn btn-link", disabled: busy,
          onClick: () => { setDrafts((ds) => ({ ...ds, [tab]: fromInfo(info, tab) })); setDirty((x) => ({ ...x, [tab]: false })); } }, "Undo changes"),
        msg && h("span", { className: "bta-setup-detail" }, msg)),
      h("section", { className: "bta-setup-group" },
        h("h3", null, "Last run"),
        !(last && last.started && Array.isArray(last.jobs)) ? h("div", { className: "bta-setup-detail" }, "Hasn't run yet.") : h("div", null,
          h("div", { className: "bta-setup-detail" }, `Started ${jobDate(last.started).toLocaleString()}` +
            (last.done ? (last.ended ? ` · all done ${jobDate(last.ended).toLocaleString()}` : " · done") : " · still going (updates every 15 s)")),
          last.jobs.map((r, i) => {
            const [icon, st] = NIGHT_ICON[r.status] || ["?", "info"];
            return h("div", { key: i, className: "bta-setup-check " + st },
              h("span", { className: "bta-setup-icon" }, icon),
              h("div", null, h("div", { className: "bta-setup-title" }, r.label),
                h("div", { className: "bta-setup-detail" }, (r.status === "waiting" ? "Waiting - starts when the jobs before it (and what they set off) are done"
                  : r.status[0].toUpperCase() + r.status.slice(1)) + (r.note ? " - " + r.note : ""))));
          })))));
  }

  if (api.register && api.register.route) {
    api.register.route(SEARCH_PAGE, AISearch);
    api.register.route(SETUP_PAGE, SetupPage);
    api.register.route(NIGHTLY_PAGE, NightlyPage);
    api.register.route(REVIEW_PATH, FaceReview);
    api.register.route(REVIEW_PAGE, FaceReview);
    // a "Review" item in Stash's menu (Modern UI's More panel picks it up)
    const Link = api.libraries && api.libraries.ReactRouterDOM && api.libraries.ReactRouterDOM.Link;
    api.patch.after("MainNavBar.MenuItems", function (...args) {
      const result = args[args.length - 1];
      if (!result || !result.props || !Link) return result;
      const item = h("div", { className: "nav-link", key: "bta-face-review" },
        h(Link, {
          to: REVIEW_PAGE,
          className: "minimal p-4 p-xl-2 d-flex d-xl-inline-block flex-column justify-content-between align-items-center btn btn-primary" +
            ([REVIEW_PATH, REVIEW_PAGE].includes(window.location.pathname) ? " active" : ""),
          title: "Strong face matches and confident tags, one at a time",
        },
          h("span", { dangerouslySetInnerHTML: { __html: FACE_ICON }, style: { display: "contents" } }),
          h("span", null, "Review")));
      const search = h("div", { className: "nav-link", key: "bta-ai-search" },
        h(Link, {
          to: SEARCH_PAGE,
          className: "minimal p-4 p-xl-2 d-flex d-xl-inline-block flex-column justify-content-between align-items-center btn btn-primary" +
            (window.location.pathname === SEARCH_PAGE ? " active" : ""),
          title: "Find photos, scenes and markers by describing them",
        },
          h("span", { dangerouslySetInnerHTML: { __html: SEARCH_ICON }, style: { display: "contents" } }),
          h("span", null, "Search")));
      return React.cloneElement(result, {}, ...React.Children.toArray(result.props.children), item, ...(can("search") ? [search] : []));
    });
  }

  api.patch.before("FilteredTagList", function (...args) {
    const props = args[0] || {};
    const op = {
      text: "Most typical picture (AI)",
      isDisplayed: (result, filter, selectedIds) => !!selectedIds && selectedIds.size > 0 && can("looks"),
      onClick: (result, filter, selectedIds) => {
        typical.request = [...(selectedIds || [])].map(String);
        typical.listeners.forEach((fn) => fn());
      },
    };
    return [Object.assign({}, props, { extraOperations: [...(props.extraOperations || []), op] }), ...args.slice(1)];
  });

  api.patch.after("FilteredTagList", function (...args) {
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(TypicalHost));
  });

  // ---- Scene page → Markers: "AI markers" ----------------------------------
  // Where the scene shifts (from the Auto Seek plugin's model), each with a
  // picture, as markers named by their time, under the tag from the setting
  // "Markers: tag for AI markers" (Scene Shift unless changed; made when first
  // needed). Each runs until the next. Nothing is added without a tap.
  async function aiMarkerTagName() {
    try {
      const d = await gql("{ configuration { plugins } }");
      const v = (((d.configuration.plugins || {})[PLUGIN_ID] || {}).aiMarkerTag || "").trim();
      if (v) return v;
    } catch (e) { /* the default */ }
    return "Scene Shift";
  }
  function AiMarkers({ sceneId }) {
    const [st, setSt] = React.useState({ state: "idle" });
    const [have, setHave] = React.useState([]); // the scene's markers: [{seconds, tag}]
    const [busy, setBusy] = React.useState({});
    const addingRef = React.useRef(false);
    const [tagName, setTagName] = React.useState("Scene Shift");
    const [showAll, setShowAll] = React.useState(false); // the cards again after all were added
    React.useEffect(() => { aiMarkerTagName().then(setTagName); }, []);
    const loadHave = () => gql("query($id: ID!) { findScene(id: $id) { scene_markers { seconds } } }", { id: sceneId })
      .then((d) => setHave(((d.findScene || {}).scene_markers || []).map((m) => ({ seconds: m.seconds }))))
      .catch(() => {});
    React.useEffect(() => { setSt({ state: "idle" }); loadHave(); }, [sceneId]);
    async function look(fresh) {
      setShowAll(false);
      setSt({ state: "loading" });
      try {
        const out = await ask({ mode: "video_markers", id: sceneId, fresh: !!fresh });
        setSt({ state: "done", ...out });
      } catch (e) {
        setSt({ state: "error", message: e.message });
      }
    }
    const added = (m) => have.some((x) => Math.abs(x.seconds - m.start) < 2 * (st.step || 8));
    function play(t) {
      const v = document.querySelector(".video-js video, video");
      if (!v) return;
      // (before the video has loaded, a jump is lost: wait for it)
      const seek = () => { v.currentTime = t; };
      if (v.readyState >= 1) seek(); else v.addEventListener("loadedmetadata", seek, { once: true });
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    }
    async function refreshStash() {
      try {
        const c = api.utils.StashService.getClient();
        await c.refetchQueries({ include: "active" });
      } catch (e) { /* the list shows them after a reload */ }
    }
    async function add(list) {
      // (one add at a time: a second tap while markers are being made is
      // ignored - the busy mark below only shows after the next redraw)
      if (addingRef.current) return;
      addingRef.current = true;
      const todo = list.filter((m) => !added(m));
      setBusy((b) => ({ ...b, ...Object.fromEntries(todo.map((m) => [m.start, true])) }));
      try {
        // named "time - scene title", tagged Scene Shift (or the setting's
        // tag), each with the picture shown on its card
        await ask({ mode: "add_markers", scene: sceneId, markers: todo.map((m) => ({ start: m.start, end: m.end, picture: m.picture })) });
      } catch (e) {
        setSt((s) => ({ ...s, addError: e.message }));
      }
      await loadHave();
      setBusy({});
      addingRef.current = false;
      refreshStash();
    }
    if (st.state === "idle") {
      return h("div", { className: "bta-markers" },
        h("button", { type: "button", className: "btn btn-secondary bta-markers-go", onClick: () => look(false) }, "✨ AI markers"),
        h("span", { className: "bta-markers-note" }, "Finds where the scene shifts and shows a picture of each; you choose which to add."));
    }
    if (st.state === "loading") {
      return h("div", { className: "bta-markers" },
        h("div", { className: "bta-markers-note" }, "Looking through the scene… a frame every few seconds, so a long scene can take a minute."));
    }
    if (st.state === "error") {
      return h("div", { className: "bta-markers" },
        h("div", { className: "bta-markers-error" }, "Couldn't look at it: " + st.message),
        h("button", { type: "button", className: "btn btn-secondary", onClick: () => look(false) }, "Try again"));
    }
    const ms = st.markers || [];
    const left = ms.filter((m) => !added(m));
    const adding = Object.keys(busy).length > 0;
    // all added (Add all, or one by one): the cards are put away
    if (ms.length && !left.length && !showAll) {
      return h("div", { className: "bta-markers" },
        h("div", { className: "bta-markers-head" },
          h("span", { className: "bta-markers-done" }, `✓ All ${ms.length} AI markers added`),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setShowAll(true) }, "Show them"),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => look(true) }, "↻ Look again")));
    }
    return h("div", { className: "bta-markers" },
      h("div", { className: "bta-markers-head" },
        h("b", null, ms.length ? `AI markers (${ms.length})` : "No shifts found"),
        showAll && !left.length && h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setShowAll(false) }, "Hide them"),
        left.length > 1 && h("button", { type: "button", className: "btn btn-primary btn-sm", disabled: adding, onClick: () => add(left) },
          adding ? "Adding…" : `+ Add all ${left.length}`),
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => look(true) }, "↻ Look again")),
      h("div", { className: "bta-markers-note" },
        `Looked at ${st.looked} frames (one every ${Math.round(st.step)} s). A marker where the action starts and at each big shift, ` +
        `running until the next. Each is named by its time and the scene's title, and tagged "${tagName}". Tap a picture to watch from there.`),
      st.addError && h("div", { className: "bta-markers-error" }, st.addError),
      h("div", { className: "bta-markers-grid" },
        ms.map((m) => h("div", { key: m.start, className: "bta-markers-card" },
          h("button", { type: "button", className: "bta-markers-pic", title: "Watch from here", onClick: () => play(m.start) },
            m.picture ? h("img", { src: "data:image/jpeg;base64," + m.picture, alt: "" }) : h("span", null, "▶"),
            h("span", { className: "bta-markers-at" }, "▶ " + clock(m.start))),
          h("div", { className: "bta-markers-line" },
            h("span", { className: "bta-markers-span" }, `${clock(m.start)} – ${clock(m.end)}`),
            added(m) ? h("span", { className: "bta-markers-done" }, "✓ Added")
              : h("button", { type: "button", className: "btn btn-primary btn-sm", disabled: adding, onClick: () => add([m]) },
                busy[m.start] ? "Adding…" : "+ Add"))))));
  }


  // ---- Scene page → Markers → Create Marker: a quicker way ---------------
  // Start = where the video is, end = one tap on a suggestion (the next
  // marker, the next big changes the AI saw, +1 min, the end), the name filled
  // in, and the marker it falls inside ends where it starts. Stash's own form
  // is a tap away.
  const parseClock = (txt) => {
    const parts = String(txt || "").trim().split(":").map((x) => Number(x));
    if (!parts.length || parts.some((x) => !Number.isFinite(x) || x < 0)) return null;
    return parts.reduce((a, x) => a * 60 + x, 0);
  };
  const playerVideo = () => document.querySelector(".scene-player-container video, .video-js video");
  let quickBypass = false; // (to open Stash's own form)

  function QuickMarker({ sceneId }) {
    const [open, setOpen] = React.useState(false);
    const [start, setStart] = React.useState(0);
    const [startTxt, setStartTxt] = React.useState("0:00");
    const [end, setEnd] = React.useState(null);
    const [endTxt, setEndTxt] = React.useState("");
    const [name, setName] = React.useState("");
    const [named, setNamed] = React.useState(false); // you typed a name: keep it
    const [trim, setTrim] = React.useState(true);
    const [info, setInfo] = React.useState(null);
    const [busy, setBusy] = React.useState(false);
    const [done, setDone] = React.useState(null); // {undo, start, end}
    const [err, setErr] = React.useState("");
    React.useEffect(() => {
      const go = () => {
        const v = playerVideo();
        const t = Math.max(0, Math.round(v ? v.currentTime : 0));
        if (v && !v.paused) v.pause();
        setStart(t); setStartTxt(clock(t)); setEnd(null); setEndTxt(""); setNamed(false); setTrim(true);
        setDone(null); setErr(""); setOpen(true);
      };
      window.addEventListener("bta-quick-marker", go);
      return () => window.removeEventListener("bta-quick-marker", go);
    }, []);
    React.useEffect(() => { setOpen(false); }, [sceneId]);
    // suggestions for this start
    React.useEffect(() => {
      if (!open) return undefined;
      let live = true;
      const t = setTimeout(() => {
        ask({ mode: "marker_suggest", scene: sceneId, start }).then((out) => {
          if (!live) return;
          setInfo(out);
          if (!named) setName(out.name);
          setEnd((cur) => {
            if (cur != null && cur > start) return cur;
            const e = out.next ? out.next.seconds : (out.changes[0] ? out.changes[0].at : Math.min(out.duration, start + 60));
            setEndTxt(clock(e));
            return e;
          });
        }).catch((e) => live && setErr(e.message));
      }, 250);
      return () => { live = false; clearTimeout(t); };
    }, [open, start]);
    if (!open && !done) return null;
    const setS = (t) => {
      t = Math.max(0, Math.min(Math.round(t), Math.floor((info && info.duration) || t)));
      setStart(t); setStartTxt(clock(t));
      const v = playerVideo();
      if (v) v.currentTime = t;
    };
    const pickEnd = (t) => { setEnd(t); setEndTxt(clock(t)); };
    async function save() {
      setBusy(true); setErr("");
      try {
        const undo = await ask({ mode: "marker_add_manual", scene: sceneId, start, end, title: name, trim_prev: !!(trim && info && info.prev) });
        setDone({ undo, start, end }); setOpen(false);
        try { await api.utils.StashService.getClient().refetchQueries({ include: "active" }); } catch (e) { /* shows after a reload */ }
      } catch (e) {
        setErr(e.message);
      }
      setBusy(false);
    }
    async function undo() {
      setBusy(true);
      try {
        await ask({ mode: "marker_add_undo", undo: done.undo });
        setDone(null);
        try { await api.utils.StashService.getClient().refetchQueries({ include: "active" }); } catch (e) { /* fine */ }
      } catch (e) { setErr(e.message); }
      setBusy(false);
    }
    if (done) {
      return h("div", { className: "bta-quick" },
        h("div", { className: "bta-quick-row" },
          h("span", { className: "bta-markers-done" }, `✓ Marker added at ${clock(done.start)}` + (done.end != null ? `–${clock(done.end)}` : "")),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", disabled: busy, onClick: undo }, "↶ Undo"),
          h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => setDone(null) }, "Close")),
        err && h("div", { className: "bta-markers-error" }, err));
    }
    const chips = [];
    if (info) {
      if (info.next) chips.push({ t: info.next.seconds, label: `Next marker ${clock(info.next.seconds)}` });
      info.changes.forEach((c) => chips.push({ t: c.at, label: `Change at ${clock(c.at)}`, hint: c.strength >= 1 ? "big change" : "smaller change" }));
      if (start + 60 < info.duration) chips.push({ t: start + 60, label: "+1 min" });
      chips.push({ t: Math.floor(info.duration), label: `End of scene ${clock(info.duration)}` });
    }
    return h("div", { className: "bta-quick" },
      h("div", { className: "bta-quick-head" },
        h("b", null, "New marker"),
        h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => {
          setOpen(false); quickBypass = true;
          const b = document.querySelector(".scene-markers-panel > button.btn-primary");
          if (b) b.click();
          quickBypass = false;
        } }, "Use Stash's form instead")),
      h("label", { className: "bta-quick-label" }, "Start"),
      h("div", { className: "bta-quick-row" },
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setS(start - 5) }, "−5s"),
        h("input", { className: "form-control bta-quick-time", value: startTxt, inputMode: "numeric",
          onChange: (e) => { setStartTxt(e.target.value); const t = parseClock(e.target.value); if (t != null) setStart(Math.round(t)); } }),
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setS(start + 5) }, "+5s"),
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { const v = playerVideo(); if (v) setS(v.currentTime); } }, "Use the video's time")),
      h("label", { className: "bta-quick-label" }, "End"),
      h("div", { className: "bta-quick-row" },
        chips.map((c) => h("button", { key: c.label, type: "button", title: c.hint || "",
          className: "btn btn-sm " + (end === c.t ? "btn-primary" : "btn-secondary"), onClick: () => pickEnd(c.t) }, c.label)),
        h("input", { className: "form-control bta-quick-time", value: endTxt, placeholder: "m:ss",
          onChange: (e) => { setEndTxt(e.target.value); const t = parseClock(e.target.value); setEnd(t == null ? null : Math.round(t)); } })),
      h("label", { className: "bta-quick-label" }, "Name"),
      h("input", { className: "form-control bta-quick-name", value: name, onChange: (e) => { setName(e.target.value); setNamed(true); } }),
      info && info.prev && h("label", { className: "bta-quick-check" },
        h("input", { type: "checkbox", checked: trim, onChange: (e) => setTrim(e.target.checked) }),
        h("span", null, " End \"", h("span", { className: "bta-quick-prev" }, info.prev.title),
          `" at ${clock(start)}` + (info.prev.end != null ? ` (it runs to ${clock(info.prev.end)} now)` : ""))),
      info && h("div", { className: "bta-markers-note" }, `Tagged ${info.tag}, with a picture from ${clock(start)}.`),
      err && h("div", { className: "bta-markers-error" }, err),
      h("div", { className: "bta-quick-row" },
        h("button", { type: "button", className: "btn btn-primary", disabled: busy || (end != null && end <= start),
          onClick: save }, busy ? "Adding…" : "Add marker"),
        h("button", { type: "button", className: "btn btn-secondary", onClick: () => setOpen(false) }, "Cancel"),
        end != null && end <= start && h("span", { className: "bta-markers-error" }, "The end has to be after the start")));
  }

  // Create Marker opens the quick panel (unless you asked for Stash's form)
  document.addEventListener("click", (e) => {
    if (quickBypass) return;
    const b = e.target instanceof Element && e.target.closest(".scene-markers-panel > button.btn-primary");
    if (!b || !/Create Marker/i.test(b.textContent || "")) return;
    if (!document.querySelector(".bta-quick-host")) return;
    e.preventDefault();
    e.stopPropagation();
    window.dispatchEvent(new Event("bta-quick-marker"));
  }, true);


  // ---- Gallery page → Chapters → Suggest chapters ---------------------------
  // Where the gallery changes (folder, file names, look, performers, studio,
  // tags, date), with a title for each from what that part has; you choose
  // which to add.
  // A chapter's images: the chapter is a run of images (it lasts until the
  // next one starts), so ticking one before or after it grows it, and
  // unticking one trims it from that end (images after go to the next chapter)
  function ChapterPicker({ list, k, imgs, onDone, onClose }) {
    const n = imgs.length;
    const c = list[k];
    const end0 = list[k + 1] ? list[k + 1].index - 1 : n;
    const minStart = k > 0 ? list[k - 1].index + 1 : 1;
    const maxEnd = list[k + 1] ? (list[k + 2] ? list[k + 2].index - 2 : n - 1) : n;
    const [s, setS] = React.useState(c.index);
    const [e, setE] = React.useState(end0);
    const lo = Math.max(minStart, c.index - 8), hi = Math.min(maxEnd, end0 + 8);
    const last = !list[k + 1]; // (the last chapter runs to the end of the gallery)
    function tap(p) {
      if (p < s) setS(p);
      else if (p > e) { if (!last) setE(p); }
      else if (p - s <= e - p || last) { if (p + 1 <= e) setS(p + 1); }
      else if (p - 1 >= s) setE(p - 1);
    }
    const pages = [];
    for (let p = lo; p <= hi; p++) pages.push(p);
    return h("div", { className: "bta-chpick-back", onClick: onClose },
      h("div", { className: "bta-chpick", onClick: (ev) => ev.stopPropagation() },
        h("div", { className: "bta-chpick-head" },
          h("b", null, c.title || "Chapter"),
          h("span", { className: "bta-chpick-count" }, ` · images ${s}–${e} (${e - s + 1})`),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { setS(lo); if (!last) setE(hi); } }, "Select all"),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => onDone(null) }, "✕ Not a chapter"),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => { setS(c.index); setE(end0); } }, "Reset"),
          h("button", { type: "button", className: "btn btn-primary btn-sm", onClick: () => onDone({ start: s, end: e }) }, "Done")),
        h("div", { className: "bta-markers-note" },
          "Tap an image before or after the chapter to add it; tap one inside to trim the chapter from that end" +
          (last ? " (it's the last chapter: it runs to the end)." : " (the rest go to the next chapter).") +
          " Not a chapter: its images join the chapter before."),
        h("div", { className: "bta-chpick-grid" },
          pages.map((p) => h("button", { key: p, type: "button", className: "bta-chpick-tile" + (p >= s && p <= e ? " on" : ""), onClick: () => tap(p) },
            h("img", { src: `/image/${imgs[p - 1]}/thumbnail`, alt: "", loading: "lazy" }),
            h("span", { className: "bta-chpick-num" }, p),
            p >= s && p <= e && h("span", { className: "bta-chpick-tick" }, "✓"))))));
  }

  // ---- Gallery page → Chapters → Suggest chapters ---------------------------
  // Where the gallery changes (folder, file names, look, performers, studio,
  // tags, date - or a comic's chapter headings), with a title for each; you
  // choose which to add. Added ones are put away.
  function GalleryChapters({ galleryId }) {
    const [st, setSt] = React.useState({ state: "idle" });
    const [list, setList] = React.useState([]); // [{key, index, title, why}]
    const [have, setHave] = React.useState([]);
    const [busy, setBusy] = React.useState(false);
    const addingRef = React.useRef(false);
    const [err, setErr] = React.useState("");
    const [pick, setPick] = React.useState(null); // the chapter whose images are shown
    const [showAdded, setShowAdded] = React.useState(false);
    React.useEffect(() => { setSt({ state: "idle" }); setList([]); setPick(null); }, [galleryId]);
    async function look() {
      setSt({ state: "loading" }); setErr(""); setShowAdded(false);
      try {
        let out = await ask({ mode: "gallery_chapters", id: galleryId });
        // a comic: its pages are read a few hundred at a time
        while (out && out.unread) {
          setSt({ state: "loading", reading: out.reading, count: out.count });
          out = await ask({ mode: "gallery_chapters", id: galleryId });
        }
        setHave(out.have || []);
        setList((out.chapters || []).map((c) => ({ key: c.index, index: c.index, title: c.title, why: c.why || [] })));
        setSt({ state: "done", count: out.count, comic: out.comic, no_reader: out.no_reader, images: out.images || [] });
      } catch (e) {
        setSt({ state: "error", message: e.message });
      }
    }
    const imgs = st.images || [];
    const added = (c) => have.some((x) => x.image_index === c.index);
    async function add(cs) {
      if (addingRef.current) return; // (one add at a time - see AI markers)
      addingRef.current = true;
      setBusy(true); setErr("");
      try {
        for (const c of cs.filter((x) => !added(x))) {
          const d = await gql("mutation($i: GalleryChapterCreateInput!) { galleryChapterCreate(input: $i) { id title image_index } }",
            { i: { gallery_id: galleryId, title: (c.title || "").trim() || `Chapter at image ${c.index}`, image_index: c.index } });
          setHave((hv) => [...hv, d.galleryChapterCreate]);
        }
        try { await api.utils.StashService.getClient().refetchQueries({ include: "active" }); } catch (e) { /* shows after a reload */ }
      } catch (e) {
        setErr(e.message);
      }
      setBusy(false);
      addingRef.current = false;
    }
    function edit(k, change) {
      setList((l) => {
        const next = l.map((x) => ({ ...x }));
        if (!change) { next.splice(k, 1); return next; } // not a chapter: its images join the one before
        next[k].index = change.start;
        if (next[k + 1]) next[k + 1].index = change.end + 1;
        return next;
      });
      setPick(null);
    }
    if (st.state === "idle") {
      return h("div", { className: "bta-markers" },
        h("button", { type: "button", className: "btn btn-secondary bta-markers-go", onClick: look }, "✨ Suggest chapters"),
        h("span", { className: "bta-markers-note" }, "Finds where the gallery changes (folder, file names, look, performers, tags, a comic's chapter headings...) and suggests chapters; you choose which to add."));
    }
    if (st.state === "loading") {
      return h("div", { className: "bta-markers" }, h("div", { className: "bta-markers-note" },
        st.count ? `Reading the comic's pages for chapter headings… ${st.reading} of ${st.count}` : "Looking through the gallery…"));
    }
    if (st.state === "error") {
      return h("div", { className: "bta-markers" },
        h("div", { className: "bta-markers-error" }, "Couldn't look at it: " + st.message),
        h("button", { type: "button", className: "btn btn-secondary", onClick: look }, "Try again"));
    }
    const left = list.filter((c) => !added(c));
    const shown = showAdded ? list : left;
    const sizeOf = (c) => {
      const k = list.indexOf(c);
      return (list[k + 1] ? list[k + 1].index : imgs.length + 1) - c.index;
    };
    // all added: put away
    if (list.length && !left.length && !showAdded) {
      return h("div", { className: "bta-markers" },
        h("div", { className: "bta-markers-head" },
          h("span", { className: "bta-markers-done" }, `✓ All ${list.length} chapters added`),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setShowAdded(true) }, "Show them"),
          h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: look }, "↻ Look again")));
    }
    return h("div", { className: "bta-markers" },
      h("div", { className: "bta-markers-head" },
        h("b", null, list.length ? `Suggested chapters (${left.length} to add${list.length > left.length ? `, ${list.length - left.length} added` : ""})` : "No chapters to suggest"),
        left.length > 1 && h("button", { type: "button", className: "btn btn-primary btn-sm", disabled: busy, onClick: () => add(left) }, `+ Add all ${left.length}`),
        list.length > left.length && h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setShowAdded((v) => !v) },
          showAdded ? "Hide added ones" : "Show added ones"),
        h("button", { type: "button", className: "btn btn-secondary btn-sm", disabled: busy, onClick: look }, "↻ Look again")),
      h("div", { className: "bta-markers-note" },
        (st.comic ? `${st.count} pages. Chapters from the chapter headings printed on the pages.`
          : list.length ? `${st.count} images. Each chapter starts where the gallery changes.`
          : `${st.count} images, and nothing changes enough to split them.`) +
        (list.length ? " Tap a chapter's picture to choose its images; edit a title before adding it." : "")),
      st.no_reader && h("div", { className: "bta-markers-note" },
        "This looks like a comic: with the face helper's text reader (on a Mac only: run the add-on's face-helper/setup.sh again), chapters come from the chapter headings printed on its pages."),
      err && h("div", { className: "bta-markers-error" }, err),
      h("div", { className: "bta-chapters" },
        shown.map((c) => {
          const k = list.indexOf(c);
          const size = sizeOf(c);
          return h("div", { key: c.key, className: "bta-chapter" },
            h("button", { type: "button", className: "bta-chapter-pic", title: "Choose its images", disabled: added(c), onClick: () => setPick(k) },
              h("img", { src: `/image/${imgs[c.index - 1]}/thumbnail`, alt: "", loading: "lazy" })),
            h("div", { className: "bta-chapter-body" },
              h("button", { type: "button", className: "bta-chapter-at", disabled: added(c), onClick: () => setPick(k) },
                `Image ${c.index} · ${size} image${size === 1 ? "" : "s"}` + (added(c) ? "" : " ▸")),
              h("input", { className: "form-control form-control-sm bta-chapter-title", value: c.title, disabled: added(c),
                onChange: (e) => { const v = e.target.value; setList((l) => l.map((x) => (x.key === c.key ? { ...x, title: v } : x))); } }),
              h("div", { className: "bta-chapter-why" }, c.why.join(" · "))),
            added(c) ? h("span", { className: "bta-markers-done" }, "✓ Added")
              : h("button", { type: "button", className: "btn btn-primary btn-sm", disabled: busy, onClick: () => add([c]) }, "+ Add"));
        })),
      pick != null && list[pick] && h(ChapterPicker, { key: list[pick].key, list, k: pick, imgs, onClose: () => setPick(null),
        onDone: (change) => edit(pick, change) }));
  }

  function checkChaptersPanel() {
    const m = location.pathname.match(/^\/galleries\/(\d+)/);
    if (!m) return;
    // (in any tab: switching tabs only shows another one, nothing new is added)
    const btn = [...document.querySelectorAll(".tab-pane button.btn-primary")].find((b) => /Create Chapter/i.test(b.textContent || ""));
    if (!btn) return;
    let host = btn.parentElement.querySelector(":scope > .bta-chapters-host");
    if (host && host.dataset.gallery === m[1]) return;
    if (!host) {
      host = document.createElement("div");
      host.className = "bta-chapters-host";
      btn.after(host);
    }
    host.dataset.gallery = m[1];
    api.ReactDOM.render(h(GalleryChapters, { galleryId: m[1] }), host);
  }
  // (at most every 150 ms while the page keeps changing - some pages never
  // stop, e.g. another plugin rewriting a title - rather than waiting for quiet)
  const soon = (fn, ms = 150) => { if (!fn.t) fn.t = setTimeout(() => { fn.t = null; fn(); }, ms); };
  new MutationObserver(() => soon(checkChaptersPanel)).observe(document.body, { childList: true, subtree: true });

  function checkMarkersPanel() {
    const m = location.pathname.match(/^\/scenes\/(\d+)/);
    const panel = m && document.querySelector(".scene-markers-panel");
    if (!panel) return;
    let host = panel.querySelector(":scope > .bta-markers-host");
    if (host && host.dataset.scene === m[1]) return;
    if (!host) {
      host = document.createElement("div");
      host.className = "bta-markers-host";
      const btn = panel.querySelector(":scope > button");
      if (btn) btn.after(host); else panel.prepend(host);
    }
    host.dataset.scene = m[1];
    api.ReactDOM.render(h(Gate, { need: "markers" }, h(AiMarkers, { sceneId: m[1] })), host);
    let quick = panel.querySelector(":scope > .bta-quick-host");
    if (!quick) {
      quick = document.createElement("div");
      quick.className = "bta-quick-host";
      host.before(quick);
    }
    api.ReactDOM.render(h(QuickMarker, { sceneId: m[1] }), quick);
  }
  new MutationObserver(() => soon(checkMarkersPanel)).observe(document.body, { childList: true, subtree: true });
})();
