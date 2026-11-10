// Better Tagging Tools - shows suggested tags under tag boxes: the
// top tags of the performers picked in the same form (the "Tag performers
// with their top tags" task fills those in), the tags most used in the picked
// galleries, or the library's most used image/scene tags. Also adds the bulk
// tag windows on performer and gallery pages.
(function () {
  "use strict";

  const PLUGIN_ID = "BetterTaggingTools";
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

  let maxSuggestions = 15;
  gql("{ configuration { plugins } }")
    .then((d) => {
      const s = (d.configuration.plugins || {})[PLUGIN_ID] || {};
      const n = Number(s.maxSuggestions);
      if (n > 0) maxSuggestions = n;
    })
    .catch(() => {});

  // ---- what's picked in the form right now ---------------------------------
  // Every mounted performer box and gallery box reports its picks here; tag
  // boxes listen.
  const picks = { performers: new Map(), galleries: new Map() }; // type -> (reporter key -> [{id, name}])
  const listeners = new Set();
  const notify = () => listeners.forEach((fn) => fn());

  function picked(type) {
    const byId = new Map();
    picks[type].forEach((list) => list.forEach((p) => byId.set(String(p.id), p)));
    return [...byId.values()];
  }

  // A performer's name, or a gallery's title / zip name / folder name
  function itemName(x) {
    const base = (path) => (path ? path.split(/[\\/]/).filter(Boolean).pop() || "" : "");
    const file = x.files && x.files[0] && (x.files[0].basename || base(x.files[0].path));
    return x.name || x.title || (file ? file.replace(/\.zip$/i, "") : "") || base(x.folder && x.folder.path);
  }

  function PickReporter({ type, values }) {
    const list = (values || []).map((p) => ({ id: String(p.id), name: itemName(p) }));
    const sig = list.map((p) => p.id).join(",");
    const key = React.useRef({}).current;
    React.useEffect(() => {
      picks[type].set(key, list);
      notify();
    }, [sig]);
    React.useEffect(
      () => () => {
        picks[type].delete(key);
        notify();
      },
      []
    );
    return null;
  }

  // ---- where suggestions come from (each cached briefly) -------------------
  const TAG_FIELDS = "id name sort_name aliases image_path stash_ids { endpoint stash_id }";

  // A small cache whose entries expire, e.g. because the edit hook can change
  // a performer's tags right after a save.
  function cached(ttl, load) {
    const store = new Map(); // key -> { at, promise }
    return (key, ...args) => {
      const hit = store.get(key);
      if (hit && Date.now() - hit.at < ttl) return hit.promise;
      const promise = load(...args);
      promise.catch(() => store.delete(key));
      store.set(key, { at: Date.now(), promise });
      return promise;
    };
  }

  // Performer -> their tags
  const performerTagsOf = cached(15000, async (id) => {
    const d = await gql(
      `query($ids: [ID!]) { findPerformers(ids: $ids) { performers { tags { ${TAG_FIELDS} } } } }`,
      { ids: [id] }
    );
    return (d.findPerformers.performers[0] || {}).tags || [];
  });
  const performerTags = (ids) => Promise.all(ids.map((id) => performerTagsOf(id, id)));

  // Tag ids the plugin made from gallery names. They're on every photo of
  // their gallery, so they'd swamp "most used" lists; asked once per page load.
  let galleryNameIds = null;
  function galleryNameTagIds() {
    if (!galleryNameIds) {
      galleryNameIds = gql(
        "mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }",
        { p: PLUGIN_ID, a: { mode: "gallerytagids" } }
      )
        .then((d) => new Set((d.runPluginOperation || []).map(String)))
        .catch(() => new Set());
    }
    return galleryNameIds;
  }

  async function tagDetails(ids) {
    if (!ids.length) return new Map();
    const d = await gql(
      `query($ids: [ID!]) { findTags(ids: $ids, filter: {per_page: -1}) { tags { ${TAG_FIELDS} } } }`,
      { ids }
    );
    return new Map(d.findTags.tags.map((t) => [String(t.id), t]));
  }

  // Galleries -> the tags most used on their images: [{ tag, uses }], total
  const galleryCommon = cached(15000, async (gids) => {
    const [d, skip] = await Promise.all([
      gql(
        `query($f: ImageFilterType) { findImages(image_filter: $f, filter: {per_page: -1}) {
           count images { tags { id } } } }`,
        { f: { galleries: { value: gids, modifier: "INCLUDES" } } }
      ),
      galleryNameTagIds(),
    ]);
    const counts = new Map();
    d.findImages.images.forEach((img) =>
      img.tags.forEach((t) => {
        const id = String(t.id);
        if (!skip.has(id)) counts.set(id, (counts.get(id) || 0) + 1);
      })
    );
    const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 60);
    const details = await tagDetails(top.map(([id]) => id));
    return {
      total: d.findImages.count,
      items: top.filter(([id]) => details.has(id)).map(([id, uses]) => ({ tag: details.get(id), uses })),
    };
  });

  // The most used image or scene tags in the whole library
  const libraryCommon = cached(60000, async (kind) => {
    const sort = kind === "image" ? "images_count" : "scenes_count";
    const [d, skip] = await Promise.all([
      gql(
        `query($f: FindFilterType) { findTags(filter: $f) { tags { ${TAG_FIELDS} image_count scene_count } } }`,
        { f: { sort, direction: "DESC", per_page: 80 } }
      ),
      galleryNameTagIds(),
    ]);
    return d.findTags.tags
      .filter((t) => !skip.has(String(t.id)))
      .map((t) => ({ tag: t, uses: kind === "image" ? t.image_count : t.scene_count }));
  });

  // On an image or scene's own page, what kind it is
  function pageKind() {
    const m = location.pathname.match(/^\/(images|scenes)\/\d+/);
    return m ? (m[1] === "images" ? "image" : "scene") : null;
  }

  // ---- the suggestion rows -------------------------------------------------
  // Up to three rows: the picked performers' top tags, the tags most used in
  // the picked galleries, and - when there are neither, on an image or scene
  // page - the library's most used image/scene tags. `fixed` gives the
  // performers directly (bulk tag window) and skips the other rows.
  function Suggestions({ values, onSelect, fixed, label }) {
    const [state, setState] = React.useState(() => ({
      performers: picked("performers"),
      galleries: picked("galleries"),
    }));
    const performers = fixed || state.performers;
    const galleries = fixed ? [] : state.galleries;
    const [rows, setRows] = React.useState([]);
    // "Ask AI" (local model via Ollama), on a scene or image's own page
    const [ai, setAi] = React.useState({ state: "idle" });
    const pageItem = fixed ? null : (location.pathname.match(/^\/(scenes|images)\/(\d+)/) || null);

    React.useEffect(() => {
      if (fixed) return;
      const fn = () => setState({ performers: picked("performers"), galleries: picked("galleries") });
      listeners.add(fn);
      fn();
      return () => listeners.delete(fn);
    }, []);

    const pSig = performers.map((p) => p.id).sort().join(",");
    const gSig = galleries.map((g) => g.id).sort().join(",");
    React.useEffect(() => {
      let live = true;
      (async () => {
        const out = [];
        if (performers.length) {
          const lists = await performerTags(performers.map((p) => p.id));
          const byTag = new Map(); // tag id -> {tag, from: [names]}
          lists.forEach((tags, i) =>
            tags.forEach((t) => {
              const e = byTag.get(t.id) || { tag: t, from: [] };
              e.from.push(performers[i].name);
              byTag.set(t.id, e);
            })
          );
          const items = [...byTag.values()]
            .sort((a, b) => b.from.length - a.from.length || a.tag.name.localeCompare(b.tag.name))
            .map((e) => ({ tag: e.tag, title: "Tagged on " + e.from.join(", ") }));
          const names = performers.map((p) => p.name);
          out.push({
            key: "performers",
            label:
              label ||
              (names.length === 1 ? `${names[0]}'s top tags` : names.length === 2 ? `Top tags of ${names.join(" & ")}` : "Performers' top tags"),
            items,
          });
        }
        if (galleries.length) {
          const ids = galleries.map((g) => g.id).sort();
          const common = await galleryCommon(ids.join(","), ids);
          const gNames = galleries.map((g) => g.name).filter(Boolean);
          out.push({
            key: "galleries",
            label:
              gNames.length === galleries.length && gNames.length <= 2
                ? `Common in ${gNames.join(" & ")}`
                : "Common in these galleries",
            items: common.items.map((e) => ({ tag: e.tag, title: `On ${e.uses} of ${common.total} images` })),
          });
        }
        const kind = pageKind();
        if (!fixed && !performers.length && !galleries.length && kind) {
          const common = await libraryCommon(kind, kind);
          out.push({
            key: "library",
            label: `Most used ${kind} tags`,
            items: common.map((e) => ({ tag: e.tag, title: `Used on ${e.uses} ${kind}s` })),
          });
        }
        if (live) setRows(out);
      })().catch((e) => console.warn("[Better Tagging Tools]", e));
      return () => {
        live = false;
      };
    }, [pSig, gSig, fixed ? "fixed" : pageKind()]);

    async function askAi() {
      setAi({ state: "loading" });
      try {
        const d = await gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", {
          p: PLUGIN_ID,
          a: { mode: "ai_suggest", kind: pageItem[1] === "scenes" ? "scene" : "image", id: pageItem[2] },
        });
        const tags = (d.runPluginOperation || []).map((t) => ({ id: String(t.id), name: t.name, aliases: [] }));
        setAi({ state: "done", tags });
      } catch (e) {
        setAi({ state: "error", message: e.message });
      }
    }

    // Leave out tags already chosen, and repeats from an earlier row
    const seen = new Set((values || []).map((t) => String(t.id)));
    const aiTags = ai.state === "done" ? ai.tags.filter((t) => !seen.has(String(t.id))) : [];
    const shown = rows
      .map((row) => {
        const items = row.items.filter((e) => !seen.has(String(e.tag.id))).slice(0, maxSuggestions);
        items.forEach((e) => seen.add(String(e.tag.id)));
        return { ...row, items };
      })
      .filter((row) => row.items.length);
    if (!shown.length && !pageItem) return null;

    const add = (tags) => onSelect([...(values || []), ...tags]);

    const aiRow =
      pageItem &&
      h(
        "div",
        { className: "pts-suggest pts-ai", key: "ai" },
        h("span", { className: "pts-label" }, "AI"),
        ai.state === "idle" &&
          h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: askAi, title: "Ask the local AI model (Ollama) which of your tags fit" }, "Ask AI for tags"),
        ai.state === "loading" && h("span", { className: "pts-ai-note" }, "Looking at it…"),
        ai.state === "error" &&
          h("span", { className: "pts-ai-note" }, "Couldn't ask the AI: " + ai.message + " ",
            h("button", { type: "button", className: "pts-chip", onClick: askAi }, "Try again")),
        ai.state === "done" && !aiTags.length && h("span", { className: "pts-ai-note" }, ai.tags.length ? "All its suggestions are already added" : "No suggestions"),
        aiTags.map((t) =>
          h("button", { key: t.id, type: "button", className: "pts-chip", title: "Suggested by the local AI model", onClick: () => add([t]) }, "+ " + t.name)
        ),
        aiTags.length > 1 && h("button", { type: "button", className: "pts-chip pts-all", onClick: () => add(aiTags) }, "Add all")
      );

    return h(
      "div",
      { className: "pts-suggest-rows" },
      aiRow,
      shown.map((row) =>
        h(
          "div",
          { className: "pts-suggest", key: row.key },
          h("span", { className: "pts-label" }, row.label),
          row.items.map((e) =>
            h(
              "button",
              {
                key: e.tag.id,
                type: "button",
                className: "pts-chip",
                title: e.title,
                onClick: () => add([e.tag]),
              },
              "+ " + e.tag.name
            )
          ),
          row.items.length > 1 &&
            h(
              "button",
              {
                type: "button",
                className: "pts-chip pts-all",
                onClick: () => add(row.items.map((e) => e.tag)),
              },
              "Add all"
            )
        )
      )
    );
  }

  // React calls components as fn(props, secondArg), so the rendered result is
  // always the last argument of an "after" patch.
  for (const [component, type] of [
    ["PerformerSelect", "performers"],
    ["GallerySelect", "galleries"],
  ]) {
    api.patch.after(component, function (...args) {
      const props = args[0] || {};
      const result = args[args.length - 1];
      if (!props.isMulti || !props.onSelect) return result;
      return h(React.Fragment, null, result, h(PickReporter, { type, values: props.values }));
    });
  }

  api.patch.after("TagSelect", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    if (!props.isMulti || !props.onSelect || props.isDisabled || props.ptsNoSuggest) return result;
    return h(
      React.Fragment,
      null,
      result,
      h(Suggestions, { values: props.values, onSelect: props.onSelect })
    );
  });

  // ---- bulk tagging (performer pages and gallery pages) --------------------
  // A target describes what can be tagged:
  //   title, options: [{ key, label, count, on }], performers (for the
  //   suggestions), what (for the job name), args(keys) -> plugin args
  function BulkTagModal({ target, onClose }) {
    const { Modal, Button, Form } = api.libraries.Bootstrap;
    const TagSelect = api.components.TagSelect;
    const Toast = api.hooks.useToast();
    const [tags, setTags] = React.useState([]);
    const [chosen, setChosen] = React.useState(() =>
      Object.fromEntries(target.options.map((o) => [o.key, !!o.on && o.count > 0]))
    );
    const [remove, setRemove] = React.useState(false);
    const [busy, setBusy] = React.useState(false);

    const keys = target.options.filter((o) => chosen[o.key]).map((o) => o.key);
    const itemCount = target.options.reduce((n, o) => n + (chosen[o.key] ? o.count : 0), 0);
    const canRun = tags.length > 0 && itemCount > 0 && !busy;
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

    async function run() {
      setBusy(true);
      try {
        await gql(
          `mutation($p: ID!, $d: String, $a: Map) {
             runPluginTask(plugin_id: $p, description: $d, args_map: $a) }`,
          {
            p: PLUGIN_ID,
            d: `Better Tagging Tools: ${remove ? "removing" : "adding"} ${plural(tags.length, "tag")} ${
              remove ? "from" : "to"
            } ${target.what}`,
            a: Object.assign(target.args(keys), {
              mode: "bulktag",
              tag_ids: tags.map((t) => t.id).join(","),
              op: remove ? "remove" : "add",
            }),
          }
        );
        Toast.success("Bulk tagging started. You can follow it under Settings → Tasks.");
        onClose();
      } catch (e) {
        Toast.error(e);
        setBusy(false);
      }
    }

    const names = target.performers.map((p) => p.name);
    const suggestLabel = !names.length
      ? ""
      : names.length === 1
      ? `${names[0]}'s top tags`
      : names.length === 2
      ? `Top tags of ${names.join(" & ")}`
      : "Top tags of these performers";

    return h(
      Modal,
      { show: true, onHide: onClose, className: "pts-bulk-modal" },
      h(Modal.Header, { closeButton: true }, h(Modal.Title, null, target.title)),
      h(
        Modal.Body,
        null,
        h(Form.Label, null, "Apply to"),
        h(
          "div",
          { className: "pts-bulk-kinds" },
          target.options.map((o) =>
            h(Form.Check, {
              key: o.key,
              id: "pts-kind-" + o.key,
              type: "checkbox",
              disabled: !o.count,
              label: o.label,
              checked: !!chosen[o.key],
              onChange: (e) => {
                const next = { ...chosen, [o.key]: e.target.checked };
                // options in the same group (e.g. all images / selected
                // images) can't both be on
                if (e.target.checked && o.group)
                  target.options.forEach((x) => {
                    if (x !== o && x.group === o.group) next[x.key] = false;
                  });
                setChosen(next);
              },
            })
          )
        ),
        h(
          "div",
          { className: "pts-bulk-kinds" },
          h(Form.Check, {
            id: "pts-op-add",
            type: "radio",
            inline: true,
            label: "Add tags",
            checked: !remove,
            onChange: () => setRemove(false),
          }),
          h(Form.Check, {
            id: "pts-op-remove",
            type: "radio",
            inline: true,
            label: "Remove tags",
            checked: remove,
            onChange: () => setRemove(true),
          })
        ),
        target.note && h("div", { className: "pts-bulk-note" }, target.note),
        h(Form.Label, null, "Tags"),
        TagSelect
          ? h(TagSelect, {
              isMulti: true,
              values: tags,
              onSelect: setTags,
              ptsNoSuggest: true,
              // with many partial matches, "Create" would be at the bottom
              // of a long list; put it at the top instead
              createOptionPosition: "first",
              // the window's body scrolls and would clip the dropdown, so
              // draw the dropdown over the page instead
              menuPortalTarget: document.body,
            })
          : h("div", { className: "text-muted" }, "Tag picker unavailable - use the suggestions below."),
        target.performers.length > 0 &&
          h(Suggestions, { values: tags, onSelect: setTags, fixed: target.performers, label: suggestLabel }),
        tags.length > 0 &&
          !TagSelect &&
          h("div", { className: "pts-bulk-chosen" }, "Chosen: " + tags.map((t) => t.name).join(", "))
      ),
      h(
        Modal.Footer,
        null,
        h(Button, { variant: "secondary", onClick: onClose }, "Cancel"),
        h(
          Button,
          { variant: remove ? "danger" : "primary", disabled: !canRun, onClick: run },
          tags.length
            ? `${remove ? "Remove" : "Add"} ${plural(tags.length, "tag")} ${remove ? "from" : "to"} ${plural(
                itemCount,
                "item"
              )}`
            : remove
            ? "Remove tags"
            : "Add tags"
        )
      )
    );
  }

  // -- performer pages: a button under the details
  const PERFORMER_KINDS = [
    ["scenes", "Scenes", "scene_count"],
    ["images", "Images", "image_count"],
    ["galleries", "Galleries", "gallery_count"],
  ];

  function performerTarget(performer) {
    return {
      title: `Bulk tag ${performer.name}'s items`,
      what: `${performer.name}'s items`,
      options: PERFORMER_KINDS.map(([key, label, c]) => ({
        key,
        label: `${label} (${performer[c] || 0})`,
        count: performer[c] || 0,
        on: true,
      })),
      performers: [{ id: String(performer.id), name: performer.name }],
      args: (keys) => ({ performer_id: String(performer.id), kinds: keys.join(",") }),
    };
  }

  function BulkTagButton({ performer }) {
    const [open, setOpen] = React.useState(false);
    const target = React.useMemo(() => performer && performer.id && performerTarget(performer), [performer]);
    if (!target || !target.options.some((o) => o.count)) return null;
    return h(
      "div",
      { className: "pts-bulk" },
      h(
        "button",
        { type: "button", className: "btn btn-secondary btn-sm", onClick: () => setOpen(true) },
        "Bulk tag scenes, images & galleries…"
      ),
      open && h(BulkTagModal, { target, onClose: () => setOpen(false) })
    );
  }

  api.patch.after("PerformerDetailsPanel", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(BulkTagButton, { performer: props.performer }));
  });

  // -- gallery pages: a "Bulk tag" button in the image list's toolbar. The
  // button lives in Stash's list, so it opens the window through this small
  // store, which the GalleryBulkHost below listens to.
  const galleryBulk = { open: null, listeners: new Set() };
  const setGalleryBulk = (v) => {
    galleryBulk.open = v;
    galleryBulk.listeners.forEach((fn) => fn());
  };

  function galleryTarget(gallery, selectedIds, imageCount) {
    const name = gallery.title || (gallery.files && gallery.files[0] && gallery.files[0].basename) || "this gallery";
    const sel = selectedIds.length;
    const options = [
      { key: "images", group: "img", label: `All images in the gallery (${imageCount})`, count: imageCount, on: !sel },
    ];
    if (sel) options.push({ key: "selected", group: "img", label: `Only the selected images (${sel})`, count: sel, on: true });
    options.push({ key: "gallery", label: "The gallery itself", count: 1, on: false });
    return {
      title: `Bulk tag ${name}`,
      what: `images in ${name}`,
      options,
      note:
        'Tagging "the gallery itself" also tags all its images if the "Tag images from galleries" plugin is on (it only adds, never removes).',
      performers: (gallery.performers || []).map((p) => ({ id: String(p.id), name: p.name })),
      args: (keys) => {
        const a = { gallery_id: String(gallery.id), kinds: keys.filter((k) => k !== "selected").join(",") };
        if (keys.includes("selected")) {
          a.image_ids = selectedIds.join(",");
          a.kinds = keys.filter((k) => k !== "selected").concat("selected_images").join(",");
        }
        return a;
      },
    };
  }

  function GalleryBulkHost() {
    const [target, setTarget] = React.useState(galleryBulk.open);
    React.useEffect(() => {
      const fn = () => setTarget(galleryBulk.open);
      galleryBulk.listeners.add(fn);
      return () => galleryBulk.listeners.delete(fn);
    }, []);
    if (!target) return null;
    return h(BulkTagModal, { target, onClose: () => setGalleryBulk(null) });
  }

  api.patch.before("GalleryImagesPanel", function (...args) {
    const props = args[0] || {};
    const gallery = props.gallery;
    if (!gallery) return args;
    const tagsIcon = api.libraries.FontAwesomeSolid && api.libraries.FontAwesomeSolid.faTags;
    const open = {
      onClick: (result, filter, selectedIds) => {
        const ids = [...(selectedIds || [])].map(String);
        // the whole gallery, even if the list is searched or filtered
        const count =
          gallery.image_count ??
          (result && result.data && result.data.findImages && result.data.findImages.count) ??
          0;
        setGalleryBulk(galleryTarget(gallery, ids, count));
      },
    };
    // A tags-icon button in the toolbar, plus a labelled entry in the "..."
    // menu (icons have no tooltip on phones, so the button is easy to miss)
    const extra = [
      ...(props.extraOperations || []),
      { ...open, text: "Bulk tag images…", icon: tagsIcon },
      { ...open, text: "Bulk tag images…" },
    ];
    return [Object.assign({}, props, { extraOperations: extra }), ...args.slice(1)];
  });

  api.patch.after("GalleryImagesPanel", function (...args) {
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(GalleryBulkHost));
  });
  // ---- scene page: use a frame as a tag's picture ----------------------------
  // "Use a frame as a tag's picture…" in the scene's ⋮ menu opens a window: pick the
  // moment (the player's current position, or a typed time), see the frame,
  // pick a tag, and set the frame as the tag's picture. The frame is grabbed
  // on the server with ffmpeg at full quality, so typing a time doesn't move
  // the video.

  function playerTime() {
    try {
      const p = api.utils.InteractiveUtils.getPlayer();
      if (p) return p.currentTime() || 0;
    } catch (e) {}
    const v = document.querySelector(".scene-player-container video");
    return v ? v.currentTime || 0 : 0;
  }

  // "83.5", "1:23", "1:02:03" -> seconds (null if not a time)
  function parseTime(text) {
    const t = String(text).trim();
    if (!t) return null;
    if (!/^\d+(\.\d+)?$|^\d+(:\d{1,2}){1,2}(\.\d+)?$/.test(t)) return null;
    return t.split(":").reduce((acc, part) => acc * 60 + Number(part), 0);
  }

  function formatTime(sec) {
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
    const tenth = Math.floor((sec % 1) * 10);
    const mm = h ? String(m).padStart(2, "0") : String(m);
    return (h ? h + ":" : "") + mm + ":" + String(s).padStart(2, "0") + (tenth ? "." + tenth : "");
  }

  function FrameTagModal({ scene, onClose }) {
    const { Modal, Button, Form } = api.libraries.Bootstrap;
    const TagSelect = api.components.TagSelect;
    const Toast = api.hooks.useToast();
    const duration = (scene.files && scene.files[0] && scene.files[0].duration) || 0;
    const [at, setAt] = React.useState(() => Math.round(playerTime() * 10) / 10);
    const [text, setText] = React.useState(() => formatTime(at));
    const [preview, setPreview] = React.useState({ state: "loading" });
    const [tag, setTag] = React.useState(null);
    const [current, setCurrent] = React.useState(null);
    const [busy, setBusy] = React.useState(false);

    // Grab the frame (a little after the time stops changing)
    React.useEffect(() => {
      let live = true;
      setPreview({ state: "loading" });
      const timer = setTimeout(() => {
        gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", {
          p: PLUGIN_ID,
          a: { mode: "frame", scene_id: String(scene.id), at },
        })
          .then((d) => live && setPreview(d.runPluginOperation ? { state: "ok", src: d.runPluginOperation } : { state: "error" }))
          .catch((e) => live && setPreview({ state: "error", message: e.message }));
      }, 350);
      return () => {
        live = false;
        clearTimeout(timer);
      };
    }, [at]);

    // The chosen tag's current picture, to compare
    React.useEffect(() => {
      setCurrent(null);
      if (!tag) return;
      gql("query($id: ID!) { findTag(id: $id) { image_path } }", { id: String(tag.id) })
        .then((d) => {
          const path = d.findTag && d.findTag.image_path;
          setCurrent(path && !/default=true/.test(path) ? path : "none");
        })
        .catch(() => {});
    }, [tag && tag.id]);

    function useTyped(value) {
      setText(value);
      const sec = parseTime(value);
      if (sec !== null) setAt(Math.min(sec, Math.max(duration - 0.1, 0)));
    }

    function useNow() {
      const sec = Math.round(playerTime() * 10) / 10;
      setAt(sec);
      setText(formatTime(sec));
    }

    async function save() {
      setBusy(true);
      try {
        await gql("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }", {
          i: { id: String(tag.id), image: preview.src },
        });
        Toast.success(`Set "${tag.name}"'s picture`);
        onClose();
      } catch (e) {
        Toast.error(e);
        setBusy(false);
      }
    }

    const typedOk = parseTime(text) !== null;
    const sceneTags = (scene.tags || []).filter((t) => !tag || String(t.id) !== String(tag.id));

    return h(
      Modal,
      { show: true, onHide: onClose, className: "pts-frame-modal" },
      h(Modal.Header, { closeButton: true }, h(Modal.Title, null, "Use a frame as a tag's picture")),
      h(
        Modal.Body,
        null,
        h(Form.Label, null, "Time"),
        h(
          "div",
          { className: "pts-frame-time" },
          h(Form.Control, {
            value: text,
            isInvalid: !typedOk,
            placeholder: "e.g. 1:23",
            onChange: (e) => useTyped(e.target.value),
            "aria-label": "Time in the video",
          }),
          h(Button, { variant: "secondary", onClick: useNow }, "Use current position"),
          duration > 0 && h("span", { className: "pts-frame-of" }, "of " + formatTime(duration))
        ),
        h(
          "div",
          { className: "pts-frame-pictures" },
          tag &&
            current &&
            h(
              "figure",
              null,
              current === "none"
                ? h("div", { className: "pts-frame-empty" }, "No picture yet")
                : h("img", { src: current, alt: "" }),
              h("figcaption", null, "Now")
            ),
          h(
            "figure",
            { className: "pts-frame-new" },
            preview.state === "ok"
              ? h("img", { src: preview.src, alt: "" })
              : h("div", { className: "pts-frame-empty" }, preview.state === "loading" ? "Grabbing frame…" : "Couldn't grab this frame" + (preview.message ? `: ${preview.message}` : "")),
            h("figcaption", null, (tag && current ? "New - " : "") + formatTime(at))
          )
        ),
        h(Form.Label, null, "Tag"),
        TagSelect &&
          h(TagSelect, {
            isMulti: false,
            values: tag ? [tag] : [],
            onSelect: (items) => setTag((items && items[0]) || null),
            ptsNoSuggest: true,
            createOptionPosition: "first",
            menuPortalTarget: document.body,
          }),
        sceneTags.length > 0 &&
          h(
            "div",
            { className: "pts-suggest" },
            h("span", { className: "pts-label" }, "This scene's tags"),
            sceneTags.slice(0, 40).map((t) =>
              h("button", { key: t.id, type: "button", className: "pts-chip", onClick: () => setTag(t) }, t.name)
            )
          )
      ),
      h(
        Modal.Footer,
        null,
        h(Button, { variant: "secondary", onClick: onClose }, "Cancel"),
        h(
          Button,
          { variant: "primary", disabled: !tag || preview.state !== "ok" || busy, onClick: save },
          tag ? `Set as "${tag.name}"'s picture` : "Set picture"
        )
      )
    );
  }

  // Adds "Use a frame as a tag's picture…" to the scene page's ⋮ menu. Stash
  // doesn't let plugins add to that menu, so the item is put in whenever the
  // menu appears, and opens the window through this component's state.
  const FRAME_ITEM_TEXT = "Use a frame as a tag's picture…";

  function FrameTagMenuItem({ scene }) {
    const [open, setOpen] = React.useState(false);
    const hasVideo = !!(scene && scene.files && scene.files.length);

    React.useEffect(() => {
      if (!hasVideo) return;
      const place = () => {
        const toggle = document.getElementById("operation-menu");
        const menu = toggle && toggle.parentElement.querySelector(".dropdown-menu");
        if (!menu || menu.querySelector(".pts-frame-item")) return;
        const item = document.createElement("a");
        item.href = "#";
        item.setAttribute("role", "button");
        item.className = "dropdown-item bg-secondary text-white pts-frame-item";
        item.textContent = FRAME_ITEM_TEXT;
        item.addEventListener("click", (e) => {
          e.preventDefault();
          toggle.click(); // close the menu
          setOpen(true);
        });
        // next to Stash's own screenshot items (Delete stays last)
        const items = [...menu.querySelectorAll(".dropdown-item")];
        const near = items.filter((el) => /screenshot|thumbnail/i.test(el.textContent)).pop();
        if (near) near.after(item);
        else if (items.length) items[items.length - 1].before(item);
        else menu.appendChild(item);
      };
      place();
      const obs = new MutationObserver(place);
      obs.observe(document.body, { childList: true, subtree: true });
      return () => {
        obs.disconnect();
        document.querySelectorAll(".pts-frame-item").forEach((el) => el.remove());
      };
    }, [hasVideo]);

    return open ? h(FrameTagModal, { scene, onClose: () => setOpen(false) }) : null;
  }

  api.patch.after("ScenePage", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(FrameTagMenuItem, { scene: props.scene }));
  });
  // ---- Tags list: "Pick a new picture" in the ••• menu ----------------------
  // For the selected tags, the plugin picks each one's next picture (same
  // rules as the picture tasks) and the cards update straight away.
  const repick = { request: null, listeners: new Set() };

  function RepickHost() {
    const Toast = api.hooks.useToast();
    React.useEffect(() => {
      const run = async (ids) => {
        let changed = 0, none = 0;
        for (const id of ids) {
          // dim the card while its picture is being picked
          const cards = [...document.querySelectorAll(".tag-card")].filter((c) =>
            c.querySelector(`a[href="/tags/${id}"], a[href^="/tags/${id}?"]`)
          );
          cards.forEach((c) => c.classList.add("pts-repicking"));
          try {
            const d = await gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", {
              p: PLUGIN_ID,
              a: { mode: "newpicture", tag_id: String(id) },
            });
            const link = d.runPluginOperation;
            if (link) {
              changed++;
              cards.forEach((c) => c.querySelectorAll("img.tag-card-image").forEach((img) => (img.src = link)));
            } else none++;
          } catch (e) {
            none++;
            console.warn("[Better Tagging Tools] new picture failed", e);
          }
          cards.forEach((c) => c.classList.remove("pts-repicking"));
        }
        const s = (n) => (n === 1 ? "" : "s");
        if (changed) Toast.success(`New picture for ${changed} tag${s(changed)}` + (none ? ` (${none} had no other picture)` : ""));
        else Toast.error(new Error(ids.length === 1 ? "This tag has no other picture to use" : "None of these tags has another picture to use"));
      };
      const fn = () => {
        const ids = repick.request;
        repick.request = null;
        if (ids && ids.length) run(ids);
      };
      repick.listeners.add(fn);
      return () => repick.listeners.delete(fn);
    }, []);
    return null;
  }

  api.patch.before("FilteredTagList", function (...args) {
    const props = args[0] || {};
    const op = {
      text: "Pick a new picture",
      isDisplayed: (result, filter, selectedIds) => !!selectedIds && selectedIds.size > 0,
      onClick: (result, filter, selectedIds) => {
        repick.request = [...(selectedIds || [])].map(String);
        repick.listeners.forEach((fn) => fn());
      },
    };
    return [Object.assign({}, props, { extraOperations: [...(props.extraOperations || []), op] }), ...args.slice(1)];
  });

  api.patch.after("FilteredTagList", function (...args) {
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(RepickHost));
  });

  // ---- Auto tag this / Auto tag selected ------------------------------------
  // Adds the performers, studio and tags whose names are in the file path
  // (the same matching as Stash's Auto Tag, which itself can only be run on
  // whole folders). "Auto tag this" goes in the ⋮ menu of scene, image and
  // gallery pages (for a gallery: the gallery and its images); "Auto tag
  // selected" in the ••• menu of the scene, image and gallery lists. Stash
  // doesn't let plugins add to the page menus, so the item is put in whenever
  // the menu appears. A small component in the nav bar shows the results.
  const AUTOTAG_ITEM_TEXT = "Auto tag this (from file name)";
  const LIST_LIMIT = 500; // more than this runs as a background task
  const autotag = { request: null, listeners: new Set(), host: null };
  const KIND_BY_PATH = { scenes: "scene", images: "image", galleries: "gallery" };

  function requestAutotag(kind, ids) {
    autotag.request = { kind, ids: ids.map(String) };
    autotag.listeners.forEach((fn) => fn());
  }

  // The scenes list (unlike images and galleries) has no way for plugins to add
  // to its ••• menu, so the item goes in when the menu opens with scenes selected.
  function selectedSceneIds() {
    const ids = new Set();
    document.querySelectorAll(".scene-card .card-check:checked, table input[type=checkbox]:checked").forEach((box) => {
      const holder = box.closest(".scene-card, tr");
      const link = holder && holder.querySelector('a[href^="/scenes/"]');
      const m = link && link.getAttribute("href").match(/^\/scenes\/(\d+)/);
      if (m) ids.add(m[1]);
    });
    return [...ids];
  }

  function placeSceneListItem(toggle) {
    {
      // the menu may be rendered next to the button or at the end of the page
      const menu = (toggle.parentElement && toggle.parentElement.querySelector(".dropdown-menu")) ||
        document.querySelector('.dropdown-menu[aria-labelledby="more-menu"]');
      if (!menu) return;
      menu.querySelectorAll(".bt-autotag-list-item").forEach((el) => el.remove());
      if ([...menu.querySelectorAll(".dropdown-item")].some((el) => /^Auto tag selected/.test(el.textContent))) return;
      if (!selectedSceneIds().length) return;
      const item = document.createElement("a");
      item.href = "#";
      item.setAttribute("role", "button");
      item.className = "dropdown-item bg-secondary text-white bt-autotag-list-item";
      item.textContent = "Auto tag selected (from file names)";
      item.addEventListener("click", (e) => {
        e.preventDefault();
        const ids = selectedSceneIds();
        toggle.click(); // close the menu
        if (ids.length) requestAutotag("scene", ids);
      });
      menu.appendChild(item);
    }
  }

  // when the ••• button is tapped (the menu appears a moment later)
  document.addEventListener("click", (e) => {
    const toggle = e.target.closest && e.target.closest("#more-menu");
    if (toggle) [0, 60, 200].forEach((ms) => setTimeout(() => placeSceneListItem(toggle), ms));
  }, true);

  function placeAutotagItems() {
    const m = location.pathname.match(/^\/(scenes|images|galleries)\/(\d+)(?:\/|$)/);
    if (!m) return;
    document.querySelectorAll("#operation-menu").forEach((toggle) => {
      const menu = toggle.parentElement && toggle.parentElement.querySelector(".dropdown-menu");
      if (!menu || menu.querySelector(".bt-autotag-item")) return;
      const item = document.createElement("a");
      item.href = "#";
      item.setAttribute("role", "button");
      item.className = "dropdown-item bg-secondary text-white bt-autotag-item";
      item.textContent = AUTOTAG_ITEM_TEXT;
      item.addEventListener("click", (e) => {
        e.preventDefault();
        toggle.click(); // close the menu
        const now = location.pathname.match(/^\/(scenes|images|galleries)\/(\d+)/);
        if (now) requestAutotag(KIND_BY_PATH[now[1]], [now[2]]);
      });
      // above Delete, which stays last
      const del = [...menu.querySelectorAll(".dropdown-item")].find((el) => /^delete/i.test(el.textContent.trim()));
      if (del) del.before(item);
      else menu.appendChild(item);
    });
  }

  function AutotagHost() {
    const Toast = api.hooks.useToast();
    const me = React.useRef({}).current;
    React.useEffect(() => {
      if (autotag.host) return; // only one host (the nav bar can render twice)
      autotag.host = me;
      placeAutotagItems();
      const obs = new MutationObserver(placeAutotagItems);
      obs.observe(document.body, { childList: true, subtree: true });

      const run = async ({ kind, ids }) => {
        const what = ids.length === 1
          ? (kind === "gallery" ? "this gallery and its images" : `this ${kind}`)
          : `${ids.length} ${kind === "gallery" ? "galleries and their images" : kind + "s"}`;
        const args = { mode: "autotag", kind, ids: ids.join(","), with_images: kind === "gallery" ? "1" : "" };
        try {
          if (ids.length > LIST_LIMIT) {
            await gql("mutation($p: ID!, $d: String, $a: Map) { runPluginTask(plugin_id: $p, description: $d, args_map: $a) }", {
              p: PLUGIN_ID, d: `Better Tagging Tools: auto tag ${ids.length} ${kind}s`, a: args,
            });
            Toast.success(`Auto tagging ${what} in the background. You can follow it under Settings → Tasks.`);
            return;
          }
          Toast.success(`Auto tagging ${what}…`);
          const d = await gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", { p: PLUGIN_ID, a: args });
          const r = d.runPluginOperation || {};
          if (!r.changed) {
            Toast.success(`Auto tag: nothing new in the file name${r.items === 1 ? "" : "s"}`);
            return;
          }
          const list = (r.added || []).slice(0, 8).join(", ") + ((r.added || []).length > 8 ? ", …" : "");
          Toast.success(r.items === 1 ? `Auto tag added ${list}` : `Auto tag added ${list} on ${r.changed} of ${r.items} items`);
          // show the new performers/tags without a reload
          const client = api.utils && api.utils.StashService && api.utils.StashService.getClient && api.utils.StashService.getClient();
          if (client && client.refetchQueries) client.refetchQueries({ include: "active" });
        } catch (e) {
          Toast.error(e);
        }
      };
      const fn = () => {
        const req = autotag.request;
        autotag.request = null;
        if (req && req.ids.length) run(req);
      };
      autotag.listeners.add(fn);
      return () => {
        obs.disconnect();
        autotag.listeners.delete(fn);
        autotag.host = null;
        document.querySelectorAll(".bt-autotag-item, .bt-autotag-list-item").forEach((el) => el.remove());
      };
    }, []);
    return null;
  }

  api.patch.after("MainNavBar.UtilityItems", function (...args) {
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(AutotagHost));
  });

  for (const [component, kind] of [["FilteredImageList", "image"], ["FilteredGalleryList", "gallery"]]) {
    api.patch.before(component, function (...args) {
      const props = args[0] || {};
      const op = {
        text: "Auto tag selected (from file names)",
        isDisplayed: (result, filter, selectedIds) => !!selectedIds && selectedIds.size > 0,
        onClick: (result, filter, selectedIds) => requestAutotag(kind, [...(selectedIds || [])]),
      };
      return [Object.assign({}, props, { extraOperations: [...(props.extraOperations || []), op] }), ...args.slice(1)];
    });
  }
})();
