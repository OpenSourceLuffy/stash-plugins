// Better Tagging AI - the page side: an "AI" row under the Tags box and an
// "AI titles" row under the Title box when editing a scene or image (titles
// also on galleries), asking the local AI model (Ollama) through this
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

  function AiTags({ values, onSelect }) {
    const m = location.pathname.match(ITEM_PAGE);
    const [ai, setAi] = React.useState({ state: "idle" });
    const key = m ? m[1] + m[2] : "";
    React.useEffect(() => setAi({ state: "idle" }), [key]);
    if (!m) return null;

    async function go() {
      setAi({ state: "loading" });
      try {
        const out = await ask({ mode: "ai_suggest", kind: m[1] === "scenes" ? "scene" : "image", id: m[2] });
        setAi({ state: "done", tags: (out || []).map((t) => ({ id: String(t.id), name: t.name, aliases: [] })) });
      } catch (e) {
        setAi({ state: "error", message: e.message });
      }
    }

    const have = new Set((values || []).map((t) => String(t.id)));
    const tags = ai.state === "done" ? ai.tags.filter((t) => !have.has(t.id)) : [];
    const add = (list) => onSelect([...(values || []), ...list]);
    return h(
      "div",
      { className: "pts-suggest pts-ai bta-tags" },
      h("span", { className: "pts-label" }, "AI"),
      ai.state === "idle" &&
        h("button", { type: "button", className: "pts-chip pts-ai-ask", onClick: go, title: "Ask the local AI model (Ollama) which of your tags fit" }, "Ask AI for tags"),
      ai.state === "loading" && h("span", { className: "pts-ai-note" }, "Looking at it…"),
      ai.state === "error" &&
        h("span", { className: "pts-ai-note" }, "Couldn't ask the AI: " + ai.message + " ",
          h("button", { type: "button", className: "pts-chip", onClick: go }, "Try again")),
      ai.state === "done" && !tags.length &&
        h("span", { className: "pts-ai-note" }, ai.tags.length ? "All its suggestions are already added" : "No suggestions"),
      tags.map((t) =>
        h("button", { key: t.id, type: "button", className: "pts-chip", title: "Suggested by the local AI model", onClick: () => add([t]) }, "+ " + t.name)
      ),
      tags.length > 1 && h("button", { type: "button", className: "pts-chip pts-all", onClick: () => add(tags) }, "Add all")
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
      groupTasks();
    }, 200);
  }).observe(document.body, { childList: true, subtree: true });
})();
