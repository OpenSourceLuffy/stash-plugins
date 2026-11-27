#!/usr/bin/env python3
"""Stash-Box Submit: checks a scene against a stash-box site's own rules
(FansDB, StashDB), links its performers / studio / tags to that site's entries,
and sends it there as a draft - which you finish and submit on the site.

Operations (from the plugin's page, through runPluginOperation):
  sites                         the stash-boxes this can send to, and your rights there
  check     scene_id endpoint   everything the draft would hold, and each rule ✓/✕
  submit    scene_id endpoint choices   sends the scene draft; returns its link
  performer performer_id endpoint       sends a performer draft (when they're not on the site)
Only reads from Stash, except "link": with submit, the performers / studio you
confirmed get that site's id in your Stash (so next time they're linked).
Standard library only.
"""
import concurrent.futures, contextlib, json, os, re, sys, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request, uuid

# --- the sites' rules (from their own guidelines) -------------------------------
# FansDB: docs.fansdb.cc (scenes, studios, performers, tags, submitting content)
# StashDB: guidelines.stashdb.org (scenes, amateur studios, drafts, edit rights)
SITES = {
    "fansdb.cc": {
        "name": "FansDB",
        "required": ["title", "date", "performers", "studio", "phash"],
        "rules": [
            "Title: the official title, else one written by the creator, else one adapted from the description, "
            "else the username and date - one line, emojis fine, no decorative Unicode lettering.",
            "Date: the earliest known release, YYYY-MM-DD (or YYYY-MM / YYYY).",
            "Studio: the account, named \"username (Network)\" - one studio per account.",
            "Performers: everyone credited or mentioned, already on FansDB.",
            "Tags: none required; don't copy tags blindly - only ones you can see in the video.",
            "Links: only sites FansDB knows.",
            "Cover: the studio's own, else a screenshot - no third-party watermarks.",
            "On the site: add a comment saying where the details came from, then Submit Edit.",
        ],
        "not_allowed": [],
    },
    "stashdb.org": {
        "name": "StashDB",
        "required": ["title", "studio", "performers", "phash"],
        "rules": [
            "Title and details: the studio's own, unchanged (spelling, casing, punctuation); "
            "details left blank if the studio has none.",
            "Date: the earliest known release (a month or year alone is fine when that's all that's known).",
            "Studio: must already be on StashDB; an amateur creator is their own studio (not just the platform).",
            "Performers: everyone named on the linked pages, already on StashDB.",
            "Tags: start with the important ones; say in the comment where they came from.",
            "Cover: the studio's static cover, else the closest static image.",
            "On the site: a comment with what you did and your sources is required to submit.",
        ],
        # (amateur content is only allowed from some platforms)
        "not_allowed": ["onlyfans.com", "justfor.fans", "patreon.com", "fansly.com"],
    },
}
SITES["theporndb.net"] = {
    "name": "ThePornDB",
    "required": ["title", "studio", "phash"],
    "rules": [
        "ThePornDB's own submission guidelines couldn't be read (its site only works in a browser), so only the basic "
        "checks run here: fingerprint, duplicates, title, studio and performers on the site.",
        "Its API can't look studios up by link, tags by name, or check links - so tags are left out, and studios are "
        "matched by name; check the draft carefully on the site.",
    ],
    "not_allowed": [],
}
DEFAULT_SITE = {"name": "", "required": ["title", "studio", "phash"], "rules": [], "not_allowed": []}

# decorative "Unicode lettering" (math bold/italic, fullwidth, small caps, circled,
# super/subscript letters) - not emojis
STYLED = re.compile("[\U0001D400-\U0001D7FF！-～ᴀ-ᴫⒶ-ⓩ①-⑳"
                    "\U0001F130-\U0001F189ᴬ-ᵪ⁰-ₜ]")


def site_of(endpoint):
    host = urllib.parse.urlparse(endpoint).hostname or ""
    host = host.removeprefix("www.")
    return SITES.get(host, dict(DEFAULT_SITE, name=host)), host


def site_base(endpoint):
    u = urllib.parse.urlparse(endpoint)
    return f"{u.scheme}://{u.netloc}"


# --- Stash ------------------------------------------------------------------------
class Stash:
    def __init__(self, conn):
        scheme, port = conn.get("Scheme", "http"), conn.get("Port", 9999)
        host = conn.get("Host") or "localhost"
        if host in ("0.0.0.0", ""):
            host = "localhost"
        self.base = f"{scheme}://{host}:{port}"
        self.headers = {"Content-Type": "application/json"}
        cookie = conn.get("SessionCookie") or {}
        if cookie.get("Name"):
            self.headers["Cookie"] = f"{cookie['Name']}={cookie['Value']}"

    def call(self, query, variables=None):
        req = urllib.request.Request(self.base + "/graphql", json.dumps({"query": query, "variables": variables or {}}).encode(),
                                     self.headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        if d.get("errors"):
            raise RuntimeError(d["errors"][0]["message"])
        return d["data"]

    def raw(self, path):
        h = {k: v for k, v in self.headers.items() if k != "Content-Type"}
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=h), timeout=120) as r:
            return r.read(), r.headers.get("Content-Type") or "image/jpeg"


# --- a stash-box ------------------------------------------------------------------
class Later(Exception):
    """A lookup left for next time (this run's share of a paced site's lookups is used up)."""


class Box:
    # ThePornDB turns away bursts ("429 Too Many Requests"): one request at a time there, spaced
    # out; when it still says "too many", everyone waits (5, then 15, then 30 s) - and after
    # that this run stops asking it. New lookups there are also limited per run (the rest are
    # done on later runs, as the answers are kept).
    PACED = {"theporndb.net": 0.7}
    WAITS = (5, 15, 30)
    LIMIT = 25          # new (not yet saved) lookups per run on a paced site
    _locks, _last, _strikes, _spent = {}, {}, {}, {}

    def __init__(self, endpoint, key):
        self.endpoint, self.key = endpoint, key
        self.host = urllib.parse.urlparse(endpoint).netloc
        self.gap = next((v for k, v in self.PACED.items() if self.host.endswith(k)), 0)

    def call(self, query, variables=None, files=None):
        if not self.gap:
            return self._call(query, variables, files)
        lock = Box._locks.setdefault(self.host, threading.Lock())
        with lock:  # (one at a time: a wait holds the others back too)
            while True:
                if Box._strikes.get(self.host, 0) > len(self.WAITS):
                    raise RuntimeError(f"{self.host} asked to slow down - try again in a few minutes")
                pause = Box._last.get(self.host, 0) + self.gap - time.time()
                if pause > 0:
                    time.sleep(pause)
                try:
                    return self._call(query, variables, files)
                except RuntimeError as e:
                    if " 429" not in str(e):
                        raise
                    n = Box._strikes[self.host] = Box._strikes.get(self.host, 0) + 1
                    if n > len(self.WAITS):
                        raise RuntimeError(f"{self.host} asked to slow down - try again in a few minutes")
                    time.sleep(self.WAITS[n - 1])
                finally:
                    Box._last[self.host] = time.time()

    def _call(self, query, variables=None, files=None):
        """GraphQL, or a multipart upload (the cover) when `files` = {var path: (name, bytes, type)}."""
        headers = {"ApiKey": self.key, "User-Agent": "Stash-Box Submit (Stash plugin)"}
        if not files:
            body = json.dumps({"query": query, "variables": variables or {}}).encode()
            headers["Content-Type"] = "application/json"
        else:
            # GraphQL multipart request spec: operations, map, then the files
            bound = uuid.uuid4().hex
            ops = {"query": query, "variables": variables or {}}
            fmap = {str(i): [p] for i, p in enumerate(files)}
            parts = [("operations", json.dumps(ops).encode(), None, None), ("map", json.dumps(fmap).encode(), None, None)]
            for i, (p, (fname, data, ctype)) in enumerate(files.items()):
                parts.append((str(i), data, fname, ctype))
            out = b""
            for name, data, fname, ctype in parts:
                out += f"--{bound}\r\nContent-Disposition: form-data; name=\"{name}\"".encode()
                if fname:
                    out += f"; filename=\"{fname}\"\r\nContent-Type: {ctype}".encode()
                out += b"\r\n\r\n" + data + b"\r\n"
            body = out + f"--{bound}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={bound}"
        req = urllib.request.Request(self.endpoint, body, headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{self.endpoint} answered {e.code}: {e.read().decode(errors='replace')[:200]}")
        if d.get("errors") and not d.get("data"):
            raise RuntimeError(d["errors"][0]["message"])
        return d.get("data") or {}


def boxes(stash):
    return stash.call("{ configuration { general { stashBoxes { name endpoint api_key } } } }")["configuration"]["general"]["stashBoxes"]


def box_for(stash, endpoint):
    b = next((x for x in boxes(stash) if x["endpoint"] == endpoint), None)
    if not b:
        raise RuntimeError(f"{endpoint} isn't set up in Stash (Settings → Metadata Providers → Stash-box Endpoints)")
    if not b.get("api_key"):
        raise RuntimeError(f"{b['name']} has no API key in Stash (Settings → Metadata Providers → Stash-box Endpoints)")
    return Box(endpoint, b["api_key"])


def roles(box):
    try:
        me = box.call("{ me { name roles } }").get("me")
    except Exception:
        return None  # (some sites don't say)
    return None if not me or me.get("roles") is None else me["roles"]  # (ThePornDB answers no roles: unknown, not "none")


# --- the scene ------------------------------------------------------------------------
SCENE = """query($id: ID!) { findScene(id: $id) { id title code details director date urls
  paths { screenshot }
  files { duration fingerprints { type value } }
  studio { id name url urls stash_ids { endpoint stash_id } parent_studio { id name urls stash_ids { endpoint stash_id } } }
  performers { id name disambiguation alias_list urls gender stash_ids { endpoint stash_id } }
  tags { id name aliases }
  stash_ids { endpoint stash_id } } }"""


def fingerprints(scene):
    out = []
    for f in scene.get("files") or []:
        dur = int(round(f.get("duration") or 0))
        for fp in f.get("fingerprints") or []:
            alg = {"oshash": "OSHASH", "phash": "PHASH", "md5": "MD5"}.get(fp["type"])
            if alg and fp.get("value"):
                out.append({"hash": fp["value"], "algorithm": alg, "duration": dur})
    return out


def sid(entity, endpoint):
    return next((s["stash_id"] for s in entity.get("stash_ids") or [] if s["endpoint"] == endpoint), None)


def match_studio(box, endpoint, studio):
    """{status: linked|found|maybe|missing, box: {...}, candidates: [...], why}"""
    if not studio:
        return {"status": "none"}
    have = sid(studio, endpoint)
    if have:
        s = box.call("query($id: ID!) { findStudio(id: $id) { id name urls { url } } }", {"id": have}).get("findStudio")
        if s:
            return {"status": "linked", "box": {"id": s["id"], "name": s["name"]}, "why": "already linked in your Stash"}
    urls = [u for u in (studio.get("urls") or []) + [studio.get("url")] if u]
    # "username (Network)" is how FansDB names accounts
    parent = (studio.get("parent_studio") or {}).get("name") or ""
    names = [studio["name"]] + ([f"{studio['name']} ({parent})"] if parent else [])
    bare = re.sub(r"^[A-Za-z0-9 .]{2,20}:\s+", "", studio["name"])  # ("FansDB: x (OnlyFans)" - a scraper's prefix)
    if bare != studio["name"]:
        names.append(bare)

    def by_link(u):
        try:
            r = box.call("query($u: String!) { queryStudios(input: {url: $u, page: 1, per_page: 5}) { studios { id name } } }", {"u": u})
        except RuntimeError:
            return None  # (this site can't look studios up by link)
        return (r.get("queryStudios") or {}).get("studios") or []

    def by_name(n):
        return box.call("query($n: String!) { findStudio(name: $n) { id name } }", {"n": n}).get("findStudio")
    # (all at once: the site can take seconds a lookup)
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        link_hits = list(pool.map(by_link, urls))
        name_hits = list(pool.map(by_name, names))
    for u, hits in zip(urls, link_hits):
        if hits and len(hits) == 1:
            return {"status": "found", "box": hits[0], "why": f"same link ({u})"}
    cands = []
    for s in name_hits:
        if s and s not in cands:
            cands.append(s)
    if cands:
        return {"status": "maybe", "candidates": cands, "why": "same name - check it's the same account"}
    return {"status": "missing", "why": "not on the site yet"}


def studio_proposal(box, studio):
    """FansDB's way to add a creator's account as a studio: "username (Network)", under
    the network's own studio, with the one account link (docs.fansdb.cc/studios).
    None when the studio has no account link on a network the site knows."""
    if not studio:
        return None
    try:
        sites = supported(box, "querySites", lambda: cached(box, "sites_full", lambda: box.call("{ querySites { sites { id name url regex valid_types } } }")["querySites"]["sites"]))
    except Exception:
        return None
    if not sites:
        return None
    for u in [x for x in (studio.get("urls") or []) + [studio.get("url")] if x]:
        for site in sites:
            if "STUDIO" not in (site.get("valid_types") or []) or not site.get("regex") or not re.match(site["regex"], u):
                continue
            base = (site.get("url") or "").rstrip("/")
            m = re.match(re.escape(base) + r"/(?:@)?([A-Za-z0-9_.-]+)/?$", re.sub(r"^https?://(www\.)?", "https://", u.split("?")[0]).rstrip("/")) if base else None
            user = m and m.group(1)
            if not user:
                continue
            # the network: a top-level studio on the site's own address, named "... (network)"
            r = cached(box, "network:" + site["name"].lower(), lambda: box.call(
                "query($n: String!) { queryStudios(input: {names: $n, has_parent: false, page: 1, per_page: 10}) { studios { id name urls { url } } } }",
                {"n": site["name"]}))
            nets = [x for x in (r.get("queryStudios") or {}).get("studios") or []
                    if "(network)" in x["name"].lower() or any(base and base in (y["url"] or "") for y in x.get("urls") or [])]
            if not nets:
                continue
            net = nets[0]
            network_name = re.sub(r"\s*\(network\)\s*$", "", net["name"], flags=re.I) or site["name"]
            link = f"{base}/{user}"
            return {"name": f"{user} ({network_name})", "parent": {"id": net["id"], "name": net["name"]},
                    "url": link, "site_id": site["id"], "site": site["name"],
                    "comment": f"Account studio for {link} (the creator's {site['name']} account), named \"username (Network)\" "
                               "per FansDB's studio guidelines. Found while submitting one of its scenes."}
    return None


POST_ACCOUNT = [  # (a post link -> the account it's from)
    (re.compile(r"https?://(?:www\.)?onlyfans\.com/\d+/([A-Za-z0-9_.-]+)", re.I), "https://onlyfans.com/{}", "OnlyFans"),
]


def scene_account(urls):
    """{"url", "user", "network"} of the account a scene's post link is from, or None."""
    for u in urls or []:
        for rx, fmt, net in POST_ACCOUNT:
            m = rx.match(u or "")
            if m:
                return {"url": fmt.format(m.group(1)), "user": m.group(1), "network": net}
    return None


def account_studio(acct):
    """A stand-in for "the studio of this account" (matched / proposed like a Stash studio)."""
    return {"id": None, "name": acct["user"], "url": acct["url"], "urls": [acct["url"]],
            "parent_studio": {"name": acct["network"]}, "stash_ids": []}


def studio_edit(stash, endpoint, studio_id, account_url=None):
    """Submits the proposed studio to the site as a (public) studio edit."""
    rules, host = site_of(endpoint)
    if host != "fansdb.cc":
        raise RuntimeError("Adding studios from here is only for FansDB (StashDB's studios follow different rules - add it on the site)")
    box = box_for(stash, endpoint)
    acct = scene_account([account_url]) if account_url and re.search(r"/\d+/", account_url) else None
    if account_url and not acct:
        m = re.match(r"https?://(?:www\.)?onlyfans\.com/([A-Za-z0-9_.-]+)/?$", account_url)
        acct = m and {"url": f"https://onlyfans.com/{m.group(1)}", "user": m.group(1), "network": "OnlyFans"}
    st = account_studio(acct) if acct else stash.call(
        "query($id: ID!) { findStudio(id: $id) { id name url urls parent_studio { name } } }", {"id": studio_id})["findStudio"]
    prop = studio_proposal(box, st)
    if not prop:
        raise RuntimeError("Can't tell which account this studio is - add its account link (e.g. https://onlyfans.com/username) in Stash first")
    # (once more: it may have been added since the check)
    have = box.call("query($n: String!) { findStudio(name: $n) { id name } }", {"n": prop["name"]}).get("findStudio")
    if have:
        return {"status": "exists", "studio": have}
    r = box.call("""mutation($i: StudioEditInput!) { studioEdit(input: $i) { id status applied
        target { ... on Studio { id name } } } }""",
                 {"i": {"edit": {"operation": "CREATE", "comment": prop["comment"]},
                        "details": {"name": prop["name"], "parent_id": prop["parent"]["id"],
                                    "urls": [{"url": prop["url"], "site_id": prop["site_id"]}]}}})
    e = r.get("studioEdit") or {}
    target = e.get("target") or None
    sent = _load(STUDIO_EDITS, {})
    sent[prop["name"].lower()] = {"name": prop["name"], "status": e.get("status"), "at": time.time(), "yes": 0, "no": 0,
                                  "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 86400)),
                                  "edit_url": f"{site_base(endpoint)}/edits/{e.get('id')}",
                                  "studio": target and {"id": target["id"], "name": target["name"],
                                                        "url": f"{site_base(endpoint)}/studios/{target['id']}"}}
    _save(STUDIO_EDITS, sent)
    forget_studio(box, st["name"])
    if target and studio_id and not acct:  # (accepted straight away: link your studio to it)
        with contextlib.suppress(Exception):
            add_stash_id(stash, "studio", studio_id, endpoint, target["id"])
    return {"status": e.get("status"), "applied": e.get("applied"), "edit": e.get("id"),
            "edit_url": f"{site_base(endpoint)}/edits/{e.get('id')}", "studio": target, "name": prop["name"]}


STUDIO_EDITS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio_edits.json")


def forget_studio(box, name):
    """Drop cached lookups of a studio (it's just been added)."""
    with _cache_lock:
        data = _load(CACHE, {})
        mine = data.get(box.endpoint) or {}
        for k in [k for k in mine if k.startswith("studio:") and k.endswith(":" + name.lower())]:
            del mine[k]
        _save(CACHE, data)


def my_pending_edits(box):
    """Your edits on the site still waiting for votes (made here or on the site), any kind."""
    try:
        if supported(box, "queryEdits", lambda: True) is None:
            return []
        me = box.call("{ me { id } }")["me"]["id"]
        r = supported(box, "queryEdits", lambda: box.call("""query($u: ID!) { queryEdits(input: {user_id: $u, status: PENDING, page: 1, per_page: 100, sort: CREATED_AT, direction: DESC}) {
            edits { id operation target_type created expires votes { vote }
              details { ... on StudioEdit { name added_urls { url } } ... on PerformerEdit { name } ... on SceneEdit { title } }
              target { ... on Studio { name } ... on Performer { name } ... on Scene { title } } } } }""", {"u": me}))
    except Exception:
        return []
    if r is None:
        return []
    out = []
    for e in (r.get("queryEdits") or {}).get("edits") or []:
        d, t = e.get("details") or {}, e.get("target") or {}
        votes = [v["vote"] for v in e.get("votes") or []]
        out.append({"kind": (e.get("target_type") or "").lower(), "operation": (e.get("operation") or "").lower(),
                    "name": d.get("name") or d.get("title") or t.get("name") or t.get("title") or "",
                    "urls": [u["url"] for u in d.get("added_urls") or []], "created": e.get("created"), "expires": e.get("expires"),
                    "edit_url": f"{site_base(box.endpoint)}/edits/{e['id']}", "yes": votes.count("ACCEPT"), "no": votes.count("REJECT")})
    return out


def my_pending_studios(box):
    """Studios you've asked the site to add that are still waiting for votes."""
    return [e for e in my_pending_edits(box) if e["kind"] == "studio" and e["operation"] == "create" and e["name"]]


def _norm(u):
    return re.sub(r"^https?://(www\.)?", "", (u or "").lower()).rstrip("/")


def pending_for(pending, st, prop_name=None):
    """The pending studio edit that is this studio (same account link, or the name FansDB would give it)."""
    if not st:
        return None
    links = {_norm(u) for u in (st.get("urls") or []) + [st.get("url")] if u}
    names = {st["name"].lower()} | ({prop_name.lower()} if prop_name else set())
    return next((p for p in pending if links & {_norm(u) for u in p["urls"]} or p["name"].lower() in names), None)


def site_studio(scene, host):
    """The studio a scene goes under on the site: FansDB wants the account it was posted from."""
    st = scene.get("studio")
    acct = scene_account(scene.get("urls"))
    links = [u.lower().rstrip("/") for u in ((st or {}).get("urls") or []) + [(st or {}).get("url") or ""] if u]
    if acct and host == "fansdb.cc" and acct["url"].lower() not in links:
        return account_studio(acct), acct
    return st, None


def scenes_by_ids(stash, query, ids):
    """findScenes(ids: ...) - without the scenes deleted since the list was made (Stash refuses the
    whole query when one id is gone, which took a whole site's section down)."""
    ids = list(ids)
    for _ in range(50):
        if not ids:
            return []
        try:
            return stash.call(query, {"ids": ids})["findScenes"]["scenes"]
        except RuntimeError as e:
            m = re.search(r"scene with id (\d+) not found", str(e))
            if not m or m.group(1) not in [str(i) for i in ids]:
                raise
            ids = [i for i in ids if str(i) != m.group(1)]
    raise RuntimeError("too many scenes in the list are gone - run Check again")


# --- "Studios to add" ---------------------------------------------------------------
def studios_all(stash):
    """The studio lists for each site you can submit to."""
    ready = _load(READY, None) or {}
    out = []
    for x in ready.get("sites") or []:
        if x.get("can_edit") is False:
            continue
        try:
            out.append(dict(studio_suggestions(stash, x["endpoint"]), name=x["name"]))
        except Exception as e:
            out.append({"endpoint": x["endpoint"], "name": x["name"], "error": str(e), "site": [], "local": [], "pending": []})
    return {"sites": out}


def studio_suggestions(stash, endpoint):
    """Studios the ready scenes wait for on a site (biggest first), and scenes linked to that site
    that have no studio in your Stash."""
    ready = _load(READY, None) or {}
    box = box_for(stash, endpoint)
    host = site_of(endpoint)[1]
    waiting = {x["id"]: x["sites"][endpoint] for x in ready.get("scenes") or []
               if endpoint in x["sites"] and not x["sites"][endpoint].get("on_site") and x["sites"][endpoint].get("studio_site") in ("missing", "pending")}
    groups = {}
    if waiting:
        found = scenes_by_ids(stash, """query($ids: [ID!]) { findScenes(ids: $ids, filter: {per_page: -1}) { scenes { id title urls
            studio { id name url urls parent_studio { name } } } } }""", list(waiting))
        for sc in found:
            st, acct = site_studio(sc, host)
            if not st:
                continue
            key = (st.get("id") or st.get("url") or "") + ":" + st["name"].lower()
            g = groups.setdefault(key, {"studio": st, "acct": acct, "scenes": [], "ready": 0})
            g["scenes"].append({"id": sc["id"], "title": sc.get("title") or ""})
            if not waiting[sc["id"]]["missing"]:
                g["ready"] += 1
    # still not there? (the saved answer - or the site's, after "Look up again"); ones found since drop out
    def still(item):
        key, g = item
        try:
            return cached(box, "studio:" + key, lambda: match_studio(box, endpoint, g["studio"]), max_age=keep_for).get("status")
        except Later:
            return "later"
        except Exception:
            return "missing"
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        now = list(pool.map(still, list(groups.items())))
    later = {k for (k, g), st_ in zip(list(groups.items()), now) if st_ == "later"}
    groups = {k: g for (k, g), st_ in zip(list(groups.items()), now) if st_ in ("missing", "later")}
    with concurrent.futures.ThreadPoolExecutor(6) as pool:  # (FansDB's naming rule - the others are added on the site)
        props = list(pool.map(lambda g: studio_proposal(box, g["studio"]) if host == "fansdb.cc" else None, groups.values()))
    sent = _load(STUDIO_EDITS, {})
    pending = my_pending_studios(box)
    rows = []
    for (key, g), prop in zip(groups.items(), props):
        st, acct = g["studio"], g["acct"]
        wait = pending_for(pending, st, prop and prop["name"])
        here = prop and sent.get(prop["name"].lower())
        if wait:
            here = {"status": "PENDING", "edit_url": wait["edit_url"], "expires": wait["expires"], "yes": wait["yes"], "no": wait["no"], "name": wait["name"]}
        elif here and here.get("status") == "PENDING":
            here = None  # (no longer waiting: accepted, or turned down - a Check shows which)
        rows.append({"name": st["name"], "local_id": st.get("id"), "account_url": acct and acct["url"],
                     "links": [u for u in (st.get("urls") or []) + [st.get("url")] if u][:4],
                     "proposal": prop, "sent": here,
                     "count": len(g["scenes"]), "ready": g["ready"], "scenes": g["scenes"][:4], "unchecked": key in later})
    rows.sort(key=lambda r: (-r["ready"], -r["count"], r["name"].lower()))
    try:
        local = local_missing(stash, box, endpoint)
    except Exception:
        local = []
    ages = [t for (e, k), t in _saved_at.items() if e == endpoint and k.startswith("studio:")]
    return {"endpoint": endpoint, "site": rows, "pending": pending, "local": local, "oldest": min(ages, default=None), "remaining": len(later)}


def performer_suggestions(stash):
    """For each site you can submit to: the performers its ready scenes need that aren't linked
    there yet - found on the site (link them), a name match to check, or not there (send a draft)."""
    ready = _load(READY, None) or {}
    out = []
    for site in ready.get("sites") or []:
        if site.get("can_edit") is False:
            continue
        endpoint = site["endpoint"]
        host = site_of(endpoint)[1]
        ids = [x["id"] for x in ready.get("scenes") or [] if endpoint in x["sites"]
               and not x["sites"][endpoint].get("on_site") and not x["sites"][endpoint].get("sent")]
        if not ids:
            out.append({"endpoint": endpoint, "name": site["name"], "rows": [], "drafts": []})
            continue
        try:
            box = box_for(stash, endpoint)
            scenes = scenes_by_ids(stash, """query($ids: [ID!]) { findScenes(ids: $ids, filter: {per_page: -1}) { scenes { id title
                performers { id name alias_list urls gender image_path stash_ids { endpoint stash_id } } } } }""", ids)
        except Exception as e:
            out.append({"endpoint": endpoint, "name": site["name"], "error": str(e), "rows": [], "drafts": []})
            continue
        people = {}
        for sc in scenes:
            for p in sc.get("performers") or []:
                if sid(p, endpoint):
                    continue
                g = people.setdefault(p["id"], {"p": p, "scenes": []})
                g["scenes"].append({"id": sc["id"], "title": sc.get("title") or ""})

        def look(g, box=box, endpoint=endpoint):
            p = g["p"]
            try:  # (kept a day; "not there yet" an hour)
                return cached(box, "performer2:" + p["id"], lambda: match_performer(box, endpoint, p),
                              max_age=keep_for)
            except Later:
                return {"status": "later"}
            except Exception as e:
                return {"status": "unknown", "why": str(e)}
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            found = list(pool.map(look, people.values()))
        try:  # performer drafts you've sent and edits waiting for votes, by name
            drafts = {(d.get("data") or {}).get("name", "").lower(): f"{site_base(endpoint)}/drafts/{d['id']}"
                      for d in (supported(box, "findDrafts", lambda: box.call("{ findDrafts { id data { __typename ... on PerformerDraft { name } } } }")) or {}).get("findDrafts") or []
                      if (d.get("data") or {}).get("__typename") == "PerformerDraft"}
        except Exception:
            drafts = {}
        waiting = {e["name"].lower(): e for e in my_pending_edits(box) if e["kind"] == "performer"}
        said_no = _load(PERF_NO, {}).get(endpoint) or {}
        rows = []
        for g, m in zip(people.values(), found):
            p = g["p"]
            m = dict(m)
            if m.get("status") == "maybe":  # (the look-alikes you said aren't them)
                m["candidates"] = [c for c in m.get("candidates") or [] if c["id"] not in said_no.get(p["id"], [])]
                if not m["candidates"]:
                    m["status"], m["why"] = "missing", "not on the site (you said the look-alikes aren't them)"
            img = p.get("image_path") or ""
            row = {"id": p["id"], "name": p["name"], "status": m.get("status"), "box": m.get("box"), "candidates": m.get("candidates") or [],
                   "image": None if "default=true" in img else img,
                   "why": m.get("why"), "count": len(g["scenes"]), "scenes": g["scenes"][:4],
                   "draft": drafts.get(p["name"].lower()), "pending": waiting.get(p["name"].lower())}
            if row["status"] == "missing" and not row["pending"]:
                try:
                    row["proposal"] = performer_proposal(box, host, stash.call(PERFORMER, {"id": p["id"]})["findPerformer"])
                except Exception:
                    row["proposal"] = None
            rows.append(row)
        order = {"found": 0, "maybe": 1, "missing": 2}
        rows.sort(key=lambda r: (order.get(r["status"], 3), -r["count"], r["name"].lower()))
        ages = [_saved_at[(endpoint, "performer2:" + r["id"])] for r in rows if (endpoint, "performer2:" + r["id"]) in _saved_at]
        out.append({"endpoint": endpoint, "name": site["name"], "rows": rows, "oldest": min(ages, default=None),
                    "remaining": sum(1 for r in rows if r["status"] == "later"),
                    "base": site_base(endpoint), "stashbox": host != "theporndb.net"})
    return {"sites": out}


PERF_NO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "not_them.json")


def not_them(endpoint, performer_id, candidate_ids):
    """Remembers that the site's look-alikes aren't this performer."""
    data = _load(PERF_NO, {})
    have = data.setdefault(endpoint, {}).setdefault(str(performer_id), [])
    have += [c for c in candidate_ids if c not in have]
    _save(PERF_NO, data)
    return {"ok": True}


def link_performer(stash, endpoint, performer_id, remote_id):
    """Links your performer to the site's (the "remember" Stash's Tagger does)."""
    add_stash_id(stash, "performer", performer_id, endpoint, remote_id)
    box = box_for(stash, endpoint)
    with _cache_lock:
        data = _load(CACHE, {})
        (data.get(box.endpoint) or {}).pop("performer2:" + str(performer_id), None)
        _save(CACHE, data)
    return {"ok": True}


def local_missing(stash, box, endpoint):
    """Scenes linked to the site that have no studio in your Stash, by the site's studio."""
    scenes = stash.call("""query($e: String!) { findScenes(scene_filter: {stash_id_endpoint: {endpoint: $e, modifier: NOT_NULL},
        studios: {modifier: IS_NULL}}, filter: {per_page: -1}) { scenes { id title stash_ids { endpoint stash_id } } } }""",
                        {"e": endpoint})["findScenes"]["scenes"]

    def site_scene(sc):
        try:  # (kept a day)
            return cached(box, "scene_studio:" + sid(sc, endpoint), lambda: box.call(
                "query($i: ID!) { findScene(id: $i) { studio { id name urls { url } parent { name } } } }",
                {"i": sid(sc, endpoint)}).get("findScene"))
        except Exception:
            return None
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        remote = list(pool.map(site_scene, scenes))
    groups = {}
    for sc, r in zip(scenes, remote):
        st = (r or {}).get("studio")
        if not st:
            continue
        g = groups.setdefault(st["id"], {"site_studio": {"id": st["id"], "name": st["name"], "url": f"{site_base(endpoint)}/studios/{st['id']}",
                                                         "links": [u["url"] for u in st.get("urls") or []],
                                                         "network": (st.get("parent") or {}).get("name")}, "scenes": []})
        g["scenes"].append({"id": sc["id"], "title": sc.get("title") or ""})
    out = []
    styled = account_style(stash)
    for g in groups.values():
        name, parent = local_name(g["site_studio"], styled)
        have = local_studio_for(stash, endpoint, g["site_studio"], name)
        out.append(dict(g, local_name=name, local_parent=parent, existing=have))
    return out


def local_name(site_st, with_platform=False):
    """Your Stash's style: the account's username under its network ("izzygreen" under "OnlyFans"),
    or with the network after it ("izzygreen (OnlyFans)") when Better Organizing Tools' "Naming:
    account studios with their platform" is on."""
    m = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", site_st["name"])
    if m and site_st.get("network"):
        return (f"{m.group(1)} ({m.group(2)})" if with_platform else m.group(1)), m.group(2)
    return site_st["name"], None


def account_style(stash):
    """Better Organizing Tools' "Naming: account studios with their platform" (off if it isn't there)."""
    try:
        return bool(((stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {})
                     .get("BetterTaggingTools") or {}).get("namingAccountPlatform"))
    except Exception:
        return False


def local_studio_for(stash, endpoint, site_st, name):
    """A studio already in your Stash for it: linked to it, with one of its links, or the same name."""
    r = stash.call("""query($e: String!, $i: String!) { findStudios(studio_filter: {stash_id_endpoint: {endpoint: $e, stash_id: $i, modifier: EQUALS}})
        { studios { id name } } }""", {"e": endpoint, "i": site_st["id"]})["findStudios"]["studios"]
    if r:
        return r[0]
    for u in site_st["links"]:
        r = stash.call("query($u: String!) { findStudios(studio_filter: {url: {value: $u, modifier: INCLUDES}}) { studios { id name } } }",
                       {"u": u.split("://", 1)[-1].rstrip("/")})["findStudios"]["studios"]
        if len(r) == 1:
            return r[0]
    for n in (name, site_st["name"]):
        r = stash.call("query($n: String!) { findStudios(studio_filter: {name: {value: $n, modifier: EQUALS}}) { studios { id name } } }",
                       {"n": n})["findStudios"]["studios"]
        if r:
            return r[0]
    return None


def make_local_studio(stash, endpoint, site_studio_id, scene_ids):
    """Creates (or reuses) the studio in your Stash, links it to the site's, and sets it on the scenes."""
    box = box_for(stash, endpoint)
    st = box.call("query($i: ID!) { findStudio(id: $i) { id name urls { url } parent { name } } }", {"i": site_studio_id}).get("findStudio")
    if not st:
        raise RuntimeError("That studio isn't on the site any more")
    site_st = {"id": st["id"], "name": st["name"], "links": [u["url"] for u in st.get("urls") or []], "network": (st.get("parent") or {}).get("name")}
    name, parent = local_name(site_st, account_style(stash))
    have = local_studio_for(stash, endpoint, site_st, name)
    made = False
    if have:
        add_stash_id(stash, "studio", have["id"], endpoint, st["id"])
        studio_id, studio_name = have["id"], have["name"]
    else:
        inp = {"name": name, "urls": site_st["links"], "stash_ids": [{"endpoint": endpoint, "stash_id": st["id"]}]}
        if parent:
            p = stash.call("query($n: String!) { findStudios(studio_filter: {name: {value: $n, modifier: EQUALS}}) { studios { id } } }",
                           {"n": parent})["findStudios"]["studios"]
            inp["parent_id"] = p[0]["id"] if p else stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id } }",
                                                               {"i": {"name": parent}})["studioCreate"]["id"]
        studio_id = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id } }", {"i": inp})["studioCreate"]["id"]
        studio_name, made = name, True
    ids = [str(x) for x in scene_ids]
    if ids:
        stash.call("mutation($i: BulkSceneUpdateInput!) { bulkSceneUpdate(input: $i) { id } }", {"i": {"ids": ids, "studio_id": studio_id}})
    return {"id": studio_id, "name": studio_name, "made": made, "scenes": len(ids)}


PF = "id name disambiguation aliases country birth_date scene_count images { url }"  # (enough to tell people apart)


def site_person(x):
    """What the page shows of one of the site's performers."""
    return {"id": x["id"], "name": x["name"], "disambiguation": x.get("disambiguation"), "aliases": (x.get("aliases") or [])[:6],
            "country": x.get("country"), "birth_date": x.get("birth_date"), "scene_count": x.get("scene_count"),
            "image": ((x.get("images") or [{}])[0] or {}).get("url")}


def match_performer(box, endpoint, p):
    have = sid(p, endpoint)
    if have:
        r = box.call(f"query($id: ID!) {{ findPerformer(id: $id) {{ {PF} }} }}", {"id": have}).get("findPerformer")
        if r:
            return {"status": "linked", "box": site_person(r), "why": "already linked in your Stash"}
    for u in p.get("urls") or []:
        try:
            r = box.call(f"query($u: String!) {{ queryPerformers(input: {{url: $u, page: 1, per_page: 5}}) {{ performers {{ {PF} }} }} }}", {"u": u})
        except RuntimeError:
            break  # (ThePornDB's version of this search wants other fields - the name search below still runs)
        hits = ((r.get("queryPerformers") or {}).get("performers") or [])
        if len(hits) == 1:
            return {"status": "found", "box": site_person(hits[0]), "why": f"same link ({u})"}
    cands = []
    for term in [p["name"]] + (p.get("alias_list") or [])[:3]:
        r = box.call(f"query($t: String!) {{ searchPerformer(term: $t, limit: 5) {{ {PF} }} }}", {"t": term})
        for x in r.get("searchPerformer") or []:
            if x["name"].lower() == term.lower() or term.lower() in [a.lower() for a in x.get("aliases") or []]:
                if x["id"] not in [c["id"] for c in cands]:
                    cands.append(site_person(x))
    if cands:
        return {"status": "maybe", "candidates": cands, "why": "same name - check it's the same person"}
    return {"status": "missing", "why": "not on the site yet"}


CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lookups.json")
CACHE_DAYS = 1
_cache_lock = threading.Lock()


DAY = 86400
KEEP = {"found": 30 * DAY, "linked": 30 * DAY, "maybe": 14 * DAY}  # ("not there yet": 3 days)
FRESH = {}    # site -> time: answers older than that are looked up again ("Look up again", done in rounds)
BATCH = {"n": None, "spent": 0}  # new performer / studio lookups allowed this round (None: no limit)
_saved_at = {}  # (site, key) -> when the answer used was looked up


def keep_for(m):
    """How long a performer / studio answer is kept: a match a month, a name match two weeks,
    "not there yet" three days (it's dropped sooner when you act on it here)."""
    return KEEP.get((m or {}).get("status"), 3 * DAY)


def cached(box, key, fetch, max_age=None):
    """A lookup that rarely changes (a tag by name, the site list), kept for a day per site.
    `max_age(value)`: seconds to keep that answer instead (e.g. less for "not there yet")."""
    with _cache_lock:
        hit = (_load(CACHE, {}).get(box.endpoint) or {}).get(key)
    lookup = key.startswith(("performer2:", "studio:"))
    fresh = lookup and hit and hit["at"] < FRESH.get(box.endpoint, 0)
    if hit and not fresh and time.time() - hit["at"] < (max_age(hit["v"]) if max_age else CACHE_DAYS * 86400):
        _saved_at[(box.endpoint, key)] = hit["at"]
        return hit["v"]
    if lookup and BATCH["n"] is not None:  # (this round's share is used up: the next round does it)
        with _cache_lock:
            if BATCH["spent"] >= BATCH["n"]:
                raise Later()
            BATCH["spent"] += 1
    if getattr(box, "gap", 0):  # (a paced site: only so many new lookups per run)
        with _cache_lock:
            if Box._spent.get(box.host, 0) >= Box.LIMIT:
                raise Later()
            Box._spent[box.host] = Box._spent.get(box.host, 0) + 1
    v = fetch()
    with _cache_lock:
        data = _load(CACHE, {})
        data.setdefault(box.endpoint, {})[key] = {"at": time.time(), "v": v}
        _save(CACHE, data)
    _saved_at[(box.endpoint, key)] = time.time()
    return v


def supported(box, feature, fetch):
    """fetch() - or None when the site doesn't have that feature (e.g. ThePornDB has no site
    list, drafts list or edits list). "Doesn't have it" is remembered for a day."""
    def go():
        try:
            return {"ok": True, "v": fetch()}
        except RuntimeError as e:
            if re.search(r"Cannot query field|answered 4(00|22)|Unknown (type|argument)|must have a selection", str(e)):
                return {"ok": False, "keep": 86400}
            if re.search(r"answered 5\d\d", str(e)):  # (ThePornDB answers some with a server error - could be passing, so an hour)
                return {"ok": False, "keep": 3600}
            raise
    key = "unsupported:" + feature
    with _cache_lock:
        hit = (_load(CACHE, {}).get(box.endpoint) or {}).get(key)
    if hit and time.time() - hit["at"] < (hit.get("keep") or 86400):
        return None
    r = go()
    if r["ok"]:
        return r["v"]
    with _cache_lock:
        data = _load(CACHE, {})
        data.setdefault(box.endpoint, {})[key] = {"at": time.time(), "v": True, "keep": r["keep"]}
        _save(CACHE, data)
    return None


def match_tags(box, tags):
    def one(t):
        return cached(box, "tag:" + t["name"].lower(), lambda: box.call(
            "query($n: String!) { findTagOrAlias(name: $n) { id name } }", {"n": t["name"]}).get("findTagOrAlias"))
    try:
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            found = list(pool.map(one, tags))
    except RuntimeError:
        return [{"local": x["name"], "box": None} for x in tags]  # (this site can't look tags up by name)
    return [{"local": t["name"], "box": r} for t, r in zip(tags, found)]


def valid_urls(box, urls, kind="SCENE"):
    try:
        sites = supported(box, "querySites", lambda: cached(box, "sites", lambda: box.call("{ querySites { sites { name regex valid_types } } }")["querySites"]["sites"]))
        if sites is None:
            raise RuntimeError("no site list")
    except Exception:
        return [{"url": u, "ok": True, "site": None} for u in urls]
    out = []
    for u in urls:
        site = next((s for s in sites if kind in (s.get("valid_types") or []) and s.get("regex")
                     and re.match(s["regex"], u)), None)
        out.append({"url": u, "ok": bool(site), "site": site and site["name"],
                    # (a content platform - a site studios can be on too - not a social one)
                    "platform": bool(site and "STUDIO" in (site.get("valid_types") or []))})
    return out


def clean_title(t):
    """One line, decorative lettering turned into plain letters (emojis kept)."""
    t = " ".join((t or "").split())
    return STYLED.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), t)


def check(stash, scene_id, endpoint):
    rules, host = site_of(endpoint)
    box = box_for(stash, endpoint)
    scene = stash.call(SCENE, {"id": scene_id})["findScene"]
    if not scene:
        raise RuntimeError(f"no scene {scene_id}")
    fps = fingerprints(scene)
    # the site lookups all at once (one after another took up to a minute when the site is slow)
    pool = concurrent.futures.ThreadPoolExecutor(8)
    later = {"roles": pool.submit(roles, box),
             "urls": pool.submit(valid_urls, box, scene.get("urls") or []),
             "tags": pool.submit(match_tags, box, scene.get("tags") or [])}
    if fps:
        later["dups"] = pool.submit(box.call, "query($f: [[FingerprintQueryInput!]!]!) { findScenesBySceneFingerprints(fingerprints: $f) { id title date studio { name } } }",
                                    {"f": [[{"hash": f["hash"], "algorithm": f["algorithm"]} for f in fps]]})

    if fps:
        later["pending"] = pool.submit(supported, box, "queryExistingScene", lambda: box.call(
            """query($i: QueryExistingSceneInput!) { queryExistingScene(input: $i) { edits { id status user { name }
                details { ... on SceneEdit { title } } } } }""",
            {"i": {"title": clean_title(scene.get("title")) or None,
                   "fingerprints": [{"hash": f["hash"], "algorithm": f["algorithm"], "duration": f["duration"]} for f in fps]}}))

    def performer_look(p):
        m = match_performer(box, endpoint, p)
        if m["status"] == "missing":
            full = stash.call(PERFORMER, {"id": p["id"]})["findPerformer"]
            m["proposal"] = performer_proposal(box, host, full)
        return m
    later["performers"] = [pool.submit(performer_look, p) for p in scene.get("performers") or []]
    # the studio: FansDB wants the account it was posted from (worked out here, the issue is added below)
    acct = scene_account(scene.get("urls"))
    local_st = scene.get("studio")
    st_links = [x.lower().rstrip("/") for x in ((local_st or {}).get("urls") or []) + [(local_st or {}).get("url") or ""] if x]
    use_st = account_studio(acct) if acct and host == "fansdb.cc" and acct["url"].lower() not in st_links else local_st

    def studio_look():
        m = match_studio(box, endpoint, use_st)
        if m["status"] == "missing" and host == "fansdb.cc":
            m["proposal"] = studio_proposal(box, use_st)
        return m
    later["studio"] = pool.submit(studio_look)
    my_roles = later["roles"].result()
    if out_sent := (_load(SENT, {}).get(str(scene["id"])) or {}).get(endpoint):
        out_sent = dict(out_sent, state=sent_state(box, out_sent, clean_title(scene.get("title"))))
    out = {"site": {"name": rules["name"] or host, "endpoint": endpoint, "base": site_base(endpoint), "roles": my_roles,
                    "can_edit": None if my_roles is None else ("EDIT" in my_roles or "ADMIN" in my_roles), "rules": rules["rules"]},
           "scene": {"id": scene["id"], "linked": sid(scene, endpoint)}, "issues": [], "notes": [],
           "sent": out_sent}

    def issue(level, text, field=None):
        # (field: the window clears it once that field is fixed there)
        out["issues"].append({"level": level, "text": text, "field": field})

    if out["site"]["can_edit"] is False:
        issue("block", f"Your {out['site']['name']} account can't submit edits yet (it's read-only)."
              + (" StashDB gives edit rights on request: see guidelines.stashdb.org → Contributing." if host == "stashdb.org" else ""))
    if out["scene"]["linked"]:
        issue("block", f"This scene is already on {out['site']['name']} (linked in your Stash) - use Stash's \"Submit to "
              "Stash-box\" update instead, or fix it on the site.")
    # content the site doesn't take
    links = [u for u in scene.get("urls") or []] + [u for u in (scene.get("studio") or {}).get("urls") or []]
    bad = [d for d in rules["not_allowed"] if any(d in (u or "") for u in links)]
    if bad:
        issue("block", f"{out['site']['name']} doesn't accept content from {', '.join(bad)} (its guidelines).")
    # fingerprints and duplicates
    out["fingerprints"] = [{"algorithm": f["algorithm"], "duration": f["duration"]} for f in fps]
    if not any(f["algorithm"] == "PHASH" for f in fps):
        issue("block", "No video fingerprint (phash) yet: run Stash's Generate task with \"Perceptual hashes\" on, then check again.")
    dups = []
    if fps:
        try:
            r = later["dups"].result()
            dups = (r.get("findScenesBySceneFingerprints") or [[]])[0] or []
        except Exception as e:
            out["notes"].append(f"Couldn't check for duplicates ({e}).")
    waiting = []
    if "pending" in later:
        try:
            r = later["pending"].result() or {}
            me_name = None
            for e in (r.get("queryExistingScene") or {}).get("edits") or []:
                if e.get("status") == "PENDING":
                    waiting.append({"url": f"{site_base(endpoint)}/edits/{e['id']}", "title": (e.get("details") or {}).get("title") or "",
                                    "by": (e.get("user") or {}).get("name") or ""})
            if waiting:
                with contextlib.suppress(Exception):
                    me_name = (box.call("{ me { name } }").get("me") or {}).get("name")
                mine = [w for w in waiting if me_name and w["by"] == me_name]
                issue("dup", (f"You've already submitted this to {out['site']['name']} - it's waiting for votes. No need to send it again."
                              if mine else f"Someone has already submitted this to {out['site']['name']} ({waiting[0]['by']}) - it's waiting for votes; "
                              "vote on it there instead of sending it again."), "duplicates")
        except Exception as e:
            out["notes"].append(f"Couldn't check for submissions waiting for votes ({e}).")
    out["waiting"] = waiting
    out["duplicates"] = [{"id": d["id"], "title": d.get("title"), "date": d.get("date"),
                          "studio": (d.get("studio") or {}).get("name"), "url": f"{site_base(endpoint)}/scenes/{d['id']}"} for d in dups]
    if dups:
        # (a fingerprint can match a different video - e.g. many broken files share one -
        # so you look, and tick "it's a different video" to carry on)
        issue("dup", f"{out['site']['name']} has {len(dups)} scene(s) with the same fingerprint - if one is this video, "
              "link it with Stash's Tagger instead of adding it again.", "duplicates")
    # fields
    title = clean_title(scene.get("title"))
    if not title and "title" in rules["required"]:
        issue("block", "No title. " + ("FansDB: use one adapted from the description, else the username and date."
                                       if host == "fansdb.cc" else "Use the studio's own title."), "title")
    elif scene.get("title") and title != " ".join(scene["title"].split()):
        out["notes"].append("Title: made one line and plain letters (the site doesn't allow decorative Unicode).")
    out["title"] = title
    out["date"] = scene.get("date") or ""
    if not out["date"] and "date" in rules["required"]:
        issue("block", "No date (FansDB uses 1970-01-01 only when the date is truly unknown).", "date")
    elif out["date"] and not re.fullmatch(r"\d{4}(-\d\d(-\d\d)?)?", out["date"]):
        issue("block", f"Date \"{out['date']}\" isn't YYYY-MM-DD.", "date")
    out["details"] = scene.get("details") or ""
    if host == "stashdb.org":
        out["notes"].append("StashDB wants the studio's own title and description, unchanged - check them against the studio's page.")
    out["code"] = scene.get("code") or ""
    out["urls"] = later["urls"].result()
    if any(not u["ok"] for u in out["urls"]):
        out["notes"].append("Links the site has no matching site for are left out (its rule).")
    if not any(u["ok"] for u in out["urls"]):
        issue("warn", "No link the site recognises - the reviewers will want a source link.")
    # studio, performers, tags
    if use_st is not local_st:
        # FansDB: the studio is the account it was posted from (not another account of the creator's)
        issue("warn", f"This video was posted on {acct['user']}'s {acct['network']}"
              + (f", but its studio in your Stash is \"{local_st['name']}\"" if local_st else "")
              + f" - FansDB wants the studio to be the account it came from, so {acct['user']} ({acct['network']}) is used here "
              "(your Stash isn't changed).")
    out["account"] = acct
    out["studio"] = dict(later["studio"].result(), local=use_st and use_st.get("id") and
                         {"id": use_st["id"], "name": use_st["name"]} or (use_st and {"id": None, "name": use_st["name"]}))
    if out["studio"]["status"] == "none" and "studio" in rules["required"]:
        issue("block", "No studio on the scene.", "studio")
    if out["studio"]["status"] == "missing" and host == "theporndb.net":
        # (ThePornDB's drafts can name a studio it doesn't have yet - its team adds studios when reviewing;
        # its API has no way for anyone else to add one)
        out["studio"]["by_name"] = use_st["name"]
        issue("warn", f"The studio \"{use_st['name']}\" isn't on ThePornDB - it's sent by name in the draft, for ThePornDB's "
              "team to add when they review it. Check the draft there.", "studio")
    elif out["studio"]["status"] == "missing":
        issue("block", f"The studio \"{use_st['name']}\" isn't on {out['site']['name']} yet - add it there first "
              + ("(FansDB names accounts \"username (Network)\")." if host == "fansdb.cc" else "."), "studio")
    out["performers"] = []
    for p, fut in zip(scene.get("performers") or [], later["performers"]):
        m = fut.result()
        if m["status"] == "missing":
            if acct and not m["proposal"]["links"] and len(scene.get("performers") or []) == 1:
                m["proposal"]["add_link"] = acct["url"]  # (a button adds it to them in your Stash)
        out["performers"].append(dict(m, local={"id": p["id"], "name": p["name"]}))
        if m["status"] == "missing":
            issue("block", f"{p['name']} isn't on {out['site']['name']} yet - send a performer draft first (button below).",
                  "performer:" + p["id"])
    if not out["performers"] and "performers" in rules["required"]:
        issue("warn" if host == "fansdb.cc" else "block", "No performers. " + (
            "FansDB: tag \"Missing Performer (Female/Male/Trans)\" when nobody's credited." if host == "fansdb.cc" else ""))
    out["tags"] = later["tags"].result()
    pool.shutdown(wait=False)
    out["cover"] = (scene.get("paths") or {}).get("screenshot") or ""
    out["comment"] = comment(out, scene)
    return out


def comment(out, scene):
    """A suggested edit comment (the site asks for your sources)."""
    src = [u["url"] for u in out["urls"] if u["ok"]]
    lines = [f"Scene from {src[0]}." if src else "Scene from the creator's own release."]
    if out["site"]["name"] == "FansDB":
        lines.append("Title adapted from the post text; date and description from the post; studio and performer from the account.")
    lines.append("Tags from watching the video." if any(t.get("box") for t in out["tags"]) else "No tags added.")
    lines.append("Fingerprints and cover from Stash.")
    return " ".join(lines)


def submit(stash, scene_id, endpoint, ch):
    """Sends the draft built from the check and your choices."""
    box = box_for(stash, endpoint)
    scene = stash.call(SCENE, {"id": scene_id})["findScene"]
    inp = {"title": clean_title(ch.get("title") or ""), "date": ch.get("date") or None, "details": ch.get("details") or None,
           "code": ch.get("code") or None, "urls": [u for u in ch.get("urls") or [] if u],
           "fingerprints": fingerprints(scene)}
    st = ch.get("studio") or {}
    if st.get("id"):
        inp["studio"] = {"id": st["id"], "name": st.get("name")}
    elif st.get("name"):
        inp["studio"] = {"name": st["name"]}  # (waiting for approval: pick it on the draft page once it's there)
    inp["performers"] = [{"id": p["id"], "name": p.get("name")} for p in ch.get("performers") or [] if p.get("id")]
    inp["tags"] = [{"id": t["id"], "name": t.get("name")} for t in ch.get("tags") or [] if t.get("id")]
    q = "mutation($input: SceneDraftInput!) { submitSceneDraft(input: $input) { id } }"
    files = None
    if ch.get("cover") and scene.get("paths", {}).get("screenshot"):
        path = urllib.parse.urlparse(scene["paths"]["screenshot"])
        data, ctype = stash.raw(path.path + ("?" + path.query if path.query else ""))
        inp["image"] = None
        files = {"variables.input.image": ("cover.jpg", data, ctype.split(";")[0])}
    r = box.call(q, {"input": inp}, files)
    draft = ((r.get("submitSceneDraft") or {}).get("id"))
    if not draft:
        raise RuntimeError("the site didn't make a draft")
    # remember the links you confirmed (next time they're linked straight away)
    if ch.get("link"):
        for p in ch.get("performers") or []:
            if p.get("id") and p.get("local_id"):
                add_stash_id(stash, "performer", p["local_id"], endpoint, p["id"])
        if st.get("id") and st.get("local_id"):
            add_stash_id(stash, "studio", st["local_id"], endpoint, st["id"])
    return {"draft": draft, "url": f"{site_base(endpoint)}/drafts/{draft}"}


# --- submitting straight away (an edit, as the site's "Submit Edit" does) ---------------
# Stash-box sites only (FansDB, StashDB with edit rights); ThePornDB takes drafts only.
EDIT_HOSTS = ("fansdb.cc", "stashdb.org")


def can_submit_edits(box, host):
    if host not in EDIT_HOSTS:
        raise RuntimeError("This site only takes drafts - finish it on the site")
    ro = roles(box)
    if ro is not None and not ({"EDIT", "ADMIN"} & set(ro)):
        raise RuntimeError("Your account on this site can't submit edits (read-only)")


def url_inputs(box, urls, kind):
    """The links as the site wants them in an edit: each with the site it belongs to (others left out)."""
    sites = supported(box, "querySites", lambda: cached(box, "sites_full", lambda: box.call(
        "{ querySites { sites { id name url regex valid_types } } }")["querySites"]["sites"])) or []
    out = []
    for u in urls:
        site = next((x for x in sites if kind in (x.get("valid_types") or []) and x.get("regex") and re.match(x["regex"], u)), None)
        if site and u not in [o["url"] for o in out]:
            out.append({"url": u, "site_id": site["id"]})
    return out


def upload_image(box, data, ctype, name="image.jpg"):
    r = box.call("mutation($input: ImageCreateInput!) { imageCreate(input: $input) { id } }", {"input": {"file": None}},
                 {"variables.input.file": (name, data, ctype.split(";")[0])})
    img = (r.get("imageCreate") or {}).get("id")
    if not img:
        raise RuntimeError("the site didn't take the picture")
    return img


def stash_image(stash, url):
    path = urllib.parse.urlparse(url)
    return stash.raw(path.path + ("?" + path.query if path.query else ""))


def remember_sent(scene_id, endpoint, url, kind="draft"):
    sent = _load(SENT, {})
    sent.setdefault(str(scene_id), {})[endpoint] = {"url": url, "at": time.time(), "kind": kind}
    _save(SENT, sent)


def already_waiting(box, title, fps):
    """A submission of the same video already waiting for votes on the site (anyone's), or None."""
    if not fps:
        return None
    r = supported(box, "queryExistingScene", lambda: box.call(
        "query($i: QueryExistingSceneInput!) { queryExistingScene(input: $i) { edits { id status } } }",
        {"i": {"title": title or None, "fingerprints": [{"hash": f["hash"], "algorithm": f["algorithm"], "duration": f["duration"]} for f in fps]}})) or {}
    e = next((x for x in (r.get("queryExistingScene") or {}).get("edits") or [] if x.get("status") == "PENDING"), None)
    return e and f"{site_base(box.endpoint)}/edits/{e['id']}"


def scene_edit(stash, scene_id, endpoint, ch, comment):
    """Submits the scene as a new-scene edit (public, waiting for votes) - no draft to finish."""
    rules, host = site_of(endpoint)
    box = box_for(stash, endpoint)
    can_submit_edits(box, host)
    if not (comment or "").strip():
        raise RuntimeError("Add a comment saying where the details come from (the site asks for one)")
    scene = stash.call(SCENE, {"id": scene_id})["findScene"]
    st = ch.get("studio") or {}
    if not st.get("id"):
        raise RuntimeError("The studio has to be on the site before the scene can be submitted (a draft can name it instead)")
    perfs = ch.get("performers") or []
    if any(not p.get("id") for p in perfs):
        raise RuntimeError("Every performer has to be on the site first")
    fps = fingerprints(scene)
    waiting = None if ch.get("not_dup") else already_waiting(box, clean_title(ch.get("title") or ""), fps)
    if waiting:
        raise RuntimeError(f"This video is already submitted and waiting for votes ({waiting}) - not sent again")
    details = {k: v for k, v in {
        "title": clean_title(ch.get("title") or "") or None, "date": ch.get("date") or None, "details": ch.get("details") or None,
        "code": ch.get("code") or None, "director": scene.get("director") or None,
        "urls": url_inputs(box, [u for u in ch.get("urls") or [] if u], "SCENE"), "studio_id": st["id"],
        "performers": [{"performer_id": p["id"]} for p in perfs], "tag_ids": [t["id"] for t in ch.get("tags") or [] if t.get("id")],
        "fingerprints": fps, "duration": max((f["duration"] for f in fps), default=0) or None}.items() if v not in (None, "")}
    if ch.get("cover") and (scene.get("paths") or {}).get("screenshot"):
        details["image_ids"] = [upload_image(box, *stash_image(stash, scene["paths"]["screenshot"]), name="cover.jpg")]
    r = box.call("mutation($i: SceneEditInput!) { sceneEdit(input: $i) { id status applied } }",
                 {"i": {"edit": {"operation": "CREATE", "comment": comment.strip()}, "details": details}})
    e = r.get("sceneEdit") or {}
    if not e.get("id"):
        raise RuntimeError("the site didn't take the edit")
    url = f"{site_base(endpoint)}/edits/{e['id']}"
    remember_sent(scene_id, endpoint, url, "edit")
    if ch.get("link"):  # (remember the matches you confirmed)
        for p in perfs:
            if p.get("local_id"):
                add_stash_id(stash, "performer", p["local_id"], endpoint, p["id"])
        if st.get("local_id"):
            add_stash_id(stash, "studio", st["local_id"], endpoint, st["id"])
    return {"edit": e["id"], "url": url, "status": e.get("status"), "applied": e.get("applied")}


ETHNICITY = {"caucasian": "CAUCASIAN", "white": "CAUCASIAN", "black": "BLACK", "african": "BLACK", "asian": "ASIAN", "indian": "INDIAN",
             "latin": "LATIN", "latina": "LATIN", "latino": "LATIN", "hispanic": "LATIN", "middle eastern": "MIDDLE_EASTERN",
             "mixed": "MIXED", "other": "OTHER"}
EYES = {"blue": "BLUE", "brown": "BROWN", "grey": "GREY", "gray": "GREY", "green": "GREEN", "hazel": "HAZEL", "red": "RED"}
HAIR = {"blonde": "BLONDE", "blond": "BLONDE", "brunette": "BRUNETTE", "brown": "BRUNETTE", "black": "BLACK", "red": "RED", "auburn": "AUBURN",
        "grey": "GREY", "gray": "GREY", "bald": "BALD", "various": "VARIOUS", "white": "WHITE", "other": "OTHER"}


def body_mods(text):
    """Stash keeps tattoos / piercings as text; the site wants a list of places."""
    return [{"location": x.strip()} for x in re.split(r"[;,\n]+", text or "") if x.strip()][:20]


def measurements(text):
    """"34C-24-34" -> band 34, cup C, waist 24, hip 34 (what's there)."""
    m = re.match(r"^\s*(\d{2})?\s*([A-Za-z]{1,4})?\s*[-/ ]\s*(\d{2})?\s*[-/ ]\s*(\d{2})?", text or "")
    if not m:
        return {}
    band, cup, waist, hip = m.groups()
    return {k: v for k, v in {"band_size": int(band) if band else None, "cup_size": cup.upper() if cup else None,
                              "waist_size": int(waist) if waist else None, "hip_size": int(hip) if hip else None}.items() if v}


def performer_edit(stash, performer_id, endpoint, comment):
    """Submits a performer the site doesn't have as a new-performer edit (public, waiting for votes)."""
    rules, host = site_of(endpoint)
    box = box_for(stash, endpoint)
    can_submit_edits(box, host)
    if not (comment or "").strip():
        raise RuntimeError("Add a comment saying where the details come from (the site asks for one)")
    p = stash.call(PERFORMER, {"id": performer_id})["findPerformer"]
    prop = performer_proposal(box, host, p)
    if prop["needs"]:
        raise RuntimeError("First add " + " and ".join(prop["needs"]))
    fake = (p.get("fake_tits") or "").lower()
    years = re.findall(r"(?:19|20)\d\d", p.get("career_length") or "")
    details = {k: v for k, v in {
        "name": p["name"], "disambiguation": p.get("disambiguation") or None, "aliases": prop["aliases"] or None,
        "gender": p.get("gender"), "urls": url_inputs(box, prop["links"], "PERFORMER"),
        "birthdate": p.get("birthdate"), "deathdate": p.get("death_date"),
        "ethnicity": ETHNICITY.get((p.get("ethnicity") or "").strip().lower()), "country": p.get("country") or None,
        "eye_color": EYES.get((p.get("eye_color") or "").strip().lower()), "hair_color": HAIR.get((p.get("hair_color") or "").strip().lower()),
        "height": p.get("height_cm") or None,
        "breast_type": "FAKE" if re.search(r"fake|augment|implant|yes", fake) else "NATURAL" if re.search(r"natural|no", fake) else None,
        "career_start_year": int(years[0]) if years else None, "career_end_year": int(years[1]) if len(years) > 1 else None,
        "tattoos": body_mods(p.get("tattoos")) or None, "piercings": body_mods(p.get("piercings")) or None,
        **measurements(p.get("measurements"))}.items() if v not in (None, "", [])}
    if p.get("image_path") and "default=true" not in p["image_path"]:
        details["image_ids"] = [upload_image(box, *stash_image(stash, p["image_path"]), name="performer.jpg")]
    r = box.call("mutation($i: PerformerEditInput!) { performerEdit(input: $i) { id status applied } }",
                 {"i": {"edit": {"operation": "CREATE", "comment": comment.strip()}, "details": details}})
    e = r.get("performerEdit") or {}
    if not e.get("id"):
        raise RuntimeError("the site didn't take the edit")
    return {"edit": e["id"], "url": f"{site_base(endpoint)}/edits/{e['id']}", "status": e.get("status"), "applied": e.get("applied")}


def draft_edit(stash, endpoint, draft_id, comment):
    """Submits a draft you've sent (a scene or performer) as the edit, without opening the site."""
    rules, host = site_of(endpoint)
    box = box_for(stash, endpoint)
    can_submit_edits(box, host)
    if not (comment or "").strip():
        raise RuntimeError("Add a comment saying where the details come from (the site asks for one)")
    d = box.call("""query($id: ID!) { findDraft(id: $id) { id data { __typename
        ... on SceneDraft { title code details director urls date production_date
          studio { __typename ... on Studio { id name } ... on DraftEntity { name eid: id } }
          performers { __typename ... on Performer { id name } ... on DraftEntity { name eid: id } }
          tags { __typename ... on Tag { id name } ... on DraftEntity { name eid: id } }
          image { id } fingerprints { hash algorithm duration } }
        ... on PerformerDraft { name disambiguation aliases gender birthdate deathdate urls ethnicity country eye_color hair_color
          height measurements breast_type tattoos piercings career_start_year career_end_year image { id } } } } }""",
                 {"id": draft_id}).get("findDraft")
    if not d:
        raise RuntimeError("That draft isn't on the site any more (expired or already used)")
    x = d["data"]
    if x["__typename"] == "SceneDraft":
        st = x.get("studio") or {}
        if st.get("__typename") != "Studio":
            raise RuntimeError(f"The draft's studio \"{st.get('name') or '?'}\" isn't picked on the site yet - finish this one on the site")
        loose = [p["name"] for p in x.get("performers") or [] if p.get("__typename") != "Performer"]
        waiting = already_waiting(box, x.get("title"), x.get("fingerprints") or [])
        if waiting:
            raise RuntimeError(f"This video is already submitted and waiting for votes ({waiting}) - this draft isn't needed")
        if loose:
            raise RuntimeError(f"The draft names {', '.join(loose)} without picking them on the site - finish this one on the site")
        details = {k: v for k, v in {
            "title": x.get("title"), "code": x.get("code"), "details": x.get("details"), "director": x.get("director"),
            "date": x.get("date"), "production_date": x.get("production_date"), "urls": url_inputs(box, x.get("urls") or [], "SCENE"),
            "studio_id": st["id"], "performers": [{"performer_id": p["id"]} for p in x.get("performers") or []],
            "tag_ids": [t["id"] for t in x.get("tags") or [] if t.get("__typename") == "Tag"],
            "image_ids": [x["image"]["id"]] if x.get("image") else None,
            "fingerprints": [{"hash": f["hash"], "algorithm": f["algorithm"], "duration": f["duration"]} for f in x.get("fingerprints") or []],
            "duration": max((f["duration"] for f in x.get("fingerprints") or []), default=0) or None,
            "draft_id": draft_id}.items() if v not in (None, "", [])}
        r = box.call("mutation($i: SceneEditInput!) { sceneEdit(input: $i) { id status } }",
                     {"i": {"edit": {"operation": "CREATE", "comment": comment.strip()}, "details": details}})
        e = r.get("sceneEdit") or {}
        url = f"{site_base(endpoint)}/edits/{e.get('id')}"
        sent = _load(SENT, {})  # (the scene it came from now points at the edit)
        for sid_, per in sent.items():
            rec = per.get(endpoint) or {}
            if rec.get("url", "").endswith("/" + draft_id):
                remember_sent(sid_, endpoint, url, "edit")
    else:
        aliases = [a.strip() for a in (x.get("aliases") or "").split(",") if a.strip()]
        upper = lambda v, table: table.get((v or "").strip().lower()) or ((v or "").strip().upper().replace(" ", "_") or None)
        details = {k: v for k, v in {
            "name": x["name"], "disambiguation": x.get("disambiguation"), "aliases": aliases or None,
            "gender": (x.get("gender") or "").upper().replace(" ", "_") or None, "urls": url_inputs(box, x.get("urls") or [], "PERFORMER"),
            "birthdate": x.get("birthdate"), "deathdate": x.get("deathdate"), "ethnicity": upper(x.get("ethnicity"), ETHNICITY),
            "country": x.get("country"), "eye_color": upper(x.get("eye_color"), EYES), "hair_color": upper(x.get("hair_color"), HAIR),
            "height": int(x["height"]) if str(x.get("height") or "").isdigit() else None,
            "breast_type": (x.get("breast_type") or "").upper() or None, "tattoos": body_mods(x.get("tattoos")) or None,
            "piercings": body_mods(x.get("piercings")) or None, "career_start_year": x.get("career_start_year"),
            "career_end_year": x.get("career_end_year"), "image_ids": [x["image"]["id"]] if x.get("image") else None,
            "draft_id": draft_id, **measurements(x.get("measurements"))}.items() if v not in (None, "", [])}
        r = box.call("mutation($i: PerformerEditInput!) { performerEdit(input: $i) { id status } }",
                     {"i": {"edit": {"operation": "CREATE", "comment": comment.strip()}, "details": details}})
        e = r.get("performerEdit") or {}
        url = f"{site_base(endpoint)}/edits/{e.get('id')}"
    if not e.get("id"):
        raise RuntimeError("the site didn't take the edit")
    return {"edit": e["id"], "url": url, "status": e.get("status")}


def add_stash_id(stash, kind, local_id, endpoint, remote):
    F = "performer" if kind == "performer" else "studio"
    cur = stash.call(f"query($id: ID!) {{ find{F.capitalize()}(id: $id) {{ stash_ids {{ endpoint stash_id }} }} }}", {"id": local_id})
    ids = (cur.get(f"find{F.capitalize()}") or {}).get("stash_ids") or []
    if any(s["endpoint"] == endpoint for s in ids):
        return
    ids = [{"endpoint": s["endpoint"], "stash_id": s["stash_id"]} for s in ids] + [{"endpoint": endpoint, "stash_id": remote}]
    stash.call(f"mutation($i: {F.capitalize()}UpdateInput!) {{ {F}Update(input: $i) {{ id }} }}", {"i": {"id": local_id, "stash_ids": ids}})


PERFORMER = """query($id: ID!) { findPerformer(id: $id) { id name disambiguation alias_list gender birthdate death_date
  country ethnicity eye_color hair_color height_cm measurements fake_tits tattoos piercings career_length urls image_path
  stash_ids { endpoint stash_id } } }"""


SOCIAL = re.compile(r"(instagram|twitter|x\.com|tiktok|youtube|facebook|snapchat|threads|reddit|linktr|getallmylinks|allmylinks|beacons)", re.I)


def performer_proposal(box, host, p):
    """What a performer draft would hold, and what the site's rules still need
    (FansDB: a unique main name, gender, at least one approved-site account link,
    usernames as aliases - not social media ones)."""
    links = valid_urls(box, p.get("urls") or [], "PERFORMER")
    ok = [u for u in links if u["ok"]]
    needs = []
    if not p.get("gender"):
        needs.append("their gender (set it in Stash)")
    if not ok:
        needs.append("a link to an account on a site " + ("FansDB" if host == "fansdb.cc" else "the site") + " knows (e.g. their OnlyFans page)")
    # usernames from the content-platform links (OnlyFans, Fansly...), then your aliases -
    # not social media usernames (FansDB: "no social media usernames as aliases")
    handles = []
    for u in [x for x in ok if x.get("platform")]:
        m = re.search(r"https?://[^/]+/(?:@)?([A-Za-z0-9_.-]+)/?$", u["url"].split("?")[0])
        if m and m.group(1).lower() not in (x.lower() for x in handles + [p["name"]]):
            handles.append(m.group(1))
    social = {m.group(1).lower() for u in (p.get("urls") or [])
              if SOCIAL.search(u or "") or any(x["url"] == u and x["ok"] and not x.get("platform") for x in links)
              for m in [re.search(r"https?://[^/]+/(?:add/|profile/)?(?:@)?([A-Za-z0-9_.-]+)", u or "")] if m}
    social |= {re.sub(r"\.(fb|ig|tt)$", "", x) for x in social}
    aliases = handles + [a for a in p.get("alias_list") or [] if a.lower() != p["name"].lower()
                         and a.lower() not in social and a.lower() not in (h.lower() for h in handles)]
    return {"name": p["name"], "disambiguation": p.get("disambiguation") or "", "gender": p.get("gender") or "",
            "aliases": aliases, "links": [u["url"] for u in ok], "left_out": [u["url"] for u in links if not u["ok"]],
            "picture": bool(p.get("image_path") and "default=true" not in p["image_path"]), "needs": needs,
            "name_note": "FansDB: \"FirstName LastName\" when known, else their unique username." if host == "fansdb.cc" else ""}


def performer_draft(stash, performer_id, endpoint):
    """A performer draft from your Stash's performer (for one the site doesn't have)."""
    rules, host = site_of(endpoint)
    box = box_for(stash, endpoint)
    p = stash.call(PERFORMER, {"id": performer_id})["findPerformer"]
    prop = performer_proposal(box, host, p)
    if prop["needs"]:
        raise RuntimeError("First add " + " and ".join(prop["needs"]))
    urls = prop["links"]
    inp = {k: v for k, v in {
        "name": p["name"], "disambiguation": p.get("disambiguation") or None, "aliases": ", ".join(prop["aliases"]),  # (one text, comma-separated)
        "gender": p.get("gender"), "birthdate": p.get("birthdate"), "deathdate": p.get("death_date"),
        "urls": urls, "ethnicity": p.get("ethnicity"), "country": p.get("country"), "eye_color": p.get("eye_color"),
        "hair_color": p.get("hair_color"), "height": str(p["height_cm"]) if p.get("height_cm") else None,
        "measurements": p.get("measurements"), "breast_type": p.get("fake_tits"), "tattoos": p.get("tattoos"),
        "piercings": p.get("piercings")}.items() if v not in (None, "", [])}
    files = None
    if p.get("image_path") and "default=true" not in p["image_path"]:
        path = urllib.parse.urlparse(p["image_path"])
        data, ctype = stash.raw(path.path + ("?" + path.query if path.query else ""))
        inp["image"] = None
        files = {"variables.input.image": ("performer.jpg", data, ctype.split(";")[0])}
    r = box.call("mutation($input: PerformerDraftInput!) { submitPerformerDraft(input: $input) { id } }", {"input": inp}, files)
    draft = (r.get("submitPerformerDraft") or {}).get("id")
    return {"draft": draft, "url": f"{site_base(endpoint)}/drafts/{draft}"}


def add_link(stash, performer_id, url):
    p = stash.call("query($id: ID!) { findPerformer(id: $id) { urls } }", {"id": performer_id})["findPerformer"]
    urls = p.get("urls") or []
    if url not in urls:
        stash.call("mutation($i: PerformerUpdateInput!) { performerUpdate(input: $i) { id } }",
                   {"i": {"id": performer_id, "urls": urls + [url]}})
    return {"added": url}


def sites(stash):
    out = []
    for b in boxes(stash):
        rules, host = site_of(b["endpoint"])
        if host not in SITES:
            continue  # (only the sites whose rules this knows)
        r = roles(Box(b["endpoint"], b["api_key"])) if b.get("api_key") else []
        out.append({"name": rules["name"], "endpoint": b["endpoint"], "has_key": bool(b.get("api_key")),
                    "can_edit": None if r is None else ("EDIT" in r or "ADMIN" in r)})
    return out


# --- "Ready to submit": scenes that have most of what a site needs ------------------
HERE = os.path.dirname(os.path.abspath(__file__))
READY = os.path.join(HERE, "ready.json")
SENT = os.path.join(HERE, "sent.json")
PAGE_VERSION = 2  # (matches the page's; bump both when what the page expects changes)
ONSITE = os.path.join(HERE, "on_site.json")  # (which scenes each site has, by fingerprint)
RECHECK_DAYS = 14
FAN = re.compile(r"onlyfans\.com|fansly\.com|patreon\.com|justfor\.fans|manyvids\.com|clips4sale\.com|loyalfans\.com|fancentro\.com|"
                 r"onlyfans|fansly|manyvids|clips4sale|pornhub\.com/model", re.I)
FAN_ONLY = re.compile(r"onlyfans|fansly|patreon|justfor\.fans|justforfans", re.I)  # (StashDB doesn't take these)


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path, data):
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def progress(v):
    """Progress in Stash's task list (0 to 1) when run as a task."""
    sys.stderr.write(f"\x01p\x02{max(0.0, min(1.0, v)):.3f}\n")
    sys.stderr.flush()


SCENE_PAGE = 500  # (a big library a page at a time - asking for all at once can run Stash out of memory)


def all_scenes(stash, fields, on_page=None):
    out, page = [], 1
    while True:
        r = stash.call(f"query($p: Int!) {{ findScenes(filter: {{per_page: {SCENE_PAGE}, page: $p, sort: \"id\"}}) {{ count scenes {{ {fields} }} }} }}",
                       {"p": page})["findScenes"]
        out += r["scenes"]
        if on_page:
            on_page(len(out), r["count"])
        if not r["scenes"] or len(out) >= r["count"]:
            return out
        page += 1


def candidates(stash, limit=300):
    """Every scene scored for each site you have set up (not ones already on that site):
    what it has of what a submission needs, best first."""
    sites_here = [(b["endpoint"], site_of(b["endpoint"])) for b in boxes(stash) if b.get("api_key")]
    sites_here = [(e, r, h) for e, (r, h) in sites_here if h in SITES]
    progress(0)
    scenes = all_scenes(stash, """id title date urls paths { screenshot }
      files { fingerprints { type value } }
      studio { id name urls stash_ids { endpoint stash_id } parent_studio { name urls } }
      performers { id name stash_ids { endpoint } } stash_ids { endpoint }""", lambda n, total: progress(0.3 * n / max(total, 1)))
    sent = _load(SENT, {})
    out = []
    for s in scenes:
        fp = any(f["type"] == "phash" for x in s.get("files") or [] for f in x.get("fingerprints") or [])
        st = s.get("studio") or {}
        where = " ".join((s.get("urls") or []) + (st.get("urls") or []) + [((st.get("parent_studio") or {}).get("name") or ""), st.get("name") or ""]
                         + ((st.get("parent_studio") or {}).get("urls") or []))
        on = {x["endpoint"] for x in s.get("stash_ids") or []}
        per_site = {}
        for endpoint, rules, host in sites_here:
            if endpoint in on:
                continue  # (already there)
            fan = bool(FAN.search(where))
            if host == "fansdb.cc" and not fan:
                continue  # (FansDB is for creator / fan-site content)
            if host == "stashdb.org" and FAN_ONLY.search(where):
                continue  # (StashDB doesn't take it)
            have = {"phash": fp, "title": bool((s.get("title") or "").strip()), "date": bool(s.get("date")),
                    "studio": bool(st), "performers": bool(s.get("performers")), "link": bool(s.get("urls"))}
            need = [k for k in rules["required"] + ["link"] if not have.get(k, True)]
            st_linked = bool(st) and any(x["endpoint"] == endpoint for x in st.get("stash_ids") or [])
            pf = s.get("performers") or []
            pf_linked = sum(1 for p in pf if any(x["endpoint"] == endpoint for x in p.get("stash_ids") or []))
            score = sum(have.values()) + (1.5 if st_linked else 0) + (1.5 * pf_linked / len(pf) if pf else 0)
            per_site[endpoint] = {"name": rules["name"], "missing": need, "score": round(score, 2),
                                  "studio_linked": st_linked, "performers_linked": f"{pf_linked}/{len(pf)}" if pf else "0/0",
                                  "sent": (sent.get(s["id"]) or {}).get(endpoint)}
        if per_site and max(v["score"] for v in per_site.values()) >= 4:
            on_any = sorted({site_of(x["endpoint"])[0]["name"] or x["endpoint"] for x in s.get("stash_ids") or []})
            out.append({"on_any": on_any, "id": s["id"], "title": s.get("title") or "", "date": s.get("date"), "cover": (s.get("paths") or {}).get("screenshot"),
                        "studio": st.get("name"), "performers": [p["name"] for p in s.get("performers") or []][:4], "sites": per_site,
                        "best": max(v["score"] - 3 * len(v["missing"]) for v in per_site.values())})
    out.sort(key=lambda x: -x["best"])
    total = len(out)
    # the best overall, plus the best of the ones no database has (else those are crowded out)
    keep = {x["id"] for x in out[:limit]} | {x["id"] for x in [y for y in out if not y["on_any"]][:limit]}
    out = [x for x in out if x["id"] in keep]
    # already on the site (the same video, by fingerprint) - many sites check many at once
    fps_of = {s["id"]: [{"hash": f["value"], "algorithm": {"phash": "PHASH", "oshash": "OSHASH", "md5": "MD5"}[f["type"]]}
                        for x in s.get("files") or [] for f in x.get("fingerprints") or [] if f["type"] in ("phash", "oshash", "md5") and f.get("value")]
              for s in scenes}
    # (answers are kept in on_site.json: a scene the site has stays known; one it didn't have is
    # asked about again after RECHECK_DAYS - so each Check only asks about new or old answers)
    known = _load(ONSITE, {})
    by_out = {x["id"]: x for x in out}
    for endpoint, rules, host in sites_here:
        mine = known.setdefault(endpoint, {})
        ids = []
        for x in out:
            if endpoint not in x["sites"] or not fps_of.get(x["id"]):
                continue
            rec = mine.get(x["id"])
            if rec and (rec["found"] or time.time() - rec["at"] < RECHECK_DAYS * 86400):
                if rec["found"]:
                    x["sites"][endpoint]["on_site"] = rec["found"]
            else:
                ids.append(x["id"])
        try:
            box = box_for(stash, endpoint)
            for i in range(0, len(ids), 40):
                chunk = ids[i:i + 40]
                r = box.call("query($f: [[FingerprintQueryInput!]!]!) { findScenesBySceneFingerprints(fingerprints: $f) { id title } }",
                             {"f": [fps_of[x] for x in chunk]})
                for sid_, found in zip(chunk, r.get("findScenesBySceneFingerprints") or []):
                    hits = [{"id": f["id"], "title": f.get("title"), "url": f"{site_base(endpoint)}/scenes/{f['id']}"} for f in (found or [])[:3]]
                    mine[sid_] = {"at": time.time(), "found": hits}
                    if hits:
                        by_out[sid_]["sites"][endpoint]["on_site"] = hits
                _save(ONSITE, known)  # (as it goes: a stopped Check keeps what it learned)
        except Exception as e:
            sys.stderr.write(f"\x01w\x02Couldn't check {rules['name']} for scenes it has already ({e}).\n")
    _save(ONSITE, known)
    progress(0.6)
    # is the studio on the site already? (FansDB: the account it was posted from) - scenes whose
    # studio is there come first, as a studio that isn't has to be added (and approved) first
    by_id = {s["id"]: s for s in scenes}
    for endpoint, rules, host in sites_here:
        todo = {}
        for x in out:
            v = x["sites"].get(endpoint)
            if not v or v.get("on_site"):
                continue
            if v["studio_linked"]:
                v["studio_site"] = "linked"
                continue
            sc = by_id[x["id"]]
            st = sc.get("studio")
            acct = scene_account(sc.get("urls"))
            links = [u.lower().rstrip("/") for u in ((st or {}).get("urls") or []) if u]
            if acct and host == "fansdb.cc" and acct["url"].lower() not in links:
                st = account_studio(acct)
            if not st:
                v["studio_site"] = "none"
                continue
            key = "studio:" + (st.get("id") or st.get("url") or "") + ":" + st["name"].lower()
            todo.setdefault(key, (st, []))[1].append(v)
        if not todo:
            continue
        box = box_for(stash, endpoint)
        pending = my_pending_studios(box)

        def look(item, box=box, endpoint=endpoint):
            key, (st, _) = item
            try:  # (kept a day; "not there yet" only an hour, so one you've just added shows up)
                return cached(box, key, lambda: match_studio(box, endpoint, st),
                              max_age=keep_for)
            except Later:
                return {"status": "later"}
            except Exception:
                return {"status": "unknown"}
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            for (key, (st, vs)), m in zip(todo.items(), pool.map(look, todo.items())):
                wait = pending_for(pending, st) if m.get("status") == "missing" else None
                for v in vs:
                    v["studio_site"] = "pending" if wait else m.get("status")
                    if wait:
                        v["studio_edit"] = wait
                    if m.get("box"):
                        v["studio_site_name"] = m["box"].get("name")
    progress(0.9)
    for x in out:
        for v in x["sites"].values():
            v["score"] = round(v["score"] + {"found": 3, "linked": 1.5, "maybe": 1}.get(v.get("studio_site"), 0), 2)
    for x in out:  # (a scene the site already has isn't one to submit there)
        new = [v for v in x["sites"].values() if not v.get("on_site")]
        x["best"] = max((v["score"] - 3 * len(v["missing"]) for v in new), default=-99)
    out.sort(key=lambda x: -x["best"])
    rights = {}
    for e, r, h in sites_here:
        try:
            ro = roles(box_for(stash, e))
            rights[e] = None if ro is None else ("EDIT" in ro or "ADMIN" in ro)
        except Exception:
            rights[e] = None
    result = {"at": time.time(), "count": total, "shown": len(out), "scenes": out,
              "sites": [{"endpoint": e, "name": r["name"], "can_edit": rights.get(e)} for e, r, h in sites_here]}
    _save(READY, result)
    return result


def sent_state(box, rec, title):
    """Where a draft sent from here is now: "draft" (still waiting to be finished), "submitted"
    (an edit waiting for votes - the dict), or "gone" (submitted and closed, or deleted)."""
    drafts = supported(box, "findDrafts", lambda: box.call("{ findDrafts { id } }"))
    if drafts is None:
        return None  # (the site can't say)
    draft = any(rec["url"].endswith("/" + d["id"]) for d in drafts.get("findDrafts") or [])
    edit = next((e for e in my_pending_edits(box) if e["kind"] == "scene" and title and e["name"].strip().lower() == title.strip().lower()), None)
    if edit:
        return {"edit_url": edit["edit_url"], "yes": edit["yes"], "no": edit["no"], "expires": edit["expires"], "extra_draft": draft and rec["url"]}
    return "draft" if draft else "gone"


def with_sent(ready):
    """The saved list, with what's been sent since it was made (sent.json)."""
    sent = _load(SENT, {})
    for x in ready.get("scenes") or []:
        for e, v in x["sites"].items():
            rec = (sent.get(str(x["id"])) or {}).get(e)
            if rec:
                v["sent"] = rec
    return ready


def mark_sent(scene_id, endpoint, draft_url):
    sent = _load(SENT, {})
    sent.setdefault(str(scene_id), {})[endpoint] = {"url": draft_url, "at": time.time()}
    _save(SENT, sent)




def review(stash):
    """Where to do the final review on each site: your drafts there (from the site when it can list
    them, else the ones sent from here in the last 24 hours) and the pages for drafts and edits."""
    sent = _load(SENT, {})
    by_url = {v.get("url"): (sid_, e) for sid_, per in sent.items() for e, v in per.items() if v and v.get("url")}
    now = time.time()
    out = []
    for b in boxes(stash):
        if not b.get("api_key"):
            continue
        rules, host = site_of(b["endpoint"])
        if host not in SITES:
            continue
        base = site_base(b["endpoint"])
        stashbox = host != "theporndb.net"  # (ThePornDB's site isn't a stash-box one: no drafts/edits pages there)
        item = {"endpoint": b["endpoint"], "name": rules["name"], "drafts": [], "listed": False,
                "drafts_page": f"{base}/drafts" if stashbox else None, "edits_page": None}
        box = box_for(stash, b["endpoint"])
        try:
            r = supported(box, "findDrafts", lambda: box.call("""{ me { name } findDrafts { id expires data { __typename
                ... on SceneDraft { title urls studio { __typename ... on DraftEntity { name } } performers { __typename ... on DraftEntity { name } } }
                ... on PerformerDraft { name urls } } } }"""))
            if r is None:
                raise RuntimeError("no drafts list")  # (the ones sent from here, below)
            me = (r.get("me") or {}).get("name")
            if me and stashbox:
                item["edits_page"] = f"{base}/users/{urllib.parse.quote(me)}/edits"
            for d in r.get("findDrafts") or []:
                url = f"{base}/drafts/{d['id']}"
                data = d.get("data") or {}
                loose = [x["name"] for x in ([data.get("studio")] if data.get("studio") else []) + (data.get("performers") or [])
                         if (x or {}).get("__typename") == "DraftEntity"]
                item["drafts"].append({"url": url, "id": d["id"], "kind": "performer" if data.get("__typename") == "PerformerDraft" else "scene",
                                       "title": data.get("title") or data.get("name") or "", "expires": d.get("expires"),
                                       "scene_id": (by_url.get(url) or (None,))[0], "urls": data.get("urls") or [],
                                       "unpicked": loose, "can_submit": host in EDIT_HOSTS and not loose})
            item["listed"] = True
        except Exception:
            # (the site can't list them: the ones sent from here that haven't expired yet)
            for sid_, per in sent.items():
                v = per.get(b["endpoint"]) or {}
                if v.get("url") and now - (v.get("at") or 0) < DAY:
                    item["drafts"].append({"url": v["url"], "kind": "scene", "title": "", "scene_id": sid_,
                                           "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v["at"] + DAY))})
        for d in item["drafts"]:
            if d["scene_id"] and not d["title"]:
                try:
                    d["title"] = (stash.call("query($id: ID!) { findScene(id: $id) { title } }", {"id": d["scene_id"]})["findScene"] or {}).get("title") or ""
                except Exception:
                    pass
        item["edits"] = my_pending_edits(box) if item["listed"] else []
        # each one's picture and page in your Stash, so the Pending cards look like the scenes
        for d in item["drafts"]:
            d["local"] = local_item(stash, d["kind"], d["title"], [], d.get("scene_id"))
        for e in item["edits"]:
            e["local"] = local_item(stash, e["kind"], e["name"], e.get("urls") or [], None)
        out.append(item)
    return {"sites": out}


def local_item(stash, kind, name, urls, scene_id):
    """{"id", "image", "path"} of the scene / performer / studio in your Stash a draft or edit is for."""
    try:
        if kind == "scene":
            q = """query($f: SceneFilterType) { findScenes(scene_filter: $f, filter: {per_page: 1}) { scenes { id paths { screenshot } } } }"""
            if scene_id:
                r = stash.call("query($id: ID!) { findScene(id: $id) { id paths { screenshot } } }", {"id": scene_id})["findScene"]
            else:
                r = (stash.call(q, {"f": {"title": {"value": name, "modifier": "EQUALS"}}})["findScenes"]["scenes"] or [None])[0] if name else None
            return r and {"id": r["id"], "image": (r.get("paths") or {}).get("screenshot"), "path": f"/scenes/{r['id']}"}
        if kind == "performer":
            r = (stash.call("""query($n: String!) { findPerformers(performer_filter: {name: {value: $n, modifier: EQUALS}}, filter: {per_page: 1})
                { performers { id image_path } } }""", {"n": name})["findPerformers"]["performers"] or [None])[0] if name else None
            return r and {"id": r["id"], "image": r.get("image_path"), "path": f"/performers/{r['id']}"}
        if kind == "studio":
            r = None
            for u in urls:  # (by its account link - the site names it "username (Network)", your Stash may not)
                hits = stash.call("query($u: String!) { findStudios(studio_filter: {url: {value: $u, modifier: INCLUDES}}) { studios { id image_path } } }",
                                  {"u": u.split("://", 1)[-1].rstrip("/")})["findStudios"]["studios"]
                if len(hits) == 1:
                    r = hits[0]
                    break
            if not r and name:
                for n in (name, re.sub(r"\s*\([^()]*\)\s*$", "", name)):
                    hits = stash.call("query($n: String!) { findStudios(studio_filter: {name: {value: $n, modifier: EQUALS}}) { studios { id image_path } } }",
                                      {"n": n})["findStudios"]["studios"]
                    if hits:
                        r = hits[0]
                        break
            if r:
                img = r.get("image_path") or ""
                return {"id": r["id"], "image": None if "default=true" in img else img, "path": f"/studios/{r['id']}"}
    except Exception:
        return None
    return None


def main():
    data = json.loads(sys.stdin.read() or "{}")
    stash = Stash(data.get("server_connection") or {})
    args = data.get("args") or {}
    mode = args.get("mode")
    try:
        if args.get("fresh_since"):
            FRESH.update({b["endpoint"]: float(args["fresh_since"]) for b in boxes(stash)
                          if not args.get("fresh_site") or b["endpoint"] == args["fresh_site"]})
        if args.get("batch"):
            BATCH["n"] = int(args["batch"])
        if mode not in (None, "task_candidates") and str(args.get("page_version") or "") != str(PAGE_VERSION):
            raise RuntimeError("The plugin was updated - reload the page (this one is out of date).")
        if mode == "sites":
            out = sites(stash)
        elif mode == "check":
            out = check(stash, str(args["scene_id"]), args["endpoint"])
        elif mode == "submit":
            out = submit(stash, str(args["scene_id"]), args["endpoint"], args.get("choices") or {})
        elif mode == "candidates":
            out = candidates(stash)
        elif mode == "ready":
            out = with_sent(_load(READY, None) or candidates(stash))
        elif mode == "studio_suggestions":
            out = studios_all(stash)
        elif mode == "performer_suggestions":
            out = performer_suggestions(stash)
        elif mode == "scene_edit":
            out = scene_edit(stash, str(args["scene_id"]), args["endpoint"], args.get("choices") or {}, args.get("comment") or "")
        elif mode == "performer_edit":
            out = performer_edit(stash, str(args["performer_id"]), args["endpoint"], args.get("comment") or "")
        elif mode == "draft_edit":
            out = draft_edit(stash, args["endpoint"], args["draft_id"], args.get("comment") or "")
        elif mode == "not_them":
            out = not_them(args["endpoint"], str(args["performer_id"]), args.get("candidate_ids") or [])
        elif mode == "link_performer":
            out = link_performer(stash, args["endpoint"], str(args["performer_id"]), args["remote_id"])
        elif mode == "local_studio":
            out = make_local_studio(stash, args["endpoint"], args["site_studio_id"], args.get("scene_ids") or [])
        elif mode == "review":
            out = review(stash)
        elif mode == "mark_sent":
            mark_sent(args["scene_id"], args["endpoint"], args.get("url"))
            out = {"ok": True}
        elif mode == "task_candidates":
            r = candidates(stash)
            progress(1)
            sys.stderr.write(f"\x01i\x02Found {r['count']} scene(s) with most of what a submission needs - open "
                             "the Ready to submit page (Scenes page button, or /plugins/stashbox-submit).\n")
            out = {"count": r["count"]}
        elif mode == "studio":
            out = studio_edit(stash, args["endpoint"], str(args.get("studio_id") or ""), args.get("account_url"))
        elif mode == "add_link":
            out = add_link(stash, str(args["performer_id"]), args["url"])
        elif mode == "performer":
            out = performer_draft(stash, str(args["performer_id"]), args["endpoint"])
        else:
            raise RuntimeError(f"unknown operation {mode!r}")
        print(json.dumps({"output": out}))
    except Exception as e:
        print(json.dumps({"output": {"error": str(e)}}))


if __name__ == "__main__":
    main()
