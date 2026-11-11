// Better Tagging AI - the page side: an "AI" row under the Tags box, a
// "Faces" row under the Performers box and an "AI titles" row under the
// Title box when editing a scene or image (titles also on galleries), and
// "Ask AI for details" when editing a performer, asking the local AI model (Ollama) through this
// plugin. Works with or without Better Tagging Tools; if that's installed,
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
        setAi({ state: "done", tags: (out || []).map((t) => ({ id: String(t.id), name: t.name, aliases: [], source: t.source || "ai" })) });
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
    };
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
      (ai.state === "done" || look.state === "done") && !busy(ai) && !busy(look) && !all.length &&
        h("span", { className: "pts-ai-note" }, (ai.tags || []).length + (look.tags || []).length ? "All suggestions are already added" : "No suggestions"),
      aiTags.map((t) =>
        h("button", { key: "a" + t.id, type: "button", className: "pts-chip" + (t.source !== "ai" ? " bta-sure" : ""), title: SOURCE[t.source] || SOURCE.ai, onClick: () => add([t]) },
          "+ " + t.name + (t.source !== "ai" ? " ✓" : ""))
      ),
      lookTags.map((t) =>
        h("button", { key: "l" + t.id, type: "button", className: "pts-chip" + (t.sure ? " bta-sure" : ""),
          title: Math.round(t.score * 100) + "% of the closest look-alikes you've tagged have it" + (t.sure ? " (sure)" : ""), onClick: () => add([t]) },
          "+ " + t.name + " " + Math.round(t.score * 100) + "%" + (t.sure ? " ✓" : ""))
      ),
      all.length > 1 && h("button", { type: "button", className: "pts-chip pts-all", onClick: () => add(all) }, "Add all")
    );
  }

  api.patch.after("TagSelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    // the edit form's Tags box (not filters, and not Better Tagging Tools'
    // own bulk windows)
    if (!props.isMulti || !props.onSelect || props.isDisabled || props.ptsNoSuggest) return result;
    if (!ITEM_PAGE.test(location.pathname)) return result;
    return h(React.Fragment, null, result, h(AiTags, { values: props.values, onSelect: props.onSelect }));
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
    head.append(label, btn, note, close);
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

  // Right under the Title box, or under Better Tagging Tools' "Title ideas"
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
  // Same idea as Better Tagging Tools: a heading above each group of tasks
  // that shows / hides them (closed to start with; remembered per browser).
  const TASK_GROUPS = [
    ["ai-tags", "AI tagging", "Tag scenes and images with the local AI model",
      ["AI tagging: preview on 10 items (no changes)", "AI tagging: items with no tags", "AI tagging: everything not done yet"]],
    ["ai-titles", "AI titles", "Titles for scenes and images without one",
      ["AI titles: preview on 10 items (no changes)", "AI titles: scenes without a title", "AI titles: images without a title"]],
    ["ai-studios", "Studios from accounts", "Studio and performers from the accounts and studio watermarks a scene shows",
      ["Studios from accounts: preview on 10 scenes (no changes)", "Studios from accounts: also read watermarks with the AI"]],
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
})();
