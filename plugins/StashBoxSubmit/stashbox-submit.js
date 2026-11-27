// Stash-Box Submit - "Submit to FansDB / StashDB…" in a scene's ⋮ menu.
//
// Opens a check of the scene against that site's own guidelines (FansDB:
// docs.fansdb.cc; StashDB: guidelines.stashdb.org): what the draft will hold,
// what's missing, and the performers / studio / tags matched to the site's own
// entries. You fix what's flagged here, pick the tags you can see in the video,
// and send it as a draft - then finish it on the site (it asks for a comment
// with your sources; one is suggested to copy).
(function () {
  "use strict";
  const api = window.PluginApi;
  if (!api || !api.React) return;
  const React = api.React;
  const h = React.createElement;
  const PLUGIN_ID = "StashBoxSubmit";
  const ITEM_TEXT = "Submit to FansDB / StashDB…";

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
  // (the page and the plugin must match: after an update, a page left open is out of date)
  const PAGE_VERSION = 2;
  async function ask(args) {
    const d = await gql("mutation($p: ID!, $a: Map) { runPluginOperation(plugin_id: $p, args: $a) }", { p: PLUGIN_ID, a: { ...args, page_version: PAGE_VERSION } });
    const out = d.runPluginOperation || {};
    if (out.error) throw new Error(out.error);
    return out;
  }

  const DATE_RE = /^\d{4}(-\d\d(-\d\d)?)?$/;
  const label = (status) => ({ linked: "Linked", found: "Found on the site", maybe: "Check this match", missing: "Not on the site", none: "None" }[status] || status);
  const mark = (ok) => h("span", { className: "sbs-mark " + (ok === true ? "ok" : ok === false ? "bad" : "warn") }, ok === true ? "✓" : ok === false ? "✕" : "!");

  function Modal({ sceneId, onClose, initialSite, onSent }) {
    const [sites, setSites] = React.useState(null);
    const [site, setSite] = React.useState(null);
    const [chk, setChk] = React.useState(null);
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState(false);
    const [done, setDone] = React.useState(null);
    // your choices
    const [title, setTitle] = React.useState("");
    const [date, setDate] = React.useState("");
    const [code, setCode] = React.useState("");
    const [details, setDetails] = React.useState("");
    const [urls, setUrls] = React.useState({});
    const [studio, setStudio] = React.useState(null);
    const [perfs, setPerfs] = React.useState({});
    const [tags, setTags] = React.useState({});
    const [cover, setCover] = React.useState(true);
    const [link, setLink] = React.useState(true);
    const [notDup, setNotDup] = React.useState(false);
    const [perfDrafts, setPerfDrafts] = React.useState({});
    const [studioAdd, setStudioAdd] = React.useState(null); // the studio edit you sent from here
    const [copied, setCopied] = React.useState(false);
    const [confirm, setConfirm] = React.useState(false);  // the "submit the edit straight away" step
    const [comment, setComment] = React.useState("");

    React.useEffect(() => {
      ask({ mode: "sites" }).then((s) => {
        setSites(s);
        const pick = (initialSite && s.find((x) => x.endpoint === initialSite))
          || s.find((x) => x.can_edit && /fansdb/.test(x.endpoint)) || s.find((x) => x.can_edit) || s[0];
        setSite(pick ? pick.endpoint : null);
      }).catch((e) => setErr(e.message));
    }, []);

    const run = React.useRef(0);                         // (a newer check makes an older answer stale)
    const [since, setSince] = React.useState(0);         // when the check started
    const [, tick] = React.useState(0);
    const load = React.useCallback(() => {
      if (!site) return;
      setChk(null); setErr(""); setDone(null);
      const me = ++run.current; setSince(Date.now());
      ask({ mode: "check", scene_id: sceneId, endpoint: site }).then((c) => {
        if (me !== run.current) return;
        setChk(c);
        setTitle(c.title || ""); setDate(c.date || ""); setCode(c.code || ""); setDetails(c.details || "");
        setComment(c.comment || ""); setConfirm(false);
        setUrls(Object.fromEntries((c.urls || []).map((u) => [u.url, u.ok])));
        const pickOf = (m) => (m.box ? m.box : m.candidates && m.candidates.length === 1 ? null : null);
        setStudio(c.studio && c.studio.box ? c.studio.box : c.studio && c.studio.by_name ? { id: null, name: c.studio.by_name, byName: true } : null);
        setPerfs(Object.fromEntries((c.performers || []).map((p) => [p.local.id, p.box || pickOf(p)])));
        setTags({}); // (none ticked: the sites ask for tags you can see, not copied ones)
        setNotDup(false);
        setStudioAdd(null);
      }).catch((e) => { if (me === run.current) setErr(e.message); });
    }, [site, sceneId]);
    React.useEffect(load, [load]);
    const waiting = site && !chk && !err;
    React.useEffect(() => {  // (a seconds counter while the site answers)
      if (!waiting) return undefined;
      const t = setInterval(() => tick((n) => n + 1), 1000);
      return () => clearInterval(t);
    }, [waiting]);

    // what still stops the draft
    const blocks = [];
    if (chk) {
      const fixed = (f) => (f === "title" ? !!title.trim() : f === "date" ? DATE_RE.test(date) || (!date && !/fansdb/.test(site))
        : f === "studio" ? !!studio : f && f.startsWith("performer:") ? !!perfs[f.slice(10)] : f === "duplicates" ? notDup : false);
      chk.issues.forEach((i) => { if ((i.level === "block" || i.level === "dup") && !fixed(i.field)) blocks.push(i); });
      if (!title.trim() && !blocks.some((b) => b.field === "title")) blocks.push({ text: "No title.", field: "title" });
      (chk.performers || []).forEach((p) => {
        if (!perfs[p.local.id] && p.status === "maybe" && !blocks.some((b) => b.field === "performer:" + p.local.id)) {
          blocks.push({ text: `Pick ${p.local.name}'s entry on the site (or check they're not there).`, field: "performer:" + p.local.id });
        }
      });
      if (chk.studio && chk.studio.status === "maybe" && !studio && !blocks.some((b) => b.field === "studio")) {
        blocks.push({ text: "Pick the studio's entry on the site.", field: "studio" });
      }
    }

    const choices = () => ({
      title, date, code, details, cover, link, not_dup: notDup,
      urls: Object.keys(urls).filter((u) => urls[u]),
      studio: studio ? { id: studio.id || null, name: studio.name, local_id: studio.id && chk.studio.local ? chk.studio.local.id : null } : null,
      performers: chk.performers.filter((p) => perfs[p.local.id]).map((p) => ({ id: perfs[p.local.id].id, name: perfs[p.local.id].name, local_id: p.local.id })),
      tags: chk.tags.filter((t) => t.box && tags[t.box.id]).map((t) => ({ id: t.box.id, name: t.box.name })),
    });
    // submitting the edit straight away (FansDB / StashDB with edit rights - ThePornDB takes drafts only):
    // only when the studio and every performer are on the site
    const editSite = /fansdb|stashdb/.test(site || "") && chk && chk.site.can_edit !== false;
    const editWhy = !chk ? "" : blocks.length ? "" : !studio || !studio.id || studio.pending || studio.byName ? "the studio has to be on the site first"
      : chk.performers.some((p) => !perfs[p.local.id] || !perfs[p.local.id].id) ? "every performer has to be on the site first" : "";
    async function submitEdit() {
      setBusy(true); setErr("");
      try {
        const out = await ask({ mode: "scene_edit", scene_id: sceneId, endpoint: site, comment, choices: choices() });
        setDone({ ...out, edit: true }); setConfirm(false);
        if (onSent) onSent(site, out.url);
      } catch (e) {
        setErr(e.message);
      } finally {
        setBusy(false);
      }
    }

    async function send() {
      setBusy(true); setErr("");
      try {
        const out = await ask({ mode: "submit", scene_id: sceneId, endpoint: site, choices: choices() });
        setDone(out);
        ask({ mode: "mark_sent", scene_id: sceneId, endpoint: site, url: out.url }).catch(() => {});
        if (onSent) onSent(site, out.url);
      } catch (e) {
        setErr(e.message);
      } finally {
        setBusy(false);
      }
    }

    async function addStudio() {
      setStudioAdd({ busy: true });
      try {
        // (the studio of the account the video came from, when that's the one proposed)
        const local = chk.studio.local && chk.studio.local.id;
        const out = await ask({ mode: "studio", endpoint: site, studio_id: local || null, account_url: local ? null : chk.account && chk.account.url });
        setStudioAdd(out);
        // there already, or accepted straight away: link it; waiting for votes: the draft names it
        if (out.studio && out.studio.id) setStudio({ id: out.studio.id, name: out.studio.name });
        else setStudio({ id: null, name: out.name, pending: true });
      } catch (e) {
        setStudioAdd({ error: e.message });
      }
    }

    async function addLink(p) {
      setPerfDrafts((d) => ({ ...d, [p.local.id]: { busy: true } }));
      try {
        await ask({ mode: "add_link", performer_id: p.local.id, url: p.proposal.add_link });
        load(); // (check again: now they have the link)
      } catch (e) {
        setPerfDrafts((d) => ({ ...d, [p.local.id]: { error: e.message } }));
      }
    }

    async function perfDraft(p) {
      setPerfDrafts((d) => ({ ...d, [p.local.id]: { busy: true } }));
      try {
        const out = await ask({ mode: "performer", performer_id: p.local.id, endpoint: site });
        setPerfDrafts((d) => ({ ...d, [p.local.id]: out }));
      } catch (e) {
        setPerfDrafts((d) => ({ ...d, [p.local.id]: { error: e.message } }));
      }
    }

    const siteName = chk ? chk.site.name : (sites && sites.find((s) => s.endpoint === site) || {}).name || "";
    const body = [];
    if (err) body.push(h("div", { key: "err", className: "alert alert-danger" }, err, " ",
      h("button", { type: "button", className: "btn btn-sm btn-light", onClick: load }, "Try again")));
    if (sites && !sites.length) {
      body.push(h("p", { key: "nosite" }, "Set up FansDB or StashDB (with your API key) under Settings → Metadata Providers → Stash-box Endpoints first."));
    }
    if (sites && sites.length) {
      body.push(h("div", { key: "sites", className: "sbs-sites" }, sites.map((s) => h("button", {
        key: s.endpoint, type: "button", className: "btn btn-sm " + (s.endpoint === site ? "btn-primary" : "btn-secondary"),
        onClick: () => setSite(s.endpoint), disabled: busy,
      }, s.name, s.can_edit === false ? " (read-only)" : ""))));
    }
    if (chk && chk.sent && !done) {
      const when = new Date(chk.sent.at * 1000).toLocaleString(), st = chk.sent.state;
      body.push(h("div", { key: "sent-before", className: "sbs-ready sbs-sent-before" }, mark(st === "gone" ? null : true),
        st && typeof st === "object" ? h(React.Fragment, null, ` You submitted this on ${siteName} - it's waiting for votes (${st.yes} yes, ${st.no} no`
          + (st.expires ? `, until ${closes(st.expires)}` : "") + "). ", h("a", { href: st.edit_url, target: "_blank", rel: "noopener" }, "Open the edit"), " - no need to send it again.",
          st.extra_draft ? h(React.Fragment, null, " A later draft of it is also waiting - ", h("a", { href: st.extra_draft, target: "_blank", rel: "noopener" }, "that draft"),
            " can be deleted there (or it goes by itself after 24 hours).") : null)
        : st === "draft" ? h(React.Fragment, null, ` You sent a draft of this on ${when}. `, h("a", { href: chk.sent.url, target: "_blank", rel: "noopener" }, "Open that draft"),
          " - finish it there; sending again makes a second draft.")
        : st === "gone" ? h(React.Fragment, null, ` You sent a draft of this on ${when}, but it's no longer waiting on ${siteName} - it expired (24 hours), was deleted, `
          + "or was submitted and has been voted on already. If the scene isn't on the site, send it again.")
        : h(React.Fragment, null, ` You sent this to ${siteName} on ${when}. `, h("a", { href: chk.sent.url, target: "_blank", rel: "noopener" }, "Open it"), ".")));
    }
    if (waiting) {
      const secs = Math.round((Date.now() - since) / 1000);
      body.push(h("div", { key: "loading", className: "sbs-wait" },
        h("span", { className: "spinner-border spinner-border-sm" }), " ",
        `Checking against ${siteName}'s guidelines… ${secs}s`,
        secs >= 15 ? h("p", { className: "sbs-muted" }, `${siteName} is answering slowly right now (it looks up the studio, performers and tags there) - this can take up to a minute.`) : null,
        secs >= 90 ? h("button", { type: "button", className: "btn btn-sm btn-secondary", onClick: load }, "Still waiting - try again") : null));
    }

    if (done && done.edit) {
      body.push(h("div", { key: "done", className: "sbs-done" },
        h("p", null, mark(true), ` Submitted to ${siteName} - it's waiting for other users' votes (3 yes votes accept it straight away; otherwise it's decided when voting ends).`),
        h("div", { className: "sbs-review-links" }, extLink(done.url, "Open the edit", "btn-primary")),
        h("p", { className: "sbs-muted" }, "It's in Pending on the Stash-Box Submit page with its votes.")));
    } else if (done) {
      body.push(h("div", { key: "done", className: "sbs-done" },
        h("p", null, mark(true), ` Draft sent to ${siteName}. It's only visible to you, and the site deletes it after 24 hours unless you submit it.`),
        h("div", { className: "sbs-review-links" },
          extLink(done.url, `Review the draft on ${siteName}`, "btn-primary"),
          /theporndb/.test(site || "") ? null : extLink(siteBase(site) + "/drafts", `All your drafts on ${siteName}`)),
        h("p", { className: "sbs-muted" }, "On the site: check everything, then on the Confirm tab paste a comment with your sources, and Submit Edit."),
        h("label", null, "Suggested comment"),
        h("textarea", { className: "form-control", rows: 3, readOnly: true, value: chk.comment }),
        h("button", { type: "button", className: "btn btn-secondary btn-sm", onClick: () => {
          navigator.clipboard && navigator.clipboard.writeText(chk.comment).then(() => setCopied(true));
        } }, copied ? "Copied" : "Copy comment")));
    } else if (chk) {
      // problems first
      const shown = chk.issues.filter((i) => blocks.includes(i) || i.level === "warn");
      if (blocks.length || shown.length) {
        body.push(h("ul", { key: "issues", className: "sbs-issues" },
          blocks.map((b, i) => h("li", { key: "b" + i, className: "bad" }, mark(false), " ", b.text)),
          chk.issues.filter((i) => i.level === "warn").map((w, i) => h("li", { key: "w" + i, className: "warn" }, mark(null), " ", w.text))));
      } else {
        body.push(h("p", { key: "ready", className: "sbs-ready" }, mark(true), ` Ready to send to ${siteName}.`));
      }
      if ((chk.waiting || []).length) {
        body.push(h("div", { key: "waiting", className: "sbs-section" },
          h("h6", null, "Already submitted, waiting for votes"),
          h("ul", null, chk.waiting.map((w) => h("li", { key: w.url }, h("a", { href: w.url, target: "_blank", rel: "noopener" }, w.title || "the edit"), w.by ? ` - by ${w.by}` : ""))),
          chk.duplicates.length ? null : h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: notDup, onChange: (e) => setNotDup(e.target.checked) }),
            " I've looked: it's a different video")));
      }
      if (chk.duplicates.length) {
        body.push(h("div", { key: "dups", className: "sbs-section" },
          h("h6", null, "Same fingerprint on the site"),
          h("ul", null, chk.duplicates.map((d) => h("li", { key: d.id }, h("a", { href: d.url, target: "_blank", rel: "noopener" }, d.title || d.id),
            ` - ${d.studio || ""} ${d.date || ""}`))),
          h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: notDup, onChange: (e) => setNotDup(e.target.checked) }),
            " I've looked: none of these is this video")));
      }
      // fields
      body.push(h("div", { key: "fields", className: "sbs-section" },
        h("label", null, "Title"),
        h("input", { className: "form-control", value: title, onChange: (e) => setTitle(e.target.value.replace(/\s*\n\s*/g, " ")) }),
        h("div", { className: "sbs-row" },
          h("div", null, h("label", null, "Date"), h("input", { className: "form-control" + (date && !DATE_RE.test(date) ? " is-invalid" : ""), value: date, placeholder: "YYYY-MM-DD", onChange: (e) => setDate(e.target.value.trim()) })),
          h("div", null, h("label", null, "Studio code"), h("input", { className: "form-control", value: code, onChange: (e) => setCode(e.target.value) }))),
        h("label", null, "Details"),
        h("textarea", { className: "form-control", rows: 4, value: details, onChange: (e) => setDetails(e.target.value) }),
        h("label", null, "Links"),
        chk.urls.length ? chk.urls.map((u) => h("label", { key: u.url, className: "sbs-check" + (u.ok ? "" : " sbs-muted") },
          h("input", { type: "checkbox", checked: !!urls[u.url], disabled: !u.ok, onChange: (e) => setUrls((x) => ({ ...x, [u.url]: e.target.checked })) }),
          " ", u.url, u.ok ? (u.site ? ` (${u.site})` : "") : " - not a site the database knows, so left out")) : h("p", { className: "sbs-muted" }, "No links on the scene.")));
      // studio and performers
      const entity = (key, m, chosen, setChosen, extra) => h("div", { key, className: "sbs-entity" },
        mark(chosen ? true : m.status === "maybe" ? null : false),
        h("b", null, " ", m.local ? m.local.name : "(none)"), " → ",
        chosen ? h("a", { href: `${chk.site.base}/${key.startsWith("st") ? "studios" : "performers"}/${chosen.id}`, target: "_blank", rel: "noopener" }, chosen.name, chosen.disambiguation ? ` (${chosen.disambiguation})` : "")
          : h("span", { className: "sbs-muted" }, label(m.status)),
        m.why && m.status !== "missing" ? h("span", { className: "sbs-muted" }, ` - ${m.why}`) : null,
        m.status === "maybe" ? h("select", { className: "form-control form-control-sm", value: chosen ? chosen.id : "",
          onChange: (e) => setChosen((m.candidates || []).find((c) => c.id === e.target.value) || null) },
          h("option", { value: "" }, "Pick their entry…"),
          (m.candidates || []).map((c) => h("option", { key: c.id, value: c.id }, c.name + (c.disambiguation ? ` (${c.disambiguation})` : "")))) : null,
        extra);
      body.push(h("div", { key: "who", className: "sbs-section" },
        h("h6", null, "Studio"),
        entity("st", chk.studio, studio && !studio.pending && !studio.byName ? studio : null, setStudio,
          chk.studio.by_name ? h("span", { className: "sbs-muted" }, ` - sent by name ("${chk.studio.by_name}") for ${siteName}'s team to add`)
            : chk.studio.status === "missing" && !chk.studio.proposal ? h("a", { className: "btn btn-sm btn-secondary", href: `${chk.site.base}/studios/add`, target: "_blank", rel: "noopener" }, `Add it on ${siteName}`) : null),
        chk.studio.status === "missing" && chk.studio.proposal ? (() => {
          const pr = chk.studio.proposal;
          const a = studioAdd || {};
          if (a.edit || a.status === "exists") {
            const live = a.studio && a.studio.id;
            return h("div", { className: "sbs-propose" },
              h("p", null, mark(live ? true : null), " ", live ? `${a.studio.name} is on ${siteName} - linked.`
                : `Studio edit sent (${String(a.status || "").toLowerCase()}): it's added once ${siteName}'s users approve it. `
                  + "Until then the draft names the studio, and you pick it on the draft page once it's there."),
              a.edit_url ? h("a", { href: a.edit_url, target: "_blank", rel: "noopener" }, "See the studio edit") : null);
          }
          return h("div", { className: "sbs-propose" },
            h("p", null, `Add it to ${siteName} the way its guidelines name accounts:`),
            h("ul", null,
              h("li", null, "Name: ", h("b", null, pr.name)),
              h("li", null, "Network: ", pr.parent.name),
              h("li", null, "Link: ", pr.url, ` (${pr.site})`),
              h("li", null, "Edit comment: ", h("span", { className: "sbs-muted" }, pr.comment))),
            h("p", { className: "sbs-muted" }, `This is a public edit on ${siteName} under your account; its users vote on it.`),
            h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: a.busy, onClick: addStudio },
              a.busy ? "Sending…" : `Submit studio to ${siteName} (public edit)`),
            a.error ? h("span", { className: "text-danger" }, " ", a.error) : null);
        })() : null,
        h("h6", null, "Performers"),
        chk.performers.length ? chk.performers.map((p) => entity("pf" + p.local.id, p, perfs[p.local.id], (v) => setPerfs((x) => ({ ...x, [p.local.id]: v })),
          null)).concat(chk.performers.filter((p) => p.status === "missing" && p.proposal).map((p) => {
            // a performer the site doesn't have: what their draft would hold, and what's still needed
            const pr = p.proposal;
            const d = perfDrafts[p.local.id];
            return h("div", { key: "pp" + p.local.id, className: "sbs-propose" },
              h("p", null, `${p.local.name} isn't on ${siteName}. A performer draft would hold:`),
              h("ul", null,
                h("li", null, "Name: ", h("b", null, pr.name), pr.disambiguation ? ` (${pr.disambiguation})` : "",
                  pr.name_note ? h("span", { className: "sbs-muted" }, ` - ${pr.name_note}`) : null),
                h("li", null, "Gender: ", pr.gender || h("span", { className: "text-danger" }, "not set")),
                h("li", null, "Aliases: ", pr.aliases.length ? pr.aliases.join(", ") : "none"),
                h("li", null, "Links: ", pr.links.length ? pr.links.join(", ") : h("span", { className: "text-danger" }, "none the site knows")),
                pr.left_out.length ? h("li", { className: "sbs-muted" }, `Left out (not sites ${siteName} lists for performers): ${pr.left_out.join(", ")}`) : null,
                h("li", null, "Picture: ", pr.picture ? "theirs from Stash" : "none")),
              pr.needs.length ? h(React.Fragment, null,
                h("p", { className: "text-danger" }, "First add " + pr.needs.join(" and ") + "."),
                pr.add_link ? h("button", { type: "button", className: "btn btn-sm btn-secondary", disabled: d && d.busy, onClick: () => addLink(p) },
                  d && d.busy ? "Adding…" : `Add ${pr.add_link} to ${p.local.name} in my Stash`) : null)
                : d && d.edit ? h(React.Fragment, null,
                  h("p", null, mark(true), ` ${p.local.name} is submitted to ${siteName} - waiting for votes. Once accepted, check this scene again to link them.`),
                  h("a", { className: "btn btn-sm btn-secondary", href: d.url, target: "_blank", rel: "noopener" }, "Open the edit"))
                : d && d.url ? h("a", { className: "btn btn-sm btn-primary", href: d.url, target: "_blank", rel: "noopener" }, "Open their draft")
                  : h(React.Fragment, null,
                    h("p", { className: "sbs-muted" }, editable(site)
                      ? `Submit them to ${siteName} from here (a public edit, waiting for votes), or send a private draft to finish on the site. Once they're accepted, check this scene again to link them.`
                      : `A private draft: you check it and submit it on ${siteName}. Once it's approved, check this scene again to link them.`),
                    h("div", { className: "sbs-person-act sbs-act-left" },
                      editable(site) && chk.site.can_edit !== false ? h(SubmitNow, { site: siteName, what: p.local.name,
                        defaultComment: `New performer from their own pages: ${pr.links.slice(0, 3).join(" , ")}. In a scene I'm submitting.`,
                        onSubmit: async (comment) => {
                          const out = await ask({ mode: "performer_edit", endpoint: site, performer_id: p.local.id, comment });
                          setPerfDrafts((x) => ({ ...x, [p.local.id]: { ...out, edit: true } }));
                        } }) : null,
                      h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: d && d.busy, onClick: () => perfDraft(p) },
                        d && d.busy ? "Sending…" : `Send ${p.local.name}'s draft to ${siteName}`))),
              d && d.error ? h("span", { className: "text-danger" }, d.error) : null);
          })) : h("p", { className: "sbs-muted" }, "No performers on the scene."),
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: link, onChange: (e) => setLink(e.target.checked) }),
          ` Remember these matches in my Stash (links them to ${siteName})`)));
      // tags
      const matched = chk.tags.filter((t) => t.box);
      const unmatched = chk.tags.filter((t) => !t.box);
      body.push(h("div", { key: "tags", className: "sbs-section" },
        h("h6", null, "Tags"),
        h("p", { className: "sbs-muted" }, `Tick only what you can see in the video - ${siteName} asks for tags not to be copied blindly.`),
        matched.length ? h("div", { className: "sbs-tags" }, matched.map((t) => h("label", { key: t.box.id, className: "sbs-check" },
          h("input", { type: "checkbox", checked: !!tags[t.box.id], onChange: (e) => setTags((x) => ({ ...x, [t.box.id]: e.target.checked })) }),
          " ", t.box.name, t.box.name !== t.local ? h("span", { className: "sbs-muted" }, ` (yours: ${t.local})`) : null)))
          : h("p", { className: "sbs-muted" }, "None of this scene's tags are on the site."),
        unmatched.length ? h("p", { className: "sbs-muted" }, `Not on ${siteName} (left out): ${unmatched.map((t) => t.local).join(", ")}`) : null));
      // cover
      body.push(h("div", { key: "cover", className: "sbs-section" },
        h("h6", null, "Cover"),
        chk.cover ? h("img", { className: "sbs-cover", src: here(chk.cover), alt: "" }) : null,
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: cover, onChange: (e) => setCover(e.target.checked) }),
          " Send this cover (no third-party watermarks; the studio's own cover is preferred)")));
      // the site's rules
      body.push(h("details", { key: "rules", className: "sbs-section" },
        h("summary", null, `${siteName}'s rules (from its guidelines)`),
        h("ul", null, chk.site.rules.map((r, i) => h("li", { key: i }, r)))));
      chk.notes.forEach((n, i) => body.push(h("p", { key: "n" + i, className: "sbs-muted" }, n)));
    }

    return h("div", { className: "sbs-backdrop", onClick: (e) => { if (e.target === e.currentTarget && !busy) onClose(); } },
      h("div", { className: "sbs-modal modal-content", role: "dialog", "aria-modal": "true", "aria-label": "Submit to a stash-box" },
       h("div", { className: "modal-body sbs-modal-in" },
        h("div", { className: "sbs-head" }, h("h5", null, "Submit to ", siteName || "a stash-box"),
          h("button", { type: "button", className: "close", "aria-label": "Close", onClick: onClose }, "×")),
        h("div", { className: "sbs-body" }, ...body),
        !done && chk ? h("p", { className: "sbs-foot-note" }, mark(null),
          ` The draft is only visible to you and ${siteName} deletes it after 24 hours - finish it there (tags you can see, a comment with your sources, Submit Edit).`
          + (chk.site.can_edit === false ? ` Your ${siteName} account is read-only, so it can't be submitted yet.` : "")
          + (/theporndb/.test(site || "") ? " ThePornDB: basic checks only - check the draft carefully." : "")) : null,
        !done && chk && confirm ? h("div", { className: "sbs-confirm" },
          h("h6", null, `Submit to ${siteName} now`),
          h("p", { className: "sbs-muted" }, `This submits the scene as a public edit under your ${siteName} account - no draft to finish. Other users vote on it.`),
          h("label", null, "Edit comment (where the details come from - the site asks for one)"),
          h("textarea", { className: "form-control", rows: 3, value: comment, onChange: (e) => setComment(e.target.value) }),
          h("p", { className: "sbs-muted" }, (() => { const n = chk.tags.filter((t) => t.box && tags[t.box.id]).length;
            return n ? `${plural(n, "tag")} ticked.` : "No tags ticked - tick the ones you can see in the video above, or add them on the site later."; })()
            + (cover ? " With the cover." : " Without a cover.")),
          h("div", { className: "sbs-confirm-btns" },
            h("button", { type: "button", className: "btn btn-secondary", onClick: () => setConfirm(false), disabled: busy }, "Back"),
            h("button", { type: "button", className: "btn btn-success", onClick: submitEdit, disabled: busy || !comment.trim() },
              busy ? "Submitting…" : "Submit edit"))) : null,
        !done && chk && !confirm ? h("div", { className: "sbs-foot" },
          h("button", { type: "button", className: "btn btn-secondary", onClick: load, disabled: busy }, "Check again"),
          editSite && !blocks.length ? h("button", { type: "button", className: "btn btn-success", onClick: () => setConfirm(true), disabled: busy || !!editWhy,
            title: editWhy ? `Can't submit straight away: ${editWhy} (a draft can still be sent)` : "Submit the edit now - no draft to finish on the site" },
            editWhy ? `Submit now: ${editWhy}` : `Submit to ${siteName}…`) : null,
          h("button", { type: "button", className: "btn btn-primary", onClick: send, disabled: busy || blocks.length > 0 },
            busy ? "Sending…" : blocks.length ? `${blocks.length} thing${blocks.length === 1 ? "" : "s"} to fix first` : `Send draft to ${siteName}`))
          : null)));
  }

  // the ⋮ menu item (Stash doesn't let plugins add to that menu, so it's put in
  // whenever the menu appears)
  function MenuItem({ scene }) {
    const [open, setOpen] = React.useState(false);
    React.useEffect(() => {
      if (!scene) return undefined;
      const place = () => {
        const toggle = document.getElementById("operation-menu");
        const menu = toggle && toggle.parentElement.querySelector(".dropdown-menu");
        if (!menu || menu.querySelector(".sbs-item")) return;
        const item = document.createElement("a");
        item.href = "#";
        item.setAttribute("role", "button");
        item.className = "dropdown-item bg-secondary text-white sbs-item";
        item.textContent = ITEM_TEXT;
        item.addEventListener("click", (e) => { e.preventDefault(); toggle.click(); setOpen(true); });
        // next to Stash's own "Submit to Stash-box"
        const items = [...menu.querySelectorAll(".dropdown-item")];
        const near = items.find((el) => /stash-?box/i.test(el.textContent));
        if (near) near.after(item);
        else if (items.length) items[items.length - 1].before(item);
        else menu.appendChild(item);
      };
      place();
      const obs = new MutationObserver(place);
      obs.observe(document.body, { childList: true, subtree: true });
      return () => { obs.disconnect(); document.querySelectorAll(".sbs-item").forEach((el) => el.remove()); };
    }, [scene && scene.id]);
    return open ? h(Modal, { sceneId: scene.id, onClose: () => setOpen(false) }) : null;
  }

  // ---- the Ready to submit page ------------------------------------------------
  const PAGE = "/plugins/stashbox-submit";
  const goTo = (path) => { window.history.pushState({}, "", path); window.dispatchEvent(new PopStateEvent("popstate")); };

  // "Send drafts for all ready scenes" (setting: bulkSend): up to 10 per click, one site,
  // only scenes the check passes with nothing to decide; no tags (add them on each draft)
  const BULK_MAX = 10;
  // the final review happens on each site: buttons to your drafts there and to your edits
  const siteBase = (endpoint) => String(endpoint || "").replace(/\/graphql\/?$/, "");
  const left = (iso) => {
    const ms = new Date(iso).getTime() - Date.now();
    if (!(ms > 0)) return "expired";
    const hrs = Math.floor(ms / 3600000), min = Math.floor((ms % 3600000) / 60000);
    return hrs ? `${hrs} h ${min} min left` : `${min} min left`;
  };
  const extLink = (href, text, cls) => h("a", { className: "btn btn-sm " + (cls || "btn-secondary"), href, target: "_blank", rel: "noopener" }, text);
  // Stash gives the plugin its own address (localhost) in picture links - use the one this browser is on
  const here = (u) => String(u || "").replace(/^https?:\/\/[^/]+(?=\/(scene|performer|studio)\/)/, "");

  const closes = (iso) => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }); };
  // whether the scene's studio is on the site already (a studio that isn't has to be added first)
  const studioText = (v) => {
    const st = v.studio_site || (v.studio_linked ? "linked" : "");
    if (st === "linked") return h(React.Fragment, null, mark(true), " studio linked");
    if (st === "found") return h(React.Fragment, null, mark(true), ` studio on ${v.name}` + (v.studio_site_name ? `: ${v.studio_site_name}` : ""));
    if (st === "maybe") return h(React.Fragment, null, mark(null), ` studio: check the match on ${v.name}`);
    if (st === "pending") return h(React.Fragment, null, mark(null), ` studio waiting for votes on ${v.name}`
      + (v.studio_edit && v.studio_edit.expires ? ` (until ${closes(v.studio_edit.expires)})` : ""));
    if (st === "missing" && /ThePornDB/.test(v.name)) return h(React.Fragment, null, mark(null), " studio not on ThePornDB - sent by name");
    if (st === "missing") return h(React.Fragment, null, mark(false), ` studio not on ${v.name} yet`);
    if (st === "none") return "no studio";
    return "studio not linked";
  };

  const WARN_KEY = "sbs-warnings-closed";
  function Warnings({ data, bulkOn }) {
    const [open, setOpen] = React.useState(() => { try { return localStorage.getItem(WARN_KEY) !== "1"; } catch (e) { return true; } });
    const has = (rx) => (data ? data.sites : []).some((x) => rx.test(x.endpoint));
    const items = [
      "Drafts are only visible to you, and the site deletes them after 24 hours. Finish each one there: check it, add the tags you can see in the video, then on the Confirm tab paste a comment with your sources and Submit Edit.",
      bulkOn ? "Bulk drafts are sent without tags - add them on each draft (the sites ask for tags you can see, not ones copied from elsewhere). Send only as many as you can finish within a day." : null,
      has(/stashdb/) ? "StashDB asks new contributors not to submit 20+ edits at once, and to keep only a few pending at a time." : null,
      "\"Already on …\" is a match by fingerprint - almost always the same video, but some broken files share one. Open it and check before linking with Stash's Tagger.",
      has(/theporndb/) ? "ThePornDB: its guidelines couldn't be read and its API has fewer lookups, so only the basic checks run (no tags; studios matched by name). Check those drafts carefully on the site." : null,
      ...(data ? data.sites.filter((x) => x.can_edit === false).map((x) => `Your ${x.name} account is read-only: drafts can be made there but not submitted`
        + (/stashdb/.test(x.endpoint) ? " (StashDB gives edit rights on request - guidelines.stashdb.org)." : ".")) : []),
      "Submissions are public once you submit them on the site, under your account.",
    ].filter(Boolean);
    const toggle = (e) => {
      const o = e.target.open; setOpen(o);
      try { localStorage.setItem(WARN_KEY, o ? "0" : "1"); } catch (err) { /* fine */ }
    };
    return h("details", { className: "sbs-warn", open, onToggle: toggle },
      h("summary", null, mark(null), " Before you submit"),
      h("ul", null, items.map((t, i) => h("li", { key: i }, t))));
  }

  function BulkBar({ data, rows, only, bulk, setBulk, setData, onDone }) {
    const site = data.sites.find((s) => s.endpoint === only);
    if (!site) return h("p", { className: "sbs-muted sbs-bulk" }, "To send drafts in bulk, pick a site above first.");
    if (site.can_edit === false) return h("p", { className: "sbs-muted sbs-bulk" }, `Your ${site.name} account can't submit, so there's nothing to send in bulk.`);
    const todo = rows.filter(({ sc }) => { const v = sc.sites[only]; return v && !v.on_site && !v.sent && !v.missing.length; }).map(({ sc }) => sc);
    const run = async () => {
      if (!window.confirm(`Send up to ${Math.min(BULK_MAX, todo.length)} drafts to ${site.name}?\n\n`
        + "Only scenes whose check passes with nothing to decide are sent, with no tags. Each draft is only visible to you, "
        + `and ${site.name} deletes it after 24 hours unless you finish it there (tags, a comment with your sources, Submit Edit).`)) return;
      const st = { running: true, sent: [], skipped: [], at: 0 };
      setBulk({ ...st });
      for (const sc of todo) {
        if (st.sent.length >= BULK_MAX) break;
        st.at += 1; setBulk({ ...st });
        try {
          const c = await ask({ mode: "check", scene_id: sc.id, endpoint: only });
          const why = [];
          c.issues.filter((i) => i.level === "block" || i.level === "dup").forEach((i) => why.push(i.text));
          const said = (f) => c.issues.some((i) => i.field === f && (i.level === "block" || i.level === "dup"));
          if (!["linked", "found"].includes(c.studio.status) && !c.studio.by_name && !said("studio")) why.push(`studio: ${label(c.studio.status).toLowerCase()}`);
          c.performers.filter((p) => !["linked", "found"].includes(p.status) && !said("performer:" + p.local.id))
            .forEach((p) => why.push(`${p.local.name}: ${label(p.status).toLowerCase()}`));
          if (why.length) { st.skipped.push({ sc, why: [...new Set(why)].join("; ") }); continue; }
          const out = await ask({ mode: "submit", scene_id: sc.id, endpoint: only, choices: {
            title: c.title, date: c.date, code: c.code, details: c.details, cover: true, link: true,
            urls: c.urls.filter((u) => u.ok).map((u) => u.url),
            studio: c.studio.box ? { id: c.studio.box.id, name: c.studio.box.name, local_id: c.studio.local && c.studio.local.id }
              : { id: null, name: c.studio.by_name },  // (ThePornDB: a studio it doesn't have yet, by name)
            performers: c.performers.map((p) => ({ id: p.box.id, name: p.box.name, local_id: p.local.id })), tags: [] } });
          await ask({ mode: "mark_sent", scene_id: sc.id, endpoint: only, url: out.url }).catch(() => {});
          st.sent.push({ sc, url: out.url, comment: c.comment });
          setData((d) => ({ ...d, scenes: d.scenes.map((x) => x.id !== sc.id ? x : { ...x, sites: { ...x.sites, [only]: { ...x.sites[only], sent: { url: out.url } } } }) }));
        } catch (e) {
          st.skipped.push({ sc, why: e.message });
        }
        setBulk({ ...st });
      }
      st.running = false; setBulk({ ...st }); if (onDone) onDone();
    };
    return h("div", { className: "sbs-bulk" },
      bulk && bulk.running ? h("p", null, `Sending to ${site.name}… checked ${bulk.at} · sent ${bulk.sent.length} · skipped ${bulk.skipped.length}`)
        : h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !todo.length, onClick: run },
          todo.length ? `Send drafts for ready scenes to ${site.name} (up to ${Math.min(BULK_MAX, todo.length)} of ${todo.length})` : `No scenes ready for ${site.name}`),
      bulk && !bulk.running ? h("div", { className: "sbs-bulk-report" },
        h("p", null, mark(true), ` Sent ${bulk.sent.length} draft(s)` + (bulk.skipped.length ? ` · skipped ${bulk.skipped.length}` : "")
          + ". Finish each on the site within 24 hours: add tags you can see, then on the Confirm tab paste a comment (suggested below) and Submit Edit."),
        /theporndb/.test(only) ? null : h("p", null, extLink(siteBase(only) + "/drafts", `All your drafts on ${site.name}`)),
        bulk.sent.map((x) => h("div", { key: x.sc.id, className: "sbs-site-row" }, extLink(x.url, "Review draft", "btn-primary"), h("span", null, x.sc.title || x.sc.id),
          h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => navigator.clipboard && navigator.clipboard.writeText(x.comment) }, "copy comment"))),
        bulk.skipped.map((x) => h("div", { key: "s" + x.sc.id, className: "sbs-site-row sbs-muted" }, mark(false), ` ${x.sc.title || x.sc.id}: ${x.why}`))) : null);
  }

  // "Studios to add": the studios ready scenes wait for on FansDB, and studios missing in your Stash
  const STUDIOS_PAGE = PAGE + "/studios";
  const stashLink = (href, text) => h("a", { href, onClick: (e) => { e.preventDefault(); goTo(href); } }, text);
  // a scene on a site: waiting there (a draft to finish, or an edit waiting for votes) - then it's in
  // Pending, not Scenes. "gone": it was sent but isn't waiting any more (expired, or voted on).
  function waitingState(pending, e, v, title) {
    const p = pending && pending.sites.find((x) => x.endpoint === e);
    const same = (x) => x.kind === "scene" && title && x.name.trim().toLowerCase() === title.trim().toLowerCase();
    const ed = p && p.listed ? (p.edits || []).find(same) : null;
    if (ed) return ed;                                 // (submitted - also when it was done on the site)
    const sameDraft = p && p.listed && title && p.drafts.some((d) => d.kind === "scene" && d.title.trim().toLowerCase() === title.trim().toLowerCase());
    if (!v.sent) return sameDraft ? "draft" : null;    // (a draft made elsewhere, e.g. Stash's own button)
    if (/\/edits\//.test(v.sent.url || "")) {           // (submitted from here: waiting while voting runs - 5 days)
      const mine = p && p.listed ? (p.edits || []).find((x) => x.edit_url === v.sent.url) : null;
      return mine || (Date.now() / 1000 - (v.sent.at || Date.now() / 1000) < 6 * 86400 ? { edit_url: v.sent.url, yes: 0, no: 0 } : "gone");
    }
    if (!p) return "draft";                            // (not known yet: assume it's still waiting)
    if (!p.listed) return Date.now() / 1000 - (v.sent.at || 0) < 86400 ? "draft" : "gone";
    return p.drafts.some((d) => d.url === v.sent.url) ? "draft" : "gone";
  }
  const isWaiting = (pending, e, v, title) => { const st = waitingState(pending, e, v, title); return !!st && st !== "gone"; };
  const until = (iso) => {
    const ms = new Date(iso).getTime() - Date.now();
    if (!(ms > 0)) return "any time now";
    const d = Math.floor(ms / 86400000), hrs = Math.floor((ms % 86400000) / 3600000);
    return d ? `in ${d} day${d === 1 ? "" : "s"}${hrs ? ` ${hrs} h` : ""}` : left(iso).replace(" left", "");
  };

  // ---- the page: sections (Scenes, Performers, Studios, Pending), each filtered by site ----
  const SECTIONS = [["scenes", "Scenes"], ["performers", "Performers"], ["studios", "Studios"], ["pending", "Pending"]];
  const siteKey = (s) => String((s && s.name) || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  function readQuery() {
    const q = new URLSearchParams(window.location.search);
    const old = /\/studios\/?$/.test(window.location.pathname);  // (the old Studios to add address)
    return { section: q.get("section") || (old ? "studios" : "scenes"), site: q.get("site") || "" };
  }
  function writeQuery(section, site) {
    const q = new URLSearchParams();
    if (section && section !== "scenes") q.set("section", section);
    if (site) q.set("site", site);
    const s = q.toString();
    window.history.replaceState(window.history.state, "", PAGE + (s ? "?" + s : ""));
  }
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const ext = (href, text) => h("a", { href, target: "_blank", rel: "noopener" }, text);
  // a counter while something slow loads
  function Waiting({ text, note }) {
    const [since] = React.useState(Date.now());
    const [, tick] = React.useState(0);
    React.useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
    const secs = Math.round((Date.now() - since) / 1000);
    return h("div", { className: "sbs-wait" }, h("span", { className: "spinner-border spinner-border-sm" }), ` ${text} ${secs}s`,
      note && secs >= 10 ? h("p", { className: "sbs-muted" }, note) : null);
  }

  function Hub() {
    const start = readQuery();
    const [section, setSection] = React.useState(SECTIONS.some(([k]) => k === start.section) ? start.section : "scenes");
    const [siteName, setSiteName] = React.useState(start.site);   // "" = all sites
    const [data, setData] = React.useState(null);
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState(false);
    const [tick, setTick] = React.useState(0);                     // something was sent: refresh Pending
    const [onlyNew, setOnlyNew] = React.useState(false);           // setting: onlyNew
    const [bulkOn, setBulkOn] = React.useState(false);             // setting: bulkSend
    // each section's data, kept while you switch between them
    const [perf, setPerf] = React.useState(null);
    const [studios, setStudios] = React.useState(null);
    const [pending, setPending] = React.useState(null);
    const [perfDone, setPerfDone] = React.useState({});      // performers linked / sent this visit
    const [studioDone, setStudioDone] = React.useState({});  // studios sent this visit
    React.useEffect(() => {
      gql("{ configuration { plugins } }").then((d) => {
        const c = ((d.configuration || {}).plugins || {}).StashBoxSubmit || {};
        setOnlyNew(!!c.onlyNew); setBulkOn(!!c.bulkSend);
      }).catch(() => {});
    }, []);
    const [job, setJob] = React.useState(null);  // {id, progress} while Check again runs as a task
    const load = (fresh) => {
      if (fresh) return checkAgain();
      setBusy(true); setErr("");
      ask({ mode: "ready" }).then(setData).catch((e) => setErr(e.message)).finally(() => setBusy(false));
    };
    // Check again runs as Stash's task "Find scenes ready to submit": a big library takes a while, so it
    // runs in the background (Settings -> Tasks shows it too) and this page follows its progress
    const follow = (id) => {
      setBusy(true); setJob({ id, progress: 0 });
      try { sessionStorage.setItem("sbs-job", id); } catch (e) { /* fine */ }
      const tick = () => gql("query($i: FindJobInput!) { findJob(input: $i) { status progress error } }", { i: { id } }).then((d) => {
        const j = d.findJob;
        if (j && !["FINISHED", "FAILED", "CANCELLED"].includes(j.status)) { setJob({ id, progress: j.progress || 0 }); setTimeout(tick, 2000); return; }
        try { sessionStorage.removeItem("sbs-job"); } catch (e) { /* fine */ }
        setJob(null);
        if (j && j.status !== "FINISHED") { setBusy(false); setErr(j.error || `Check again ${j.status.toLowerCase()}`); return; }
        ask({ mode: "ready" }).then((r) => { setData(r); setPerf(null); setStudios(null); })  // (they're worked out from the new list)
          .catch((e) => setErr(e.message)).finally(() => setBusy(false));
      }).catch((e) => { setJob(null); setBusy(false); setErr(e.message); });
      tick();
    };
    const checkAgain = () => {
      setErr("");
      gql("mutation($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }", { p: PLUGIN_ID, t: "Find scenes ready to submit" })
        .then((d) => follow(d.runPluginTask)).catch((e) => setErr(e.message));
    };
    React.useEffect(() => {
      load(false);
      let id = null;
      try { id = sessionStorage.getItem("sbs-job"); } catch (e) { /* fine */ }
      if (id) follow(id);  // (one still running from before a reload)
    }, []);
    React.useEffect(() => writeQuery(section, siteName), [section, siteName]);
    // (Pending: loaded straight away for its count, and again whenever something is sent)
    React.useEffect(() => { ask({ mode: "review" }).then(setPending).catch(() => {}); }, [tick]);
    const sites = data ? data.sites : [];
    const site = sites.find((s) => siteKey(s) === siteName);
    const only = site ? site.endpoint : "";
    const shown = (endpoint) => !only || endpoint === only;

    // counts on the section tabs (once known)
    const count = {
      scenes: data ? data.scenes.filter((sc) => Object.entries(sc.sites).some(([e, v]) => shown(e) && !v.on_site && !isWaiting(pending, e, v, sc.title)
        && (sites.find((x) => x.endpoint === e) || {}).can_edit !== false)).length : null,
      performers: perf ? perf.sites.filter((x) => shown(x.endpoint)).reduce((n, x) => n + x.rows.filter((r) => !r.draft && !r.pending && !(perfDone[x.endpoint + ":" + r.id])).length, 0) : null,
      studios: studios ? studios.sites.filter((x) => shown(x.endpoint)).reduce((n, x) => n + x.site.filter((r) => !r.sent && !studioDone[x.endpoint + ":" + r.name]).length + x.local.length, 0) : null,
      pending: pending ? pending.sites.filter((x) => shown(x.endpoint)).reduce((n, x) => n + x.drafts.length + (x.edits || []).length, 0) : null,
    };
    const props = { data, setData, only, site, sites, tick, setTick, onlyNew, setOnlyNew, bulkOn };
    return h("div", { className: "sbs-page" },
      h("div", { className: "sbs-page-head" },
        h("div", { className: "sbs-hub-title" },
          h("h2", null, "Stash-Box Submit"),
          h("button", { type: "button", className: "btn btn-sm btn-secondary", disabled: busy, onClick: () => load(true),
            title: "Scores every scene again and looks up what's new on each site (runs in the background)" },
            job ? `Checking… ${Math.round((job.progress || 0) * 100)}%` : busy ? "Loading…" : "Check again")),
        job ? h("div", { className: "sbs-job" }, h("div", { className: "sbs-job-bar", style: { width: `${Math.max(3, Math.round((job.progress || 0) * 100))}%` } }),
          h("span", { className: "sbs-muted" }, "Running in the background - you can leave this page; it's also under Settings → Tasks.")) : null,
        data ? h("p", { className: "sbs-muted sbs-count" }, `Checked ${new Date(data.at * 1000).toLocaleString()} · ${data.count} scenes have most of what a submission needs`) : null,
        h("div", { className: "sbs-sections", role: "tablist" }, SECTIONS.map(([k, label]) => h("button", {
          key: k, type: "button", role: "tab", "aria-selected": section === k, className: "sbs-section-tab" + (section === k ? " active" : ""), onClick: () => setSection(k),
        }, label, count[k] != null ? h("span", { className: "sbs-badge" }, count[k]) : null))),
        h("div", { className: "sbs-tabs" },
          h("button", { type: "button", className: "btn btn-sm " + (!only ? "btn-primary" : "btn-secondary"), onClick: () => setSiteName("") }, "All sites"),
          sites.map((s) => h("button", { key: s.endpoint, type: "button", className: "btn btn-sm " + (only === s.endpoint ? "btn-primary" : "btn-secondary"), onClick: () => setSiteName(siteKey(s)) },
            s.name, s.can_edit === false ? h("span", { className: "sbs-ro" }, " (read-only)") : null)))),
      err ? h("div", { className: "alert alert-danger" }, err, " ", h("button", { type: "button", className: "btn btn-sm btn-light", onClick: () => load(false) }, "Try again")) : null,
      !data && !err ? h(Waiting, { text: "Loading…" }) : null,
      data && section === "scenes" ? h(ScenesSection, { ...props, pending }) : null,
      data && section === "performers" ? h(PerformersSection, { ...props, perf, setPerf, perfDone, setPerfDone, goPending: () => setSection("pending") }) : null,
      data && section === "studios" ? h(StudiosSection, { ...props, studios, setStudios, studioDone, setStudioDone, goPending: () => setSection("pending") }) : null,
      data && section === "pending" ? h(PendingSection, { ...props, pending, setPending }) : null,  // (props has setTick)
      // (Pending's count shows on its tab before you open it)
      null);
  }

  // ---- Scenes: the ready list ----
  function ScenesSection({ data, setData, only, sites, setTick, onlyNew, setOnlyNew, bulkOn, pending }) {
    const [hideOn, setHideOn] = React.useState(true);    // hide ones the site has already
    const [hideMissing, setHideMissing] = React.useState(false);
    const [hideRO, setHideRO] = React.useState(true);     // hide sites your account can't submit to
    const [open, setOpen] = React.useState(null);        // {sceneId, site}
    const [bulk, setBulk] = React.useState(null);        // {running, sent: [], skipped: [], at}
    const readOnly = new Set(sites.filter((x) => x.can_edit === false).map((x) => x.endpoint));
    const rows = data.scenes.filter((sc) => !onlyNew || !(sc.on_any || []).length).map((sc) => {
      const ss = Object.entries(sc.sites).filter(([e, v]) => (!only || e === only) && (!hideOn || !v.on_site) && (!hideMissing || !v.missing.length)
        && (only || !hideRO || !readOnly.has(e))  // (picking that site still shows it)
        && !isWaiting(pending, e, v, sc.title));  // (sent or submitted: it's in Pending)
      return { sc, sites: ss };
    }).filter((r) => r.sites.length);
    const rank = (v) => v.score - 3 * v.missing.length;
    if (only) rows.sort((a, b) => rank(b.sc.sites[only]) - rank(a.sc.sites[only]));
    const [show, setShow] = React.useState(50);  // (a big list a part at a time)
    React.useEffect(() => setShow(50), [only, hideOn, hideMissing, hideRO, onlyNew]);
    return h("div", null,
      h("p", { className: "sbs-muted sbs-intro" }, "Scenes with most of what a submission needs, best first, leaving out ones already linked to the site. "
        + "\"Already on …\" means the site has the same video: link it with Stash's Tagger instead. Submit opens the check against that site's rules."),
      h("div", { className: "sbs-page-tools" },
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: hideOn, onChange: (e) => setHideOn(e.target.checked) }), " Hide ones the site has already"),
        readOnly.size && !only ? h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: hideRO, onChange: (e) => setHideRO(e.target.checked) }),
          ` Hide sites I can't submit to (${sites.filter((x) => readOnly.has(x.endpoint)).map((x) => x.name).join(", ")}: read-only)`) : null,
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: hideMissing, onChange: (e) => setHideMissing(e.target.checked) }), " Only ones with nothing missing"),
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: onlyNew, onChange: (e) => setOnlyNew(e.target.checked) }), " Only scenes not on any database")),
      h("p", { className: "sbs-muted sbs-count" }, `${rows.length} shown · from the best ${data.shown || data.scenes.length} of ${data.count} · ones you've sent are in Pending`),
      h(Warnings, { data, bulkOn }),
      bulkOn ? h(BulkBar, { data, rows, only, bulk, setBulk, setData, onDone: () => setTick((t) => t + 1) }) : null,
      h("div", { className: "sbs-cards" }, rows.slice(0, show).map(({ sc, sites: ss }) => h("div", { key: sc.id, className: "sbs-card" },
        h("a", { href: `/scenes/${sc.id}`, onClick: (e) => { e.preventDefault(); goTo(`/scenes/${sc.id}`); } },
          sc.cover ? h("img", { src: here(sc.cover), alt: "", loading: "lazy" }) : null),
        h("div", { className: "sbs-card-body" },
          h("a", { className: "sbs-card-title", href: `/scenes/${sc.id}`, onClick: (e) => { e.preventDefault(); goTo(`/scenes/${sc.id}`); } }, sc.title || "(no title)"),
          h("div", { className: "sbs-muted" }, [sc.studio, sc.date, sc.performers.join(", ")].filter(Boolean).join(" · ")),
          (sc.on_any || []).length ? h("div", { className: "sbs-muted" }, `Already on ${sc.on_any.join(", ")} (linked in your Stash)`) : null),
        h("div", { className: "sbs-card-sites" },
          ss.map(([e, v]) => h("div", { key: e, className: "sbs-site-row" },
            h("b", null, v.name),
            v.on_site ? h(React.Fragment, null, mark(null), " Already on ", v.name, ": ",
              v.on_site.map((o) => h("a", { key: o.id, href: o.url, target: "_blank", rel: "noopener" }, o.title || o.id)), " - link it with the Tagger")
              : v.sent ? h(React.Fragment, null, mark(null), " Sent" + (v.sent.at ? ` ${new Date(v.sent.at * 1000).toLocaleDateString()}` : "") + ", no longer waiting (expired or voted on)",
                readOnly.has(e) ? null : h("button", { type: "button", className: "btn btn-link btn-sm", onClick: () => setOpen({ sceneId: sc.id, site: e }) }, "send again…"))
              : h(React.Fragment, null,
                v.missing.length ? h("span", { className: "sbs-muted" }, mark(false), " missing ", v.missing.join(", ")) : h("span", null, mark(true), " has everything"),
                h("span", { className: "sbs-muted sbs-linked" }, studioText(v), ` · performers linked ${v.performers_linked}`),
                readOnly.has(e) ? h("span", { className: "sbs-muted sbs-linked" }, "read-only account")
                  : v.studio_site === "pending" ? h("span", { className: "sbs-muted sbs-linked" }, "submit it once the studio is accepted",
                    v.studio_edit ? h(React.Fragment, null, " - ", ext(v.studio_edit.edit_url, "the studio edit")) : null)
                  : h("button", { type: "button", className: "btn btn-sm btn-primary sbs-submit", onClick: () => setOpen({ sceneId: sc.id, site: e }) }, `Submit to ${v.name}…`)))))))),
      rows.length > show ? h("button", { type: "button", className: "btn btn-secondary sbs-more", onClick: () => setShow((n) => n + 50) },
        `Show more (${rows.length - show} left)`) : null,
      open ? h(Modal, { sceneId: open.sceneId, initialSite: open.site, onClose: () => setOpen(null),
        onSent: (s, url) => (setTick((t) => t + 1), setData((d) => ({ ...d, scenes: d.scenes.map((x) => x.id !== open.sceneId ? x
          : { ...x, sites: { ...x.sites, [s]: { ...x.sites[s], sent: { url, at: Date.now() / 1000 } } } }) }))) }) : null);
  }

  // "Submit…": a comment box, then the edit is submitted straight away (public, waiting for votes)
  function SubmitNow({ site, what, defaultComment, onSubmit }) {
    const [open, setOpen] = React.useState(false);
    const [comment, setComment] = React.useState(defaultComment || "");
    const [busy, setBusy] = React.useState(false);
    const [err, setErr] = React.useState("");
    if (!open) return h("button", { type: "button", className: "btn btn-sm btn-success", onClick: () => setOpen(true),
      title: `Submit ${what} to ${site} now - no draft to finish on the site` }, "Submit…");
    const go = async () => {
      setBusy(true); setErr("");
      try { await onSubmit(comment.trim()); } catch (e) { setErr(e.message); setBusy(false); }
    };
    return h("div", { className: "sbs-confirm sbs-confirm-inline" },
      h("p", { className: "sbs-muted" }, `Submits ${what} as a public edit under your ${site} account - other users vote on it.`),
      h("label", null, "Edit comment (where the details come from)"),
      h("textarea", { className: "form-control", rows: 2, value: comment, onChange: (e) => setComment(e.target.value) }),
      err ? h("p", { className: "text-danger" }, err) : null,
      h("div", { className: "sbs-confirm-btns" },
        h("button", { type: "button", className: "btn btn-sm btn-secondary", onClick: () => setOpen(false), disabled: busy }, "Cancel"),
        h("button", { type: "button", className: "btn btn-sm btn-success", onClick: go, disabled: busy || !comment.trim() }, busy ? "Submitting…" : "Submit edit")));
  }
  const editable = (endpoint) => /fansdb|stashdb/.test(endpoint || "");
  const BATCH = 20;  // new lookups per round - each request stays short, even on a big library
  // keeps asking `mode` until nothing's left to look up; data and its "Look up again" time live in the page
  function useRounds(mode, only, data, setData) {
    const run = React.useRef(0);
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState(false);  // (a round is being looked up)
    const left = data ? data.sites.reduce((n, x) => n + (x.remaining || 0), 0) : 0;
    const start = (fresh, keep) => {
      const me = ++run.current;
      const since = fresh ? Date.now() / 1000 : (data && data.since) || null;
      setErr(""); setBusy(true);
      if (!keep) setData(null);
      const step = () => ask({ mode, batch: BATCH, fresh_since: since, fresh_site: since ? (only || null) : null }).then((out) => {
        if (me !== run.current) return;
        setData({ ...out, since });
        if (out.sites.some((x) => x.remaining)) step(); else setBusy(false);
      }).catch((e) => { if (me === run.current) { setErr(e.message); setBusy(false); } });
      step();
    };
    React.useEffect(() => {
      if (!data) start(false, false);
      else if (left) start(false, true);  // (carry on where it stopped - e.g. after switching tabs)
      return () => { run.current += 1; };  // (stop when leaving the section)
    }, []);
    const stop = () => { run.current += 1; setBusy(false); setData((d) => d && ({ ...d, stopped: true })); };
    return { err, left, start, stop, running: busy };
  }
  function RoundsLine({ r, what }) {
    if (!r.left && !r.running) return null;
    return h("div", { className: "sbs-rounds" },
      r.running ? h("span", { className: "spinner-border spinner-border-sm" }) : null,
      r.left ? ` ${r.left} ${what} still to look up` + (r.running ? ` - looking up ${BATCH} at a time, the list fills in as they come` : " (stopped)")
        : ` Looking up ${what} - ${BATCH} at a time, the list fills in as they come`,
      r.running ? h("button", { type: "button", className: "btn btn-sm btn-link", onClick: r.stop }, "Stop")
        : h("button", { type: "button", className: "btn btn-sm btn-link", onClick: () => r.start(false, true) }, "Carry on"));
  }
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 3600 ? "less than an hour ago" : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} days ago`; };
  function savedLine(groups) {
    const ts = groups.map((g) => g.oldest).filter(Boolean);
    if (!ts.length) return null;
    return h("p", { className: "sbs-muted sbs-saved" }, `Using saved answers (the oldest from ${ago(Math.min(...ts))}). They're kept: a match for a month, `
      + "a name match for two weeks, \"not there\" for three days - and dropped as soon as you act on one here. \"Look up again\" checks the site now.");
  }

  // ---- Performers: who the ready scenes need on each site, in the order you'd deal with them ----
  //   1. on the site already (matched by a link)  -> link them in your Stash
  //   2. someone on the site has the same name    -> is it them?
  //   3. not on the site                          -> send a draft (or add what it needs first)
  const personFacts = (c) => [c.disambiguation, c.country, c.birth_date && c.birth_date.slice(0, 4),
    c.scene_count ? plural(c.scene_count, "scene") : null].filter(Boolean).join(" · ");
  const face = (src, cls) => src ? h("img", { className: "sbs-face " + (cls || ""), src: here(src), alt: "", loading: "lazy" })
    : h("div", { className: "sbs-face sbs-face-none " + (cls || "") }, "?");
  function PerformersSection({ only, perf, setPerf, setTick, perfDone, setPerfDone, goPending }) {
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState({});
    const [res, setRes] = React.useState({});
    const [tab, setTab] = React.useState(() => { try { return sessionStorage.getItem("sbs-perf-tab") || ""; } catch (e) { return ""; } });
    // (saved answers first, then the rest in rounds; "Look up again" checks the site now - just the site picked above)
    const rounds = useRounds("performer_suggestions", only, perf, setPerf);
    const load = (fresh) => rounds.start(fresh, !!perf);
    React.useEffect(() => { if (rounds.err) setErr(rounds.err); }, [rounds.err]);
    async function act(k, args, done) {
      setBusy((b) => ({ ...b, [k]: true }));
      try {
        const out = await ask(args); setRes((x) => ({ ...x, [k]: out }));
        if (done) setPerfDone((d) => ({ ...d, [k]: done }));
        if (args.mode === "performer") setTick((t) => t + 1);
        return out;
      } catch (e) {
        setRes((x) => ({ ...x, [k]: { error: e.message } }));
      } finally {
        setBusy((b) => ({ ...b, [k]: false }));
      }
      return null;
    }
    if (err) return h("div", { className: "alert alert-danger" }, err, " ", h("button", { type: "button", className: "btn btn-sm btn-light", onClick: () => load(false) }, "Try again"));
    if (!perf) return h(Waiting, { text: "Looking up the performers on each site…", note: "The first time takes a minute or two (ThePornDB is asked slowly, a few at a time); answers are kept, so later it's quick." });
    const groups = perf.sites.filter((x) => !only || x.endpoint === only);
    const sceneList = (r) => h("div", { className: "sbs-muted sbs-scenes-of" }, `In ${plural(r.count, "ready scene")}: `,
      r.scenes.slice(0, 2).map((sc, i) => h(React.Fragment, { key: sc.id }, i ? " · " : "", stashLink(`/scenes/${sc.id}`, sc.title || `scene ${sc.id}`))), r.count > 2 ? " …" : "");
    const you = (r) => h("div", { className: "sbs-person" }, face(r.image),
      h("div", { className: "sbs-person-text" }, h("b", null, stashLink(`/performers/${r.id}`, r.name)), h("span", { className: "sbs-muted" }, "in your Stash"), sceneList(r)));
    // each site's performers, sorted into the three checks
    const parts = groups.map((g) => {
      const key = (r) => g.endpoint + ":" + r.id;
      const left_ = g.rows.filter((r) => !r.draft && !r.pending && !perfDone[key(r)] && r.status !== "linked");
      const missing = left_.filter((r) => r.status === "missing");
      return { g, key, base: g.base || siteBase(g.endpoint),
        found: left_.filter((r) => r.status === "found" && r.box), maybe: left_.filter((r) => r.status === "maybe"), missing,
        ready: missing.filter((r) => r.proposal && !r.proposal.needs.length), needs: missing.filter((r) => !r.proposal || r.proposal.needs.length),
        other: left_.filter((r) => !["found", "maybe", "missing"].includes(r.status)),
        linked: g.rows.filter((r) => perfDone[key(r)] === "linked").length, sent: g.rows.filter((r) => perfDone[key(r)] === "sent").length,
        errors: g.rows.filter((r) => (res[key(r)] || {}).error) };
    });
    const total = (f) => parts.reduce((n, x) => n + x[f].length, 0);
    const TABS = [["found", "Link", "found"], ["maybe", "Is it them?", "maybe"], ["missing", only ? `Not on ${(parts[0] || {}).g ? parts[0].g.name : "site"}` : "Not on site", "missing"]];
    if (total("other")) TABS.push(["later", "Not looked up", "other"]);
    // (the tab you were on, else the first one with something in it)
    const step = TABS.some(([k]) => k === tab) ? tab : (TABS.find(([, , f]) => total(f)) || TABS[0])[0];
    const pick = (k) => { setTab(k); try { sessionStorage.setItem("sbs-perf-tab", k); } catch (e) { /* fine */ } };
    const many = parts.length > 1;
    return h("div", null,
      h("p", { className: "sbs-muted sbs-intro" }, "The performers your ready scenes need. Once a performer is linked to a site, their scenes are ready to submit there."),
      h("div", { className: "sbs-perf-tabs", role: "tablist" },
        TABS.map(([k, label, f], i) => h("button", { key: k, type: "button", role: "tab", "aria-selected": step === k,
          className: "sbs-perf-tab" + (step === k ? " active" : ""), onClick: () => pick(k) },
          k !== "later" ? h("span", { className: "sbs-step-n" }, i + 1) : null, " ", label, h("span", { className: "sbs-badge" }, total(f))))),
      parts.map((x) => x.linked || x.sent || x.errors.length ? h("div", { key: "done" + x.g.endpoint },
        x.linked || x.sent ? h("p", { className: "sbs-studio-done" }, mark(true), many ? `${x.g.name}: ` : "",
          [x.linked ? `${plural(x.linked, "performer")} linked` : "", x.sent ? `${plural(x.sent, "draft")} sent - ` : ""].filter(Boolean).join(" · "),
          x.sent ? h("a", { href: "#", onClick: (e) => { e.preventDefault(); goPending(); } }, "see Pending") : null) : null,
        x.errors.map((r) => h("p", { key: "e" + r.id, className: "text-danger" }, `${r.name}: ${res[x.key(r)].error}`))) : null),
      h("div", { className: "sbs-perf-tools" },
        h("p", { className: "sbs-muted" }, {
          found: "The site has them - found by the same link (OnlyFans, X…). Linking saves their site ID in your Stash; nothing is sent.",
          maybe: "The site has someone with the same name. Compare them, then pick the right one - or \"None of these\" if it's someone else.",
          missing: "Not on the site yet. Send a performer draft, then finish it on the site (drafts are deleted after 24 hours).",
          later: "Only some are looked up on ThePornDB each time, so it isn't asked too much at once - \"Look up again\" does the next ones.",
        }[step]),
        h("button", { type: "button", className: "btn btn-sm btn-secondary", onClick: () => load(true),
          title: "Checks the site again now instead of using the saved answers" }, "Look up again")),
      savedLine(parts.map((x) => x.g)),
      h(RoundsLine, { r: rounds, what: "performers" }),
      parts.map((x) => {
        const { g, key, base } = x;
        const head = (count, extra) => many || extra ? h("div", { className: "sbs-step-head" }, many ? h("h5", null, g.name, h("span", { className: "sbs-badge" }, count)) : h("span"), extra || null) : null;
        if (step === "found") {
          const linkAll = async () => { for (const r of x.found) await act(key(r), { mode: "link_performer", endpoint: g.endpoint, performer_id: r.id, remote_id: r.box.id }, "linked"); };
          return h("div", { key: g.endpoint, className: "sbs-step" },
            head(x.found.length, x.found.length > 1 ? h("button", { type: "button", className: "btn btn-sm btn-primary", onClick: linkAll }, `Link all ${x.found.length}` + (many ? ` on ${g.name}` : "")) : null),
            !x.found.length ? h("p", { className: "sbs-muted" }, "None to link.") : h("div", { className: "sbs-person-grid" }, x.found.map((r) => h("div", { key: r.id, className: "sbs-person-card" },
              face(r.image),
              h("div", { className: "sbs-person-text" },
                h("b", null, stashLink(`/performers/${r.id}`, r.name)),
                h("span", { className: "sbs-muted" }, "= ", ext(`${base}/performers/${r.box.id}`, r.box.name), ` · ${plural(r.count, "scene")}`)),
              h("button", { type: "button", className: "btn btn-sm btn-secondary", disabled: !!busy[key(r)],
                onClick: () => act(key(r), { mode: "link_performer", endpoint: g.endpoint, performer_id: r.id, remote_id: r.box.id }, "linked") }, busy[key(r)] ? "…" : "Link")))));
        }
        if (step === "maybe") return h("div", { key: g.endpoint, className: "sbs-step" },
          head(x.maybe.length),
          !x.maybe.length ? h("p", { className: "sbs-muted" }, "None to check.") : x.maybe.map((r) => h("div", { key: r.id, className: "sbs-compare" },
            you(r),
            h("div", { className: "sbs-compare-arrow" }, "→"),
            h("div", { className: "sbs-compare-cands" },
              r.candidates.map((c) => h("div", { key: c.id, className: "sbs-person" }, face(c.image, "sbs-face-site"),
                h("div", { className: "sbs-person-text" },
                  h("b", null, ext(`${base}/performers/${c.id}`, c.name)),
                  personFacts(c) ? h("span", { className: "sbs-muted" }, personFacts(c)) : null,
                  c.aliases && c.aliases.length ? h("span", { className: "sbs-muted" }, "aka " + c.aliases.slice(0, 4).join(", ")) : null),
                h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !!busy[key(r)],
                  onClick: () => act(key(r), { mode: "link_performer", endpoint: g.endpoint, performer_id: r.id, remote_id: c.id }, "linked") }, "That's them"))),
              h("button", { type: "button", className: "btn btn-sm btn-link sbs-none", disabled: !!busy[key(r)],
                onClick: async () => { if (await act(key(r), { mode: "not_them", endpoint: g.endpoint, performer_id: r.id, candidate_ids: r.candidates.map((c) => c.id) })) load(false); } },
                "None of these - they're not on " + g.name)))));
        if (step === "missing") return h("div", { key: g.endpoint, className: "sbs-step" },
          head(x.missing.length),
          !x.missing.length ? h("p", { className: "sbs-muted" }, "None.") : null,
          x.ready.length ? h("h6", null, `Ready to send (${x.ready.length})`) : null,
          h("div", { className: "sbs-person-grid" }, x.ready.map((r) => h("div", { key: r.id, className: "sbs-person-card" },
            face(r.image),
            h("div", { className: "sbs-person-text" },
              h("b", null, stashLink(`/performers/${r.id}`, r.name)),
              h("span", { className: "sbs-muted" }, [r.proposal.gender && r.proposal.gender.toLowerCase().replace("_", " "), plural(r.proposal.links.length, "link"),
                r.proposal.aliases.length ? "aka " + r.proposal.aliases.slice(0, 3).join(", ") : null, plural(r.count, "scene")].filter(Boolean).join(" · "))),
            h("div", { className: "sbs-person-act" },
              h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !!busy[key(r)],
                onClick: () => act(key(r), { mode: "performer", endpoint: g.endpoint, performer_id: r.id }, "sent") }, busy[key(r)] ? "Sending…" : "Send draft"),
              editable(g.endpoint) ? h(SubmitNow, { site: g.name, what: r.name,
                defaultComment: `New performer from their own pages: ${r.proposal.links.slice(0, 3).join(" , ")}. In ${plural(r.count, "scene")} I'm submitting.`,
                onSubmit: async (comment) => {
                  const out = await ask({ mode: "performer_edit", endpoint: g.endpoint, performer_id: r.id, comment });
                  setRes((z) => ({ ...z, [key(r)]: out })); setPerfDone((d) => ({ ...d, [key(r)]: "sent" })); setTick((t) => t + 1);
                } }) : null)))),
          x.needs.length ? h("h6", null, `Add info first (${x.needs.length})`) : null,
          h("div", { className: "sbs-person-grid" }, x.needs.map((r) => h("div", { key: r.id, className: "sbs-person-card" },
            face(r.image),
            h("div", { className: "sbs-person-text" },
              h("b", null, stashLink(`/performers/${r.id}`, r.name)),
              h("span", { className: "sbs-muted" }, r.proposal ? "Needs " + r.proposal.needs.join(" and ") : "Couldn't prepare a draft")),
            h("a", { className: "btn btn-sm btn-secondary", href: `/performers/${r.id}`, onClick: (e) => { e.preventDefault(); goTo(`/performers/${r.id}`); } }, "Edit")))));
        return x.other.length || !many ? h("div", { key: g.endpoint, className: "sbs-step" },
          head(x.other.length),
          !x.other.length ? h("p", { className: "sbs-muted" }, "None.") : h("div", { className: "sbs-person-grid" }, x.other.map((r) => h("div", { key: r.id, className: "sbs-person-card" },
            face(r.image), h("div", { className: "sbs-person-text" }, h("b", null, stashLink(`/performers/${r.id}`, r.name)),
              h("span", { className: "sbs-muted" }, r.status === "later" ? "next time" : (r.why || "couldn't look them up"))))))) : null;
      }));
  }

  // ---- Studios: the ones ready scenes wait for, and ones missing in your Stash ----
  function StudiosSection({ only, studios, setStudios, setTick, studioDone, setStudioDone, goPending }) {
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState({});      // row key -> true while sending
    const [res, setRes] = React.useState({});        // row key -> result or {error}
    const [onlyAuto, setOnlyAuto] = React.useState(false);
    const rounds = useRounds("studio_suggestions", only, studios, setStudios);
    const load = (fresh) => rounds.start(fresh, !!studios);
    React.useEffect(() => { if (rounds.err) setErr(rounds.err); }, [rounds.err]);
    const keyOf = (g, r) => g.endpoint + ":" + (r.local_id || r.account_url || "") + ":" + r.name;
    async function run(k, args, confirmText) {
      if (confirmText && !window.confirm(confirmText)) return;
      setBusy((b) => ({ ...b, [k]: true }));
      try {
        const out = await ask(args); setRes((x) => ({ ...x, [k]: out }));
        if (args.mode === "studio") { setStudioDone((d) => ({ ...d, [args.endpoint + ":" + args.row_name]: out })); setTick((t) => t + 1); }
      }
      catch (e) { setRes((x) => ({ ...x, [k]: { error: e.message } })); }
      finally { setBusy((b) => ({ ...b, [k]: false })); }
    }
    const sentText = (g, s) => s.studio
      ? h(React.Fragment, null, mark(true), ` On ${g.name}: `, ext(s.studio.url || `${siteBase(g.endpoint)}/studios/${s.studio.id}`, s.studio.name), " - Check again to see its scenes as ready.")
      : s.status === "exists" ? h(React.Fragment, null, mark(true), ` It's on ${g.name} already - Check again.`)
      : h(React.Fragment, null, mark(null), " Waiting for votes" + (s.yes != null ? ` (${s.yes} yes, ${s.no} no)` : "")
        + (s.expires ? ` until ${closes(s.expires)}` : "") + ". Its scenes can be submitted once it's accepted. ", s.edit_url ? ext(s.edit_url, "Open the edit") : null);
    if (err) return h("div", { className: "alert alert-danger" }, err, " ", h("button", { type: "button", className: "btn btn-sm btn-light", onClick: () => load(false) }, "Try again"));
    if (!studios) return h(Waiting, { text: "Looking up the studios…" });
    const groups = studios.sites.filter((x) => !only || x.endpoint === only);
    return h("div", null,
      h("p", { className: "sbs-muted sbs-intro" }, "A scene can't go to a site until its studio is there. These are the studios your ready scenes wait for, the ones that unlock the most scenes first. "
        + "FansDB names a creator's studio after the account, \"username (Network)\" - those can be created from here."),
      h("div", { className: "sbs-page-tools" },
        h("label", { className: "sbs-check" }, h("input", { type: "checkbox", checked: onlyAuto, onChange: (e) => setOnlyAuto(e.target.checked) }), " Only ones that can be created from here"),
        h("button", { type: "button", className: "btn btn-sm btn-secondary sbs-recheck", onClick: () => load(true),
          title: "Checks the site again now instead of using the saved answers" }, "Look up again")),
      savedLine(groups),
      h(RoundsLine, { r: rounds, what: "studios" }),
      !groups.length ? h("p", { className: "sbs-muted" }, only ? "Your account on this site can't add studios (read-only)." : "No sites to add studios to.") : null,
      groups.map((g) => {
        const base = siteBase(g.endpoint), fans = /fansdb/.test(g.endpoint), tpdb = /theporndb/.test(g.endpoint);
        const rows = g.site.filter((r) => !r.sent && !studioDone[g.endpoint + ":" + r.name] && (!onlyAuto || r.proposal));  // (sent: in Pending)
        const sentHere = g.site.filter((r) => studioDone[g.endpoint + ":" + r.name]);
        return h("div", { key: g.endpoint, className: "sbs-site-group" },
          h("h4", null, g.name),
          g.error ? h("p", { className: "text-danger" }, g.error) : null,
          g.local.length ? h("div", { className: "sbs-section sbs-studio-group" },
            h("h5", null, `Missing in your Stash (${g.local.length})`),
            h("p", { className: "sbs-muted" }, `Scenes linked to ${g.name} whose studio didn't come across (Stash's Tagger only fills in studios you already have).`),
            g.local.map((x) => {
              const k = "l:" + g.endpoint + ":" + x.site_studio.id, r = res[k];
              return h("div", { key: k, className: "sbs-studio-row" },
                h("div", { className: "sbs-studio-main" },
                  h("b", null, x.site_studio.name), " ", h("a", { className: "sbs-muted", href: x.site_studio.url, target: "_blank", rel: "noopener" }, `on ${g.name}`),
                  h("div", { className: "sbs-muted" }, x.existing ? `Your Stash has "${x.existing.name}": it gets linked to ${g.name} and set on the scene${x.scenes.length === 1 ? "" : "s"}.`
                    : `Creates "${x.local_name}"` + (x.local_parent ? ` under "${x.local_parent}"` : "") + ` with its links, linked to ${g.name}.`),
                  h("div", { className: "sbs-muted" }, x.scenes.map((sc, i) => h(React.Fragment, { key: sc.id }, i ? " · " : "", stashLink(`/scenes/${sc.id}`, sc.title || `scene ${sc.id}`))))),
                r && !r.error ? h("p", { className: "sbs-studio-done" }, mark(true), ` ${r.made ? "Created" : "Linked"} `, stashLink(`/studios/${r.id}`, r.name), ` and set on ${plural(r.scenes, "scene")}.`)
                  : h("div", { className: "sbs-studio-act" },
                    r && r.error ? h("p", { className: "text-danger" }, r.error) : null,
                    h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !!busy[k],
                      onClick: () => run(k, { mode: "local_studio", endpoint: g.endpoint, site_studio_id: x.site_studio.id, scene_ids: x.scenes.map((s) => s.id) }) },
                      busy[k] ? "Working…" : `${x.existing ? "Link" : "Create"} & set on ${plural(x.scenes.length, "scene")}`)));
            })) : null,
          h("div", { className: "sbs-section sbs-studio-group" },
            h("h5", null, `To add to ${g.name} (${rows.length})`),
            sentHere.map((r) => h("p", { key: "s" + r.name, className: "sbs-studio-done" }, sentText(g, studioDone[g.endpoint + ":" + r.name]),
              " ", h("a", { href: "#", onClick: (e) => { e.preventDefault(); goPending(); } }, "See Pending"))),
            !g.site.length ? h("p", { className: "sbs-muted" }, `None - every ready scene's studio is on ${g.name}.`) : null,
            rows.map((r) => {
              const k = keyOf(g, r), done = res[k] && !res[k].error ? res[k] : r.sent, p = r.proposal;
              return h("div", { key: k, className: "sbs-studio-row" },
                h("div", { className: "sbs-studio-main" },
                  h("b", null, p ? p.name : r.name),
                  p ? h("span", { className: "sbs-muted" }, ` under ${p.parent.name} · `, ext(p.url, p.url.replace(/^https?:\/\//, ""))) : null,
                  h("div", { className: "sbs-muted" }, "Your Stash: ", r.local_id ? stashLink(`/studios/${r.local_id}`, r.name) : r.name,
                    r.account_url ? " (from the account the videos were posted on)" : ""),
                  h("div", { className: "sbs-muted" }, `${plural(r.count, "scene")} waiting` + (r.ready ? ` - ${r.ready} with nothing else missing` : "") + ": ",
                    r.scenes.map((sc, i) => h(React.Fragment, { key: sc.id }, i ? " · " : "", stashLink(`/scenes/${sc.id}`, sc.title || `scene ${sc.id}`))),
                    r.count > r.scenes.length ? " …" : "")),
                done ? h("p", { className: "sbs-studio-done" }, sentText(g, done))
                  : r.unchecked ? h("p", { className: "sbs-muted" }, `Checking ${g.name} for it…`)
                  : p ? h("div", { className: "sbs-studio-act" },
                    res[k] && res[k].error ? h("p", { className: "text-danger" }, res[k].error) : null,
                    h("button", { type: "button", className: "btn btn-sm btn-primary", disabled: !!busy[k],
                      onClick: () => run(k, { mode: "studio", endpoint: g.endpoint, studio_id: r.account_url ? null : r.local_id, account_url: r.account_url || null, row_name: r.name },
                        `Create the studio "${p.name}" on ${g.name}, under ${p.parent.name}, with the link ${p.url}?\n\nThis is a public edit under your ${g.name} account (other users vote on it).`) },
                      busy[k] ? "Sending…" : `Create on ${g.name}…`))
                  : h("div", { className: "sbs-studio-act" },
                    h("p", { className: "sbs-muted" }, fans ? "Can't tell which account it is from its links. Add the account's link (e.g. its OnlyFans or ManyVids page) to the studio in Stash, or add it on FansDB yourself."
                      : tpdb ? "ThePornDB adds studios itself (its API has no way to create one) - its scenes can still be sent: the draft names the studio for ThePornDB's team to add."
                      : `Add it on ${g.name} (its studio pages follow its own rules).`),
                    h("a", { className: "btn btn-sm btn-secondary", href: tpdb ? base : `${base}/studios/add`, target: "_blank", rel: "noopener" }, tpdb ? `Open ${g.name}` : `Add on ${g.name}`)));
            })));
      }));
  }

  // ---- Pending: your drafts (finish them on the site) and edits waiting for votes ----
  const KIND = { scene: "Scene", performer: "Performer", studio: "Studio" };
  function PendingSection({ only, pending, setPending, setTick }) {
    const onChanged = () => setTick((t) => t + 1);  // (reloads Pending: the draft is now an edit)
    const [err, setErr] = React.useState("");
    const [busy, setBusy] = React.useState(false);
    const load = () => { setBusy(true); setErr(""); ask({ mode: "review" }).then(setPending).catch((e) => setErr(e.message)).finally(() => setBusy(false)); };
    if (err) return h("div", { className: "alert alert-danger" }, err, " ", h("button", { type: "button", className: "btn btn-sm btn-light", onClick: load }, "Try again"));
    if (!pending) return h(Waiting, { text: "Looking for your drafts and edits…" });
    const groups = pending.sites.filter((x) => (!only || x.endpoint === only) && (only || x.drafts.length || (x.edits || []).length || !x.listed));
    const card = (key, it) => h("div", { key, className: "sbs-card sbs-pending-card" },
      it.local && it.local.image ? h("a", { href: it.local.path, onClick: (e) => { e.preventDefault(); goTo(it.local.path); } },
        h("img", { src: here(it.local.image), alt: "", loading: "lazy", className: it.kind !== "scene" ? "sbs-thumb-" + it.kind : "" }))
        : h("div", { className: "sbs-noimg" }, KIND[it.kind] || "Item"),
      h("div", { className: "sbs-card-body" },
        h("span", { className: "sbs-kind" }, it.label),
        it.local ? h("a", { className: "sbs-card-title", href: it.local.path, onClick: (e) => { e.preventDefault(); goTo(it.local.path); } }, it.title || "(no title)")
          : h("span", { className: "sbs-card-title" }, it.title || "(no title)"),
        it.status,
        it.note ? h("div", { className: "sbs-muted" }, it.note) : null),
      h("div", { className: "sbs-card-sites" }, it.action));
    return h("div", null,
      h("p", { className: "sbs-muted sbs-intro" }, "What you've sent. Drafts are finished on the site (check it, add the tags you can see, then on the Confirm tab a comment with "
        + "your sources and Submit Edit) - the site deletes them after 24 hours. Submitted edits wait for other users' votes: 3 yes votes (and no no's) accept one straight away; "
        + "otherwise it's accepted when voting ends, unless more people voted no than yes."),
      h("div", { className: "sbs-page-tools" }, h("button", { type: "button", className: "btn btn-sm btn-secondary sbs-recheck", disabled: busy, onClick: load }, busy ? "Checking…" : "Refresh")),
      !groups.length ? h("p", { className: "sbs-muted" }, "Nothing waiting.") : null,
      groups.map((x) => h("div", { key: x.endpoint, className: "sbs-site-group" },
        h("div", { className: "sbs-hub-title" }, h("h4", null, x.name),
          h("div", { className: "sbs-review-links" },
            x.drafts_page ? extLink(x.drafts_page, "Your drafts") : null,
            x.edits_page ? extLink(x.edits_page, "Your edits") : null)),
        !x.drafts.length && !(x.edits || []).length ? h("p", { className: "sbs-muted" }, x.listed ? "Nothing waiting." : "No drafts sent from here in the last 24 hours (this site can't list them).") : null,
        h("div", { className: "sbs-cards" },
          (x.edits || []).map((e) => card(e.edit_url, {
            kind: e.kind, title: e.name, local: e.local,
            label: (e.operation === "create" ? "New " : e.operation === "modify" ? "Change to " : (e.operation || "") + " ") + (KIND[e.kind] || e.kind).toLowerCase(),
            status: h("div", null, mark(null), ` Waiting for votes - ${e.yes} yes, ${e.no} no`),
            note: e.expires ? `Voting ends ${closes(e.expires)} (${until(e.expires)})` + (e.no > e.yes ? " - more no's than yes so far" : " - accepted then if no more no's than yes") : null,
            action: extLink(e.edit_url, "Open the edit"),
          })),
          x.drafts.map((d) => {
            const done = (x.edits || []).some((e) => e.kind === d.kind && d.title && e.name.trim().toLowerCase() === d.title.trim().toLowerCase());
            const gone = d.expires ? `${left(d.expires)} - deleted ${closes(d.expires)} at ${new Date(d.expires).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : null;
            return card(d.url, done ? {
              kind: d.kind, title: d.title, local: d.local, label: `Extra ${(KIND[d.kind] || "scene").toLowerCase()} draft`,
              status: h("div", null, mark(true), " Already submitted (above) - this draft isn't needed"),
              note: "Delete it on the site, or leave it: " + (gone || "it's deleted after 24 hours"),
              action: extLink(d.url, "Open it"),
            } : {
              kind: d.kind, title: d.title, local: d.local, label: `${KIND[d.kind] || "Scene"} draft`,
              status: h("div", null, mark(null), " Draft - finish it on " + x.name + (d.can_submit ? ", or submit it from here" : "")),
              note: [gone, (d.unpicked || []).length ? `${d.unpicked.join(", ")} still to be picked on the site, so this one is finished there.` : null].filter(Boolean).join(" · "),
              action: h("div", { className: "sbs-person-act" },
                extLink(d.url, "Finish on " + x.name, "btn-primary"),
                d.can_submit ? h(SubmitNow, { site: x.name, what: `"${d.title}"`,
                  defaultComment: (d.urls || []).length ? `Details from ${d.urls.slice(0, 3).join(" , ")}` : "",
                  onSubmit: async (comment) => { await ask({ mode: "draft_edit", endpoint: x.endpoint, draft_id: d.id, comment }); onChanged(); } }) : null),
            });
          })))));
  }

  // the old address of the studios page opens its section
  function StudiosRedirect() {
    React.useEffect(() => { window.history.replaceState(window.history.state, "", PAGE + "?section=studios"); window.dispatchEvent(new PopStateEvent("popstate")); }, []);
    return null;
  }

  if (api.register && api.register.route) {
    api.register.route(STUDIOS_PAGE, StudiosRedirect);  // (first: the other's path is the start of this one)
    api.register.route(PAGE, Hub);
  }

  // links to it: a button on the Scenes page, and under the task in Settings → Tasks
  function pageLinks() {
    if (/^\/scenes\/?$/.test(location.pathname) && !document.querySelector(".sbs-ready-btn")) {
      const bar = document.querySelector(".filtered-list-toolbar, .list-filter-toolbar, .filter-container");
      if (bar) {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn btn-secondary btn-sm sbs-ready-btn"; b.textContent = "Ready to submit";
        b.addEventListener("click", () => goTo(PAGE));
        bar.appendChild(b);
      }
    }
    if (location.pathname.startsWith("/settings") && !document.querySelector(".sbs-task-link")) {
      const head = [...document.querySelectorAll(".setting h3")].find((e) => (e.textContent || "").trim() === "Find scenes ready to submit");
      const row = head && head.closest(".setting");
      if (row) {
        const a = document.createElement("a");
        a.href = PAGE; a.className = "sbs-task-link"; a.textContent = "→ Open the Ready to submit page";
        a.addEventListener("click", (e) => { e.preventDefault(); goTo(PAGE); });
        row.after(a);
      }
    }
  }
  new MutationObserver(pageLinks).observe(document.body, { childList: true, subtree: true });
  pageLinks();

  api.patch.after("ScenePage", function (...args) {
    const props = args[0] || {};
    const result = args[args.length - 1];
    return h(React.Fragment, null, result, h(MenuItem, { scene: props.scene }));
  });
})();
