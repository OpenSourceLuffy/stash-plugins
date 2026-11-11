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
import json
import operator
import os
import re
import shutil
import subprocess
import sys
import time
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
        headers = {k: v for k, v in self.headers.items() if k == "Cookie"}
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as resp:
            return resp.read(), resp.headers.get_content_type()


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
    }


def tag_lookup(stash, cfg, plugin_dir):
    """Tag names by id, and the ids of the tags to ignore."""
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
    names = {t["id"]: t["name"] for t in tags}
    excluded = {t["id"] for t in tags if t["name"].lower() in cfg["exclude"]}
    # Gallery-name tags this plugin made would otherwise become everyone's
    # "top tags" (a 200-photo gallery = 200 uses), so they never count
    excluded |= gallery_name_tag_ids(plugin_dir)
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
        "input": "PerformerUpdateInput", "fields": "id name tags { id }",
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
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)


def locked_record(plugin_dir):
    """Which tags the plugin added to which performer (auto_tags.json)."""
    return locked_json(os.path.join(plugin_dir, "auto_tags.json"))


def locked_queue(plugin_dir):
    """Performers waiting for a refresh after edits (queue.json)."""
    return locked_json(os.path.join(plugin_dir, "queue.json"))


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
            if cfg["ai"]["auto"] or cfg["ai"]["new_all"] or cfg["ai"]["face_auto"]:
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
        # tags, then a scene's studio and performers from accounts, then who
        # it looks like, then a title (which can use all of that)
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
        if ai["face_auto"]:
            try:
                run_faces(stash, plugin_dir, ["scene", "image"], ids=new)
            except Exception as e:
                log("warning", f"Faces for new items skipped: {e}")
        if ai["new_all"]:
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

GALLERY_FIELDS = "id title files { basename } folder { path }"


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


def gallery_name(g):
    """The gallery's title, else its zip's name (no extension), else its folder's."""
    if (g.get("title") or "").strip():
        return g["title"].strip()
    if g.get("files"):
        return os.path.splitext(g["files"][0]["basename"])[0].strip()
    if g.get("folder") and g["folder"].get("path"):
        return re.split(r"[\\/]", g["folder"]["path"].rstrip("\\/"))[-1].strip()
    return ""


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
        "{ galleries { image_count paths { cover } } } }",
        {"t": {"tags": has_tag}},
    )["findGalleries"]["galleries"]
    for g in galleries:
        if g["image_count"] and g["paths"].get("cover"):
            yield g["paths"]["cover"], "a gallery cover"

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


def give_picture(stash, tag, taken, cache, allow_shared=True, avoid=()):
    """Give one tag a picture no other tag has (by file hash). `taken` maps
    hash -> tag id and is updated; pictures in `avoid` (by hash) are never
    used. If every candidate is taken, the best one is used anyway when
    allow_shared. Returns (source, shared?) or None."""
    fallback = None
    for n, (where, source) in enumerate(candidates(stash, tag["id"])):
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
            if fallback is None:
                fallback = (data, ctype, h, source)
            continue
        set_picture(stash, tag, data, ctype, h, taken, cache)
        return source, False
    if fallback and allow_shared:
        data, ctype, h, source = fallback
        set_picture(stash, tag, data, ctype, h, taken, cache)
        return source, True
    return None


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
        for t in todo:
            try:
                result = give_picture(stash, t, taken, cache)
                if result:
                    source, shared = result
                    note = " (shared - every picture of it is already another tag's)" if shared else ""
                    log("info", f'Gave tag "{t["name"]}" a picture from {source}{note}')
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')


def run_tag_pictures(stash, plugin_dir):
    """Task: every tag without a picture gets its own one from the library."""
    tags = all_tags_for_pictures(stash)
    todo = sorted((t for t in tags if not has_picture(t)), key=uses)  # rarest first
    log("info", f"{len(todo)} of {len(tags)} tags have no picture")
    given, shared, nothing = 0, [], []
    with locked_picture_hashes(plugin_dir) as cache:
        taken = taken_map(picture_hashes(stash, cache, tags))
        for n, t in enumerate(todo):
            try:
                result = give_picture(stash, t, taken, cache)
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result:
                given += 1
                if result[1]:
                    shared.append(t["name"])
                log("debug", f'"{t["name"]}": picture from {result[0]}')
            else:
                nothing.append(t["name"])
            progress((n + 1) / max(len(todo), 1))
    log("info", f"Done: gave {given} tag(s) a picture.")
    if shared:
        log("info", f"{len(shared)} of them had to share a picture with another tag (no unused picture left): {short_list(shared)}")
    if nothing:
        log("info", f"{len(nothing)} tag(s) aren't used on anything with a picture, so they were left as they are: {short_list(nothing)}")


def run_unique_pictures(stash, plugin_dir):
    """Task: tags sharing the same picture get their own where possible. In
    each group the rarest tag keeps the picture."""
    tags = all_tags_for_pictures(stash)
    by_id = {t["id"]: t for t in tags}
    with locked_picture_hashes(plugin_dir) as cache:
        log("info", "Comparing tag pictures (the first run downloads each tag's picture once)...")
        hashes = picture_hashes(stash, cache, tags)
        groups = {}
        for tid, h in hashes.items():
            groups.setdefault(h, []).append(by_id[tid])
        groups = [sorted(g, key=uses) for g in groups.values() if len(g) > 1]
        movers = [t for g in groups for t in g[1:]]
        log("info", f"{len(groups)} picture(s) are shared by more than one tag ({len(movers) + len(groups)} tags).")
        taken = taken_map(hashes, prefer=[g[0]["id"] for g in groups])
        changed, stuck = 0, []
        for n, t in enumerate(sorted(movers, key=uses)):
            try:
                result = give_picture(stash, t, taken, cache, allow_shared=False)
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
        taken = {}  # pictures handed out in this run
        for n, t in enumerate(tags):
            old = current.get(t["id"])
            try:
                result = give_picture(stash, t, taken, cache, allow_shared=False,
                                      avoid={old} if old else ())
            except Exception as e:
                log("warning", f'Tag "{t["name"]}": could not set a picture: {e}')
                result = None
            if result:
                given += 1
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
        result = give_picture(stash, tag, taken, cache, allow_shared=False, avoid=set(recent) | now)
        while not result and recent:
            # been through them all: allow the one it had longest ago again
            recent.pop()
            result = give_picture(stash, tag, taken, cache, allow_shared=False, avoid=set(recent) | now)
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


def fill_titles(stash, work, apply, spellings=None, keep_names=None):
    """Give items without a title one made from their file name. `work`:
    [(kind, item)]. Returns [(kind, item, title)] (given, or would be)."""
    names = title_names(stash)
    if keep_names is None:
        keep_names = settings(stash)["titles_keep_names"]
    done = []
    for kind, item in work:
        if (item.get("title") or "").strip():
            continue
        title = title_from_path(item_path(kind, item), names, spellings, keep_names)
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
    for n in range(0, len(empty), 200):
        done += fill_titles(stash, empty[n:n + 200], apply=not preview, spellings=spellings)
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
        + f"; {len(empty) - len(done)} had only a camera / app name or an id and were left alone"
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
        "auto": bool(s.get("aiAuto")),
        "new_all": bool(s.get("aiNewAll")),
        "autotag_first": not s.get("aiNoAutoTag"),
        "face_url": (s.get("faceHelperUrl") or "http://host.docker.internal:11535").rstrip("/"),
        "face_auto": bool(s.get("faceAutoAssign")),
        # cosine likeness of two face fingerprints (SFace): about 0.36 and up
        # is usually the same person
        "face_min": num_pct(s.get("faceSuggestMin"), 36),
        "face_auto_min": num_pct(s.get("faceAutoMin"), 50),
    }


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


def ollama_tags(cfg, pictures, names):
    """Ask the model which of `names` apply to the pictures (JPEG bytes).
    The rules and the tag list go first and are the same every time, so
    Ollama reads them once and reuses that for every item (reading the list
    is most of the work); only the pictures are new each time."""
    ai = cfg["ai"]
    system = AI_SYSTEM + "\n\nAllowed tags: " + "; ".join(names)
    prompt = (
        f"Which of the allowed tags clearly apply to {'these frames from one video' if len(pictures) > 1 else 'this picture'}? "
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
    if not f or not (f.get("duration") or 0) > 2:
        return []
    return [grab_frame(stash, f["path"], f["duration"] * p)[0] for p in AI_FRAME_POINTS]


AI_ITEM_FIELDS = {
    "image": "id title date organized tags { id name } performers { id } studio { id } paths { thumbnail } "
             "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }",
    "scene": "id title date organized tags { id name } performers { id } studio { id } files { path duration }",
}


def ai_tag_item(stash, cfg, vocab, names, kind, item, apply, have_too=()):
    """Returns the names of the tags the model picked that the item doesn't
    have yet (added when `apply`). `have_too`: tag ids just added by Auto Tag."""
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
    picked = picked[: cfg["ai"]["max_tags"]]
    if apply and picked:
        mutation = "bulkImageUpdate(input: $i) { id }" if kind == "image" else "bulkSceneUpdate(input: $i) { id }"
        input_type = "BulkImageUpdateInput" if kind == "image" else "BulkSceneUpdateInput"
        stash.call(f"mutation($i: {input_type}!) {{ {mutation} }}",
                   {"i": {"ids": [item["id"]], "tag_ids": {"ids": [t for t, _ in picked], "mode": "ADD"}}})
    return [n for _, n in picked]


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
        if preview:
            import random
            targets = random.sample(targets, min(10, len(targets)))
    what = "Preview" if preview else "AI tagging"
    log("info", f"{what}: {len(targets)} item(s), choosing from your {len(names)} most used tags with {cfg['ai']['model']}")
    index = autotag_index(stash, plugin_dir) if cfg["ai"]["autotag_first"] else None
    added = tagged = from_names = 0
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
        try:
            picked = ai_tag_item(stash, cfg, vocab, names, kind, item, apply=not preview, have_too=pre_tags)
        except Exception as e:
            log("warning", f"{label}: {e}")
            if "urlopen" in repr(e) or "Connection" in repr(e):
                raise RuntimeError(f"lost contact with Ollama: {e}")
            continue
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
    took = time.time() - started
    per = f" ({took / max(len(targets), 1):.1f}s each)" if targets else ""
    names_note = f" Auto tag (file names) {'would add' if preview else 'added'} to {from_names} item(s)." if index is not None else ""
    if preview:
        log("info", f"Preview done{per}: {tagged} of {len(targets)} items would get new AI tags.{names_note} Nothing was saved.")
    else:
        log("info", f"Done{per}: AI added {added} tag(s) to {tagged} of {len(targets)} item(s).{names_note}")


def run_ai_queued(stash, plugin_dir, scene_ids, image_ids):
    """Background job: AI-tag scenes/images that were just added."""
    targets = []
    for kind, ids in (("scene", scene_ids), ("image", image_ids)):
        if not ids:
            continue
        query, field = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
        items = stash.call(f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ {AI_ITEM_FIELDS[kind]} }} }} }}",
                           {"ids": ids})[query][field]
        targets += [(kind, i) for i in items]
    if targets:
        run_ai(stash, plugin_dir, targets=targets)


def run_ai_suggest(stash, plugin_dir, kind, item_id):
    """Asked by the page (runPluginOperation): AI suggestions for one scene
    or image, nothing saved. Returns [{id, name}]."""
    cfg = settings(stash)
    ai_check(cfg)
    vocab, names = ai_vocabulary(stash, cfg, plugin_dir)
    query, field = ("findScene", "scene") if kind == "scene" else ("findImage", "image")
    item = stash.call(f"query($id: ID!) {{ {query}(id: $id) {{ {AI_ITEM_FIELDS[kind]} }} }}", {"id": str(item_id)})[query]
    if not item:
        raise RuntimeError(f"{kind} {item_id} not found")
    picked = ai_tag_item(stash, cfg, vocab, names, kind, item, apply=False) or []
    return [{"id": vocab[n.lower()], "name": n} for n in picked]


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
        + ("this video (the pictures are frames from it)" if len(pictures) > 1 else "this picture")
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
            t = re.sub(r"\s+", " ", t.strip().strip('"\'“”').rstrip("."))
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
    `preview`: only log what it would set (on `limit` random items)."""
    cfg = settings(stash, plugin_dir)
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
# Finds the OnlyFans / Fansly / Instagram / TikTok account a scene comes from -
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
}
PLATFORM_ORDER = ("onlyfans", "fansly", "tiktok", "instagram")  # which one wins if there are several
HANDLE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{1,39}$")
HANDLE_STOP = {"onlyfans", "fansly", "instagram", "tiktok", "com", "www", "http", "https", "profile", "me", "my",
               "username", "user", "handle", "link", "bio", "follow", "subscribe", "watermark"}
HANDLE_FRAME_POINTS = (0.03, 0.2, 0.4, 0.6, 0.8, 0.97)
HANDLE_SCENE_FIELDS = "id title details urls studio { id } performers { id } files { path duration }"


def clean_handle(h):
    h = (h or "").strip().lstrip("@").strip(" /.,:;!?\"'")
    h = re.sub(r"^(?:https?://)?(?:www\.)?(?:onlyfans|fansly|instagram|tiktok)\.com/@?", "", h, flags=re.I)
    h = h.split("/")[0].split("?")[0].rstrip(".")
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
    "fansly": r"fansly",
}


def handles_in_ocr(text):
    """[(platform or None, handle)] in text the model transcribed off a frame:
    profile links, or "@name" with the platform named next to it."""
    out = [(plat, h) for (plat, _), h in handles_in_text(text).items()]
    have = {h.lower() for _, h in out}
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


def accounts_context(stash):
    """What a scan matches against, loaded once per run: all studios, the
    existing studios whose name could show up as a watermark, and the
    performers by name / alias / profile link."""
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
            # names worth looking for in a watermark: two words, or 6+ letters
            if len(k) >= 6 or len([w for w in (n or "").split() if len(w) >= 2]) >= 2:
                perf_marks += [(pat, p) for pat in name_patterns(n)]
    return {"studios": studios, "marks": marks, "perf_keys": by_key, "perfs": perfs, "perf_marks": perf_marks}


def performer_for_handle(ctx, platform, handle):
    """A performer whose name or alias is the handle (ignoring spaces, dots,
    case), or who has the account's profile link."""
    p = ctx["perf_keys"].get(re.sub(r"[^a-z0-9]", "", handle.lower()))
    if p:
        return p
    url = PLATFORMS[platform][1].format(handle).lower()
    tail = url.split("://", 1)[1].replace("www.", "")
    for p in ctx["perfs"]:
        if any(tail == u.lower().rstrip("/").split("://", 1)[-1].replace("www.", "") for u in p.get("urls") or []):
            return p
    return None


def scene_accounts(stash, cfg, ctx, scene, use_ai=True):
    """What a scene shows: ([(platform, handle, where)] best first,
    [(existing studio, frames)] whose name is in the watermark)."""
    text = " ".join([scene.get("title") or "", scene.get("details") or "", " ".join(scene.get("urls") or []),
                     item_path("scene", scene)])
    from_text = handles_in_text(text)
    seen = defaultdict(int)  # (platform or None, handle lower) -> frames
    spelled = {}
    marks = defaultdict(int)  # studio id -> frames its name was read in
    mark_studio = {}
    pmarks = defaultdict(int)  # performer id -> frames their name was read in
    mark_perf = {}
    f = (scene.get("files") or [None])[0]
    if use_ai and f and (f.get("duration") or 0) > 1:
        for i, at in enumerate(HANDLE_FRAME_POINTS):
            try:
                pic = grab_frame(stash, f["path"], f["duration"] * at, width=1280)[0]
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
        if n >= 2 and plat:
            results.append((plat, spelled[hl], f"{n} frames"))
    results.sort(key=lambda r: (not r[2].startswith("text"), PLATFORM_ORDER.index(r[0])))
    studios = sorted(((mark_studio[i], n) for i, n in marks.items() if n >= 2), key=lambda x: -x[1])
    performers = [mark_perf[i] for i, n in pmarks.items() if n >= 2]
    return results, studios, performers


def process_scene_accounts(stash, cfg, ctx, scene, apply, use_ai=True):
    """Give one scene its studio (if it has none) and performers from the
    accounts / studio watermarks it shows. Returns (a log line or None,
    whether a new studio was made)."""
    handles, mark_studios, mark_perfs = scene_accounts(stash, cfg, ctx, scene, use_ai)
    name = os.path.basename(item_path("scene", scene)) or f"scene {scene['id']}"
    parts, inp, new = [], {"id": scene["id"]}, False
    if not scene.get("studio"):
        if mark_studios:
            st, n = mark_studios[0]
            parts.append(f'studio "{st["name"]}" [its watermark, in {n} frames]')
            inp["studio_id"] = st["id"]
        elif handles:
            plat, handle, where = handles[0]
            st, new = studio_for_handle(stash, ctx["studios"], plat, handle, apply)
            parts.append(f'studio "{st["name"]}"' + (f" (new, under {PLATFORMS[plat][0]})" if new else "")
                         + f" [{PLATFORMS[plat][0]} @{handle}, from {where}]")
            inp["studio_id"] = st["id"]
    have = {p["id"] for p in scene.get("performers") or []}
    add = []
    for p in [performer_for_handle(ctx, plat, handle) for plat, handle, _ in handles] + mark_perfs:
        if p and p["id"] not in have and p["id"] not in [x["id"] for x in add]:
            add.append(p)
    if add:
        parts.append(("performers " if len(add) > 1 else "performer ") + ", ".join(f'"{p["name"]}"' for p in add)
                     + " [account name / name in the watermark]")
        inp["performer_ids"] = sorted(have | {p["id"] for p in add}, key=int)
    if not parts:
        return None, False
    if apply and len(inp) > 1:
        stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }", {"i": inp})
    others = ", ".join(f"{PLATFORMS[p][0]} @{h}" for p, h, _ in handles[1:])
    return f'{"Would give" if not apply else "Gave"} {name} ' + " and ".join(parts) + (f"; also saw {others}" if others else ""), new


def studio_for_handle(stash, studios, platform, handle, apply):
    """The studio for an account: an existing one with that name, alias or
    profile URL, else a new one under the platform's studio. `studios` is
    the list of all studios (kept up to date)."""
    parent_name, url_fmt, _ = PLATFORMS[platform]
    url = url_fmt.format(handle)
    low = handle.lower()
    for st in studios:
        names = [st["name"].lower()] + [a.lower() for a in st.get("aliases") or []]
        if low in names or any(u.rstrip("/").lower() == url.lower() for u in st.get("urls") or []):
            return st, False
    if not apply:
        return {"id": None, "name": handle}, True
    parent = next((st for st in studios if st["name"].lower() == parent_name.lower()), None)
    if not parent:
        parent = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id name aliases urls } }",
                            {"i": {"name": parent_name}})["studioCreate"]
        studios.append(parent)
    st = stash.call("mutation($i: StudioCreateInput!) { studioCreate(input: $i) { id name aliases urls } }",
                    {"i": {"name": handle, "urls": [url], "parent_id": parent["id"]}})["studioCreate"]
    studios.append(st)
    return st, True


def run_accounts_items(stash, plugin_dir, scene_ids):
    """New scenes from a scan: studio and performers from accounts."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    ctx = accounts_context(stash)
    scenes = stash.call(f"query($ids: [ID!]) {{ findScenes(ids: $ids, filter: {{per_page: -1}}) {{ scenes {{ {HANDLE_SCENE_FIELDS} }} }} }}",
                        {"ids": scene_ids})["findScenes"]["scenes"]
    for sc in scenes:
        try:
            line, _ = process_scene_accounts(stash, cfg, ctx, sc, apply=True)
            if line:
                log("info", line)
        except Exception as e:
            log("warning", f"scene {sc['id']}: accounts skipped ({e})")


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
    ctx = accounts_context(stash)
    log("info", f"Looking for OnlyFans / Fansly / Instagram / TikTok accounts and studio watermarks in {len(scenes)} scene(s) without a studio"
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
                tmp = done_path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump({"scenes": sorted(done, key=int)}, fh)
                os.replace(tmp, done_path)
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
            got = stash.call(f"query($ids: [ID!]) {{ {query}(ids: $ids, filter: {{per_page: -1}}) {{ {field} {{ id title {'files { basename }' if kind == 'scene' else 'visual_files { ... on BaseFile { basename } }'} }} }} }}",
                             {"ids": ids})[query][field]
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
