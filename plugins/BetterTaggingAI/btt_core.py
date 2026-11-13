"""Better Tagging Tools / Better Tagging AI - shared code.

Both plugins ship this same file, so each works on its own. Better Tagging
Tools (no AI): performers', studios' and galleries' top tags, suggested tags,
bulk tagging, gallery-name tags, tag pictures, tag name capitalisation, auto
tag, dates and titles from file names, studios from account links. Better
Tagging AI: everything that asks the local AI model (Ollama) - AI tags, AI
titles, reading watermarks for studios and performers, and doing it all for
new items after a scan.

When both are installed they share settings (the AI plugin reads "Tags to
ignore", special spellings and the like from Better Tagging Tools) and
records (gallery-name tags, titles made from file names).
"""
import base64
import contextlib
import fcntl
import hashlib
import heapq
import http.client
import json
import math
import operator
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE_ID = "BetterTaggingTools"   # the plugin without AI
AI_ID = "BetterTaggingAI"        # the AI plugin
PLUGIN_ID = BASE_ID              # the one running now (set by its script)


def plugin_name():
    return "Better Tagging AI Add-on" if PLUGIN_ID == AI_ID else "Better Tagging Tools"


def base_dir(plugin_dir):
    """Where Better Tagging Tools keeps its records (gallery-name tags,
    titles made from file names): its own folder, or next to this plugin's."""
    if PLUGIN_ID == BASE_ID or not plugin_dir:
        return plugin_dir
    sibling = os.path.join(os.path.dirname(os.path.abspath(plugin_dir)), BASE_ID)
    return sibling if os.path.isdir(sibling) else plugin_dir
PAGE_SIZE = 1000


def log(level, msg):
    codes = {"trace": "t", "debug": "d", "info": "i", "warning": "w", "error": "e"}
    sys.stderr.write(f"\x01{codes[level]}\x02{msg}\n")
    sys.stderr.flush()


def progress(p):
    sys.stderr.write(f"\x01p\x02{min(max(p, 0.0), 1.0)}\n")
    sys.stderr.flush()


class Stash:
    def __init__(self, conn):
        self.conn = conn
        scheme = conn.get("Scheme", "http")
        host = conn.get("Host", "localhost")
        if host in ("0.0.0.0", ""):
            host = "localhost"
        port = conn.get("Port", 9999)
        self.config_dir = conn.get("Dir") or ""
        self.url = f"{scheme}://{host}:{port}/graphql"
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        cookie = conn.get("SessionCookie") or {}
        if cookie.get("Name"):
            self.headers["Cookie"] = f"{cookie['Name']}={cookie['Value']}"
        # The session a task is given runs out after about an hour, and from
        # then on Stash answers with its login page (long tasks then skipped
        # everything). Stash's own API key, from its settings file next to
        # the plugins, doesn't run out: used when there is one.
        key = self._api_key()
        if key:
            self.headers["ApiKey"] = key

    def _api_key(self):
        for d in (self.config_dir, "/root/.stash", os.path.expanduser("~/.stash")):
            if not d:
                continue
            with contextlib.suppress(OSError):
                with open(os.path.join(d, "config.yml"), encoding="utf-8") as f:
                    for line in f:
                        m = re.match(r"^api_key:\s*['\"]?([^'\"\s]+)", line)
                        if m:
                            return m.group(1)
        return None

    def call(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.url, data=body, headers=self.headers)
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        if result.get("errors"):
            raise RuntimeError(result["errors"])
        return result["data"]

    def fetch(self, url):
        """Download a file from Stash (thumbnails etc.): (bytes, content type)."""
        headers = {k: v for k, v in self.headers.items() if k in ("Cookie", "ApiKey")}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
            return resp.read(), resp.headers.get_content_type()


def find_by_ids(stash, query, field, ids, fields):
    """findScenes / findImages (ids: ...) that doesn't fail when some of the
    ids are gone (deleted since a scan remembered them): Stash refuses the
    whole lookup then, so the missing ones are left out and it's asked again."""
    ids = [str(i) for i in ids]
    for _ in range(len(ids) + 1):
        if not ids:
            return []
        try:
            return stash.call(f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ {fields} }} }} }}",
                              {"ids": ids})[query][field]
        except RuntimeError as e:
            gone = set(re.findall(r"with id (\d+) not found", str(e)))
            if not gone or not gone & set(ids):
                raise
            ids = [i for i in ids if i not in gone]
    return []


SETTINGS_CACHE_SECONDS = 30


def plugin_settings(stash, plugin_dir=None, plugin_id=BASE_ID):
    """A plugin's raw settings (Better Tagging Tools' unless said otherwise).
    With plugin_dir, a copy up to 30 seconds old is reused: scans fire a hook
    per new image, so this saves a call."""
    path = os.path.join(plugin_dir, "settings_cache.json") if plugin_dir else None
    if path:
        try:
            with open(path) as f:
                cached = json.load(f)
            if time.time() - cached["time"] < SETTINGS_CACHE_SECONDS and "all" in cached:
                return cached["all"].get(plugin_id) or {}
        except (OSError, ValueError, KeyError):
            pass
    try:
        cfg = stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {}
        s = cfg.get(plugin_id) or {}
    except Exception:
        return {}
    cfg = {k: v for k, v in cfg.items() if k in (BASE_ID, AI_ID)}
    if path:
        try:
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w") as f:
                json.dump({"time": time.time(), "all": cfg}, f)
            os.replace(tmp, path)
        except OSError:
            pass
    return s


def settings(stash, plugin_dir=None):
    s = plugin_settings(stash, plugin_dir)
    # AI settings belong to Better Tagging AI; ones still saved under Better
    # Tagging Tools (from before the two were split) count until changed there
    ai_raw = {k: v for k, v in s.items() if k.startswith("ai")}
    ai_raw.update(plugin_settings(stash, plugin_dir, AI_ID))

    def num(key, default, lo):
        try:
            return max(int(s.get(key) or default), lo)
        except (TypeError, ValueError):
            return default

    exclude = [n.strip().lower() for n in (s.get("excludeTags") or "").split(",") if n.strip()]
    skip_images = bool(s.get("skipImages"))
    return {
        "top": num("topTagCount", 10, 1),
        "min": num("minUses", 3, 1),
        "kinds": ["scenes", "galleries"] if skip_images else ["scenes", "images", "galleries"],
        "exclude": exclude,
        "no_hook": bool(s.get("noAutoRefresh")),
        "hook_owners": [o for o, key in (("studios", "studioTopTags"), ("galleries", "galleryTopTags")) if s.get(key)],
        "gallery_tags": bool(s.get("galleryNameTags")),
        "gallery_parent": (s.get("galleryTagParent") or "").strip(),
        "auto_pictures": bool(s.get("autoTagPictures")),
        "title_case_new": bool(s.get("titleCaseNewTags")),
        "spellings": {i.strip().lower(): i.strip() for i in (s.get("titleCaseSpellings") or "").split(",") if i.strip()},
        "ai": ai_settings(ai_raw),
        "autotag_new": bool(s.get("autoTagNew")),
        "dates_new": bool(s.get("datesNewItems")),
        "titles_new": bool(s.get("titlesNewItems")),
        "titles_keep_names": bool(s.get("titlesKeepNames")),
        "titles_no_gallery": bool(s.get("titlesNoGallery")),
    }


def tag_lookup(stash, cfg, plugin_dir):
    """Tag names by id, and the ids of the tags to ignore."""
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
    names = {t["id"]: t["name"] for t in tags}
    excluded = {t["id"] for t in tags if t["name"].lower() in cfg["exclude"]}
    # Gallery-name tags this plugin made would otherwise become everyone's
    # "top tags" (a 200-photo gallery = 200 uses), so they never count
    excluded |= gallery_only_tag_ids(stash, plugin_dir)
    missing = set(cfg["exclude"]) - {names[i].lower() for i in excluded}
    if missing:
        log("warning", f"Tags to ignore not found: {', '.join(sorted(missing))}")
    return names, excluded


# --- counting -------------------------------------------------------------

KINDS = {
    # kind: (query, result field, filter argument, filter type)
    "scenes": ("findScenes", "scenes", "scene_filter", "SceneFilterType"),
    "images": ("findImages", "images", "image_filter", "ImageFilterType"),
    "galleries": ("findGalleries", "galleries", "gallery_filter", "GalleryFilterType"),
}


# Who gets top tags: what they're called, how to find and update them, how
# the items that count point at them, and where they're kept in
# auto_tags.json (performers by plain id, the others with a prefix).
OWNERS = {
    "performers": {
        "one": "performer", "find": "findPerformers", "update": "performerUpdate",
        "input": "PerformerUpdateInput", "fields": "id name gender tags { id }",
        "filter": "performers", "item": "performers { id }",
        "ids": lambda it: [p["id"] for p in it.get("performers") or []], "prefix": "",
    },
    "studios": {
        "one": "studio", "find": "findStudios", "update": "studioUpdate",
        "input": "StudioUpdateInput", "fields": "id name tags { id }",
        "filter": "studios", "item": "studio { id }",
        "ids": lambda it: [it["studio"]["id"]] if it.get("studio") else [], "prefix": "studio:",
    },
    "galleries": {
        "one": "gallery", "find": "findGalleries", "update": "galleryUpdate",
        "input": "GalleryUpdateInput", "fields": "id title files { basename } folder { path } tags { id }",
        "filter": "galleries", "item": "galleries { id }",
        "ids": lambda it: [g["id"] for g in it.get("galleries") or []], "prefix": "gallery:",
    },
}


def owner_sources(owner, cfg):
    """What gets counted: a gallery's images; everyone else's scenes, images
    (unless "Don't count images") and galleries."""
    return ["images"] if owner == "galleries" else cfg["kinds"]


def owner_name(owner, obj):
    return gallery_name(obj) or f"Gallery {obj['id']}" if owner == "galleries" else obj["name"]


def count_all(stash, owner, kinds, step, step_total):
    """Tag uses for every performer / studio / gallery:
    {id: Counter({tag_id: uses})}."""
    o = OWNERS[owner]
    counts = defaultdict(Counter)
    for i, kind in enumerate(kinds):
        query_name, field, _, _ = KINDS[kind]
        q = f"""query($f: FindFilterType) {{
            {query_name}(filter: $f) {{ count {field} {{ {o["item"]} tags {{ id }} }} }}
        }}"""
        page = 1
        seen = 0
        while True:
            data = stash.call(q, {"f": {"page": page, "per_page": PAGE_SIZE}})[query_name]
            items = data[field]
            for item in items:
                tags = item.get("tags") or []
                for oid in o["ids"](item):
                    c = counts[oid]
                    for t in tags:
                        c[t["id"]] += 1
            seen += len(items)
            progress((step + i + seen / (data["count"] or 1)) / step_total)
            if len(items) < PAGE_SIZE:
                break
            page += 1
        log("info", f"Counted tags on {seen} {kind} for {owner}")
    return counts


def count_one(stash, owner, kinds, oid):
    """One performer's / studio's / gallery's tag uses, from just its own
    items."""
    c = Counter()
    for kind in kinds:
        query_name, field, farg, ftype = KINDS[kind]
        q = f"""query($p: {ftype}, $f: FindFilterType) {{
            {query_name}({farg}: $p, filter: $f) {{ {field} {{ tags {{ id }} }} }}
        }}"""
        page = 1
        while True:
            items = stash.call(q, {
                "p": {OWNERS[owner]["filter"]: {"value": [oid], "modifier": "INCLUDES"}},
                "f": {"page": page, "per_page": PAGE_SIZE},
            })[query_name][field]
            for item in items:
                for t in item.get("tags") or []:
                    c[t["id"]] += 1
            if len(items) < PAGE_SIZE:
                break
            page += 1
    return c


# --- applying -------------------------------------------------------------


@contextlib.contextmanager
def locked_json(path):
    """Load a JSON dict under a lock, and save it back when done."""
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        yield data
        tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)


def locked_record(plugin_dir):
    """Which tags the plugin added to which performer (auto_tags.json)."""
    return locked_json(os.path.join(plugin_dir, "auto_tags.json"))


def locked_queue(plugin_dir):
    """Performers waiting for a refresh after edits (queue.json)."""
    return locked_json(os.path.join(plugin_dir, "queue.json"))


# Tags that describe one gender's body (or person): a woman isn't given "Big
# Dick" because her scenes have it, nor a man "Big Tits". Only for performers
# marked Female or Male (trans, non-binary and unknown are left alone).
_SIZE = r"(?:(?:big|small|tiny|medium|huge|large|average|thick|long|short|massive|monster|fat|curved|perky|saggy|natural|fake|real|small-?ish|hairy|shaved|smooth|uncut|cut|black|white|pink)\s+)*"
MALE_BODY_TAG = re.compile(
    rf"^{_SIZE}(?:dick|dicks|cock|cocks|penis|penises|balls)$|^(?:un)?circumcised$|^(?:bbc|bwc)$"
    r"|\b(?:man|men|male|males|guy|guys|boy|boys|dude)$|\((?:male|man)\)$", re.I)
FEMALE_BODY_TAG = re.compile(
    rf"^{_SIZE}(?:tits|titties|boobs|breasts|breast)$|^(?:\w+\s+)+pussy$|^pussy$|^milfs?$"
    r"|\b(?:woman|women|female|females|girl|girls)$|\((?:female|woman)\)$", re.I)


MALE_WORD = re.compile(r"\b(?:man|men|male|males|guy|guys|boy|boys|dude|dick|cock|penis)\b", re.I)
FEMALE_WORD = re.compile(r"\b(?:woman|women|female|females|girl|girls|tits|boobs|breasts|pussy)\b", re.I)


def wrong_gender_tag(name, gender):
    """True if the tag describes the other gender's body (for a performer
    who's FEMALE or MALE). Tags naming both ("Older Man / Younger Woman")
    are fine for either."""
    name = (name or "").strip()
    if gender == "FEMALE":
        return bool(MALE_BODY_TAG.search(name)) and not FEMALE_WORD.search(name)
    if gender == "MALE":
        return bool(FEMALE_BODY_TAG.search(name)) and not MALE_WORD.search(name)
    return False


def refresh_owner(stash, cfg, owner, obj, counts, record, names, excluded, dry_run):
    """Bring one performer's / studio's / gallery's auto tags in line with
    its top tags.

    Updates `record` in place and returns True if it changed."""
    o = OWNERS[owner]
    key = o["prefix"] + obj["id"]
    current = [t["id"] for t in obj["tags"]]
    current_set = set(current)
    prev_auto = set(record.get(key, [])) & current_set  # still on it

    ranked = [tid for tid, uses in counts.most_common() if uses >= cfg["min"] and tid not in excluded]
    if owner == "performers" and obj.get("gender"):
        ranked = [tid for tid in ranked if not wrong_gender_tag(names.get(tid), obj["gender"])]
    top = ranked[: cfg["top"]]
    top_set = set(top)

    to_add = [t for t in top if t not in current_set]
    to_remove = prev_auto - top_set
    auto_now = (prev_auto & top_set) | set(to_add)
    if not dry_run:
        if auto_now:
            record[key] = sorted(auto_now, key=int)
        else:
            record.pop(key, None)

    if not (to_add or to_remove):
        return False

    parts = []
    if to_add:
        parts.append("+ " + ", ".join(names.get(t, t) for t in to_add))
    if to_remove:
        parts.append("- " + ", ".join(names.get(t, t) for t in to_remove))
    prefix = "" if owner == "performers" else o["one"].capitalize() + " "
    log("info", f"{prefix}{owner_name(owner, obj)}: {'  '.join(parts)}")
    if not dry_run:
        stash.call(
            f"mutation($i: {o['input']}!) {{ {o['update']}(input: $i) {{ id }} }}",
            {"i": {"id": obj["id"], "tag_ids": [t for t in current if t not in to_remove] + to_add}},
        )
    return True


def find_owners(stash, owner, ids=None):
    o = OWNERS[owner]
    if ids is None:
        q = f"{{ {o['find']}(filter: {{per_page: -1}}) {{ {owner} {{ {o['fields']} }} }} }}"
        return stash.call(q)[o["find"]][owner]
    q = f"query($ids: [ID!]) {{ {o['find']}(ids: $ids, filter: {{per_page: -1}}) {{ {owner} {{ {o['fields']} }} }} }}"
    return stash.call(q, {"ids": ids})[o["find"]][owner]


def run_all(stash, plugin_dir, dry_run, owners=("performers",)):
    cfg = settings(stash)
    names, excluded = tag_lookup(stash, cfg, plugin_dir)
    steps = sum(len(owner_sources(o, cfg)) + 1 for o in owners)
    step = 0
    summary = []
    for owner in owners:
        o = OWNERS[owner]
        kinds = owner_sources(owner, cfg)
        counts = count_all(stash, owner, kinds, step, steps)
        step += len(kinds)
        objs = find_owners(stash, owner)
        changed = 0
        with locked_record(plugin_dir) as record:
            if not dry_run:
                # ones that no longer exist drop out of the record
                live = {o["prefix"] + x["id"] for x in objs}
                for key in list(record):
                    is_mine = key.startswith(o["prefix"]) if o["prefix"] else ":" not in key
                    if is_mine and key not in live:
                        del record[key]
            for n, obj in enumerate(objs):
                if refresh_owner(stash, cfg, owner, obj, counts.get(obj["id"], Counter()),
                                 record, names, excluded, dry_run):
                    changed += 1
                progress((step + (n + 1) / max(len(objs), 1)) / steps)
        step += 1
        summary.append(f"{changed} of {len(objs)} {owner}")

    if dry_run:
        log("info", f"Preview done: {', '.join(summary)} would change. Nothing was saved.")
    else:
        log("info", f"Done: updated top tags on {', '.join(summary)}.")


HOOK_ITEMS = {
    "Scene": ("findScene", "scene"),
    "Image": ("findImage", "image"),
    "Gallery": ("findGallery", "gallery"),
}


def run_hook(stash, plugin_dir, hook):
    fields = hook.get("inputFields") or []
    kind, _, action = (hook.get("type") or "").partition(".")
    action = action.split(".")[0]  # "Create" / "Update"

    if PLUGIN_ID == AI_ID:
        # Better Tagging AI: new scenes and images for the AI
        if action == "Create" and kind in ("Scene", "Image"):
            cfg = settings(stash, plugin_dir)
            if cfg["ai"]["auto"] or cfg["ai"]["new_all"] or cfg["ai"]["face_auto"] or \
                    (kind == "Image" and cfg["ai"]["photo_studios"]):
                key = "ai_scenes" if kind == "Scene" else "ai_images"
                enqueue(stash, plugin_dir, **{key: [str(hook["id"])]})
        return

    if kind in ("Tag", "Performer", "Studio"):
        if action == "Create":
            if kind == "Tag":
                title_case_new_tag(stash, plugin_dir, hook["id"])
            if settings(stash, plugin_dir)["autotag_new"]:
                key = {"Tag": "autotag_tags", "Performer": "autotag_performers", "Studio": "autotag_studios"}[kind]
                enqueue(stash, plugin_dir, **{key: [str(hook["id"])]})
        return

    # Edits that touch tags or performers can change someone's top tags
    # (this also skips play counts, ratings, cover changes and so on)
    performer_edit = (
        action == "Update" and kind in HOOK_ITEMS and ("tag_ids" in fields or "performer_ids" in fields)
    )
    # ... and tags or the studio a studio's; an image's tags or galleries a
    # gallery's
    studio_edit = action == "Update" and kind in HOOK_ITEMS and ("tag_ids" in fields or "studio_id" in fields)
    gallery_edit = action == "Update" and kind == "Image" and ("tag_ids" in fields or "gallery_ids" in fields)
    # A new gallery, a renamed one, or one a scan touched (no fields)
    gallery_event = kind == "Gallery" and (action == "Create" or not fields or "title" in fields)
    # A new image (scans), or one moved into a gallery
    image_event = kind == "Image" and (action == "Create" or "gallery_ids" in fields)
    new_item = action == "Create" and kind in ("Scene", "Image")
    created = action == "Create" and kind in HOOK_ITEMS
    if not (performer_edit or studio_edit or gallery_edit or gallery_event or image_event or new_item or created):
        return

    cfg = settings(stash, plugin_dir)
    performers, galleries, images, tags = [], [], [], []
    counted = not (kind == "Image" and "images" not in cfg["kinds"])
    want_performers = performer_edit and not cfg["no_hook"] and counted
    want_studios = studio_edit and not cfg["no_hook"] and counted and "studios" in cfg["hook_owners"]
    want_galleries = gallery_edit and not cfg["no_hook"] and "galleries" in cfg["hook_owners"]
    # Tags were changed: any of the item's tags that still have no picture
    want_tags = performer_edit and "tag_ids" in fields and cfg["auto_pictures"]
    top_studios, top_galleries = [], []
    if want_performers or want_studios or want_galleries or want_tags:
        query_name, _ = HOOK_ITEMS[kind]
        extra = " galleries { id }" if kind == "Image" else ""
        item = stash.call(
            f"query($id: ID!) {{ {query_name}(id: $id) {{ performers {{ id }} studio {{ id }}{extra} tags {{ id image_path }} }} }}",
            {"id": str(hook["id"])},
        )[query_name] or {}
        if want_performers:
            performers = [p["id"] for p in item.get("performers") or []]
        if want_studios:
            top_studios = OWNERS["studios"]["ids"](item)
        if want_galleries:
            top_galleries = OWNERS["galleries"]["ids"](item)
        if want_tags:
            tags = [t["id"] for t in item.get("tags") or [] if not has_picture(t)]
    if cfg["gallery_tags"]:
        if gallery_event:
            galleries = [str(hook["id"])]
        if image_event:
            images = [str(hook["id"])]  # its galleries are looked up by the job
    ai_scenes, ai_images = [], []
    # (new items for the AI are queued by Better Tagging AI's own hook)
    dates = {}
    if created and cfg["dates_new"]:
        dates["dates_" + kind.lower()] = [str(hook["id"])]
    if created and cfg["titles_new"]:
        dates["titles_" + kind.lower()] = [str(hook["id"])]
    if performers or galleries or images or tags or ai_scenes or ai_images or dates or top_studios or top_galleries:
        enqueue(stash, plugin_dir, performers, galleries, images, tags, ai_scenes, ai_images,
                top_studios=top_studios, top_galleries=top_galleries, **dates)


def job_description():
    return f"{plugin_name()}: processing recent edits and new items"
QUIET_SECONDS = 4  # how long edits must stop before the batch counts as done
MAX_WAIT_SECONDS = 600
QUEUE_KEYS = ("performers", "galleries", "images", "tags", "ai_scenes", "ai_images",
              "autotag_performers", "autotag_studios", "autotag_tags",
              "dates_scene", "dates_image", "dates_gallery", "top_studios", "top_galleries",
              "titles_scene", "titles_image", "titles_gallery")


def enqueue(stash, plugin_dir, performers=(), galleries=(), images=(), tags=(), ai_scenes=(), ai_images=(),
            autotag_performers=(), autotag_studios=(), autotag_tags=(),
            dates_scene=(), dates_image=(), dates_gallery=(), top_studios=(), top_galleries=(),
            titles_scene=(), titles_image=(), titles_gallery=()):
    """Add to the waiting list and make sure one background job is lined up.

    Stash runs hooks one after another and waits for each, so a batch edit
    or a scan calls the hook once per item. Rather than doing the work there,
    the hook only adds to the list; the job does it all once things go quiet.
    """
    with locked_queue(plugin_dir) as queue:
        last = queue.get("touched", 0)
        for key, ids in zip(QUEUE_KEYS, (performers, galleries, images, tags, ai_scenes, ai_images,
                                         autotag_performers, autotag_studios, autotag_tags,
                                         dates_scene, dates_image, dates_gallery, top_studios, top_galleries,
                                         titles_scene, titles_image, titles_gallery)):
            if ids:
                queue[key] = sorted(set(queue.get(key, [])) | set(ids), key=int)
        queue["touched"] = time.time()
        # Mid-batch (a hook moments ago already lined up the job) skip asking
        # Stash's job queue again
        recent = time.time() - last < 30
        if not (queue.get("job_waiting") and (recent or job_pending(stash))):
            stash.call(
                "mutation($p: ID!, $d: String, $a: Map) "
                "{ runPluginTask(plugin_id: $p, description: $d, args_map: $a) }",
                {"p": PLUGIN_ID, "d": job_description(), "a": {"mode": "queued"}},
            )
            queue["job_waiting"] = True


def job_pending(stash):
    """Is our background job already waiting in or running on the job queue?"""
    jobs = stash.call("{ jobQueue { description status } }")["jobQueue"] or []
    return any(
        j["description"] == job_description() and j["status"] in ("READY", "RUNNING") for j in jobs
    )


def run_queued(stash, plugin_dir):
    """Background job: work through everything on the waiting list once."""
    started = time.time()
    while True:
        with locked_queue(plugin_dir) as queue:
            idle = time.time() - queue.get("touched", 0)
            if idle >= QUIET_SECONDS or time.time() - started > MAX_WAIT_SECONDS:
                todo = {k: queue.get(k, []) for k in QUEUE_KEYS}
                # From here on, new edits need a new job
                queue.clear()
                break
        time.sleep(1)

    cfg = settings(stash)

    # Gallery-name tags first
    galleries = set(todo["galleries"])
    if todo["images"] and cfg["gallery_tags"]:
        galleries |= galleries_of_images(stash, todo["images"])
    if galleries and cfg["gallery_tags"]:
        tag_galleries(stash, plugin_dir, cfg, sorted(galleries, key=int))

    if todo["tags"] and cfg["auto_pictures"]:
        give_pictures(stash, plugin_dir, todo["tags"])

    if (todo["autotag_performers"] or todo["autotag_studios"] or todo["autotag_tags"]) and cfg["autotag_new"]:
        try:
            autotag_new(stash, plugin_dir, todo["autotag_performers"], todo["autotag_studios"], todo["autotag_tags"])
        except Exception as e:
            log("warning", f"Auto tagging for new performers/studios/tags skipped: {e}")

    if cfg["dates_new"] and any(todo["dates_" + k] for k in ("scene", "image", "gallery")):
        try:
            filled = 0
            for k in ("scene", "image", "gallery"):
                if todo["dates_" + k]:
                    filled += fill_dates(stash, [(k, x) for x in autotag_load(stash, k, todo["dates_" + k])], apply=True, quiet=True)
            if filled:
                log("info", f"Filled in the date of {filled} new item(s) from their file names")
        except Exception as e:
            log("warning", f"Dates for new items skipped: {e}")

    if cfg["titles_new"] and any(todo["titles_" + k] for k in ("scene", "image", "gallery")):
        try:
            given = []
            for k in ("scene", "image", "gallery"):
                if todo["titles_" + k]:
                    given += fill_titles(stash, [(k, x) for x in autotag_load(stash, k, todo["titles_" + k])],
                                         apply=True, spellings=cfg["spellings"])
            if given:
                log("info", f"Gave {len(given)} new item(s) a title from their file names")
        except Exception as e:
            log("warning", f"Titles for new items skipped: {e}")

    if todo["ai_scenes"] or todo["ai_images"]:
        ai = cfg["ai"]
        new = {"scene": todo["ai_scenes"], "image": todo["ai_images"]}
        if ai["nightly"]:
            # quick steps now (no AI model), the AI model's at night
            if ai["auto"] or ai["new_all"]:
                try:
                    run_quick_new(stash, plugin_dir, cfg, new)
                except Exception as e:
                    log("warning", f"Quick steps for new items skipped: {e}")
            if ai["auto"] or ai["new_all"] or (ai["photo_studios"] and todo["ai_images"]):
                save_for_tonight(stash, plugin_dir, cfg, new)
        else:
            # tags, then a scene's studio and performers from accounts, then
            # a title (which can use all of that)
            if ai["auto"] or ai["new_all"]:
                try:
                    run_ai_queued(stash, plugin_dir, todo["ai_scenes"], todo["ai_images"])
                except Exception as e:
                    log("warning", f"AI tagging of new items skipped: {e}")
            if ai["new_all"] and todo["ai_scenes"]:
                try:
                    run_accounts_items(stash, plugin_dir, todo["ai_scenes"])
                except Exception as e:
                    log("warning", f"Studios from accounts for new scenes skipped: {e}")
            if ai["photo_studios"] and todo["ai_images"]:
                try:
                    run_accounts_items(stash, plugin_dir, todo["ai_images"], kind="image")
                except Exception as e:
                    log("warning", f"Studios from watermarks for new photos skipped: {e}")
        # who it looks like: quick (face helper), so always right away
        if ai["face_auto"]:
            try:
                run_faces(stash, plugin_dir, ["scene", "image"], ids=new)
            except Exception as e:
                log("warning", f"Faces for new items skipped: {e}")
        if ai["new_all"] and not ai["nightly"]:
            try:
                run_ai_titles(stash, plugin_dir, ["scene", "image"], ids=new)
            except Exception as e:
                log("warning", f"AI titles for new items skipped: {e}")

    work = [(o, todo[k]) for o, k in (("performers", "performers"), ("studios", "top_studios"),
                                      ("galleries", "top_galleries")) if todo[k]]
    if not work:
        return
    names, excluded = tag_lookup(stash, cfg, plugin_dir)
    for owner, ids in work:
        objs = find_owners(stash, owner, ids)
        kinds = owner_sources(owner, cfg)
        changed = 0
        with locked_record(plugin_dir) as record:
            for n, obj in enumerate(objs):
                counts = count_one(stash, owner, kinds, obj["id"])
                if refresh_owner(stash, cfg, owner, obj, counts, record, names, excluded, False):
                    changed += 1
                progress((n + 1) / len(objs))
        log("info", f"Refreshed top tags for {len(objs)} edited {OWNERS[owner]['one']}(s), {changed} changed.")


# --- gallery-name tags ----------------------------------------------------
# Every image in a gallery gets a tag named after the gallery. gallery_tags.json
# remembers, per gallery, which tag that is and whether this plugin created
# it, so a renamed gallery can swap its tag.

GALLERY_FIELDS = "id title files { basename } folder { path } studio { name aliases }"


def locked_gallery_record(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "gallery_tags.json"))


def gallery_name_tag_ids(plugin_dir):
    """Ids of the gallery-name tags this plugin created."""
    try:
        with open(os.path.join(base_dir(plugin_dir), "gallery_tags.json")) as f:
            record = json.load(f)
    except (OSError, ValueError):
        return set()
    return {v["tag"] for v in record.values() if v.get("created")}


GALLERY_ONLY_SHARE = 0.95  # a gallery-named tag this much on its gallery's photos is just the gallery's name
_GALLERY_ONLY = None


def gallery_only_tag_ids(stash, plugin_dir):
    """Tags that are just a gallery's name: named after a gallery (its title,
    zip or folder - whoever made the tag) and (almost) only on that
    gallery's photos, like a comic's title. Category folders ("BDSM",
    "Celeb"...) whose tag is also used elsewhere are real tags and aren't
    included. Kept for an hour (gallery_only_tags.json)."""
    global _GALLERY_ONLY
    if _GALLERY_ONLY is not None:
        return _GALLERY_ONLY
    path = os.path.join(base_dir(plugin_dir), "gallery_only_tags.json")
    with contextlib.suppress(Exception):
        with open(path) as f:
            saved = json.load(f)
        if time.time() - saved.get("at", 0) < 3600:
            _GALLERY_ONLY = set(saved["ids"])
            return _GALLERY_ONLY
    try:
        gals = stash.call("{ findGalleries(filter: {per_page: -1}) { galleries { id title image_count files { basename } "
                          "folder { path } studio { name aliases } } } }")["findGalleries"]["galleries"]
        by_name = defaultdict(list)
        for g in gals:
            n = gallery_name(g)
            if n:
                by_name[n.strip().lower()].append(g)
        tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name scene_count image_count } } }")["findTags"]["tags"]
        ids = set()
        for t in tags:
            gs = by_name.get(t["name"].strip().lower())
            uses = (t.get("scene_count") or 0) + (t.get("image_count") or 0)
            if not gs or not uses:
                continue
            inside = stash.call("query($t: [ID!], $g: [ID!]) { findImages(image_filter: {tags: {value: $t, modifier: INCLUDES}, "
                                "galleries: {value: $g, modifier: INCLUDES}}, filter: {per_page: 0}) { count } }",
                                {"t": [t["id"]], "g": [g["id"] for g in gs]})["findImages"]["count"]
            if inside >= uses * GALLERY_ONLY_SHARE:
                ids.add(t["id"])
    except Exception as e:
        log("debug", f"gallery-name tags: {e}")
        ids = set(gallery_name_tag_ids(plugin_dir))
    _GALLERY_ONLY = ids
    with contextlib.suppress(Exception):
        tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
        with open(tmp, "w") as f:
            json.dump({"at": time.time(), "ids": sorted(ids, key=int)}, f)
        os.replace(tmp, path)
    return ids


def without_studio(name, studio):
    """A gallery's name without its studio's name (or aliases) in it -
    "[Studio] Beach Day" or "Studio - Beach Day" becomes "Beach Day". A name
    that is only the studio stays as it is."""
    if not name or not studio:
        return name
    out = name
    for n in sorted([studio.get("name") or ""] + (studio.get("aliases") or []), key=len, reverse=True):
        words = re.findall(r"[^\W_]+", n)
        if not words:
            continue
        core = r"[\s_.\-]*".join(re.escape(w) for w in words)
        # the name, with any brackets round it, as whole words
        out = re.sub(r"[\[(\{]\s*" + core + r"\s*[\])\}]|(?<![^\W_])" + core + r"(?![^\W_])", " ", out, flags=re.I)
    if out == name:  # the studio isn't in it: leave the name exactly as it is
        return name
    out = re.sub(r"\s+", " ", out)
    out = re.sub(r"(\s*[-|:~,]\s*){2,}", " - ", out)  # "A -  - B" left where the name was
    out = re.sub(r"^[\s\-_|:~,.]+|[\s\-_|:~,]+$", "", out)
    out = re.sub(r"(?i)\s+by$", "", out)  # "Beach Day by Studio"
    return out if re.search(r"[^\W_]", out) else name


def gallery_name(g):
    """The gallery's title, else its zip's name (no extension), else its
    folder's - without its studio's name, unless that's all there is."""
    name = ""
    if (g.get("title") or "").strip():
        name = g["title"].strip()
    elif g.get("files"):
        name = os.path.splitext(g["files"][0]["basename"])[0].strip()
    elif g.get("folder") and g["folder"].get("path"):
        name = re.split(r"[\\/]", g["folder"]["path"].rstrip("\\/"))[-1].strip()
    return without_studio(name, g.get("studio"))


def galleries_of_images(stash, image_ids):
    found = set()
    for i in range(0, len(image_ids), 500):
        images = stash.call(
            "query($ids: [ID!]) { findImages(ids: $ids, filter: {per_page: -1}) { images { galleries { id } } } }",
            {"ids": image_ids[i:i + 500]},
        )["findImages"]["images"]
        for img in images:
            found |= {g["id"] for g in img.get("galleries") or []}
    return found


def find_tag(stash, name):
    """An existing tag with this name or alias (ignoring case), or None."""
    for field in ("name", "aliases"):
        tags = stash.call(
            "query($f: TagFilterType) { findTags(tag_filter: $f, filter: {per_page: 5}) { tags { id name aliases } } }",
            {"f": {field: {"value": name, "modifier": "EQUALS"}}},
        )["findTags"]["tags"]
        for t in tags:
            names = [t["name"]] + (t.get("aliases") or [])
            if any(n.lower() == name.lower() for n in names):
                return t["id"]
    return None


def ensure_tag(stash, name, parent_id=None):
    """(tag id, created?) for a tag with this name, creating it if needed."""
    tid = find_tag(stash, name)
    if tid:
        return tid, False
    tag_input = {"name": name}
    if parent_id:
        tag_input["parent_ids"] = [parent_id]
    created = stash.call(
        "mutation($i: TagCreateInput!) { tagCreate(input: $i) { id } }", {"i": tag_input}
    )["tagCreate"]["id"]
    return created, True


def bulk_images(stash, ids, tag_id, mode):
    for i in range(0, len(ids), BULK_BATCH):
        stash.call(
            "mutation($i: BulkImageUpdateInput!) { bulkImageUpdate(input: $i) { id } }",
            {"i": {"ids": ids[i:i + BULK_BATCH], "tag_ids": {"ids": [tag_id], "mode": mode}}},
        )


def tag_gallery(stash, g, record, parent_id):
    """Give every image in gallery g its gallery-name tag. Returns how many
    images were tagged."""
    gid = str(g["id"])
    name = gallery_name(g)
    if not name:
        return 0
    tag_id, created = ensure_tag(stash, name, parent_id)
    prev = record.get(gid)
    if prev and prev["tag"] == tag_id:
        created = created or prev.get("created", False)
    elif prev and prev.get("created"):
        # Renamed gallery: take the old name's tag (which this plugin made)
        # off its images
        # (the old tag may have been deleted since, e.g. by Library Cleanup)
        try:
            old = item_ids(stash, "images", {
                "galleries": {"value": [gid], "modifier": "INCLUDES"},
                "tags": {"value": [prev["tag"]], "modifier": "INCLUDES"},
            })
            bulk_images(stash, old, prev["tag"], "REMOVE")
        except Exception:
            old = []
        if old:
            log("info", f'Gallery renamed to "{name}": removed its old name tag from {len(old)} image(s)')
    record[gid] = {"tag": tag_id, "created": created}

    missing = item_ids(stash, "images", {
        "galleries": {"value": [gid], "modifier": "INCLUDES"},
        "tags": {"value": [tag_id], "modifier": "EXCLUDES"},
    })
    bulk_images(stash, missing, tag_id, "ADD")
    if missing:
        log("info", f'Tagged {len(missing)} image(s) in "{name}" with "{name}"')
    return len(missing)


def parent_tag_id(stash, cfg):
    if not cfg["gallery_parent"]:
        return None
    return ensure_tag(stash, cfg["gallery_parent"])[0]


def tag_galleries(stash, plugin_dir, cfg, gallery_ids):
    parent_id = parent_tag_id(stash, cfg)
    galleries = stash.call(
        f"query($ids: [ID!]) {{ findGalleries(ids: $ids, filter: {{per_page: -1}}) {{ galleries {{ {GALLERY_FIELDS} }} }} }}",
        {"ids": gallery_ids},
    )["findGalleries"]["galleries"]
    with locked_gallery_record(plugin_dir) as record:
        for g in galleries:
            tag_gallery(stash, g, record, parent_id)


def run_gallery_all(stash, plugin_dir):
    """Task: tag the images of every gallery with the gallery's name."""
    cfg = settings(stash)
    parent_id = parent_tag_id(stash, cfg)
    galleries = stash.call(
        f"{{ findGalleries(filter: {{per_page: -1}}) {{ galleries {{ {GALLERY_FIELDS} }} }} }}"
    )["findGalleries"]["galleries"]
    tagged = 0
    with locked_gallery_record(plugin_dir) as record:
        live = {str(g["id"]) for g in galleries}
        for gid in list(record):
            if gid not in live:
                del record[gid]
        for n, g in enumerate(galleries):
            try:
                tagged += tag_gallery(stash, g, record, parent_id)
            except Exception as e:
                log("warning", f"Gallery {g['id']}: {e}")
            progress((n + 1) / max(len(galleries), 1))
    log("info", f"Done: checked {len(galleries)} galleries, tagged {tagged} image(s) with their gallery's name.")


UNDERAGE_TAGS = {"lolicon", "loli", "shotacon", "shota", "toddlercon"}


def run_gallery_tags_to_images(stash, plugin_dir, preview=False, gallery_ids=None):
    """Task: give every image in a gallery the gallery's tags and performers,
    and its studio if the image has none (a studio already set is kept).
    gallery_ids: only these galleries (from the gallery page / list).
    Top tags this plugin put on the gallery (counted from its images) aren't
    copied back down, or a tag on a few images would spread to all of them."""
    with locked_record(plugin_dir) as record:
        auto = {k.split(":", 1)[1]: set(v) for k, v in record.items() if k.startswith("gallery:")}
    galleries = stash.call(
        "query($ids: [ID!]) { findGalleries(ids: $ids, filter: {per_page: -1}) { galleries { id title files { basename }"
        " folder { path } tags { id name } performers { id name } studio { id name aliases } } } }",
        {"ids": gallery_ids or None},
    )["findGalleries"]["galleries"]
    if gallery_ids and not any(g["tags"] or g["performers"] or g["studio"] for g in galleries):
        log("info", "Nothing to copy: the gallery has no tags, performers or studio.")
        return
    galleries = [g for g in galleries if g["tags"] or g["performers"] or g["studio"]]
    totals = Counter()
    changed = 0
    for n, g in enumerate(galleries):
        progress((n + 1) / max(len(galleries), 1))
        name = gallery_name(g) or f"gallery {g['id']}"
        if any(t["name"].strip().lower() in UNDERAGE_TAGS for t in g["tags"]):
            log("warning", f'"{name}": skipped - tagged as sexual content involving minors')
            continue
        in_gallery = {"galleries": {"value": [g["id"]], "modifier": "INCLUDES"}}
        skip = auto.get(str(g["id"]), set())
        added = []
        # (what, field to filter on, its id, label, bulk update input)
        work = [("tag", "tags", t["id"], t["name"], {"tag_ids": {"ids": [t["id"]], "mode": "ADD"}})
                for t in g["tags"] if t["id"] not in skip]
        work += [("performer", "performers", p["id"], p["name"], {"performer_ids": {"ids": [p["id"]], "mode": "ADD"}})
                 for p in g["performers"]]
        if g["studio"]:
            work.append(("studio", "studios", None, f'studio {g["studio"]["name"]}', {"studio_id": g["studio"]["id"]}))
        for what, field, oid, label, update in work:
            crit = {"modifier": "IS_NULL"} if oid is None else {"value": [oid], "modifier": "EXCLUDES"}
            missing = item_ids(stash, "images", {**in_gallery, field: crit})
            if not missing:
                continue
            if not preview:
                for i in range(0, len(missing), BULK_BATCH):
                    stash.call(
                        "mutation($i: BulkImageUpdateInput!) { bulkImageUpdate(input: $i) { id } }",
                        {"i": {"ids": missing[i:i + BULK_BATCH], **update}},
                    )
            added.append(f"{label} ({len(missing)})")
            totals[what] += len(missing)
        if added:
            changed += 1
            log("info", f'"{name}": {"would add" if preview else "added"} {", ".join(added)}')
    what = f"{totals['tag']} tag(s), {totals['performer']} performer(s) and {totals['studio']} studio(s)"
    if preview:
        log("info", f"Preview done: {changed} of {len(galleries)} galleries would add {what} to their images. Nothing was saved.")
    else:
        log("info", f"Done: {changed} of {len(galleries)} galleries added {what} to their images.")


# --- studio tags to its items --------------------------------------------
# Every scene, image and gallery of a studio gets the studio's own tags (the
# top tags this plugin added and any you added). Only this studio's items,
# not its sub-studios'. Nothing is removed.

STUDIO_KINDS = ("scenes", "images", "galleries")


def item_count(stash, kind, item_filter):
    query_name, _field, farg, ftype = KINDS[kind]
    return stash.call(
        f"query($p: {ftype}) {{ {query_name}({farg}: $p, filter: {{per_page: 0}}) {{ count }} }}",
        {"p": item_filter},
    )[query_name]["count"]


def of_studio(sid):
    return {"studios": {"value": [str(sid)], "modifier": "INCLUDES", "depth": 0}}


def studio_tags_to_copy(stash, plugin_dir, studio):
    """The studio's tags, less the ones to ignore (and gallery-name tags)."""
    cfg = settings(stash)
    excluded = {t["id"] for t in studio["tags"] if t["name"].lower() in cfg["exclude"]}
    excluded |= gallery_only_tag_ids(stash, plugin_dir)
    return [t for t in studio["tags"] if t["id"] not in excluded]


def studio_tag_plan(stash, plugin_dir, studio_id):
    """For the studio page: each of the studio's tags and how many of its
    scenes, images and galleries don't have it yet."""
    st = stash.call(
        "query($id: ID!) { findStudio(id: $id) { id name tags { id name } } }", {"id": str(studio_id)}
    )["findStudio"]
    if not st:
        raise RuntimeError(f"studio {studio_id} not found")
    if any(t["name"].strip().lower() in UNDERAGE_TAGS for t in st["tags"]):
        return {"name": st["name"], "blocked": True, "tags": [], "totals": {}}
    base = of_studio(st["id"])
    totals = {k: item_count(stash, k, base) for k in STUDIO_KINDS}
    tags = []
    for t in studio_tags_to_copy(stash, plugin_dir, st):
        missing = {
            k: (item_count(stash, k, {**base, "tags": {"value": [t["id"]], "modifier": "EXCLUDES"}}) if totals[k] else 0)
            for k in STUDIO_KINDS
        }
        tags.append({"id": t["id"], "name": t["name"], "missing": missing})
    return {"name": st["name"], "tags": tags, "totals": totals}


def run_studio_tags_to_items(stash, plugin_dir, preview=False, studio_ids=None, tag_ids=None, kinds=None):
    """Task / studio page: give a studio's scenes, images and galleries the
    studio's tags. studio_ids: only these studios (else every studio with
    tags); tag_ids: only these of its tags; kinds: scenes / images / galleries."""
    kinds = [k for k in (kinds or STUDIO_KINDS) if k in STUDIO_KINDS]
    studios = stash.call(
        "query($ids: [ID!]) { findStudios(ids: $ids, filter: {per_page: -1}) { studios { id name tags { id name } } } }",
        {"ids": studio_ids or None},
    )["findStudios"]["studios"]
    studios = [st for st in studios if st["tags"]]
    if not studios:
        log("info", "Nothing to do: " + ("the studio has no tags." if studio_ids else "no studio has tags."))
        return
    totals = Counter()
    changed = 0
    for n, st in enumerate(studios):
        progress(n / max(len(studios), 1))
        if any(t["name"].strip().lower() in UNDERAGE_TAGS for t in st["tags"]):
            log("warning", f'"{st["name"]}": skipped - tagged as sexual content involving minors')
            continue
        tags = studio_tags_to_copy(stash, plugin_dir, st)
        if tag_ids:
            tags = [t for t in tags if t["id"] in tag_ids]
        base = of_studio(st["id"])
        added = []
        for t in tags:
            for k in kinds:
                missing = item_ids(stash, k, {**base, "tags": {"value": [t["id"]], "modifier": "EXCLUDES"}})
                if not missing:
                    continue
                if not preview:
                    mutation, input_type = BULK_MUTATIONS[k]
                    for i in range(0, len(missing), BULK_BATCH):
                        stash.call(
                            f"mutation($i: {input_type}!) {{ {mutation}(input: $i) {{ id }} }}",
                            {"i": {"ids": missing[i:i + BULK_BATCH], "tag_ids": {"ids": [t["id"]], "mode": "ADD"}}},
                        )
                added.append(f"{t['name']} → {len(missing)} {k}")
                totals[k] += len(missing)
        if added:
            changed += 1
            log("info", f'"{st["name"]}": {"would add" if preview else "added"} {"; ".join(added)}')
    progress(1)
    what = ", ".join(f"{totals[k]} {k}" for k in STUDIO_KINDS if k in kinds)
    if preview:
        log("info", f"Preview done: {changed} of {len(studios)} studios would add their tags to {what}. Nothing was saved.")
    else:
        log("info", f"Done: {changed} of {len(studios)} studios added their tags to {what} (tags added per item).")


# --- "Tags to ignore" suggestions ----------------------------------------------
# Big tags that look like housekeeping rather than what's shown: their name
# (process, review, categories, underscores...), that they group other tags,
# or - with Better Tagging AI's picture fingerprints - that their items don't
# look alike at all. Shown under the setting; you choose what to add.

HOUSEKEEPING_WORDS = re.compile(
    r"\b(?:process(?:ed|ing)?|categor(?:y|ies)|to[ -]?review|review(?:ed)?|to[ -]?do|todo|unsorted|sorted|"
    r"organi[sz]ed|unorgani[sz]ed|missing|needs?|scraped|imported|import|export(?:ed)?|hidden|private|personal|"
    r"temp|test|misc|uncategori[sz]ed|wip|done|checked|tagged|untagged|queue|duplicates?|delete|ai|auto|"
    r"favou?rites?|watched|unwatched|downloaded|new|old|archive|collection|batch|import(?:ed)? from)\b", re.I)


def _coherence(vectors):
    """How alike a set of picture fingerprints are: the mean cosine of each
    to their average (0 = all different)."""
    if len(vectors) < 5:
        return None
    dims = len(vectors[0])
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return sum(sum(a * b for a, b in zip(v, mean)) / norm for v in vectors) / len(vectors)


def ignore_suggestions(stash, plugin_dir, limit=10):
    """Asked by the settings page: [{name, uses, share, why: [...]}], the
    likeliest housekeeping tags first (not ones already ignored)."""
    cfg = settings(stash, plugin_dir)
    tags = stash.call(f"{{ findTags(filter: {{per_page: -1}}) {{ tags {{ id name {TAG_USES_FIELDS} child_count }} }} }}")["findTags"]["tags"]
    counts = stash.call("{ findScenes(filter: {per_page: 0}) { count } findImages(filter: {per_page: 0}) { count } }")
    total = max(counts["findScenes"]["count"] + counts["findImages"]["count"], 1)
    skip = set(cfg["exclude"])
    gallery_tags = gallery_only_tag_ids(stash, plugin_dir)
    big = sorted((t for t in tags if t["id"] not in gallery_tags and t["name"].lower() not in skip),
                 key=lambda t: -(t.get("scene_count", 0) + t.get("image_count", 0)))[:200]
    big = [t for t in big if (t.get("scene_count", 0) + t.get("image_count", 0)) >= 20]

    # with the AI add-on's fingerprints: how alike each big tag's items look
    looks = {}
    ai_dir = os.path.join(os.path.dirname(os.path.abspath(plugin_dir)), AI_ID)
    with contextlib.suppress(Exception):
        fp = LookCache(ai_dir).data
        if fp:
            def unit(v):
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                return [x / n for x in v]
            members = defaultdict(list)
            wanted = {t["id"] for t in big}
            for key, ts in all_item_tags(stash).items():
                e = fp.get(key)
                if e and e.get("e"):
                    for t in ts & wanted:
                        if len(members[t]) < 150:
                            members[t].append(key)
            cache = {}
            for t in big:
                vs = [cache.setdefault(k, unit(_ints(fp[k]["e"]))) for k in members.get(t["id"], [])]
                c = _coherence(vs)
                if c is not None:
                    looks[t["id"]] = c
    cut = sorted(looks.values())[len(looks) // 4] if len(looks) >= 8 else None  # the least alike quarter

    out = []
    for t in big:
        n = t.get("scene_count", 0) + t.get("image_count", 0)
        why, score = [], 0
        # a name like a note or a process ("To Review", "Site_Process"), or a
        # tag that groups others; that its items don't look alike only backs
        # these up (whole pictures of e.g. feet in different settings don't)
        if HOUSEKEEPING_WORDS.search(t["name"]) or re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", t["name"]):
            why.append("its name sounds like housekeeping")
            score += 2
        if (t.get("child_count") or 0) >= 3:
            why.append(f"it groups {t['child_count']} other tags")
            score += 2
        if not why:
            continue
        if cut is not None and t["id"] in looks and looks[t["id"]] <= cut:
            why.append("its items don't look alike")
            score += 1
        share = n / total
        out.append({"name": t["name"], "uses": n, "share": round(share * 100), "why": why, "_k": score + share})
    out.sort(key=lambda x: -x["_k"])
    for x in out:
        x.pop("_k")
    return out[:limit]


# --- tag descriptions -------------------------------------------------------
# Tags without a description get one: from the stash-box they're linked to
# (StashDB...), else StashDB by exact name / alias, else the E-Hentai wiki
# (comic and doujinshi tags), else Danbooru's wiki. Only empty descriptions
# are filled; names, aliases and descriptions you've written are never touched.

WEB_HEADERS = {"User-Agent": "BetterTaggingTools/2.3 (Stash plugin; tag descriptions)"}
STASHDB = "https://stashdb.org/graphql"
CHAPTER_NAME = re.compile(r"^\d+(?:\.\d+)?\.?\s")  # "12. Family Values 1 - ..." (a story, not a tag to look up)


def _web_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=WEB_HEADERS), timeout=20) as r:
        return json.loads(r.read())


def _clean_desc(text):
    """Wiki markup -> plain text: links, bold / italics, DText tags."""
    t = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    t = re.sub(r"\[\[([^\]]+)\]\]", r"\1", t)
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"\[/?[a-z]+(?:=[^\]]*)?\]", "", t)          # DText [b], [i], [url=...]
    t = re.sub(r'"([^"]+)":\[?\S+?\]?(?=\s|$)', r"\1", t)  # DText "text":url
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1000]


def _stashbox_desc(stash, endpoint, query, names=None):
    """A stash-box tag's description, by its id - or by name, then only when
    its name or an alias is one of `names` (lower case)."""
    try:
        found = stash.call(
            "query($s: ScraperSourceInput!, $i: ScrapeSingleTagInput!) { scrapeSingleTag(source: $s, input: $i) { name description alias_list } }",
            {"s": {"stash_box_endpoint": endpoint}, "i": {"query": query}},
        )["scrapeSingleTag"] or []
    except Exception:
        return None
    for t in found:
        if names is not None and not ({t["name"].lower()} | {a.lower() for a in t.get("alias_list") or []}) & names:
            continue
        if (t.get("description") or "").strip():
            return _clean_desc(t["description"])
    return None


def _ehwiki_desc(name):
    title = name.strip().lower()
    try:
        d = _web_json("https://ehwiki.org/api.php?action=query&prop=revisions&rvprop=content&rvslots=main&format=json&titles="
                      + urllib.parse.quote(title))
    except Exception:
        return None
    page = next(iter(d.get("query", {}).get("pages", {}).values()), {})
    text = ((page.get("revisions") or [{}])[0].get("slots", {}).get("main", {}).get("*")) or ""
    m = re.search(r"\|\s*description\s*=(.*?)(?:\n\s*\||\n\}\})", text, re.S)
    return _clean_desc(m.group(1)) if m and m.group(1).strip() else None


def _danbooru_desc(name):
    """Danbooru's wiki - only for its general tags (what's shown), not
    artists, characters or series, and not one-line stubs."""
    key = name.strip().lower().replace(" ", "_")
    try:
        tags = _web_json("https://danbooru.donmai.us/tags.json?limit=1&search[name]=" + urllib.parse.quote(key))
        if not tags or tags[0].get("category") != 0:  # 0 = general
            return None
        d = _web_json("https://danbooru.donmai.us/wiki_pages.json?limit=1&search[title_normalize]=" + urllib.parse.quote(key))
    except Exception:
        return None
    if not d or not (d[0].get("body") or "").strip() or d[0].get("is_deleted"):
        return None
    first = _clean_desc(re.split(r"\r?\n\s*\r?\n|\r?\nh\d\.", d[0]["body"].strip())[0])
    return first if len(first) >= 40 else None


def tag_description(stash, tag):
    """(description, where it came from) or (None, None)."""
    names = {tag["name"].strip().lower()} | {a.strip().lower() for a in tag.get("aliases") or []}
    links = sorted(tag.get("stash_ids") or [], key=lambda x: x["endpoint"] != STASHDB)
    for link in links:  # the stash-box it's linked to (StashDB first)
        d = _stashbox_desc(stash, link["endpoint"], link["stash_id"])
        if d:
            return d, link["endpoint"].split("//")[-1].split("/")[0]
    if not any(l["endpoint"] == STASHDB for l in links):
        for n in [tag["name"], *(tag.get("aliases") or [])][:3]:
            d = _stashbox_desc(stash, STASHDB, n, names)
            if d:
                return d, "stashdb.org (by name)"
    for n in [tag["name"], *(tag.get("aliases") or [])][:3]:
        d = _ehwiki_desc(n)
        if d:
            return d, "E-Hentai wiki"
        time.sleep(0.2)
    d = _danbooru_desc(tag["name"])
    if d:
        return d, "Danbooru wiki"
    return None, None


def run_tag_descriptions(stash, plugin_dir, preview=False, limit=None):
    """Task: every tag with an empty description gets one (see above).
    preview: only log what would be set (on `limit` tags)."""
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name aliases description stash_ids { endpoint stash_id } } } }")["findTags"]["tags"]
    todo = [t for t in tags if not (t.get("description") or "").strip() and not CHAPTER_NAME.match(t["name"])]
    if limit:
        import random
        todo = random.sample(todo, min(limit, len(todo)))
    log("info", f"{len(todo)} tag(s) without a description" + (" (preview, nothing is saved)" if preview else
        " - looking each one up (a second or two each); cancel and run again to carry on"))
    found, sources, missing = 0, Counter(), []
    for n, t in enumerate(todo):
        progress(n / max(len(todo), 1))
        try:
            desc, where = tag_description(stash, t)
        except Exception as e:
            log("debug", f'"{t["name"]}": {e}')
            desc = None
        if not desc:
            missing.append(t["name"])
            continue
        found += 1
        sources[where] += 1
        log("info" if preview else "debug", f'"{t["name"]}" [{where}]: {desc[:200]}')
        if not preview:
            stash.call("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }", {"i": {"id": t["id"], "description": desc}})
    progress(1)
    where = ", ".join(f"{n} from {w}" for w, n in sources.most_common())
    log("info", f"{'Preview done' if preview else 'Done'}: {found} of {len(todo)} tag(s) {'would get' if preview else 'got'} a description"
        + (f" ({where})" if where else "") + ("." if not preview else ". Nothing was saved."))
    if missing:
        log("info", f"No description found for {len(missing)}: {short_list(missing)}")


# --- tag pictures ---------------------------------------------------------
# Tags without a picture get one from the library: the best photo with that
# tag (highest rated of the ones with the fewest other tags, i.e. most
# "about" the tag), else a scene screenshot, a marker screenshot or a gallery
# cover.
#
# Each tag should get its own picture. Related tags often share their best
# photo, so before a picture is used, its file is compared (by hash) with
# every other tag's picture and the next candidate is tried if it's taken.
# The rarest tags pick first, as they have the fewest photos to choose from.
# picture_hashes.json caches each tag's picture hash (keyed by its image
# link, which changes when the picture does).

TAG_USES_FIELDS = "image_count scene_count gallery_count scene_marker_count"
MAX_TRIES = 120  # candidate pictures tried per tag at most
# Last resort for a picture of its own: a frame from one of the tag's scenes,
# at these points through the video (the first few scenes only)
FRAME_POINTS = (0.5, 0.3, 0.7, 0.2, 0.8, 0.4, 0.6, 0.15, 0.85)
FRAME_SCENES = 5


def has_picture(tag):
    # Stash adds "default=true" to the image link of tags without a picture
    return "default=true" not in (tag.get("image_path") or "")


def uses(tag):
    return sum(tag.get(k) or 0 for k in TAG_USES_FIELDS.split())


def candidates(stash, tag_id):
    """Pictures for this tag, best first: (url, where it came from). Lazy, so
    later kinds are only looked up when the earlier ones run out."""
    has_tag = {"value": [tag_id], "modifier": "INCLUDES", "depth": 0}
    few_tags = {"per_page": 40, "sort": "tag_count", "direction": "ASC"}
    by_rating = lambda items: sorted(items, key=lambda i: -(i.get("rating100") or 0))  # stable: ties keep fewest-tags order

    images = stash.call(
        "query($t: ImageFilterType, $f: FindFilterType) { findImages(image_filter: $t, filter: $f) "
        "{ images { rating100 paths { thumbnail } visual_files { __typename } } } }",
        {"t": {"tags": has_tag}, "f": few_tags},
    )["findImages"]["images"]
    # photos only (not videos/animations stored as images)
    for i in by_rating(i for i in images if all(f["__typename"] == "ImageFile" for f in i["visual_files"])):
        if i["paths"].get("thumbnail"):
            yield i["paths"]["thumbnail"], "a photo"

    scenes = by_rating(stash.call(
        "query($t: SceneFilterType, $f: FindFilterType) { findScenes(scene_filter: $t, filter: $f) "
        "{ scenes { rating100 paths { screenshot } files { path duration } } } }",
        {"t": {"tags": has_tag}, "f": few_tags},
    )["findScenes"]["scenes"])
    for s in scenes:
        if s["paths"].get("screenshot"):
            yield s["paths"]["screenshot"], "a scene"

    markers = stash.call(
        "query($t: SceneMarkerFilterType) { findSceneMarkers(scene_marker_filter: $t, filter: {per_page: 10}) "
        "{ scene_markers { screenshot } } }",
        {"t": {"tags": has_tag}},
    )["findSceneMarkers"]["scene_markers"]
    for m in markers:
        if m.get("screenshot"):
            yield m["screenshot"], "a scene marker"

    galleries = stash.call(
        "query($t: GalleryFilterType) { findGalleries(gallery_filter: $t, filter: {per_page: 10}) "
        "{ galleries { id image_count paths { cover } } } }",
        {"t": {"tags": has_tag}},
    )["findGalleries"]["galleries"]
    for g in galleries:
        if g["image_count"] and g["paths"].get("cover"):
            yield g["paths"]["cover"], "a gallery cover"

    # Pages inside the tag's galleries: tags that are only on a gallery (a
    # comic's tags, say) would otherwise all have just its cover
    for g in galleries:
        if not g["image_count"]:
            continue
        pages = stash.call(
            "query($g: ImageFilterType, $f: FindFilterType) { findImages(image_filter: $g, filter: $f) "
            "{ images { paths { thumbnail } visual_files { __typename } } } }",
            {"g": {"galleries": {"value": [g["id"]], "modifier": "INCLUDES"}}, "f": {"per_page": 60, "sort": "title"}},
        )["findImages"]["images"]
        for i in pages:
            if all(f["__typename"] == "ImageFile" for f in i["visual_files"]) and i["paths"].get("thumbnail"):
                yield i["paths"]["thumbnail"], "a page of one of its galleries"

    # Frames from the tag's scenes: tags that only appear on scenes shared
    # with many other tags can each get a different moment of the video
    for s in scenes[:FRAME_SCENES]:
        f = (s.get("files") or [None])[0]
        if not f or not f.get("path") or not (f.get("duration") or 0) > 2:
            continue
        for point in FRAME_POINTS:
            yield {"video": f["path"], "at": f["duration"] * point}, "a frame from a scene"


_FFMPEG = None


def ffmpeg_path(stash):
    """Stash's own ffmpeg: the path set in Settings, then the system PATH,
    then the copy Stash downloads into its config folder."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    found = []
    try:
        configured = stash.call("{ configuration { general { ffmpegPath } } }")["configuration"]["general"]["ffmpegPath"]
        if configured:
            found.append(configured)
    except Exception:
        pass
    found.append(shutil.which("ffmpeg"))
    if stash.config_dir:
        found += [os.path.join(stash.config_dir, "ffmpeg"), os.path.join(stash.config_dir, "ffmpeg.exe")]
    for c in found:
        if c and os.path.isfile(c):
            _FFMPEG = c
            return c
    raise RuntimeError("ffmpeg not found")


def grab_frame(stash, video, at, width=640):
    """One frame of a video as a JPEG, `width` pixels wide (never enlarged)."""
    proc = subprocess.run(
        [ffmpeg_path(stash), "-loglevel", "error", "-ss", f"{at:.2f}", "-i", video,
         # yuvj420p: 10-bit / HDR phone videos can't go straight to JPEG
         "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2,format=yuvj420p", "-q:v", "3",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True, timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "no frame")
    return proc.stdout, "image/jpeg"


def locked_picture_hashes(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "picture_hashes.json"))


def picture_hashes(stash, cache, tags):
    """Hash of every tag's current picture: {tag id: hash}. `cache` (the
    picture_hashes.json dict) is updated in place; only new or changed
    pictures are downloaded."""
    out = {}
    live = set()
    for t in tags:
        if not has_picture(t):
            continue
        live.add(t["id"])
        entry = cache.get(t["id"])
        if not entry or entry.get("link") != t["image_path"]:
            try:
                data, _ = stash.fetch(t["image_path"])
            except Exception:
                continue
            entry = {**(entry or {}), "link": t["image_path"], "hash": hashlib.sha1(data).hexdigest()}
            cache[t["id"]] = entry
        out[t["id"]] = entry["hash"]
    for tid in [k for k in cache if k not in live]:
        del cache[tid]
    return out


def give_picture(stash, tag, taken, cache, avoid=(), cands=None, keep=None, looks=None):
    """Give one tag a picture no other tag has (by file hash) - a picture is
    never used for two tags; if every candidate is another tag's, the tag
    gets nothing. `taken` maps hash -> tag id and is updated; pictures in
    `avoid` (by hash) are never used. cands: the pictures to try, best
    first (default: the usual ones). keep: the hash of the tag's current
    picture - if that's the one picked, nothing is saved. looks (TagLooks,
    when the face helper is running): a picture that looks the same as
    another tag's (a variant of the same page, a near-identical shot)
    counts as that tag's too. Returns (source, False) or None; source is
    None when the tag already had that picture."""
    for n, (where, source) in enumerate(cands if cands is not None else candidates(stash, tag["id"])):
        if n >= MAX_TRIES:
            break
        try:
            if isinstance(where, dict):
                data, ctype = grab_frame(stash, where["video"], where["at"])
            else:
                data, ctype = stash.fetch(where)
        except Exception:
            continue
        if not ctype.startswith("image/") or not data:
            continue
        h = hashlib.sha1(data).hexdigest()
        if h in avoid:
            continue
        if taken.get(h, tag["id"]) != tag["id"]:
            continue  # another tag's picture
        fp = None
        if looks:
            fp = looks.fingerprint(data)
            if fp and looks.clash(fp, tag["id"]):
                continue  # looks the same as another tag's picture
        if keep and h == keep:
            taken[h] = tag["id"]
            if looks and fp:
                looks.claim(tag["id"], fp, cache)
            return None, False
        set_picture(stash, tag, data, ctype, h, taken, cache)
        if looks and fp:
            looks.claim(tag["id"], fp, cache)
        return source, False
    return None


SAME_LOOK = 0.96  # picture fingerprints this alike are the same picture
# (variants of one comic page scored 0.988-0.998; different photos never
# above 0.94)


class TagLooks:
    """What every tag's picture looks like (a picture fingerprint from the
    face helper, cached in picture_hashes.json next to the file hash), so a
    picture that only differs a little from another tag's - a variant of the
    same page, a near-identical shot - isn't used again."""

    def __init__(self, stash, cfg, cache, tags):
        self.cfg = cfg
        self.fp = {}  # tag id -> unit fingerprint
        todo = []
        for t in tags:
            e = cache.get(t["id"])
            if not has_picture(t) or not e:
                continue
            if e.get("fp") and e.get("fp_link") == e.get("link"):
                self.fp[t["id"]] = self._unit(_ints(e["fp"]))
            else:
                todo.append(t)
        if todo:
            log("info", f"Fingerprinting {len(todo)} tag picture(s) to compare their looks (once)...")
        for i in range(0, len(todo), 16):
            batch, pics = [], []
            for t in todo[i:i + 16]:
                with contextlib.suppress(Exception):
                    pics.append(stash.fetch(cache[t["id"]]["link"])[0])
                    batch.append(t)
            if not pics:
                continue
            for t, emb in zip(batch, picture_helper(cfg, pics)):
                if emb:
                    cache[t["id"]]["fp"] = q8(emb)
                    cache[t["id"]]["fp_link"] = cache[t["id"]]["link"]
                    self.fp[t["id"]] = self._unit(emb)

    @staticmethod
    def _unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    @classmethod
    def maybe(cls, stash, plugin_dir, cache, tags):
        """TagLooks if the face helper (with its picture model) is running,
        else None (then only identical files count as the same)."""
        try:
            cfg = settings(stash, plugin_dir)
            with urllib.request.urlopen(cfg["ai"]["face_url"] + "/health", timeout=4) as resp:
                if not json.loads(resp.read()).get("embed"):
                    return None
            return cls(stash, cfg, cache, tags)
        except Exception as e:
            log("debug", f"Tag pictures: not comparing looks ({e})")
            return None

    def fingerprint(self, data):
        try:
            emb = picture_helper(self.cfg, [data])[0]
        except Exception:
            return None
        return self._unit(emb) if emb else None

    def clash(self, fp, tag_id):
        """Another tag whose picture looks the same, or None."""
        for tid, other in self.fp.items():
            if tid != tag_id and sum(map(operator.mul, fp, other)) >= SAME_LOOK:
                return tid
        return None

    def claim(self, tag_id, fp, cache):
        self.fp[tag_id] = fp
        e = cache.get(tag_id)
        if e is not None:
            e["fp"] = q8(fp)
            e["fp_link"] = e.get("link")

    def groups(self, tag_ids):
        """Tags whose pictures look the same: [[tag ids]] (2 or more each)."""
        ids = [t for t in tag_ids if t in self.fp]
        parent = {t: t for t in ids}

        def root(t):
            while parent[t] != t:
                parent[t] = parent[parent[t]]
                t = parent[t]
            return t
        for i, a in enumerate(ids):
            va = self.fp[a]
            for b in ids[i + 1:]:
                if sum(map(operator.mul, va, self.fp[b])) >= SAME_LOOK:
                    parent[root(b)] = root(a)
        out = defaultdict(list)
        for t in ids:
            out[root(t)].append(t)
        return [g for g in out.values() if len(g) > 1]


def set_picture(stash, tag, data, ctype, h, taken, cache):
    uri = f"data:{ctype};base64," + base64.b64encode(data).decode()
    link = stash.call(
        "mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { image_path } }",
        {"i": {"id": tag["id"], "image": uri}},
    )["tagUpdate"]["image_path"]
    taken[h] = tag["id"]
    cache[tag["id"]] = {**(cache.get(tag["id"]) or {}), "link": link, "hash": h}


def run_frame(stash, args):
    """Asked by the page (runPluginOperation): one frame of a scene at a
    given time, as a data URI, for the "Tag picture" window."""
    scene = stash.call(
        "query($id: ID!) { findScene(id: $id) { files { path duration } } }", {"id": str(args["scene_id"])}
    )["findScene"]
    f = (scene or {}).get("files") or []
    if not f:
        raise RuntimeError("this scene has no video file")
    duration = f[0].get("duration") or 0
    at = min(max(float(args.get("at") or 0), 0), max(duration - 0.1, 0))
    data, ctype = grab_frame(stash, f[0]["path"], at, width=960)
    return f"data:{ctype};base64," + base64.b64encode(data).decode()


def all_tags_for_pictures(stash):
    return stash.call(
        f"{{ findTags(filter: {{per_page: -1}}) {{ tags {{ id name image_path {TAG_USES_FIELDS} }} }} }}"
    )["findTags"]["tags"]


def taken_map(hashes, prefer=()):
    """hash -> one tag using it; tags in `prefer` win."""
    taken = {}
    for tid, h in hashes.items():
        taken.setdefault(h, tid)
    for tid in prefer:
        if tid in hashes:
            taken[hashes[tid]] = tid
    return taken


def give_pictures(stash, plugin_dir, tag_ids):
    """Job: tags that were just used and still have no picture get one."""
    wanted = set(tag_ids)
    tags = all_tags_for_pictures(stash)
    todo = sorted((t for t in tags if t["id"] in wanted and not has_picture(t)), key=uses)
    if not todo:
        return
    with locked_picture_hashes(plugin_dir) as cache:
        taken = taken_map(picture_hashes(stash, cache, tags))
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        for t in todo:
            try:
                result = give_picture(stash, t, taken, cache, looks=looks)
                if result:
                    log("info", f'Gave tag "{t["name"]}" a picture from {result[0]}')
                else:
                    log("info", f'Tag "{t["name"]}": no picture of its own (every one is already another tag\'s)')
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')


def run_tag_pictures(stash, plugin_dir):
    """Task: every tag without a picture gets its own one from the library."""
    tags = all_tags_for_pictures(stash)
    todo = sorted((t for t in tags if not has_picture(t)), key=uses)  # rarest first
    log("info", f"{len(todo)} of {len(tags)} tags have no picture")
    given, nothing = 0, []
    with locked_picture_hashes(plugin_dir) as cache:
        taken = taken_map(picture_hashes(stash, cache, tags))
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        for n, t in enumerate(todo):
            try:
                result = give_picture(stash, t, taken, cache, looks=looks)
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result:
                given += 1
                log("debug", f'"{t["name"]}": picture from {result[0]}')
            else:
                nothing.append(t["name"])
            progress((n + 1) / max(len(todo), 1))
    log("info", f"Done: gave {given} tag(s) a picture.")
    if nothing:
        log("info", f"{len(nothing)} tag(s) were left without one: they aren't used on anything with a picture, or "
            f"every picture they have is already another tag's (a picture is never used twice): {short_list(nothing)}")


def run_unique_pictures(stash, plugin_dir):
    """Task: tags sharing the same picture get their own where possible. In
    each group the rarest tag keeps the picture."""
    tags = all_tags_for_pictures(stash)
    by_id = {t["id"]: t for t in tags}
    with locked_picture_hashes(plugin_dir) as cache:
        log("info", "Comparing tag pictures (the first run downloads each tag's picture once)...")
        hashes = picture_hashes(stash, cache, tags)
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        if looks:
            # same file, or pictures that look the same (variants of a page)
            same = {}
            for tid, h in hashes.items():
                same.setdefault(h, []).append(tid)
            for g in looks.groups(list(hashes)):
                for tid in g:
                    same.setdefault(hashes[tid], [])
                merged = set(g)
                for h in {hashes[t] for t in g}:
                    merged |= set(same.pop(h, []))
                same["look:" + min(merged)] = sorted(merged)
            groups = [sorted((by_id[t] for t in g), key=uses) for g in same.values() if len(g) > 1]
        else:
            groups = {}
            for tid, h in hashes.items():
                groups.setdefault(h, []).append(by_id[tid])
            groups = [sorted(g, key=uses) for g in groups.values() if len(g) > 1]
        movers = [t for g in groups for t in g[1:]]
        log("info", f"{len(groups)} picture(s) are shared by more than one tag ({len(movers) + len(groups)} tags).")
        taken = taken_map(hashes, prefer=[g[0]["id"] for g in groups])
        changed, stuck = 0, []
        if looks:  # the movers' current pictures don't count as theirs
            for t in movers:
                looks.fp.pop(t["id"], None)
        for n, t in enumerate(sorted(movers, key=uses)):
            try:
                result = give_picture(stash, t, taken, cache, looks=looks)
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result:
                changed += 1
                log("debug", f'"{t["name"]}": new picture from {result[0]}')
            else:
                stuck.append(t["name"])
            progress((n + 1) / max(len(movers), 1))
    log("info", f"Done: gave {changed} tag(s) a picture of their own.")
    if stuck:
        log("info", f"{len(stuck)} tag(s) still share a picture because every picture they have is already another tag's: {short_list(stuck)}")


def run_new_pictures(stash, plugin_dir):
    """Task: every tag gets a new picture - a different one from the picture
    it has now, unique where possible. Tags with no other picture available
    keep theirs."""
    tags = sorted(all_tags_for_pictures(stash), key=uses)  # rarest first
    log("info", f"Picking new pictures for {len(tags)} tags (rarest first)...")
    given, kept, nothing = 0, [], 0
    with locked_picture_hashes(plugin_dir) as cache:
        current = picture_hashes(stash, cache, tags)
        # every tag's current picture stays its own until that tag has moved
        # on, so a picture never ends up on two tags
        taken = taken_map(current)
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        for n, t in enumerate(tags):
            old = current.get(t["id"])
            try:
                result = give_picture(stash, t, taken, cache,
                                      avoid={old} if old else (), looks=looks)
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result:
                given += 1
                if old and taken.get(old) == t["id"]:
                    del taken[old]  # its old picture is free for the tags after it
                log("debug", f'"{t["name"]}": new picture from {result[0]}')
            elif old:
                taken.setdefault(old, t["id"])
                kept.append(t["name"])
            else:
                nothing += 1
            progress((n + 1) / max(len(tags), 1))
    log("info", f"Done: gave {given} tag(s) a new picture.")
    if kept:
        log("info", f"{len(kept)} tag(s) kept their picture because there's no other unused one: {short_list(kept)}")
    if nothing:
        log("info", f"{nothing} tag(s) still have no picture (not used on anything with one).")


# --- most typical tag pictures (AI add-on) ----------------------------------
# A tag's "typical look" is the average picture fingerprint (look_items.json)
# of the scenes and photos that carry it - leaving out ones where the add-on
# added the tag itself, so its own guesses don't count. The tag's photo or
# scene closest to that average is its most typical picture. The usual rules
# still apply: a picture is never used for two tags.

TYPICAL_MIN = 3    # fingerprinted items with the tag needed for a typical look
TYPICAL_TRY = 40   # best-matching pictures tried per tag (for a unique one)
# The fewer other tags an item has, the more likely it's about this one: its
# score drops by this much times log(number of tags on it), so 1 -> 5 tags
# matters more than 30 -> 35. (Gallery-name tags don't count.)
TYPICAL_FOCUS = 0.15


class TypicalPictures:
    def __init__(self, stash, plugin_dir, item_tags=None, focus=TYPICAL_FOCUS):
        """item_tags: {"scene:ID"/"image:ID": {tag ids}} for every item (the
        whole library); looked up when not given."""
        self.stash = stash
        self.base = stash.url.rsplit("/graphql", 1)[0]
        def unit(v):
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            return [x / n for x in v]
        raw = {k: unit(_ints(e["e"])) for k, e in LookCache(plugin_dir).data.items() if e.get("e")}
        # Take away the library's average look (photos and scenes apart):
        # otherwise the most "average" pictures win for every tag they're
        # on, rather than the ones that show what's special about the tag
        self.vectors = {}
        for kind in ("image:", "scene:"):
            ks = [k for k in raw if k.startswith(kind)]
            if not ks:
                continue
            avg = [sum(col) / len(ks) for col in zip(*(raw[k] for k in ks))]
            for k in ks:
                self.vectors[k] = unit([a - b for a, b in zip(raw[k], avg)])
        try:
            with open(os.path.join(plugin_dir, "machine_tags.json")) as f:
                self.machine = {k: set(v) for k, v in json.load(f).items()}
        except (OSError, ValueError):
            self.machine = {}
        if item_tags is None:
            item_tags = all_item_tags(stash)
        self.focus = focus
        gallery_tags = gallery_only_tag_ids(stash, plugin_dir)
        self.members = defaultdict(list)
        self.tag_count = {}
        for key, tags in item_tags.items():
            if key in self.vectors:
                self.tag_count[key] = max(len(tags - gallery_tags), 1)
                for t in tags:
                    self.members[t].append(key)

    def _members(self, tag_id):
        return self.members.get(tag_id, [])

    def ranked(self, tag_id):
        """[(match 0-1, "scene:ID"/"image:ID")], most typical first; [] when
        too few of the tag's items have a fingerprint."""
        keys = self._members(tag_id)
        learn = [self.vectors[k] for k in keys if tag_id not in self.machine.get(k, ())]
        if len(learn) < TYPICAL_MIN:
            return []
        # each item against the average of the tag's OTHER items, so an item
        # doesn't count towards its own score
        total = [sum(col) for col in zip(*learn)]
        tt = sum(x * x for x in total)
        learned = {k for k in keys if tag_id not in self.machine.get(k, ())}
        out = []
        for k in keys:
            v = self.vectors[k]
            dot = sum(map(operator.mul, v, total))
            if k in learned:  # (total - v): dot = dot - 1, length^2 = tt - 2 dot + 1
                dot, length = dot - 1.0, math.sqrt(max(tt - 2 * dot + 1.0, 1e-9))
            else:
                length = math.sqrt(tt) or 1.0
            # fewer other tags: more likely to be about this one
            out.append((dot / length - self.focus * math.log(self.tag_count.get(k, 1)), k))
        # A scene's fingerprint is an average of several frames, so it's
        # always "closer to the middle" than a single photo: rank photos and
        # scenes apart, and try first the kind the tag is mostly on
        photos = sum(1 for k in keys if k.startswith("image:"))
        first = "image:" if photos * 2 >= len(keys) else "scene:"
        out.sort(key=lambda sk: (not sk[1].startswith(first), -sk[0]))
        return out

    def link(self, key):
        kind, iid = key.split(":", 1)
        return f"{self.base}/image/{iid}/thumbnail" if kind == "image" else f"{self.base}/scene/{iid}/screenshot"

    def candidates(self, tag_id, ranked=None, fallback=True):
        """Pictures for give_picture: the most typical ones first, then (if
        fallback) the usual ones."""
        for score, key in (ranked if ranked is not None else self.ranked(tag_id))[:TYPICAL_TRY]:
            what = "photo" if key.startswith("image:") else "scene"
            yield self.link(key), f"the most typical {what} ({key.split(':')[0]} {key.split(':')[1]}, score {score:.2f}, {self.tag_count.get(key, 1)} tags)"
        if fallback:
            yield from candidates(self.stash, tag_id)


def run_typical_pictures(stash, plugin_dir, mode):
    """Tasks (AI add-on): give tags their most typical picture.
    mode "preview": only write what each tag would get to the log;
    "missing": tags without a picture; "all": every tag with enough
    fingerprinted items, replacing its picture (tags without enough keep
    theirs)."""
    log("info", "Working out each tag's typical look from the picture fingerprints...")
    tags = all_tags_for_pictures(stash)
    typical = TypicalPictures(stash, plugin_dir, all_item_tags(stash))
    if not typical.vectors:
        log("warning", 'No picture fingerprints yet: run "Look-alike tags: scan scenes and images" first.')
        return
    ranked = {}
    for n, t in enumerate(tags):
        r = typical.ranked(t["id"])
        if r:
            ranked[t["id"]] = r
        progress(0.5 * (n + 1) / max(len(tags), 1))
    if mode == "missing":
        todo = [t for t in tags if not has_picture(t)]
    else:
        todo = [t for t in tags if t["id"] in ranked]
    todo.sort(key=uses)  # rarest first: they have the fewest pictures to pick from
    few = sum(1 for t in tags if t["id"] not in ranked)
    log("info", f"{len(ranked)} of {len(tags)} tags have a typical look ({few} are on fewer than {TYPICAL_MIN} "
        "fingerprinted items; run the look-alike scan to include newer items).")

    if mode == "preview":
        for t in todo[:400]:
            r = ranked.get(t["id"])
            if r:
                score, key = r[0]
                log("info", f'"{t["name"]}": {key.replace(":", " ")} (score {score:.2f}, {typical.tag_count.get(key, 1)} tags) - {typical.link(key)}')
        log("info", f"Preview done: {len(todo)} tag(s) listed. Nothing was saved. (The tasks also make sure each tag's "
            "picture is its own, so a tag can end up with its next most typical picture.)")
        return

    given, kept, nothing, stuck = 0, 0, [], []
    with locked_picture_hashes(base_dir(plugin_dir)) as cache:
        hashes = picture_hashes(stash, cache, tags)
        # every tag's current picture stays its own until that tag moves to
        # another one, so a picture never ends up on two tags
        taken = taken_map(hashes)
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        for n, t in enumerate(todo):
            progress(0.5 + 0.5 * (n + 1) / max(len(todo), 1))
            r = ranked.get(t["id"])
            try:
                old = hashes.get(t["id"])
                if mode == "missing":
                    # (no typical look: the usual pick)
                    result = give_picture(stash, t, taken, cache, cands=typical.candidates(t["id"], r or []), looks=looks)
                else:
                    result = give_picture(stash, t, taken, cache,
                                          cands=typical.candidates(t["id"], r, fallback=False), keep=old, looks=looks)
                    own = old and taken.get(old, t["id"]) == t["id"] and not (
                        looks and t["id"] in looks.fp and looks.clash(looks.fp[t["id"]], t["id"]))
                    if not result and own:
                        taken[old] = t["id"]  # nothing typical of its own: keep its picture
                        result = (None, False)
                    elif not result:
                        # its picture is (or looks like) another tag's: the usual pick
                        result = give_picture(stash, t, taken, cache, looks=looks)
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result and result[0] is None:
                kept += 1
            elif result:
                given += 1
                old = hashes.get(t["id"])
                if old and taken.get(old) == t["id"] and cache.get(t["id"], {}).get("hash") != old:
                    del taken[old]  # its old picture is free for the tags after it
                log("debug", f'"{t["name"]}": picture from {result[0]}')
            elif hashes.get(t["id"]):
                stuck.append(t["name"])
            else:
                nothing.append(t["name"])
    log("info", f"Done: gave {given} tag(s) their most typical picture" + (f"; {kept} already had it (or nothing better of their own)." if kept else "."))
    if stuck:
        log("info", f"{len(stuck)} tag(s) have no picture left that isn't another tag's, so they still share "
            f"their old one: {short_list(stuck)}")
    if nothing:
        log("info", f"{len(nothing)} tag(s) were left without a picture: none of theirs is unused (a picture is "
            f"never used twice): {short_list(nothing)}")


def run_typical_picture(stash, plugin_dir, tag_id):
    """Asked by the page (runPluginOperation) for "Most typical picture" on
    the Tags list: give one tag its most typical picture that's its own.
    Returns {"link", "changed", "reason"}."""
    tags = all_tags_for_pictures(stash)
    tag = next((t for t in tags if str(t["id"]) == str(tag_id)), None)
    if not tag:
        raise RuntimeError(f"tag {tag_id} not found")
    typical = TypicalPictures(stash, plugin_dir)
    ranked = typical.ranked(tag["id"])
    if not ranked:
        return {"link": None, "changed": False, "reason": "few"}
    with locked_picture_hashes(base_dir(plugin_dir)) as cache:
        hashes = picture_hashes(stash, cache, tags)
        taken = {h: tid for h, tid in taken_map(hashes).items() if tid != tag["id"]}
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        result = give_picture(stash, tag, taken, cache,
                              cands=typical.candidates(tag["id"], ranked, fallback=False), keep=hashes.get(tag["id"]),
                              looks=looks)
        if not result:
            return {"link": None, "changed": False, "reason": "taken"}
        if result[0] is None:
            return {"link": tag["image_path"], "changed": False, "reason": "same"}
        log("info", f'"{tag["name"]}": picture from {result[0]}')
        return {"link": cache[tag["id"]]["link"], "changed": True}


RECENT_PICTURES = 20  # earlier pictures of a tag skipped by "Pick a new picture"


def run_new_picture(stash, plugin_dir, tag_id):
    """Asked by the page (runPluginOperation) for "Pick a new picture" on the
    Tags list: give one tag the next picture by the usual rules - unique,
    and not one it's had recently (so pressing it again keeps moving on;
    when there's nothing new left it cycles back through the older ones).
    Returns the new picture's link, or None if there's no other picture."""
    tags = all_tags_for_pictures(stash)
    tag = next((t for t in tags if str(t["id"]) == str(tag_id)), None)
    if not tag:
        raise RuntimeError(f"tag {tag_id} not found")
    with locked_picture_hashes(plugin_dir) as cache:
        hashes = picture_hashes(stash, cache, tags)
        current = hashes.get(tag["id"])
        recent = list((cache.get(tag["id"]) or {}).get("recent", []))
        taken = {h: tid for h, tid in taken_map(hashes).items() if tid != tag["id"]}
        now = {current} if current else set()
        looks = TagLooks.maybe(stash, plugin_dir, cache, tags)
        result = give_picture(stash, tag, taken, cache, avoid=set(recent) | now, looks=looks)
        while not result and recent:
            # been through them all: allow the one it had longest ago again
            recent.pop()
            result = give_picture(stash, tag, taken, cache, avoid=set(recent) | now, looks=looks)
        if not result:
            return None
        if current:
            recent = ([current] + [h for h in recent if h != current])[:RECENT_PICTURES]
        cache[tag["id"]]["recent"] = recent
        log("info", f'"{tag["name"]}": new picture from {result[0]}')
        return cache[tag["id"]]["link"]


def short_list(names, limit=40):
    names = sorted(names)
    return ", ".join(names[:limit]) + (" ..." if len(names) > limit else "")


# --- tag title case -------------------------------------------------------
# Keeps tag names capitalised the same way: "dating app" -> "Dating App",
# while small words stay lowercase in the middle ("Behind the Scenes").
# Capitals already in a name are never lowered ("POV", "McKenzie"), words
# starting with a digit are left alone ("3rd"), hyphenated words count as one
# ("T-shirt"), and the "Special spellings" setting forces exact spellings
# (e.g. "CNN").

MINOR_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor",
    "of", "on", "onto", "or", "per", "the", "to", "via", "vs", "vs.", "with",
}
NAME_WORD = re.compile(r"\S+")  # words are split on spaces; "T-shirt" stays one word


def capitalise_word(word, spellings):
    special = spellings.get(word.lower())
    if special:
        return special
    # capitalise the first letter (skipping a leading "(" or quote); words
    # starting with a digit ("3rd", "4k") are left alone
    at = next((j for j, c in enumerate(word) if c.isalnum()), None)
    if at is None or not word[at].islower():
        return word
    return word[:at] + word[at].upper() + word[at + 1:]


def title_case(name, spellings):
    whole = spellings.get(name.strip().lower())
    if whole:
        return whole
    words = list(NAME_WORD.finditer(name))
    parts, last = [], 0
    for i, m in enumerate(words):
        word = m.group()
        middle = 0 < i < len(words) - 1
        parts.append(name[last:m.start()])
        if middle and word.lower() in MINOR_WORDS and word.lower() not in spellings:
            parts.append(word)
        else:
            parts.append(capitalise_word(word, spellings))
        last = m.end()
    parts.append(name[last:])
    return "".join(parts)


def rename_tag(stash, tag, new):
    stash.call(
        "mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }",
        {"i": {"id": tag["id"], "name": new}},
    )


def run_title_case(stash, dry_run):
    """Task: capitalise every tag name that needs it (or just list them)."""
    cfg = settings(stash)
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
    changed = failed = 0
    for n, tag in enumerate(tags):
        new = title_case(tag["name"], cfg["spellings"])
        if new != tag["name"]:
            log("info", f'"{tag["name"]}" -> "{new}"')
            if not dry_run:
                try:
                    rename_tag(stash, tag, new)
                except Exception as e:
                    failed += 1
                    log("warning", f'Could not rename "{tag["name"]}": {e}')
                    continue
            changed += 1
        progress((n + 1) / max(len(tags), 1))
    if dry_run:
        log("info", f"Preview done: {changed} of {len(tags)} tags would be renamed. Nothing was saved.")
    else:
        extra = f", {failed} failed (see warnings)" if failed else ""
        log("info", f"Done: renamed {changed} of {len(tags)} tags{extra}.")


def title_case_new_tag(stash, plugin_dir, tag_id):
    """Hook: capitalise a tag as soon as it's created (when switched on)."""
    cfg = settings(stash, plugin_dir)
    if not cfg["title_case_new"]:
        return
    tag = stash.call("query($id: ID!) { findTag(id: $id) { id name } }", {"id": str(tag_id)})["findTag"]
    if not tag:
        return
    new = title_case(tag["name"], cfg["spellings"])
    if new != tag["name"]:
        rename_tag(stash, tag, new)
        log("info", f'New tag "{tag["name"]}" renamed to "{new}"')


# --- auto tag ---------------------------------------------------------------
# Stash's Auto Tag finds performers, studios and tags whose names appear in a
# file's path. Three ways this plugin uses it:
# - new performers, studios and tags: Stash's own Auto Tag job is started for
#   just those (the whole library is searched for their names)
# - "Auto tag this" on a scene, image or gallery (and "Auto tag selected" in
#   lists): the plugin matches that item's path itself, the same way Stash
#   does, because Stash's Auto Tag can only be limited to folders, not files
# - before AI tagging each item gets the same file-name matching first
# The plugin's own gallery-name tags are left out (the gallery-name setting
# handles them).

AUTOTAG_SEP = r"[.\-_ ]*"


def autotag_new(stash, plugin_dir, performer_ids, studio_ids, tag_ids):
    """Queued job: start Stash's Auto Tag for newly created performers,
    studios and tags (minus ones set to be ignored by Auto Tag)."""
    skip_tags = gallery_name_tag_ids(plugin_dir)
    chosen = {}
    for key, query, ids in (("performers", "findPerformers", performer_ids),
                            ("studios", "findStudios", studio_ids), ("tags", "findTags", tag_ids)):
        if not ids:
            continue
        try:
            found = stash.call(
                f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {key} {{ id name ignore_auto_tag }} }} }}",
                {"ids": ids},
            )[query][key]
        except RuntimeError:  # one was deleted again in the meantime
            found = []
            for i in ids:
                try:
                    found += stash.call(
                        f"query($ids: [ID!]) {{ {query}(ids: $ids) {{ {key} {{ id name ignore_auto_tag }} }} }}", {"ids": [i]}
                    )[query][key]
                except RuntimeError:
                    pass
        found = [x for x in found if not x["ignore_auto_tag"] and not (key == "tags" and x["id"] in skip_tags)]
        if found:
            chosen[key] = found
    if not chosen:
        return
    stash.call("mutation($i: AutoTagMetadataInput!) { metadataAutoTag(input: $i) }",
               {"i": {k: [x["id"] for x in v] for k, v in chosen.items()}})
    names = ", ".join(f'{k[:-1] if k != "studios" else "studio"} "{x["name"]}"' for k, v in chosen.items() for x in v)
    log("info", f"Started Stash's Auto Tag for new {names}")


def autotag_pattern(name):
    """Stash's rule: the name, case ignored, with spaces also matching dots,
    dashes, underscores or nothing, not inside a longer word."""
    parts = [re.escape(w) for w in name.lower().split()]
    if not parts:
        return None
    return re.compile(r"(?:^|[\W_])" + AUTOTAG_SEP.join(parts) + r"(?=$|[\W_])")


def autotag_index(stash, plugin_dir):
    """Everything Auto Tag can match: [(kind, id, name, first word, pattern)].
    Performers match by name; studios and tags by name and aliases."""
    data = stash.call(
        "{ findPerformers(filter: {per_page: -1}) { performers { id name ignore_auto_tag } }"
        " findStudios(filter: {per_page: -1}) { studios { id name aliases ignore_auto_tag } }"
        " findTags(filter: {per_page: -1}) { tags { id name aliases ignore_auto_tag } } }"
    )
    skip_tags = gallery_name_tag_ids(plugin_dir)
    index = []
    for kind, items in (("performer", data["findPerformers"]["performers"]),
                        ("studio", data["findStudios"]["studios"]),
                        ("tag", data["findTags"]["tags"])):
        for x in items:
            if x["ignore_auto_tag"] or (kind == "tag" and x["id"] in skip_tags):
                continue
            for n in [x["name"]] + (x.get("aliases") or [] if kind != "performer" else []):
                pat = autotag_pattern(n)
                if pat:
                    index.append((kind, x["id"], x["name"], n.lower().split()[0], pat))
    return index


def item_path(kind, item):
    """The item's file path (a gallery: its zip, else its folder)."""
    if kind == "gallery":
        f = (item.get("files") or [None])[0]
        path = f["path"] if f else ((item.get("folder") or {}).get("path") or "")
    else:
        files = item.get("visual_files") if kind == "image" else item.get("files")
        path = ((files or [None])[0] or {}).get("path") or ""
    return path.replace("\\", "/")


def autotag_path(kind, item):
    """The path Auto Tag looks at, without the file extension."""
    path = item_path(kind, item)
    if path and kind != "gallery" or path.lower().endswith(".zip"):
        path = os.path.splitext(path)[0]
    return path


# --- dates from file names --------------------------------------------------
# Only full dates count (a lone year like "1998_IMG_0004" or a month like
# "01-January" doesn't). Looked for in the file name, then in its folder's.

MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?(?![a-z])"
_SEP = r"[\s\-_.,]+"
DATE_PATTERNS = (
    # 2023-05-19, 2023_05_19, 2023.05.19, 20230519
    ("ymd", re.compile(r"(?<!\d)((?:19|20)\d\d)([-_. ]?)(\d\d)\2(\d\d)(?!\d)")),
    # 6 Nov 2026, 17 may 2026, 6th November, 2026
    ("dmy_name", re.compile(r"(?<![a-z\d])(\d{1,2})(?:st|nd|rd|th)?" + _SEP + _MON + _SEP + r"((?:19|20)\d\d)(?!\d)")),
    # Nov 6 2026, November 6th, 2026
    ("mdy_name", re.compile(r"(?<![a-z])" + _MON + _SEP + r"(\d{1,2})(?:st|nd|rd|th)?" + _SEP + r"((?:19|20)\d\d)(?!\d)")),
    # 05-27-2014 / 27.05.2014 (only when it's clear which is the day)
    ("nny", re.compile(r"(?<!\d)(\d\d)([-_.])(\d\d)\2((?:19|20)\d\d)(?!\d)")),
    # scene releases: Studio.21.07.08.Name
    ("yymmdd", re.compile(r"(?:^|[\s._\-\[(])(\d\d)\.(\d\d)\.(\d\d)(?=$|[\s._\-\])])")),
)


def _valid_date(y, m, d):
    import datetime
    try:
        day = datetime.date(int(y), int(m), int(d))
    except ValueError:
        return None
    if day.year < 1970 or day > datetime.date.today() + datetime.timedelta(days=1):
        return None
    return day.isoformat()


def date_in_text(text):
    """The first full date in a piece of text, as YYYY-MM-DD, else None."""
    text = text.lower()
    found = []
    for kind, pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            g = m.groups()
            if kind == "ymd":
                day = _valid_date(g[0], g[2], g[3])
            elif kind == "dmy_name":
                day = _valid_date(g[2], MONTHS[g[1][:3]], g[0])
            elif kind == "mdy_name":
                day = _valid_date(g[2], MONTHS[g[0][:3]], g[1])
            elif kind == "nny":
                a, b = int(g[0]), int(g[2])
                if a > 12 and b <= 12:
                    day = _valid_date(g[3], b, a)
                elif b > 12 and a <= 12:
                    day = _valid_date(g[3], a, b)
                else:
                    day = None  # 05-06-2014: May 6th or 5th June? can't tell
            else:
                day = _valid_date("20" + g[0], g[1], g[2])
            if day:
                found.append((m.start(), day))
    return min(found)[1] if found else None


def date_from_path(path):
    """A date from the file name, else from the folder it's in."""
    if not path:
        return None
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0] if "." in name else name
    return date_in_text(stem) or date_in_text(os.path.basename(os.path.dirname(path)))


def fill_dates(stash, work, apply, quiet=False):
    """Give items without a date the one in their file name. `work`:
    [(kind, item)]. Returns how many got (or would get) a date."""
    by_date = defaultdict(list)
    for kind, item in work:
        if item.get("date"):
            continue
        day = date_from_path(item_path(kind, item))
        if day:
            by_date[(kind, day)].append(item)
    if apply:
        for (kind, day), items in by_date.items():
            mutation, input_type = AUTOTAG_UPDATE[kind]
            ids = [i["id"] for i in items]
            for i in range(0, len(ids), BULK_BATCH):
                stash.call(f"mutation($i: {input_type}!) {{ {mutation}(input: $i) {{ id }} }}",
                           {"i": {"ids": ids[i:i + BULK_BATCH], "date": day}})
    return sum(len(v) for v in by_date.values()) if quiet else by_date


def run_dates(stash, preview):
    """Task: fill in empty dates on scenes, images and galleries from their
    file names (or `preview`: only log what it would do)."""
    work = []
    for kind in ("scene", "image", "gallery"):
        query, field = {"scene": ("findScenes", "scenes"), "image": ("findImages", "images"),
                        "gallery": ("findGalleries", "galleries")}[kind]
        items = stash.call(f"{{ {query}(filter: {{per_page: -1}}) {{ {field} {{ {AUTOTAG_FIELDS[kind]} }} }} }}")[query][field]
        work += [(kind, i) for i in items]
    empty = sum(1 for _, i in work if not i.get("date"))
    by_date = fill_dates(stash, work, apply=not preview)
    counts = Counter(kind for (kind, _), items in by_date.items() for _ in items)
    total = sum(counts.values())
    examples = [f'{os.path.basename(item_path(k, i))} → {d}' for (k, d), items in sorted(by_date.items(), key=lambda x: x[0][1]) for i in items[:1]]
    for line in examples[:40]:
        log("info", ("Would set " if preview else "Set ") + line)
    if len(examples) > 40:
        log("info", f"... and {len(examples) - 40} more dates")
    parts = ", ".join(f"{n} {k}{'s' if n != 1 and k != 'gallery' else ''}".replace("gallerys", "galleries") for k, n in counts.items())
    verb = "would get" if preview else "got"
    log("info", f"{'Preview: ' if preview else 'Done: '}{total} of {empty} items without a date {verb} one from the file name"
        + (f" ({parts})" if parts else "") + (". Nothing was saved." if preview else "."))


# --- titles from file names ------------------------------------------------
# A cleaned-up file name becomes the title of a scene, image or gallery that
# has none: no [ids] or [sources], dates, resolutions or codecs, performer /
# studio names as their own " - " part, camera or app names (IMG_1234,
# RPReplay..., FullSizeRender) or random ids (UUIDs, hashes, TikTok ids).
# Names that are nothing but junk get no title.

TITLE_GENERIC = {
    "img", "imgs", "image", "images", "vid", "vids", "video", "videos", "movie", "movies", "clip",
    "clips", "photo", "photos", "pic", "pics", "picture", "pictures", "dsc", "dscn", "dscf", "dcim",
    "pxl", "mvimg", "screenshot", "screenshots", "screen", "recording", "rpreplay", "final",
    "fullsizerender", "full", "size", "render", "lp", "moments", "trim", "trimmed", "edit", "edited",
    "copy", "untitled", "new", "file", "snapchat", "snap", "whatsapp", "telegram", "signal",
    "original", "export", "exported", "camera", "cam", "download", "downloaded", "tmp", "temp",
    "output", "sample", "preview", "thumb", "thumbnail", "part", "scene", "hd", "sd", "cm", "chat",
    "media", "album", "albums", "upload", "uploads", "uploaded", "data",
}
# download sites and release groups that end up in file names
TITLE_SITES = re.compile(r"(?i)^-?(?:erome\d*|thothub|viralxxxporn|xleech|hls|xvideos|pornhub|xhamster|"
                         r"spankbang|eporner|redgifs|coomer|kemono|fapello|leakedzone|ytmp3)$")
TITLE_QUALITY = re.compile(
    r"(?i)(?<![a-z0-9])(?:\d{3,4}p|[2458]k|uhd|fhd|hd|sd|hq|x26[45]|h\.?26[45]|hevc|avc|aac|ac3|"
    r"web[- ]?dl|webrip|web|bluray|brrip|hdtv|hdr|\d{2,3}fps|xxx|mp4|mov|m4v|wmv|avi|mkv|"
    r"av1|vp9|internal|repack|proper|remastered)(?![a-z0-9])")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_title_names = None


def title_names(stash):
    """Performer and studio names (and aliases), lowercased with spaces only."""
    global _title_names
    if _title_names is None:
        d = stash.call("{ findPerformers(filter: {per_page: -1}) { performers { name alias_list } }"
                       " findStudios(filter: {per_page: -1}) { studios { name aliases } } }")
        names = set()
        for x in d["findPerformers"]["performers"]:
            names.update([x["name"]] + (x.get("alias_list") or []))
        for x in d["findStudios"]["studios"]:
            names.update([x["name"]] + (x.get("aliases") or []))
        _title_names = {_title_key(n) for n in names if n}
    return _title_names


def _title_key(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _junk_token(tok):
    """Random ids: hex runs, UUID parts, TikTok-style ids, long numbers."""
    t = tok.strip("()[]{}.,;:!?'\"")
    if not t:
        return True
    has_d, has_a = any(c.isdigit() for c in t), any(c.isalpha() for c in t)
    if has_d and has_a and len(t) >= 4 and re.fullmatch(r"[0-9a-fA-F]+", t):
        return True  # hex, e.g. 70773c89..., CD957061
    if len(re.findall(r"[A-Za-z]+|\d+", t)) >= 4:
        return True  # letters and digits mixed up, e.g. v12044gd0000cj7f7
    if len(t) <= 12 and has_a and re.search(r"[a-z][A-Z]", t) and not re.fullmatch(
            r"(?:[A-Z]{0,3}[a-z]+)(?:[A-Z][a-z]+)*\d*", t):
        return True  # short random ids, e.g. hTJlutVr, XplFXgK, fhu3hYE (not CamelCase words)
    if len(t) <= 12 and len(re.findall(r"[A-Za-z]+|\d+", t)) >= 3:
        return True  # e.g. Tx9bqvdb, yjez7nq, QAZ3MVg
    if TITLE_SITES.match(t):
        return True
    return False


def title_from_path(path, names, spellings=None, keep_names=False):
    """A title made from the file (or folder) name, or None if it's junk.
    `keep_names`: leave performer / studio names in it."""
    if not path:
        return None
    name = os.path.basename(path.rstrip("/"))
    stem = os.path.splitext(name)[0] if re.search(r"\.[A-Za-z0-9]{2,4}$", name) else name
    t = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", stem)  # [11166], [OnlyFans]
    t = re.sub(r"(?<=[A-Za-z0-9])_(?=[st](?:[\s_.\-]|$))", "'", t)  # it_s -> it's
    t = re.sub(r"^(?:19|20)\d\d_", "", t)  # iPhone exports: 1998_IMG_0778
    t = re.sub(r"\(\s*\d*\s*\)", " ", t)  # (1)
    t = re.sub(r"\(([^)]*)\)", lambda m: " " if TITLE_QUALITY.fullmatch(m.group(1).strip()) else m.group(0), t)
    release = not re.search(r"\s", t.strip())  # Studio.21.07.08.Scene.Name
    low = t.lower()
    for _, pat in DATE_PATTERNS:  # dates go in the date field
        for m in pat.finditer(low):
            t = t[:m.start()] + " " * (m.end() - m.start()) + t[m.end():]
    if release:
        t = re.sub(r"[._\-]+", " ", t)  # Some.Release_name-like-this
    else:
        t = re.sub(r"_+", " ", t)
        t = re.sub(r"\.{2,}|…", " ", t)
    t = TITLE_QUALITY.sub(" ", t)
    words = []
    for tok in t.split():
        if _junk_token(tok):
            continue
        if len(_CAMEL.findall(tok)) >= 2:  # TiedToTwins -> Tied To Twins
            tok = _CAMEL.sub(" ", tok)
        tok = re.sub(r"(?<=[A-Za-z]{2})(?=\d)|(?<=\d)(?=[A-Za-z]{2})", " ", tok)  # Twins33 -> Twins 33
        words += [w for w in tok.split() if not re.fullmatch(r"#?\d{5,}|[#;:,.]+", w)]
    t = " ".join(words)
    # performer / studio names (and empty bits) as their own " - " part
    parts = [p.strip(" -_.,") for p in re.split(r"\s+[-–|]\s+", t)]
    named = [p for p in parts if p and (keep_names or _title_key(p) not in names)]
    seen_parts = set()
    named = [p for p in named if not (_title_key(p) in seen_parts or seen_parts.add(_title_key(p)))]
    if keep_names:  # a name part that's already in the rest of the title only once
        named = [p for i, p in enumerate(named) if not (
            _title_key(p) in names and any(f" {_title_key(p)} " in f" {_title_key(q)} " for j, q in enumerate(named) if j != i))]
    t = " - ".join(named if named else [p for p in parts if p])
    t = re.sub(r"\s+", " ", t).strip(" -_.,;:|")
    # a performer / studio name at the start, e.g. "Maimy Nyan Spit Roast"
    ws = [] if keep_names else t.split()
    for n in (3, 2, 1):
        if len(ws) > n + 1 and _title_key(" ".join(ws[:n])) in names:
            t = " ".join(ws[n:]).lstrip(" -_.,;:|")
            break
    if _title_key(t) in names:
        return None  # just a performer's or studio's name
    meaningful = [w for w in re.findall(r"[A-Za-z]+", t) if len(w) >= 2 and w.lower() not in TITLE_GENERIC]
    if len(meaningful) < 2 or max(len(w) for w in meaningful) < 3:
        return None
    letters = [c for c in t if c.isalpha()]
    if letters and (all(c.islower() for c in letters) or all(c.isupper() for c in letters)):
        t = title_case(t.lower(), spellings or {})
    return t[:1].upper() + t[1:]


# Titles this plugin set, so "Redo titles" knows which are its own:
# {"scene:12": "Title", ...} in auto_titles.json
_plugin_dir = None


def locked_titles():
    return locked_json(os.path.join(_plugin_dir or os.path.dirname(os.path.abspath(__file__)), "auto_titles.json"))


def set_title(stash, kind, item, title, record=True):
    stash.call(f"mutation($i: {TITLE_INPUT[kind]}!) {{ {TITLE_UPDATE[kind]}(input: $i) {{ id }} }}",
               {"i": {"id": item["id"], "title": title}})
    if not record:  # e.g. AI titles: not from the file name, so "Redo titles" leaves them alone
        return
    try:
        with locked_titles() as rec:
            rec[f"{kind}:{item['id']}"] = title
    except OSError:
        pass


TITLE_UPDATE = {"scene": "sceneUpdate", "image": "imageUpdate", "gallery": "galleryUpdate"}
TITLE_INPUT = {"scene": "SceneUpdateInput", "image": "ImageUpdateInput", "gallery": "GalleryUpdateInput"}


def redo_titles(stash, work, apply, spellings=None):
    """Titles this plugin made from a file name (they match what it makes,
    names kept or left out) are made again with the current settings, e.g.
    after turning "Keep performer and studio names in titles" on or off.
    Titles typed in or scraped don't match, so they're left alone."""
    names = title_names(stash)
    keep = settings(stash)["titles_keep_names"]
    try:
        with open(os.path.join(_plugin_dir or os.path.dirname(os.path.abspath(__file__)), "auto_titles.json")) as f:
            record = json.load(f)
    except (OSError, ValueError):
        record = {}
    done = []
    for kind, item in work:
        old = (item.get("title") or "").strip()
        if not old:
            continue
        path = item_path(kind, item)
        # its own: recorded when set, or (titles set before the record
        # existed, always without names) matching that
        mine = record.get(f"{kind}:{item['id']}") == old or old == title_from_path(path, names, spellings, False)
        new = title_from_path(path, names, spellings, keep)
        if not mine or not new or new == old:
            continue
        if apply:
            set_title(stash, kind, item, new)
        done.append((kind, item, old, new))
    return done


def run_redo_titles(stash, preview):
    spellings = settings(stash)["spellings"]
    work = []
    for kind in ("scene", "image", "gallery"):
        query, field = {"scene": ("findScenes", "scenes"), "image": ("findImages", "images"),
                        "gallery": ("findGalleries", "galleries")}[kind]
        items = stash.call(f"{{ {query}(filter: {{per_page: -1}}) {{ {field} {{ {AUTOTAG_FIELDS[kind]} }} }} }}")[query][field]
        work += [(kind, i) for i in items]
    done = redo_titles(stash, work, apply=not preview, spellings=spellings)
    for kind, item, old, new in done[:60]:
        log("info", f'{"Would change" if preview else "Changed"} "{old}" → "{new}"')
    if len(done) > 60:
        log("info", f"... and {len(done) - 60} more titles")
    log("info", f"{'Preview: ' if preview else 'Done: '}{len(done)} title(s) made from file names "
        + ("would change" if preview else "changed") + " to match the current settings."
        + (" Nothing was saved." if preview else ""))


ARCHIVE_EXT = re.compile(r"\.(zip|cbz|cbr|rar|7z)$", re.I)


def gallery_display_name(g):
    """A gallery's name for image titles: its title, else its zip / folder
    name, without an archive extension or its studio's name."""
    name = (g.get("title") or "").strip()
    if not name:
        path = ((g.get("files") or [{}])[0].get("path") or (g.get("folder") or {}).get("path") or "")
        name = os.path.basename(path.rstrip("/"))
    return without_studio(ARCHIVE_EXT.sub("", name).strip(), g.get("studio")) or None


class GalleryTitles:
    """Titles for images whose file name makes none: "<gallery> #<place>",
    the place being the image's position in the gallery (by path, the way
    Stash orders them). An image in several galleries uses the most specific:
    a zip / folder gallery first, then the smallest."""

    def __init__(self, stash):
        self.stash = stash
        self.galleries = {}  # id -> gallery
        self.order = {}  # gallery id -> {image id: place}

    def of(self, image_ids):
        """{image id: title} for these images (those in a gallery)."""
        if not image_ids:
            return {}
        rows = self.stash.call("query($ids: [ID!]) { findImages(ids: $ids, filter: {per_page: -1}) { images { id galleries { id } } } }",
                               {"ids": list(image_ids)})["findImages"]["images"]
        need = {g["id"] for r in rows for g in r["galleries"]} - set(self.galleries)
        if need:
            for g in self.stash.call("query($ids: [ID!]) { findGalleries(ids: $ids, filter: {per_page: -1}) "
                                     "{ galleries { id title image_count files { path } folder { path } studio { name aliases } } } }",
                                     {"ids": sorted(need, key=int)})["findGalleries"]["galleries"]:
                self.galleries[g["id"]] = g
        out = {}
        for r in rows:
            gs = [self.galleries[g["id"]] for g in r["galleries"] if g["id"] in self.galleries]
            gs = [g for g in gs if gallery_display_name(g)]
            if not gs:
                continue
            g = min(gs, key=lambda g: (not (g.get("files") or g.get("folder")), g.get("image_count") or 0))
            place = self.place(g["id"], r["id"])
            if place:
                out[r["id"]] = f"{gallery_display_name(g)} #{place}"
        return out

    def place(self, gallery_id, image_id):
        if gallery_id not in self.order:
            ids = [i["id"] for i in self.stash.call(
                "query($g: [ID!]) { findImages(image_filter: {galleries: {value: $g, modifier: INCLUDES}}, "
                "filter: {per_page: -1, sort: \"path\", direction: ASC}) { images { id } } }", {"g": [gallery_id]}
            )["findImages"]["images"]]
            self.order[gallery_id] = {i: n + 1 for n, i in enumerate(ids)}
        return self.order[gallery_id].get(image_id)


def fill_titles(stash, work, apply, spellings=None, keep_names=None, use_galleries=None, galleries=None):
    """Give items without a title one made from their file name (an image
    whose file name makes none: from its gallery's name, see GalleryTitles).
    `work`: [(kind, item)]. Returns [(kind, item, title)] (given, or would be)."""
    names = title_names(stash)
    if keep_names is None or use_galleries is None:
        cfg = settings(stash)
        keep_names = cfg["titles_keep_names"] if keep_names is None else keep_names
        use_galleries = not cfg["titles_no_gallery"] if use_galleries is None else use_galleries
    todo = [(kind, item) for kind, item in work if not (item.get("title") or "").strip()]
    from_name = {(kind, item["id"]): title_from_path(item_path(kind, item), names, spellings, keep_names)
                 for kind, item in todo}
    from_gallery = {}
    if use_galleries:
        galleries = galleries or GalleryTitles(stash)
        from_gallery = galleries.of([item["id"] for kind, item in todo if kind == "image" and not from_name[(kind, item["id"])]])
    done = []
    for kind, item in todo:
        title = from_name[(kind, item["id"])] or (from_gallery.get(item["id"]) if kind == "image" else None)
        if not title:
            continue
        if apply:
            set_title(stash, kind, item, title)
        done.append((kind, item, title))
    return done


def run_titles(stash, preview):
    """Task: fill in empty titles on scenes, images and galleries from their
    file names (or `preview`: only log what it would do)."""
    spellings = settings(stash)["spellings"]
    work = []
    for kind in ("scene", "image", "gallery"):
        query, field = {"scene": ("findScenes", "scenes"), "image": ("findImages", "images"),
                        "gallery": ("findGalleries", "galleries")}[kind]
        items = stash.call(f"{{ {query}(filter: {{per_page: -1}}) {{ {field} {{ {AUTOTAG_FIELDS[kind]} }} }} }}")[query][field]
        work += [(kind, i) for i in items]
    empty = [(k, i) for k, i in work if not (i.get("title") or "").strip()]
    done = []
    galleries = GalleryTitles(stash)
    for n in range(0, len(empty), 200):
        done += fill_titles(stash, empty[n:n + 200], apply=not preview, spellings=spellings, galleries=galleries)
        progress(min(n + 200, len(empty)) / max(len(empty), 1))
    for kind, item, title in done[:60]:
        log("info", f'{"Would set" if preview else "Set"} {os.path.basename(item_path(kind, item))} → "{title}"')
    if len(done) > 60:
        log("info", f"... and {len(done) - 60} more titles")
    counts = Counter(k for k, _, _ in done)
    parts = ", ".join(f"{n} {k}{'s' if n != 1 and k != 'gallery' else ''}".replace("gallerys", "galleries")
                      for k, n in counts.items())
    verb = "would get" if preview else "got"
    log("info", f"{'Preview: ' if preview else 'Done: '}{len(done)} of {len(empty)} items without a title {verb} one "
        f"from the file name" + (f" ({parts})" if parts else "")
        + f"; {len(empty) - len(done)} had only a camera / app name or an id (and no gallery) and were left alone"
        + (". Nothing was saved." if preview else "."))


def autotag_match(index, path):
    """(performer ids, studio id or None, tag ids, {id: name}) found in a path."""
    low = path.lower()
    performers, tags, names = set(), set(), {}
    studio, studio_at = None, -1
    for kind, xid, name, first, pat in index:
        if first not in low:
            continue
        found = [m.start() for m in pat.finditer(low)]
        if not found:
            continue
        names[xid if kind != "studio" else "s" + xid] = name
        if kind == "performer":
            performers.add(xid)
        elif kind == "tag":
            tags.add(xid)
        elif found[-1] > studio_at:  # several studios: the one deepest in the path
            studio, studio_at = xid, found[-1]
    return performers, studio, tags, names


AUTOTAG_FIELDS = {
    "scene": "id title date performers { id } studio { id } tags { id } files { path }",
    "image": "id title date performers { id } studio { id } tags { id } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
    "gallery": "id title date performers { id } studio { id } tags { id } files { path } folder { path }",
}
AUTOTAG_UPDATE = {
    "scene": ("bulkSceneUpdate", "BulkSceneUpdateInput"),
    "image": ("bulkImageUpdate", "BulkImageUpdateInput"),
    "gallery": ("bulkGalleryUpdate", "BulkGalleryUpdateInput"),
}


def autotag_item(stash, index, kind, item, apply):
    """Match one item's path; add what it doesn't have yet (when `apply`),
    and fill in an empty date from the file name.
    Returns (names added, ids of the tags added)."""
    path = autotag_path(kind, item)
    if not path:
        return [], set()
    performers, studio, tags, names = autotag_match(index, path)
    performers -= {p["id"] for p in item.get("performers") or []}
    tags -= {t["id"] for t in item.get("tags") or []}
    if item.get("studio"):
        studio = None  # Auto Tag never replaces a studio
    day = None if item.get("date") else date_from_path(item_path(kind, item))
    title = None if (item.get("title") or "").strip() else title_from_path(
        item_path(kind, item), title_names(stash), keep_names=settings(stash)["titles_keep_names"])
    if not (performers or studio or tags or day or title):
        return [], set()
    if apply and title:
        set_title(stash, kind, item, title)
    if apply and (performers or studio or tags or day):
        inp = {"ids": [item["id"]]}
        if day:
            inp["date"] = day
        if performers:
            inp["performer_ids"] = {"ids": sorted(performers), "mode": "ADD"}
        if tags:
            inp["tag_ids"] = {"ids": sorted(tags), "mode": "ADD"}
        if studio:
            inp["studio_id"] = studio
        mutation, input_type = AUTOTAG_UPDATE[kind]
        stash.call(f"mutation($i: {input_type}!) {{ {mutation}(input: $i) {{ id }} }}", {"i": inp})
    added = [names[p] for p in sorted(performers, key=int)]
    added += [names["s" + studio]] if studio else []
    added += [names[t] for t in sorted(tags, key=int)]
    added += [f"date {day}"] if day else []
    added += [f'title "{title}"'] if title else []
    return added, tags


def autotag_load(stash, kind, ids):
    query, field = {"scene": ("findScenes", "scenes"), "image": ("findImages", "images"),
                    "gallery": ("findGalleries", "galleries")}[kind]
    out = []
    for i in range(0, len(ids), PAGE_SIZE):
        out += stash.call(
            f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ {AUTOTAG_FIELDS[kind]} }} }} }}",
            {"ids": ids[i:i + PAGE_SIZE]},
        )[query][field]
    return out


def run_autotag_items(stash, plugin_dir, kind, ids, with_images=False):
    """"Auto tag this" / "Auto tag selected". For a gallery, `with_images`
    also does the images in it. Returns {items, changed, added}."""
    index = autotag_index(stash, plugin_dir)
    work = [(kind, x) for x in autotag_load(stash, kind, ids)]
    if kind == "gallery" and with_images:
        image_ids = item_ids(stash, "images", {"galleries": {"value": ids, "modifier": "INCLUDES"}})
        work += [("image", x) for x in autotag_load(stash, "image", image_ids)]
    changed, added = 0, Counter()
    for n, (k, item) in enumerate(work):
        names, _ = autotag_item(stash, index, k, item, apply=True)
        if names:
            changed += 1
            # many items: count dates together instead of listing each one
            added.update(n if len(work) == 1 or not n.startswith(("date ", "title ")) else
                         ("a date" if n.startswith("date ") else "a title") for n in names)
        progress((n + 1) / max(len(work), 1))
    summary = ", ".join(f"{name} ({c})" if c > 1 else name for name, c in added.most_common())
    if changed:
        log("info", f"Auto tag: added {summary} on {changed} of {len(work)} item(s)")
    else:
        log("info", f"Auto tag: nothing new found in the file names of {len(work)} item(s)")
    return {"items": len(work), "changed": changed, "added": [n for n, _ in added.most_common()]}


# --- AI tagging (local, via Ollama) -----------------------------------------
# Shows a vision model running in Ollama (on this computer or another one on
# the network) a few frames of a scene, or an image, together with a list of
# your most used tags, and adds the tags it says clearly apply. Nothing
# leaves your network. The model can only answer with tags from the list
# (Ollama's structured output enforces it), so it never invents tags.
# ai_done.json remembers which scenes/images were done, so runs can be
# stopped and picked up again.

AI_FRAME_POINTS = (0.25, 0.5, 0.75)  # scene frames shown to the model
AI_SYSTEM = (
    "You tag photos and video frames for a private media library.\n"
    "Rules:\n"
    "- Only choose tags that clearly describe what is visible in the pictures. "
    "Do not guess; leave a tag out if you are not sure.\n"
    "- Never choose two tags that contradict each other (for example two different hair "
    "colours, or two different sizes of the same thing); pick the one that fits best.\n"
    "- Fewer, accurate tags are better than many.\n"
    "- Notice whether it's a photo or a drawing, cartoon, 3D render or animation, and use the "
    "matching tag if there is one.\n"
    "- Only use tags from the allowed list, spelled exactly as listed."
)


def num_pct(v, default):
    """A percentage setting as a fraction (e.g. 36 -> 0.36)."""
    try:
        n = float(v)
        return n / 100 if 1 <= n <= 100 else default / 100
    except (TypeError, ValueError):
        return default / 100


def ai_settings(s):
    def num(key, default, lo):
        try:
            return max(int(s.get(key) or default), lo)
        except (TypeError, ValueError):
            return default
    return {
        "url": (s.get("aiOllamaUrl") or "http://host.docker.internal:11434").rstrip("/"),
        "model": (s.get("aiModel") or "gemma3:4b").strip(),
        "vocab_size": num("aiTagCount", 250, 10),
        "max_tags": num("aiMaxTags", 6, 1),
        "include_organized": bool(s.get("aiIncludeOrganized")),
        # edit page "Tags from look-alikes": shown from this share (you pick
        # what to add, so it can be looser than what the tasks add)
        "look_suggest": num_pct(s.get("aiLookSuggest"), 20),
        # items with more tags than this are already well tagged: no AI model
        # step for them (body tags still run). 0 = never skip.
        "skip_tagged": max(int(s.get("aiSkipTagged")), 0) if str(s.get("aiSkipTagged") or "").strip().lstrip("-").isdigit() else 5,
        "auto": bool(s.get("aiAuto")),
        "new_all": bool(s.get("aiNewAll")),
        # new photos: studio from the watermark the AI reads (like scenes)
        "photo_studios": bool(s.get("aiNewPhotoStudios")),
        # which items the AI model tags / titles: scenes, images or both
        "tag_kinds": item_kinds(s.get("aiTagKinds")),
        "title_kinds": item_kinds(s.get("aiTitleKinds")),
        "autotag_first": not s.get("aiNoAutoTag"),
        "face_url": (s.get("faceHelperUrl") or "http://host.docker.internal:11535").rstrip("/"),
        "face_auto": bool(s.get("faceAutoAssign")),
        # cosine likeness of two face fingerprints (SFace): about 0.36 and up
        # is usually the same person
        "face_min": num_pct(s.get("faceSuggestMin"), 36),
        "face_auto_min": num_pct(s.get("faceAutoMin"), 50),
        # new items: quick steps right away, AI model steps once at night
        "nightly": bool(s.get("aiNightly")),
        # the body tags task (items not checked yet) as part of the night run
        "night_body": bool(s.get("aiNightBody")),
        "night_time": _clock(s.get("aiNightTime"), "03:00"),
        "time_zone": (s.get("aiTimeZone") or os.environ.get("TZ") or "UTC").strip(),
    }


def _clock(value, default):
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(value or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return default
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def ai_vocabulary(stash, cfg, plugin_dir):
    """The tags the model may choose from: your most used ones (minus
    ignored and gallery-name tags). {lowercase name: tag id}, plus the
    names in order."""
    tags = stash.call(
        f"{{ findTags(filter: {{per_page: -1}}) {{ tags {{ id name {TAG_USES_FIELDS} child_count }} }} }}"
    )["findTags"]["tags"]
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    usable = [t for t in tags if t["id"] not in excluded and uses(t) > 0]
    usable.sort(key=lambda t: -uses(t))
    chosen = usable[: cfg["ai"]["vocab_size"]]
    return {t["name"].lower(): t["id"] for t in chosen}, [t["name"] for t in chosen]


# (Tried 2026-11-11 with gemma3:4b: checking the AI's picks against the tags'
# descriptions in a second model call didn't make them more accurate - it
# kept nearly everything, or when made strict dropped right and wrong picks
# alike - and matching new-tag ideas to descriptions found little and could
# mislead. So the descriptions aren't used by the AI.)


def ollama_tags(cfg, pictures, names):
    """Ask the model which of `names` apply to the pictures (JPEG bytes).
    The rules and the tag list go first and are the same every time, so
    Ollama reads them once and reuses that for every item (reading the list
    is most of the work); only the pictures are new each time."""
    ai = cfg["ai"]
    system = AI_SYSTEM + "\n\nAllowed tags: " + "; ".join(names)
    prompt = (
        f"Which of the allowed tags clearly apply to {what_pictures(pictures)}? "
        f"Choose at most {ai['max_tags']}, best first. If none clearly apply, return an empty list."
    )
    body = {
        "model": ai["model"],
        "stream": False,
        "keep_alive": "15m",
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt, "images": [base64.b64encode(p).decode() for p in pictures]},
        ],
        # structured output: an object whose "tags" are only allowed names
        "format": {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string", "enum": names}}},
            "required": ["tags"],
        },
    }
    req = urllib.request.Request(ai["url"] + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(resp.read())
    content = (reply.get("message") or {}).get("content") or "{}"
    try:
        picked = json.loads(content).get("tags") or []
    except ValueError:
        picked = []
    return [p for p in picked if isinstance(p, str)]


def ai_pictures(stash, kind, item):
    """JPEG bytes to show the model: an image's thumbnail, or a few frames
    of a scene."""
    if kind == "image":
        if not all(f["__typename"] == "ImageFile" for f in item.get("visual_files") or []):
            return []  # videos / animations stored as images
        data, ctype = stash.fetch(item["paths"]["thumbnail"])
        return [data] if ctype.startswith("image/") else []
    f = (item.get("files") or [None])[0]
    duration = (f or {}).get("duration") or 0
    if not f or duration <= 0.2:
        return []
    # one picture of 4 frames: the model reads one picture instead of three,
    # about twice as fast (on the user's library: 11.3s -> 5.7s a scene)
    if duration > 4:
        with contextlib.suppress(Exception):
            return [grab_grid(stash, f["path"], duration)]
    # short clips (phone Live Photos are 1-3 s) and videos the grid can't be
    # made from: whichever frames can be had. Phone clips are often a little
    # shorter than they say, so a frame near the end can fail - that mustn't
    # lose the others.
    out = []
    for p in (AI_FRAME_POINTS if duration > 4 else (0.5,)) + (0.0,):
        if out and p == 0.0:
            break
        with contextlib.suppress(Exception):
            out.append(grab_frame(stash, f["path"], duration * p)[0])
    return out


GRID_POINTS = (0.2, 0.4, 0.6, 0.8)  # frames in the 2x2 grid shown to the model
GRID_TILE = 448  # each frame fits a square this big (the grid: 896, what gemma3 reads)


class GridPicture(bytes):
    """A JPEG that's several frames of one video in a grid (so prompts can
    say so)."""


def grab_grid(stash, video, duration, points=GRID_POINTS, tile=GRID_TILE):
    """Frames at `points` (fractions of `duration`) side by side in a 2x2
    grid, each fitted into a tile x tile square: one JPEG, one ffmpeg run."""
    cmd = [ffmpeg_path(stash), "-loglevel", "error"]
    for p in points:
        cmd += ["-ss", f"{duration * p:.2f}", "-i", video]
    fit = (f"scale={tile}:{tile}:force_original_aspect_ratio=decrease,"
           f"pad={tile}:{tile}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuvj420p")
    graph = "".join(f"[{i}:v]{fit}[t{i}];" for i in range(len(points)))
    graph += "".join(f"[t{i}]" for i in range(len(points))) + f"xstack=inputs={len(points)}:layout=0_0|w0_0|0_h0|w0_h0[out]"
    cmd += ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "3",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip() or "no grid")
    return GridPicture(proc.stdout)


def what_pictures(pictures):
    """How to describe the pictures to the model."""
    if len(pictures) == 1 and isinstance(pictures[0], GridPicture):
        return "this picture, which shows 4 frames from one video in a 2x2 grid"
    return "these frames from one video" if len(pictures) > 1 else "this picture"


AI_ITEM_FIELDS = {
    "image": "id title date organized tags { id name } performers { id } studio { id } paths { thumbnail image } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
    "scene": "id title date organized tags { id name } performers { id } studio { id } files { path duration } "
             "paths { screenshot sprite vtt }",
}


def combine_picks(ai_picked, scores, have, looks, limit):
    """The AI's picks checked against the look-alikes: [(tag id, name,
    "both" | "looks" | "ai")]. With look-alike scores, an AI pick is kept
    only if the look-alikes back it a little (the AI alone is often wrong
    about your tags), and tags the look-alikes are sure of are added. Without
    them (not scanned / no helper), the AI's picks as they are."""
    if scores is None:
        return [(t, n, "ai") for t, n in ai_picked][:limit]
    out = [(t, n, "both") for t, n in ai_picked if scores.get(t, 0) >= LOOK_SUPPORT and t not in looks.rejected]
    seen = {t for t, _, _ in out} | set(have)
    for t in look_picks(scores, seen, looks.names, limit):
        out.append((t, looks.names[t], "looks"))
    out.sort(key=lambda x: -scores.get(x[0], 0))
    return out[:limit]


def ai_tag_item(stash, cfg, vocab, names, kind, item, apply, have_too=(), looks=None, plugin_dir=None, details=False):
    """Returns the names of the tags picked that the item doesn't have yet
    (added when `apply`). `have_too`: tag ids just added by Auto Tag.
    `looks`: a LookIndex to check the AI against (see combine_picks).
    `details`: return [(id, name, source)] instead of names."""
    pictures = ai_pictures(stash, kind, item)
    if not pictures:
        return None
    have = {t["id"] for t in item.get("tags") or []} | set(have_too)
    picked, seen = [], set()
    for name in ollama_tags(cfg, pictures, names):
        tid = vocab.get(name.lower())
        if tid and tid not in have and tid not in seen:
            seen.add(tid)
            picked.append((tid, name))
    scores = None
    if looks:
        try:
            scores = looks.for_item(stash, kind, item)
        except RuntimeError:
            raise
        except Exception as e:
            log("debug", f"{kind} {item['id']}: look-alikes skipped ({e})")
    full = combine_picks(picked, scores, have, looks, cfg["ai"]["max_tags"])
    picked = [(t, n) for t, n, _ in full]
    if apply and picked:
        mutation = "bulkImageUpdate(input: $i) { id }" if kind == "image" else "bulkSceneUpdate(input: $i) { id }"
        input_type = "BulkImageUpdateInput" if kind == "image" else "BulkSceneUpdateInput"
        stash.call(f"mutation($i: {input_type}!) {{ {mutation} }}",
                   {"i": {"ids": [item["id"]], "tag_ids": {"ids": [t for t, _ in picked], "mode": "ADD"}}})
        if plugin_dir:
            record_machine_tags(plugin_dir, kind, item["id"], [t for t, _ in picked])
    return full if details else [n for _, n in picked]


def locked_ai_done(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "ai_done.json"))


def ai_check(cfg):
    """Fail early with a clear message if Ollama or the model isn't there."""
    ai = cfg["ai"]
    try:
        with urllib.request.urlopen(ai["url"] + "/api/tags", timeout=10) as resp:
            models = [m.get("name") for m in json.loads(resp.read()).get("models", [])]
    except Exception as e:
        raise RuntimeError(f"can't reach Ollama at {ai['url']} ({e}). Is it running?")
    want = ai["model"] if ":" in ai["model"] else ai["model"] + ":latest"
    if want not in models:
        raise RuntimeError(f'Ollama doesn\'t have the model "{ai["model"]}" - run: ollama pull {ai["model"]}')


def ai_targets(stash, cfg, only_untagged, done):
    """(kind, item) pairs still to do."""
    out = []
    for kind, (query, field, farg, ftype) in (("scene", ("findScenes", "scenes", "scene_filter", "SceneFilterType")),
                                             ("image", ("findImages", "images", "image_filter", "ImageFilterType"))):
        f = {}
        if not cfg["ai"]["include_organized"]:
            f["organized"] = False
        if only_untagged:
            f["tag_count"] = {"value": 0, "modifier": "EQUALS"}
        page = 1
        skip = set(done.get(kind + "s", []))
        while True:
            items = stash.call(
                f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ {AI_ITEM_FIELDS[kind]} }} }} }}",
                {"p": f, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}},
            )[query][field]
            out += [(kind, i) for i in items if i["id"] not in skip]
            if len(items) < 500:
                break
            page += 1
    return out


def item_kinds(value):
    """A "scenes" / "images" / "both" setting as ["scene", "image"] (both
    when empty or not understood)."""
    v = str(value or "").strip().lower()
    if v.startswith("scene") or v in ("videos", "video"):
        return ["scene"]
    if v.startswith("image") or v.startswith("photo") or v in ("pictures", "picture"):
        return ["image"]
    return ["scene", "image"]


def kinds_note(kinds, setting):
    return "" if len(kinds) == 2 else f" (only {kinds[0]}s - setting \"{setting}\")"


def run_ai(stash, plugin_dir, only_untagged=False, preview=False, targets=None):
    """Task: AI-tag scenes and images. `preview`: 10 random items, nothing
    saved. `targets`: specific (kind, item) pairs (the automatic job)."""
    cfg = settings(stash)
    ai_check(cfg)
    vocab, names = ai_vocabulary(stash, cfg, plugin_dir)
    if not names:
        log("warning", "No tags to choose from yet.")
        return
    if targets is None:
        with locked_ai_done(plugin_dir) as done:
            done = dict(done)
        targets = ai_targets(stash, cfg, only_untagged, {} if preview else done)
        targets = [t for t in targets if t[0] in cfg["ai"]["tag_kinds"]]
        if preview:
            import random
            targets = random.sample(targets, min(10, len(targets)))
    what = "Preview" if preview else "AI tagging"
    log("info", f"{what}: {len(targets)} item(s){kinds_note(cfg['ai']['tag_kinds'], 'AI tagging: which items')}, "
        f"choosing from your {len(names)} most used tags with {cfg['ai']['model']}")
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded)
    body = BodyIndex(stash, plugin_dir, body_excluded(stash, cfg)) if body_ready(cfg) else None
    if body:
        log("info", "Adding body tags too (what's shown, and sizes from the performers)")
    if looks:
        log("info", f"Checking the AI's picks against look-alikes among {len(looks.rows)} item(s) you've tagged")
        if looks.rejected:
            log("info", "Not added any more (you removed them from most items they were added to): "
                + ", ".join(f"{looks.names.get(t, t)} (kept {k} of {n})" for t, (n, k) in looks.rejected.items()))
    index = autotag_index(stash, plugin_dir) if cfg["ai"]["autotag_first"] else None
    added = tagged = from_names = well_tagged = 0
    limit = cfg["ai"]["skip_tagged"]
    if limit:
        log("info", f"Skipping the AI model for items with more than {limit} tags already (setting \"AI: skip items with more tags than\")")
    started = time.time()
    for n, (kind, item) in enumerate(targets):
        label = f'{kind} {item["id"]}' + (f' "{item["title"]}"' if item.get("title") else "")
        pre_tags = set()
        if index is not None:
            try:
                pre, pre_tags = autotag_item(stash, index, kind, item, apply=not preview)
            except Exception as e:
                log("warning", f"{label}: auto tag failed: {e}")
                pre = []
            if pre:
                from_names += 1
                log("info", f"{label}: from the file name {'would add' if preview else '+'} {', '.join(pre)}")
        have = ({t["id"] for t in item.get("tags") or []} | set(pre_tags)) - gallery_only_tag_ids(stash, plugin_dir)
        try:
            if limit and len(have) > limit:
                well_tagged += 1
                picked = []  # already well tagged: no AI model step (body tags below still run)
            else:
                picked = ai_tag_item(stash, cfg, vocab, names, kind, item, apply=not preview, have_too=pre_tags,
                                     looks=looks, plugin_dir=plugin_dir)
        except Exception as e:
            log("warning", f"{label}: {e}")
            if "urlopen" in repr(e) or "Connection" in repr(e):
                raise RuntimeError(f"lost contact with Ollama: {e}")
            continue
        if body:
            try:
                extra = body_item(stash, cfg, body, kind, item, apply=not preview, plugin_dir=plugin_dir,
                                  rejected=looks.rejected if looks else (), skip_tagged=True)
                extra = [n for _, n, _ in extra if n not in (picked or [])]
                if extra:
                    picked = (picked or []) + extra
            except RuntimeError:
                raise
            except Exception as e:
                log("debug", f"{label}: body tags skipped ({e})")
        if picked is None:
            log("debug", f"{label}: nothing to look at, skipped")
        elif picked:
            tagged += 1
            added += len(picked)
            log("info", f"{label}: {'would add' if preview else '+'} {', '.join(picked)}")
        else:
            log("debug", f"{label}: no new tags")
        if not preview and picked is not None:
            with locked_ai_done(plugin_dir) as done:
                done.setdefault(kind + "s", []).append(item["id"])
        progress((n + 1) / max(len(targets), 1))
    if looks:
        looks.cache.save()
    took = time.time() - started
    per = f" ({took / max(len(targets), 1):.1f}s each)" if targets else ""
    names_note = f" Auto tag (file names) {'would add' if preview else 'added'} to {from_names} item(s)." if index is not None else ""
    if well_tagged:
        names_note += f" {well_tagged} item(s) already had more than {limit} tags, so the AI model skipped them."
    if preview:
        log("info", f"Preview done{per}: {tagged} of {len(targets)} items would get new AI tags.{names_note} Nothing was saved.")
    else:
        log("info", f"Done{per}: AI added {added} tag(s) to {tagged} of {len(targets)} item(s).{names_note}")


def run_ai_queued(stash, plugin_dir, scene_ids, image_ids):
    """Background job: AI-tag scenes/images that were just added (the kinds
    "AI tagging: which items" says)."""
    kinds = settings(stash, plugin_dir)["ai"]["tag_kinds"]
    targets = []
    for kind, ids in (("scene", scene_ids), ("image", image_ids)):
        if not ids or kind not in kinds:
            continue
        query, field = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
        items = find_by_ids(stash, query, field, ids, f"{AI_ITEM_FIELDS[kind]}")
        targets += [(kind, i) for i in items]
    if targets:
        run_ai(stash, plugin_dir, targets=targets)


NEW_IDEAS_MAX = 6
# words that say nothing on their own, and ones only padding the end of an
# idea ("Selfie Style" -> "Selfie")
NEW_IDEA_EMPTY = {"setting", "style", "scene", "shot", "picture", "photo", "image", "video", "frame", "background",
                  "foreground", "artwork", "art", "detail", "details", "detailed", "figure", "figures", "person",
                  "people", "pose", "posing", "relaxed", "view", "angle", "content", "clothing", "outfit"}
NEW_IDEA_TAIL = {"style", "setting", "shot", "scene", "view", "pose", "look"}


def ollama_new_tags(cfg, pictures):
    """Short tags for what's clearly visible, in the model's own words (no
    list to choose from). Used on the edit page only, for tags you may not
    have yet; the caller drops the ones you already have."""
    ai = cfg["ai"]
    prompt = (
        f"Describe {what_pictures(pictures)} with up to {NEW_IDEAS_MAX + 4} short tags of 1 to 3 words each: "
        "things that are clearly visible, such as the setting, clothing, objects, actions, positions and the style "
        "of the picture. Use plain common words. No names of people or places, no text or watermarks you can read, "
        "no numbers, and nothing you are unsure about. If nothing is clear, return an empty list."
    )
    body = {
        "model": ai["model"], "stream": False, "keep_alive": "15m", "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(p).decode() for p in pictures]}],
        "format": {"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": ["tags"]},
    }
    req = urllib.request.Request(ai["url"] + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(resp.read())
    try:
        tags = json.loads((reply.get("message") or {}).get("content") or "{}").get("tags") or []
    except ValueError:
        tags = []
    return [t for t in tags if isinstance(t, str)]


def new_tag_ideas(stash, cfg, kind, item):
    """The model's own tags for the item that aren't one of your tags (by
    name or alias, ignoring case, hyphens and a plural s): [names], Title
    Cased."""
    pictures = ai_pictures(stash, kind, item)
    if not pictures:
        return []
    known = set()
    for t in stash.call("{ findTags(filter: {per_page: -1}) { tags { name aliases } } }")["findTags"]["tags"]:
        for n in [t["name"], *(t.get("aliases") or [])]:
            n = re.sub(r"[-_]+", " ", n.strip().lower())
            known |= {n, n.rstrip("s"), n + "s"}
    out = []
    # the model sometimes packs several into one ("Microphone, Studio")
    pieces = [x for raw in ollama_new_tags(cfg, pictures) for x in re.split(r"\s*(?:[,;/|]|\band\b|&)\s*", raw)]
    for piece in pieces:
        words = re.sub(r"\s+", " ", piece).strip(" .,-#\"'").split(" ")
        while len(words) > 1 and words[-1].lower() in NEW_IDEA_TAIL:
            words.pop()
        name = " ".join(words).strip()
        low = re.sub(r"[-_]+", " ", name.lower())
        if (not name or len(name) > 30 or len(words) > 3 or re.search(r"\d", name)
                or all(w.lower() in NEW_IDEA_EMPTY for w in words)
                or low in known or low.rstrip("s") in known or low in [o.lower() for o in out]):
            continue
        out.append(title_case(name, cfg["spellings"]))
        if len(out) >= NEW_IDEAS_MAX:
            break
    return out


def run_ai_suggest(stash, plugin_dir, kind, item_id):
    """Asked by the page (runPluginOperation): AI suggestions for one scene
    or image, nothing saved. Returns [{id, name}]; tags you don't have yet
    come last as {id: None, name, source: "new"} (edit page only - the
    tasks never make new tags)."""
    cfg = settings(stash)
    ai_check(cfg)
    vocab, names = ai_vocabulary(stash, cfg, plugin_dir)
    query, field = ("findScene", "scene") if kind == "scene" else ("findImage", "image")
    item = stash.call(f"query($id: ID!) {{ {query}(id: $id) {{ {AI_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[query]
    if not item:
        raise RuntimeError(f"{kind} {item_id} not found")
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded, quiet=True)
    picked = ai_tag_item(stash, cfg, vocab, names, kind, item, apply=False, looks=looks, details=True) or []
    if looks:
        looks.cache.save()
    out = [{"id": t, "name": n, "source": src} for t, n, src in picked]
    if body_ready(cfg):
        with contextlib.suppress(Exception):
            body = BodyIndex(stash, plugin_dir, body_excluded(stash, cfg))
            for t, n, why in body_item(stash, cfg, body, kind, item, apply=False):
                if t not in [o["id"] for o in out]:
                    out.append({"id": t, "name": n, "source": "body", "why": why})
    try:
        out += [{"id": None, "name": n, "source": "new"} for n in new_tag_ideas(stash, cfg, kind, item)]
    except Exception as e:
        log("debug", f"new tag ideas skipped: {e}")
    return out


# --- tags from look-alikes ---------------------------------------------------
# Every scene (its cover and 4 frames from its preview sprite) and image gets
# a picture fingerprint (CLIP, from the face helper), kept in
# look_items.json. An item's tags are then guessed from the 20 most alike
# items you've tagged: a tag scores by how many of them have it, the closest
# counting most (score 0-1). So it learns from your own tagging - tags the AI
# model has never heard of included - and from what you change later.
#
# Tags the add-on added itself (machine_tags.json) aren't learned from, so
# its own guesses don't reinforce themselves; and a tag you've removed from
# most of the items it was added to isn't added any more ("corrections").

LOOK_TILE_POINTS = (0.2, 0.4, 0.6, 0.8)
LOOK_K = 20
LOOK_TEMP = 0.02
LOOK_SUGGEST = 0.3   # shown as a suggestion
LOOK_SURE = 0.5      # added by the tasks; AI picks need LOOK_SUPPORT
LOOK_SUPPORT = 0.15  # an AI pick is kept if the look-alikes give it this much
LOOK_MIN_ITEMS = 100  # tagged, fingerprinted items needed before it's used
REJECT_MIN = 4       # a tag added this often ...
REJECT_KEPT = 0.5    # ... and kept on fewer than half: not added any more

LOOK_ITEM_FIELDS = {
    "scene": "id title organized tags { id name } files { path duration } paths { screenshot sprite vtt }",
    "image": "id title organized tags { id name } paths { thumbnail } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
}


def picture_helper(cfg, pictures, boxes=None):
    """Picture fingerprints from the helper: one per picture, or with
    `boxes`, a list per picture (one per box cut out of it)."""
    body = {"images": [base64.b64encode(p).decode() for p in pictures]}
    if boxes:
        body["boxes"] = boxes
    data = json.dumps(body).encode()
    for attempt in range(4):
        req = urllib.request.Request(cfg["ai"]["face_url"] + "/embed", data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["results"]
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = json.loads(e.read()).get("error", "")
            if "isn't installed" in detail:
                raise RuntimeError("the face helper doesn't have the picture model yet: run face-helper/setup.sh again")
            if e.code == 404:
                raise RuntimeError("the face helper is out of date: run face-helper/setup.sh again")
            raise ValueError(f"the helper couldn't read it ({e.code} {detail})")
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (BrokenPipeError, ConnectionResetError)):
                raise ValueError(f"the helper dropped it ({reason})")
            if attempt < 3:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({reason}). "
                               "Is it running? See the add-on's README.")


def look_fingerprint(stash, cfg, kind, item):
    """One picture fingerprint for the item (the average over a scene's
    pictures), or None if there's nothing to look at."""
    pictures, boxes = [], []
    if kind == "image":
        if not all(f["__typename"] == "ImageFile" for f in item.get("visual_files") or []):
            return None
        data, ctype = stash.fetch(item["paths"]["thumbnail"])
        if ctype.startswith("image/"):
            pictures.append(data)
            boxes.append(None)
    else:
        paths = item.get("paths") or {}
        with contextlib.suppress(Exception):
            data, ctype = stash.fetch(paths["screenshot"])
            if ctype.startswith("image/"):
                pictures.append(data)
                boxes.append(None)
        tiles = []
        with contextlib.suppress(Exception):  # the preview sprite: frames all through it
            vtt = stash.fetch(paths["vtt"])[0].decode("utf-8", "replace")
            spots = [[int(v) for v in m] for m in re.findall(r"#xywh=(\d+),(\d+),(\d+),(\d+)", vtt)]
            if spots:
                sprite, ctype = stash.fetch(paths["sprite"])
                if ctype.startswith("image/"):
                    tiles = [spots[min(int(len(spots) * p), len(spots) - 1)] for p in LOOK_TILE_POINTS]
                    pictures.append(sprite)
                    boxes.append(tiles)
        if not tiles:  # no sprite generated: take frames from the video
            f = (item.get("files") or [None])[0]
            if f and (f.get("duration") or 0) > 1:
                for p in LOOK_TILE_POINTS:
                    with contextlib.suppress(Exception):
                        pictures.append(grab_frame(stash, f["path"], f["duration"] * p, width=448)[0])
                        boxes.append(None)
    if not pictures:
        return None
    embs = []
    for r in picture_helper(cfg, pictures, boxes if any(boxes) else None):
        if r and isinstance(r[0], list):
            embs += r
        elif r:
            embs.append(r)
    if not embs:
        return None
    mean = [sum(col) / len(embs) for col in zip(*embs)]
    norm = math.sqrt(sum(v * v for v in mean)) or 1.0
    return [v / norm for v in mean]


def _ints(s):
    return [b - 256 if b > 127 else b for b in base64.b64decode(s)]


class LookCache:
    """look_items.json: each item's picture fingerprint (or None when there
    was nothing to look at), merged back in batches."""

    def __init__(self, plugin_dir):
        self.path = os.path.join(plugin_dir, "look_items.json")
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.new = {}

    def has(self, kind, item):
        hit = self.new.get(f"{kind}:{item['id']}") or self.data.get(f"{kind}:{item['id']}")
        return bool(hit) and hit.get("p") == item_path(kind, item)

    def vector(self, stash, cfg, kind, item, compute=True):
        """The fingerprint as whole numbers (-127..127), or None."""
        key, sig = f"{kind}:{item['id']}", item_path(kind, item)
        hit = self.new.get(key) or self.data.get(key)
        if hit and hit.get("p") == sig:
            return _ints(hit["e"]) if hit.get("e") else None
        if not compute:
            return None
        emb = look_fingerprint(stash, cfg, kind, item)
        self.new[key] = {"p": sig, "e": q8(emb) if emb else None}
        if len(self.new) >= 50:
            self.save()
        return _ints(self.new[key]["e"]) if emb else None

    def save(self):
        if not self.new:
            return
        with locked_json(self.path) as data:
            data.update(self.new)
            self.data = dict(data)
        self.new = {}


def locked_machine_tags(plugin_dir):
    """Which tags the add-on added to which item (machine_tags.json)."""
    return locked_json(os.path.join(plugin_dir, "machine_tags.json"))


def record_machine_tags(plugin_dir, kind, item_id, tag_ids):
    if tag_ids:
        with locked_machine_tags(plugin_dir) as rec:
            key = f"{kind}:{item_id}"
            rec[key] = sorted(set(rec.get(key, [])) | {str(t) for t in tag_ids}, key=int)


def all_item_tags(stash):
    """{"scene:ID" / "image:ID": {tag ids}} for every item with tags."""
    out = {}
    for kind, (query, field, farg, ftype) in (("scene", ("findScenes", "scenes", "scene_filter", "SceneFilterType")),
                                             ("image", ("findImages", "images", "image_filter", "ImageFilterType"))):
        page = 1
        while True:
            items = stash.call(
                f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ id tags {{ id }} }} }} }}",
                {"p": {"tag_count": {"value": 0, "modifier": "GREATER_THAN"}}, "f": {"page": page, "per_page": 5000}})[query][field]
            for i in items:
                out[f"{kind}:{i['id']}"] = {t["id"] for t in i["tags"]}
            if len(items) < 5000:
                break
            page += 1
    return out


def organized_keys(stash):
    """{"scene:ID" / "image:ID"} for every organized scene and image."""
    out = set()
    for kind, query, field, farg, ftype in (("scene", "findScenes", "scenes", "scene_filter", "SceneFilterType"),
                                            ("image", "findImages", "images", "image_filter", "ImageFilterType")):
        page = 1
        while True:
            items = stash.call(f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ id }} }} }}",
                               {"p": {"organized": True}, "f": {"page": page, "per_page": 5000}})[query][field]
            out |= {f"{kind}:{i['id']}" for i in items}
            if len(items) < 5000:
                break
            page += 1
    return out


def rejected_tags(item_tags, machine):
    """Tags the add-on added often that you've mostly removed again:
    {tag id: (added, kept)}."""
    added, kept = Counter(), Counter()
    for key, tids in machine.items():
        have = item_tags.get(key, set())
        for t in tids:
            added[t] += 1
            kept[t] += t in have
    return {t: (n, kept[t]) for t, n in added.items() if n >= REJECT_MIN and kept[t] < n * REJECT_KEPT}


class LookIndex:
    """Everything needed to guess tags from look-alikes, loaded once per run."""

    def __init__(self, stash, cfg, plugin_dir, excluded=frozenset()):
        self.cfg = cfg
        self.cache = LookCache(plugin_dir)
        tags = all_item_tags(stash)
        try:
            with open(os.path.join(plugin_dir, "machine_tags.json")) as f:
                machine = json.load(f)
        except (OSError, ValueError):
            machine = {}
        self.rejected = rejected_tags(tags, machine)
        # an organized item has been looked over: the AI's tags still on it
        # count as yours
        reviewed = organized_keys(stash)
        self.names = {t["id"]: t["name"] for t in stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
                      if t["id"] not in excluded}
        self.rows = []  # (key, fingerprint, tags you gave it)
        for key, ent in self.cache.data.items():
            t = tags.get(key)
            if t and ent.get("e"):
                t = t - set(excluded) - (set() if key in reviewed else set(machine.get(key, [])))
                if t:
                    self.rows.append((key, _ints(ent["e"]), frozenset(t)))

    def ready(self):
        return len(self.rows) >= LOOK_MIN_ITEMS

    def scores(self, vec, skip=None):
        """{tag id: 0-1} from the LOOK_K most alike tagged items."""
        if not vec or not self.rows:
            return {}
        best = heapq.nlargest(LOOK_K, ((sum(map(operator.mul, vec, r[1])), n) for n, r in enumerate(self.rows)
                                       if r[0] != skip))
        if not best:
            return {}
        top = best[0][0] / 16129
        out, total = defaultdict(float), 0.0
        for dot, n in best:
            w = math.exp((dot / 16129 - top) / LOOK_TEMP)
            total += w
            for t in self.rows[n][2]:
                out[t] += w
        return {t: s / total for t, s in out.items() if t not in self.rejected}

    def for_item(self, stash, kind, item, compute=True):
        vec = self.cache.vector(stash, self.cfg, kind, item, compute=compute)
        return self.scores(vec, skip=f"{kind}:{item['id']}") if vec else None


def load_look_index(stash, cfg, plugin_dir, excluded=frozenset(), quiet=False):
    """A LookIndex when the helper can make picture fingerprints and enough
    items have one; otherwise None (things work as before without it)."""
    try:
        with urllib.request.urlopen(cfg["ai"]["face_url"] + "/health", timeout=5) as resp:
            if not json.loads(resp.read()).get("embed"):
                raise RuntimeError("no picture model")
        looks = LookIndex(stash, cfg, plugin_dir, excluded)
    except Exception as e:
        if not quiet:
            log("info", f"Look-alike tags not used ({e}); the AI works on its own.")
        return None
    if not looks.ready():
        if not quiet:
            log("info", "Look-alike tags not used yet: run \"Look-alike tags: scan scenes and images\" first.")
        return None
    return looks


def _look_targets(stash, cfg, only_untagged):
    out = []
    for kind, (query, field, farg, ftype) in (("scene", ("findScenes", "scenes", "scene_filter", "SceneFilterType")),
                                             ("image", ("findImages", "images", "image_filter", "ImageFilterType"))):
        f = {}
        if only_untagged:
            f["tag_count"] = {"value": 0, "modifier": "EQUALS"}
        if only_untagged and not cfg["ai"]["include_organized"]:
            f["organized"] = False
        page = 1
        while True:
            items = stash.call(
                f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ {LOOK_ITEM_FIELDS[kind]} }} }} }}",
                {"p": f, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}})[query][field]
            out += [(kind, i) for i in items]
            if len(items) < 500:
                break
            page += 1
    return out


def run_look_scan(stash, plugin_dir):
    """Task: a picture fingerprint for every scene and image (once; changed
    files again)."""
    cfg = settings(stash, plugin_dir)
    picture_helper(cfg, [])  # fails early if the helper or its model isn't there
    cache = LookCache(plugin_dir)
    todo = _look_targets(stash, cfg, False)
    left = [(k, i) for k, i in todo if not cache.has(k, i)]
    log("info", f"Fingerprinting {len(left)} of {len(todo)} scene(s)/image(s) ({len(todo) - len(left)} done before). "
        "Well under a second each; you can cancel and run it again to carry on.")
    done = 0
    for n, (kind, item) in enumerate(left):
        progress(n / max(len(left), 1))
        try:
            done += cache.vector(stash, cfg, kind, item) is not None
        except RuntimeError:
            cache.save()
            raise
        except Exception as e:
            log("debug", f"{kind} {item['id']}: {e}")
    cache.save()
    progress(1)
    log("info", f"Done: {done} of {len(left)} item(s) fingerprinted. Look-alike tags are now used by the AI tagging "
        "tasks and the edit page.")


def look_picks(scores, have, names_by_id, limit):
    """Tags the look-alikes are sure of that the item doesn't have."""
    picks = sorted(((s, t) for t, s in scores.items() if s >= LOOK_SURE and t not in have and t in names_by_id),
                   reverse=True)
    return [t for _, t in picks[:limit]]


def run_looks(stash, plugin_dir, preview=False, limit=None):
    """Task: items with no tags get the tags their look-alikes are sure of."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded)
    if not looks:
        return
    names = looks.names
    targets = _look_targets(stash, cfg, True)
    if limit:
        import random
        targets = random.sample(targets, min(limit, len(targets)))
    log("info", f"{'Preview: ' if preview else ''}{len(targets)} item(s) with no tags; learning from "
        f"{len(looks.rows)} item(s) you've tagged" + (" (nothing is saved)" if preview else ""))
    if looks.rejected:
        log("info", "Not added any more (you removed them from most items they were added to): "
            + ", ".join(f"{names.get(t, t)} (kept {k} of {n})" for t, (n, k) in looks.rejected.items()))
    tagged = added = 0
    for n, (kind, item) in enumerate(targets):
        progress(n / max(len(targets), 1))
        label = f'{kind} {item["id"]}' + (f' "{item["title"]}"' if item.get("title") else "")
        try:
            scores = looks.for_item(stash, kind, item)
        except RuntimeError:
            looks.cache.save()
            raise
        except Exception as e:
            log("debug", f"{label}: {e}")
            continue
        if not scores:
            continue
        picks = look_picks(scores, set(), names, cfg["ai"]["max_tags"])
        maybe = sorted(((s, t) for t, s in scores.items() if LOOK_SUGGEST <= s < LOOK_SURE and t in names), reverse=True)[:4]
        if picks and not preview:
            stash.call(f"mutation($i: {'BulkSceneUpdateInput' if kind == 'scene' else 'BulkImageUpdateInput'}!) "
                       f"{{ {'bulkSceneUpdate' if kind == 'scene' else 'bulkImageUpdate'}(input: $i) {{ id }} }}",
                       {"i": {"ids": [item["id"]], "tag_ids": {"ids": picks, "mode": "ADD"}}})
            record_machine_tags(plugin_dir, kind, item["id"], picks)
        if picks or (preview and maybe):
            tagged += bool(picks)
            added += len(picks)
            log("info", f"{label}: {'would add' if preview else '+'} "
                + (", ".join(f"{names[t]} {round(scores[t] * 100)}%" for t in picks) or "nothing")
                + (f" (less sure: {', '.join(f'{names[t]} {round(s * 100)}%' for s, t in maybe)})" if preview and maybe else ""))
    looks.cache.save()
    progress(1)
    log("info", f"{'Preview done' if preview else 'Done'}: {tagged} of {len(targets)} item(s) "
        f"{'would get' if preview else 'got'} {added} tag(s) from look-alikes.")


def run_look_suggest(stash, plugin_dir, kind, item_id):
    """Asked by the edit page: look-alike tag suggestions for one item,
    nothing saved. [{id, name, score}] best first."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded, quiet=True)
    if not looks:
        raise RuntimeError("run the task \"Look-alike tags: scan scenes and images\" first (it needs the face helper)")
    q = "findScene" if kind == "scene" else "findImage"
    item = stash.call(f"query($id: ID!) {{ {q}(id: $id) {{ {LOOK_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[q]
    scores = looks.for_item(stash, kind, item) or {}
    looks.cache.save()
    names = looks.names
    cut = cfg["ai"].get("look_suggest", LOOK_SUGGEST)
    # body tags (Tits, Ass, sizes...) come from the body-part detector, which
    # sees what's shown: look-alike guesses for them are left out
    best = sorted(((s, t) for t, s in scores.items()
                   if s >= cut and t in names and names[t].strip().lower() not in BODY_TAG_NAMES), reverse=True)
    return [{"id": t, "name": names[t], "score": round(s, 2), "sure": s >= LOOK_SURE} for s, t in best[:12]]


# --- body tags: what's shown, and how big -----------------------------------
# What's shown comes from a body-part detector in the face helper (NudeNet,
# optional: setup.sh --body). How big comes from the performer: the size you
# usually tag them with (e.g. most of their scenes have "Big Ass"), else for
# breasts their cup size (B and under small, D and up big; C is called either,
# so it's skipped), and natural / fake from their details. A size is only
# added when that part is shown, and never when the item has a size already.
# Tested on the user's library: breasts found in 93% of items tagged "Tits",
# buttocks in 89% of "Ass"; sizes from the performer's own tags right 79%
# (breasts), 87% (ass), 100% (cock) of the time; cup sizes 83%.

BODY_SHOWN = {  # tag -> (detector part, likeness needed: in two pictures, or 0.1 more in one)
    "Tits": ("FEMALE_BREAST_EXPOSED", 0.45),
    "Ass": ("BUTTOCKS_EXPOSED", 0.6),
    "Pussy": ("FEMALE_GENITALIA_EXPOSED", 0.5),
    "Cock": ("MALE_GENITALIA_EXPOSED", 0.55),  # the detector's weakest part
}
BODY_BELLY = ("BELLY_EXPOSED", 0.5)
# size groups: tag names (yours) -> what they mean; the first of each kind is
# the one added
BODY_SIZES = {
    "Tits": {"Small Tits": "small", "Tiny Tits": "small", "Medium Tits": "medium", "Medium Boobs": "medium",
             "Big Tits": "big", "Huge Tits": "big"},
    "Ass": {"Small Ass": "small", "Medium Ass": "medium", "Big Ass": "big", "Bubble Butt": "big"},
    "Cock": {"Average Dick": "medium", "Big Dick": "big"},
}
BODY_NATURAL = ("Natural Tits", ("Fake Tits", "Fake Boobs"))
BODY_ABS = ("Defined Abs", "Muscular", "Muscular Man")
BODY_FRAME_POINTS = (0.2, 0.4, 0.6, 0.8)
# every tag name that counts as a body tag (an item with any of them is
# skipped by the body tag tasks, except "check everything again")
BODY_TAG_NAMES = {n.lower() for n in [*BODY_SHOWN, *(n for g in BODY_SIZES.values() for n in g),
                                      BODY_NATURAL[0], *BODY_NATURAL[1], *BODY_ABS]}


def has_body_tags(item):
    return any((t.get("name") or "").strip().lower() in BODY_TAG_NAMES for t in item.get("tags") or [])
TRAIT_MIN, TRAIT_SHARE = 2, 0.7  # a performer's size: on 2+ of their items, and 70% of them
FEMALE = ("FEMALE", "TRANSGENDER_FEMALE")
MALE = ("MALE", "TRANSGENDER_MALE")
BODY_ITEM_FIELDS = {
    "scene": "id title organized tags { id name } performers { id } files { path duration }",
    "image": "id title organized tags { id name } performers { id } paths { image thumbnail } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
}


def body_helper(cfg, pictures):
    body = json.dumps({"images": [base64.b64encode(p).decode() for p in pictures]}).encode()
    for attempt in range(4):
        req = urllib.request.Request(cfg["ai"]["face_url"] + "/body", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["results"]
        except urllib.error.HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = json.loads(e.read()).get("error", "")
            if "isn't installed" in detail or e.code == 404:
                raise RuntimeError("the face helper has no body-part model: run face-helper/setup.sh --body")
            raise ValueError(f"the helper couldn't read it ({e.code} {detail})")
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException) as e:
            # (a dropped or reset connection isn't a URLError: before, those
            # skipped the item quietly, so a whole run could find nothing)
            reason = getattr(e, "reason", e)
            if attempt < 3:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({reason}).")


def body_pictures(stash, kind, item):
    if kind == "image":
        if not all(f["__typename"] == "ImageFile" for f in item.get("visual_files") or []):
            return []
        data, ctype = stash.fetch(item["paths"]["image"])
        if len(data) > FACE_MAX_IMAGE_BYTES:
            data, ctype = stash.fetch(item["paths"]["thumbnail"])
        if ctype == "text/html":
            raise ValueError("Stash sent a web page instead of the picture (signed out?)")
        return [data] if ctype.startswith("image/") else []
    f = (item.get("files") or [None])[0]
    if not f or not (f.get("duration") or 0) > 1:
        return []
    out, why = [], None
    for at in BODY_FRAME_POINTS:
        try:
            out.append(grab_frame(stash, f["path"], f["duration"] * at, width=960)[0])
        except Exception as e:
            why = e
    if not out and why:
        raise ValueError(f"no frames could be taken ({why})")
    return out


def parts_shown(cfg, pictures):
    """{part: best score} for parts seen: in one picture clearly (0.1 over
    what's needed), or in two."""
    seen = defaultdict(list)
    for found in body_helper(cfg, pictures) if pictures else []:
        best = {}
        for d in found:
            best[d["part"]] = max(best.get(d["part"], 0), d["score"])
        for part, sc in best.items():
            seen[part].append(sc)
    need = {p: th for p, th in list(BODY_SHOWN.values()) + [BODY_BELLY]}
    out = {}
    for part, scores in seen.items():
        th = need.get(part)
        if th is None:
            continue
        scores.sort(reverse=True)
        if scores[0] >= th + 0.1 or (len(scores) >= 2 and scores[1] >= th):
            out[part] = scores[0]
    return out


def _cup(measurements):
    m = re.search(r"\d{2}\s*([A-K]+)", measurements or "")
    return m and m.group(1).upper()


class BodyIndex:
    """Loaded once per run: tag names, each performer's details and the
    sizes you usually tag them with."""

    def __init__(self, stash, plugin_dir, excluded=frozenset()):
        tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
        self.tag_id = {t["name"].lower(): t["id"] for t in tags if t["id"] not in excluded}
        self.tag_name = {t["id"]: t["name"] for t in tags}
        try:
            with open(os.path.join(plugin_dir, "machine_tags.json")) as f:
                machine = json.load(f)
        except (OSError, ValueError):
            machine = {}
        self.perfs = {p["id"]: p for p in stash.call(
            "{ findPerformers(filter: {per_page: -1}) { performers { id name gender measurements fake_tits } } }"
        )["findPerformers"]["performers"]}
        size_of = {}  # tag id -> (group, size)
        for group, names in BODY_SIZES.items():
            for n, size in names.items():
                if n.lower() in self.tag_id:
                    size_of[self.tag_id[n.lower()]] = (group, size)
        abs_ids = {self.tag_id[n.lower()] for n in BODY_ABS if n.lower() in self.tag_id}
        counts = defaultdict(Counter)  # performer -> Counter((group, size)) / ("abs", tag)
        totals = defaultdict(Counter)  # performer -> Counter(group)
        for kind, (query, field) in (("scene", ("findScenes", "scenes")), ("image", ("findImages", "images"))):
            items = stash.call(f'{{ {query}(filter: {{per_page: -1}}, {kind}_filter: {{performer_count: {{value: 0, modifier: GREATER_THAN}}, '
                               f'tag_count: {{value: 0, modifier: GREATER_THAN}}}}) {{ {field} {{ id tags {{ id }} performers {{ id gender }} }} }} }}'
                               )[query][field]
            for it in items:
                own = set(machine.get(f"{kind}:{it['id']}", []))  # don't learn from the add-on's own tags
                tids = [t["id"] for t in it["tags"] if t["id"] not in own]
                for p in it["performers"]:
                    same = [q for q in it["performers"] if (q["gender"] in FEMALE) == (p["gender"] in FEMALE)
                            and (q["gender"] in MALE) == (p["gender"] in MALE)]
                    if len(same) != 1:
                        continue  # can't tell whose it is
                    groups = set()
                    for t in tids:
                        if t in size_of:
                            counts[p["id"]][size_of[t]] += 1
                            groups.add(size_of[t][0])
                        if t in abs_ids:
                            counts[p["id"]][("abs", t)] += 1
                    for g in groups:
                        totals[p["id"]][g] += 1
        self.traits = {}  # performer -> {group: size}
        self.abs = {}  # performer -> tag id
        for pid, c in counts.items():
            for group, total in totals[pid].items():
                best = max(((n, s) for (g, s), n in c.items() if g == group), default=None)
                if best and best[0] >= TRAIT_MIN and best[0] >= TRAIT_SHARE * total:
                    self.traits.setdefault(pid, {})[group] = best[1]
            ab = [(n, t) for (g, t), n in c.items() if g == "abs" and n >= TRAIT_MIN]
            if ab:
                self.abs[pid] = max(ab)[1]

    def tag(self, name):
        return self.tag_id.get(name.lower())

    def size_tag(self, group, size):
        for n, s in BODY_SIZES[group].items():
            if s == size and self.tag(n):
                return self.tag(n)
        return None

    def picks(self, item, shown):
        """[(tag id, why)] for what's shown and how big."""
        have = {t["id"] for t in item.get("tags") or []}
        perfs = [self.perfs[p["id"]] for p in item.get("performers") or [] if p["id"] in self.perfs]
        women = [p for p in perfs if p["gender"] in FEMALE]
        men = [p for p in perfs if p["gender"] in MALE]
        out = []

        def add(tid, why):
            if tid and tid not in have and tid not in [t for t, _ in out]:
                out.append((tid, why))

        for name, (part, _) in BODY_SHOWN.items():
            if part not in shown:
                continue
            # the detector can take a man's chest for breasts: trust who's in it
            if perfs and name in ("Tits", "Pussy") and not women:
                continue
            if perfs and name == "Cock" and not men and all(p["gender"] == "FEMALE" for p in perfs):
                continue
            add(self.tag(name), f"{part.lower().replace('_', ' ')} {round(shown[part] * 100)}%")
            if name not in BODY_SIZES or any(self.tag(n) in have for n in BODY_SIZES[name]):
                continue
            who = men if name == "Cock" else women if (women or name != "Ass") else men
            if len(who) != 1:
                continue
            p = who[0]
            size = (self.traits.get(p["id"]) or {}).get(name)
            why = f"{p['name']} is usually tagged so"
            if not size and name == "Tits":
                cup = _cup(p.get("measurements"))
                if cup:
                    size = "small" if cup in ("AA", "A", "B") else "big" if cup >= "D" else None
                    why = f"{p['name']}: {cup} cup"
            if size:
                add(self.size_tag(name, size), why)
            if name == "Tits" and (p.get("fake_tits") or "").strip():
                fake = p["fake_tits"].strip().lower().startswith("f")
                nat, fakes = BODY_NATURAL
                if not any(self.tag(n) in have for n in (nat,) + fakes):
                    add(next((self.tag(n) for n in fakes if self.tag(n)), None) if fake else self.tag(nat),
                        f"{p['name']}'s details")
        if BODY_BELLY[0] in shown and len(perfs) == 1 and perfs[0]["id"] in self.abs:
            add(self.abs[perfs[0]["id"]], f"belly shown; {perfs[0]['name']} is usually tagged so")
        return out


def body_excluded(stash, cfg):
    """Only your "Tags to ignore" (gallery-name tags like "Tits" are fine
    to add here: they're what's shown)."""
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
    return {t["id"] for t in tags if t["name"].lower() in cfg["exclude"]}


def body_ready(cfg):
    try:
        with urllib.request.urlopen(cfg["ai"]["face_url"] + "/health", timeout=5) as resp:
            return bool(json.loads(resp.read()).get("body"))
    except Exception:
        return False


def body_item(stash, cfg, body, kind, item, apply, plugin_dir=None, rejected=(), skip_tagged=False):
    """Body tags for one item: [(tag id, name, why)] (added when apply).
    skip_tagged: nothing (and no frames looked at) when it already has a
    body tag."""
    if skip_tagged and has_body_tags(item):
        return []
    shown = parts_shown(cfg, body_pictures(stash, kind, item))
    picks = [(t, why) for t, why in body.picks(item, shown) if t not in rejected]
    if apply and picks:
        stash.call(f"mutation($i: {'BulkSceneUpdateInput' if kind == 'scene' else 'BulkImageUpdateInput'}!) "
                   f"{{ {'bulkSceneUpdate' if kind == 'scene' else 'bulkImageUpdate'}(input: $i) {{ id }} }}",
                   {"i": {"ids": [item["id"]], "tag_ids": {"ids": [t for t, _ in picks], "mode": "ADD"}}})
        if plugin_dir:
            record_machine_tags(plugin_dir, kind, item["id"], [t for t, _ in picks])
    return [(t, body.tag_name.get(t, t), why) for t, why in picks]


def run_body(stash, plugin_dir, preview=False, limit=None, redo=False):
    """Task: body tags for every scene and image not done yet (or a
    preview on `limit` random items). redo: every scene and image again,
    organized ones too, whether done before or not (e.g. after making a
    new body tag); it keeps its own progress, so cancelling and running it
    again carries on, and a finished round starts over next time."""
    cfg = settings(stash, plugin_dir)
    if not body_ready(cfg):
        raise RuntimeError("the face helper has no body-part model: run face-helper/setup.sh --body")
    body = BodyIndex(stash, plugin_dir, body_excluded(stash, cfg))
    missing = [n for n in BODY_SHOWN if not body.tag(n)]
    if missing:
        log("info", f"No tag called {', '.join(missing)}: those parts aren't tagged (make the tag to have them added)")
    try:
        with open(os.path.join(plugin_dir, "machine_tags.json")) as f:
            rejected = rejected_tags(all_item_tags(stash), json.load(f))
    except (OSError, ValueError):
        rejected = {}
    done_path = os.path.join(plugin_dir, "body_done.json")
    with locked_json(done_path) as done:
        done = {k: set(v) for k, v in done.items() if isinstance(v, list)}
    skip = (lambda kind: done.get("redo_" + kind + "s", set())) if redo else (lambda kind: done.get(kind + "s", set()))
    targets = []
    for kind in ("scene", "image"):
        query, field = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
        f = {} if (redo or cfg["ai"]["include_organized"]) else {"organized": False}
        page = 1
        while True:
            items = stash.call(f"query($p: {kind.capitalize()}FilterType, $f: FindFilterType) {{ {query}({kind}_filter: $p, filter: $f) "
                               f"{{ {field} {{ {BODY_ITEM_FIELDS[kind]} }} }} }}",
                               {"p": f, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}})[query][field]
            targets += [(kind, i) for i in items if preview or i["id"] not in skip(kind)]
            if len(items) < 500:
                break
            page += 1
    if limit:
        import random
        targets = random.sample(targets, min(limit, len(targets)))
    if redo and not preview:
        log("info", "Checking every scene and image again (organized ones too, and ones done before). "
            "Items without performers get what's shown but no sizes.")
    log("info", f"{'Preview: ' if preview else ''}body tags for {len(targets)} item(s); sizes known for "
        f"{len(body.traits)} performer(s) from your tags" + (" (nothing is saved)" if preview else
                                                               ". Scenes take a few seconds, images under one; cancel and run again to carry on."))
    tagged = added = had = failed = 0
    for n, (kind, item) in enumerate(targets):
        progress(n / max(len(targets), 1))
        label = f'{kind} {item["id"]}' + (f' "{item["title"]}"' if item.get("title") else "")
        try:
            if not redo and has_body_tags(item):
                had += 1
                picks = []
            else:
                picks = body_item(stash, cfg, body, kind, item, apply=not preview, plugin_dir=plugin_dir, rejected=rejected)
        except RuntimeError:
            raise
        except Exception as e:
            failed += 1
            log("warning" if failed <= 3 else "debug", f"{label}: couldn't check it ({e})")
            continue
        if picks:
            tagged += 1
            added += len(picks)
            log("info", f"{label}: {'would add' if preview else '+'} " + ", ".join(f"{name} ({why})" for _, name, why in picks))
        if not preview:
            with locked_json(done_path) as d:
                if item["id"] not in d.setdefault(kind + "s", []):
                    d[kind + "s"].append(item["id"])
                if redo:
                    d.setdefault("redo_" + kind + "s", []).append(item["id"])
    if redo and not preview:
        # the round is finished: next time starts over
        with locked_json(done_path) as d:
            d.pop("redo_scenes", None)
            d.pop("redo_images", None)
    progress(1)
    log("info", f"{'Preview done' if preview else 'Done'}: {tagged} of {len(targets)} item(s) "
        f"{'would get' if preview else 'got'} {added} body tag(s)."
        + (f" {had} already had body tags and were skipped." if had else "")
        + (f" {failed} couldn't be checked (see the warnings above)." if failed else ""))


# --- new items: quick steps now, AI model steps at night ---------------------
# With "AI: wait until night for new items" on, a scan's new items get the
# quick steps straight away (no AI model, about a second each: tags from the
# file names, studios from links in them, look-alike tags, body tags) and
# are saved in ai_night.json for the AI model steps (AI tags, watermarks, AI
# titles), which run once at night. A small background process in Stash's
# container (night_runner.*) wakes at that time and starts the task "AI: run
# tonight's new items now"; it's started again by any scan if Stash restarted.

NIGHT_TASK = "AI: run tonight's new items now"


def locked_night(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "ai_night.json"))


def run_quick_new(stash, plugin_dir, cfg, new):
    """The quick steps for new items (no AI model)."""
    ai = cfg["ai"]
    targets = []
    for kind, ids in (("scene", new.get("scene") or []), ("image", new.get("image") or [])):
        if ids:
            query, field = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
            items = find_by_ids(stash, query, field, ids, f"{AI_ITEM_FIELDS[kind]}")
            targets += [(kind, i) for i in items]
    if not targets:
        return
    index = None
    if ai["autotag_first"]:
        with contextlib.suppress(Exception):
            index = autotag_index(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded, quiet=True)
    body = BodyIndex(stash, plugin_dir, body_excluded(stash, cfg)) if body_ready(cfg) else None
    ctx = accounts_context(stash, plugin_dir) if ai["new_all"] else None
    done = 0
    for kind, item in targets:
        label = f'{kind} {item["id"]}' + (f' "{item["title"]}"' if item.get("title") else "")
        added = []
        try:
            have = set()
            if index is not None:
                pre, have = autotag_item(stash, index, kind, item, apply=True)
                added += pre or []
            if looks:
                scores = looks.for_item(stash, kind, item) or {}
                own = {t["id"] for t in item.get("tags") or []} | set(have)
                picks = look_picks(scores, own, looks.names, ai["max_tags"])
                picks = [t for t in picks if t not in looks.rejected]
                if picks:
                    stash.call(f"mutation($i: {'BulkSceneUpdateInput' if kind == 'scene' else 'BulkImageUpdateInput'}!) "
                               f"{{ {'bulkSceneUpdate' if kind == 'scene' else 'bulkImageUpdate'}(input: $i) {{ id }} }}",
                               {"i": {"ids": [item["id"]], "tag_ids": {"ids": picks, "mode": "ADD"}}})
                    record_machine_tags(plugin_dir, kind, item["id"], picks)
                    added += [looks.names[t] for t in picks]
            if body:
                added += [n for _, n, _ in body_item(stash, cfg, body, kind, item, apply=True, plugin_dir=plugin_dir,
                                                      rejected=looks.rejected if looks else ())]
            if ctx is not None and kind == "scene":
                line, _ = process_scene_accounts(stash, cfg, ctx, item, apply=True, use_ai=False)
                if line:
                    log("info", line)
        except RuntimeError as e:
            log("warning", f"Quick steps for new items stopped: {e}")
            break
        except Exception as e:
            log("debug", f"{label}: {e}")
        if added:
            done += 1
            log("info", f"{label}: + {', '.join(dict.fromkeys(added))}")
    if looks:
        looks.cache.save()
    log("info", f"Quick steps done for {len(targets)} new item(s) ({done} got tags)")


def save_for_tonight(stash, plugin_dir, cfg, new):
    with locked_night(plugin_dir) as night:
        for kind in ("scene", "image"):
            have = night.setdefault(kind + "s", [])
            have += [i for i in new.get(kind) or [] if i not in have]
        waiting = len(night.get("scenes", [])) + len(night.get("images", []))
    ensure_night_runner(stash, plugin_dir, cfg)
    log("info", f"Saved for tonight's AI run at {cfg['ai']['night_time']} ({cfg['ai']['time_zone']}): "
        f"{len(new.get('scene') or []) + len(new.get('image') or [])} new item(s), {waiting} waiting in all")


def run_night(stash, plugin_dir):
    """Task: the AI model steps for the new items saved for tonight."""
    cfg = settings(stash, plugin_dir)
    ai = cfg["ai"]
    if ai["nightly"] or ai["night_body"]:
        ensure_night_runner(stash, plugin_dir, cfg)  # e.g. after Stash restarted
    with locked_night(plugin_dir) as night:
        todo = {"scene": list(night.get("scenes", [])), "image": list(night.get("images", []))}
    n = len(todo["scene"]) + len(todo["image"])
    if not n:
        log("info", "No new items waiting for the AI.")
        if ai["night_body"]:
            run_night_body(stash, plugin_dir, cfg)
        return
    log("info", f"Tonight's AI run: {len(todo['scene'])} scene(s), {len(todo['image'])} image(s)")
    if ai["auto"] or ai["new_all"]:
        run_ai_queued(stash, plugin_dir, todo["scene"], todo["image"])
    if ai["new_all"] and todo["scene"]:
        run_accounts_items(stash, plugin_dir, todo["scene"])
    if ai["photo_studios"] and todo["image"]:
        run_accounts_items(stash, plugin_dir, todo["image"], kind="image")
    if ai["new_all"]:
        run_ai_titles(stash, plugin_dir, ["scene", "image"], ids=todo)
    with locked_night(plugin_dir) as night:  # items added while this ran stay for next time
        for kind in ("scene", "image"):
            night[kind + "s"] = [i for i in night.get(kind + "s", []) if i not in todo[kind]]
    if ai["night_body"]:
        run_night_body(stash, plugin_dir, cfg)
    log("info", "Tonight's AI run is done.")


def run_night_body(stash, plugin_dir, cfg):
    """Night run: the body tags task for everything not checked yet (items
    with a body tag already are skipped). Needs the face helper's body
    model; without it, this is skipped with a note."""
    if not body_ready(cfg):
        log("warning", "Body tags tonight skipped: the face helper isn't running or has no body-part model.")
        return
    log("info", "Body tags for everything not checked yet...")
    run_body(stash, plugin_dir)


def _night_files(plugin_dir):
    return {k: os.path.join(plugin_dir, "night_runner." + k) for k in ("json", "pid", "log")}


def ensure_night_runner(stash, plugin_dir, cfg):
    """Start the background process that starts tonight's run (unless it's
    running already); it gets the current time and connection."""
    files = _night_files(plugin_dir)
    tmp = files["json"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"conn": stash.conn, "time": cfg["ai"]["night_time"], "tz": cfg["ai"]["time_zone"],
                   "plugin_id": AI_ID, "task": NIGHT_TASK, "body": cfg["ai"]["night_body"]}, f)
    os.replace(tmp, files["json"])
    with contextlib.suppress(OSError, ValueError):
        with open(files["pid"]) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # still there
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            if b"--night-runner" in f.read():
                return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "better_tagging_ai.py")
    if not os.path.exists(script):
        script = os.path.join(plugin_dir, "better_tagging_ai.py")
    with open(files["log"], "a") as logf:
        proc = subprocess.Popen([sys.executable, script, "--night-runner", plugin_dir], stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=logf, close_fds=True, start_new_session=True)
    with open(files["pid"], "w") as f:  # straight away, so a second call sees it
        f.write(str(proc.pid))


def night_runner_main(plugin_dir):
    """The background process: wait for the night time, start the task if
    anything is waiting, repeat. Exits if another one took over."""
    from datetime import datetime, timedelta
    files = _night_files(plugin_dir)
    with open(files["pid"], "w") as f:
        f.write(str(os.getpid()))

    def say(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

    say("night runner started")
    while True:
        try:
            with open(files["json"]) as f:
                conf = json.load(f)
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(conf.get("tz") or "UTC")
            except Exception:
                tz = None
            now = datetime.now(tz)
            hh, mm = (int(x) for x in (conf.get("time") or "03:00").split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at <= now:
                at += timedelta(days=1)
            wait = (at - now).total_seconds()
            while wait > 0:  # short naps, so a changed time or a new runner is noticed
                time.sleep(min(wait, 300))
                with open(files["pid"]) as f:
                    if f.read().strip() != str(os.getpid()):
                        say("another night runner took over; stopping")
                        return
                with open(files["json"]) as f:
                    if json.load(f) != conf:
                        break  # settings or connection changed: work out the time again
                wait = (at - datetime.now(tz)).total_seconds()
            if wait > 0:
                continue
            try:
                with open(os.path.join(plugin_dir, "ai_night.json")) as f:
                    night = json.load(f)
            except (OSError, ValueError):
                night = {}
            if night.get("scenes") or night.get("images") or conf.get("body"):
                stash = Stash(conf["conn"])
                job = stash.call("mutation($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }",
                                 {"p": conf["plugin_id"], "t": conf["task"]})
                say(f"started tonight's AI run (job {job.get('runPluginTask')})")
            else:
                say("nothing waiting tonight")
            time.sleep(90)
        except Exception as e:
            say(f"problem: {e}; trying again in 10 minutes")
            time.sleep(600)


# --- title ideas -------------------------------------------------------------
# Asked by the edit page (runPluginOperation "title_ideas"): titles made from
# the item's performers, studio, most telling tags, date and file name, and
# (on request) ones the local AI model writes after looking at it. Nothing is
# saved; clicking an idea puts it in the Title box.

TITLE_ITEM_FIELDS = {
    "scene": "id title date tags { id name scene_count image_count gallery_count } "
             "performers { id name tags { id name scene_count image_count gallery_count } } "
             "studio { id name } files { path duration }",
    "image": "id title date tags { id name scene_count image_count gallery_count } "
             "performers { id name tags { id name scene_count image_count gallery_count } } "
             "studio { id name } paths { thumbnail } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
    "gallery": "id title date tags { id name scene_count image_count gallery_count } "
               "performers { id name tags { id name scene_count image_count gallery_count } } "
               "studio { id name } files { path } folder { path } paths { cover }",
}


def _names_text(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1] if names else ""


def title_ideas_from_data(stash, plugin_dir, kind, item):
    """Titles put together from what's known about the item, best first."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    performers = [p["name"] for p in item.get("performers") or []][:3]
    studio = (item.get("studio") or {}).get("name")
    # the most telling tags: the rarest ones (used at least twice, so not one-offs)
    tags = [t for t in item.get("tags") or [] if t["id"] not in excluded]
    if not tags:  # none yet: the performers' top tags
        tags = list({t["id"]: t for p in item.get("performers") or [] for t in p.get("tags") or []
                     if t["id"] not in excluded}.values())
    use = lambda t: t["scene_count"] + t["image_count"] + t["gallery_count"]
    tags.sort(key=lambda t: (use(t) < 2, use(t)))
    t = []
    for x in tags:  # skip ones another already says ("Solo" next to "Solo Female")
        low = x["name"].lower()
        if not any(low in y.lower() or y.lower() in low for y in t):
            t.append(x["name"])
        if len(t) == 3:
            break
    who = _names_text(performers)
    day = None
    if item.get("date"):
        import datetime
        try:
            day = datetime.date.fromisoformat(item["date"]).strftime("%-d %b %Y")
        except ValueError:
            pass
    ideas = []
    names = title_names(stash)
    path = item_path(kind, item)
    for keep in (cfg["titles_keep_names"], not cfg["titles_keep_names"]):
        ideas.append(title_from_path(path, names, cfg["spellings"], keep))
    if who and t:
        ideas.append(f"{who} {' '.join(t[:2])}")
        ideas.append(f"{who} - {_names_text(t)}")
    if studio and who:
        ideas.append(f"{studio} - {who}" + (f" {t[0]}" if t else ""))
    if who and day:
        ideas.append(f"{who} - {day}")
    if not who and t:
        ideas.append(_names_text(t))
        if studio:
            ideas.append(f"{studio} - {' '.join(t[:2])}")
    out, seen = [], {_title_key(item.get("title") or "")}
    for i in ideas:
        if i and _title_key(i) not in seen:  # "A - B" and "A B" count as the same
            seen.add(_title_key(i))
            out.append(i)
    return out[:6]


def ollama_titles(cfg, pictures, context):
    """Ask the model for a few short titles, given the pictures and what's
    known about the item."""
    ai = cfg["ai"]
    prompt = (
        "Suggest 4 different short titles (3 to 9 words each) for "
        + ("this video (the pictures are frames from it)" if len(pictures) > 1
           else "this video (the picture shows 4 frames from it in a 2x2 grid)" if isinstance(pictures[0], GridPicture)
           else "this picture")
        + " in a private adult media library. Describe what actually happens or is shown, in a natural, "
        "catchy way, like a video title on a website. Use Title Case. No quotes, no emojis, no hashtags, "
        "no file names or numbers. Never make up names: only use a person's name if it's listed below as a "
        "performer, otherwise describe them (e.g. 'Redhead', 'Blonde').\n" + context
    )
    body = {
        "model": ai["model"], "stream": False, "keep_alive": "15m",
        "options": {"temperature": 0.8},
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(p).decode() for p in pictures]}],
        "format": {"type": "object", "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
                   "required": ["titles"]},
    }
    for attempt in (1, 2):  # Ollama now and then answers 500 once
        req = urllib.request.Request(ai["url"] + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                reply = json.loads(resp.read())
            break
        except urllib.error.HTTPError:
            if attempt == 2:
                raise
            time.sleep(1)
    try:
        titles = json.loads((reply.get("message") or {}).get("content") or "{}").get("titles") or []
    except ValueError:
        titles = []
    clean = []
    for t in titles:
        if isinstance(t, str):
            # the model sometimes runs on past the title, e.g.
            # 'Stage Performance", 4 words, <start_of_image>': keep the title
            t = re.split(r'["”]\s*,', t)[0]
            t = re.sub(r"\s+", " ", t.strip().strip('"\'“”').rstrip("."))
            if re.search(r"[<>{}\[\]]|\b\d+ words?\b", t, re.I):
                continue
            if 2 < len(t) <= 100:
                clean.append(t)
    return clean[:5]


def ai_title_context(stash, cfg, plugin_dir, kind, item, excluded=None):
    """The pictures to show the model and what's known about the item."""
    if kind == "gallery":
        cover = (item.get("paths") or {}).get("cover")
        data, ctype = stash.fetch(cover) if cover else (b"", "")
        pictures = [data] if ctype.startswith("image/") else []
    else:
        pictures = ai_pictures(stash, kind, item)
    if excluded is None:
        _, excluded = tag_lookup(stash, cfg, plugin_dir)
    bits = []
    performers = [p["name"] for p in item.get("performers") or []]
    if performers:
        bits.append("Performers (you may use their names): " + ", ".join(performers))
    if (item.get("studio") or {}).get("name"):
        bits.append("Studio: " + item["studio"]["name"])
    tags = [t["name"] for t in item.get("tags") or [] if t["id"] not in excluded]
    if tags:
        bits.append("Its tags: " + ", ".join(tags[:25]))
    from_file = title_from_path(item_path(kind, item), title_names(stash), cfg["spellings"], True)
    if from_file:
        bits.append("Its file name says: " + from_file)
    return pictures, "\n".join(bits)


def run_ai_titles(stash, plugin_dir, kinds, preview=False, limit=None, ids=None):
    """Task: give every scene / image without a title one - from the file
    name when that makes a good one, else the local AI model's best idea.
    `preview`: only log what it would set (on `limit` random items). New
    items (`ids`) and the preview only get the kinds "AI titles: which
    items" says; the tasks named for scenes / images do just those."""
    cfg = settings(stash, plugin_dir)
    if ids is not None or preview:
        kinds = [k for k in kinds if k in cfg["ai"]["title_kinds"]]
        if not kinds:
            return
    ai_check(cfg)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    names = title_names(stash)
    todo = []
    for kind in kinds:
        query, field, farg, ftype = {"scene": ("findScenes", "scenes", "scene_filter", "SceneFilterType"),
                                     "image": ("findImages", "images", "image_filter", "ImageFilterType")}[kind]
        f = {"title": {"value": "", "modifier": "IS_NULL"}}
        if not cfg["ai"]["include_organized"] and ids is None:
            f["organized"] = False
        page = 1
        while True:
            if ids is not None:  # just these (e.g. new from a scan)
                if not ids.get(kind):
                    break
                items = stash.call(
                    f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ {TITLE_ITEM_FIELDS[kind]} }} }} }}",
                    {"ids": ids[kind]})[query][field]
                todo += [(kind, i) for i in items if not (i.get("title") or "").strip()]
                break
            items = stash.call(
                f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ {TITLE_ITEM_FIELDS[kind]} }} }} }}",
                {"p": f, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}},
            )[query][field]
            todo += [(kind, i) for i in items if not (i.get("title") or "").strip()]
            if len(items) < 500:
                break
            page += 1
    if limit:
        import random
        todo = random.sample(todo, min(limit, len(todo)))
    what = " and ".join(k + "s" for k in kinds)
    log("info", f"{len(todo)} {what} without a title" + (" (preview, nothing is saved)" if preview else "")
        + (f". The AI takes a few seconds each, so this can take a while; you can cancel it under Settings → Tasks."
           if len(todo) > 50 else "."))
    from_file = from_ai = failed = 0
    for n, (kind, item) in enumerate(todo):
        progress(n / max(len(todo), 1))
        name = os.path.basename(item_path(kind, item)) or f"{kind} {item['id']}"
        title = title_from_path(item_path(kind, item), names, cfg["spellings"], cfg["titles_keep_names"])
        source = "file name"
        if not title:
            try:
                pictures, context = ai_title_context(stash, cfg, plugin_dir, kind, item, excluded)
                ideas = ollama_titles(cfg, pictures, context) if pictures else []
            except Exception as e:
                failed += 1
                log("warning", f"{name}: AI failed ({e})")
                continue
            title, source = (ideas[0] if ideas else None), "AI"
        if not title:
            failed += 1
            continue
        if not preview:
            set_title(stash, kind, item, title, record=source == "file name")
        if source == "AI":
            from_ai += 1
        else:
            from_file += 1
        log("info", f'{"Would set" if preview else "Set"} {name} → "{title}" ({source})')
    progress(1)
    log("info", f"{'Preview: ' if preview else 'Done: '}{from_file + from_ai} of {len(todo)} {what} "
        + ("would get" if preview else "got") + f" a title ({from_file} from the file name, {from_ai} from the AI)"
        + (f"; {failed} had nothing to show the AI or failed" if failed else "")
        + (". Nothing was saved." if preview else "."))


def run_title_ideas(stash, plugin_dir, kind, item_id, use_ai):
    """Returns {"ideas": [...], "ai": [...]} for one scene, image or gallery."""
    query = {"scene": "findScene", "image": "findImage", "gallery": "findGallery"}[kind]
    item = stash.call(f"query($id: ID!) {{ {query}(id: $id) {{ {TITLE_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[query]
    if not item:
        raise RuntimeError(f"{kind} {item_id} not found")
    if not use_ai:
        return {"ideas": title_ideas_from_data(stash, plugin_dir, kind, item)}
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    pictures, context = ai_title_context(stash, cfg, plugin_dir, kind, item)
    if not pictures:
        raise RuntimeError("there's no picture to show the AI (a video clip stored as an image?)")
    return {"ai": ollama_titles(cfg, pictures, context)}


# --- studios from social media handles ----------------------------------------
# Finds the OnlyFans / Fansly / Patreon / Instagram / TikTok account a scene comes from -
# in its file name, title, details or URLs (e.g. "onlyfans.com/name"), or in
# the watermark / on-screen text the local AI model reads off a few frames -
# and gives scenes without a studio a studio for it (made if needed, under a
# parent studio for the platform). A handle the AI reads only counts if it's
# in at least two frames (or in one frame and the text), as small models can
# misread small text.

PLATFORMS = {
    # key: (parent studio name, profile URL, patterns in text)
    "onlyfans": ("OnlyFans", "https://onlyfans.com/{}", [r"onlyfans\.com/([A-Za-z0-9_.\-]{2,40})"]),
    "fansly": ("Fansly", "https://fansly.com/{}", [r"fansly\.com/([A-Za-z0-9_.\-]{2,40})"]),
    "instagram": ("Instagram", "https://www.instagram.com/{}", [r"instagram\.com/([A-Za-z0-9_.]{2,30})"]),
    "tiktok": ("TikTok", "https://www.tiktok.com/@{}", [r"tiktok\.com/@([A-Za-z0-9_.]{2,30})"]),
    # patreon.com/name, patreon.com/c/name, or "Patreon: name". (Comic
    # watermarks often say just "patreon/name": only taken from a watermark,
    # see PATREON_LOOSE - in a file path "…patreon/next folder" isn't one.)
    "patreon": ("Patreon", "https://www.patreon.com/{}",
                [r"patreon\.com/(?:c/)?@?([A-Za-z0-9_.\-]{2,40})", r"patreon\s*:\s*@?([A-Za-z0-9_.\-]{2,40})"]),
}
PLATFORM_ORDER = ("onlyfans", "fansly", "patreon", "tiktok", "instagram")  # which one wins if there are several
HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{1,39}$")
HANDLE_STOP = {"onlyfans", "fansly", "instagram", "tiktok", "patreon", "com", "www", "http", "https", "profile", "me",
               "my", "username", "user", "handle", "link", "bio", "follow", "subscribe", "watermark",
               "c", "posts", "join", "home", "support", "membership", "login", "checkout"}
HANDLE_FRAME_POINTS = (0.03, 0.2, 0.4, 0.6, 0.8, 0.97)
HANDLE_SCENE_FIELDS = "id title details urls studio { id } performers { id } files { path duration }"
HANDLE_IMAGE_FIELDS = ("id title details urls studio { id } performers { id } paths { image thumbnail } "
                       "visual_files { __typename ... on ImageFile { path size } ... on VideoFile { path size } }")
HANDLE_FIELDS = {"scene": HANDLE_SCENE_FIELDS, "image": HANDLE_IMAGE_FIELDS}


def photo_picture(stash, image):
    """The photo itself for the model to read (small watermark text needs
    more than the thumbnail); an animation / video stored as an image, or a
    huge file: its thumbnail."""
    f = (image.get("visual_files") or [{}])[0]
    if f.get("__typename") == "ImageFile" and (f.get("size") or 0) <= FACE_MAX_IMAGE_BYTES:
        data, ctype = stash.fetch(image["paths"]["image"])
    else:
        data, ctype = stash.fetch(image["paths"]["thumbnail"])
    if not ctype.startswith("image/") or ctype == "image/gif":
        if ctype == "image/gif":
            data, ctype = stash.fetch(image["paths"]["thumbnail"])
        if not ctype.startswith("image/"):
            raise ValueError(f"Stash sent {ctype or 'nothing'} instead of the picture")
    return data


# read in watermarks but not anyone's account: leak / repost sites
HANDLE_NOT_ACCOUNT = re.compile(r"(?i)fappening|leak|thothub|nudostar|coomer|kemono|simpcity|reddit|imgur|"
                                r"repost|onlyfanz|camwhores|erome")


def clean_handle(h):
    h = (h or "").strip().lstrip("@").strip(" /.,:;!?\"'")
    h = re.sub(r"^(?:https?://)?(?:www\.)?(?:onlyfans|fansly|instagram|tiktok|patreon)\.com/(?:c/)?@?", "", h, flags=re.I)
    h = h.split("/")[0].split("?")[0].rstrip(".")
    # an email or web address ("@gmail.com", "site.net") isn't an account
    if re.search(r"(?i)\.(com|net|org|io|co|me|tv|xxx|uk|de|ru)$", h) or HANDLE_NOT_ACCOUNT.search(h):
        return None
    return h if HANDLE_RE.match(h) and h.lower() not in HANDLE_STOP and not h.isdigit() else None


def handles_in_text(text):
    """{(platform, handle lowercased): handle as written} from links in text."""
    found = {}
    for plat, (_, _, pats) in PLATFORMS.items():
        for pat in pats:
            for m in re.finditer(pat, text or "", re.I):
                h = clean_handle(m.group(1))
                if h:
                    found.setdefault((plat, h.lower()), h)
    return found


PLATFORM_WORDS = {
    "tiktok": r"tik\s*tok", "instagram": r"instagram|\big\b|insta\b", "onlyfans": r"only\s*fans|\bof\b",
    "fansly": r"fansly", "patreon": r"patreon",
}


PATREON_LOOSE = re.compile(r"patreon\s*/\s*@?([A-Za-z0-9_.\-]{2,40})", re.I)


def handles_in_ocr(text):
    """[(platform or None, handle)] in text the model transcribed off a frame:
    profile links, "patreon/name", or "@name" with the platform named next
    to it."""
    out = [(plat, h) for (plat, _), h in handles_in_text(text).items()]
    have = {h.lower() for _, h in out}
    for m in PATREON_LOOSE.finditer(text or ""):
        h = clean_handle(m.group(1))
        if h and h.lower() not in have:
            out.append(("patreon", h))
            have.add(h.lower())
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        for m in re.finditer(r"@([A-Za-z0-9_][A-Za-z0-9_.]{1,39})", line):
            h = clean_handle(m.group(1))
            if not h or h.lower() in have:
                continue
            near = " ".join(lines[max(0, i - 1):i + 2]).lower()  # this line and its neighbours
            plat = next((p for p, w in PLATFORM_WORDS.items() if re.search(w, near)), None)
            out.append((plat, h))
            have.add(h.lower())
    return out


def ollama_frame_text(cfg, picture):
    """The text the model can read in one frame ("" if none). It only
    transcribes (asked for account names directly, small models tend to make
    one up); names are picked out of the text here."""
    ai = cfg["ai"]
    body = {
        "model": ai["model"], "stream": False, "keep_alive": "15m", "options": {"temperature": 0},
        "messages": [{"role": "user", "content": (
            "Transcribe all text that is visible in this image (watermarks, captions, usernames, signs), "
            "exactly as written, one item per line. If there is no readable text at all, answer exactly: NONE"),
            "images": [base64.b64encode(picture).decode()]}],
    }
    for attempt in (1, 2):
        req = urllib.request.Request(ai["url"] + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                reply = json.loads(resp.read())
            break
        except urllib.error.HTTPError:
            if attempt == 2:
                raise
            time.sleep(1)
    text = ((reply.get("message") or {}).get("content") or "").strip()
    return "" if text.upper().startswith("NONE") else text


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def name_patterns(name):
    """Regexes for a name as it could be written in a watermark: as is, or
    without its spaces (netvideogirls.com), symbols kept (PORN+ isn't "porn")."""
    low = " ".join((name or "").lower().split())
    variants = {low}
    if " " in low:
        variants.add(low.replace(" ", ""))
    return [re.compile(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])") for v in variants if v]


def accounts_context(stash, plugin_dir=None):
    """What a scan matches against, loaded once per run: all studios, the
    existing studios whose name could show up as a watermark, the
    performers by name / alias / profile link, and account names read in
    one scene's watermark so far (waiting for a second scene)."""
    studios = stash.call("{ findStudios(filter: {per_page: -1}) { studios { id name aliases urls } } }")["findStudios"]["studios"]
    platform_names = {v[0].lower() for v in PLATFORMS.values()}
    marks = []  # (pattern, studio)
    for st in studios:
        if st["name"].lower() in platform_names:
            continue
        for n in [st["name"]] + (st.get("aliases") or []):
            if len(re.sub(r"[^a-z0-9]", "", n.lower())) >= 4:
                marks += [(pat, st) for pat in name_patterns(n)]
    perfs = stash.call("{ findPerformers(filter: {per_page: -1}) { performers { id name alias_list urls } } }")["findPerformers"]["performers"]
    by_key, perf_marks = {}, []
    for p in perfs:
        for n in [p["name"]] + (p.get("alias_list") or []):
            k = re.sub(r"[^a-z0-9]", "", (n or "").lower())
            if len(k) >= 4:
                by_key.setdefault(k, p)
            # names worth looking for in a watermark: two words or more. (A
            # one-word name or alias - "Christian", "Summer", "Chanel" - turns
            # up in any caption or magazine page.)
            if len([w for w in (n or "").split() if len(w) >= 2]) >= 2:
                perf_marks += [(pat, p) for pat in name_patterns(n)]
    pending_path = os.path.join(plugin_dir, "handle_pending.json") if plugin_dir else None
    pending = {}
    if pending_path:
        try:
            with open(pending_path) as fh:
                pending = json.load(fh)
        except (OSError, ValueError):
            pending = {}
    # tag and gallery names: a new studio for an account takes the spelling
    # you already use for it (a gallery "Kasujinn" for watermarks "KUSUJINN")
    words = [t["name"] for t in stash.call("{ findTags(filter: {per_page: -1}) { tags { name } } }")["findTags"]["tags"]]
    words += [g["title"] for g in stash.call("{ findGalleries(filter: {per_page: -1}) { galleries { title } } }")["findGalleries"]["galleries"]
              if g.get("title")]
    return {"studios": studios, "marks": marks, "perf_keys": by_key, "perfs": perfs, "perf_marks": perf_marks,
            "pending": pending, "pending_path": pending_path, "words": words}


# Watermarks read by a small model come out slightly wrong now and then
# ("f1nnster", "flnn5ter", "Ann5ter" for f1nn5ter). A name read off frames is
# compared with the studios and performers you have, letters that look alike
# counted as the same, and a near miss is taken as that one.
OCR_LOOKALIKE = str.maketrans({"0": "o", "1": "l", "i": "l", "5": "s", "3": "e", "8": "b"})


def ocr_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower()).translate(OCR_LOOKALIKE)


def edit_distance(a, b, most):
    """Letters to change, add or drop to turn a into b (most + 1 if more)."""
    if abs(len(a) - len(b)) > most:
        return most + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > most:
            return most + 1
        prev = cur
    return prev[-1]


def misread_allowance(a, b):
    """How far off a misread may be: none for short names (under 5
    letters), 1 letter for names of 5-6, 2 for longer ones."""
    n = min(len(a), len(b))
    return 0 if n < 5 else 1 if n < 7 else 2


def alike(a, b):
    """How alike two names read off pictures are, 0-1 (letters that look
    alike counted as the same): KUSUJOINN / KUSUJINN 0.94."""
    import difflib
    x, y = ocr_key(a), ocr_key(b)
    return difflib.SequenceMatcher(None, x, y).ratio() if x and y else 0.0


ALIKE_SAME = 0.8  # names this alike (and 6+ letters) are taken as one account


def close_name(names, handle):
    """The one of `names` that the handle is most likely a misreading of
    (ALIKE_SAME or more, 6+ letters), or None."""
    if len(ocr_key(handle)) < 6:
        return None
    best = max(((alike(n, handle), n) for n in names if len(ocr_key(n)) >= 6), default=(0, None))
    return best[1] if best[0] >= ALIKE_SAME else None


def known_name_for(ctx, handle):
    """("studio", studio) or ("performer", performer) whose name or alias
    the handle (read off frames) most likely is, or None. A studio wins over
    a performer at the same distance; two different studios (or performers)
    equally close mean it can't be told, so None."""
    key = ocr_key(handle)
    if len(key) < 2:
        return None
    platform_names = {v[0].lower() for v in PLATFORMS.values()}
    cands = []  # (distance, kind order, kind, thing)
    for st in ctx["studios"]:
        if st["name"].lower() in platform_names:
            continue
        for n in [st["name"]] + (st.get("aliases") or []):
            k = ocr_key(n)
            if len(k) >= 4 or k == key:
                d = edit_distance(key, k, misread_allowance(key, k))
                if d <= misread_allowance(key, k):
                    cands.append((d, 0, "studio", st))
    for p in ctx["perfs"]:
        for n in [p["name"]] + (p.get("alias_list") or []):
            k = ocr_key(n)
            if len(k) >= 4 or k == key:
                d = edit_distance(key, k, misread_allowance(key, k))
                if d <= misread_allowance(key, k):
                    cands.append((d, 1, "performer", p))
    if not cands:
        # not a near miss letter for letter, but about the same as a studio
        # you have (a misread with more letters off: KUSUJOINN for KUSUJINN)
        names = {}
        for st in ctx["studios"]:
            if st["name"].lower() in platform_names:
                continue
            for n in [st["name"]] + (st.get("aliases") or []):
                names.setdefault(n, st)
        hit = close_name(list(names), handle)
        return ("studio", names[hit]) if hit else None
    cands.sort(key=lambda c: (c[0], c[1]))
    best = cands[0]
    rivals = {c[3]["id"] for c in cands if c[:2] == best[:2]}
    return (best[2], best[3]) if len(rivals) == 1 else None


def pending_handle(stash, ctx, platform, handle, scene, apply, kind="scene"):
    """An account name read only in this scene's (or photo's) watermark,
    matching nothing you have. It gets a studio once it's been read in a
    second scene or photo (any near spelling counts; the most read spelling
    is used), which the earlier ones then get too. Returns (studio or None,
    new, items waiting). Entries keep "scenes" and "images" id lists."""
    pend = ctx["pending"] if apply else json.loads(json.dumps(ctx["pending"]))
    key = ocr_key(handle)
    if len(key) < 4:  # too short to be sure it's an account ("ess")
        return None, False, []
    match = next((k for k in pend if edit_distance(key, k, misread_allowance(key, k)) <= misread_allowance(key, k)), None)
    if match is None:
        match = close_name(list(pend), key)
    if match is None:
        match = key
        pend[key] = {"platform": platform, "spellings": {}, "scenes": [], "images": []}
    e = pend[match]
    e.setdefault("scenes", [])
    e.setdefault("images", [])
    mine = e[kind + "s"]
    if scene["id"] not in mine:
        mine.append(scene["id"])
    e["spellings"][handle] = e["spellings"].get(handle, 0) + 1
    if len(e["scenes"]) + len(e["images"]) < 2:
        if apply:
            save_pending(ctx)
        return None, False, e["scenes"] + e["images"]
    spelling = max(e["spellings"].items(), key=lambda kv: kv[1])[0]
    # you already have a tag or gallery by about that name: use its spelling
    spelling = close_name(ctx.get("words", []), spelling) or spelling
    st, new = studio_for_handle(stash, ctx["studios"], e["platform"], spelling, apply)
    earlier = {k: [i for i in e[k + "s"] if not (k == kind and i == scene["id"])] for k in ("scene", "image")}
    if apply:
        for k, ids in earlier.items():
            if not ids:
                continue
            q, f, bulk, typ = (("findScenes", "scenes", "bulkSceneUpdate", "BulkSceneUpdateInput") if k == "scene"
                               else ("findImages", "images", "bulkImageUpdate", "BulkImageUpdateInput"))
            still = stash.call(f"query($ids: [ID!]) {{ {q}(ids: $ids, filter: {{per_page: -1}}) {{ {f} {{ id studio {{ id }} }} }} }}",
                               {"ids": ids})[q][f]
            todo = [x["id"] for x in still if not x.get("studio")]
            if todo:
                stash.call(f"mutation($i: {typ}!) {{ {bulk}(input: $i) {{ id }} }}",
                           {"i": {"ids": todo, "studio_id": st["id"]}})
        del pend[match]
        save_pending(ctx)
    return st, new, earlier["scene"] + earlier["image"]


def save_pending(ctx):
    if not ctx.get("pending_path"):
        return
    tmp = ctx["pending_path"] + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(ctx["pending"], fh)
    os.replace(tmp, ctx["pending_path"])


def plat_name(platform):
    """OnlyFans / Fansly / ... - or "account" for a bare @name (no platform read)."""
    return PLATFORMS[platform][0] if platform else "account"


def performer_for_handle(ctx, platform, handle):
    """A performer whose name or alias is the handle (ignoring spaces, dots,
    case), or who has the account's profile link."""
    p = ctx["perf_keys"].get(re.sub(r"[^a-z0-9]", "", handle.lower()))
    if p:
        return p
    if not platform:
        return None
    url = PLATFORMS[platform][1].format(handle).lower()
    tail = url.split("://", 1)[1].replace("www.", "")
    for p in ctx["perfs"]:
        if any(tail == u.lower().rstrip("/").split("://", 1)[-1].replace("www.", "") for u in p.get("urls") or []):
            return p
    return None


def scene_accounts(stash, cfg, ctx, scene, use_ai=True, kind="scene"):
    """What a scene (or photo) shows: ([(platform, handle, where)] best
    first, [(existing studio, frames)] whose name is in the watermark).
    A scene's frames must agree (two of them); a photo is one picture, so
    one read counts - a brand-new account still needs a second item."""
    text = " ".join([scene.get("title") or "", scene.get("details") or "", " ".join(scene.get("urls") or []),
                     item_path(kind, scene)])
    from_text = handles_in_text(text)
    seen = defaultdict(int)  # (platform or None, handle lower) -> frames
    spelled = {}
    marks = defaultdict(int)  # studio id -> frames its name was read in
    mark_studio = {}
    pmarks = defaultdict(int)  # performer id -> frames their name was read in
    mark_perf = {}
    need = 1 if kind == "image" else 2  # reads needed
    f = (scene.get("files") or [None])[0]
    if kind == "image":
        points = (None,) if use_ai else ()
    else:
        points = HANDLE_FRAME_POINTS if use_ai and f and (f.get("duration") or 0) > 1 else ()
    if points:
        for i, at in enumerate(points):
            try:
                pic = (photo_picture(stash, scene) if kind == "image"
                       else grab_frame(stash, f["path"], f["duration"] * at, width=1280)[0])
                frame_text = ollama_frame_text(cfg, pic)
                for plat, h in set(handles_in_ocr(frame_text)):
                    seen[(plat, h.lower())] += 1
                    spelled.setdefault(h.lower(), h)
                low = " ".join(frame_text.lower().split())
                hit = {st["id"]: st for pat, st in ctx["marks"] if pat.search(low)}
                for st_id, st in hit.items():
                    marks[st_id] += 1
                    mark_studio[st_id] = st
                phit = {p["id"]: p for pat, p in ctx["perf_marks"] if pat.search(low)}
                for p_id, p in phit.items():
                    pmarks[p_id] += 1
                    mark_perf[p_id] = p
            except Exception as e:
                if kind == "image":
                    raise
                log("debug", f"frame at {at:.0%}: {e}")
            # stop early: two frames agree (on a name with its platform, or a
            # studio), or three frames show nothing (watermarks don't come and go)
            agreed = defaultdict(int)
            for (plat, hl), n in seen.items():
                agreed[hl] += n
            if any(n >= 2 and any(p and h == hl for p, h in seen) for hl, n in agreed.items()) \
                    or any(n >= 2 for n in marks.values()):
                break
            if i == 2 and not seen and not marks and not pmarks:
                break
    # a handle read without its platform takes the platform it was read with
    # elsewhere
    by_handle = defaultdict(int)
    plat_of = {}
    for (plat, hl), n in seen.items():
        by_handle[hl] += n
        if plat:
            plat_of.setdefault(hl, plat)
    results = []
    for (plat, hl), h in from_text.items():
        results.append((plat, h, "text" + (f" + {by_handle[hl]} frame(s)" if by_handle.get(hl) else "")))
    for hl, n in by_handle.items():
        if any(hl == r[1].lower() for r in results):
            continue
        plat = plat_of.get(hl)
        # a bare "@name" (no platform next to it) counts too - many photo
        # watermarks are just that; a new one still needs a second item
        if n >= need and (plat or len(hl) >= 4):
            results.append((plat, spelled[hl], f"{n} frames"))
    results.sort(key=lambda r: (not r[2].startswith("text"), r[0] is None,
                                PLATFORM_ORDER.index(r[0]) if r[0] else len(PLATFORM_ORDER)))
    studios = sorted(((mark_studio[i], n) for i, n in marks.items() if n >= need), key=lambda x: -x[1])
    # a photo is read once, so its text could be anything (a magazine page):
    # a performer only from a name in two frames of a scene. (An account name
    # that's a performer's still counts, for photos too.)
    performers = [mark_perf[i] for i, n in pmarks.items() if n >= 2] if kind != "image" else []
    return results, studios, performers


def process_scene_accounts(stash, cfg, ctx, scene, apply, use_ai=True, kind="scene"):
    """Give one scene (or photo) its studio (if it has none) and performers
    from the accounts / studio watermarks it shows. Returns (a log line or
    None, whether a new studio was made)."""
    handles, mark_studios, mark_perfs = scene_accounts(stash, cfg, ctx, scene, use_ai, kind)
    name = os.path.basename(item_path(kind, scene)) or f"{kind} {scene['id']}"
    parts, inp, new = [], {"id": scene["id"]}, False
    # names read off frames may be misread: take a near match with what you have
    known = {}  # handle -> ("studio" | "performer", thing)
    for plat, handle, where in handles:
        if not where.startswith("text"):
            hit = known_name_for(ctx, handle)
            if hit:
                known[handle] = hit
    if not scene.get("studio"):
        if mark_studios:
            st, n = mark_studios[0]
            parts.append(f'studio "{st["name"]}" [its watermark' + (", in the picture]" if kind == "image" else f", in {n} frames]"))
            inp["studio_id"] = st["id"]
        elif handles:
            plat, handle, where = handles[0]
            hit, thing = known.get(handle, (None, None))
            src = f" [{plat_name(plat)} @{handle}, from {where}"
            if hit == "studio":
                st = thing
                parts.append(f'studio "{st["name"]}"' + src + ("" if st["name"] == handle else f', taken as "{st["name"]}"') + "]")
                inp["studio_id"] = st["id"]
            elif hit == "performer" or where.startswith("text"):
                spelling = thing["name"] if hit == "performer" else handle
                st, new = studio_for_handle(stash, ctx["studios"], plat, spelling, apply, with_url=hit != "performer")
                parts.append(f'studio "{st["name"]}"' + (f" (new, under {plat_name(plat)})" if new and plat else " (new)" if new else "")
                             + src + (f', taken as performer "{thing["name"]}"' if hit == "performer" else "") + "]")
                inp["studio_id"] = st["id"]
            else:  # a new name, only in this scene's watermark so far
                st, new, earlier = pending_handle(stash, ctx, plat, handle, scene, apply, kind)
                if st:
                    parts.append(f'studio "{st["name"]}"' + (f" (new, under {plat_name(plat)})" if new and plat else " (new)" if new else "")
                                 + src + f", also read in {len(earlier)} earlier item(s), which get it too]")
                    inp["studio_id"] = st["id"]
                else:
                    log("info", f"{name}: read {plat_name(plat)} @{handle} in the watermark; a studio is made "
                        "once it's read in a second scene or photo too (misreads rarely repeat)")
    have = {p["id"] for p in scene.get("performers") or []}
    add = []
    by_handle = [known[h][1] if known.get(h, ("",))[0] == "performer" else performer_for_handle(ctx, plat, h)
                 for plat, h, _ in handles]
    for p in by_handle + mark_perfs:
        if p and p["id"] not in have and p["id"] not in [x["id"] for x in add]:
            add.append(p)
    if add:
        parts.append(("performers " if len(add) > 1 else "performer ") + ", ".join(f'"{p["name"]}"' for p in add)
                     + " [account name / name in the watermark]")
        inp["performer_ids"] = sorted(have | {p["id"] for p in add}, key=int)
    if not parts:
        return None, False
    if apply and len(inp) > 1:
        if kind == "image":
            stash.call("mutation($i: ImageUpdateInput!) { imageUpdate(input: $i) { id } }", {"i": inp})
        else:
            stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }", {"i": inp})
    others = ", ".join(f"{plat_name(p)} @{h}" for p, h, _ in handles[1:])
    return f'{"Would give" if not apply else "Gave"} {name} ' + " and ".join(parts) + (f"; also saw {others}" if others else ""), new


def studio_for_handle(stash, studios, platform, handle, apply, with_url=True):
    """The studio for an account: an existing one with that name, alias or
    profile URL, else a new one under the platform's studio. `studios` is
    the list of all studios (kept up to date). The platform's studio is set
    to be ignored by Auto Tag, or every file with "OnlyFans" in its name
    would get it."""
    parent_name, url_fmt, _ = PLATFORMS[platform] if platform else (None, None, None)
    url = url_fmt.format(handle) if url_fmt else None
    low = handle.lower()
    for st in studios:
        names = [st["name"].lower()] + [a.lower() for a in st.get("aliases") or []]
        if low in names or (url and any(u.rstrip("/").lower() == url.lower() for u in st.get("urls") or [])):
            return st, False
    if not apply:
        return {"id": None, "name": handle}, True
    if not platform:  # a bare @name: a studio of its own, under no platform
        st = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id name aliases urls } }",
                        {"i": {"name": handle}})["studioCreate"]
        studios.append(st)
        return st, True
    parent = next((st for st in studios if st["name"].lower() == parent_name.lower()), None)
    if not parent:
        parent = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id name aliases urls } }",
                            {"i": {"name": parent_name, "ignore_auto_tag": True}})["studioCreate"]
        studios.append(parent)
    st = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id name aliases urls } }",
                    {"i": {"name": handle, "parent_id": parent["id"], **({"urls": [url]} if with_url else {})}})["studioCreate"]
    studios.append(st)
    return st, True


def accounts_needed(scene):
    """Worth reading the scene's watermarks? Not when it already has a
    studio and performers: that's 6 AI model calls with nothing to find."""
    return not scene.get("studio") or not scene.get("performers")


def run_accounts_items(stash, plugin_dir, scene_ids, kind="scene"):
    """New scenes (or photos) from a scan: studio and performers from
    accounts."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    ctx = accounts_context(stash, plugin_dir)
    q, f = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
    items = stash.call(f"query($ids: [ID!]) {{ {q}(ids: $ids, filter: {{per_page: -1}}) {{ {f} {{ {HANDLE_FIELDS[kind]} }} }} }}",
                       {"ids": scene_ids})[q][f]
    skip = [sc for sc in items if not accounts_needed(sc)]
    if skip:
        log("info", f"Watermarks: skipping {len(skip)} {kind}(s) that already have a studio and performers")
    for sc in (sc for sc in items if accounts_needed(sc)):
        try:
            line, _ = process_scene_accounts(stash, cfg, ctx, sc, apply=True, kind=kind)
            if line:
                log("info", line)
        except Exception as e:
            log("warning", f"{kind} {sc['id']}: accounts skipped ({e})")


def run_photo_studios(stash, plugin_dir, in_galleries=False, preview=False, limit=None):
    """Task: photos without a studio get one from the account or studio
    watermark the AI model reads in them (one model call each). Photos in a
    gallery (comic pages, sets) only with `in_galleries`. Photos already
    read are skipped next time (handle_scan.json)."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    flt = {"studios": {"modifier": "IS_NULL"}}
    if not in_galleries:
        flt["galleries"] = {"modifier": "IS_NULL"}
    photos, page = [], 1
    while True:
        got = stash.call(
            f"query($p: ImageFilterType, $f: FindFilterType) {{ findImages(image_filter: $p, filter: $f) {{ images {{ {HANDLE_IMAGE_FIELDS} }} }} }}",
            {"p": flt, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}})["findImages"]["images"]
        photos += [x for x in got if not x.get("studio")]
        if len(got) < 500:
            break
        page += 1
    done_path = os.path.join(plugin_dir, "handle_scan.json")
    try:
        with open(done_path) as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    done = set(saved.get("images", []))
    if limit:
        import random
        photos = random.sample(photos, min(limit, len(photos)))
    else:
        photos = [x for x in photos if x["id"] not in done]
    ctx = accounts_context(stash, plugin_dir)
    log("info", f"Reading watermarks in {len(photos)} photo(s) without a studio"
        + ("" if in_galleries else " (not in a gallery)") + (" (preview, nothing is saved)" if preview else "")
        + (". A few seconds each, so this takes a while; you can cancel it under Settings → Tasks and run it again to carry on."
           if len(photos) > 20 else "."))
    assigned = created = failed = 0
    for n, ph in enumerate(photos):
        progress(n / max(len(photos), 1))
        name = os.path.basename(item_path("image", ph)) or f"image {ph['id']}"
        try:
            line, new = process_scene_accounts(stash, cfg, ctx, ph, apply=not preview, kind="image")
        except RuntimeError:
            raise
        except Exception as e:
            failed += 1
            log("warning" if failed <= 3 else "debug", f"{name}: skipped ({e})")
            continue
        if line:
            log("info", line)
            assigned += 1
            created += new
        if (n + 1) % 50 == 0:
            log("info", f"Read {n + 1} of {len(photos)} photo(s): {assigned} got a studio so far ({created} new), "
                f"{len(ctx['pending'])} account name(s) waiting to be seen a second time")
        if not preview and not limit:
            done.add(ph["id"])
            if n % 10 == 9 or n == len(photos) - 1:
                with locked_json(done_path) as d:
                    d["images"] = sorted(set(d.get("images", [])) | done, key=int)
    progress(1)
    log("info", f"{'Preview: ' if preview else 'Done: '}{assigned} of {len(photos)} photo(s) "
        + ("would get" if preview else "got") + f" a studio and/or performers from a watermark ({created} new studio(s))"
        + (f"; {failed} couldn't be read" if failed else "") + (". Nothing was saved." if preview else "."))


def run_handle_studios(stash, plugin_dir, preview=False, limit=None, use_ai=True):
    """Task: scenes without a studio get one for the account they come from."""
    cfg = settings(stash, plugin_dir)
    if use_ai:
        ai_check(cfg)
    scenes = []
    page = 1
    while True:
        items = stash.call(
            f"query($p: SceneFilterType, $f: FindFilterType) {{ findScenes(scene_filter: $p, filter: $f) {{ scenes {{ {HANDLE_SCENE_FIELDS} }} }} }}",
            {"p": {"studios": {"value": [], "modifier": "IS_NULL"}},
             "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}},
        )["findScenes"]["scenes"]
        scenes += [s_ for s_ in items if not s_.get("studio")]
        if len(items) < 500:
            break
        page += 1
    done_path = os.path.join(plugin_dir, "handle_scan.json")
    try:
        with open(done_path) as fh:
            done = set(json.load(fh).get("scenes", []))
    except (OSError, ValueError):
        done = set()
    if limit:
        import random
        scenes = random.sample(scenes, min(limit, len(scenes)))
    elif use_ai:  # the AI pass skips scenes it has already looked at
        scenes = [s_ for s_ in scenes if s_["id"] not in done]
    ctx = accounts_context(stash, plugin_dir)
    log("info", f"Looking for OnlyFans / Fansly / Patreon / Instagram / TikTok accounts and studio watermarks in {len(scenes)} scene(s) without a studio"
        + (" (preview, nothing is saved)" if preview else "")
        + (". The AI reads up to 6 frames of each, so this takes a while; you can cancel it under Settings → Tasks and run it again to carry on."
           if use_ai and len(scenes) > 20 else "."))
    assigned = created = 0
    for n, sc in enumerate(scenes):
        progress(n / max(len(scenes), 1))
        name = os.path.basename(item_path("scene", sc)) or f"scene {sc['id']}"
        try:
            line, new = process_scene_accounts(stash, cfg, ctx, sc, apply=not preview, use_ai=use_ai)
        except Exception as e:
            log("warning", f"{name}: skipped ({e})")
            continue
        if line:
            log("info", line)
            assigned += 1
            created += new
        if not preview and not limit and use_ai:
            done.add(sc["id"])
            if n % 10 == 9 or n == len(scenes) - 1:
                # (merged: the photo task keeps its own list in the same file)
                with locked_json(done_path) as d:
                    d["scenes"] = sorted(set(d.get("scenes", [])) | done, key=int)
    progress(1)
    log("info", f"{'Preview: ' if preview else 'Done: '}{assigned} of {len(scenes)} scene(s) "
        + ("would get" if preview else "got") + f" a studio and/or performers from an account or watermark ({created} new studio(s))"
        + (". Nothing was saved." if preview else "."))


# --- faces: who's in it ---------------------------------------------------
# The face helper (a small local service, see the add-on's face-helper
# folder) finds faces and turns each into a fingerprint (128 numbers). Two
# faces whose fingerprints point the same way (a high "likeness") are
# probably the same person.
#
# face_index.json - each performer's fingerprints: from their profile
#   picture and a few photos where they're alone ("embs"), from scenes and
#   images you've tagged them in ("tagged"), and from matches you accepted
#   with "Find performers by face" ("conf").
# face_items.json - the faces found in each scene (a few frames) and image,
#   so they're only looked at once ("Faces: scan scenes and images").
# Fingerprints are kept as 128 small whole numbers (base64), which is plenty
# for comparing and keeps the files small.

FACE_FRAME_POINTS = (0.1, 0.25, 0.4, 0.55, 0.7, 0.85)
FACE_EXTRA_PHOTOS = 3
FACE_MAX_TAGGED = 12   # fingerprints learned from tagged items, per performer
FACE_MAX_CONF = 12     # ... and from accepted matches
FACE_SAME_AS_PROFILE = 0.45  # a tagged face must be this alike to count as them
FACE_MARGIN = 0.05     # a confident match is this far ahead of anyone else
FACE_DUPLICATE_MIN = 0.55
FACE_MAX_IMAGE_BYTES = 15 * 1024 * 1024
FACE_LOOKALIKE_MIN = 0.45  # "Might also be in" lists the whole library, so it's stricter


def face_helper(cfg, pictures):
    """Faces in each picture: [[{box, score, emb}], ...]."""
    body = json.dumps({"images": [base64.b64encode(p).decode() for p in pictures]}).encode()
    for attempt in range(4):  # a moment's network blip shouldn't stop a long task
        req = urllib.request.Request(cfg["ai"]["face_url"] + "/faces", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())["results"]
        except urllib.error.HTTPError as e:
            raise ValueError(f"the face helper couldn't read it ({e.code})")  # this item only
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (BrokenPipeError, ConnectionResetError)):
                raise ValueError(f"the face helper dropped it ({reason})")  # e.g. too big: this item only
            if attempt < 3:
                time.sleep(2 + attempt * 2)
                continue
            raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({reason}). "
                               "Is it running? See the add-on's README.")


def _dot(a, b):
    return sum(map(operator.mul, a, b))


def _biggest(faces):
    return max(faces, key=lambda f: f["box"][2] * f["box"][3]) if faces else None


def q8(emb):
    """A fingerprint as 128 bytes (base64): each number as a whole -127..127."""
    return base64.b64encode(bytes((max(-127, min(127, round(v * 127))) & 0xFF) for v in emb)).decode()


def unq8(s):
    return [(b - 256 if b > 127 else b) / 127 for b in base64.b64decode(s)]


def known_embs(entry):
    """All of a performer's fingerprints (decoded)."""
    return entry.get("embs", []) + [unq8(x) for x in entry.get("tagged", []) + entry.get("conf", [])]


def locked_face_index(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "face_index.json"))


def load_face_index(plugin_dir):
    try:
        with open(os.path.join(plugin_dir, "face_index.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update_face_index(stash, cfg, plugin_dir, only=None, quiet=False):
    """Learn the faces of performers that are new or got a new picture (what
    was learned from tagged items and accepted matches is kept). Returns the
    index {performer id: {"name", "img", "embs", "tagged", "conf"}}."""
    perfs = stash.call("{ findPerformers(filter: {per_page: -1}) { performers { id name image_path } } }")["findPerformers"]["performers"]
    if only:
        perfs = [p for p in perfs if p["id"] in only]
    with locked_face_index(plugin_dir) as index:
        live = {p["id"] for p in perfs}
        if not only:
            for pid in list(index):
                if pid not in live:
                    del index[pid]
        todo = [p for p in perfs if p.get("image_path") and "default=true" not in p["image_path"]
                and (index.get(p["id"]) or {}).get("img") != p["image_path"]]
        learned = 0
        for n, p in enumerate(todo):
            if not quiet:
                progress(n / max(len(todo), 1))
            try:
                data, ctype = stash.fetch(p["image_path"])
                main = _biggest(face_helper(cfg, [data])[0]) if ctype.startswith("image/") else None
                embs = [main["emb"]] if main else []
                if main:
                    # a few photos where they're the only performer, if the
                    # face there looks like the profile picture
                    imgs = stash.call(
                        "query($p: ImageFilterType) { findImages(image_filter: $p, filter: {per_page: 12, sort: \"random\"}) "
                        "{ images { paths { thumbnail } } } }",
                        {"p": {"performers": {"value": [p["id"]], "modifier": "INCLUDES"},
                               "performer_count": {"value": 1, "modifier": "EQUALS"}}})["findImages"]["images"]
                    pics = []
                    for im in imgs[:6]:
                        try:
                            d, ct = stash.fetch(im["paths"]["thumbnail"])
                            if ct.startswith("image/"):
                                pics.append(d)
                        except Exception:
                            pass
                    for faces in (face_helper(cfg, pics) if pics else []):
                        f = _biggest(faces)
                        if f and _dot(f["emb"], main["emb"]) >= FACE_SAME_AS_PROFILE and len(embs) < 1 + FACE_EXTRA_PHOTOS:
                            embs.append(f["emb"])
                old = index.get(p["id"]) or {}
                index[p["id"]] = {"name": p["name"], "img": p["image_path"], "embs": embs,
                                  "tagged": old.get("tagged", []), "conf": old.get("conf", []), "not": old.get("not", [])}
                learned += bool(embs)
            except RuntimeError:
                raise
            except Exception as e:
                log("debug", f"{p['name']}: {e}")
        for p in perfs:  # keep names current
            if p["id"] in index:
                index[p["id"]]["name"] = p["name"]
        if todo and not quiet:
            log("info", f"Learned the faces of {learned} of {len(todo)} new or changed performer(s) "
                f"({sum(1 for v in index.values() if known_embs(v))} known in all)")
        return dict(index)


# ---- the faces in scenes and images (kept in face_items.json) ----

def item_face_pictures(stash, kind, item):
    """Pictures to look for faces in: a few frames of a scene, or an image."""
    if kind == "image":
        if not all(f["__typename"] == "ImageFile" for f in item.get("visual_files") or []):
            return []
        data, ctype = stash.fetch(item["paths"]["image"])
        if len(data) > FACE_MAX_IMAGE_BYTES:  # a huge photo: its thumbnail is plenty
            data, ctype = stash.fetch(item["paths"]["thumbnail"])
        return [data] if ctype.startswith("image/") else []
    f = (item.get("files") or [None])[0]
    if not f or not (f.get("duration") or 0) > 1:
        return []
    out = []
    for at in FACE_FRAME_POINTS:
        try:
            out.append(grab_frame(stash, f["path"], f["duration"] * at, width=1280)[0])
        except Exception as e:
            log("debug", f"frame at {at:.0%}: {e}")
    return out


FACE_ITEM_FIELDS = {
    "scene": "id title performers { id } files { path duration }",
    "image": "id title performers { id } paths { image thumbnail } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
}


class FaceCache:
    """face_items.json, read once; new results are merged back in batches
    (so a long scan never holds the file locked)."""

    def __init__(self, plugin_dir):
        self.path = os.path.join(plugin_dir, "face_items.json")
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.new = {}

    def groups(self, stash, cfg, kind, item, compute=True):
        """The faces in each picture of the item: [[fingerprint, ...], ...]
        (None if it isn't scanned yet and compute is off)."""
        key, sig = f"{kind}:{item['id']}", item_path(kind, item)
        hit = self.new.get(key) or self.data.get(key)
        if hit and hit.get("p") == sig:
            return [[unq8(x) for x in g] for g in hit["f"]]
        if not compute:
            return None
        pics = item_face_pictures(stash, kind, item)
        found = face_helper(cfg, pics) if pics else []
        groups = [[f["emb"] for f in faces] for faces in found]
        self.new[key] = {"p": sig, "f": [[q8(e) for e in g] for g in groups]}
        if len(self.new) >= 25:
            self.save()
        return groups

    def save(self):
        if not self.new:
            return
        with locked_json(self.path) as data:
            data.update(self.new)
            self.data = dict(data)
        self.new = {}


def face_candidates(stash, cfg, index, kind, item, cache=None):
    """Performers who look like the people in it, best first:
    [{"id", "name", "score", "pictures", "emb"}] - score is the best likeness
    (0-1), pictures how many frames / faces they were the closest match in,
    emb the face that matched best (to learn from if you accept it)."""
    groups = cache.groups(stash, cfg, kind, item) if cache else None
    if groups is None:
        pics = item_face_pictures(stash, kind, item)
        groups = [[f["emb"] for f in faces] for faces in (face_helper(cfg, pics) if pics else [])]
    known = [(pid, v["name"], known_embs(v)) for pid, v in index.items()]
    known = [k for k in known if k[2]]
    best = {}  # performer id -> {...}
    for faces in groups:
        seen_here = set()
        for emb in faces:
            scored = sorted(((max(_dot(emb, e) for e in embs), pid, name) for pid, name, embs in known), reverse=True)
            for score, pid, name in scored[:3]:
                if score < cfg["ai"]["face_min"]:
                    break
                b = best.setdefault(pid, {"id": pid, "name": name, "score": 0.0, "pictures": 0, "emb": None})
                if score > b["score"]:
                    b["score"], b["emb"] = score, q8(emb)
                if pid not in seen_here and score == scored[0][0]:
                    b["pictures"] += 1
                    seen_here.add(pid)
    return sorted(best.values(), key=lambda b: (-b["pictures"] if kind == "scene" else 0, -b["score"]))


def confident(cfg, kind, c, cands):
    """Good enough to add without asking: a high likeness, clearly ahead of
    everyone else who looks similar (look-alikes are close together), and in
    a scene the closest match in at least two frames, and in the most."""
    others = [o for o in cands if o["id"] != c["id"]]
    # one photo is weaker evidence than several frames of a scene
    if c["score"] < cfg["ai"]["face_auto_min"] + (0.05 if kind == "image" else 0):
        return False
    if others and c["score"] - max(o["score"] for o in others) < FACE_MARGIN:
        return False
    if kind == "scene" and (c["pictures"] < 2 or any(o["pictures"] >= c["pictures"] for o in others)):
        return False
    return True


def run_face_suggest(stash, plugin_dir, kind, item_id):
    """Asked by the edit page: who it looks like. Nothing is saved (except
    the faces found, so they're not looked for again)."""
    cfg = settings(stash, plugin_dir)
    index = update_face_index(stash, cfg, plugin_dir, quiet=True)
    query = "findScene" if kind == "scene" else "findImage"
    item = stash.call(f"query($id: ID!) {{ {query}(id: $id) {{ {FACE_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[query]
    if not item:
        raise RuntimeError(f"{kind} {item_id} not found")
    cache = FaceCache(plugin_dir)
    cands = face_candidates(stash, cfg, index, kind, item, cache)
    cache.save()
    sure = {c["id"] for c in cands if confident(cfg, kind, c, cands)}
    return [{**c, "score": round(c["score"] * 100), "sure": c["id"] in sure} for c in cands[:6]]


def _add_fingerprint(entry, key, emb_b64, cap):
    """Add a fingerprint unless it's a near copy of one already there."""
    emb = unq8(emb_b64)
    have = [unq8(x) for x in entry.get(key, [])]
    if any(_dot(emb, h) >= 0.92 for h in have):
        return False
    entry[key] = (entry.get(key, []) + [emb_b64])[-cap:]
    return True


def run_face_confirm(plugin_dir, performer_id, emb_b64):
    """You accepted a face match: remember that face as theirs too."""
    if not emb_b64:
        return False
    with locked_face_index(plugin_dir) as index:
        entry = index.get(str(performer_id))
        if not entry:
            return False
        return _add_fingerprint(entry, "conf", emb_b64, FACE_MAX_CONF)


def run_face_reject(plugin_dir, performer_id, key):
    """"Not them" on a performer's "Might also be in": don't suggest that
    scene / image for them again."""
    with locked_face_index(plugin_dir) as index:
        entry = index.get(str(performer_id))
        if not entry or not key:
            return False
        nots = entry.get("not", [])
        if key not in nots:
            entry["not"] = (nots + [key])[-2000:]
        return True


def look_knn(cfg, vectors, base, k=LOOK_K):
    """The face helper's /knn: each vector's k most alike base vectors."""
    body = json.dumps({"vectors": vectors, "base": base, "k": k}).encode()
    req = urllib.request.Request(cfg["ai"]["face_url"] + "/knn", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("the face helper is too old for tag review - copy the add-on's face-helper/face_helper.py "
                               "over the running one and restart it (see the README)")
        raise RuntimeError(f"the face helper couldn't compare pictures ({e.code})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({getattr(e, 'reason', e)})")


TAG_REVIEW_CACHE_SECONDS = 15 * 60


def locked_tag_review_not(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "tag_review_not.json"))


def run_tag_review_answer(plugin_dir, key, tag_id, no=True):
    """Tag review's "No" (or its Undo): don't suggest that tag for that item
    again. tag_review_not.json: {"scene:12": [tag ids]}."""
    with locked_tag_review_not(plugin_dir) as data:
        have = data.get(key, [])
        if no and str(tag_id) not in have:
            data[key] = have + [str(tag_id)]
        elif not no and str(tag_id) in have:
            data[key] = [t for t in have if t != str(tag_id)]
            if not data[key]:
                del data[key]
    return True


def _tag_review_candidates(stash, cfg, plugin_dir, excluded, fresh=False):
    """Every confident look-alike tag (LOOK_SURE+) for every fingerprinted
    item, whatever it has now: [[key, tag id, score, look-alikes with it]].
    The comparing is done by the face helper; kept for 15 minutes."""
    path = os.path.join(plugin_dir, "tag_review_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                return saved["found"], saved["names"]
    looks = LookIndex(stash, cfg, plugin_dir, excluded)
    if not looks.ready():
        raise RuntimeError('not enough look-alikes yet - run "Look-alike tags: scan scenes and images" first')
    keys = [k for k, ent in looks.cache.data.items() if ent.get("e")]
    pos = {k: i for i, k in enumerate(keys)}
    rows = {pos[r[0]]: r for r in looks.rows if r[0] in pos}
    nn = look_knn(cfg, [looks.cache.data[k]["e"] for k in keys], sorted(rows), LOOK_K)
    found = []
    for key, near in zip(keys, nn):
        if not near:
            continue
        top = near[0][1] / 16129
        out, total, count = defaultdict(float), 0.0, Counter()
        for j, dot in near:
            w = math.exp((dot / 16129 - top) / LOOK_TEMP)
            total += w
            for t in rows[j][2]:
                out[t] += w
                count[t] += 1
        for t, w in out.items():
            sc = w / total
            if sc >= LOOK_SURE and t in looks.names and t not in looks.rejected:
                found.append([key, t, round(sc, 3), count[t]])
    names = looks.names
    tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "found": found, "names": names}, f)
    os.replace(tmp, path)
    return found, names


TAG_REVIEW_FIELDS = {
    "scene": "id title tags { id } files { basename } paths { screenshot }",
    "image": "id title tags { id } visual_files { ... on BaseFile { basename } } paths { image }",
}


def run_tag_review(stash, plugin_dir, tag=None, limit=40, skip=(), fresh=False):
    """Tag review page: confident look-alike tags (the ones the tasks would
    add, 50%+ of the closest look-alikes' vote) that items don't have yet and
    you haven't turned down. Body tags are left out (the body detector does
    those), as are "Tags to ignore", gallery-name tags and tags you've
    mostly removed. Best first. `skip`: "kind:id:tag" already shown."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    found, names = _tag_review_candidates(stash, cfg, plugin_dir, excluded, fresh)
    have = all_item_tags(stash)
    with locked_tag_review_not(plugin_dir) as nots:
        nots = {k: set(v) for k, v in nots.items()}
    skip = set(skip or ())
    want = str(tag) if tag else None
    picks = []
    for key, t, sc, n in found:
        if want and t != want:
            continue
        if t in excluded or names.get(t, "").strip().lower() in BODY_TAG_NAMES:
            continue
        if t in have.get(key, ()) or t in nots.get(key, ()) or f"{key}:{t}" in skip:
            continue
        picks.append((sc, n, key, t))
    picks.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for at in range(0, len(picks), 150):
        chunk = picks[at:at + 150]
        items = {}
        for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            ids = sorted({k.split(":")[1] for _, _, k, _ in chunk if k.startswith(kind + ":")}, key=int)
            if ids:
                got = find_by_ids(stash, query, field, ids, f"{TAG_REVIEW_FIELDS[kind]}")
                items.update({f"{kind}:{x['id']}": x for x in got})
        for sc, n, key, t in chunk:
            it = items.get(key)
            if not it or t in {x["id"] for x in it.get("tags") or []}:
                continue
            kind, iid = key.split(":")
            files = it.get("files") or it.get("visual_files") or [{}]
            link = (it.get("paths") or {}).get("screenshot" if kind == "scene" else "image") or ""
            out.append({
                "kind": kind, "id": iid, "tag": t, "name": names.get(t, ""), "score": round(sc * 100), "near": n,
                "title": it.get("title") or (files[0] or {}).get("basename") or "",
                # the page is opened from the phone too: no localhost address
                "picture": re.sub(r"^https?://[^/]+", "", link), "tags": len(it.get("tags") or []),
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # the Batch view: how many there are for each tag, biggest first
    per = Counter(t for _, _, _, t in picks)
    groups = [{"id": t, "name": names.get(t, t), "count": c} for t, c in per.most_common()]
    return {"items": out, "total": len(picks), "groups": groups}


# --- new performers: the same unknown face in several items --------------
NEW_PERF_UNKNOWN = 0.45   # a face this far from everyone you have is someone new
NEW_PERF_SAME = 0.6       # faces this alike are put together as one person (0.5 mixed people
                          # up: the same performer on only 71% of a group's tagged items; 0.6: 90%)
NEW_PERF_MIN_ITEMS = 3
# drawn faces all look alike to the face model: comics, cartoons... are left out
NEW_PERF_DRAWN_TAGS = {"animated", "animation", "comics", "comic", "hentai", "sketches", "sketch", "cartoon",
                       "anime", "manga", "doujinshi", "drawn", "drawing", "3d", "ai", "art", "illustration",
                       "black and white", "captions"}
NEW_PERF_GENERIC_FOLDER = re.compile(r"(?i)(^[\d_\W]|locked|folder|download|dcim|camera|^img|pictures?$|photos?$|"
                                     r"videos?$|^misc|^new\b|^stuff|^tmp|^temp)")


def face_cluster(cfg, faces, threshold=NEW_PERF_SAME):
    body = json.dumps({"faces": faces, "threshold": threshold}).encode()
    req = urllib.request.Request(cfg["ai"]["face_url"] + "/cluster", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("the face helper is too old for new performers - copy the add-on's face-helper/face_helper.py "
                               "over the running one and restart it (see the README)")
        raise RuntimeError(f"the face helper couldn't group faces ({e.code})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({getattr(e, 'reason', e)})")


def locked_new_perf_not(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "new_perf_not.json"))


def run_new_perf_answer(plugin_dir, keys, no=True):
    """"Not a performer" (or its Undo) for a group of items."""
    keys = sorted(set(keys or []))
    with locked_new_perf_not(plugin_dir) as data:
        groups = data.setdefault("groups", [])
        if no and keys not in groups:
            groups.append(keys)
        elif not no:
            data["groups"] = [g for g in groups if g != keys]
    return True


NEW_PERF_FIELDS = {
    "scene": "id title details urls performers { id name } studio { id name parent_studio { name } } tags { id name } "
             "files { path } paths { screenshot }",
    "image": "id title details urls performers { id name } studio { id name parent_studio { name } } tags { id name } "
             "visual_files { ... on BaseFile { path } } paths { thumbnail image }",
}


NEW_PERF_NAME_LIKE = re.compile(r"^[A-Z][\w'.-]*(?: [A-Z][\w'.-]*){0,2}$")


def new_perf_names(xs, n, studios, studio_names, folders, tag_uses, path_of, tag_people=None):
    """Name ideas for a new performer from what most of their items share:
    an account named in the file names / titles / links, a studio, a tag
    that looks like a name and is used (nearly) only on them, or a named
    folder. [{name, why}] best first, at most 3."""
    out, seen = [], set()

    def add(name, why, strength, url=None):
        name = " ".join(str(name or "").split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "why": why, "k": strength, "url": url})

    quarter = max(2, -(-n // 4))
    accounts, spelled = Counter(), {}
    for x in xs:
        text = " ".join([x.get("title") or "", x.get("details") or "", " ".join(x.get("urls") or []), path_of(x)])
        for (plat, hl), h in handles_in_text(text).items():
            accounts[(plat, hl)] += 1
            spelled.setdefault((plat, hl), h)
    for (plat, hl), c in accounts.most_common(2):
        if c >= quarter:
            add(spelled[(plat, hl)], f"{c} of them name the {PLATFORMS[plat][0]} account @{spelled[(plat, hl)]}", 3 + c / n,
                PLATFORMS[plat][1].format(spelled[(plat, hl)]))
    for sid, c in studios.most_common(2):
        if c >= quarter:
            add(studio_names[sid], f"{c} of them are from the studio {studio_names[sid]}", 2 + c / n)
    tags = Counter((t["id"], t["name"]) for x in xs for t in x.get("tags") or [])
    for (tid, tname), c in tags.most_common(12):
        if c * 2 < n or not NEW_PERF_NAME_LIKE.match(tname.strip()):
            continue
        if tname.strip().lower() in BODY_TAG_NAMES or tname.strip().lower() in NEW_PERF_DRAWN_TAGS:
            continue
        if tag_uses.get(tid, 0) - c > 4 * n:  # all over the library: a category, not a name
            continue
        if tag_people is not None and len(tag_people.get(tid, ())) >= 3:  # on many people: a description
            continue
        add(tname, f"{c} of them are tagged {tname}", 1.5 + c / n)
    fd = folders.most_common(1)
    # (a folder with numbers in its name is a series or a date, not a person)
    if fd and fd[0][0] and fd[0][1] * 2 >= n and not NEW_PERF_GENERIC_FOLDER.search(fd[0][0]) and not re.search(r"\d", fd[0][0]):
        add(re.sub(r"[_.]+", " ", fd[0][0]).strip(), f"{fd[0][1]} of them are in the folder \"{fd[0][0]}\"", 1 + fd[0][1] / n)
    out.sort(key=lambda c: -c["k"])
    return [{"name": c["name"], "why": c["why"], **({"url": c["url"]} if c.get("url") else {})} for c in out[:3]]


def run_new_performers(stash, plugin_dir, limit=30, skip=(), fresh=False):
    """Review page, "New" tab: people seen in several scenes / photos whose
    face matches none of your performers. A group where a quarter or more of
    the items already have the same performer becomes "It's [them]?" (add them to the rest, learn the
    face); otherwise "New performer?", with a name from a studio or folder
    most of them share. Drawn items (comics...) are left out. Kept 15 min."""
    cfg = settings(stash, plugin_dir)
    path = os.path.join(plugin_dir, "new_perf_cache.json")
    found = None
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS and saved.get("same") == NEW_PERF_SAME:
                found = saved["found"]
    items = {}
    for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
        got = stash.call(f"{{ {query}(filter: {{per_page: -1}}) {{ {field} {{ {NEW_PERF_FIELDS[kind]} }} }} }}")[query][field]
        items.update({f"{kind}:{x['id']}": x for x in got})

    def path_of(x):
        return (((x.get("files") or x.get("visual_files") or [{}])[0]) or {}).get("path") or ""

    # how often each tag is used in all: a tag that's someone's name is used
    # on them (mostly this group), a category tag all over the library
    tag_uses = {t["id"]: (t.get("scene_count") or 0) + (t.get("image_count") or 0) for t in stash.call(
        "{ findTags(filter: {per_page: -1}) { tags { id scene_count image_count } } }")["findTags"]["tags"]}
    # ... and isn't spread over lots of different performers either
    tag_people = defaultdict(set)
    for x in items.values():
        pids = {p["id"] for p in x.get("performers") or []}
        if pids:
            for t in x.get("tags") or []:
                tag_people[t["id"]] |= pids

    if found is None:
        index = load_face_index(plugin_dir)
        known = {}
        for pid, entry in index.items():
            embs = [q8(e) for e in entry.get("embs", [])] + entry.get("tagged", []) + entry.get("conf", [])
            if embs:
                known[pid] = embs
        cache = FaceCache(plugin_dir)
        if not cache.data:
            raise RuntimeError('nothing is scanned yet - run "Faces: scan scenes and images" first')

        def drawn(key):
            x = items.get(key)
            if not x:
                return True
            if re.search(r"\.(zip|cbz|cbr|rar|7z)[\\/]", path_of(x).lower()):
                return True
            return any(t["name"].strip().lower() in NEW_PERF_DRAWN_TAGS for t in x.get("tags") or [])

        faces, where = [], []
        for key, entry in cache.data.items():
            if drawn(key):
                continue
            for g, group in enumerate(entry.get("f", [])):
                for f in group:
                    faces.append(f)
                    where.append(key)
        matches = face_match_all(cfg, faces, known) if known else [[None, 0.0, 0.0]] * len(faces)
        unknown = [i for i, (_, best, _) in enumerate(matches) if best < NEW_PERF_UNKNOWN]
        groups = face_cluster(cfg, [faces[i] for i in unknown])
        members = defaultdict(dict)  # group -> item key -> (likeness, face)
        for i, (gid, sim) in zip(unknown, groups):
            if gid < 0:
                continue
            have = members[gid].get(where[i])
            if not have or sim > have[0]:
                members[gid][where[i]] = (sim, faces[i])
        found = []
        for gid, m in members.items():
            if len(m) < NEW_PERF_MIN_ITEMS:
                continue
            ranked = sorted(m.items(), key=lambda kv: -kv[1][0])
            found.append({"keys": [k for k, _ in ranked], "faces": [v[1] for _, v in ranked[:6]]})
        tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
        with open(tmp, "w") as f:
            json.dump({"at": time.time(), "found": found, "same": NEW_PERF_SAME}, f)
        os.replace(tmp, path)

    # performers whose face is known: if their face didn't match, it isn't them
    face_known = {pid for pid, entry in load_face_index(plugin_dir).items() if known_embs(entry)}
    with locked_new_perf_not(plugin_dir) as data:
        turned_down = [set(g) for g in data.get("groups", [])]
    skip = set(skip or ())
    out = []
    for g in found:
        keys = [k for k in g["keys"] if k in items]
        if len(keys) < NEW_PERF_MIN_ITEMS:
            continue
        sig = "|".join(sorted(keys)[:12])
        if sig in skip or any(len(set(keys) & t) * 2 >= len(keys) for t in turned_down):
            continue
        xs = [items[k] for k in keys]
        n = len(keys)
        perf = Counter(p["id"] for x in xs for p in x.get("performers") or [])
        names = {p["id"]: p["name"] for x in xs for p in x.get("performers") or []}
        studios = Counter((x.get("studio") or {}).get("id") for x in xs if x.get("studio"))
        studio_names = {x["studio"]["id"]: x["studio"]["name"] for x in xs if x.get("studio")}
        folders = Counter(re.split(r"[\\/]", path_of(x))[-2] if len(re.split(r"[\\/]", path_of(x))) > 1 else "" for x in xs)
        reasons = []
        kinds = Counter(k.split(":")[0] for k in keys)
        reasons.append("The same face in " + " and ".join(
            f"{c} {k}{'s' if c != 1 else ''}".replace("image", "photo") for k, c in kinds.items()))
        top = perf.most_common(1)
        # only ever added to items with no performer: one that has someone
        # most likely shows them
        untagged = [k for k in keys if not items[k].get("performers")]
        tagged_n = n - len(untagged)
        sugg = {"sig": sig, "faces": g["faces"]}
        if top and top[0][1] >= max(2, -(-n // 4)):
            pid, have = top[0]
            # a performer on a good share of them whose face isn't known yet:
            # probably them. One whose face IS known didn't match it, so it's
            # someone else (or a poor likeness of them) - nothing to suggest.
            if pid in face_known or not untagged:
                continue
            reasons.append(f"{names[pid]} is already on {have} of them, and their face isn't known yet")
            sugg.update(type="assign", performer={"id": pid, "name": names[pid]}, missing=untagged, keys=keys,
                        count=len(untagged))
            show = untagged + [k for k in keys if k not in untagged]
        elif len(untagged) >= NEW_PERF_MIN_ITEMS:
            ux = [items[k] for k in untagged]
            un = len(untagged)
            ukinds = Counter(k.split(":")[0] for k in untagged)
            reasons[0] = "The same face in " + " and ".join(
                f"{c} {k}{'s' if c != 1 else ''}".replace("image", "photo") for k, c in ukinds.items()) + " without a performer"
            ustudios = Counter((x.get("studio") or {}).get("id") for x in ux if x.get("studio"))
            ufolders = Counter(re.split(r"[\\/]", path_of(x))[-2] if len(re.split(r"[\\/]", path_of(x))) > 1 else "" for x in ux)
            cands = new_perf_names(ux, un, ustudios, studio_names, ufolders, tag_uses, path_of, tag_people)
            if cands:
                reasons.append(cands[0]["why"])
            if tagged_n:
                reasons.append(f"also in {tagged_n} that have performers (left as they are)")
            sugg.update(type="new", name=cands[0]["name"] if cands else "", names=cands, keys=untagged, count=un)
            show = untagged
        else:
            continue
        sugg["scenes"] = sum(1 for k in sugg["keys"] if k.startswith("scene:"))
        sugg["photos"] = sum(1 for k in sugg["keys"] if k.startswith("image:"))
        # pictures: photos first (a face shows better), best likeness first
        order = sorted(show, key=lambda k: (not k.startswith("image:"), show.index(k)))
        pics = []
        for k in order[:6]:
            kind, iid = k.split(":")
            x = items[k]
            link = (x.get("paths") or {}).get("screenshot" if kind == "scene" else "thumbnail") or ""
            pics.append({"kind": kind, "id": iid, "picture": re.sub(r"^https?://[^/]+", "", link)})
        sugg["samples"] = pics
        # every item it would change, to look through (and leave some out)
        allp = []
        for k in (sugg.get("missing") if sugg["type"] == "assign" else sugg["keys"]):
            kind, iid = k.split(":")
            x = items[k]
            link = (x.get("paths") or {}).get("screenshot" if kind == "scene" else "thumbnail") or ""
            allp.append({"key": k, "kind": kind, "id": iid, "title": x.get("title") or os.path.basename(path_of(x)),
                         "picture": re.sub(r"^https?://[^/]+", "", link)})
        sugg["all"] = allp
        if sugg["type"] == "new":
            ks = sugg["keys"]
            tc = Counter((t["id"], t["name"]) for k in ks for t in items[k].get("tags") or [])
            sugg["tag_ideas"] = [{"id": tid, "name": tn, "count": c} for (tid, tn), c in tc.most_common(14)
                                 if c >= max(2, len(ks) // 4) and tn.strip().lower() not in NEW_PERF_DRAWN_TAGS][:10]
        sugg["reasons"] = reasons
        out.append(sugg)
    # "It's ..." first (more to go on), then the most items
    out.sort(key=lambda x: (x["type"] != "assign", -x["count"]))
    return {"items": out[:limit], "total": len(out)}


def run_new_perf_read(stash, plugin_dir, keys, limit=6):
    """New tab: the local AI model reads the text in a few of a suggestion's
    pictures (watermarks, captions) for account names - OnlyFans, Fansly,
    Patreon, Instagram, TikTok - and studio / performer names you have.
    Name ideas [{name, why, url?}], best first. Remembered per set of items
    (new_perf_ocr.json), so each is only read once."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    keys = [k for k in (keys or []) if re.match(r"^(scene|image):\d+$", k)]
    sig = hashlib.sha1("|".join(sorted(keys)).encode()).hexdigest()[:16]
    path = os.path.join(plugin_dir, "new_perf_ocr.json")
    with locked_json(path) as saved:
        if sig in saved:
            return saved[sig]
    # photos first (a watermark is sharper there), then scenes (2 frames each)
    order = [k for k in keys if k.startswith("image:")] + [k for k in keys if k.startswith("scene:")]
    ctx = accounts_context(stash, plugin_dir)
    plat_names = {v[0].lower() for v in PLATFORMS.values()}
    seen, spelled, plat_of = Counter(), {}, {}
    studios_read, perfs_read = Counter(), Counter()
    looked = 0
    for k in order:
        if looked >= limit:
            break
        kind, iid = k.split(":")
        q = "findScene" if kind == "scene" else "findImage"
        item = stash.call(f"query($id: ID!) {{ {q}(id: $id) {{ {HANDLE_FIELDS[kind]} }} }}", {"id": iid})[q]
        if not item:
            continue
        pics = []
        try:
            if kind == "image":
                pics = [photo_picture(stash, item)]
            else:
                f = (item.get("files") or [None])[0]
                if f and (f.get("duration") or 0) > 1:
                    pics = [grab_frame(stash, f["path"], f["duration"] * at, width=1280)[0] for at in (0.15, 0.6)]
        except Exception as e:
            log("debug", f"{k}: {e}")
        if not pics:
            continue
        looked += 1
        here, here_st, here_pf = set(), set(), set()
        for pic in pics:
            try:
                text = ollama_frame_text(cfg, pic)
            except RuntimeError:
                raise
            except Exception as e:
                log("debug", f"{k}: {e}")
                continue
            for plat, h in handles_in_ocr(text):
                hl = h.lower()
                if hl in plat_names:
                    continue
                here.add(hl)
                spelled.setdefault(hl, h)
                if plat:
                    plat_of.setdefault(hl, plat)
            low = " ".join(text.lower().split())
            here_st |= {st["name"] for pat, st in ctx["marks"] if pat.search(low)}
            here_pf |= {p["name"] for pat, p in ctx["perf_marks"] if pat.search(low)}
        seen.update(here)
        studios_read.update(here_st)
        perfs_read.update(here_pf)
        # two pictures agree on an account: that's enough
        if any(c >= 2 for c in seen.values()):
            break
    out = []
    for hl, c in seen.most_common(3):
        plat = plat_of.get(hl)
        where = f"{PLATFORMS[plat][0]} @{spelled[hl]}" if plat else f"@{spelled[hl]}"
        # a near miss of a studio / performer you have is probably them
        known = known_name_for(ctx, spelled[hl])
        name = known[1]["name"] if known else spelled[hl]
        why = f"the AI read {where} in {c} picture{'s' if c != 1 else ''}" + (f' (you have "{name}")' if known else "")
        idea = {"name": name, "why": why}
        if plat and not known:
            idea["url"] = PLATFORMS[plat][1].format(spelled[hl])
        out.append(idea)
    for nm, c in (perfs_read + studios_read).most_common(2):
        if nm.lower() not in {o["name"].lower() for o in out}:
            out.append({"name": nm, "why": f"the AI read \"{nm}\" in {c} picture{'s' if c != 1 else ''}"})
    result = {"names": out[:3], "looked": looked}
    with locked_json(path) as saved:
        saved[sig] = result
    return result


def run_new_perf_lookalikes(plugin_dir, faces, limit=8):
    """New tab, "add to a performer you already have": the performers whose
    known faces are closest to this group's faces (best pair), as ideas - not
    a match (it's on the New tab because no one matched well). [{id, name, score}]"""
    index = load_face_index(plugin_dir)
    mine = [unq8(f) for f in (faces or [])[:6] if isinstance(f, str)]
    if not mine:
        return []
    hits = []
    for pid, entry in index.items():
        embs = known_embs(entry)
        if embs:
            hits.append((max(_dot(a, b) for a in mine for b in embs), pid, entry.get("name", "")))
    hits.sort(reverse=True)
    return [{"id": pid, "name": name, "score": round(sc * 100)} for sc, pid, name in hits[:limit] if sc > 0.2]


def run_review_no_many(plugin_dir, kind, pairs, no=True):
    """Batch view: many "no"s (or their Undo) at once. kind "tags": pairs
    of [item key, tag id]; "faces": [item key, performer id]."""
    pairs = [p for p in pairs or [] if isinstance(p, (list, tuple)) and len(p) == 2]
    if kind == "tags":
        with locked_tag_review_not(plugin_dir) as data:
            for key, tag in pairs:
                have = data.get(key, [])
                if no and str(tag) not in have:
                    data[key] = have + [str(tag)]
                elif not no and str(tag) in have:
                    data[key] = [t for t in have if t != str(tag)]
                    if not data[key]:
                        del data[key]
    else:
        with locked_face_index(plugin_dir) as index:
            for key, pid in pairs:
                entry = index.get(str(pid))
                if not entry:
                    continue
                nots = entry.get("not", [])
                if no and key not in nots:
                    entry["not"] = (nots + [key])[-2000:]
                elif not no and key in nots:
                    entry["not"] = [k for k in nots if k != key]
    return len(pairs)


# --- Review "Check": what's on an item that might not belong ---------------
CHECK_TAG_UNDER = 0.05       # a tag on it that under 5% of its closest look-alikes have
CHECK_TAG_EXAMPLES = 25      # ... and only tags with this many tagged items to judge by
CHECK_TAG_TRUSTED = 0.4      # ... that look-alikes usually see (median score on its own items):
                             # "Celeb" isn't something a picture shows, so it isn't judged
CHECK_FACE_UNDER = 0.25      # a performer whose known face is this far from every face in it
CHECK_FIELDS = {
    "scene": "id title files { basename } paths { screenshot }",
    "image": "id title visual_files { ... on BaseFile { basename } } paths { image }",
}


def locked_check_keep(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "check_keep.json"))


def run_check_answer(plugin_dir, key, what, keep=True):
    """"It fits" (or its Undo): don't ask about it again. what: "t:ID" / "p:ID"."""
    with locked_check_keep(plugin_dir) as data:
        have = data.get(key, [])
        if keep and what not in have:
            data[key] = have + [what]
        elif not keep and what in have:
            data[key] = [x for x in have if x != what]
            if not data[key]:
                del data[key]
    return True


def _check_candidates(stash, cfg, plugin_dir, excluded, fresh=False):
    """Every tag / performer on an item that looks out of place:
    [[key, "t" | "p", id, score, extra]]. Kept 15 minutes."""
    path = os.path.join(plugin_dir, "check_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                return saved["found"], saved["names"]
    found = []
    have = all_item_tags(stash)
    # tags: the look-alike vote of each item, for the tags it has
    looks = LookIndex(stash, cfg, plugin_dir, excluded)
    names = dict(looks.names)
    if looks.ready():
        examples = Counter(t for r in looks.rows for t in r[2])
        judged = {t for t, n in examples.items() if n >= CHECK_TAG_EXAMPLES
                  and names.get(t, "").strip().lower() not in BODY_TAG_NAMES}
        keys = [k for k, ent in looks.cache.data.items() if ent.get("e") and have.get(k, set()) & judged]
        pos = {k: i for i, k in enumerate(looks.cache.data)}
        allkeys = list(looks.cache.data)
        rows = {}
        vecs, idx = [], {}
        for k in set(keys) | {r[0] for r in looks.rows}:
            if looks.cache.data.get(k, {}).get("e"):
                idx[k] = len(vecs)
                vecs.append(looks.cache.data[k]["e"])
        row_at = {idx[r[0]]: r for r in looks.rows if r[0] in idx}
        nn = look_knn(cfg, vecs, sorted(row_at), LOOK_K)
        per_tag = defaultdict(list)  # tag -> [(score, key)] on the items that have it
        for k in keys:
            near = nn[idx[k]]
            if not near:
                continue
            top = near[0][1] / 16129
            out, total = defaultdict(float), 0.0
            for j, dot in near:
                w = math.exp((dot / 16129 - top) / LOOK_TEMP)
                total += w
                for t in row_at[j][2]:
                    out[t] += w
            for t in have[k] & judged:
                per_tag[t].append((out.get(t, 0.0) / total if total else 0.0, k))
        for t, lst in per_tag.items():
            med = sorted(sc for sc, _ in lst)[len(lst) // 2]
            if med < CHECK_TAG_TRUSTED:
                continue  # look-alikes don't see this tag well enough to judge it
            odd = [(sc, k) for sc, k in lst if sc < CHECK_TAG_UNDER]
            # a real slip is a rare one; lots of them means the look-alikes
            # just don't capture that tag (e.g. a folder of mixed things)
            if len(odd) > max(2, len(lst) // 10):
                continue
            for sc, k in odd:
                found.append([k, "t", t, round(sc, 3), examples[t]])
    # performers: their known face against the faces found in the item
    index = load_face_index(plugin_dir)
    known = {pid: known_embs(e) for pid, e in index.items()}
    known = {p: e for p, e in known.items() if e}
    cache = FaceCache(plugin_dir)
    for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
        got = stash.call(f"query($f: {kind.capitalize()}FilterType) {{ {query}({kind}_filter: $f, filter: {{per_page: -1}}) {{ {field} {{ id performers {{ id name }} }} }} }}",
                         {"f": {"performer_count": {"value": 0, "modifier": "GREATER_THAN"}}})[query][field]
        for x in got:
            key = f"{kind}:{x['id']}"
            groups = (cache.data.get(key) or {}).get("f") or []
            faces = [unq8(f) for g in groups for f in g]
            if not faces:
                continue
            for p in x["performers"]:
                names.setdefault("p" + p["id"], p["name"])
                embs = known.get(p["id"])
                if not embs:
                    continue
                best = max(_dot(a, b) for a in faces for b in embs)
                if best < CHECK_FACE_UNDER:
                    found.append([key, "p", p["id"], round(best, 3), len(faces)])
    tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "found": found, "names": names}, f)
    os.replace(tmp, path)
    return found, names


def run_check_keep_many(plugin_dir, pairs, keep=True):
    """Batch: many "It fits" (or their Undo) at once: [[item key, "t:ID" / "p:ID"]]."""
    with locked_check_keep(plugin_dir) as data:
        for key, what in pairs or []:
            have = data.get(key, [])
            if keep and what not in have:
                data[key] = have + [what]
            elif not keep and what in have:
                data[key] = [x for x in have if x != what]
                if not data[key]:
                    del data[key]
    return len(pairs or [])


def run_check(stash, plugin_dir, limit=40, skip=(), fresh=False, group=None, only=None):
    """Review page, "Check" tab: tags on items that their look-alikes say
    don't fit, and performers whose face isn't in the item. Most out of
    place first. Removing is done by the page; "It fits" is remembered."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    if only == "g":  # Galleries → Check: photos that look like none of the rest of their gallery
        found, names = _gallery_check_candidates(stash, cfg, plugin_dir, fresh)
    else:
        found, names = _check_candidates(stash, cfg, plugin_dir, excluded, fresh)
    have = all_item_tags(stash)
    with locked_check_keep(plugin_dir) as kept:
        kept = {k: set(v) for k, v in kept.items()}
    skip = set(skip or ())
    picks = []
    for key, what, xid, sc, extra in found:
        if group and f"{what}:{xid}" != group:
            continue
        if only and what != only:
            continue  # Faces → Check: performers only; Tags → Check: tags only
        if what == "t" and (xid in excluded or xid not in have.get(key, ())):
            continue  # ignored now, or already taken off
        if f"{what}:{xid}" in kept.get(key, ()) or f"{key}:{what}:{xid}" in skip:
            continue
        picks.append((sc, key, what, xid, extra))
    picks.sort(key=lambda x: x[0])
    out = []
    for at in range(0, len(picks), 150):
        chunk = picks[at:at + 150]
        items = {}
        for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            ids = sorted({k.split(":")[1] for _, k, _, _, _ in chunk if k.startswith(kind + ":")}, key=int)
            if ids:
                fields = CHECK_FIELDS[kind] + " performers { id } tags { id }" + (" galleries { id }" if kind == "image" else "")
                items.update({f"{kind}:{x['id']}": x for x in find_by_ids(stash, query, field, ids, fields)})
        for sc, key, what, xid, extra in chunk:
            it = items.get(key)
            if not it:
                continue
            if what == "p" and xid not in {p["id"] for p in it.get("performers") or []}:
                continue  # already taken off
            if what == "g" and xid not in {g["id"] for g in it.get("galleries") or []}:
                continue
            kind, iid = key.split(":")
            files = it.get("files") or it.get("visual_files") or [{}]
            link = (it.get("paths") or {}).get("screenshot" if kind == "scene" else "image") or ""
            name = names.get(xid, xid) if what == "t" else names.get(what + xid, xid)
            why = (f"only {round(sc * 100)}% of its 20 closest look-alikes have it ({extra} tagged items to go by)" if what == "t"
                   else f"only {round(sc * 100)}% of its 20 closest look-alikes are in it ({extra} photos in it)" if what == "g"
                   else f"none of the {extra} face{'s' if extra != 1 else ''} found in it look like them"
                   + (f" (closest {round(sc * 100)}%)" if sc > 0 else ""))
            out.append({"kind": kind, "id": iid, "what": what, "target": xid, "name": name, "score": round(sc * 100),
                        "why": why, "title": it.get("title") or (files[0] or {}).get("basename") or "",
                        "picture": re.sub(r"^https?://[^/]+", "", link)})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # the Batch view: one tag / performer at a time, the most first
    per = Counter(f"{what}:{xid}" for _, _, what, xid, _ in picks)
    groups = [{"id": gid, "name": names.get(gid[2:], gid) if gid.startswith("t:") else names.get(gid[0] + gid[2:], gid),
               "count": c} for gid, c in per.most_common()]
    return {"items": out, "total": len(picks), "groups": groups}


def run_face_unreject(plugin_dir, performer_id, key):
    """Undo a "Not them" (face review's Undo)."""
    with locked_face_index(plugin_dir) as index:
        entry = index.get(str(performer_id))
        if not entry or key not in entry.get("not", []):
            return False
        entry["not"] = [k for k in entry["not"] if k != key]
        return True


def face_match_all(cfg, faces, known):
    """The face helper's /match: [[best performer id, best, second best]]
    for each q8 face (numpy there; plain Python here would take minutes)."""
    body = json.dumps({"faces": faces, "known": known}).encode()
    req = urllib.request.Request(cfg["ai"]["face_url"] + "/match", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("the face helper is too old for face review - copy the add-on's face-helper/face_helper.py "
                               "over the running one and restart it (see the README)")
        raise RuntimeError(f"the face helper couldn't match faces ({e.code})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({getattr(e, 'reason', e)})")


FACE_REVIEW_FIELDS = {
    "scene": "id title performers { id } files { basename duration } paths { screenshot }",  # duration: the times
    "image": "id title performers { id } visual_files { ... on BaseFile { basename } } paths { thumbnail image }",
}


def run_face_review(stash, plugin_dir, performer=None, limit=40, skip=()):
    """Face review page: strong face matches across the whole library (or
    one performer) that aren't on the item yet and you haven't turned down -
    the same bar as "add confident matches automatically": a high likeness,
    clearly ahead of anyone else, and in a scene the best match in 2+ frames.
    Best first. `skip`: "kind:id:performer" already shown this visit."""
    cfg = settings(stash, plugin_dir)
    index = load_face_index(plugin_dir)
    known = {}
    for pid, entry in index.items():
        embs = [q8(e) for e in entry.get("embs", [])] + entry.get("tagged", []) + entry.get("conf", [])
        if embs:
            known[pid] = embs
    if not known:
        raise RuntimeError("no performer faces known yet - run \"Faces: learn performers' faces\"")
    cache = FaceCache(plugin_dir)
    if not cache.data:
        raise RuntimeError('nothing is scanned yet - run "Faces: scan scenes and images" first')
    faces, where = [], []  # where: (item key, picture number)
    groups_of = {}  # item key -> how many pictures were looked at
    for key, entry in cache.data.items():
        groups_of[key] = len(entry.get("f", []))
        for g, group in enumerate(entry.get("f", [])):
            for f in group:
                faces.append(f)
                where.append((key, g))
    matches = face_match_all(cfg, faces, known)
    # per item and performer: best likeness, the face it was, the closest
    # other performer for that face, and in how many pictures they won
    hits = {}
    for (key, g), f, (pid, best, second) in zip(where, faces, matches):
        if not pid:
            continue
        h = hits.setdefault((key, pid), {"score": 0.0, "rival": 0.0, "emb": None, "pics": set(), "best_g": g})
        h["pics"].add(g)
        if best > h["score"]:
            h["score"], h["rival"], h["emb"], h["best_g"] = best, second, f, g
    floor = cfg["ai"]["face_auto_min"]
    want = str(performer) if performer else None
    skip = set(skip or ())
    tagged = {}  # item key -> performer ids already on it
    for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
        got = stash.call(f"{{ {query}(filter: {{per_page: -1}}) {{ {field} {{ id performers {{ id }} }} }} }}")[query][field]
        tagged.update({f"{kind}:{x['id']}": {p["id"] for p in x["performers"]} for x in got})
    strong = []
    for (key, pid), h in hits.items():
        kind, iid = key.split(":")
        if want and pid != want:
            continue
        if h["score"] < floor + (0.05 if kind == "image" else 0) or h["score"] - h["rival"] < FACE_MARGIN:
            continue
        if kind == "scene" and len(h["pics"]) < 2:
            continue
        if key in (index.get(pid) or {}).get("not", []) or f"{key}:{pid}" in skip or pid in tagged.get(key, ()):
            continue
        strong.append((h["score"], kind, iid, pid, h))
    strong.sort(key=lambda x: -x[0])
    # drop the ones already tagged with them (checked on the best few only)
    out = []
    for at in range(0, len(strong), 200):
        chunk = strong[at:at + 200]
        items = {}
        for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            ids = sorted({iid for _, k, iid, _, _ in chunk if k == kind}, key=int)
            if ids:
                got = find_by_ids(stash, query, field, ids, f"{FACE_REVIEW_FIELDS[kind]}")
                items.update({(kind, x["id"]): x for x in got})
        for score, kind, iid, pid, h in chunk:
            it = items.get((kind, iid))
            if not it or pid in {p["id"] for p in it.get("performers") or []}:
                continue
            files = it.get("files") or it.get("visual_files") or [{}]
            # a scene: when they're seen (the frames the face scan looked at,
            # FACE_FRAME_POINTS of the length - only if it got all of them, so
            # the pictures line up with those points)
            times, best_at = [], None
            dur = (files[0] or {}).get("duration") or 0
            if kind == "scene" and dur and groups_of.get(f"{kind}:{iid}") == len(FACE_FRAME_POINTS):
                times = [round(dur * FACE_FRAME_POINTS[g]) for g in sorted(h["pics"])]
                best_at = round(dur * FACE_FRAME_POINTS[h["best_g"]])
            out.append({
                "times": times, "best_at": best_at,
                "kind": kind, "id": iid, "performer": pid, "name": (index.get(pid) or {}).get("name", ""),
                "score": round(score * 100), "rival": round(h["rival"] * 100), "pictures": len(h["pics"]),
                "emb": h["emb"], "title": it.get("title") or (files[0] or {}).get("basename") or "",
                "picture": (it.get("paths") or {}).get("screenshot") if kind == "scene" else (it.get("paths") or {}).get("image"),
                "others": len(it.get("performers") or []),
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # the Batch view: how many there are for each performer, biggest first
    per = Counter(pid for _, _, _, pid, _ in strong)
    groups = [{"id": pid, "name": (index.get(pid) or {}).get("name", pid), "count": c} for pid, c in per.most_common()]
    return {"items": out, "total": len(strong), "groups": groups}


def add_performers(stash, kind, item, ids):
    have = {p["id"] for p in item.get("performers") or []}
    mutation, input_type = AUTOTAG_UPDATE[kind]
    stash.call(f"mutation($i: {input_type}!) {{ {mutation}(input: $i) {{ id }} }}",
               {"i": {"ids": [item["id"]], "performer_ids": {"ids": sorted(set(ids) - have, key=int), "mode": "ADD"}}})


def _face_items(stash, kind, item_filter=None, ids=None):
    query, field, farg, ftype = {"scene": ("findScenes", "scenes", "scene_filter", "SceneFilterType"),
                                 "image": ("findImages", "images", "image_filter", "ImageFilterType")}[kind]
    if ids is not None:
        if not ids:
            return []
        return stash.call(
            f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ {FACE_ITEM_FIELDS[kind]} }} }} }}",
            {"ids": ids})[query][field]
    out, page = [], 1
    while True:
        items = stash.call(
            f"query($p: {ftype}, $f: FindFilterType) {{ {query}({farg}: $p, filter: $f) {{ {field} {{ {FACE_ITEM_FIELDS[kind]} }} }} }}",
            {"p": item_filter or {}, "f": {"page": page, "per_page": 500, "sort": "created_at", "direction": "DESC"}})[query][field]
        out += items
        if len(items) < 500:
            return out
        page += 1


def run_faces(stash, plugin_dir, kinds, preview=False, limit=None, ids=None):
    """Task: find who's in scenes / images without performers. Confident
    matches are added when "Faces: add confident matches automatically" is
    on; otherwise (or in a preview) they're only written to the log."""
    cfg = settings(stash, plugin_dir)
    index = update_face_index(stash, cfg, plugin_dir, quiet=ids is not None)
    if not any(known_embs(v) for v in index.values()):
        log("warning", "No performer faces known yet - performers need a profile picture with a face in it.")
        return
    apply = cfg["ai"]["face_auto"] and not preview
    todo = []
    for kind in kinds:
        if ids is not None:
            todo += [(kind, i) for i in _face_items(stash, kind, ids=ids.get(kind) or [])]
        else:
            todo += [(kind, i) for i in _face_items(stash, kind, {"performer_count": {"value": 0, "modifier": "EQUALS"}})]
    if limit:
        import random
        todo = random.sample(todo, min(limit, len(todo)))
    if ids is None:
        log("info", f"Looking for known faces in {len(todo)} item(s) without performers"
            + (" (preview, nothing is saved)" if preview else "" if apply else
               " - only writing suggestions to the log, as \"Faces: add confident matches automatically\" is off"))
    cache = FaceCache(plugin_dir)
    added = found = 0
    for n, (kind, item) in enumerate(todo):
        if ids is None:
            progress(n / max(len(todo), 1))
        name = os.path.basename(item_path(kind, item)) or f"{kind} {item['id']}"
        try:
            cands = face_candidates(stash, cfg, index, kind, item, cache)
        except RuntimeError:
            cache.save()
            raise
        except Exception as e:
            log("warning", f"{name}: skipped ({e})")
            continue
        if not cands:
            continue
        found += 1
        sure = [c for c in cands if confident(cfg, kind, c, cands)]
        text = ", ".join(f'{c["name"]} {round(c["score"] * 100)}%' + (f' in {c["pictures"]} frames' if kind == "scene" else "")
                         for c in cands[:3])
        if sure and apply:
            add_performers(stash, kind, item, [c["id"] for c in sure])
            added += 1
            log("info", f'{name}: added {", ".join(c["name"] for c in sure)} (looks like: {text})')
        else:
            log("info", f'{name}: looks like {text}' + (f' - would add {", ".join(c["name"] for c in sure)}' if sure else ""))
    cache.save()
    if ids is None:
        progress(1)
        log("info", f"{'Preview: ' if preview else 'Done: '}found a likeness in {found} of {len(todo)} item(s)"
            + (f"; added performers to {added}" if apply else "") + ("." if apply else ". Nothing was saved."))


def run_face_scan(stash, plugin_dir, kinds=("scene", "image")):
    """Task: find the faces in every scene (a few frames) and image once, so
    look-alike searches and learning from tagged items are quick. Picks up
    where it left off; changed files are looked at again."""
    cfg = settings(stash, plugin_dir)
    face_helper(cfg, [])  # fails early if the helper isn't running
    cache = FaceCache(plugin_dir)
    todo = [(k, i) for k in kinds for i in _face_items(stash, k)]
    left = [(k, i) for k, i in todo if cache.groups(stash, cfg, k, i, compute=False) is None]
    log("info", f"Scanning {len(left)} of {len(todo)} scene(s)/image(s) for faces ({len(todo) - len(left)} done before). "
        "Scenes take a few seconds each, images well under one; you can cancel and run it again to carry on.")
    faces = 0
    for n, (kind, item) in enumerate(left):
        progress(n / max(len(left), 1))
        try:
            faces += sum(len(g) for g in cache.groups(stash, cfg, kind, item))
        except RuntimeError:
            cache.save()
            raise
        except Exception as e:
            log("debug", f"{kind} {item['id']}: {e}")
    cache.save()
    progress(1)
    log("info", f"Done: scanned {len(left)} item(s), {faces} face(s) found.")


def run_face_learn_tagged(stash, plugin_dir):
    """Task: learn more of each performer's faces from scenes and images
    where they're the only performer (and that face looks like them)."""
    cfg = settings(stash, plugin_dir)
    index = update_face_index(stash, cfg, plugin_dir)
    base = {pid: v["embs"] for pid, v in index.items() if v.get("embs")}
    cache = FaceCache(plugin_dir)
    solo = {"performer_count": {"value": 1, "modifier": "EQUALS"}}
    todo = [(k, i) for k in ("scene", "image") for i in _face_items(stash, k, solo)]
    todo = [(k, i) for k, i in todo if i["performers"] and i["performers"][0]["id"] in base]
    log("info", f"Learning from {len(todo)} scene(s)/image(s) where a known performer is the only one"
        " (items not scanned for faces yet are scanned now)")
    found = defaultdict(list)  # performer id -> [(likeness, fingerprint)]
    for n, (kind, item) in enumerate(todo):
        progress(n / max(len(todo), 1))
        pid = item["performers"][0]["id"]
        try:
            groups = cache.groups(stash, cfg, kind, item)
        except RuntimeError:
            cache.save()
            raise
        except Exception as e:
            log("debug", f"{kind} {item['id']}: {e}")
            continue
        for faces in groups:
            for emb in faces:
                score = max(_dot(emb, e) for e in base[pid])
                if score >= FACE_SAME_AS_PROFILE:
                    found[pid].append((score, emb))
    cache.save()
    grew = 0
    with locked_face_index(plugin_dir) as idx:
        for pid, faces in found.items():
            entry = idx.get(pid)
            if not entry:
                continue
            before = len(entry.get("tagged", []))
            # most like them first, but keep a spread (near copies are skipped)
            for _, emb in sorted(faces, key=lambda x: -x[0]):
                if len(entry.get("tagged", [])) >= FACE_MAX_TAGGED:
                    break
                _add_fingerprint(entry, "tagged", q8(emb), FACE_MAX_TAGGED)
            grew += len(entry.get("tagged", [])) > before
    progress(1)
    log("info", f"Done: learned more faces for {grew} performer(s).")


def run_look_similar(stash, plugin_dir, kind, item_id, limit=40):
    """Asked by Stash Suggestions' "Similar photos" / "Suggested" rows: the
    scenes or photos (same kind) that look most like this one, from the
    picture fingerprints (look_items.json), with the library's average look
    taken away so it's what's special about this one that counts. An item
    without a fingerprint gets one now when the face helper is running (new
    items). Nothing else is saved. {"known": bool, "items": [[id, 0-100]]}."""
    kind = "scene" if kind in ("scene", "scenes") else "image"
    cache = LookCache(plugin_dir)
    key = f"{kind}:{item_id}"
    ent = cache.new.get(key) or cache.data.get(key)
    if not (ent and ent.get("e")):
        with contextlib.suppress(Exception):
            q = "findScene" if kind == "scene" else "findImage"
            item = stash.call(f"query($id: ID!) {{ {q}(id: $id) {{ {LOOK_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[q]
            if item:
                cache.vector(stash, settings(stash, plugin_dir), kind, item)
                cache.save()
        ent = cache.data.get(key)
    if not (ent and ent.get("e")):
        return {"known": False, "items": []}

    def unit(v):
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]
    raw = {k: unit(_ints(e["e"])) for k, e in cache.data.items() if k.startswith(kind + ":") and e.get("e")}
    avg = [sum(col) / len(raw) for col in zip(*raw.values())]
    me = unit([a - b for a, b in zip(raw[key], avg)])
    hits = []
    for k, v in raw.items():
        if k != key:
            hits.append((sum(map(operator.mul, me, unit([a - b for a, b in zip(v, avg)]))), k.split(":", 1)[1]))
    hits.sort(reverse=True)
    return {"known": True, "items": [[i, round(sc * 100)] for sc, i in hits[:limit]]}


def run_face_similar(plugin_dir, performer_id, limit=60):
    """Asked by Stash Suggestions' "Similar Performers" row: how alike other
    performers' faces are to this one's (best pair of their known face
    fingerprints, 0-100, the same "% alike" as elsewhere). Nothing is saved.
    How alike faces score differs a lot from one performer to the next (how
    many pictures of them it knows), so it also gives this performer's own
    top-5% line ("p95") to judge by. {"known", "scores": {id: %}, "p95"}:
    the most alike `limit` performers at or above that line."""
    index = load_face_index(plugin_dir)
    pid = str(performer_id)
    mine = known_embs(index.get(pid) or {})
    if not mine:
        return {"known": False, "scores": {}}
    hits = []
    for other, entry in index.items():
        if other == pid:
            continue
        theirs = known_embs(entry)
        if not theirs:
            continue
        hits.append((max(_dot(x, y) for x in mine for y in theirs), other))
    if not hits:
        return {"known": True, "scores": {}, "p95": 100}
    hits.sort(reverse=True)
    p95 = round(hits[min(len(hits) - 1, len(hits) // 20)][0] * 100)
    return {"known": True, "p95": p95,
            "scores": {o: round(sc * 100) for sc, o in hits[:limit] if round(sc * 100) >= p95}}


def run_face_lookalikes(stash, plugin_dir, performer_id, limit=80):
    """Asked by a performer's page: scanned scenes / images that don't have
    them but have a face that looks like them, best first. Nothing is saved."""
    cfg = settings(stash, plugin_dir)
    index = load_face_index(plugin_dir)
    pid = str(performer_id)
    mine = known_embs(index.get(pid) or {})
    if not mine:
        raise RuntimeError("their face isn't known yet - they need a profile picture with a face in it "
                           "(then run \"Faces: learn performers' faces\")")
    cache = FaceCache(plugin_dir)
    if not cache.data:
        raise RuntimeError('nothing is scanned yet - run "Faces: scan scenes and images" first')
    have = set((index.get(pid) or {}).get("not", []))  # ones you said aren't them
    for kind, (query, field, farg) in (("scene", ("findScenes", "scenes", "scene_filter")),
                                       ("image", ("findImages", "images", "image_filter"))):
        got = stash.call(f"query($p: {kind.capitalize()}FilterType) {{ {query}({farg}: $p, filter: {{per_page: -1}}) {{ {field} {{ id }} }} }}",
                         {"p": {"performers": {"value": [pid], "modifier": "INCLUDES"}}})[query][field]
        have |= {f"{kind}:{x['id']}" for x in got}
    others = [(p, known_embs(v)) for p, v in index.items() if p != pid]
    others = [o for o in others if o[1]]
    floor = max(cfg["ai"]["face_min"], FACE_LOOKALIKE_MIN)
    hits = []
    for key, entry in cache.data.items():
        if key in have:
            continue
        best, best_emb, pictures = 0.0, None, 0
        for g in entry.get("f", []):
            here = 0.0
            for s_ in g:
                emb = unq8(s_)
                score = max(_dot(emb, e) for e in mine)
                if score > here:
                    here = score
                if score > best:
                    best, best_emb = score, emb
            pictures += here >= floor
        if best >= floor:
            hits.append([key, best, best_emb, pictures])
    hits.sort(key=lambda h: -h[1])
    out = []
    for key, best, emb, pictures in hits[:limit * 3]:
        kind, iid = key.split(":")
        # only where they're the closest match for that face of everyone known
        rival = max((max(_dot(emb, e) for e in embs) for _, embs in others), default=0.0)
        if rival > best:
            continue
        if len(out) >= limit:
            break
        sure = (best >= cfg["ai"]["face_auto_min"] + (0.05 if kind == "image" else 0) and best - rival >= FACE_MARGIN
                and (kind == "image" or pictures >= 2))
        out.append({"kind": kind, "id": iid, "score": round(best * 100), "pictures": pictures,
                    "rival": round(rival * 100), "sure": sure, "emb": q8(emb)})
    # titles for the list
    for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
        ids = [o["id"] for o in out if o["kind"] == kind]
        if ids:
            got = find_by_ids(stash, query, field, ids, f"id title {'files { basename }' if kind == 'scene' else 'visual_files { ... on BaseFile { basename } }'}")
            names = {x["id"]: x.get("title") or ((x.get("files") or x.get("visual_files") or [{}])[0] or {}).get("basename") or "" for x in got}
            for o in out:
                if o["kind"] == kind:
                    o["title"] = names.get(o["id"], "")
    out.sort(key=lambda o: (not o["sure"], -o["score"]))
    return out


def run_face_duplicates(stash, plugin_dir):
    """Task: performers whose faces are nearly the same - often one person
    under two names. Only writes the pairs to the log."""
    cfg = settings(stash, plugin_dir)
    index = update_face_index(stash, cfg, plugin_dir)
    people = [(pid, v["name"], v["embs"]) for pid, v in index.items() if v.get("embs")]
    pairs = []
    for i in range(len(people)):
        progress(i / max(len(people), 1))
        a = people[i]
        for b in people[i + 1:]:
            score = max(_dot(x, y) for x in a[2] for y in b[2])
            if score >= FACE_DUPLICATE_MIN:
                pairs.append((score, a, b))
    pairs.sort(key=lambda p: -p[0])
    for score, a, b in pairs[:100]:
        log("info", f'{round(score * 100)}% alike: "{a[1]}" (/performers/{a[0]}) and "{b[1]}" (/performers/{b[0]})')
    progress(1)
    log("info", f"Done: {len(pairs)} pair(s) of performers are very alike (at least {round(FACE_DUPLICATE_MIN * 100)}%)"
        + (" - the 100 most alike are listed above" if len(pairs) > 100 else "")
        + ". Check them: look-alikes score this high too, so not every pair is one person. Nothing was changed;"
        " if a pair is one person, merge them in Stash (select both on the Performers page, ••• → Merge).")


# --- performer details from their pictures ---------------------------------
# What can be seen in a performer's pictures: hair and eye colour, tattoos,
# piercings. Only empty fields are filled; height, measurements and the like
# can't be seen in a picture (use Stash's scrapers for those).

PERFORMER_DETAIL_FIELDS = ("hair_color", "eye_color", "tattoos", "piercings")
HAIR_COLORS = ["Blonde", "Brunette", "Black", "Red", "Auburn", "Grey", "White", "Bald", "Various", "unknown"]
EYE_COLORS = ["Blue", "Brown", "Green", "Grey", "Hazel", "unknown"]


def performer_pictures(stash, perf, extra=2, skip=0):
    """Their profile picture and a couple of photos where they're alone (the
    same ones each time; `skip` takes the next few instead)."""
    pics = []
    if perf.get("image_path") and "default=true" not in perf["image_path"]:
        d, ct = stash.fetch(perf["image_path"])
        if ct.startswith("image/"):
            pics.append(d)
    imgs = stash.call(
        "query($p: ImageFilterType) { findImages(image_filter: $p, filter: {per_page: 12, sort: \"created_at\", direction: DESC}) { images { paths { thumbnail } } } }",
        {"p": {"performers": {"value": [perf["id"]], "modifier": "INCLUDES"}, "performer_count": {"value": 1, "modifier": "EQUALS"}}},
    )["findImages"]["images"]
    for im in imgs[skip:]:
        if len(pics) >= 1 + extra:
            break
        try:
            d, ct = stash.fetch(im["paths"]["thumbnail"])
            if ct.startswith("image/"):
                pics.append(d)
        except Exception:
            pass
    return pics


def ollama_performer_details(cfg, pictures, name):
    ai = cfg["ai"]
    # no example values in the prompt: a small model tends to copy them
    prompt = (
        f"These pictures all show the same person ({name}). Report only what you can clearly see in these pictures. "
        "hair_color: their hair colour. eye_color: their eye colour, or unknown if the eyes aren't clearly visible. "
        "tattoos: each tattoo you can actually see, as 'body part: what it shows', separated by semicolons; an empty "
        "string if you see none. piercings: each piercing you can actually see, by body part, separated by semicolons; "
        "an empty string if you see none. Do not guess and do not make anything up."
    )
    body = {
        "model": ai["model"], "stream": False, "keep_alive": "15m", "options": {"temperature": 0},
        "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(p).decode() for p in pictures]}],
        "format": {"type": "object", "properties": {
            "hair_color": {"type": "string", "enum": HAIR_COLORS},
            "eye_color": {"type": "string", "enum": EYE_COLORS},
            "tattoos": {"type": "string"}, "piercings": {"type": "string"}},
            "required": ["hair_color", "eye_color", "tattoos", "piercings"]},
    }
    req = urllib.request.Request(ai["url"] + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        reply = json.loads(resp.read())
    try:
        out = json.loads((reply.get("message") or {}).get("content") or "{}")
    except ValueError:
        return {}
    clean = {}
    for k in PERFORMER_DETAIL_FIELDS:
        v = (out.get(k) or "").strip().strip(".")
        if k in ("tattoos", "piercings"):
            # drop parts like "ear: none" or "Unknown"
            parts = [x.strip() for x in v.split(";")]
            v = "; ".join(x for x in parts if x and not re.search(r"\b(none|unknown|not visible|n/a)\b", x, re.I)
                          and "body part" not in x.lower())
        low = v.lower()
        if not v or low in ("unknown", "none", "n/a", "no", "not visible", "none visible"):
            continue
        clean[k] = v[:200]
    return clean


PERFORMER_DETAIL_QUERY = "id name image_path " + " ".join(PERFORMER_DETAIL_FIELDS)


def run_performer_details_one(stash, plugin_dir, perf_id):
    """Asked by the performer edit page: what the AI sees. Nothing is saved."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    perf = stash.call(f"query($id: ID!) {{ findPerformer(id: $id) {{ {PERFORMER_DETAIL_QUERY} }} }}", {"id": str(perf_id)})["findPerformer"]
    pics = performer_pictures(stash, perf)
    if not pics:
        raise RuntimeError("they have no pictures to look at")
    return ollama_performer_details(cfg, pics, perf["name"])


def run_performer_details(stash, plugin_dir, preview=False, limit=None):
    """Task: fill in performers' empty hair / eye colour, tattoos and
    piercings from their pictures."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    perfs = stash.call(f"{{ findPerformers(filter: {{per_page: -1}}) {{ performers {{ {PERFORMER_DETAIL_QUERY} }} }} }}")["findPerformers"]["performers"]
    perfs = [p for p in perfs if any(not (p.get(k) or "").strip() for k in ("hair_color", "eye_color"))]
    if limit:
        import random
        perfs = random.sample(perfs, min(limit, len(perfs)))
    log("info", f"Looking at {len(perfs)} performer(s) with no hair or eye colour"
        + (" (preview, nothing is saved)" if preview else ""))
    changed = 0
    for n, p in enumerate(perfs):
        progress(n / max(len(perfs), 1))
        try:
            pics = performer_pictures(stash, p)
            if not pics:
                continue
            # asked twice, with two different sets of their photos; only what
            # both answers agree on counts
            first = ollama_performer_details(cfg, pics, p["name"])
            other = performer_pictures(stash, p, skip=2)
            second = ollama_performer_details(cfg, other, p["name"]) if other[1:] != pics[1:] else first
            # (tattoos and piercings are too hit-and-miss to save unseen; the
            # "Ask AI for details" button offers them for you to check)
            seen = {k: first[k] for k in ("hair_color", "eye_color")
                    if first.get(k) and first.get(k, "").lower() == second.get(k, "").lower()}
        except Exception as e:
            log("warning", f"{p['name']}: skipped ({e})")
            continue
        fill = {k: v for k, v in seen.items() if not (p.get(k) or "").strip()}
        if not fill:
            continue
        changed += 1
        log("info", f'{p["name"]}: {"would set" if preview else "set"} ' + "; ".join(f"{k.replace('_', ' ')} = {v}" for k, v in fill.items()))
        if not preview:
            stash.call("mutation($i: PerformerUpdateInput!) { performerUpdate(input: $i) { id } }", {"i": {"id": p["id"], **fill}})
    progress(1)
    log("info", f"{'Preview: ' if preview else 'Done: '}{changed} of {len(perfs)} performer(s) "
        + ("would get" if preview else "got") + " details from their pictures" + (". Nothing was saved." if preview else "."))


BULK_MUTATIONS = {
    "scenes": ("bulkSceneUpdate", "BulkSceneUpdateInput"),
    "images": ("bulkImageUpdate", "BulkImageUpdateInput"),
    "galleries": ("bulkGalleryUpdate", "BulkGalleryUpdateInput"),
}
BULK_BATCH = 200


def item_ids(stash, kind, item_filter):
    """Ids of every scene/image/gallery matching a filter."""
    query_name, field, farg, ftype = KINDS[kind]
    q = f"""query($p: {ftype}, $f: FindFilterType) {{
        {query_name}({farg}: $p, filter: $f) {{ {field} {{ id }} }}
    }}"""
    ids, page = [], 1
    while True:
        items = stash.call(q, {"p": item_filter, "f": {"page": page, "per_page": PAGE_SIZE}})[query_name][field]
        ids += [i["id"] for i in items]
        if len(items) < PAGE_SIZE:
            return ids
        page += 1


def bulk_work(stash, args):
    """What to tag: (owner name, [(label, mutation kind, ids)])."""
    split = lambda key: [x for x in str(args.get(key) or "").split(",") if x]
    kinds = split("kinds")

    if args.get("performer_id"):
        pid = str(args["performer_id"])
        perf = stash.call("query($id: ID!) { findPerformer(id: $id) { name } }", {"id": pid})["findPerformer"]
        if not perf:
            raise RuntimeError(f"performer {pid} not found")
        by_performer = {"performers": {"value": [pid], "modifier": "INCLUDES"}}
        return perf["name"] + "'s", [
            (k, k, item_ids(stash, k, by_performer)) for k in kinds if k in BULK_MUTATIONS
        ]

    if args.get("gallery_id"):
        gid = str(args["gallery_id"])
        g = stash.call(
            "query($id: ID!) { findGallery(id: $id) { title files { basename } } }", {"id": gid}
        )["findGallery"]
        if not g:
            raise RuntimeError(f"gallery {gid} not found")
        name = g["title"] or (g["files"][0]["basename"] if g["files"] else f"gallery {gid}")
        work = []
        if "images" in kinds:
            in_gallery = {"galleries": {"value": [gid], "modifier": "INCLUDES"}}
            work.append(("images", "images", item_ids(stash, "images", in_gallery)))
        elif "selected_images" in kinds:
            work.append(("selected images", "images", split("image_ids")))
        if "gallery" in kinds:
            work.append(("gallery", "galleries", [gid]))
        return f'"{name}"', work

    raise RuntimeError("bulk tag: no performer or gallery given")


def run_bulktag(stash, args):
    """Add (or remove) tags on a performer's items or a gallery's images."""
    tag_ids = [t for t in str(args.get("tag_ids", "")).split(",") if t]
    mode = "REMOVE" if args.get("op") == "remove" else "ADD"
    owner, work = bulk_work(stash, args)
    if not tag_ids or not work:
        log("warning", "Bulk tag: nothing to do (no tags or nothing chosen to tag).")
        return

    tags = stash.call(
        "query($ids: [ID!]) { findTags(ids: $ids, filter: {per_page: -1}) { tags { name } } }",
        {"ids": tag_ids},
    )["findTags"]["tags"]
    tag_list = ", ".join(t["name"] for t in tags)

    total = sum(len(ids) for _, _, ids in work) or 1
    done = 0
    for label, kind, ids in work:
        mutation, input_type = BULK_MUTATIONS[kind]
        for i in range(0, len(ids), BULK_BATCH):
            chunk = ids[i:i + BULK_BATCH]
            stash.call(
                f"mutation($i: {input_type}!) {{ {mutation}(input: $i) {{ id }} }}",
                {"i": {"ids": chunk, "tag_ids": {"ids": tag_ids, "mode": mode}}},
            )
            done += len(chunk)
            progress(done / total)
        verb = "Removed" if mode == "REMOVE" else "Added"
        where = "from" if mode == "REMOVE" else "on"
        if label == "gallery":
            log("info", f"{verb} {tag_list} {where} the gallery {owner}")
        else:
            log("info", f"{verb} {tag_list} {where} {len(ids)} of {owner} {label}")


# ---------------------------------------------------------------------------
# Review page, "Studios" tab: studios to merge (names that are misreadings of
# one another), to delete (junk or empty ones the watermark task made), and
# to add (account names read once, and items in a gallery or folder whose
# others all have a studio).

STUDIO_REVIEW_FIELDS = ("id name aliases urls details ignore_auto_tag favorite rating100 image_path "
                        "parent_studio { id name } child_studios { id } stash_ids { endpoint stash_id } tags { id } "
                        "scene_count image_count gallery_count")
STUDIO_ITEM_FIELDS = {
    "scene": "id title studio { id name } files { path } paths { screenshot }",
    "image": "id title studio { id name } galleries { id title } visual_files { ... on BaseFile { path } } paths { thumbnail }",
}
STUDIO_JUNK = re.compile(r"(?i)^(gmail|yahoo|hotmail|outlook|icloud|proton(mail)?)\b|\.(com|net|org|io|co|me|tv|xxx)$")
STUDIO_MATE_SHARE = 0.8  # of a gallery's / folder's items with a studio, this many have the same one
STUDIO_MATE_MIN = 0.25   # ... and at least this much of the whole gallery / folder


def _studio_name_key(name):
    """A studio name to compare: without a "Site: " prefix or "(platform)" ending
    ("FansDB: Babyfooji (onlyfans)" -> babyfooji)."""
    n = re.sub(r"^[^:]{2,20}:\s*", "", name or "")
    n = re.sub(r"\s*\([^)]*\)\s*$", "", n)
    return ocr_key(n)


def _handle_like(name):
    return bool(name) and " " not in name.strip()


def locked_studio_review(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "studio_review.json"))


def _studio_items(stash, sid):
    """{"scene": [ids], "image": [ids], "gallery": [ids]} with exactly this studio."""
    out = {}
    flt = {"studios": {"value": [str(sid)], "modifier": "INCLUDES", "depth": 0}}
    for kind, q, f, typ, arg in (("scene", "findScenes", "scenes", "SceneFilterType", "scene_filter"),
                                 ("image", "findImages", "images", "ImageFilterType", "image_filter"),
                                 ("gallery", "findGalleries", "galleries", "GalleryFilterType", "gallery_filter")):
        out[kind] = [x["id"] for x in stash.call(
            f"query($f: {typ}) {{ {q}({arg}: $f, filter: {{per_page: -1}}) {{ {f} {{ id }} }} }}", {"f": flt})[q][f]]
    return out


def _set_studio(stash, kind_ids, sid):
    """Studio sid (None: none) on {"scene": ids, "image": ids, "gallery": ids}."""
    for kind, ids in kind_ids.items():
        if not ids:
            continue
        mut, typ = {"scene": ("bulkSceneUpdate", "BulkSceneUpdateInput"), "image": ("bulkImageUpdate", "BulkImageUpdateInput"),
                    "gallery": ("bulkGalleryUpdate", "BulkGalleryUpdateInput")}[kind]
        stash.call(f"mutation($i: {typ}!) {{ {mut}(input: $i) {{ id }} }}", {"i": {"ids": [str(i) for i in ids], "studio_id": sid}})


def _keys_by_kind(keys):
    out = {"scene": [], "image": [], "gallery": []}
    for k in keys or []:
        kind, _, iid = str(k).partition(":")
        if kind in out and iid.isdigit():
            out[kind].append(iid)
    return out


def _studio_made_by_scan(st):
    """Looks like one the watermark task made: no picture, details or StashDB link."""
    return not st.get("details") and not st.get("stash_ids") and "default=true" in (st.get("image_path") or "default=true")


def run_studio_review(stash, plugin_dir, limit=30, skip=(), kinds=None):
    platform_names = {v[0].lower() for v in PLATFORMS.values()}
    studios = stash.call(f"{{ findStudios(filter: {{per_page: -1}}) {{ studios {{ {STUDIO_REVIEW_FIELDS} }} }} }}")["findStudios"]["studios"]
    by_id = {st["id"]: st for st in studios}
    items = {}
    for kind, q, f in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
        got = stash.call(f"{{ {q}(filter: {{per_page: -1}}) {{ {f} {{ {STUDIO_ITEM_FIELDS[kind]} }} }} }}")[q][f]
        items.update({f"{kind}:{x['id']}": x for x in got})
    with locked_studio_review(plugin_dir) as data:
        turned_down = set(data.get("no", []))
    skip = set(skip or ()) | turned_down

    def path_of(x):
        return (((x.get("files") or x.get("visual_files") or [{}])[0]) or {}).get("path") or ""

    def pic(key):
        kind, iid = key.split(":")
        x = items.get(key) or {}
        link = (x.get("paths") or {}).get("screenshot" if kind == "scene" else "thumbnail") or ""
        return {"key": key, "kind": kind, "id": iid, "title": x.get("title") or os.path.basename(path_of(x)),
                "picture": re.sub(r"^https?://[^/]+", "", link)}

    in_studio = defaultdict(list)
    for k, x in items.items():
        if x.get("studio"):
            in_studio[x["studio"]["id"]].append(k)

    def count(st):
        return (st.get("scene_count") or 0) + (st.get("image_count") or 0) + (st.get("gallery_count") or 0)

    def card(st):
        keys = in_studio.get(st["id"], [])
        keys = [k for k in keys if k.startswith("image:")][:4] + [k for k in keys if k.startswith("scene:")][:4]
        return {"id": st["id"], "name": st["name"], "aliases": st.get("aliases") or [], "count": count(st),
                "scenes": st.get("scene_count") or 0, "photos": st.get("image_count") or 0,
                "parent": (st.get("parent_studio") or {}).get("name"), "samples": [pic(k) for k in keys[:4]]}

    out = []
    # --- merge: names that are about the same (a misreading, or case / dots)
    names = [(st, [st["name"]] + (st.get("aliases") or [])) for st in studios
             if st["name"].lower() not in platform_names and not st.get("child_studios")]
    keys = {st["id"]: [_studio_name_key(n) for n in ns] for st, ns in names}
    parent = {st["id"]: st["id"] for st, _ in names}

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    import difflib
    why = {}
    for i, (a, an) in enumerate(names):
        for b, bn in names[i + 1:]:
            pa, pb = (a.get("parent_studio") or {}).get("id"), (b.get("parent_studio") or {}).get("id")
            if pa and pb and pa != pb:
                continue  # the same name on two platforms: two accounts
            if a.get("stash_ids") and b.get("stash_ids"):
                continue  # both from StashDB: two real studios
            best = 0.0
            for ka in keys[a["id"]]:
                for kb in keys[b["id"]]:
                    if not ka or not kb:
                        continue
                    if ka == kb:
                        best = 1.0
                    elif min(len(ka), len(kb)) >= 6 and abs(len(ka) - len(kb)) <= 2:
                        sm = difflib.SequenceMatcher(None, ka, kb)
                        if sm.quick_ratio() >= ALIKE_SAME:
                            best = max(best, sm.ratio())
            if best >= ALIKE_SAME:
                parent[root(a["id"])] = root(b["id"])
                why[(a["id"], b["id"])] = best
    clusters = defaultdict(list)
    for st, _ in names:
        clusters[root(st["id"])].append(st)
    for group in clusters.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda st: (-count(st), st["name"]))
        sig = "m:" + ",".join(sorted(st["id"] for st in group))
        if sig in skip:
            continue
        same = all(len(set(keys[st["id"]]) & set(keys[group[0]["id"]])) for st in group)
        out.append({"sig": sig, "st": True, "type": "merge", "studios": [card(st) for st in group], "keep": group[0]["id"],
                    "count": sum(count(st) for st in group),
                    "reasons": ["The same name written differently" if same else "Names that look like misreadings of each other"]})

    # --- delete: junk names, empty, or made from a watermark read in one item
    for st in studios:
        n = count(st)
        if st["name"].lower() in platform_names or st.get("child_studios"):
            continue
        sig = "d:" + st["id"]
        if sig in skip:
            continue
        made = _studio_made_by_scan(st)
        reason = None
        if made and (HANDLE_NOT_ACCOUNT.search(st["name"]) or STUDIO_JUNK.search(st["name"]) or len(ocr_key(st["name"])) < 4):
            reason = "Looks like a website, email or repost site rather than an account"
        elif n == 0:
            reason = "Has nothing in it"
        elif made and n == 1 and _handle_like(st["name"]) and (
                not st.get("urls") or ((st.get("parent_studio") or {}).get("name") or "").lower() in platform_names):
            reason = "Made from a watermark read in just 1 item"
        if reason:
            out.append({"sig": sig, "st": True, "type": "delete", "studios": [card(st)], "count": n, "reasons": [reason]})

    # --- add to an existing studio: the rest of a gallery / folder has it
    galleries = {g["id"]: g for g in stash.call(
        "{ findGalleries(filter: {per_page: -1}) { galleries { id title studio { id } folder { path } files { path } } } }")["findGalleries"]["galleries"]}

    def gallery_label(gid):
        g = galleries.get(gid) or {}
        path = (g.get("folder") or {}).get("path") or ((g.get("files") or [{}])[0] or {}).get("path") or ""
        return g.get("title") or os.path.basename(path.rstrip("/\\")) or "a gallery"
    places = defaultdict(list)  # (where, label) -> keys
    for k, x in items.items():
        gals = x.get("galleries") or []
        if gals:
            for g in gals:
                places[("g" + g["id"], gallery_label(g["id"]))].append(k)
        else:
            parts = re.split(r"[\\/]", path_of(x))
            if len(parts) > 1 and not re.search(r"\.(zip|cbz|cbr|rar|7z)$", parts[-2], re.I):
                places[("f" + "/".join(parts[:-1]), parts[-2])].append(k)
    mates = defaultdict(list)  # studio id -> [(where, label, keys missing it, have it, of)]
    for (where, label), ks in places.items():
        have = Counter(items[k]["studio"]["id"] for k in ks if items[k].get("studio"))
        missing = [k for k in ks if not items[k].get("studio")]
        if not have or not missing:
            continue
        sid, c = have.most_common(1)[0]
        if c < 2 or c < STUDIO_MATE_SHARE * sum(have.values()) or c < STUDIO_MATE_MIN * len(ks) or sid not in by_id:
            continue
        mates[sid].append((where, label, missing, c, len(ks)))
    for sid, places_of in mates.items():
        places_of.sort(key=lambda x: -len(x[2]))
        sig = f"s:{sid}:" + hashlib.sha1("|".join(sorted(w for w, *_ in places_of)).encode()).hexdigest()[:12]
        if sig in skip:
            continue
        missing = [k for _, _, ks, _, _ in places_of for k in ks]
        missing = list(dict.fromkeys(missing))
        gals = [w[1:] for w, *_ in places_of if w.startswith("g") and not (galleries.get(w[1:]) or {}).get("studio")]
        where, label, ks, c, of = places_of[0]
        kind = "gallery" if where.startswith("g") else "folder"
        reason = f"{c} of the {of} in the {kind} \"{label}\" are {by_id[sid]['name']}"
        if len(places_of) > 1:
            reason = f"Most of {len(places_of)} galleries / folders are {by_id[sid]['name']} (e.g. \"{label}\": {c} of {of})"
        out.append({"sig": sig, "st": True, "type": "join", "studio": card(by_id[sid]), "keys": missing + [f"gallery:{g}" for g in gals],
                    "count": len(missing), "samples": [pic(k) for k in missing[:6]], "all": [pic(k) for k in missing],
                    "places": [{"name": lbl, "missing": len(m)} for _, lbl, m, _, _ in places_of[:12]],
                    "reasons": [reason, f"{len(missing)} without a studio" + (f", and {len(gals)} galler{'y' if len(gals) == 1 else 'ies'}" if gals else "")]})

    # --- new studio: an account name read in a watermark only once so far
    try:
        with open(os.path.join(plugin_dir, "handle_pending.json")) as fh:
            pending = json.load(fh)
    except (OSError, ValueError):
        pending = {}
    ctx_names = {}
    for st in studios:
        if st["name"].lower() not in platform_names:
            for n in [st["name"]] + (st.get("aliases") or []):
                ctx_names.setdefault(n, st)
    for pkey, e in pending.items():
        ks = [f"scene:{i}" for i in e.get("scenes", [])] + [f"image:{i}" for i in e.get("images", [])]
        ks = [k for k in ks if k in items and not items[k].get("studio")]
        if not ks:
            continue
        spelling = max((e.get("spellings") or {pkey: 1}).items(), key=lambda kv: kv[1])[0]
        if clean_handle(spelling) is None or len(ocr_key(spelling)) < 4:
            continue
        near = close_name(list(ctx_names), spelling)
        if near:
            st = ctx_names[near]
            sig = f"s:{st['id']}:p{pkey}"
            if sig in skip:
                continue
            out.append({"sig": sig, "st": True, "type": "join", "studio": card(st), "keys": ks, "count": len(ks), "pending": pkey,
                        "samples": [pic(k) for k in ks[:6]], "all": [pic(k) for k in ks],
                        "reasons": [f"Watermark read as {plat_name(e.get('platform'))} @{spelling}, close to {st['name']}"]})
            continue
        sig = "a:" + pkey
        if sig in skip:
            continue
        # studios you have whose name is most like it, for "…or add to a studio you already have"
        near, seen_ids = [], set()
        for sc, n in sorted(((alike(n, spelling), n) for n in ctx_names), reverse=True):
            st = ctx_names[n]
            if sc < 0.5 or len(near) >= 6:
                break
            if st["id"] not in seen_ids:
                seen_ids.add(st["id"])
                near.append({"id": st["id"], "name": st["name"], "score": round(sc * 100), "count": count(st),
                             "via": n if n != st["name"] else None})
        out.append({"sig": sig, "st": True, "type": "add", "name": spelling, "platform": e.get("platform"), "pending": pkey, "near": near,
                    "keys": ks, "count": len(ks), "samples": [pic(k) for k in ks[:6]], "all": [pic(k) for k in ks],
                    "reasons": [f"Watermark read as {plat_name(e.get('platform'))} @{spelling} in {len(ks)} item{'s' if len(ks) != 1 else ''}"]})

    if kinds:
        out = [x for x in out if x["type"] in kinds]
    order = {"merge": 0, "delete": 1, "join": 2, "add": 3}
    out.sort(key=lambda x: (order[x["type"]], -x["count"] if x["type"] != "delete" else x["count"]))
    return {"items": out[:limit], "total": len(out),
            "kinds": dict(Counter(x["type"] for x in out))}


def _studio_snapshot(stash, st):
    """Enough to make a deleted studio again (Undo): its details, picture and items."""
    snap = {"name": st["name"], "aliases": st.get("aliases") or [], "urls": st.get("urls") or [],
            "details": st.get("details"), "ignore_auto_tag": st.get("ignore_auto_tag"), "favorite": st.get("favorite"),
            "rating100": st.get("rating100"), "parent_id": (st.get("parent_studio") or {}).get("id"),
            "tag_ids": [t["id"] for t in st.get("tags") or []],
            "stash_ids": [{"endpoint": s["endpoint"], "stash_id": s["stash_id"]} for s in st.get("stash_ids") or []],
            "items": _studio_items(stash, st["id"])}
    if "default=true" not in (st.get("image_path") or "default=true"):
        with contextlib.suppress(Exception):
            data, ctype = stash.fetch(st["image_path"])
            if ctype.startswith("image/") and len(data) < 8 * 1024 * 1024:
                snap["image"] = f"data:{ctype};base64," + base64.b64encode(data).decode()
    return snap


def _studio_restore(stash, snap):
    inp = {k: snap[k] for k in ("name", "aliases", "urls", "details", "ignore_auto_tag", "favorite", "rating100",
                                "parent_id", "tag_ids", "stash_ids", "image") if snap.get(k) not in (None, [], "")}
    st = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id } }", {"i": inp})["studioCreate"]
    _set_studio(stash, snap["items"], st["id"])
    return st["id"]


def _get_studio(stash, sid):
    st = stash.call(f"query($id: ID!) {{ findStudio(id: $id) {{ {STUDIO_REVIEW_FIELDS} }} }}", {"id": str(sid)})["findStudio"]
    if not st:
        raise RuntimeError(f"studio {sid} is gone")
    return st


def _drop_pending(plugin_dir, pkey):
    path = os.path.join(plugin_dir, "handle_pending.json")
    if not pkey:
        return None
    with locked_json(path) as pend:
        return pend.pop(pkey, None)


def run_studio_apply(stash, plugin_dir, action, data):
    """Does what a Studios card says. Returns what Undo needs."""
    data = data or {}
    if action == "no":
        with locked_studio_review(plugin_dir) as d:
            d["no"] = sorted(set(d.get("no", [])) | {data["sig"]})
        return {"action": "no", "sig": data["sig"]}
    if action == "merge":
        keep = _get_studio(stash, data["keep"])
        removed = []
        aliases = list(keep.get("aliases") or [])
        urls = list(keep.get("urls") or [])
        sids = [{"endpoint": x["endpoint"], "stash_id": x["stash_id"]} for x in keep.get("stash_ids") or []]
        for sid in data.get("others") or []:
            if str(sid) == str(keep["id"]):
                continue
            st = _get_studio(stash, sid)
            snap = _studio_snapshot(stash, st)
            _set_studio(stash, snap["items"], keep["id"])
            left = _studio_items(stash, sid)
            if any(left.values()):
                raise RuntimeError(f"couldn't move everything off {st['name']}")
            stash.call("mutation($i: StudioDestroyInput!) { studioDestroy(input: $i) }", {"i": {"id": str(sid)}})
            removed.append(snap)
            aliases += [st["name"]] + (st.get("aliases") or [])
            urls += [u for u in st.get("urls") or [] if u not in urls]
            sids += [x for x in snap["stash_ids"] if x not in sids]
        name = (data.get("name") or "").strip() or keep["name"]
        seen, al = {name.lower()}, []
        for a in aliases + ([keep["name"]] if name != keep["name"] else []):
            if a and a.lower() not in seen:
                seen.add(a.lower())
                al.append(a)
        stash.call("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }",
                   {"i": {"id": keep["id"], "name": name, "aliases": al, "urls": urls, "stash_ids": sids}})
        return {"action": "merge", "keep": keep["id"],
                "before": {"name": keep["name"], "aliases": keep.get("aliases") or [], "urls": keep.get("urls") or [],
                           "stash_ids": [{"endpoint": x["endpoint"], "stash_id": x["stash_id"]} for x in keep.get("stash_ids") or []]},
                "removed": removed}
    if action == "delete":
        st = _get_studio(stash, data["id"])
        snap = _studio_snapshot(stash, st)
        stash.call("mutation($i: StudioDestroyInput!) { studioDestroy(input: $i) }", {"i": {"id": st["id"]}})
        return {"action": "delete", "removed": [snap]}
    if action in ("add", "join"):
        kinds = _keys_by_kind(data.get("keys"))
        # only the ones still without a studio
        todo = {}
        for kind, q, f in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            got = find_by_ids(stash, q, f, kinds[kind], "id studio { id }") if kinds[kind] else []
            todo[kind] = [x["id"] for x in got if not x.get("studio")]
        todo["gallery"] = []
        for gid in kinds["gallery"]:
            g = stash.call("query($id: ID!) { findGallery(id: $id) { id studio { id } } }", {"id": gid})["findGallery"]
            if g and not g.get("studio"):
                todo["gallery"].append(g["id"])
        if not any(todo.values()):
            raise RuntimeError("they all have a studio by now")
        made = None
        if action == "add":
            name = clean_handle(data.get("name") or "") or (data.get("name") or "").strip()
            if not name:
                raise RuntimeError("give it a name first")
            studios = stash.call("{ findStudios(filter: {per_page: -1}) { studios { id name aliases urls } } }")["findStudios"]["studios"]
            before = {st["id"] for st in studios}
            st, _ = studio_for_handle(stash, studios, data.get("platform") or None, name, True)
            sid = st["id"]
            made = [s["id"] for s in studios if s["id"] not in before]
        else:
            sid = str(data["studio"])
        _set_studio(stash, todo, sid)
        alias_added = None
        alias = (data.get("alias") or "").strip()
        if action == "join" and alias:
            st = _get_studio(stash, sid)
            if alias.lower() not in [st["name"].lower()] + [a.lower() for a in st.get("aliases") or []]:
                with contextlib.suppress(Exception):  # (taken by another studio's name: skip it)
                    stash.call("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }",
                               {"i": {"id": sid, "aliases": (st.get("aliases") or []) + [alias]}})
                    alias_added = alias
        pend = _drop_pending(plugin_dir, data.get("pending"))
        return {"action": action, "studio": sid, "set": todo, "made": made or [], "pending": [data.get("pending"), pend],
                "alias": alias_added}
    raise RuntimeError(f"unknown studio action {action!r}")


def run_studio_undo(stash, plugin_dir, undo):
    action = undo.get("action")
    if action == "no":
        with locked_studio_review(plugin_dir) as d:
            d["no"] = [s for s in d.get("no", []) if s != undo["sig"]]
    elif action in ("merge", "delete"):
        if action == "merge":
            # the kept studio as it was first (its name, aliases and links free again)
            stash.call("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }",
                       {"i": {"id": undo["keep"], **undo["before"]}})
        for snap in undo.get("removed") or []:
            _studio_restore(stash, snap)  # (takes its items back off the kept one)
    elif action in ("add", "join"):
        _set_studio(stash, undo.get("set") or {}, None)
        if undo.get("alias"):
            with contextlib.suppress(Exception):
                st = _get_studio(stash, undo["studio"])
                stash.call("mutation($i: StudioUpdateInput!) { studioUpdate(input: $i) { id } }",
                           {"i": {"id": st["id"], "aliases": [a for a in st.get("aliases") or [] if a != undo["alias"]]}})
        for sid in reversed(undo.get("made") or []):
            if not any(_studio_items(stash, sid).values()):
                with contextlib.suppress(Exception):
                    stash.call("mutation($i: StudioDestroyInput!) { studioDestroy(input: $i) }", {"i": {"id": str(sid)}})
        pkey, pend = (undo.get("pending") or [None, None])
        if pkey and pend:
            with locked_json(os.path.join(plugin_dir, "handle_pending.json")) as p:
                p.setdefault(pkey, pend)
    return True


# ---------------------------------------------------------------------------
# Review page, "Galleries" tab. Suggested: loose photos (in no gallery) that
# belong in one - they look like its photos, or sit in the folder its photos
# are in - and performers / a studio most of a gallery's photos have but the
# gallery doesn't. Check: photos in a gallery that look like none of the rest
# (on the Check cards, what "g"). New: folders of loose photos that could be
# a gallery.

GALLERY_TRUSTED = 0.4   # a gallery whose photos mostly look like each other (median share)
GALLERY_ODD = 0.05      # a photo in it whose look-alikes are under 5% in that gallery
GALLERY_JOIN = 0.6      # a loose photo whose look-alikes are 60%+ in one gallery
GALLERY_MIN = 8         # photos (with a fingerprint) a gallery needs to be judged
GALLERY_NEW_MIN = 5     # loose photos in one folder to suggest a gallery
GALLERY_FOLDER_SHARE = 0.8
GALLERY_IMAGE_FIELDS = ("id title galleries { id } performers { id name } studio { id name } "
                        "visual_files { ... on BaseFile { path } } paths { thumbnail }")


def locked_gallery_review(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "gallery_review.json"))


def _gallery_world(stash):
    images = {f"image:{x['id']}": x for x in stash.call(
        f"{{ findImages(filter: {{per_page: -1}}) {{ images {{ {GALLERY_IMAGE_FIELDS} }} }} }}")["findImages"]["images"]}
    galleries = {g["id"]: g for g in stash.call(
        "{ findGalleries(filter: {per_page: -1}) { galleries { id title image_count folder { path } files { path } "
        "studio { id name } performers { id name } } } }")["findGalleries"]["galleries"]}
    return images, galleries


def _gallery_title(g):
    path = (g.get("folder") or {}).get("path") or ((g.get("files") or [{}])[0] or {}).get("path") or ""
    return g.get("title") or os.path.basename(path.rstrip("/\\")) or f"Gallery {g['id']}"


def _gallery_shares(stash, cfg, plugin_dir, images, fresh=False):
    """{image key: {gallery id: share}}: how much of each photo's 20 closest
    look-alikes (weighted, the closest counting most) are in each gallery.
    Only photos with a look-alike fingerprint. Kept 15 minutes."""
    path = os.path.join(plugin_dir, "gallery_look_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                return saved["share"]
    cache = LookCache(plugin_dir)
    keys = [k for k in images if (cache.data.get(k) or {}).get("e")]
    if not keys:
        raise RuntimeError('no photos have look-alike fingerprints yet - run "Look-alike tags: scan scenes and images" first')
    vecs = [cache.data[k]["e"] for k in keys]
    nn = look_knn(cfg, vecs, list(range(len(keys))), LOOK_K + 1)
    gal_of = [[g["id"] for g in images[k].get("galleries") or []] for k in keys]
    share = {}
    for i, near in enumerate(nn):
        near = [(j, d) for j, d in near if j != i][:LOOK_K]
        if not near:
            continue
        top = near[0][1] / 16129
        out, total = defaultdict(float), 0.0
        for j, dot in near:
            w = math.exp((dot / 16129 - top) / LOOK_TEMP)
            total += w
            for g in gal_of[j]:
                out[g] += w
        mine = {g: round(v / total, 3) for g, v in out.items() if total and v / total >= 0.02}
        for g in gal_of[i]:
            mine.setdefault(g, round(out.get(g, 0.0) / total, 3) if total else 0.0)
        share[keys[i]] = mine
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "share": share}, f)
    os.replace(tmp, path)
    return share


def _gallery_trust(images, share):
    """{gallery id: (median share of its own photos, their [(share, key)])} -
    galleries whose photos look alike enough to be judged by look-alikes."""
    members = defaultdict(list)
    for k, s in share.items():
        for g in images[k].get("galleries") or []:
            members[g["id"]].append((s.get(g["id"], 0.0), k))
    out = {}
    for g, lst in members.items():
        if len(lst) >= GALLERY_MIN:
            out[g] = (sorted(sc for sc, _ in lst)[len(lst) // 2], lst)
    return out


def _gallery_check_candidates(stash, cfg, plugin_dir, fresh=False):
    """Check cards for galleries: [[key, "g", gallery id, share, photos]] and names. Kept 15 minutes."""
    path = os.path.join(plugin_dir, "gallery_check_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                return saved["found"], saved["names"]
    images, galleries = _gallery_world(stash)
    share = _gallery_shares(stash, cfg, plugin_dir, images, fresh)
    found = []
    for g, (med, lst) in _gallery_trust(images, share).items():
        if med < GALLERY_TRUSTED:
            continue  # a gallery of mixed pictures: look-alikes can't judge it
        odd = [(sc, k) for sc, k in lst if sc < GALLERY_ODD]
        if len(odd) > max(2, len(lst) // 10):
            continue
        found += [[k, "g", g, sc, len(lst)] for sc, k in odd]
    names = {"g" + gid: _gallery_title(g) for gid, g in galleries.items()}
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "found": found, "names": names}, f)
    os.replace(tmp, path)
    return found, names


def run_gallery_review(stash, plugin_dir, limit=30, skip=(), kinds=None, fresh=False):
    cfg = settings(stash, plugin_dir)
    images, galleries = _gallery_world(stash)
    with locked_gallery_review(plugin_dir) as data:
        skip = set(skip or ()) | set(data.get("no", []))
    kinds = set(kinds or ("gjoin", "gdetails", "gnew"))

    def path_of(x):
        return (((x.get("visual_files") or [{}])[0]) or {}).get("path") or ""

    def folder_of(x):
        return os.path.dirname(path_of(x))

    def pic(k):
        x = images[k]
        return {"key": k, "kind": "image", "id": x["id"], "title": x.get("title") or os.path.basename(path_of(x)),
                "picture": re.sub(r"^https?://[^/]+", "", (x.get("paths") or {}).get("thumbnail") or "")}

    def gcard(g):
        return {"id": g["id"], "name": _gallery_title(g), "count": g.get("image_count") or 0}

    members = defaultdict(list)
    for k, x in images.items():
        for g in x.get("galleries") or []:
            members[g["id"]].append(k)
    loose = [k for k, x in images.items() if not x.get("galleries")]
    out = []

    # --- add loose photos to a gallery: they look like it, or they're in its folder
    if "gjoin" in kinds:
        votes = defaultdict(dict)  # gallery -> key -> reason
        try:
            share = _gallery_shares(stash, cfg, plugin_dir, images, fresh)
            trust = _gallery_trust(images, share)
            for k in loose:
                for g, sc in (share.get(k) or {}).items():
                    if sc >= GALLERY_JOIN and trust.get(g, (0,))[0] >= GALLERY_TRUSTED:
                        votes[g][k] = sc
        except RuntimeError as e:
            log("debug", f"gallery look-alikes skipped: {e}")
        folders = defaultdict(list)
        for k in loose:
            folders[folder_of(images[k])].append(k)
        by_folder = {}
        # which galleries the photos in each folder are in: a folder whose
        # photos went to several galleries (a camera roll sorted by hand)
        # says nothing about where the rest go
        folder_gals = defaultdict(Counter)
        for k, x in images.items():
            for g in x.get("galleries") or []:
                folder_gals[folder_of(x)][g["id"]] += 1
        for gid, ks in members.items():
            g = galleries.get(gid) or {}
            if g.get("folder") or g.get("files") or len(ks) < 3:
                continue  # a folder / zip gallery has its whole folder already
            top, n = Counter(folder_of(images[k]) for k in ks).most_common(1)[0]
            fg = folder_gals.get(top) or Counter()
            if (top and n >= GALLERY_FOLDER_SHARE * len(ks) and fg[gid] >= GALLERY_FOLDER_SHARE * sum(fg.values())
                    and len(folders.get(top, [])) <= len(ks)):
                for k in folders.get(top, []):
                    votes[gid].setdefault(k, None)
                by_folder[gid] = top
        for gid, ks in votes.items():
            g = galleries.get(gid)
            if not g or not ks:
                continue
            ordered = sorted(ks, key=lambda k: -(ks[k] or 0))
            sig = f"gj:{gid}:" + hashlib.sha1("|".join(sorted(ordered)).encode()).hexdigest()[:10]
            if sig in skip:
                continue
            looked = sum(1 for k in ordered if ks[k] is not None)
            reasons = []
            if looked:
                reasons.append(f"{looked} look{'s' if looked == 1 else ''} like its photos (60%+ of their closest look-alikes are in it)")
            if gid in by_folder:
                reasons.append(f"{sum(1 for k in ordered if ks[k] is None) or len(ordered)} in the folder its photos are in")
            out.append({"sig": sig, "gal": True, "type": "gjoin", "gallery": gcard(g), "keys": ordered, "count": len(ordered),
                        "samples": [pic(k) for k in ordered[:6]], "all": [pic(k) for k in ordered], "reasons": reasons})

    # --- performers / studio most of a gallery's photos have
    if "gdetails" in kinds:
        for gid, ks in members.items():
            g = galleries.get(gid)
            if not g or len(ks) < 3:
                continue
            have = {p["id"] for p in g.get("performers") or []}
            pc = Counter((p["id"], p["name"]) for k in ks for p in images[k].get("performers") or [])
            perfs = [{"id": pid, "name": nm, "on": c} for (pid, nm), c in pc.most_common()
                     if pid not in have and c >= max(3, len(ks) / 2)]
            studio = None
            if not g.get("studio"):
                sc = Counter((images[k]["studio"]["id"], images[k]["studio"]["name"]) for k in ks if images[k].get("studio"))
                if sc:
                    (sid, snm), c = sc.most_common(1)[0]
                    if c >= GALLERY_FOLDER_SHARE * len(ks):
                        studio = {"id": sid, "name": snm, "on": c}
            if not perfs and not studio:
                continue
            sig = f"gd:{gid}:" + ",".join(p["id"] for p in perfs) + (f":s{studio['id']}" if studio else "")
            if sig in skip:
                continue
            out.append({"sig": sig, "gal": True, "type": "gdetails", "gallery": gcard(g), "performers": perfs, "studio": studio,
                        "count": len(ks), "samples": [pic(k) for k in ks[:6]], "all": [pic(k) for k in ks],
                        "reasons": [f"Of its {len(ks)} photos: " + ", ".join(
                            [f"{p['name']} is on {p['on']}" for p in perfs] + ([f"{studio['name']} on {studio['on']}"] if studio else []))]})

    # --- a new gallery: a folder of loose photos
    if "gnew" in kinds:
        gal_folders = {((g.get("folder") or {}).get("path") or "").rstrip("/") for g in galleries.values()}
        folders = defaultdict(list)
        for k in loose:
            folders[folder_of(images[k])].append(k)
        for folder, ks in folders.items():
            if len(ks) < GALLERY_NEW_MIN or not folder or folder.rstrip("/") in gal_folders:
                continue
            sig = "gn:" + hashlib.sha1(folder.encode()).hexdigest()[:12]
            if sig in skip:
                continue
            ks.sort(key=lambda k: path_of(images[k]))
            sc = Counter((images[k]["studio"]["id"], images[k]["studio"]["name"]) for k in ks if images[k].get("studio"))
            studio = None
            if sc:
                (sid, snm), c = sc.most_common(1)[0]
                if c >= GALLERY_FOLDER_SHARE * len(ks):
                    studio = {"id": sid, "name": snm}
            pc = Counter((p["id"], p["name"]) for k in ks for p in images[k].get("performers") or [])
            perfs = [{"id": pid, "name": nm} for (pid, nm), c in pc.most_common(4) if c >= len(ks) / 2]
            name = re.sub(r"[_]+", " ", os.path.basename(folder)).strip()
            out.append({"sig": sig, "gal": True, "type": "gnew", "name": name, "folder": folder, "studio": studio, "performers": perfs,
                        "keys": ks, "count": len(ks), "samples": [pic(k) for k in ks[:6]], "all": [pic(k) for k in ks],
                        "reasons": [f"{len(ks)} photos in no gallery, all in the folder \"{os.path.basename(folder)}\""]
                        + ([f"with {', '.join(p['name'] for p in perfs)}"] if perfs else [])})

    order = {"gjoin": 0, "gdetails": 1, "gnew": 2}
    out.sort(key=lambda x: (order[x["type"]], -x["count"]))
    return {"items": out[:limit], "total": len(out)}


def _gallery_get(stash, gid):
    g = stash.call("query($id: ID!) { findGallery(id: $id) { id title studio { id } performers { id } } }", {"id": str(gid)})["findGallery"]
    if not g:
        raise RuntimeError(f"gallery {gid} is gone")
    return g


def run_gallery_apply(stash, plugin_dir, action, data):
    data = data or {}
    if action == "no":
        with locked_gallery_review(plugin_dir) as d:
            d["no"] = sorted(set(d.get("no", [])) | {data["sig"]})
        return {"action": "no", "sig": data["sig"]}
    ids = _keys_by_kind(data.get("keys"))["image"]
    if action == "gjoin":
        gid = str(data["gallery"])
        got = find_by_ids(stash, "findImages", "images", ids, "id galleries { id }")
        todo = [x["id"] for x in got if gid not in {g["id"] for g in x.get("galleries") or []}]
        if not todo:
            raise RuntimeError("they're all in it already")
        stash.call("mutation($i: GalleryAddInput!) { addGalleryImages(input: $i) }", {"i": {"gallery_id": gid, "image_ids": todo}})
        return {"action": "gjoin", "gallery": gid, "added": todo}
    if action == "gdetails":
        g = _gallery_get(stash, data["gallery"])
        before = {"performer_ids": [p["id"] for p in g.get("performers") or []],
                  "studio_id": (g.get("studio") or {}).get("id")}
        inp = {"id": g["id"]}
        add = [str(p) for p in data.get("performers") or [] if str(p) not in before["performer_ids"]]
        if add:
            inp["performer_ids"] = before["performer_ids"] + add
        if data.get("studio") and not before["studio_id"]:
            inp["studio_id"] = str(data["studio"])
        if len(inp) == 1:
            raise RuntimeError("nothing left to add")
        stash.call("mutation($i: GalleryUpdateInput!) { galleryUpdate(input: $i) { id } }", {"i": inp})
        return {"action": "gdetails", "gallery": g["id"], "before": before}
    if action == "gnew":
        title = (data.get("name") or "").strip()
        if not title:
            raise RuntimeError("give it a name first")
        got = find_by_ids(stash, "findImages", "images", ids, "id")
        if not got:
            raise RuntimeError("those photos are gone")
        inp = {"title": title}
        if data.get("studio"):
            inp["studio_id"] = str(data["studio"])
        if data.get("performers"):
            inp["performer_ids"] = [str(p) for p in data["performers"]]
        g = stash.call("mutation($i: GalleryCreateInput!) { galleryCreate(input: $i) { id } }", {"i": inp})["galleryCreate"]
        stash.call("mutation($i: GalleryAddInput!) { addGalleryImages(input: $i) }",
                   {"i": {"gallery_id": g["id"], "image_ids": [x["id"] for x in got]}})
        return {"action": "gnew", "gallery": g["id"]}
    raise RuntimeError(f"unknown gallery action {action!r}")


def run_gallery_undo(stash, plugin_dir, undo):
    action = undo.get("action")
    if action == "no":
        with locked_gallery_review(plugin_dir) as d:
            d["no"] = [s for s in d.get("no", []) if s != undo["sig"]]
    elif action == "gjoin":
        stash.call("mutation($i: GalleryRemoveInput!) { removeGalleryImages(input: $i) }",
                   {"i": {"gallery_id": undo["gallery"], "image_ids": undo["added"]}})
    elif action == "gdetails":
        stash.call("mutation($i: GalleryUpdateInput!) { galleryUpdate(input: $i) { id } }",
                   {"i": {"id": undo["gallery"], **undo["before"]}})
    elif action == "gnew":
        stash.call("mutation($i: GalleryDestroyInput!) { galleryDestroy(input: $i) }",
                   {"i": {"ids": [undo["gallery"]], "delete_file": False, "delete_generated": False}})
    return True
