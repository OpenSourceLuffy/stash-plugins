// Updates for OpenSourceLuffy's Stash plugins (Better Organizing Tools / AI,
// Modern UI, Privacy Blur, Stash Suggestions, Library Cleanup, Vertical
// Stitcher). The same file ships with each of them; whichever loads first runs
// it. Now and then (twice a day at most) it compares the installed versions
// with the published plugin index and, when there are newer ones, shows what's
// new (from each plugin's changelog) with Install updates / Later / Don't show
// again. Plugins installed through Stash's plugin source are updated with
// Stash's own update job; ones copied in by hand get a pointer instead.
(function () {
  "use strict";
  if (window.__oslUpdates) return;
  window.__oslUpdates = true;

  const INDEX = "https://opensourceluffy.github.io/stash-plugins/main/index.yml";
  const SOURCE = "https://OpenSourceLuffy.github.io/stash-plugins/main/index.yml";
  const RAW = "https://raw.githubusercontent.com/OpenSourceLuffy/stash-plugins/main/plugins/";
  const REPO = "https://github.com/OpenSourceLuffy/stash-plugins/blob/main/plugins/";
  const FOLDER = { vertical_stitcher: "VerticalStitcher" }; // plugin id -> its folder (else the same)
  const EVERY = 12 * 3600 * 1000;      // how often to look
  const LATER = 24 * 3600 * 1000;      // "Later": ask again after this
  const KEY = "osl-updates";           // { checked, later, off }

  const store = () => { try { return JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } };
  const save = (patch) => { try { localStorage.setItem(KEY, JSON.stringify({ ...store(), ...patch })); } catch (e) { /* this visit only */ } };

  async function gql(query, variables) {
    const res = await fetch("/graphql", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query, variables }) });
    const j = await res.json();
    if (j.errors) throw new Error(j.errors[0].message);
    return j.data;
  }
  // "1.8" < "1.8.1" < "1.9" (a "-abc123" build suffix is ignored)
  const parts = (v) => String(v || "").split("-")[0].split(".").map((x) => parseInt(x, 10) || 0);
  const newer = (a, b) => {
    const x = parts(a), y = parts(b);
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0);
    }
    return false;
  };
  const plain = (s) => s.replace(/\*\*|`/g, "").replace(/(^|\s)\*(\S[^*]*?)\*(?=\W|$)/g, "$1$2")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/\s+/g, " ").trim();
  // a changelog point in a line: its first sentence (a long one cut short)
  const gist = (s) => {
    const m = s.match(/^(.{30,220}?[.!?])(?=\s+["(]?[A-Z]|$)/); // (not "e.g. users")
    const t = m ? m[1] : s;
    return t.length > 220 ? t.slice(0, 217).replace(/\s+\S*$/, "") + "…" : t;
  };

  // the index: [{ id, name, version }]
  async function published() {
    const text = await (await fetch(INDEX, { cache: "no-store" })).text();
    const out = [];
    let cur = null;
    for (const line of text.split("\n")) {
      let m = line.match(/^- id:\s*(.+)$/);
      if (m) { cur = { id: m[1].trim() }; out.push(cur); continue; }
      if (!cur) continue;
      m = line.match(/^ {2}(name|version):\s*"?(.*?)"?\s*$/);
      if (m) cur[m[1]] = m[2];
    }
    return out;
  }

  // what's new since `from`: [{ version, lines }] from the plugin's changelog
  async function whatsNew(id, from) {
    try {
      const text = await (await fetch(RAW + (FOLDER[id] || id) + "/CHANGELOG.md", { cache: "no-store" })).text();
      const out = [];
      for (const sec of text.split(/\n(?=## )/).slice(1)) {
        const m = sec.match(/^## ([\d.]+)/);
        if (!m || !newer(m[1], from)) continue;
        // the main points ("- ..." and the lines that continue them), not their sub-points
        const items = [];
        let inSub = false;
        for (const l of sec.split("\n").slice(1)) {
          if (/^- /.test(l)) { items.push(l.slice(2)); inSub = false; }
          else if (/^ {2,}- /.test(l)) inSub = true;
          else if (/^ {2,}\S/.test(l) && items.length && !inSub) items[items.length - 1] += " " + l.trim();
        }
        out.push({ version: m[1], lines: items.map((t) => gist(plain(t))) });
      }
      return out;
    } catch (e) {
      return [];
    }
  }

  async function check() {
    const st = store();
    if (st.off || (st.later && Date.now() < st.later) || (st.checked && Date.now() - st.checked < EVERY)) return;
    let index, mine, managed = {};
    try {
      [index, mine] = await Promise.all([published(), gql("{ plugins { id name version } }").then((d) => d.plugins || [])]);
    } catch (e) {
      return; // offline, or GitHub unreachable: try next time
    }
    // installed through a plugin source: Stash knows the installed build and
    // the source's, by version and date (so a fix published under the same
    // version number counts too)
    try {
      for (const p of (await gql("{ installedPackages(type: Plugin) { package_id version date sourceURL source_package { version date } } }")).installedPackages || []) managed[p.package_id] = p;
    } catch (e) { /* older Stash: treat all as copied in by hand */ }
    save({ checked: Date.now() });
    const have = Object.fromEntries(mine.map((p) => [p.id, p]));
    const pending = (p) => {
      const m = managed[p.id], sp = m && m.source_package;
      if (sp) return sp.version !== m.version && (!sp.date || !m.date || new Date(sp.date) > new Date(m.date));
      return newer(p.version, have[p.id].version); // (copied in by hand: by version number)
    };
    const updates = index.filter((p) => have[p.id] && pending(p))
      .map((p) => {
        const sp = managed[p.id] && managed[p.id].source_package;
        return { id: p.id, name: have[p.id].name || p.name, from: have[p.id].version,
          to: String((sp && sp.version) || p.version).split("-")[0], source: managed[p.id] && managed[p.id].sourceURL };
      });
    if (!updates.length) return;
    for (const u of updates) u.notes = await whatsNew(u.id, u.from);
    show(updates);
  }

  function show(updates) {
    if (document.getElementById("osl-updates")) return;
    // not on top of another window (Stash's own, or a plugin's welcome / tour): wait for it to close
    if (document.querySelector(".modal.show, .modal-backdrop, .bta-welcome, .bta-tour")) {
      setTimeout(() => show(updates), 2000);
      return;
    }
    const style = document.createElement("style");
    style.textContent = `
      #osl-updates { position: fixed; inset: 0; z-index: 2100; background: rgba(0,0,0,.55); display: flex; align-items: center; justify-content: center; padding: 16px; }
      #osl-updates .box { background: #394b59; color: #f5f8fa; border: 1px solid rgba(255,255,255,.12); border-radius: 12px; max-width: 560px; width: 100%;
        max-height: calc(100vh - 32px); display: flex; flex-direction: column; box-shadow: 0 12px 48px rgba(0,0,0,.5); font-size: 1rem; line-height: 1.45; }
      #osl-updates h4 { margin: 0; padding: 1.1rem 1.3rem .6rem; font-size: 1.2rem; font-weight: 600; color: #fff !important; }
      #osl-updates .list { overflow: auto; padding: 0 1.3rem; }
      #osl-updates .plugin { margin: .4rem 0 .9rem; }
      #osl-updates .plugin b { color: #fff; }
      #osl-updates .ver { color: #c5d0d9; font-size: .92em; margin-left: .3rem; }
      #osl-updates ul { margin: .35rem 0 0; padding-left: 1.15rem; color: #e1e8ed; font-size: .93em; }
      #osl-updates li { margin-bottom: .25rem; }
      #osl-updates .note { color: #d3dce3; font-size: .9em; margin: .3rem 0; }
      #osl-updates a { color: #8cc7ff; }
      #osl-updates .actions { display: flex; flex-wrap: wrap; gap: .6rem; padding: .9rem 1.3rem 1.1rem; border-top: 1px solid rgba(255,255,255,.1); }
      #osl-updates .actions .btn { font-size: .95rem; padding: .45rem .9rem; }
      #osl-updates .actions .btn-secondary { background: #5c7080; border-color: #5c7080; color: #fff; }
      #osl-updates .status { padding: 0 1.3rem; color: #f5f8fa; }`;
    const wrap = document.createElement("div");
    wrap.id = "osl-updates";
    const box = document.createElement("div");
    box.className = "box";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    const h = document.createElement("h4");
    h.textContent = updates.length === 1 ? `An update for ${updates[0].name}` : `Updates for ${updates.length} of your plugins`;
    const list = document.createElement("div");
    list.className = "list";
    const SHOW = 5; // lines per plugin (the rest: "and N more")
    for (const u of updates) {
      const d = document.createElement("div");
      d.className = "plugin";
      d.innerHTML = "<b></b><span class='ver'></span>";
      d.querySelector("b").textContent = u.name;
      d.querySelector(".ver").textContent = u.from === u.to ? `${u.to} (newer build)` : `${u.from} → ${u.to}`;
      const lines = u.notes.flatMap((n) => n.lines);
      if (lines.length) {
        const ul = document.createElement("ul");
        lines.slice(0, SHOW).forEach((l) => {
          const li = document.createElement("li");
          li.textContent = l;
          ul.appendChild(li);
        });
        d.appendChild(ul);
      }
      const more = document.createElement("div");
      more.className = "note";
      more.innerHTML = (lines.length > SHOW ? `And ${lines.length - SHOW} more - ` : "") + "<a target='_blank' rel='noopener'>see all changes</a>";
      more.querySelector("a").href = REPO + (FOLDER[u.id] || u.id) + "/CHANGELOG.md";
      if (!u.source) {
        more.append(document.createElement("br"),
          "Installed by hand: update it in Settings → Plugins → Available Plugins (source " + SOURCE + "), or download it again from GitHub.");
      }
      d.appendChild(more);
      list.appendChild(d);
    }
    const status = document.createElement("div");
    status.className = "status";
    const actions = document.createElement("div");
    actions.className = "actions";
    const btn = (label, cls, fn) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "btn " + cls; b.textContent = label;
      b.addEventListener("click", fn);
      actions.appendChild(b);
      return b;
    };
    const close = () => { wrap.remove(); style.remove(); };
    const auto = updates.filter((u) => u.source);
    if (auto.length) {
      const install = btn(auto.length === updates.length ? "Yes, install" : `Install ${auto.length} of them`, "btn-primary", async () => {
        install.disabled = true;
        install.textContent = "Installing…";
        try {
          const job = (await gql("mutation($p: [PackageSpecInput!]) { updatePackages(type: Plugin, packages: $p) }",
            { p: auto.map((u) => ({ id: u.id, sourceURL: u.source })) })).updatePackages;
          for (let i = 0; i < 120; i++) { // (up to 4 minutes)
            await new Promise((r) => setTimeout(r, 2000));
            const j = (await gql("query($i: ID!) { findJob(input: { id: $i }) { status error } }", { i: job })).findJob;
            if (!j || j.status === "FINISHED") break;
            if (j.status === "FAILED" || j.status === "CANCELLED") throw new Error(j.error || j.status.toLowerCase());
          }
          await gql("mutation { reloadPlugins }").catch(() => {});
          save({ checked: Date.now() });
          status.textContent = "✓ Updated. Reload the page to use the new versions.";
          install.textContent = "Reload the page";
          install.disabled = false;
          install.onclick = () => location.reload();
        } catch (e) {
          status.textContent = "Couldn't install: " + e.message + " - try Settings → Plugins.";
          install.textContent = "Try again";
          install.disabled = false;
        }
      });
    }
    btn("Later", "btn-secondary", () => { save({ later: Date.now() + LATER }); close(); });
    btn("Don't show again", "btn-secondary", () => { save({ off: true }); close(); });
    box.append(h, list, status, actions);
    wrap.appendChild(box);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) { save({ later: Date.now() + LATER }); close(); } });
    document.head.appendChild(style);
    document.body.appendChild(wrap);
  }

  // a little after Stash has loaded
  setTimeout(() => { check().catch(() => {}); }, 6000);
})();
