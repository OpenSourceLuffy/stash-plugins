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
    if (json.errors) throw new Error(json.errors[0].message);
    return json.data;
  }

  function ask(args) {
    return gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", {
      p: PLUGIN_ID,
      a: args,
    }).then((d) => d.runPluginOperation);
  }

  // ---- "Ask AI for tags" under the Tags box ---------------------------------
  const ITEM_PAGE = /^\/(scenes|images)\/(\d+)/;

  function lookList(out) {
    return (out || []).map((t) => ({ id: String(t.id), name: t.name, aliases: [], score: t.score, sure: t.sure }));
  }

  function AiTags({ values, onSelect }) {
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
    return h(
      "div",
      { className: "pts-suggest pts-ai bta-tags" },
      h("span", { className: "pts-label" }, "AI"),
      ai.state === "idle" &&
        h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: go, title: "Ask the local AI model (Ollama) which of your tags fit, checked against look-alikes" }, "Ask AI for tags"),
      look.state === "idle" &&
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

  api.patch.after("TagSelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    // the edit form's Tags box (not filters, and not Better Organizing Tools'
    // own bulk windows)
    if (!props.isMulti || !props.onSelect || props.isDisabled || props.ptsNoSuggest) return result;
    if (!ITEM_PAGE.test(location.pathname)) return result;
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
    return h(React.Fragment, null, result, h(LookGalleries, { values: props.values, onSelect: props.onSelect }));
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
    return h(React.Fragment, null, result, h(FaceRow, { values: props.values, onSelect: props.onSelect }));
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
    if (!m) return;
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
    if (!m) return;
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

  // ---- Settings → Tasks: this plugin's tasks in sections --------------------
  // Same idea as Better Organizing Tools: a heading above each group of tasks
  // that shows / hides them (closed to start with; remembered per browser).
  const TASK_GROUPS = [
    ["ai-night", "Tonight's run", "The AI work saved for the night time, started now",
      ["AI: run tonight's new items now"]],
    ["ai-tags", "AI tagging", "Tag scenes and images with the local AI model",
      ["AI tagging: preview on 10 items (no changes)", "AI tagging: items with no tags", "AI tagging: everything not done yet"]],
    ["ai-looks", "Look-alike tags", "Tags learned from the items you've tagged that look most alike",
      ["Look-alike tags: scan scenes and images", "Look-alike tags: preview on 10 items (no changes)",
       "Look-alike tags: items with no tags"]],
    ["ai-pictures", "Tag pictures", "Each tag's most typical scene or photo as its picture",
      ["Tag pictures: preview the most typical ones (no changes)", "Tag pictures: most typical one for tags without a picture",
       "Tag pictures: most typical one for every tag (replaces existing ones)"]],
    ["ai-body", "Body tags", "What's shown (Tits, Ass, Pussy, Cock) and sizes from the performers",
      ["Body tags: preview on 10 items (no changes)", "Body tags: scenes and images", "Body tags: check everything again"]],
    ["ai-titles", "AI titles", "Titles for scenes and images without one",
      ["AI titles: preview on 10 items (no changes)", "AI titles: scenes without a title", "AI titles: images without a title"]],
    ["ai-studios", "Studios from accounts", "Studio and performers from the accounts and studio watermarks a scene or photo shows",
      ["Studios from accounts: preview on 10 scenes (no changes)", "Studios from accounts: also read watermarks with the AI",
       "Studios from watermarks: preview on 10 photos without a studio (no changes)", "Studios from watermarks: photos without a studio, not in a gallery",
       "Studios from watermarks: photos without a studio, galleries too"]],
    ["ai-faces", "Faces", "Who the people in scenes and images look like, from your performers' pictures",
      ["Faces: learn performers' faces", "Faces: scan scenes and images", "Faces: learn from what you've tagged",
       "Faces: find performers who may be the same person", "Faces: preview on 10 items (no changes)",
       "Faces: scenes without performers", "Faces: images without performers"]],
    ["ai-perf", "Performer details", "Hair and eye colour from performers' pictures",
      ["Performer details: preview on 10 performers (no changes)", "Performer details: fill in empty hair and eye colour"]],
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

  new MutationObserver(() => {
    clearTimeout(titleTimer);
    titleTimer = setTimeout(() => {
      checkTitles();
      checkPerformerForm();
      checkPerformerPage();
      groupTasks();
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
        if (few) notes.push(`${few} ${few === 1 ? "is" : "are"} on too few scanned items (run "Look-alike tags: scan scenes and images")`);
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
      load: (only, skip) => ask({ mode: "face_review", performer: only || null, limit: 40, skip }),  // (never kept)
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
      undoYes: (it) => setOnItem(it, "tag_ids", it.tag, "REMOVE"),
      undoNo: (it) => ask({ mode: "tag_review_answer", key: `${it.kind}:${it.id}`, tag: it.tag, no: false }),
      avatar: (it) => `/tag/${it.tag}/image`,
      page: (it) => `/tags/${it.tag}`,
      question: (it) => ["Tag it ", h("b", null, it.name), "?"],
      meta: (it, pct) => `${pct(it.score)} sure · ${it.near} of its 20 closest look-alikes have it`,
      extra: (it) => ` · has ${it.tags} tag${it.tags === 1 ? "" : "s"}`,
      noLabel: "✕ No", yesLabel: "✓ Yes, add", noStamp: "NO",
      empty: "No confident tags left. New ones show up after \"Look-alike tags: scan scenes and images\" and as you tag more.",
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
      no: (it) => setOnItem(it, CHECK_FIELD[it.what], it.target, "REMOVE"),
      undoYes: (it) => ask({ mode: "check_answer", key: `${it.kind}:${it.id}`, what: `${it.what}:${it.target}`, keep: false }),
      undoNo: (it) => setOnItem(it, CHECK_FIELD[it.what], it.target, "ADD"),
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
    studios_add: { ...REVIEW_MODES.studios, base: "studios", label: "Suggested", card: true, studioKinds: ["join"],
      empty: "No scenes or photos to add to a studio you have. New ones show up as galleries and folders get studios." },
    studios_check: { ...REVIEW_MODES.studios, base: "studios", label: "Check", card: true, studioKinds: ["merge", "delete"],
      empty: "No studios to merge or delete." },
    studios_new: { ...REVIEW_MODES.studios, base: "studios", label: "New", card: true, studioKinds: ["add"],
      empty: "No account names waiting. New ones show up after \"Studios from watermarks\" runs." },
    galleries_add: { ...REVIEW_MODES.galleries, base: "galleries", label: "Suggested", card: true, galleryKinds: ["gjoin", "gdetails"],
      empty: "No photos or details to add to a gallery. Look-alike suggestions need \"Look-alike tags: scan scenes and images\"." },
    galleries_check: { ...REVIEW_MODES.check, base: "check", label: "Check", checkOnly: "g",
      empty: "No photo looks out of place in its gallery." },
    galleries_new: { ...REVIEW_MODES.galleries, base: "galleries", label: "New", card: true, galleryKinds: ["gnew"],
      empty: "No folders of photos without a gallery." },
  };
  const REVIEW_GROUPS = [
    { key: "faces", label: "Faces", actions: ["faces", "faces_check", "new"] },
    { key: "tags", label: "Tags", actions: ["tags", "tags_check"] },
    { key: "studios", label: "Studios", actions: ["studios_add", "studios_check", "studios_new"] },
    { key: "galleries", label: "Galleries", actions: ["galleries_add", "galleries_check", "galleries_new"] },
  ];
  const OLD_MODES = { check: "tags_check", studios: "studios_check" }; // links from before
  const groupOf = (k) => REVIEW_GROUPS.find((g) => g.actions.includes(k)) || REVIEW_GROUPS[0];
  // the action last used in each tab, so tapping Faces / Tags / Studios goes back to it
  const lastAction = (g) => {
    try { const k = localStorage.getItem("bta-review-" + g.key); if (g.actions.includes(k)) return k; } catch (e) { /* fine */ }
    return g.actions[0];
  };

  async function setOnKeys(keys, field, id, mode) {
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
    const [mutation, type] = it.kind === "scene" ? ["bulkSceneUpdate", "BulkSceneUpdateInput"] : ["bulkImageUpdate", "BulkImageUpdateInput"];
    await gql(`mutation($i: ${type}!) { ${mutation}(input: $i) { id } }`, { i: { ids: [it.id], [field]: { ids: [id], mode } } });
  }

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
            : it.gallery ? `${it.gallery.name}${it.type === "gjoin" ? "?" : ""}` : (it._name || it.name || "New performer"),
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

  function FaceReview() {
    const params = new URLSearchParams(window.location.search);
    const asked = OLD_MODES[params.get("mode")] || params.get("mode");
    const tabKey = window.location.pathname === REVIEW_PATH ? "faces" : (ACTIONS[asked] ? asked : "faces");
    const M = ACTIONS[tabKey];
    const tab = M.base; // what kind of cards (faces, tags, check, new, studios)
    REVIEW_MODES._checkOnly = M.checkOnly || null;
    REVIEW_MODES._studioKinds = M.studioKinds || null;
    REVIEW_MODES._galleryKinds = M.galleryKinds || null;
    const grp = groupOf(tabKey);
    React.useEffect(() => { try { localStorage.setItem("bta-review-" + grp.key, tabKey); } catch (e) { /* fine */ } }, [tabKey]);
    const only = params.get(M.param) || "";
    const [queue, setQueue] = React.useState([]);
    const [state, setState] = React.useState({ loading: true });
    const [total, setTotal] = React.useState(0);
    const [counts, setCounts] = React.useState({ yes: 0, no: 0, skip: 0 });
    const [undo, setUndo] = React.useState([]);
    const [drag, setDrag] = React.useState(null); // { dx } while swiping
    const [busy, setBusy] = React.useState(false);
    const [zoom, setZoom] = React.useState(null);
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
        // r.total counts what's left apart from the ones already shown
        setTotal(Math.max(0, (r.total || 0) - items.length));
        setState({ loading: false });
      } catch (e) {
        setState({ loading: false, error: e.message });
      } finally {
        loadingMore.current = false;
      }
    }, [only, tabKey]);

    React.useEffect(() => {
      const mem = reviewMemory;
      if (mem && mem.tab === tabKey && mem.only === only && Date.now() - mem.at < 30 * 60 * 1000) {
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
    }, [only, tabKey]);

    // remember where you are (for coming back from a performer's / tag's page)
    React.useEffect(() => {
      if (state.loading) return;
      reviewMemory = { tab: tabKey, only, queue, counts, undo, total, seen: [...seen.current], at: Date.now() };
    }, [queue, counts, undo, total, state.loading]);

    // (for a moment after switching tabs the old tab's cards are still here)
    const shown = queue.filter((it) => (tab === "tags" ? it.tag != null : tab === "new" ? it.sig != null && !it.st && !it.gal
      : tab === "studios" ? !!it.st : tab === "galleries" ? !!it.gal
      : tab === "check" ? it.what != null : it.kind != null && it.performer != null));
    const cur = shown[0];
    const next = shown[1];
    React.useEffect(() => {
      if (!state.loading && queue.length < 6 && total > 0) load(false);
    }, [queue.length]);

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
    const batchLoad = (gid, fresh) => (tab === "faces"
      ? ask({ mode: "face_review", performer: gid, limit: 400 })
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
    }, [tabKey, only]);

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
        setZoom(cur);
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
                    it.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶ Scene"),
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
              h("button", { type: "button", className: "bta-fr-tile-pic", title: "See it big", onClick: () => setZoom(it) },
                h("img", { src: it.picture, alt: "", loading: "lazy", draggable: false }),
                it.kind === "scene" && h("span", { className: "bta-fr-tile-kind" }, "▶ Scene")),
              h("button", { type: "button", className: "bta-fr-tile-who", title: `See ${it.name}'s page`,
                onClick: () => { reviewReturn = M.page(it); goTo(M.page(it)); } },
                h("img", { className: tab === "tags" || (tab === "check" && it.what !== "p") ? "bta-fr-avatar-tag" : "", src: M.avatar(it), alt: "", loading: "lazy" }),
                h("span", { className: "bta-fr-tile-name" }, it.name)),
              h("div", { className: "bta-fr-tile-meta" },
                tab === "faces"
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
            h("img", { src: cur.picture, alt: "", draggable: false, key: keyOf(cur) }),
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
                h("a", { href: `/${cur.kind}s/${cur.id}`, target: "_blank", rel: "noopener", onPointerDown: (e) => e.stopPropagation() },
                  (cur.kind === "scene" ? "Scene" : "Photo") + ": " + (cur.title || cur.id) + " ↗"),
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
            REVIEW_GROUPS.map((g) =>
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
          only && h("button", { type: "button", className: "bta-fr-chip", onClick: () => goTo(`${REVIEW_PAGE}?mode=${tabKey}`) },
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
        ...(allOf.st || (allOf.gal && allOf.type !== "gdetails") ? {
          doLabel: (n) => (allOf.type === "add" || allOf.type === "gnew" ? `✓ Create · ${n}` : `✓ Add ${n}`),
          onDo: () => { const one = allOf; setAllOf(null); answer("yes", one); },
        } : {}),
      }),
      zoom && h(ZoomView, {
        it: zoom, onClose: () => setZoom(null), list: zoom._list,
        onNav: (x) => setZoom((z) => ({ ...x, name: z.name, _list: z._list, _ctx: z._ctx, _sig: z._sig })),
        pick: zoom._ctx === "batch"
          ? { off: batchOff.has(keyOf(zoom)), check: tab === "check",
              toggle: () => setBatchOff((s0) => { const k = keyOf(zoom); const n0 = new Set(s0); if (n0.has(k)) n0.delete(k); else n0.add(k); return n0; }) }
          : zoom._ctx === "all"
          ? { off: (leftOut[zoom._sig] || new Set()).has(zoom.key),
              toggle: () => setLeftOut((lo) => { const n0 = new Set(lo[zoom._sig] || []); if (n0.has(zoom.key)) n0.delete(zoom.key); else n0.add(zoom.key); return { ...lo, [zoom._sig]: n0 }; }) }
          : null,
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

  if (api.register && api.register.route) {
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
      return React.cloneElement(result, {}, ...React.Children.toArray(result.props.children), item);
    });
  }

  api.patch.before("FilteredTagList", function (...args) {
    const props = args[0] || {};
    const op = {
      text: "Most typical picture (AI)",
      isDisplayed: (result, filter, selectedIds) => !!selectedIds && selectedIds.size > 0,
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
})();
