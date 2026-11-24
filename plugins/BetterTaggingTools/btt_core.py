"""Better Organizing Tools / Better Organizing AI - shared code.

Both plugins ship this same file, so each works on its own. Better Tagging
Tools (no AI): performers', studios' and galleries' top tags, suggested tags,
bulk tagging, gallery-name tags, tag pictures, tag name capitalisation, auto
tag, dates and titles from file names, studios from account links. Better
Tagging AI: everything that asks the local AI model (Ollama) - AI tags, AI
titles, reading watermarks for studios and performers, and doing it all for
new items after a scan.

When both are installed they share settings (the AI plugin reads "Tags to
ignore", special spellings and the like from Better Organizing Tools) and
records (gallery-name tags, titles made from file names).
"""
import base64
import contextlib
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

try:  # file locks: fcntl on Linux / macOS, msvcrt on Windows
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

# Windows: Python reads and writes Stash's pipes (and the night runner's log
# file) in the console's code page, so names with accents, "→" or emoji come
# out garbled or as \u escapes - or stop a write. Stash talks UTF-8.
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Windows: ffmpeg and the other tools this runs get no console window of
# their own (a window flashing up for every frame otherwise, when Stash runs
# without a console)
if os.name == "nt":
    _NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
    _sp_popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
        _sp_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_no_window

BASE_ID = "BetterTaggingTools"   # the plugin without AI
AI_ID = "BetterTaggingAI"        # the AI plugin
PLUGIN_ID = BASE_ID              # the one running now (set by its script)


def plugin_name():
    return "Better Organizing AI Add-on" if PLUGIN_ID == AI_ID else "Better Organizing Tools"


def base_dir(plugin_dir):
    """Where Better Organizing Tools keeps its records (gallery-name tags,
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


class NotReady(RuntimeError):
    """Something has to be done first (e.g. a scan): the entry scripts log it
    as a short info line and hand the message back, without a traceback."""


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

    # Big libraries: asking Stash for every scene or image at once (per_page:
    # -1), with their tags, files and so on, can take more memory than Stash
    # has and stop it (reported on a library of tens of thousands of items).
    # Such requests are split into pages here, and long lists of ids into
    # batches; callers get the same answer.
    PAGE = 500
    _ALL = re.compile(r"\b(findScenes|findImages)\s*\(([^()]*?)filter:\s*\{\s*per_page:\s*-1")

    def call(self, query, variables=None):
        roots = re.findall(r"\b(find[A-Z]\w*)\s*\(", query)
        ids = (variables or {}).get("ids")
        if len(roots) == 1 and roots[0] in ("findScenes", "findImages"):
            if isinstance(ids, list) and len(ids) > self.PAGE and "$ids" in query:
                return self._in_batches(query, variables, roots[0])
            if "$ids" not in query and self._ALL.search(query):
                return self._in_pages(query, variables, roots[0])
        return self._call(query, variables)

    def _merge(self, out, got, root):
        part = got[root]
        if out is None:
            return {root: dict(part)}
        for k, v in part.items():
            if isinstance(v, list):
                out[root][k] = out[root].get(k, []) + v
        return out

    def _in_batches(self, query, variables, root):
        out = None
        ids = variables["ids"]
        for at in range(0, len(ids), self.PAGE):
            out = self._merge(out, self._call(query, dict(variables, ids=ids[at:at + self.PAGE])), root)
        return out

    def _in_pages(self, query, variables, root):
        # per_page: -1 -> per_page: PAGE, page: N (sorted by id when no order is given, so pages don't overlap)
        sort = "" if re.search(r"per_page:\s*-1\s*,\s*sort:", query) else ', sort: "id"'
        out, page = None, 1
        while True:
            q = re.sub(r"per_page:\s*-1", f"per_page: {self.PAGE}, page: {page}{sort}", query, count=1)
            got = self._call(q, variables)
            out = self._merge(out, got, root)
            lists = [v for v in got[root].values() if isinstance(v, list)]
            if not lists or len(lists[0]) < self.PAGE:
                return out
            page += 1

    def _call(self, query, variables=None):
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
    """A plugin's raw settings (Better Organizing Tools' unless said otherwise).
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
            _replace(tmp, path)
        except OSError:
            pass
    return s


def settings(stash, plugin_dir=None):
    s = plugin_settings(stash, plugin_dir)
    # AI settings belong to Better Organizing AI; ones still saved under Better
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
        # naming conventions: a style ("title", "words", "sentence" or "")
        # for each kind, and whether new ones get it straight away
        "naming": {k: naming_style(s.get(key)) for k, key in NAMING_SETTINGS.items()},
        "naming_new": bool(s.get("namingNew")),
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
    (unless "Top tags: don't count images") and galleries."""
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
    with open(_lock_path(path), "w") as lock:
        _lock_file(lock)
        try:
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            yield data
            tmp = f"{path}.{os.getpid()}.tmp"  # (two requests at once mustn't share it)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            _replace(tmp, path)
        finally:
            _unlock_file(lock)


def _lock_path(path):
    """Where the lock for a data file lives: in this computer's own temporary
    folder, not next to the data - file locks don't work in a folder shared
    into a container (Stash in Docker Desktop on a Mac: two processes could
    hold the same lock at once). Named after the data file's full path, so
    both plugins (they share this code) use the same lock for the same file."""
    import tempfile  # noqa: PLC0415
    folder = os.path.join(tempfile.gettempdir(), "better-organizing-locks")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, hashlib.sha1(os.path.abspath(path).encode()).hexdigest() + ".lock")


def _lock_file(handle):
    """Wait for an exclusive lock on an open file (released by _unlock_file,
    or when the process ends)."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return
    while True:  # Windows: LK_LOCK gives up after about 10 s, so keep trying
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        except OSError:
            time.sleep(0.2)


def _unlock_file(handle):
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _replace(tmp, path):
    """os.replace, which on Windows fails while someone else has the file
    open for a moment (e.g. reading it): then try again for a few seconds."""
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 49:
                raise
            time.sleep(0.1)


def pid_running(pid, marker=None):
    """Whether process `pid` is still running (and, with `marker`, whether
    its command line contains it, where that can be checked). Never signals
    it: on Windows os.kill(pid, 0) would end the process."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
        k32 = ctypes.windll.kernel32
        # (the types spelled out: a handle is 64 bits on 64-bit Windows)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return False
            if marker is None:
                return True
            # no command line without more privileges: settle for "a Python is running there"
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return "python" in os.path.basename(buf.value).lower()
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # exists, owned by someone else
    except OSError:
        return False
    if marker is None:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:  # Linux (Stash in Docker)
            return marker.encode() in f.read()
    except OSError:
        pass
    try:  # macOS and others without /proc
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return marker in out.stdout
    except (OSError, subprocess.SubprocessError):
        return True


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

    if PLUGIN_ID == AI_ID and kind == "SceneMarker":
        # a marker made or moved in Stash's own form: Stash deletes its
        # picture when its times change (and a new one has none) - make it,
        # so it shows without running Generate
        # ...and a tag it got that its scene doesn't have goes on the scene too
        if action == "Create" or {"tag_ids", "primary_tag_id"} & set(fields):
            try:
                if not plugin_settings(stash, plugin_dir, AI_ID).get("markerTagsStayOnMarker"):
                    marker_tags_to_scene(stash, plugin_dir, [hook["id"]])
            except Exception as e:
                log("debug", f"marker {hook['id']}: tags not added to its scene ({e})")
        if action == "Create" or {"seconds", "end_seconds"} & set(fields):
            with contextlib.suppress(Exception):
                ensure_marker_picture(stash, plugin_dir, hook["id"], wait=3 if action == "Create" else 0)
        return

    if PLUGIN_ID == AI_ID:
        # Better Organizing AI: new scenes and images for the AI
        if action == "Create" and kind == "Image":
            enqueue(stash, plugin_dir, facts_images=[str(hook["id"])])  # (its aesthetic score and labels)
        if action == "Create" and kind in ("Scene", "Image"):
            cfg = settings(stash, plugin_dir)
            if cfg["ai"]["auto"] or cfg["ai"]["new_all"] or cfg["ai"]["face_auto"] or \
                    (kind == "Image" and cfg["ai"]["photo_studios"]):
                key = "ai_scenes" if kind == "Scene" else "ai_images"
                enqueue(stash, plugin_dir, **{key: [str(hook["id"])]})
        return

    if action == "Create" and kind in ("Performer", "Studio", "Gallery"):
        # naming conventions for new ones (when switched on)
        with contextlib.suppress(Exception):
            if settings(stash, plugin_dir)["naming_new"]:
                for k, changed in run_naming(stash, plugin_dir, kinds=[kind.lower()], ids=[hook["id"]]).items():
                    if changed:
                        log("info", f"New {k} {hook['id']} renamed to its naming convention")

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
    # (new items for the AI are queued by Better Organizing AI's own hook)
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
              "titles_scene", "titles_image", "titles_gallery", "facts_images")


def enqueue(stash, plugin_dir, performers=(), galleries=(), images=(), tags=(), ai_scenes=(), ai_images=(),
            autotag_performers=(), autotag_studios=(), autotag_tags=(),
            dates_scene=(), dates_image=(), dates_gallery=(), top_studios=(), top_galleries=(),
            titles_scene=(), titles_image=(), titles_gallery=(), facts_images=()):
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
                                         titles_scene, titles_image, titles_gallery, facts_images)):
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

    if todo.get("facts_images"):
        try:
            run_picture_facts(stash, plugin_dir, ids=todo["facts_images"], quiet=True)
        except Exception as e:
            log("debug", f"Aesthetic scores for new images skipped: {e}")

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
        _replace(tmp, path)
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
    missing = keep_allowed("image", missing, tag_id)
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
            if what == "tag":
                missing = keep_allowed("image", missing, oid)
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
                missing = keep_allowed(k.rstrip("s"), missing, t["id"])
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


# --- "Top tags: tags to ignore" suggestions ----------------------------------------------
# Big tags that look like housekeeping rather than what's shown: their name
# (process, review, categories, underscores...), that they group other tags,
# or - with Better Organizing AI's picture fingerprints - that their items don't
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


# How good a picture is, by Apple's aesthetics score (on a Mac, through the
# face helper): -1..1, sharp, well lit and framed scores high. On 700 of your
# pictures: dark or blurry ones scored below 0, phone screenshots and comic
# pages about 0-0.1, ordinary frames about 0.2, studio photos 0.75 and up.
PICTURE_NICE = 0.3        # a picked picture needs this; else the next one is tried
PICTURE_MORE_TRIES = 15   # after one under it, how many more are tried for a better one
last_pick_quality = []    # the score of the picture give_picture picked last (None: not scored)
PHOTO_GOOD_ENOUGH = 0.1   # a photo scoring this (not dark, blurry or a screenshot) is used before any scene picture
_quality_url = []         # the face helper's address, once looked up ("" = no Apple scores)


PICTURE_NOBODY = -0.9     # a marker picture with nobody in it (a title card, a logo, an empty room) counts as this


def quality_ready(stash):
    """Whether Apple's scores are there (the face helper on a Mac)."""
    if not _quality_url:
        url = ""
        with contextlib.suppress(Exception):
            url = settings(stash)["ai"]["face_url"]
            with urllib.request.urlopen(url + "/health", timeout=4) as resp:
                url = url if json.loads(resp.read()).get("apple") else ""
        _quality_url.append(url)
    return bool(_quality_url[0])


def picture_quality(stash, data, people=False):
    """Apple's aesthetics score for a picture (bytes), a screenshot or document
    counting as -1 (and with people, one with nobody in it as PICTURE_NOBODY);
    None without a Mac's face helper (then any picture does)."""
    if not quality_ready(stash):
        return None
    body = json.dumps({"images": [base64.b64encode(data).decode()], "aesthetics_only": True, "people": people}).encode()
    try:
        req = urllib.request.Request(_quality_url[0] + "/apple", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read())["results"][0]
    except urllib.error.HTTPError:
        _quality_url[0] = ""  # (a face helper from before the scores)
        return None
    except Exception:
        return None
    if not res:
        return None
    if people and res.get("person") is not None:
        # (video frames with someone in them are often called "utility" -
        # watermarks? - so for those it's only the score; nobody: a title
        # card, a logo, an empty room)
        return float(res["score"]) if res["person"] else min(-1.0 if res.get("utility") else float(res["score"]), PICTURE_NOBODY)
    return -1.0 if res.get("utility") else float(res["score"])


def jpeg_size(data):
    """(width, height) of a JPEG (bytes), or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        kind, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if kind in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + size
    return None


def card_picture(stash, data):
    """A tall marker picture (a vertical video's) cut to a wide one around the
    people in it (the face helper, on a Mac), so cards and the marker row don't
    crop it in the middle; anything else as it is."""
    size = jpeg_size(data)
    if not size or size[1] <= size[0] * 1.05 or not quality_ready(stash):
        return data
    body = json.dumps({"images": [base64.b64encode(data).decode()], "card": True}).encode()
    with contextlib.suppress(Exception):
        req = urllib.request.Request(_quality_url[0] + "/apple", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            crop = (json.loads(resp.read())["results"][0] or {}).get("crop")
        if crop:
            return base64.b64decode(crop)
    return data


def saved_quality(where):
    """A photo's saved aesthetic score (the picture facts task), as Apple's
    -1..1 (a screenshot -1), when `where` is its thumbnail link; else None."""
    m = re.search(r"/image/(\d+)/thumbnail", where) if isinstance(where, str) else None
    f = _removed_file("picture_facts.json").get(m.group(1)) if m else None
    if not f or f.get("a") is None:
        return None
    return -1.0 if f.get("u") else f["a"] / 50 - 1


def give_picture(stash, tag, taken, cache, avoid=(), cands=None, keep=None, looks=None, better_than=None):
    """Give one tag a picture no other tag has (by file hash) - a picture is
    never used for two tags; if every candidate is another tag's, the tag
    gets nothing. `taken` maps hash -> tag id and is updated; pictures in
    `avoid` (by hash) are never used. cands: the pictures to try, best
    first (default: the usual ones). keep: the hash of the tag's current
    picture - if that's the one picked, nothing is saved. looks (TagLooks,
    when the face helper is running): a picture that looks the same as
    another tag's (a variant of the same page, a near-identical shot)
    counts as that tag's too. On a Mac, a picture Apple scores under
    PICTURE_NICE (blurry, dark, a screenshot) is passed over for the next one;
    if none of the next few is better, the best of them is used. better_than:
    only a picture Apple scores above this is used (none: nothing). Returns
    (source, False) or None; source is None when the tag already had that
    picture."""
    best, left = None, None  # the best low-scoring one so far: (score, data, ctype, h, fp, source)
    photo = None             # the best low-scoring photo so far (photos beat scene screenshots)

    def photo_ok():
        return photo is not None and photo[0] >= PHOTO_GOOD_ENOUGH

    def use(q, data, ctype, h, fp, source):
        last_pick_quality[:] = [q]
        if keep and h == keep:
            taken[h] = tag["id"]
            if looks and fp:
                looks.claim(tag["id"], fp, cache)
            return None, False
        set_picture(stash, tag, data, ctype, h, taken, cache)
        if looks and fp:
            looks.claim(tag["id"], fp, cache)
        return source, False

    for n, (where, source) in enumerate(cands if cands is not None else candidates(stash, tag["id"])):
        if n >= MAX_TRIES or (left is not None and left <= 0 and (photo_ok() or photo is None)):
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
        q = saved_quality(where)
        if q is None:
            q = picture_quality(stash, data)
        if better_than is not None and (q is None or q <= better_than):
            continue
        is_photo = is_photo_source(source)
        if q is not None and q < PICTURE_NICE:
            if best is None or q > best[0]:
                best = (q, data, ctype, h, fp, source)
            if is_photo and (photo is None or q > photo[0]):
                photo = (q, data, ctype, h, fp, source)
            left = PICTURE_MORE_TRIES if left is None else left - 1
            continue
        if not is_photo and photo_ok():
            return use(*photo)  # a decent photo beats a scene screenshot
        return use(q, data, ctype, h, fp, source)
    if photo_ok():
        return use(*photo)
    if best:
        return use(*best)
    return None


def is_photo_source(source):
    """A photo (or a gallery's page or cover) rather than a scene, marker or video frame."""
    s = (source or "").lower()
    return ("photo" in s or "galler" in s) and "scene" not in s


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


# --- better pictures: swap low-scoring tag and marker pictures --------------
PICTURE_BETTER_BY = 0.1   # a replacement must score this much more than the picture it replaces
MARKER_REPICK_AT = 2      # seconds into a marker its new picture is looked for (then +3, +6, then 2 and 4 before it)


def _marker_picture_quality_file(plugin_dir):
    return os.path.join(plugin_dir, "marker_picture_quality.json")


def run_better_pictures(stash, plugin_dir, preview=False, limit=None, what=("tags", "markers")):
    """Task (on a Mac): every tag picture and marker picture Apple's aesthetics
    score puts under PICTURE_NICE (blurry, dark, a screenshot) is replaced by a
    better one: for a tag, the next picture the usual way (most typical first,
    never one another tag has); for a marker, a frame a few seconds into it.
    Only a clearly better picture is used. Scores are remembered, so running it
    again only looks at new or changed pictures. preview: report on `limit`
    random tags and markers, saving nothing."""
    if not quality_ready(stash):
        raise RuntimeError("this needs the face helper on a Mac (Apple's aesthetics score): run face-helper/setup.sh there")
    if "tags" in what:
        _better_tag_pictures(stash, plugin_dir, preview, limit, 0.3 if "markers" in what else 1.0)
    if "markers" in what:
        _better_marker_pictures(stash, plugin_dir, preview, limit, 0.3 if "tags" in what else 0.0)
    progress(1)


def _better_tag_pictures(stash, plugin_dir, preview, limit, share):
    import random  # noqa: PLC0415
    tags = sorted(all_tags_for_pictures(stash), key=uses)
    if limit:
        tags = random.sample(tags, min(limit, len(tags)))
    swapped, kept, low = [], 0, 0
    with locked_picture_hashes(plugin_dir) as cache:
        current = picture_hashes(stash, cache, tags)
        taken = taken_map(current)
        # (a preview doesn't compare looks: that would remember pictures it didn't set)
        looks = None if preview else TagLooks.maybe(stash, plugin_dir, cache, tags)
        typical = None
        with contextlib.suppress(Exception):
            typical = TypicalPictures(stash, plugin_dir)
        with_pic = [t for t in tags if t["id"] in current]
        log("info", f"{'Preview: ' if preview else ''}checking the pictures of {len(with_pic)} tag(s)...")
        for n, t in enumerate(with_pic):
            progress(share * n / max(len(with_pic), 1))
            e = cache.get(t["id"]) or {}
            if e.get("q_link") == e.get("link") and "q" in e:
                q = e["q"]
            else:
                try:
                    q = picture_quality(stash, stash.fetch(t["image_path"])[0])
                except Exception:
                    continue
                if q is None:
                    continue
                e["q"], e["q_link"] = q, e.get("link")
            if q >= PICTURE_NICE or e.get("q_tried") == e.get("link"):
                continue
            low += 1
            old = current[t["id"]]
            real_set = None
            last_pick_quality[:] = []
            if preview:
                real_set = globals()["set_picture"]
                globals()["set_picture"] = lambda *a: None
            try:
                result = give_picture(stash, t, taken, cache, keep=old, looks=looks, better_than=q + PICTURE_BETTER_BY,
                                      cands=typical.candidates(t["id"]) if typical else None)
            except Exception as ex:
                log("warning", f'Tag "{t["name"]}": could not pick a picture: {ex}')
                result = None
            finally:
                if real_set:
                    globals()["set_picture"] = real_set
            if result and result[0]:
                new = cache.get(t["id"]) or {}
                nq = (last_pick_quality or [None])[0]
                if not preview:
                    if taken.get(old) == t["id"]:
                        del taken[old]
                    if nq is not None:
                        new["q"], new["q_link"] = nq, new.get("link")
                swapped.append(t["name"])
                log("info", f'Tag "{t["name"]}": {"would get" if preview else "new picture,"} '
                    f'scored {q:.2f} -> {nq if nq is None else round(nq, 2)}, from {result[0]}')
            else:
                kept += 1
                if not preview:
                    e["q_tried"] = e.get("link")
    log("info", f"Tags: {low} picture(s) scored under {PICTURE_NICE}; {len(swapped)} {'would be' if preview else ''} replaced, "
        f"{kept} kept (nothing clearly better that no other tag has).")


def _better_marker_pictures(stash, plugin_dir, preview, limit, start):
    """Marker pictures: blurry, dark or with nobody in it (Apple and the
    body-part detector) -> a better frame near the marker's start."""
    import random  # noqa: PLC0415
    gen = stash.call("{ configuration { general { generatedPath videoFileNamingAlgorithm } } }")["configuration"]["general"]
    want = "md5" if gen.get("videoFileNamingAlgorithm") == "MD5" else "oshash"
    markers = stash.call("{ findSceneMarkers(filter: {per_page: -1}) { scene_markers { id seconds end_seconds "
                         "scene { id files { path duration width height fingerprints { type value } } } } } }")["findSceneMarkers"]["scene_markers"]
    if limit:
        markers = random.sample(markers, min(limit, len(markers)))
    qpath = _marker_picture_quality_file(plugin_dir)
    try:
        with open(qpath) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    A = None
    better = tried = missing = mlow = cropped = 0
    log("info", f"{'Preview: ' if preview else ''}checking the pictures of {len(markers)} marker(s)...")
    for n, m in enumerate(markers):
        progress(start + (1 - start) * n / max(len(markers), 1))
        f = ((m.get("scene") or {}).get("files") or [None])[0]
        prints = {fp["type"]: fp["value"] for fp in (f or {}).get("fingerprints") or []}
        if not f or not prints.get(want) or not gen.get("generatedPath"):
            continue
        sec = int(float(m["seconds"]))
        path = os.path.join(gen["generatedPath"], "markers", prints[want], f"{sec}.jpg")
        try:
            st = os.stat(path)
        except OSError:
            missing += 1  # (not generated yet)
            continue
        if f.get("height") and f.get("width") and f["height"] > f["width"]:
            # a vertical video's tall picture: cut to a wide one around the people
            with open(path, "rb") as fh:
                old = fh.read()
            size = jpeg_size(old)
            if size and size[1] > size[0] * 1.05:
                new = card_picture(stash, old)
                if new is not old:
                    cropped += 1
                    if not preview:
                        tmp = path + ".bta.tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(new)
                        _replace(tmp, path)
                        st = os.stat(path)
        sig = f"{sec}:{st.st_size}:{int(st.st_mtime)}"
        ent = seen.get(m["id"]) or {}
        if ent.get("v") != 2:
            ent = {}  # (scored before people were looked for)
        if ent.get("sig") == sig and (ent.get("q", -2) >= PICTURE_NICE or ent.get("tried")):
            continue
        if ent.get("sig") == sig:
            q = ent["q"]
        else:
            with open(path, "rb") as fh:
                q = picture_quality(stash, fh.read(), people=True)
            if q is None:
                continue
            seen[m["id"]] = ent = {"sig": sig, "q": q, "v": 2}
        if q >= PICTURE_NICE:
            continue
        mlow += 1
        end = float(m["end_seconds"]) if m.get("end_seconds") else min(float(m["seconds"]) + 20, float(f.get("duration") or 1e9))
        at = min(float(m["seconds"]) + MARKER_REPICK_AT, max(end - 7, float(m["seconds"])))
        try:
            pic = _marker_pictures(stash, A, f["path"], [at], [float(m["seconds"])])[0]
        except Exception as ex:
            log("debug", f"marker {m['id']}: no frame ({ex})")
            pic = None
        data = base64.b64decode(pic) if pic else None
        nq = picture_quality(stash, data, people=True) if data else None
        if nq is not None and nq > q + PICTURE_BETTER_BY:
            better += 1
            if preview:
                log("info", f"marker {m['id']} (scene {m['scene']['id']} at {_marker_clock(sec)}): would get a picture scoring {q:.2f} -> {nq:.2f}")
            else:
                tmp = path + ".bta.tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                _replace(tmp, path)
                st = os.stat(path)
                seen[m["id"]] = {"sig": f"{sec}:{st.st_size}:{int(st.st_mtime)}", "q": nq, "v": 2}
        else:
            tried += 1
            ent["tried"] = True
        if not preview and n % 50 == 49:
            with open(qpath, "w") as fh:
                json.dump(seen, fh)
    if not preview:
        with open(qpath, "w") as fh:
            json.dump(seen, fh)
    log("info", f"Markers: {mlow} picture(s) scored under {PICTURE_NICE}; {better} {'would be' if preview else ''} replaced, "
        f"{tried} kept (no clearly better frame)" + (f"; {cropped} vertical scenes' picture(s) {'would be ' if preview else ''}cut to the people" if cropped else "")
        + (f"; {missing} have no picture yet (Generate makes them)" if missing else "")
        + ("." if preview else ". Pages already open may show the old pictures until reloaded."))


# --- scene covers: swap low-scoring ones for the best frame -----------------
# Stash makes a scene's cover (its thumbnail) from one frame. On a Mac, a cover
# Apple's aesthetics score puts low - blurry, dark, mid-move, or with nobody in
# it - is compared with frames from across the video, and the best of them
# becomes the cover if it's clearly better. The old cover is kept
# (cover-originals in Stash's config folder) and can be put back. A vertical
# video's frames are cut to wide covers around the people, like AI marker
# pictures (a cover Vertical Cover Stitcher made isn't one frame of the video,
# so it counts as not Stash's and is left alone unless allowed).
COVER_LOW = PICTURE_NICE           # a cover under this is compared with other frames
COVER_TRIES = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)  # where in the video (share of its length)
COVER_WIDTH = 1280                 # the most a new cover is wide (pixels)
COVER_MIN_NEW = 0.0                # a new cover must score at least this (not just less bad)
COVER_STASH_AT = 0.2               # where Stash takes a generated cover (share of the video)
COVER_SAME = 14                    # tiny greyscale pictures this close (average 0-255) are the same frame


def _cover_files(stash):
    """The covers checked (in the add-on's folder) and the replaced originals
    (in Stash's config folder, like screenshot-originals, so updating or
    reinstalling the add-on keeps them)."""
    return (os.path.join(_ai_data_dir(), "cover_quality.json"),
            os.path.join(stash.config_dir or "/root/.stash", "cover-originals"))


_FFMPEG_SLOTS = __import__("threading").BoundedSemaphore(4)  # ffmpeg runs at once (frame grabbing)


def _ffmpeg(stash):
    """Stash's ffmpeg: the one set in its settings, else on the PATH, else the
    copy Stash downloads into its config folder."""
    try:
        return ffmpeg_path(stash)  # (found once per run)
    except Exception:
        raise RuntimeError("ffmpeg wasn't found (Stash's Settings → System → ffmpeg path)") from None


def _video_frame(ff, path, t, width=COVER_WIDTH):
    """A JPEG of the video at `t` seconds, at most `width` wide (bytes or None)."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="bta-cover-") as folder:
        out = os.path.join(folder, "f.jpg")
        cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1", "-ss", f"{max(t, 0):.2f}",
               "-i", path, "-frames:v", "1", "-an", "-sn", "-vf", f"scale='min({width},iw)':-2", "-q:v", "2", out]
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        try:
            with open(out, "rb") as f:
                return f.read() or None
        except OSError:
            return None


# --- where a video's own content ends ----------------------------------------
# Phone clips often end on something that isn't the video: TikTok's and
# Instagram's end cards (a dark screen with their logo and the account, 4-5 s),
# a creator's logo card, or Control Center pulled down to stop a screen
# recording. Frames from across a video (covers, the AI's pictures, faces,
# look-alikes, body tags, AI markers) are taken from before that. An end card:
# after a cut, dark and flat frames that hardly change until the end (checked
# on 16 of your vertical clips: TikTok cards are brightness ~20 / contrast ~22,
# Instagram's 23 / 6-9, a logo card on black 4 / 15; content never stays that
# dark, flat and still after a cut).
VIDEO_END_SKIP = 2.0      # the last seconds are never used (Control Center, fades)
VIDEO_END_LOOK = 10.0     # how far back end cards are looked for
VIDEO_END_LONGEST = 600   # longer videos aren't checked (end cards are on short phone clips)
VIDEO_CARD_DARK = 45      # an end card's frames: brightness under this...
VIDEO_CARD_FLAT = 40      # ...contrast under this (a logo animating in: up to ~34)...
VIDEO_CARD_STILL = 10     # ...and changing less than this from one frame to the next
VIDEO_CARD_CUT = 25       # a cut into it: this much change in the half second before
VIDEO_CARD_MIN = 1.0      # seconds an end card lasts at least
_VIDEO_FPS, _VIDEO_W, _VIDEO_H = 4, 18, 32


def end_card_start(ff, path, duration):
    """When an end card starts (seconds), or None. See VIDEO_END_*."""
    import statistics  # noqa: PLC0415
    win = min(VIDEO_END_LOOK, duration * 0.7)
    s0 = duration - win
    try:
        raw = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-ss", f"{s0:.2f}",
                              "-i", path, "-an", "-sn", "-vf", f"fps={_VIDEO_FPS},scale={_VIDEO_W}:{_VIDEO_H},format=gray",
                              "-f", "rawvideo", "-"], capture_output=True, timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    n = _VIDEO_W * _VIDEO_H
    frames = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    if len(frames) < 6:
        return None
    diff = lambda a, b: sum(abs(x - y) for x, y in zip(a, b)) / n  # noqa: E731
    card = lambda x: sum(x) / n < VIDEO_CARD_DARK and statistics.pstdev(x) < VIDEO_CARD_FLAT  # noqa: E731
    i = len(frames) - 1
    if not card(frames[i]):
        return None
    while i > 0 and card(frames[i - 1]) and diff(frames[i - 1], frames[i]) < VIDEO_CARD_STILL:
        i -= 1
    if i == 0 or (len(frames) - i) / _VIDEO_FPS < VIDEO_CARD_MIN:
        return None  # (all dark: a dark video, not a card)
    before = frames[max(i - 2, 0)]
    if diff(before, frames[i]) < VIDEO_CARD_CUT:
        return None
    return s0 + max(i - 2, 0) / _VIDEO_FPS  # (from just before the cut: its fade)


_video_ends = {}


def content_end(stash, f):
    """Up to when a video file (with path and duration) shows its own content:
    before an end card, and never its last VIDEO_END_SKIP seconds (on clips
    over 4 s). Kept in video_ends.json."""
    dur = float(f.get("duration") or 0)
    if dur <= 4:
        return dur
    end = dur - min(VIDEO_END_SKIP, dur * 0.1)
    if dur > VIDEO_END_LONGEST:
        return end
    key = f"{f['path']}|{round(dur, 2)}"
    path = os.path.join(_ai_data_dir(), "video_ends.json")
    if not _video_ends:
        with contextlib.suppress(OSError, ValueError):
            with open(path) as fh:
                _video_ends.update(json.load(fh))
        _video_ends.setdefault("", 0)
    if key not in _video_ends:
        try:
            card = end_card_start(_ffmpeg(stash), f["path"], dur)
        except Exception:
            return end
        _video_ends[key] = round(card, 2) if card else None
        with contextlib.suppress(OSError, ValueError), locked_json(path) as rec:
            rec[key] = _video_ends[key]
    card = _video_ends.get(key)
    return min(end, card) if card and card > dur * 0.3 else end


def _wide_crop(ff, data):
    """A tall picture cut to 16:9 a third of the way down (where faces usually
    are) - for when the face helper can't find the people (e.g. Windows)."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="bta-cover-") as folder:
        src, out = os.path.join(folder, "p.jpg"), os.path.join(folder, "o.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf",
                        "crop=iw:trunc(iw*9/16/2)*2:0:(ih-iw*9/16)/3", "-q:v", "2", out],
                       capture_output=True, timeout=30, check=False)
        try:
            with open(out, "rb") as fh:
                return fh.read() or data
        except OSError:
            return data


def cover_picture(stash, ff, data):
    """A frame as a scene cover: a vertical video's tall frame is cut to a wide
    (16:9) one around the people, like AI marker pictures (the face helper on a
    Mac), else a third of the way down; a wide frame as it is."""
    size = jpeg_size(data) if data else None
    if not size or size[1] <= size[0] * 1.05:
        return data
    out = card_picture(stash, data)
    return out if out is not data else _wide_crop(ff, data)


def _tiny_grey(ff, data):
    """A 32x18 greyscale version of a picture (bytes), to compare pictures."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="bta-cover-") as folder:
        src = os.path.join(folder, "p.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        r = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-vf",
                            "scale=32:18,format=gray", "-f", "rawvideo", "-"], capture_output=True, timeout=30, check=False)
        return r.stdout if len(r.stdout) == 32 * 18 else None


def stash_made_cover(ff, path, duration, cover):
    """Whether a scene's cover is the frame Stash generates (at 20% of the
    video) - not a studio's or scraper's cover, or one you set yourself."""
    frame = _video_frame(ff, path, duration * COVER_STASH_AT, width=640)
    a, b = _tiny_grey(ff, cover), frame and _tiny_grey(ff, frame)
    if not a or not b:
        return False
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) <= COVER_SAME


COVER_FROM_VIDEO = 20             # a cover this close to one of the video's frames is a frame of it


def _sprite_greys(stash, ff, scene):
    """Stash's sprite sheet for a scene (its ~81 preview frames across the
    video) as tiny greyscale pictures, or None when there isn't one."""
    import tempfile  # noqa: PLC0415
    p = scene.get("paths") or {}
    if not p.get("sprite") or not p.get("vtt"):
        return None
    try:
        vtt = stash.fetch(p["vtt"])[0].decode(errors="ignore")
        sprite = stash.fetch(p["sprite"])[0]
    except Exception:
        return None  # (not generated)
    m = re.search(r"#xywh=\d+,\d+,(\d+),(\d+)", vtt)
    size = jpeg_size(sprite)
    if not m or not size:
        return None
    tw, th = int(m.group(1)), int(m.group(2))
    cols, rows = size[0] // tw, size[1] // th
    n = min(len(re.findall(r"#xywh=", vtt)), cols * rows)
    if not n:
        return None
    with tempfile.TemporaryDirectory(prefix="bta-cover-") as folder:
        src = os.path.join(folder, "s.jpg")
        with open(src, "wb") as fh:
            fh.write(sprite)
        raw = subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-vf",
                              f"crop={cols * tw}:{rows * th}:0:0,scale={cols * 32}:{rows * 18},format=gray",
                              "-f", "rawvideo", "-"], capture_output=True, timeout=60, check=False).stdout
    width = cols * 32
    if len(raw) < width * rows * 18:
        return None
    out = []
    for i in range(n):
        r, col = divmod(i, cols)
        out.append(b"".join(raw[(r * 18 + y) * width + col * 32:(r * 18 + y) * width + col * 32 + 32] for y in range(18)))
    return out


def _closest(grey, greys):
    return min((sum(abs(a - b) for a, b in zip(grey, g)) / len(grey) for g in greys if g), default=255)


def stitched_cover(stash, ff, scene_id, cover):
    """Whether this cover is the one Vertical Stitcher made for the scene (it
    records a 32x18 grey copy of each cover it sets in Stash's config folder,
    plugin-data/vertical_stitcher/stitched.json)."""
    path = os.path.join(stash.config_dir or "/root/.stash", "plugin-data", "vertical_stitcher", "stitched.json")
    try:
        with open(path) as fh:
            rec = json.load(fh).get(str(scene_id))
        made = base64.b64decode(rec["grey"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    grey = _tiny_grey(ff, cover)
    return bool(grey) and len(grey) == len(made) and sum(abs(a - b) for a, b in zip(grey, made)) / len(grey) <= COVER_SAME


def cover_kind(stash, ff, scene, f, cover, frames=None):
    """Where a scene's cover most likely came from: "stash" (the frame Stash
    generates - certain), else "scraped" (the scene is linked to StashDB /
    ThePornDB, or the cover isn't a frame of the video) or "picked" (a frame
    of the video, on a scene not linked anywhere: chosen in Stash's player).
    Stash doesn't record where a cover came from, so only "stash" is certain.
    frames: pictures from across the video, used when there's no sprite."""
    if stash_made_cover(ff, f["path"], float(f["duration"]), cover):
        return "stash"
    if stitched_cover(stash, ff, scene["id"], cover):
        return "stitched"  # (Vertical Stitcher's: always left alone)
    if scene.get("stash_ids"):
        return "scraped"
    grey = _tiny_grey(ff, cover)
    greys = _sprite_greys(stash, ff, scene) or [_tiny_grey(ff, x) for x in (frames or []) if x]
    if grey and greys and _closest(grey, greys) <= COVER_FROM_VIDEO:
        return "picked"
    return "scraped"


COVER_KIND_TEXT = {"scraped": "a scraped or uploaded cover", "picked": "a cover picked from the video",
                   "stitched": "a cover Vertical Stitcher made"}


def run_better_covers(stash, plugin_dir, preview=False, limit=None):
    """Task (on a Mac): scene covers Apple scores under COVER_LOW get the
    best-scoring of several frames from across the video, when it's clearly
    better. Only covers Stash generated (its frame at 20%) are changed unless
    the settings allow more: scraped or uploaded covers, covers picked from
    the video, organized scenes' covers (see cover_kind). Each cover is checked once (until it changes). preview: `limit`
    random landscape scenes, nothing saved."""
    import random  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    if not quality_ready(stash):
        raise NotReady("this needs the face helper on a Mac (Apple's aesthetics score): run face-helper/setup.sh there")
    ff = _ffmpeg(stash)
    qpath, backups = _cover_files(stash)
    try:
        with open(qpath) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    cfg = settings(stash, plugin_dir)["ai"]
    allowed = {"stash"} | ({"scraped"} if cfg["cover_scraped"] else set()) | ({"picked"} if cfg["cover_picked"] else set())
    scenes = stash.call("{ findScenes(filter: {per_page: -1}) { scenes { id title organized stash_ids { endpoint } "
                        "paths { screenshot sprite vtt } files { path duration width height } } } }")["findScenes"]["scenes"]
    todo = []
    for s in scenes:
        f = (s.get("files") or [None])[0]
        if not f or not s["paths"].get("screenshot") or not f.get("duration"):
            continue
        if s.get("organized") and not cfg["cover_organized"]:
            continue  # organized: left as it is (setting "also replace organized scenes' covers")
        if not preview and (seen.get(s["id"]) or {}).get("sig") == s["paths"]["screenshot"]:
            continue  # checked already, cover unchanged
        todo.append((s, f))
    if limit:
        todo = random.sample(todo, min(limit, len(todo)))
    log("info", f"{'Preview: ' if preview else ''}checking {len(todo)} scene cover(s)...")
    replaced = low = kept = custom = 0
    kinds = {}
    for n, (s, f) in enumerate(todo):
        progress(n / max(len(todo), 1))
        try:
            old = stash.fetch(s["paths"]["screenshot"])[0]
        except Exception:
            continue
        q = picture_quality(stash, old, people=True)
        if q is None:
            continue
        sig = s["paths"]["screenshot"]
        if q >= COVER_LOW:
            seen[s["id"]] = {"sig": sig, "q": round(q, 3)}
            if preview:
                log("info", f'Scene {s["id"]} "{s.get("title") or os.path.basename(f["path"])}": cover scores {q:.2f} - fine')
            continue
        dur = float(f["duration"])
        cend = content_end(stash, f)  # (frames from before an end card)
        name = s.get("title") or os.path.basename(f["path"])
        frames = None
        if not stash_made_cover(ff, f["path"], dur, old):
            with ThreadPoolExecutor(max_workers=3) as pool:
                frames = list(pool.map(lambda share: _video_frame(ff, f["path"], cend * share), COVER_TRIES))
            kind = cover_kind(stash, ff, s, f, old, frames)
        else:
            kind = "stash"
        if kind not in allowed:
            # a scraped / uploaded cover, or one picked from the video: left
            # alone unless its setting is on
            custom += 1
            seen[s["id"]] = {"sig": sig, "q": round(q, 3), "kind": kind}
            kinds[kind] = kinds.get(kind, 0) + 1
            if preview:
                log("info", f'Scene {s["id"]} "{name}": cover {q:.2f}, {COVER_KIND_TEXT[kind]} - left alone')
            continue
        low += 1
        if frames is None:
            with ThreadPoolExecutor(max_workers=3) as pool:
                frames = list(pool.map(lambda share: _video_frame(ff, f["path"], cend * share), COVER_TRIES))
        best = None
        for share, data in zip(COVER_TRIES, frames):
            if not data:
                continue
            data = cover_picture(stash, ff, data)  # (a vertical video's frame: cut to wide, around the people)
            fq = picture_quality(stash, data, people=True)
            if fq is not None and (best is None or fq > best[0]):
                best = (fq, data, share)
        if best and best[0] >= max(q + PICTURE_BETTER_BY, COVER_MIN_NEW):
            replaced += 1
            if preview:
                log("info", f'Scene {s["id"]} "{name}": cover {q:.2f} -> frame at {int(best[2] * 100)}% scoring {best[0]:.2f}')
                continue
            os.makedirs(backups, exist_ok=True)
            keep = os.path.join(backups, f"{s['id']}.jpg")
            if not os.path.exists(keep):  # (the first, original one)
                with open(keep, "wb") as fh:
                    fh.write(old)
            stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                       {"i": {"id": s["id"], "cover_image": "data:image/jpeg;base64," + base64.b64encode(best[1]).decode()}})
            new_sig = stash.call("query($id: ID!) { findScene(id: $id) { paths { screenshot } } }",
                                 {"id": s["id"]})["findScene"]["paths"]["screenshot"]
            seen[s["id"]] = {"sig": new_sig, "q": round(best[0], 3), "was": round(q, 3)}
            log("info", f'Scene {s["id"]} "{name}": new cover, scored {q:.2f} -> {best[0]:.2f} (frame at {int(best[2] * 100)}%)')
        else:
            kept += 1
            seen[s["id"]] = {"sig": sig, "q": round(q, 3), "tried": True}
            if preview:
                log("info", f'Scene {s["id"]} "{name}": cover {q:.2f}, no clearly better frame (best {best[0]:.2f})' if best
                    else f'Scene {s["id"]} "{name}": cover {q:.2f}, no frames could be taken')
        if not preview and n % 25 == 24:
            with locked_json(qpath) as d:
                d.update(seen)
    if not preview:
        with locked_json(qpath) as d:
            d.update(seen)
    progress(1)
    log("info", f"Scene covers: {low + custom} of {len(todo)} scored under {COVER_LOW}; {custom} of those left alone "
        f"({kinds.get('scraped', 0)} scraped or uploaded, {kinds.get('picked', 0)} picked from the video, "
        f"{kinds.get('stitched', 0)} made by Vertical Stitcher - see the "
        f"\"Scene covers: also replace...\" settings); {replaced} {'would be replaced' if preview else 'replaced'}, "
        f"{kept} kept (no clearly better frame)." + ("" if preview else " Originals are in cover-originals in Stash's config folder; "
        "\"Scene covers: put back the originals\" restores them."))


def _score100(q):
    """Apple's -1..1 score as 0-100 (like the photos' aesthetic field)."""
    return None if q is None else max(0, min(100, round((q + 1) * 50)))


COVER_IDEAS_LOOK = 24   # frames compared each round of "Suggest covers"
COVER_IDEAS_SHOW = 9    # ... and shown
COVER_IDEAS_APART = 22  # shown ones differ at least this much (tiny grey pictures, 0-255; 14 = the same
                        # frame, different shots 30+); relaxed step by step when a video is mostly one shot


COVER_IDEAS_WINDOW = 25  # variety is looked for among covers scoring this close to the best


def _varied(items, greys, n, apart=COVER_IDEAS_APART):
    """`n` of `items` (best first) that don't look like one picked before
    them: from those scoring within COVER_IDEAS_WINDOW of the best, then
    within 1.4 times that, lowering the bar step by step; near copies and covers
    far below the best only fill places left."""
    scores = [it.get("score") for it in items]
    top = max((x for x in scores if x is not None), default=None)
    near = [i for i, x in enumerate(scores) if top is None or x is None or x >= top - COVER_IDEAS_WINDOW]
    mid = [i for i, x in enumerate(scores) if i not in near and x >= top - 1.4 * COVER_IDEAS_WINDOW]
    far = [i for i in range(len(items)) if i not in near and i not in mid]
    picked = []

    def fill(pool, bar):
        for i in pool:
            if len(picked) >= n:
                return
            if i in picked:
                continue
            g = greys[i]
            if g and any(greys[j] and sum(abs(a - b) for a, b in zip(g, greys[j])) / len(g) < bar for j in picked):
                continue
            picked.append(i)

    # a different-looking cover a little lower down beats a near copy of a
    # better one; covers far below the best only fill what's left
    for bar in (apart, apart * 0.7, apart * 0.4):
        fill(near, bar)
        fill(mid, bar)
    for pool in (near, mid, far):
        fill(pool, 0)
    return [items[i] for i in sorted(picked)]


# --- with Vertical Stitcher (optional) -----------------------------------------
# When the Vertical Stitcher plugin is installed and turned on, a vertical
# scene's "Suggest covers" also offers covers stitched its way (several frames
# side by side), made with its own code: the best-scoring frames (on a Mac),
# and evenly spaced ones like it makes itself. A chosen one is noted as
# Vertical Stitcher's, so the covers tasks leave it alone.
COVER_STITCH_SETS = 2   # stitched suggestions from the best frames (plus one evenly spaced)
_stitcher = []


def vertical_stitcher(stash):
    """Vertical Stitcher's code (1.4 or later) when it's installed and turned on, else None."""
    if not _stitcher:
        mod = None
        with contextlib.suppress(Exception):
            on = any(p["id"] == "vertical_stitcher" and p.get("enabled")
                     for p in stash.call("{ plugins { id enabled } }")["plugins"])
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for d in sorted(os.listdir(root)) if on else []:
                path = os.path.join(root, d, "vertical_stitcher.py")
                if os.path.isfile(path):
                    import importlib.util  # noqa: PLC0415
                    spec = importlib.util.spec_from_file_location("vertical_stitcher_plugin", path)
                    m = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(m)
                    if hasattr(m, "pick_times") and hasattr(m, "record_stitched"):  # (1.4: build_cover takes times)
                        m.log = lambda level, msg: log(level, msg)
                        mod = m
                    break
        _stitcher.append(mod)
    return _stitcher[0]


def _stitch_sets(found, dur, k, scored, more):
    """Which frames to stitch: [[seconds, ...]] - the best-scoring ones a
    tenth of the video apart (two sets), and evenly spaced ones."""
    sets = []
    if scored:
        ranked = sorted((x for x in found if x["score"] is not None), key=lambda x: -x["score"])
        used = set()
        for _ in range(COVER_STITCH_SETS):
            pick = []
            for x in ranked:
                if any(abs(x["at"] - u) < dur * 0.06 for u in used) or any(abs(x["at"] - t) < dur * 0.1 for t in pick):
                    continue
                pick.append(x["at"])
                if len(pick) == k:
                    break
            if len(pick) < k:
                break
            sets.append(sorted(pick))
            used |= set(pick)
    start = min(0.5, dur * 0.05)
    off = (int(more) % 4) / 4
    sets.append([round(start + (dur - start) * (i + 1 + off) / (k + 1.5), 2) for i in range(k)])
    return sets


def run_cover_ideas(stash, plugin_dir, scene_id, more=0):
    """The scene edit page's "Suggest covers": frames from across the video
    (round `more` takes the ones in between), each with Apple's score where
    the face helper runs on a Mac (no scores elsewhere, e.g. on Windows), and
    the current cover's score and where it most likely came from."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    sc = stash.call("query($id: ID!) { findScene(id: $id) { id organized stash_ids { endpoint } "
                    "paths { screenshot sprite vtt } files { path duration width height } } }",
                    {"id": str(scene_id)})["findScene"]
    f = ((sc or {}).get("files") or [None])[0]
    if not f or not f.get("duration"):
        raise NotReady("this scene has no video file to take frames from")
    ff = _ffmpeg(stash)
    dur = content_end(stash, f)  # (not from an end card)
    scored = quality_ready(stash)
    # 24 frames each round (each "More frames" round between the last ones),
    # the best 9 shown; without scores there's no best, so 9 spread out
    n = COVER_IDEAS_LOOK if scored else COVER_IDEAS_SHOW
    off = (int(more) % 4) / 4 * (1 / (n + 1))  # rounds: 0, 1/4, 2/4, 3/4 of the gap
    shares = [(i + 1) / (n + 1) + off for i in range(n)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        frames = list(pool.map(lambda sh: _video_frame(ff, f["path"], dur * sh, width=480), shares))
    out = []
    for sh, data in zip(shares, frames):
        if not data:
            continue
        pic = cover_picture(stash, ff, data)
        q = picture_quality(stash, pic, people=True) if scored else None
        grey = _tiny_grey(ff, pic)
        out.append({"at": round(dur * sh, 2), "image": base64.b64encode(pic).decode(), "score": _score100(q),
                    "sig": base64.b64encode(grey).decode() if grey else ""})
    vertical = (f.get("height") or 0) > (f.get("width") or 0)
    stitched, st = [], vertical_stitcher(stash) if vertical else None
    if st:
        k = st.frame_count(stash)
        for times in _stitch_sets(out, dur, k, scored, more):
            try:
                pic = st.build_cover(f["path"], times, ff)
            except Exception as e:
                log("debug", f"scene {sc['id']}: stitched cover not made ({e})")
                continue
            q = picture_quality(stash, pic, people=True) if scored else None
            grey = _tiny_grey(ff, pic)
            stitched.append({"at": times[0], "stitch": [round(t, 2) for t in times],
                             "image": base64.b64encode(pic).decode(), "score": _score100(q),
                             "sig": base64.b64encode(grey).decode() if grey else ""})
        if scored:
            stitched.sort(key=lambda x: -(x["score"] if x["score"] is not None else -1))
        stitched = _varied(stitched, [base64.b64decode(x["sig"]) if x["sig"] else None for x in stitched], len(stitched), COVER_IDEAS_APART / 2)
    if scored:
        # the best, but not several of the same shot
        out.sort(key=lambda x: -(x["score"] if x["score"] is not None else -1))
        # (later rounds send more: the page shows 9 more each round)
        out = _varied(out, [base64.b64decode(x["sig"]) if x["sig"] else None for x in out],
                      COVER_IDEAS_SHOW * (2 if int(more) else 1))
    current = {"score": None, "kind": None}
    with contextlib.suppress(Exception):
        old = stash.fetch(sc["paths"]["screenshot"])[0]
        if scored:
            current["score"] = _score100(picture_quality(stash, old, people=True))
        current["kind"] = cover_kind(stash, ff, sc, f, old, frames)
    return {"frames": out, "stitched": stitched, "scored": scored, "current": current, "duration": dur, "looked": len(frames),
            "vertical": vertical,
            "backup": os.path.exists(os.path.join(_cover_files(stash)[1], f"{sc['id']}.jpg"))}


def run_cover_use(stash, plugin_dir, scene_id, at, stitch=None):
    """"Use this cover": the frame at `at` seconds, full size, becomes the
    scene's cover (or, with `stitch`, those frames stitched side by side by
    Vertical Stitcher). The cover it had first is kept (cover-originals) for
    Undo and for "Scene covers: put back the originals"."""
    sid = str(scene_id)
    sc = stash.call("query($id: ID!) { findScene(id: $id) { paths { screenshot } files { path duration } } }",
                    {"id": sid})["findScene"]
    f = ((sc or {}).get("files") or [None])[0]
    if not f:
        raise NotReady("this scene has no video file")
    ff = _ffmpeg(stash)
    st = vertical_stitcher(stash) if stitch else None
    if stitch:
        if not st:
            raise NotReady("Vertical Stitcher (1.4 or later) isn't installed and turned on any more")
        stitch = [float(t) for t in stitch]
        data = st.build_cover(f["path"], stitch, ff)
    else:
        data = _video_frame(ff, f["path"], float(at))
        if not data:
            raise RuntimeError("couldn't take that frame from the video")
        data = cover_picture(stash, ff, data)  # (a vertical video's: cut to wide, as shown)
    qpath, backups = _cover_files(stash)
    keep = os.path.join(backups, f"{sid}.jpg")             # the very first cover (for "put back the originals")
    undo = os.path.join(backups, f"{sid}.before-edit.jpg")  # the cover just before this choice (for Undo)
    old = stash.fetch(sc["paths"]["screenshot"])[0]
    os.makedirs(backups, exist_ok=True)
    for path in ([keep] if not os.path.exists(keep) else []) + [undo]:
        with open(path, "wb") as fh:
            fh.write(old)
    with locked_json(qpath) as d:  # (what the covers task knew about it, for Undo)
        prev_record = d.get(sid)
    with open(undo + ".json", "w") as fh:
        json.dump({"record": prev_record}, fh)
    stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
               {"i": {"id": sid, "cover_image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}})
    shot = stash.call("query($id: ID!) { findScene(id: $id) { paths { screenshot } } }", {"id": sid})["findScene"]["paths"]["screenshot"]
    with locked_json(qpath) as d:  # (chosen by you: the covers task leaves it alone)
        d[sid] = {"sig": shot, "kind": "stitched" if stitch else "picked", "by_you": True}
    if stitch:
        st.record_stitched(stash, sid, data, ff, stitch)  # (Vertical Stitcher's: left alone)
        log("info", f"Scene {sid}: new stitched cover from {' + '.join(_marker_clock(t) for t in stitch)} (chosen on the edit page)")
    else:
        log("info", f"Scene {sid}: new cover from {_marker_clock(float(at))} (chosen on the edit page)")
    return {"screenshot": shot, "backup": True}


# --- Performer edit: "Suggest profile pictures" -------------------------------
# Pictures of the performer cut to a portrait (2:3) around their face: from
# their photos (best aesthetics score first, when the photos have one) and
# frames of their scenes. The face has to be theirs (their known faces, from
# "Faces: learn performers' faces"; without those, a picture with one face).
# Each round looks at PROFILE_PHOTOS photos and PROFILE_FRAMES scene frames;
# with Apple's score (a Mac's face helper) the best come first, else the
# closest faces. The page puts the chosen one in Stash's own form (Set image),
# so Save keeps it and Cancel doesn't.
PROFILE_PHOTOS = 16
PROFILE_FRAMES = 8
PROFILE_SHARES = (0.3, 0.55, 0.15, 0.75, 0.42, 0.65, 0.22, 0.85)
PROFILE_SAME = 0.4        # how alike a face must be to the performer's known faces
PROFILE_MIN_FACE = 48     # pixels: smaller faces make a blurry portrait
PROFILE_SIZE = (600, 900)  # the portrait (2:3)


def _shrink(ff, data, longest=1600):
    """A picture (bytes) at most `longest` pixels on its longer side, as JPEG."""
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory(prefix="bta-prof-") as folder:
        src, out = os.path.join(folder, "p"), os.path.join(folder, "o.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-frames:v", "1", "-vf",
                        f"scale='if(gt(iw,ih),min({longest},iw),-2)':'if(gt(iw,ih),-2,min({longest},ih))'", "-q:v", "2", out],
                       capture_output=True, timeout=60, check=False)
        with contextlib.suppress(OSError):
            with open(out, "rb") as fh:
                return fh.read() or None
    return None


def portrait_crop(ff, data, box):
    """A 2:3 portrait of a picture (bytes) around a face box [x, y, w, h]:
    head and shoulders with room above, kept inside the picture."""
    import tempfile  # noqa: PLC0415
    x, y, w, h = (float(v) for v in box)
    cx, top = x + w / 2, y - h * 0.8
    tall = h * 3.4
    crop = (f"crop=w='floor(min(min(iw,ih*2/3),{tall:.1f}*2/3)/2)*2':h='floor(ow*3/4)*2'"
            f":x='max(0,min(iw-ow,{cx:.1f}-ow/2))':y='max(0,min(ih-oh,{top:.1f}))'")
    pw, ph = PROFILE_SIZE
    with tempfile.TemporaryDirectory(prefix="bta-prof-") as folder:
        src, out = os.path.join(folder, "p.jpg"), os.path.join(folder, "o.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-frames:v", "1", "-vf",
                        f"{crop},scale='min({pw},iw)':-2", "-q:v", "3", out], capture_output=True, timeout=60, check=False)
        with contextlib.suppress(OSError):
            with open(out, "rb") as fh:
                return fh.read() or None
    return None


def face_card_crop(ff, data, box, aspect=4 / 3):
    """A picture (bytes) cut to `aspect` around a face box [x, y, w, h], with
    the head and shoulders and some of what's around them, kept inside it."""
    import tempfile  # noqa: PLC0415
    x, y, w, h = (float(v) for v in box)
    cx, top, tall = x + w / 2, y - h * 1.1, h * 4.5
    crop = (f"crop=w='floor(min(min(iw,ih*{aspect:.4f}),{tall:.1f}*{aspect:.4f})/2)*2':h='floor(ow/{aspect:.4f}/2)*2'"
            f":x='max(0,min(iw-ow,{cx:.1f}-ow/2))':y='max(0,min(ih-oh,{top:.1f}))'")
    with tempfile.TemporaryDirectory(prefix="bta-card-") as folder:
        src, out = os.path.join(folder, "p.jpg"), os.path.join(folder, "o.jpg")
        with open(src, "wb") as fh:
            fh.write(data)
        subprocess.run([ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-frames:v", "1", "-vf",
                        f"{crop},scale='min(960,iw)':-2", "-q:v", "3", out], capture_output=True, timeout=60, check=False)
        with contextlib.suppress(OSError):
            with open(out, "rb") as fh:
                return fh.read() or None
    return None


FACE_CARD_SMALL = 0.3  # a photo's face smaller than this share of its height gets cut around


def run_face_card_picture(stash, plugin_dir, kind, item_id, at=None, emb=None):
    """Review → Faces: the picture a card shows - the frame of the scene where
    the face was seen best (not its cover, which may not show them), cut
    around that face; a photo cut around the face when it's small in it.
    emb: the card's face (q8), to pick the right face when there are several.
    {"image": base64 JPEG} or {} (then the card keeps its picture). Kept in
    face_cards/ (made once)."""
    folder = os.path.join(plugin_dir, "face_cards")
    key = f"{kind}_{int(item_id)}_{int(round(float(at or 0)))}"
    path = os.path.join(folder, key + ".jpg")
    with contextlib.suppress(OSError):
        with open(path, "rb") as fh:
            return {"image": base64.b64encode(fh.read()).decode()}
    cfg = settings(stash, plugin_dir)
    ff = _ffmpeg(stash)
    if kind == "scene":
        sc = stash.call("query($id: ID!) { findScene(id: $id) { files { path } } }", {"id": str(item_id)})["findScene"]
        f = ((sc or {}).get("files") or [None])[0]
        data = _video_frame(ff, f["path"], float(at or 0), width=1280) if f else None
    else:
        im = stash.call("query($id: ID!) { findImage(id: $id) { id paths { image thumbnail } visual_files { __typename ... on ImageFile { size } } } }",
                        {"id": str(item_id)})["findImage"]
        data = _shrink(ff, photo_picture(stash, im)) if im else None
    if not data:
        return {}
    faces = (face_helper(cfg, [data]) or [[]])[0]
    if not faces:
        return {} if kind == "image" else _face_card_save(path, data)
    want = unq8(emb) if emb else None
    face = max(faces, key=lambda fc: _dot(fc["emb"], want)) if want else _biggest(faces)
    if kind == "image":
        hgt = _picture_size(ff, data)[1]
        if hgt and face["box"][3] >= hgt * FACE_CARD_SMALL:
            return {}  # (the face is big in it already: the photo as it is)
    pic = face_card_crop(ff, data, face["box"])
    return _face_card_save(path, pic or data)


def _face_card_save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with contextlib.suppress(OSError):
        with open(path, "wb") as fh:
            fh.write(data)
        # (kept small: the oldest go once there are many)
        folder = os.path.dirname(path)
        names = sorted(os.listdir(folder), key=lambda n: os.path.getmtime(os.path.join(folder, n)))
        for n in names[:-600]:
            os.remove(os.path.join(folder, n))
    return {"image": base64.b64encode(data).decode()}


def _picture_size(ff, data):
    """(width, height) of a picture (bytes), or (0, 0)."""
    r = subprocess.run([ff, "-hide_banner", "-i", "pipe:0"], input=data, capture_output=True, timeout=30, check=False)
    m = re.search(rb", (\d{2,5})x(\d{2,5})", r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def run_profile_ideas(stash, plugin_dir, performer_id, more=0):
    """Performer edit → Suggest profile pictures: {"pictures": [{key, from,
    link, image (base64 JPEG), score (0-100 or None), match (%)}], "scored",
    "current", "looked", "known"}."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    pid, more = str(performer_id), int(more or 0)
    cfg = settings(stash, plugin_dir)
    ff = _ffmpeg(stash)
    p = stash.call("query($id: ID!) { findPerformer(id: $id) { id name image_path } }", {"id": pid})["findPerformer"]
    if not p:
        raise RuntimeError("no such performer")
    known = known_embs(load_face_index(plugin_dir).get(pid) or {})
    who = {"performers": {"value": [pid], "modifier": "INCLUDES"}}
    photos = stash.call("query($f: ImageFilterType) { findImages(image_filter: $f, filter: {per_page: -1, sort: \"id\"}) "
                        "{ images { id custom_fields paths { image thumbnail } visual_files { __typename ... on ImageFile { size } } } } }",
                        {"f": who})["findImages"]["images"]
    photos = [x for x in photos if (x.get("visual_files") or [{}])[0].get("__typename") == "ImageFile"]
    photos.sort(key=lambda x: -float((x.get("custom_fields") or {}).get("aesthetic") or 50))
    scenes = stash.call("query($f: SceneFilterType) { findScenes(scene_filter: $f, filter: {per_page: -1, sort: \"id\"}) "
                        "{ scenes { id title files { path duration } } } }", {"f": who})["findScenes"]["scenes"]
    scenes = [x for x in scenes if x.get("files") and (x["files"][0].get("duration") or 0) > 4]
    todo = [("image", x, None) for x in photos[more * PROFILE_PHOTOS:(more + 1) * PROFILE_PHOTOS]]
    if scenes:
        for k in range(PROFILE_FRAMES):
            n = more * PROFILE_FRAMES + k
            if n >= len(scenes) * len(PROFILE_SHARES):
                break
            todo.append(("scene", scenes[n % len(scenes)], PROFILE_SHARES[(n // len(scenes)) % len(PROFILE_SHARES)]))

    def picture(job):
        kind, item, share = job
        try:
            if kind == "image":
                return _shrink(ff, photo_picture(stash, item))
            f = item["files"][0]
            at = content_end(stash, f) * share
            return _video_frame(ff, f["path"], at, width=1600)
        except Exception as e:
            log("debug", f"profile ideas: {kind} {item['id']} left out ({e})")
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        pics = list(pool.map(picture, todo))
    jobs = [(j, d) for j, d in zip(todo, pics) if d]
    found = face_helper(cfg, [d for _, d in jobs]) if jobs else []
    scored = quality_ready(stash)
    out = []
    for ((kind, item, share), data), faces in zip(jobs, found):
        faces = [f for f in faces or [] if f["box"][3] >= PROFILE_MIN_FACE]
        if not faces:
            continue
        if known:
            best = max(faces, key=lambda f: max(_dot(f["emb"], e) for e in known))
            match = max(_dot(best["emb"], e) for e in known)
            if match < PROFILE_SAME:
                continue
        else:
            if len(faces) > 1:
                continue
            best, match = faces[0], None
        crop = portrait_crop(ff, data, best["box"])
        if not crop:
            continue
        q = picture_quality(stash, crop, people=True) if scored else None
        if kind == "image":
            src, link, key = "a photo", f"/images/{item['id']}", f"image:{item['id']}"
        else:
            at = round(content_end(stash, item["files"][0]) * share)
            src, link, key = f"the scene \"{item.get('title') or 'untitled'}\" at {_marker_clock(at)}", f"/scenes/{item['id']}?t={at}", f"scene:{item['id']}:{at}"
        out.append({"key": key, "from": src, "link": link, "image": base64.b64encode(crop).decode(),
                    "score": _score100(q), "match": round(match * 100) if match is not None else None,
                    "size": best["box"][3]})
    if scored:
        out.sort(key=lambda x: -(x["score"] if x["score"] is not None else -1))
    else:
        out.sort(key=lambda x: (-(x["match"] or 0), -x["size"]))
    # (photos taken moments apart look the same: the better one only)
    kept, greys = [], []
    for x in out:
        g = _tiny_grey(ff, base64.b64decode(x["image"]))
        if g and greys and _closest(g, greys) <= COVER_SAME:
            continue
        kept.append(x)
        greys.append(g)
    out = kept
    current = {"score": None}
    if scored and p.get("image_path") and "default=true" not in p["image_path"]:
        with contextlib.suppress(Exception):
            current["score"] = _score100(picture_quality(stash, stash.fetch(p["image_path"])[0], people=True))
    left = max(0, len(photos) - (more + 1) * PROFILE_PHOTOS) + max(0, len(scenes) * len(PROFILE_SHARES) - (more + 1) * PROFILE_FRAMES)
    return {"pictures": out, "scored": scored, "current": current, "looked": len(jobs), "known": bool(known),
            "more": left > 0, "photos": len(photos), "scenes": len(scenes)}


def run_cover_undo(stash, plugin_dir, scene_id):
    """Undo on the edit page: the cover the scene had just before "Use this
    cover" (not necessarily the very first one, which stays kept for "Scene
    covers: put back the originals")."""
    sid = str(scene_id)
    qpath, backups = _cover_files(stash)
    undo = os.path.join(backups, f"{sid}.before-edit.jpg")
    if not os.path.exists(undo):
        raise NotReady("there's no earlier cover kept for this scene")
    with open(undo, "rb") as fh:
        data = fh.read()
    prev = {}
    with contextlib.suppress(OSError, ValueError):
        with open(undo + ".json") as fh:
            prev = json.load(fh)
    stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
               {"i": {"id": sid, "cover_image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}})
    shot = stash.call("query($id: ID!) { findScene(id: $id) { paths { screenshot } } }", {"id": sid})["findScene"]["paths"]["screenshot"]
    with locked_json(qpath) as d:  # (as the covers task knew it, with the address it has now)
        if prev.get("record"):
            d[sid] = dict(prev["record"], sig=shot)
        else:
            d.pop(sid, None)
    keep = os.path.join(backups, f"{sid}.jpg")
    with contextlib.suppress(OSError):
        with open(keep, "rb") as fh:
            if fh.read() == data:  # back to the very first cover: nothing to put back any more
                os.remove(keep)
    for path in (undo, undo + ".json"):
        with contextlib.suppress(OSError):
            os.remove(path)
    log("info", f"Scene {sid}: the cover from before is back")
    return {"screenshot": shot}


def run_restore_covers(stash, plugin_dir):
    """Task: put back every scene cover the covers task replaced."""
    qpath, backups = _cover_files(stash)
    try:
        files = [f for f in os.listdir(backups) if f.endswith(".jpg") and ".before-edit" not in f]
    except OSError:
        files = []
    back = 0
    for name in files:
        sid = name[:-4]
        with open(os.path.join(backups, name), "rb") as fh:
            data = fh.read()
        try:
            stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }",
                       {"i": {"id": sid, "cover_image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}})
        except Exception as e:
            log("warning", f"Scene {sid}: couldn't put its cover back ({e})")
            continue
        os.remove(os.path.join(backups, name))
        for extra in (f"{sid}.before-edit.jpg", f"{sid}.before-edit.jpg.json"):  # (an edit page Undo no longer applies)
            with contextlib.suppress(OSError):
                os.remove(os.path.join(backups, extra))
        back += 1
    with locked_json(qpath) as d:  # (checked again next time, as the originals)
        for name in files:
            d.pop(name[:-4], None)
    log("info", f"Put back {back} original scene cover(s)." if back else "No replaced scene covers to put back.")


# --- screenshots of posts: cut down to the photo ---------------------------
# A phone screenshot of an Instagram / OnlyFans / Reddit post is mostly the
# app: the photo in it is small, so tags and pictures suffer. The face helper
# finds the photo (see shot_box there) and the file is replaced by it, at the
# same path so Stash keeps its performers and tags. The screenshot is kept in
# <config>/screenshot-originals/<its path> and "Screenshots: put back the
# originals" restores them all.
SHOT_EXTS = {".jpg": "jpg", ".jpeg": "jpg", ".png": "png", ".webp": "webp"}
# phone screen sizes (portrait): a screenshot is exactly one of these (a tall
# comic page or art isn't; the first try without this took ~60 of those)
PHONE_SCREENS = {(750, 1334), (1080, 1920), (1242, 2208), (828, 1792), (750, 1624), (1125, 2436), (1242, 2688),
                 (1170, 2532), (1284, 2778), (1179, 2556), (1290, 2796), (1206, 2622), (1320, 2868), (1080, 2340),
                 (1080, 2400), (720, 1600), (1440, 3200), (1440, 3120), (1080, 2316), (1080, 2408), (1440, 3088)}


def _shot_record(plugin_dir):
    return os.path.join(plugin_dir, "screenshot_crops.json")


def _shot_backup_dir(stash):
    return os.path.join(stash.config_dir or "/root/.stash", "screenshot-originals")


def _shot_candidates(stash, checked):
    """Images exactly a phone screen's size (and not drawn), as
    files of their own (not in a zip), not checked before as they are now."""
    out, page = [], 1
    while True:
        items = stash.call("query($f: FindFilterType) { findImages(filter: $f) { images { id title tags { name } "
                           "visual_files { __typename ... on ImageFile { path width height } } } } }",
                           {"f": {"page": page, "per_page": 1000, "sort": "id"}})["findImages"]["images"]
        for i in items:
            f = (i.get("visual_files") or [None])[0]
            if not f or f["__typename"] != "ImageFile" or len(i["visual_files"]) != 1:
                continue
            p = f["path"]
            ext = os.path.splitext(p)[1].lower()
            if ext not in SHOT_EXTS or re.search(r"(?i)\.(zip|cbz)/", p):
                continue
            if (f.get("width"), f.get("height")) not in PHONE_SCREENS or is_drawn(i):
                continue
            with contextlib.suppress(OSError):
                if checked.get(i["id"]) == int(os.stat(p).st_mtime):
                    continue
                out.append((i, p, ext))
        if len(items) < 1000:
            break
        page += 1
    return out


def run_screenshot_crop(stash, plugin_dir, preview=False):
    """Task: every image that's a phone screenshot of a post is cut down to the
    photo in it (the screenshot is backed up first). preview: list them only."""
    cfg = settings(stash, plugin_dir)
    url = cfg["ai"]["face_url"]
    try:
        with urllib.request.urlopen(url + "/health", timeout=5) as resp:
            if not json.loads(resp.read()).get("shotcrop"):
                raise RuntimeError("the face helper is too old: copy face-helper/face_helper.py over the running one and restart it")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the face helper at {url} ({getattr(e, 'reason', e)})")
    rec_path = _shot_record(plugin_dir)
    try:
        with open(rec_path) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        rec = {}
    rec.setdefault("cropped", {})
    rec.setdefault("checked", {})
    todo = _shot_candidates(stash, rec["checked"])
    log("info", f"{'Preview: ' if preview else ''}{len(todo)} phone-screenshot-shaped image(s) to look at...")
    backups = _shot_backup_dir(stash)
    done, changed = 0, []
    for n, (img, path, ext) in enumerate(todo):
        progress(n / max(len(todo), 1))
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            body = json.dumps({"images": [base64.b64encode(data).decode()], "format": [SHOT_EXTS[ext]]}).encode()
            req = urllib.request.Request(url + "/shotcrop", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                res = json.loads(resp.read())["results"][0] or {}
        except Exception as e:
            log("warning", f"image {img['id']}: couldn't check it ({e})")
            continue
        label = f"image {img['id']}" + (f' "{img["title"]}"' if img.get("title") else "")
        if not res.get("box"):
            if not preview:
                rec["checked"][img["id"]] = int(os.stat(path).st_mtime)
            continue
        done += 1
        x0, y0, x1, y1 = res["box"]
        if preview:
            log("info", f"{label}: would keep the photo at {x1 - x0}x{y1 - y0} (from {x0},{y0})")
            continue
        backup = os.path.join(backups, path.lstrip("/"))
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        tmp = path + ".bta-crop.tmp"
        with open(tmp, "wb") as fh:
            fh.write(base64.b64decode(res["crop"]))
        _replace(tmp, path)
        rec["cropped"][img["id"]] = {"path": path, "backup": backup, "box": res["box"], "at": time.strftime("%Y-%m-%d %H:%M")}
        rec["checked"][img["id"]] = int(os.stat(path).st_mtime)
        changed.append(path)
        log("info", f"{label}: cut down to the photo ({x1 - x0}x{y1 - y0})")
        if n % 20 == 19:
            _shot_save(rec_path, rec)
    if not preview:
        _shot_save(rec_path, rec)
        _shot_forget(plugin_dir, [iid for iid, r in rec["cropped"].items() if r["path"] in set(changed)])
        _shot_rescan(stash, changed)
    progress(1)
    log("info", f"{'Preview done' if preview else 'Done'}: {done} screenshot(s) {'would be' if preview else ''} cut down to their photo"
        + ("" if preview else f"; the originals are in {backups}. \"Screenshots: put back the originals\" undoes it."))


def _shot_forget(plugin_dir, ids):
    """Cropped (or put back) images: what was worked out from the old picture
    (look-alike fingerprint, faces, body pose, body tags and AI tags done) is
    dropped, so the next runs look at the new one."""
    keys = {f"image:{i}" for i in ids}
    if not keys:
        return
    for name in ("look_items.json", "face_items.json", "poses.json"):
        path = os.path.join(plugin_dir, name)
        with contextlib.suppress(OSError, ValueError):
            with open(path) as fh:
                d = json.load(fh)
            if keys & set(d):
                for k in keys:
                    d.pop(k, None)
                _shot_save(path, d)
    with contextlib.suppress(OSError, ValueError):  # (picture_facts.json is keyed by the bare id)
        path = os.path.join(plugin_dir, "picture_facts.json")
        with open(path) as fh:
            d = json.load(fh)
        if {str(i) for i in ids} & set(d):
            for i in ids:
                d.pop(str(i), None)
            _shot_save(path, d)
    for name in ("body_done.json", "ai_done.json"):
        with contextlib.suppress(OSError, ValueError), locked_json(os.path.join(plugin_dir, name)) as d:
            for field in ("images", "redo_images"):
                if isinstance(d.get(field), list):
                    d[field] = [x for x in d[field] if str(x) not in set(map(str, ids))]


def _shot_save(path, rec):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    _replace(tmp, path)


def _shot_rescan(stash, paths):
    """Stash looks at the changed files again (new size, fingerprint and thumbnail)."""
    if not paths:
        return
    with contextlib.suppress(Exception):
        stash.call("mutation($i: ScanMetadataInput!) { metadataScan(input: $i) }",
                   {"i": {"paths": paths, "scanGenerateThumbnails": True, "rescan": True}})
        log("info", f"Asked Stash to scan the {len(paths)} changed file(s) again (a scan job in the queue).")


def run_screenshot_restore(stash, plugin_dir):
    """Task: every screenshot cut down by the crop task goes back to the original."""
    rec_path = _shot_record(plugin_dir)
    try:
        with open(rec_path) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        rec = {}
    cropped = rec.get("cropped") or {}
    if not cropped:
        log("info", "Nothing to put back: no screenshots have been cropped.")
        return
    back, restored, missing = [], [], 0
    for iid, r in list(cropped.items()):
        if not os.path.exists(r["backup"]):
            missing += 1
            continue
        shutil.copy2(r["backup"], r["path"])
        back.append(r["path"])
        restored.append(iid)
        cropped.pop(iid)
        rec.get("checked", {})[iid] = int(os.stat(r["path"]).st_mtime)  # (not cropped again by itself)
    _shot_save(rec_path, rec)
    _shot_forget(plugin_dir, restored)
    _shot_rescan(stash, back)
    log("info", f"Put back {len(back)} original screenshot(s)" + (f"; {missing} backup(s) missing" if missing else "") + ".")


# --- picture facts: an aesthetic score and what Apple sees, on each image ---
# Saved on the image as custom fields Stash can filter by (Images → Filter →
# Custom fields): "aesthetic" 0-100 (Apple's aesthetics score: sharp, well lit,
# framed; a screenshot about 50, studio photos 85+) and "seen" (Apple's picture
# labels, e.g. "beach, ocean, swimsuit": filter with "includes beach"). Kept in
# picture_facts.json too, for Review (better-looking photos first) and the
# picture picking.
FACTS_BATCH = 16


def _facts_cache(plugin_dir):
    try:
        with open(os.path.join(plugin_dir, "picture_facts.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def picture_facts(plugin_dir):
    """{image id: {"a": aesthetic 0-100 or None, "u": screenshot-like, "l": labels}}."""
    return _facts_cache(plugin_dir)


def run_picture_facts(stash, plugin_dir, ids=None, redo=False, quiet=False):
    """Task: the aesthetic score and labels for every image not done yet (or
    `ids`), saved as the image's custom fields "aesthetic" and "seen"."""
    if not quality_ready(stash):
        raise RuntimeError("this needs the face helper on a Mac (Apple's Vision): run face-helper/setup.sh there")
    url = _quality_url[0]
    cache = _facts_cache(plugin_dir)
    fields = "id paths { thumbnail } visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }"
    if ids:
        items = find_by_ids(stash, "findImages", "images", list(ids), fields)
    else:
        items, page = [], 1
        while True:
            got = stash.call("query($f: FindFilterType) { findImages(filter: $f) { images { " + fields + " } } }",
                             {"f": {"page": page, "per_page": 1000, "sort": "id"}})["findImages"]["images"]
            items += got
            if len(got) < 1000:
                break
            page += 1
    todo = []
    for it in items:
        f = (it.get("visual_files") or [None])[0]
        if not f or f["__typename"] != "ImageFile":
            continue
        if redo or (cache.get(it["id"]) or {}).get("s") != f["path"]:
            todo.append((it, f["path"]))
    if not quiet:
        log("info", f"Aesthetic scores and labels for {len(todo)} image(s) ({len(items) - len(todo)} done before or not photos); "
            "well under a second each, cancel and run again to carry on.")
    done = 0
    for at in range(0, len(todo), FACTS_BATCH):
        if not quiet:
            progress(at / max(len(todo), 1))
        batch, pics = [], []
        for it, path in todo[at:at + FACTS_BATCH]:
            try:
                data, ctype = stash.fetch(it["paths"]["thumbnail"])
            except Exception as e:
                log("debug", f"image {it['id']}: no picture ({e})")
                continue
            if ctype.startswith("image/"):
                batch.append((it, path))
                pics.append(base64.b64encode(data).decode())
        if not pics:
            continue
        results = None
        for attempt in range(4):  # (the face helper restarting for a moment)
            try:
                req = urllib.request.Request(url + "/apple", data=json.dumps({"images": pics, "facts": True}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=300) as resp:
                    results = json.loads(resp.read())["results"]
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException) as e:
                if attempt == 3:
                    raise RuntimeError(f"the face helper stopped answering ({getattr(e, 'reason', e)})")
                time.sleep(3 + attempt * 3)
        for (it, path), r in zip(batch, results):
            if not r:
                continue
            a = None if r.get("score") is None else max(0, min(100, round((r["score"] + 1) * 50)))
            labels = [l.replace("_", " ") for l, _ in r.get("labels") or []]
            if r.get("utility"):
                labels = ["screenshot"] + labels
            custom = {"seen": ", ".join(labels)}
            if a is not None:
                custom["aesthetic"] = a
            with contextlib.suppress(Exception):
                stash.call("mutation($i: ImageUpdateInput!) { imageUpdate(input: $i) { id } }",
                           {"i": {"id": it["id"], "custom_fields": {"partial": custom}}})
                cache[it["id"]] = {"s": path, "a": a, "u": bool(r.get("utility")), "l": labels}
                done += 1
        if (at // FACTS_BATCH) % 20 == 19:
            _pose_save_named(plugin_dir, "picture_facts.json", cache)
    _pose_save_named(plugin_dir, "picture_facts.json", cache)
    if not quiet:
        progress(1)
        log("info", f"Done: {done} image(s) got an aesthetic score and labels (custom fields \"aesthetic\" and \"seen\": "
            "Images → Filter → Custom fields).")


def _pose_save_named(plugin_dir, name, data):
    path = os.path.join(plugin_dir, name)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    _replace(tmp, path)


# --- "Describe it" search: find pictures by words ----------------------------
# The words are turned into a CLIP fingerprint (the face helper's text model)
# and compared with the picture fingerprints the look-alike scan made for
# every photo and scene (and the AI markers' frames). The face helper keeps
# the fingerprints in memory, sent again only when they change.
SEARCH_INDEX = "looks"


SEARCH_PERSON = re.compile(r"(?i)\b(man|men|guy|guys|boy|boys|woman|women|girl|girls|lady|ladies|couple|people|person|"
                           r"she|he|her|him|trans|femboy|someone|they)\b")
SEARCH_WEAK = 0.275  # the best match under this: nothing looks like it ("spreadsheet of tax numbers" 0.268; real searches 0.30+; nonsense can still find look-alikes, so only clear misses are flagged)


def search_wordings(text):
    """CLIP finds more with a few wordings averaged: "a photo of …", and when
    no person is named "a photo of a woman, …" (on 12 test searches, a plain
    "kneeling" found building shots; "a photo of a woman kneeling" found kneeling)."""
    if SEARCH_PERSON.search(text):
        return [f"a photo of {text}", text]
    return [f"a photo of {text}", f"a photo of a woman, {text}"]


def _search_version(plugin_dir):
    parts = ["v2"]
    for name in ("look_items.json", "marker_looks.json"):
        with contextlib.suppress(OSError):
            parts.append(str(int(os.stat(os.path.join(plugin_dir, name)).st_mtime)))
    return "-".join(parts) or "0"


def _search_post(cfg, path, body):
    req = urllib.request.Request(cfg["ai"]["face_url"] + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("the face helper is too old for search: copy face-helper/face_helper.py over the running one and restart it")
        detail = ""
        with contextlib.suppress(Exception):
            detail = json.loads(e.read()).get("error", "")
        raise RuntimeError(f"the face helper couldn't search ({detail or e.code})")
    except urllib.error.URLError as e:
        raise RuntimeError(f"can't reach the face helper at {cfg['ai']['face_url']} ({getattr(e, 'reason', e)})")


def _search_send_index(stash, cfg, plugin_dir, version):
    keys, vectors, groups = [], [], []
    for k, ent in LookCache(plugin_dir).data.items():
        if ent.get("e") and k.split(":")[0] in ("image", "scene"):
            keys.append(k)
            vectors.append(ent["e"])
            groups.append(k)
    # a marker's group is its scene: a scene and its markers show at most twice
    scene_of = {}
    with contextlib.suppress(Exception):
        for m in stash.call("{ findSceneMarkers(filter: {per_page: -1}) { scene_markers { id scene { id } } } }")["findSceneMarkers"]["scene_markers"]:
            scene_of[m["id"]] = m["scene"]["id"]
    with contextlib.suppress(OSError, ValueError):
        with open(os.path.join(plugin_dir, "marker_looks.json")) as f:
            for mid, ent in json.load(f).items():
                if (ent or {}).get("e") and mid in scene_of:  # (deleted markers left out)
                    keys.append(f"marker:{mid}")
                    vectors.append(ent["e"])
                    groups.append(f"scene:{scene_of[mid]}")
    if not keys:
        raise NotReady('nothing to search yet - run "Tags: scan scenes and images for look-alikes (do this first)" first')
    _search_post(cfg, "/index", {"name": SEARCH_INDEX, "version": version, "keys": keys, "vectors": vectors, "groups": groups})


def find_markers_by_ids(stash, ids, fields):
    """findSceneMarkers (ids: ...) that doesn't fail when some were deleted
    since (Stash refuses the whole lookup then): the missing ones are left out."""
    ids = [str(i) for i in ids]
    for _ in range(len(ids) + 1):
        if not ids:
            return []
        try:
            return stash.call(f"query($ids: [ID!]) {{ findSceneMarkers(ids: $ids) {{ scene_markers {{ {fields} }} }} }}",
                              {"ids": ids})["findSceneMarkers"]["scene_markers"]
        except RuntimeError as e:
            gone = set(re.findall(r"marker with id (\d+) not found", str(e)))
            if not gone:
                raise
            ids = [i for i in ids if i not in gone]
    return []


def run_ai_search(stash, plugin_dir, text, kinds=None, limit=60, min_aesthetic=None):
    """Pictures that match a description, best first:
    {"items": [{kind, id, title, picture, link, score, aesthetic}]}."""
    text = (text or "").strip()
    if not text:
        return {"items": []}
    cfg = settings(stash, plugin_dir)
    kinds = set(kinds or ("image", "scene", "marker"))
    version = _search_version(plugin_dir)
    ask = {"name": SEARCH_INDEX, "version": version, "texts": search_wordings(text),
           "k": int(limit) * 2 if not min_aesthetic else max(400, int(limit) * 8), "kinds": sorted(kinds), "per_group": 2}
    res = _search_post(cfg, "/search", ask)
    if res.get("missing"):
        _search_send_index(stash, cfg, plugin_dir, version)
        res = _search_post(cfg, "/search", ask)
    facts = picture_facts(plugin_dir)
    hits = []
    for key, score in res.get("results") or []:
        kind, _, iid = key.partition(":")
        if kind not in kinds:
            continue
        a = (facts.get(iid) or {}).get("a") if kind == "image" else None
        if min_aesthetic and kind == "image" and (a is None or a < int(min_aesthetic)):
            continue
        hits.append((kind, iid, score, a))
        if len(hits) >= int(limit) * 2:  # (some may have been deleted since the scan)
            break
    by_kind = defaultdict(list)
    for kind, iid, _, _ in hits:
        by_kind[kind].append(iid)
    info = {}
    if by_kind.get("image"):
        for x in find_by_ids(stash, "findImages", "images", by_kind["image"], "id title paths { thumbnail } visual_files { ... on BaseFile { basename } }"):
            info[f"image:{x['id']}"] = (x.get("title") or ((x.get("visual_files") or [{}])[0] or {}).get("basename") or "",
                                        x["paths"]["thumbnail"], f"/images/{x['id']}")
    if by_kind.get("scene"):
        for x in find_by_ids(stash, "findScenes", "scenes", by_kind["scene"], "id title paths { screenshot } files { basename }"):
            info[f"scene:{x['id']}"] = (x.get("title") or ((x.get("files") or [{}])[0] or {}).get("basename") or "",
                                        x["paths"]["screenshot"], f"/scenes/{x['id']}")
    if by_kind.get("marker"):
        got = find_markers_by_ids(stash, by_kind["marker"], "id title seconds scene { id title }")
        for m in got:
            sid = m["scene"]["id"]
            info[f"marker:{m['id']}"] = (m.get("title") or f"{m['scene'].get('title') or 'Scene'} at {_marker_clock(m['seconds'])}",
                                         f"/scene/{sid}/scene_marker/{m['id']}/screenshot", f"/scenes/{sid}?t={int(float(m['seconds']))}")
    items = []
    for kind, iid, score, a in hits:
        got = info.get(f"{kind}:{iid}")
        if not got:
            continue  # (deleted since)
        title, picture, link = got
        items.append({"kind": kind, "id": iid, "title": title, "picture": re.sub(r"^https?://[^/]+", "", picture or ""),
                      "link": link, "score": round(score, 3), **({"aesthetic": a} if a is not None else {})})
        if len(items) >= int(limit):
            break
    weak = bool(items) and items[0]["score"] < SEARCH_WEAK
    return {"items": items, "weak": weak}


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
        first = "image:" if photos else "scene:"  # (photos first whenever the tag has any)
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
        log("warning", 'No picture fingerprints yet: run "Tags: scan scenes and images for look-alikes (do this first)" first.')
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
# ("T-shirt"), and the "Naming: special spellings (tags too)" setting forces exact spellings
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


# --- naming conventions (performers, studios, galleries, titles) ------------
# Like the tags' capitalisation, for the rest: a style for each kind, set in
# the settings - "Title Case" (small words lowercase in the middle, and made
# lowercase there in titles: "Relaxing By The Pool" -> "Relaxing by the
# Pool"), "Capitalise Every Word", "Sentence case" (only the first word), or
# off. Spaces are tidied; capitals are never lowered (but a small word in a
# Title Case title); "Naming: special spellings (tags too)" win. Performers' name particles (de,
# van, von...) stay as written; account names (studios under OnlyFans,
# Fansly...; names with _, @ or a dot and no space) are left alone.
NAMING_SETTINGS = {"performer": "namingPerformers", "studio": "namingStudios", "gallery": "namingGalleries",
                   "scene": "namingTitles", "image": "namingTitles"}
NAMING_KINDS = {  # kind: (find query, list field, name field, update mutation, input type, extra fields)
    "performer": ("findPerformers", "performers", "name", "performerUpdate", "PerformerUpdateInput", ""),
    "studio": ("findStudios", "studios", "name", "studioUpdate", "StudioUpdateInput", "parent_studio { name }"),
    "gallery": ("findGalleries", "galleries", "title", "galleryUpdate", "GalleryUpdateInput", ""),
    "scene": ("findScenes", "scenes", "title", "sceneUpdate", "SceneUpdateInput", ""),
    "image": ("findImages", "images", "title", "imageUpdate", "ImageUpdateInput", ""),
}
NAME_PARTICLES = {"de", "da", "di", "del", "della", "der", "den", "van", "von", "du", "dos", "das", "la", "le",
                  "bin", "al", "y", "ten", "ter", "zu"}
NAMING_BREAKS = {"-", "–", "—", "|", "/", "&"}
# small words in romanised Japanese titles (kept lowercase when written so)
ROMAJI_SMALL = {"wa", "ga", "no", "ni", "na", "o", "wo", "de", "to", "mo", "e", "ka", "yo", "ne", "kara", "made"}
# spellings the naming styles know (yours in "Naming: special spellings (tags too)" win)
NAMING_SPELLINGS = {w.lower(): w for w in (
    "POV", "JOI", "CEI", "ASMR", "WIP", "BF", "GF", "VR", "HD", "UHD", "4K", "BBC", "MILF", "JAV", "DIY",
    "OnlyFans", "ManyVids", "TikTok", "YouTube", "FansDB", "StashDB", "ThePornDB", "Pornhub", "xHamster", "iPhone")}
MOJIBAKE = re.compile(r"[ÃÂâ][\x80-\xbf\u2018-\u201e\u2020-\u2122€™œ]")


def naming_style(value):
    """A setting's style: "title", "words", "sentence" or "" (off)."""
    v = re.sub(r"[^a-z]", "", str(value or "").lower())
    if v.startswith("title"):
        return "title"
    if v.startswith(("words", "every", "each", "capitali", "upper")):
        return "words"
    if v.startswith("sentence"):
        return "sentence"
    return ""


def looks_like_handle(name):
    """An account name or web address, not words: kept as it is."""
    return " " not in name.strip() and bool(re.search(r"[_@.]", name))


def style_name(name, style, spellings, kind="scene"):
    """`name` in a naming style (see above); "" style: as it is."""
    if not style or not (name or "").strip():
        return name
    text = " ".join(name.split())
    spellings = {**NAMING_SPELLINGS, **spellings}
    whole = spellings.get(text.lower())
    if whole:
        return whole
    words = text.split(" ")
    out = []
    for i, word in enumerate(words):
        low = word.lower()
        prev = words[i - 1] if i else ""
        # the start of the name, or of a part after ":" or " - "
        starts = i == 0 or prev.endswith(":") or prev in NAMING_BREAKS
        last = i == len(words) - 1
        if MOJIBAKE.search(word):
            out.append(word)  # (text garbled before it got here: left as it is)
        elif re.sub(r"^\W+|\W+$", "", low) in spellings:  # (also "(onlyfans)" -> "(OnlyFans)")
            m = re.match(r"^(\W*)(.*?)(\W*)$", word)
            out.append(m.group(1) + spellings[m.group(2).lower()] + m.group(3))
        elif style == "sentence" and not starts:
            out.append(word)
        elif kind == "performer" and low in NAME_PARTICLES and not starts and not last and word == low:
            out.append(word)  # "Lena de Vries"
        elif style == "title" and word in ROMAJI_SMALL and not starts and not last:
            out.append(word)
        elif style == "title" and low == "w/" and not starts:
            out.append(low)  # ("with")
        elif style == "title" and low in MINOR_WORDS and not starts and not last:
            # lowercase in the middle - lowered only from "The"/"the" forms, never "OR" or "iOS"
            out.append(low if word[:1].isupper() and word[1:] == word[1:].lower() or word == low else word)
        elif kind == "performer":
            # names: each part of Mary-Jane, and O'Neil / D'Angelo
            parts = [capitalise_word(p, spellings) for p in word.split("-")]
            out.append(re.sub(r"^([OoDd]')([a-z])", lambda m: m.group(1).upper() + m.group(2).upper(), "-".join(parts)))
        else:
            out.append(capitalise_word(word, spellings))
    return " ".join(out)


_naming_cache = []


def naming_config(stash):
    """(styles by kind, spellings), read once per run."""
    if not _naming_cache:
        cfg = settings(stash)
        _naming_cache.append((cfg["naming"], cfg["spellings"]))
    return _naming_cache[0]


def named(stash, kind, text):
    """`text` (a title or name) in its kind's naming style."""
    with contextlib.suppress(Exception):
        styles, spellings = naming_config(stash)
        return style_name(text, styles.get(kind, ""), spellings, kind)
    return text


def _naming_new_name(kind, item, style, spellings):
    q, f, field, mut, typ, extra = NAMING_KINDS[kind]
    old = item.get(field) or ""
    if not old.strip():
        return None
    if kind in ("performer", "studio") and looks_like_handle(old):
        return None
    if kind == "studio" and ((item.get("parent_studio") or {}).get("name") or "").lower() in {v[0].lower() for v in PLATFORMS.values()}:
        return None  # (an account: its name is the handle)
    new = style_name(old, style, spellings, kind)
    return new if new != old else None


def run_naming(stash, plugin_dir, preview=False, kinds=None, ids=None):
    """Task: performers, studios, galleries and titles in their naming
    styles (kinds whose style is off are skipped). ids: only these (one
    kind). Returns {kind: changed}."""
    cfg = settings(stash, plugin_dir)
    styles, spellings = cfg["naming"], cfg["spellings"]
    todo = [k for k in (kinds or NAMING_KINDS) if styles.get(k)]
    if not todo and not ids:
        log("info", "Naming: every style is off - choose them in the settings (Naming: performers, studios, galleries, titles).")
        return {}
    out = {}
    for n, kind in enumerate(todo):
        q, f, field, mut, typ, extra = NAMING_KINDS[kind]
        fields = f"id {field} {extra}"
        if ids:
            items = stash.call(f"query($ids: [ID!]) {{ {q}(ids: $ids, filter: {{per_page: -1}}) {{ {f} {{ {fields} }} }} }}",
                               {"ids": [str(i) for i in ids]})[q][f]
        else:
            items, page = [], 1
            while True:
                got = stash.call(f"query($p: FindFilterType) {{ {q}(filter: $p) {{ {f} {{ {fields} }} }} }}",
                                 {"p": {"page": page, "per_page": 1000, "sort": "id"}})[q][f]
                items += got
                if len(got) < 1000:
                    break
                page += 1
        changed = failed = 0
        for item in items:
            new = _naming_new_name(kind, item, styles[kind], spellings)
            if not new:
                continue
            log("info", f'{kind} {item["id"]}: "{item[field]}" -> "{new}"')
            if not preview:
                try:
                    stash.call(f"mutation($i: {typ}!) {{ {mut}(input: $i) {{ id }} }}", {"i": {"id": item["id"], field: new}})
                except Exception as e:
                    failed += 1
                    log("warning", f'{kind} {item["id"]}: couldn\'t rename "{item[field]}" ({e})')
                    continue
            changed += 1
        out[kind] = changed
        if not ids:
            log("info", f"Naming: {changed} of {len(items)} {f} {'would be' if preview else ''} renamed".replace("  ", " ")
                + (f", {failed} failed (another with that name already exists?)" if failed else "")
                + f" ({dict(title='Title Case', words='Capitalise Every Word', sentence='Sentence case')[styles[kind]]})")
        progress((n + 1) / len(todo))
    if preview and not ids:
        log("info", "Preview: nothing was saved.")
    return out


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
        if re.fullmatch(r"#[^\W\d_]\w*", tok):  # hashtags (X / TikTok downloads): "#amateur" - not "#12"
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
    title = named(stash, kind, title)  # (the naming convention for titles / galleries)
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
    after turning "Titles: keep performer and studio names" on or off.
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
    tags -= removed_tags(kind, item["id"], [t["id"] for t in item.get("tags") or []])
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
        "url": (s.get("aiOllamaUrl") or f"http://{_default_host()}:11434").rstrip("/"),
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
        "face_url": (s.get("faceHelperUrl") or f"http://{_default_host()}:11535").rstrip("/"),
        "face_auto": bool(s.get("faceAutoAssign")),
        # cosine likeness of two face fingerprints (SFace): about 0.36 and up
        # is usually the same person
        "face_min": num_pct(s.get("faceSuggestMin"), 36),
        "face_auto_min": num_pct(s.get("faceAutoMin"), 50),
        # which performers faces are suggested / added for (faces are still
        # compared with everyone's): favourites only, and / or performers in
        # at least this many scenes and images
        "face_only_favs": bool(s.get("faceOnlyFavourites")),
        "face_min_items": max(0, int(float(s.get("faceMinItems") or 0))),
        # new items: quick steps right away, AI model steps once at night
        "nightly": bool(s.get("aiNightly")),
        # the body tags task (items not checked yet) as part of the night run
        "night_body": bool(s.get("aiNightBody")),
        "night_time": _clock(s.get("aiNightTime"), "03:00"),
        "time_zone": (s.get("aiTimeZone") or os.environ.get("TZ") or "UTC").strip(),
        # scene covers the covers task may change besides Stash's own generated ones
        "cover_scraped": bool(s.get("coverReplaceScraped")),
        "cover_picked": bool(s.get("coverReplacePicked")),
        "cover_organized": bool(s.get("coverReplaceOrganized")),
        # the Nightly jobs page: Stash's tasks and plugin tasks to run each night
        "night_jobs": _kept_job_list("night") or _night_jobs_setting(s),
        # ...and what runs after a scan you started
        "scan_jobs": _kept_job_list("scan") or _job_list_setting(s.get("afterScanJobs")),
    }


_IN_DOCKER = None


def _default_host():
    """Where Ollama and the face helper are when their address isn't set:
    the computer running Docker from inside a container, else this computer
    (Stash running natively - e.g. on Windows or a Mac)."""
    global _IN_DOCKER
    if _IN_DOCKER is None:
        _IN_DOCKER = os.path.exists("/.dockerenv")
        if not _IN_DOCKER:
            with contextlib.suppress(OSError):
                with open("/proc/1/cgroup") as f:
                    _IN_DOCKER = any(x in f.read() for x in ("docker", "containerd", "kubepods"))
    return "host.docker.internal" if _IN_DOCKER else "127.0.0.1"


def _clean_order(items):
    """A job list in its run order: [{"stash": id} or {"plugin", "task"}],
    unknown and repeated entries dropped."""
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("stash") in STASH_NIGHT_JOBS:
            item, key = {"stash": it["stash"]}, ("s", it["stash"])
        elif it.get("plugin") and it.get("task"):
            task = str(it["task"])
            if str(it["plugin"]) == AI_ID:
                task = TASK_RENAMES.get(task, task)  # (saved before the tasks were renamed)
            elif str(it["plugin"]) == BASE_ID:
                task = TOOLS_TASK_RENAMES.get(task, task)
            item, key = {"plugin": str(it["plugin"]), "task": task}, ("t", str(it["plugin"]), task)
        else:
            continue
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _job_list(order, saved):
    return {"order": order, "stash": [i["stash"] for i in order if "stash" in i],
            "tasks": [i for i in order if "task" in i], "saved": saved}


def _old_order(stash_jobs, tasks):
    """Lists saved before 1.8 (Stash's tasks in a fixed place around the
    plugin tasks), as one list in that same order."""
    chosen = set(stash_jobs)
    return _clean_order([{"stash": j} for j in STASH_NIGHT_ORDER if j in chosen and STASH_NIGHT_JOBS[j][2] == "before"]
                        + list(tasks)
                        + [{"stash": j} for j in STASH_NIGHT_ORDER if j in chosen and STASH_NIGHT_JOBS[j][2] == "after"])


def _job_list_setting(raw):
    """A saved job list: {"order": [...]} (or, saved before 1.8, {"stash",
    "tasks"})."""
    if raw:
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            d = json.loads(raw) if isinstance(raw, str) else raw
            if "order" in d:
                return _job_list(_clean_order(d["order"]), True)
            return _job_list(_old_order(d.get("stash") or [], d.get("tasks") or []), True)
    return _job_list([], False)


# The job lists are also kept in the add-on's own file (job_lists.json), which
# wins over the settings: Settings → Plugins saves all of a plugin's settings
# as that page loaded them, so a page left open from before a list was changed
# would put the old list back when any other setting is changed there.
# The add-on's tasks were renamed (2026-11-24, 1.8): job lists saved with the
# old names still find them.
TASK_RENAMES = {
    "Setup check: write a report to the log": "Setup: check everything and write a report",
    "Nightly jobs: run now": "Schedules: run the nightly jobs now",
    "After-scan jobs: run now": "Schedules: run the after-scan jobs now",
    "AI: run tonight's new items now": "Schedules: run tonight's waiting steps for new items now",
    "Look-alike tags: scan scenes and images": "Tags: scan scenes and images for look-alikes (do this first)",
    "AI tagging: items with no tags": "Tags: tag scenes and images with no tags (AI model)",
    "AI tagging: everything not done yet": "Tags: tag all scenes and images not done yet (AI model)",
    "Look-alike tags: items with no tags": "Tags: tag scenes and images with no tags (look-alikes)",
    "Body tags: scenes and images": "Tags: add body tags to scenes and images not done yet",
    "Body tags: clothing from the look (quick)": "Tags: add clothing tags from the look (quick)",
    "Body tags: check everything again": "Tags: redo body tags on all scenes and images",
    "Poses: read body poses": "Tags: scan images' body poses for pose suggestions (Mac)",
    "AI tagging: preview on 10 items (no changes)": "Tags: preview on 10 (AI model, no changes)",
    "Look-alike tags: preview on 10 items (no changes)": "Tags: preview on 10 (look-alikes, no changes)",
    "Body tags: preview on 10 items (no changes)": "Tags: preview body tags on 10 (no changes)",
    "Body tags: preview clothing from the look (no changes)": "Tags: preview clothing tags from the look (no changes)",
    "Faces: learn from what you've tagged": "Faces: learn more from what you've tagged",
    "Faces: scenes without performers": "Faces: find performers in scenes without any",
    "Faces: images without performers": "Faces: find performers in images without any",
    "Faces: preview on 10 items (no changes)": "Faces: preview on 10 (no changes)",
    "Video model: download it (for AI markers and positions)": "Markers: download the video model (needed first)",
    "AI markers: scenes without markers": "Markers: add AI markers to scenes without markers",
    "AI markers: redo with the newest method": "Markers: redo AI markers with the newest method",
    "Positions in videos: look at scenes": "Markers: scan scenes for positions (for Review)",
    "AI markers: preview on 5 scenes (no changes)": "Markers: preview AI markers on 5 scenes (no changes)",
    "Markers: preview adding their tags to their scenes (no changes)": "Markers: preview adding their tags to scenes (no changes)",
    "AI titles: scenes without a title": "Titles: title scenes without one (AI model)",
    "AI titles: images without a title": "Titles: title images without one (AI model)",
    "AI titles: preview on 10 items (no changes)": "Titles: preview on 10 (AI model, no changes)",
    "Studios from accounts: also read watermarks with the AI": "Studios: find scenes' studios from accounts and watermarks",
    "Studios from watermarks: photos without a studio, not in a gallery": "Studios: find images' studios from watermarks (not in galleries)",
    "Studios from watermarks: photos without a studio, galleries too": "Studios: find images' studios from watermarks (galleries too)",
    "Links: add account and written links to scenes and photos": "Links: add account and written links to scenes and images",
    "Studios from accounts: preview on 10 scenes (no changes)": "Studios: preview on 10 scenes (no changes)",
    "Studios from watermarks: preview on 10 photos without a studio (no changes)": "Studios: preview on 10 images (no changes)",
    "Tag pictures: most typical one for tags without a picture": "Tag pictures: pick the most typical for tags without one",
    "Tag pictures: most typical one for every tag (replaces existing ones)": "Tag pictures: pick the most typical for every tag (replaces yours)",
    "Pictures: replace blurry or dark tag pictures": "Tag pictures: replace blurry or dark ones (Mac)",
    "Pictures: replace blurry, dark or empty marker pictures": "Marker pictures: replace blurry, dark or empty ones (Mac)",
    "Pictures: aesthetic score and labels": "Image scores: add aesthetic scores and labels (Mac)",
    "Tag pictures: preview the most typical ones (no changes)": "Tag pictures: preview the most typical (no changes)",
    "Pictures: preview better tag and marker pictures (no changes)": "Tag & marker pictures: preview replacing blurry ones (Mac, no changes)",
    "Scene covers: replace low-scoring ones": "Scene covers: replace low-scoring ones (Mac)",
    "Scene covers: preview on 10 scenes (no changes)": "Scene covers: preview on 10 (Mac, no changes)",
    "Screenshots: preview cropping to the photo (no changes)": "Screenshots: preview cropping (no changes)",
    "Performer details: preview on 10 performers (no changes)": "Performer details: preview on 10 (no changes)",
}


# ... and Better Organizing Tools' (2.4)
TOOLS_TASK_RENAMES = {
    "Tag performers with their top tags": "Top tags: tag performers with their top tags",
    "Tag studios with their top tags": "Top tags: tag studios with their top tags",
    "Tag galleries with their top tags": "Top tags: tag galleries with their top tags",
    "Tag studios' items with the studio's tags": "Top tags: tag studios' items with the studio's tags",
    "Preview top tags (no changes)": "Top tags: preview (no changes)",
    "Preview studio tags on their items (no changes)": "Top tags: preview studio tags on their items (no changes)",
    "Fill in titles from file names": "Titles: fill in titles from file names",
    "Redo titles made from file names": "Titles: redo titles made from file names",
    "Fill in dates from file names": "Dates: fill in dates from file names",
    "Preview titles from file names (no changes)": "Titles: preview from file names (no changes)",
    "Preview dates from file names (no changes)": "Dates: preview from file names (no changes)",
    "Studios from accounts: from file names and links only": "Studios: find scenes' studios from file names and links",
    "Give tags without a picture one from your library": "Tag pictures: give tags without one a picture from your library",
    "Give tags that share a picture their own": "Tag pictures: give tags that share a picture their own",
    "Give every tag a new picture (replaces existing ones)": "Tag pictures: give every tag a new one (replaces yours)",
    "Capitalise tag names": "Naming: capitalise tag names",
    "Preview naming conventions (no changes)": "Naming: preview (no changes)",
    "Fill in empty tag descriptions": "Tag descriptions: fill in empty ones",
    "Preview tag name capitalisation (no changes)": "Naming: preview tag capitalisation (no changes)",
    "Preview tag descriptions on 30 tags (no changes)": "Tag descriptions: preview on 30 tags (no changes)",
    "Tag gallery images with the gallery's name": "Gallery images: tag them with the gallery's name",
    "Tag gallery images with the gallery's tags, performers and studio": "Gallery images: add the gallery's tags, performers and studio",
    "Preview gallery tags, performers and studio on images (no changes)": "Gallery images: preview the gallery's tags, performers and studio (no changes)",
}


def _job_lists_path():
    return os.path.join(_ai_data_dir(), "job_lists.json")


def _job_lists_saved_at():
    """When each list was last saved: {"night": "...", "scan": "..."}."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        with open(_job_lists_path()) as fh:
            return {w: v.get("at", "") for w, v in json.load(fh).items()}
    return {}


def _kept_job_list(which):
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        with open(_job_lists_path()) as fh:
            order = json.load(fh)[which]["order"]
        return _job_list(_clean_order(order), True)
    return None


def _night_jobs_setting(s):
    """The jobs chosen on the Nightly jobs page ({"stash": [...], "tasks":
    [{"plugin", "task"}]}). Before the page was first saved: what the older
    settings asked for (new items' AI steps, body tags)."""
    got = _job_list_setting(s.get("nightJobs"))
    if got["saved"]:
        return got
    tasks = []
    if s.get("aiNightly"):
        tasks.append({"plugin": AI_ID, "task": NIGHT_TASK})
    if s.get("aiNightBody"):
        tasks.append({"plugin": AI_ID, "task": NIGHT_BODY_TASK})
    return _job_list(_clean_order(tasks), False)


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
    # about twice as fast (on the user's library: 11.3s -> 5.7s a scene).
    # With 4+ markers, the 4 most different markers' pictures: a frame from
    # each part of the scene (markers sit where it shifts)
    if duration > 4:
        with contextlib.suppress(Exception):
            grid = marker_grid(stash, item["id"])
            if grid:
                return [grid]
        with contextlib.suppress(Exception):
            return [grab_grid(stash, f["path"], content_end(stash, f))]
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


_MARKER_LOOKS_READ = {}  # (marker_looks.json read once per change)


def _marker_fingerprints():
    path = os.path.join(_ai_data_dir(), "marker_looks.json")
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    if _MARKER_LOOKS_READ.get("at") != mtime:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        _MARKER_LOOKS_READ.update(at=mtime, data={k: _ints(v["e"]) for k, v in data.items() if (v or {}).get("e")})
    return _MARKER_LOOKS_READ["data"]


def marker_grid(stash, scene_id, tile=GRID_TILE):
    """The AI's picture of a scene from its markers: the 4 most different
    markers' pictures in a 2x2 grid (most different by their picture
    fingerprints, else spread through the scene). None with fewer than 4."""
    import tempfile  # noqa: PLC0415
    sc = stash.call("query($id: ID!) { findScene(id: $id) { files { path duration } scene_markers { id seconds end_seconds screenshot } } }",
                    {"id": str(scene_id)})["findScene"]
    ms = sorted((m for m in sc["scene_markers"] if m.get("screenshot")), key=lambda m: float(m["seconds"]))
    f = (sc.get("files") or [None])[0]
    if f and f.get("duration") and len(ms) >= 4:
        upto = content_end(stash, f)  # (not a marker on an end card)
        ms = [m for m in ms if float(m["seconds"]) < upto - 1]
    if len(ms) >= 5 and float(ms[0]["seconds"]) < 5:
        ms = ms[1:]  # (the intro: often an empty room or a title)
    if len(ms) < 4:
        return None
    fp = _marker_fingerprints()
    have = [m for m in ms if m["id"] in fp]
    if len(have) >= 4:
        # farthest first: start at the longest marker (the main part of the
        # scene), then each time the one least like any picked so far
        def dot(a, b):
            return sum(x * y for x, y in zip(a, b))

        def length(m):
            return float(m.get("end_seconds") or m["seconds"]) - float(m["seconds"])
        picked = [max(have, key=length)]
        while len(picked) < 4:
            picked.append(max((m for m in have if m not in picked),
                              key=lambda m: -max(dot(fp[m["id"]], fp[p["id"]]) for p in picked)))
        picked.sort(key=lambda m: float(m["seconds"]))
    else:
        picked = [ms[round(i * (len(ms) - 1) / 3)] for i in range(4)]
    with tempfile.TemporaryDirectory(prefix="bta-grid-") as folder:
        files = []
        for i, m in enumerate(picked):
            data, ctype = stash.fetch(m["screenshot"])
            if not ctype.startswith("image/"):
                return None
            files.append(os.path.join(folder, f"{i}.jpg"))
            with open(files[-1], "wb") as fh:
                fh.write(data)
        cmd = [ffmpeg_path(stash), "-loglevel", "error"]
        for x in files:
            cmd += ["-i", x]
        fit = (f"scale={tile}:{tile}:force_original_aspect_ratio=decrease,"
               f"pad={tile}:{tile}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuvj420p")
        graph = "".join(f"[{i}:v]{fit}[t{i}];" for i in range(4))
        graph += "".join(f"[t{i}]" for i in range(4)) + "xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[out]"
        cmd += ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", "-q:v", "3",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
        if proc.returncode != 0 or not proc.stdout:
            return None
    return GridPicture(proc.stdout)


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
    gone = removed_tags(kind, item["id"], [t["id"] for t in item.get("tags") or []])
    full = [x for x in full if str(x[0]) not in gone]
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
    log("info", f"{what}: {len(targets)} item(s){kinds_note(cfg['ai']['tag_kinds'], 'Tags: scenes, images or both (AI model)')}, "
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
        log("info", f"Skipping the AI model for items with more than {limit} tags already (setting \"Tags: skip items with more tags than (AI model)\")")
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
    "Tags: scenes, images or both (AI model)" says)."""
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
                    f0 = (item.get("files") or [None])[0] or {}
                    upto = content_end(stash, f0) / float(f0["duration"]) if f0.get("duration") and f0.get("path") else 1.0
                    tiles = [spots[min(int(len(spots) * p * upto), len(spots) - 1)] for p in LOOK_TILE_POINTS]
                    pictures.append(sprite)
                    boxes.append(tiles)
        if not tiles:  # no sprite generated: take frames from the video
            f = (item.get("files") or [None])[0]
            if f and (f.get("duration") or 0) > 1:
                for p in LOOK_TILE_POINTS:
                    with contextlib.suppress(Exception):
                        pictures.append(grab_frame(stash, f["path"], content_end(stash, f) * p, width=448)[0])
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


# --- tags you took off an item stay off -----------------------------------
# A tag you remove from an item (in Review's Check, or anywhere in Stash when
# the add-on had added it), or say No to in Review's suggestions, is never
# put back on that item by any task: AI tags, look-alikes, body tags,
# gallery / studio tags, file names. (Before, only tags you'd removed almost
# everywhere were left out, so a task could add one back to the same item.)
_removed_cache = {}


def _ai_data_dir():
    """Where the AI add-on keeps its records (also when Better Organizing
    Tools runs this): its plugin folder next to this one."""
    here = os.path.dirname(os.path.abspath(__file__))
    ai = os.path.join(os.path.dirname(here), AI_ID)
    return ai if os.path.isdir(ai) else here


def _removed_file(name):
    path = os.path.join(_ai_data_dir(), name)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {}
    hit = _removed_cache.get(name)
    if not hit or hit[0] != mtime:
        try:
            with open(path) as f:
                hit = (mtime, json.load(f))
        except (OSError, ValueError):
            hit = (mtime, {})
        _removed_cache[name] = hit
    return hit[1]


def removed_tags(kind, item_id, have=()):
    """Tag ids not to add to this item again: the ones you said No to in
    Review, and the ones the add-on added that it no longer has (`have`: its
    tags now; for an item known not to have a tag, leave it empty)."""
    key = f"{kind}:{item_id}"
    out = {str(t) for t in _removed_file("tag_review_not.json").get(key, [])}
    have = {str(t) for t in have}
    out |= {str(t) for t in _removed_file("machine_tags.json").get(key, [])} - have
    return out


def keep_allowed(kind, ids, tag_id):
    """Of these items (none has the tag yet), the ones it may be added to."""
    return [i for i in ids if str(tag_id) not in removed_tags(kind, i)]


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
        self.stash, self.plugin_dir, self.excluded = stash, plugin_dir, frozenset(excluded)
        self._markers = None  # scene id -> tags its markers point to (marker_scene_tags)
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
        out = self.scores(vec, skip=f"{kind}:{item['id']}") if vec else None
        if kind == "scene":
            # what its markers look like (each marker: a part of the scene):
            # a tag they're sure of in 2+ markers counts in full; in only one,
            # just under "sure" - a suggestion, never added by the tasks
            if self._markers is None:
                self._markers = marker_scene_tags(stash, self.cfg, self.plugin_dir, self.excluded)
            for t, m in (self._markers.get(str(item["id"])) or {}).items():
                sc = m["score"] if m["count"] >= 2 else min(m["score"], LOOK_SURE - 0.01)
                out = out if out is not None else {}
                out[t] = max(out.get(t, 0.0), sc)
        return out


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
            log("info", "Look-alike tags not used yet: run \"Tags: scan scenes and images for look-alikes (do this first)\" first.")
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
        gone = removed_tags(kind, item["id"], [t["id"] for t in item.get("tags") or []] if "tags" in item else picks)
        picks = [t for t in picks if str(t) not in gone]
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
        raise NotReady("run the task \"Tags: scan scenes and images for look-alikes (do this first)\" first (it needs the face helper)")
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



def run_look_suggest_many(stash, plugin_dir, kind, ids):
    """Asked by the bulk edit window: look-alike tags for several selected
    scenes / images. [{id, name, count, of, score}]: how many of them it
    suits (scored like the edit page), most first. Nothing saved."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    looks = load_look_index(stash, cfg, plugin_dir, excluded, quiet=True)
    if not looks:
        raise NotReady("run the task \"Tags: scan scenes and images for look-alikes (do this first)\" first (it needs the face helper)")
    q, f = ("findScenes", "scenes") if kind == "scene" else ("findImages", "images")
    ids = [str(i) for i in (ids or [])][:200]
    items = find_by_ids(stash, q, f, ids, LOOK_ITEM_FIELDS[kind])
    cut = cfg["ai"].get("look_suggest", LOOK_SUGGEST)
    names = looks.names
    hits, total = defaultdict(list), 0
    for item in items:
        scores = looks.for_item(stash, kind, item, compute=len(items) <= 30) or {}
        if not scores:
            continue
        total += 1
        have = {t["id"] for t in item.get("tags") or []}
        for t, sc in scores.items():
            if sc >= cut and t in names and t not in have and names[t].strip().lower() not in BODY_TAG_NAMES:
                hits[t].append(sc)
    looks.cache.save()
    out = [{"id": t, "name": names[t], "count": len(v), "of": total, "score": round(sum(v) / len(v), 2)}
           for t, v in hits.items() if len(v) >= max(1, total // 4)]
    out.sort(key=lambda x: (-x["count"], -x["score"]))
    return {"tags": out[:16], "looked": total, "of": len(items)}

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
# what's worn, and bare feet: tag names (the first you have is added) -> the
# detector part and how sure; a bra or panties also need a bare belly (so a
# shirt or jeans don't count) and nothing bare in the same place
BODY_WORN = {
    "Feet": (("Feet", "Barefoot"), "FEET_EXPOSED", 0.55, None),
    "Bra": (("Bra", "Bras"), "FEMALE_BREAST_COVERED", 0.6, "FEMALE_BREAST_EXPOSED"),
    "Panties": (("Panties", "Underwear"), "FEMALE_GENITALIA_COVERED", 0.55, "FEMALE_GENITALIA_EXPOSED"),
    "Shoes": (("Shoes",), "FEET_COVERED", 0.7, None),
}


# Clothing in photos, from their CLIP picture fingerprints (clothing.json:
# each item's descriptions, worked out once with CLIP's text model, scored
# against plain ones). Only items that held up when checked by eye, only on
# photos (drawings and comics fool it: it reads the word "bra" in a speech
# bubble), and at a high bar.
_clothing = None


_clothing_extra = {}  # first tag name -> {"near": level, "women": True}


def clothing_items():
    global _clothing
    if _clothing is None:
        _clothing = []
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "clothing.json")) as f:
                data = json.load(f)
            unpack = lambda b: [x / 127 for x in _ints(b)]  # noqa: E731
            neutral = [unpack(v) for v in data["neutral"]]
            for it in data["items"]:
                # (an item can bring its own neutral descriptions, a near-miss
                # level for Review, and "women": only with women or nobody named)
                _clothing.append((it["tags"], it["bar"], [unpack(v) for v in it["vectors"]],
                                  [unpack(v) for v in it["neutral"]] if it.get("neutral") else neutral))
                _clothing_extra[it["tags"][0]] = {k: it[k] for k in ("near", "women") if k in it}
    return _clothing


def clothing_scores(vec):
    """{first tag name: (tag names, likeness 0-1, bar)} for every clothing item."""
    if not vec:
        return {}
    n = math.sqrt(sum(x * x for x in vec)) or 1
    v = [x / n for x in vec]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    out = {}
    for names, bar, vecs, neutral in clothing_items():
        d = max(dot(v, t) for t in vecs) - max(dot(v, t) for t in neutral)
        out[names[0]] = (names, 1 / (1 + math.exp(-100 * d)), bar)
    return out


BIKINI_OVER_UNDERWEAR = 0.9  # covered + bare belly that looks this much like a bikini (more than lingerie): Bikini, not Bra / Panties


def is_drawn(item, tag_name=None):
    """A page of a comic (from a zip / cbz) or an item tagged as drawn."""
    path = ((item.get("visual_files") or item.get("files") or [{}])[0] or {}).get("path") or ""
    if re.search(r"(?i)\.(zip|cbz)/", path):
        return True
    return any((t.get("name") or (tag_name or {}).get(t["id"], "")).strip().lower() in NEW_PERF_DRAWN_TAGS for t in item.get("tags") or [])
# nothing on: bare breasts and bare genitals or bottom, and nothing seen covered
BODY_NUDE = (("Nude", "Naked", "Fully Nude"), "FEMALE_BREAST_EXPOSED",
             ("FEMALE_GENITALIA_EXPOSED", "BUTTOCKS_EXPOSED"),
             ("FEMALE_BREAST_COVERED", "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED"))
# topless: bare breasts, and covered below (panties, bikini bottoms)
BODY_TOPLESS = (("Topless",), "FEMALE_BREAST_EXPOSED", ("FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED"))
BODY_COVERED_SEEN = 0.5  # how sure the detector is of a covered part before it rules out "nude"
BODY_ONE_PICTURE = 0.05  # one picture (a photo): this much over the bar, as there's no second to agree
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


# Apple's Vision (the face helper on a Mac): its picture classifier's labels
# for clothing it knows well (better than CLIP for shoes and jeans), and
# hands - fingers over a breast the body-part detector found, in a picture of
# one person (from its body pose), is a hand bra. Without a Mac, these are left out.
APPLE_LABELS = {  # Apple's label -> (tag names, the first you have is added; likeness needed)
    "high_heel": (("Woman's Heels", "Heels", "High Heels"), 0.6),
    "boot": (("Boots",), 0.6),
    "sneaker": (("Sneakers",), 0.6),
    "sock": (("Socks",), 0.6),
    "shoes": (("Shoes",), 0.6),
    "jeans": (("Jeans",), 0.7),
    "eyeglasses": (("Glasses",), 0.6),
    "mask": (("Mask",), 0.7),
    "glove": (("Gloves",), 0.7),
    "costume": (("Costume",), 0.7),
    "hat": (("Hat", "Hats"), 0.6),
    "swimsuit": (("Swimsuit", "Bikini"), 0.6),  # (a bikini when the picture looks like one)
}
HAND_BRA = (("Hand Bra", "Handbra"), 8)  # tag names; finger points on a breast the body-part detector found


def apple_helper(cfg, pictures):
    """Apple's labels, hands and chest areas for each picture, or None (no Mac)."""
    if not pictures:
        return None
    body = json.dumps({"images": [base64.b64encode(p).decode() for p in pictures]}).encode()
    with contextlib.suppress(Exception):
        req = urllib.request.Request(cfg["ai"]["face_url"] + "/apple", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())["results"]
    return None


def hand_over_breasts(found, apple):
    """In one picture: fingers on a breast (or on the chest), with skin showing."""
    if not apple or not apple.get("hands"):
        return False
    if len(apple.get("chests") or []) > 1:
        return False  # more than one person: their hands could be anyone's
    faces = {d["part"]: d["score"] for d in found or [] if d["part"].startswith("FACE_")}
    if faces.get("FACE_MALE", 0) >= 0.5 and faces.get("FACE_FEMALE", 0) < 0.5:
        return False  # a man's chest
    w, h = apple["size"]
    # bare breasts (or nothing on below): a hand holding a phone in front of a
    # bra doesn't count
    skin = any((d["part"] == "FEMALE_BREAST_EXPOSED" and d["score"] >= 0.3)
               or (d["part"] == "FEMALE_GENITALIA_EXPOSED" and d["score"] >= 0.4) for d in found or [])
    if not skin:
        return False
    boxes = [(d["box"][0] / w, d["box"][1] / h, (d["box"][0] + d["box"][2]) / w, (d["box"][1] + d["box"][3]) / h)
             for d in found or [] if "BREAST" in d["part"] and d["score"] >= 0.25]

    def inside(p, b, pad):
        dx, dy = (b[2] - b[0]) * pad, (b[3] - b[1]) * pad
        return b[0] - dx <= p[0] <= b[2] + dx and b[1] - dy <= p[1] <= b[3] + dy
    for hand in apple["hands"]:
        if any(sum(inside(p, b, 0.15) for p in hand) >= HAND_BRA[1] for b in boxes):
            return True
    return False


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
            out.append(grab_frame(stash, f["path"], content_end(stash, f) * at, width=960)[0])
        except Exception as e:
            why = e
    if not out and why:
        raise ValueError(f"no frames could be taken ({why})")
    return out


BODY_NEAR = 0.08          # this far under what's needed: kept as a near-miss, suggested in Review
BODY_NEAR_MAX_TAGS = 3    # near-misses are suggested for items with this many tags or fewer


def parts_shown(cfg, pictures, found_all=None, slack=0.0):
    """{part: best score} for parts seen: in one picture clearly (a little
    over what's needed), or in two. slack: that much less is needed (for
    near-misses)."""
    seen = defaultdict(list)
    for found in (found_all if found_all is not None else body_helper(cfg, pictures) if pictures else []):
        best = {}
        for d in found:
            best[d["part"]] = max(best.get(d["part"], 0), d["score"])
        for part, sc in best.items():
            seen[part].append(sc)
    need = {p: th for p, th in list(BODY_SHOWN.values()) + [BODY_BELLY]}
    need.update({part: th for _, part, th, _ in BODY_WORN.values()})
    need.update({part: BODY_COVERED_SEEN - BODY_ONE_PICTURE for part in BODY_NUDE[3] if part not in need})
    out = {}
    for part, scores in seen.items():
        th = need.get(part)
        if th is None:
            continue
        th -= slack
        scores.sort(reverse=True)
        if scores[0] >= th + BODY_ONE_PICTURE or (len(scores) >= 2 and scores[1] >= th):
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
        # what's worn (a bra, panties) and bare feet
        for _, (names, part, _, bare) in BODY_WORN.items():
            if part not in shown or (bare and bare in shown):
                continue
            if bare and BODY_BELLY[0] not in shown:
                continue  # covered, but not underwear: a shirt, jeans...
            if part == "FEMALE_BREAST_COVERED" and "FEMALE_GENITALIA_EXPOSED" in shown:
                continue  # bare below: hands over the breasts (a hand bra), not a bra
            if bare and perfs and not women:
                continue
            tid = next((self.tag(n) for n in names if self.tag(n)), None)
            add(tid, f"{part.lower().replace('_', ' ')} {round(shown[part] * 100)}%"
                + (" with a bare belly" if bare else ""))
        # nude: bare breasts and below, and nothing covered
        names, top, below, covered = BODY_NUDE
        if top in shown and any(b in shown for b in below) and not any(c in shown for c in covered) \
                and (not perfs or women):
            add(next((self.tag(n) for n in names if self.tag(n)), None),
                "bare " + " and ".join(p.split("_")[1].lower().replace("genitalia", "genitals").replace("buttocks", "bottom")
                                         for p in [top] + [b for b in below if b in shown]) + ", nothing covered")
        # topless: bare breasts, covered below
        tnames, ttop, tbelow = BODY_TOPLESS
        if ttop in shown and any(b in shown for b in tbelow) and "FEMALE_GENITALIA_EXPOSED" not in shown \
                and (not perfs or women):
            add(next((self.tag(n) for n in tnames if self.tag(n)), None), "bare breasts, covered below")
        if BODY_BELLY[0] in shown and len(perfs) == 1 and perfs[0]["id"] in self.abs:
            add(self.abs[perfs[0]["id"]], f"belly shown; {perfs[0]['name']} is usually tagged so")
        return out


def body_excluded(stash, cfg):
    """Only your "Top tags: tags to ignore" (gallery-name tags like "Tits" are fine
    to add here: they're what's shown)."""
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name } } }")["findTags"]["tags"]
    return {t["id"] for t in tags if t["name"].lower() in cfg["exclude"]}


def body_ready(cfg):
    try:
        with urllib.request.urlopen(cfg["ai"]["face_url"] + "/health", timeout=5) as resp:
            return bool(json.loads(resp.read()).get("body"))
    except Exception:
        return False


def clothing_picks(stash, cfg, body, kind, item, have, rejected, plugin_dir):
    """Clothing (and a butt the picture is about) from the photo's look:
    (tags to add, near-misses for Review, every item's score)."""
    worn, near, looks_like = [], [], {}
    if kind != "image" or not plugin_dir or is_drawn(item, body.tag_name):
        return worn, near, looks_like
    if getattr(body, "looks", None) is None:
        body.looks = LookCache(plugin_dir)
    looks_like = clothing_scores(body.looks.vector(stash, cfg, "image", item, compute=False))
    women = not item.get("performers") or any(
        (body.perfs.get(p["id"]) or {}).get("gender") in FEMALE for p in item.get("performers") or [])
    gone = removed_tags(kind, item["id"], have)
    for names, p, bar in looks_like.values():
        extra = _clothing_extra.get(names[0], {})
        if extra.get("women") and not women:
            continue
        tid = next((body.tag(n) for n in names if body.tag(n)), None)
        if not tid or tid in have or tid in rejected or str(tid) in gone:
            continue
        what = "a butt out" if names[0] == "Ass" else names[0].lower()
        if p >= bar:
            worn.append((tid, f"looks like {what} ({round(p * 100, 1)}%)"))
        elif extra.get("near") and p >= extra["near"]:
            near.append((tid, f"looks like {what} ({round(p * 100, 1)}%)"))
    return worn, near, looks_like


def run_body_looks(stash, plugin_dir, preview=False):
    """Task: clothing tags (and Ass, clothed or not) for every photo from its
    picture fingerprint alone - no detector, so the whole library takes
    minutes. What's nearly there goes to Review as a near-miss."""
    cfg = settings(stash, plugin_dir)
    body = BodyIndex(stash, plugin_dir, body_excluded(stash, cfg))
    try:
        with open(os.path.join(plugin_dir, "machine_tags.json")) as f:
            rejected = rejected_tags(all_item_tags(stash), json.load(f))
    except (OSError, ValueError):
        rejected = {}
    items, page = [], 1
    while True:
        got = stash.call("query($f: FindFilterType) { findImages(filter: $f) { images { " + BODY_ITEM_FIELDS["image"] + " } } }",
                         {"f": {"page": page, "per_page": 1000, "sort": "id"}})["findImages"]["images"]
        items += got
        if len(got) < 1000:
            break
        page += 1
    log("info", f"{'Preview: ' if preview else ''}clothing from the look for {len(items)} image(s)...")
    added, tagged, nears = Counter(), 0, 0
    near_new = {}
    for n, it in enumerate(items):
        if n % 200 == 0:
            progress(n / max(len(items), 1))
        have = {t["id"] for t in it.get("tags") or []}
        worn, near, _ = clothing_picks(stash, cfg, body, "image", it, have, rejected, plugin_dir)
        if near:
            near_new[f"image:{it['id']}"] = near
            nears += 1
        if not worn:
            continue
        tagged += 1
        for t, why in worn:
            added[body.tag_name.get(t, t)] += 1
        if preview:
            if tagged <= 40:
                log("info", f"image {it['id']}: would add " + ", ".join(f"{body.tag_name.get(t, t)} ({why})" for t, why in worn))
            continue
        stash.call("mutation($i: BulkImageUpdateInput!) { bulkImageUpdate(input: $i) { id } }",
                   {"i": {"ids": [it["id"]], "tag_ids": {"ids": [t for t, _ in worn], "mode": "ADD"}}})
        record_machine_tags(plugin_dir, "image", it["id"], [t for t, _ in worn])
    if not preview and near_new:
        with locked_json(os.path.join(plugin_dir, "body_near.json")) as nd:
            for key, near in near_new.items():
                old = [e for e in nd.get(key, []) if e[0] not in {t for t, _ in near}]
                nd[key] = old + [[t, why] for t, why in near]
    progress(1)
    log("info", f"{'Preview done' if preview else 'Done'}: {tagged} image(s) {'would get' if preview else 'got'} "
        + (", ".join(f"{n} {name}" for name, n in added.most_common()) or "nothing")
        + f"; {nears} near-miss(es) {'would go' if preview else 'went'} to Review → Tags → Suggested.")


def body_item(stash, cfg, body, kind, item, apply, plugin_dir=None, rejected=(), skip_tagged=False):
    """Body tags for one item: [(tag id, name, why)] (added when apply).
    skip_tagged: nothing (and no frames looked at) when it already has a
    body tag."""
    have = {t["id"] for t in item.get("tags") or []}
    # clothing (photos only): from the picture fingerprint, so no detector needed
    worn, near, looks_like = clothing_picks(stash, cfg, body, kind, item, have, rejected, plugin_dir)
    if skip_tagged and has_body_tags(item):
        picks = worn
    else:
        pictures = body_pictures(stash, kind, item)
        found_all = body_helper(cfg, pictures) if pictures else []
        shown = parts_shown(cfg, pictures, found_all)
        picks = [(t, why) for t, why in body.picks(item, shown) if t not in rejected]
        picks += [(t, why) for t, why in worn if t not in [x for x, _ in picks]]
        # near-misses: what a little less sure detector would add, kept for
        # Review (never added by itself)
        near_shown = parts_shown(cfg, pictures, found_all, slack=BODY_NEAR)
        near += [(t, why) for t, why in body.picks(item, near_shown)
                 if t not in rejected and t not in have and t not in {x for x, _ in picks} and t not in {x for x, _ in near}]
        near = [(t, why) for t, why in near if t not in {x for x, _ in picks}]
        # Apple's tools (on a Mac): clothing labels and hands over the breasts
        apple = apple_helper(cfg, pictures)
        women_ok = not item.get("performers") or any(
            (body.perfs.get(p["id"]) or {}).get("gender") in FEMALE for p in item.get("performers") or [])
        if apple:
            one = len(pictures) == 1
            for label, (names, bar) in APPLE_LABELS.items():
                conf = sorted(((a or {}).get("labels", {}).get(label, 0) for a in apple), reverse=True)
                if not (conf and (conf[0] >= bar + BODY_ONE_PICTURE or (len(conf) > 1 and conf[1] >= bar))):
                    continue
                if label == "swimsuit":
                    bik = looks_like.get("Bikini")
                    names = ("Bikini", "Swimsuit") if bik and bik[1] >= 0.5 else names
                    looks_like.setdefault("_apple_swimsuit", (("Swimsuit",), conf[0], 0))
                tid = next((body.tag(n) for n in names if body.tag(n)), None)
                if tid and tid not in have and tid not in rejected and tid not in [t for t, _ in picks]:
                    picks.append((tid, f"Apple sees {label.replace('_', ' ')} ({round(conf[0] * 100)}%)"))
            hits = sum(hand_over_breasts(f, a) for f, a in zip(found_all, apple))
            if women_ok and (hits >= 1 if one else hits >= 2):
                tid = next((body.tag(n) for n in HAND_BRA[0] if body.tag(n)), None)
                bra = next((body.tag(n) for n in BODY_WORN["Bra"][0] if body.tag(n)), None)
                if tid and tid not in have and tid not in rejected:
                    picks = [(t, why) for t, why in picks if t != bra]  # (hands over the breasts, not a bra)
                    if tid not in [t for t, _ in picks]:
                        picks.append((tid, "hands over the breasts"))
        # nude: drop clothing from the picture that would cover what's bare
        nude_tag = next((body.tag(n) for n in BODY_NUDE[0] if body.tag(n)), None)
        if nude_tag and nude_tag in [t for t, _ in picks]:
            covering = {next((body.tag(n) for n in names if body.tag(n)), None)
                        for names, _, _ in looks_like.values()
                        if names[0] in ("Lingerie", "Panties", "Thong", "Bikini", "Latex", "Crop Top", "Skirt")} - {None}
            covering |= {next((body.tag(n) for n in names if body.tag(n)), None)
                         for names in (("Swimsuit",), ("Bikini",), ("Jeans",))} - {None}
            picks = [(t, why) for t, why in picks if t not in covering]
        # a bikini, not underwear: the detector sees the same covered parts
        bik = looks_like.get("Bikini")
        lin = looks_like.get("Lingerie")
        underwear = {next((body.tag(n) for n in BODY_WORN[k][0] if body.tag(n)), None) for k in ("Bra", "Panties")} - {None}
        bikini_tag = next((body.tag(n) for n in ("Bikini", "String Bikini") if body.tag(n)), None)
        apple_swim = "_apple_swimsuit" in looks_like
        if ((bik and bik[1] >= BIKINI_OVER_UNDERWEAR and bik[1] > (lin[1] if lin else 0)) or apple_swim) and bikini_tag \
                and any(t in underwear for t, _ in picks):
            picks = [(t, why) for t, why in picks if t not in underwear]
            if bikini_tag not in have and bikini_tag not in rejected and bikini_tag not in [t for t, _ in picks]:
                picks.append((bikini_tag, f"covered with a bare belly, and looks like a bikini ({round(bik[1] * 100)}%)"))
    gone = removed_tags(kind, item["id"], have)
    picks = [(t, why) for t, why in picks if str(t) not in gone]
    near = [(t, why) for t, why in near if str(t) not in gone]
    if apply and plugin_dir and not (skip_tagged and has_body_tags(item)):
        near = [(t, why) for t, why in near if t not in {x for x, _ in picks}]
        with contextlib.suppress(Exception), locked_json(os.path.join(plugin_dir, "body_near.json")) as nd:
            key = f"{kind}:{item['id']}"
            if near:
                nd[key] = [[t, why] for t, why in near]
            else:
                nd.pop(key, None)
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
    if not targets and not preview:
        # nothing left: say why, so a run that ends straight away isn't a mystery
        org = "" if (redo or cfg["ai"]["include_organized"]) else " that isn't marked organized"
        log("info", f"Nothing to do: every scene and image{org} has had body tags already "
            f"({len(done.get('scenes', ()))} scene(s), {len(done.get('images', ()))} image(s) done). New items get them after a scan. "
            + ("" if org == "" else "Organized items are left out unless \"AI - include organized items\" is on; ")
            + "\"Tags: redo body tags on all scenes and images\" goes through everything, organized ones too.")
        return
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
# With "New items: wait until night for the AI model steps" on, a scan's new items get the
# quick steps straight away (no AI model, about a second each: tags from the
# file names, studios from links in them, look-alike tags, body tags) and
# are saved in ai_night.json for the AI model steps (AI tags, watermarks, AI
# titles), which run once at night. A small background process in Stash's
# container (night_runner.*) wakes at that time and starts the task "AI: run
# tonight's new items now"; it's started again by any scan if Stash restarted.

NIGHT_TASK = "Schedules: run tonight's waiting steps for new items now"


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
                gone = removed_tags(kind, item["id"], own)
                picks = [t for t in picks if str(t) not in gone]
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
    if night_wanted(cfg):
        ensure_night_runner(stash, plugin_dir, cfg)  # e.g. after Stash restarted
    with locked_night(plugin_dir) as night:
        todo = {"scene": list(night.get("scenes", [])), "image": list(night.get("images", []))}
    n = len(todo["scene"]) + len(todo["image"])
    if not n:
        log("info", "No new items waiting for the AI.")
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
    log("info", "Tonight's AI run is done.")


# --- nightly jobs ------------------------------------------------------------
# The Nightly jobs page (/ai-nightly) picks what runs each night: Stash's own
# tasks (scan, auto tag, generate...) and any plugin's tasks. At the night time
# the night runner starts the task "Schedules: run the nightly jobs now", which queues them
# all in Stash's job queue - Stash runs one job at a time, so they run in this
# order - and starts a small tracker that records how each one went
# (night_jobs_last.json, shown on the page). Stash's tasks use the options
# saved as defaults on Stash's own Tasks page.

NIGHT_JOBS_TASK = "Schedules: run the nightly jobs now"
NIGHT_BODY_TASK = "Tags: add body tags to scenes and images not done yet"
NIGHT_LAST = "night_jobs_last.json"
JOBS_PAGE = "/ai-nightly"

# id: (label, what it does, when it runs: "before" the plugin tasks or "after")
STASH_NIGHT_JOBS = {
    "backup": ("Back up the database", "A copy of Stash's database in its backup folder (before anything else runs).", "before"),
    "scan": ("Scan for new files", "Stash's Scan of the whole library, with the scan options saved on Stash's Tasks page.", "before"),
    "clean": ("Clean: remove missing files", "Removes scenes, images and galleries whose files are gone. Skipped if a library folder "
              "is missing or empty that night (e.g. the NAS isn't mounted), so nothing is removed by mistake.", "before"),
    "identify": ("Identify from StashDB / scrapers", "Stash's Identify with the sources and options saved on Stash's Tasks "
                 "page. Skipped if none are set up.", "before"),
    "autotag": ("Auto Tag", "Performers, studios and tags from file names (Stash's Auto Tag, your saved options).", "before"),
    "generate": ("Generate", "Previews, sprites, covers, phashes and so on - what's ticked under Generate on Stash's Tasks page.", "after"),
    "clean_generated": ("Clean generated files", "Deletes generated files (previews, sprites, thumbnails...) that nothing uses any more.", "after"),
    "optimise": ("Optimise the database", "Tidies Stash's database so it stays quick (last, when nothing else runs).", "after"),
}
STASH_NIGHT_ORDER = ["backup", "scan", "clean", "identify", "autotag", "generate", "clean_generated", "optimise"]

# offered on the page as a start (this add-on's tasks that suit the night)
NIGHT_SUGGESTED = [NIGHT_TASK, "Tags: scan scenes and images for look-alikes (do this first)", "Faces: scan scenes and images",
                   NIGHT_BODY_TASK, "Tags: scan images' body poses for pose suggestions (Mac)", "Image scores: add aesthetic scores and labels (Mac)",
                   "Tag pictures: pick the most typical for tags without one", "Markers: add AI markers to scenes without markers",
                   "Scene covers: replace low-scoring ones (Mac)"]


def night_wanted(cfg):
    """Whether the night runner has anything to do."""
    nj, sj = cfg["ai"]["night_jobs"], cfg["ai"]["scan_jobs"]
    return bool(cfg["ai"]["nightly"] or nj["stash"] or nj["tasks"] or sj["stash"] or sj["tasks"])


_TYPES = {}


def _gql_type(stash, name):
    """A GraphQL type's fields (output) or input fields: {name: (kind, type name)}."""
    if name not in _TYPES:
        q = ("query($n: String!) { __type(name: $n) { kind fields { name type { kind name ofType { kind name "
             "ofType { kind name ofType { kind name } } } } } inputFields { name type { kind name ofType { kind name "
             "ofType { kind name ofType { kind name } } } } } } }")
        t = stash.call(q, {"n": name})["__type"] or {}
        out = {}
        for f in (t.get("fields") or t.get("inputFields") or []):
            ty = f["type"]
            while ty.get("ofType"):
                ty = ty["ofType"]
            out[f["name"]] = (ty["kind"], ty["name"])
        _TYPES[name] = out
    return _TYPES[name]


def _selection(stash, type_name, depth=0):
    parts = []
    for name, (kind, tname) in _gql_type(stash, type_name).items():
        if kind == "OBJECT":
            if depth < 4:
                sub = _selection(stash, tname, depth + 1)
                if sub:
                    parts.append(f"{name} {{ {sub} }}")
        else:
            parts.append(name)
    return " ".join(parts)


def _as_input(stash, value, input_type):
    """A saved-defaults value (output type) shaped as the matching input type:
    only the fields the input has, nested ones too, no empty values."""
    if isinstance(value, list):
        return [_as_input(stash, v, input_type) for v in value]
    if not isinstance(value, dict):
        return value
    fields = _gql_type(stash, input_type)
    out = {}
    for k, v in value.items():
        if v is None or k not in fields:
            continue
        kind, tname = fields[k]
        out[k] = _as_input(stash, v, tname) if kind == "INPUT_OBJECT" else v
    return out


def stash_defaults(stash, which):
    """The options chosen on Stash's Tasks page, as task input: which =
    scan / autoTag / generate / identify (fields the input doesn't have are
    left out, so it works across Stash versions)."""
    out_type = {"scan": "ScanMetadataOptions", "autoTag": "AutoTagMetadataOptions",
                "generate": "GenerateMetadataOptions", "identify": "IdentifyMetadataTaskOptions"}[which]
    in_type = {"scan": "ScanMetadataInput", "autoTag": "AutoTagMetadataInput",
               "generate": "GenerateMetadataInput", "identify": "IdentifyMetadataInput"}[which]
    # Stash's Tasks page keeps its checkboxes in the UI settings
    # (ui.taskDefaults); older ones / Identify also in configuration.defaults
    ui = stash.call("{ configuration { ui } }")["configuration"].get("ui") or {}
    d = ((ui.get("taskDefaults") or {}) if isinstance(ui, dict) else {}).get(which)
    if not d:
        sel = _selection(stash, out_type)
        d = stash.call(f"{{ configuration {{ defaults {{ {which} {{ {sel} }} }} }} }}")["configuration"]["defaults"][which]
    return _as_input(stash, d or {}, in_type)


def _library_folders_ok(stash):
    """Every library folder there and not empty (else: the folder that isn't)."""
    for s in stash.call("{ configuration { general { stashes { path } } } }")["configuration"]["general"]["stashes"] or []:
        p = s["path"]
        try:
            with os.scandir(p) as it:
                if next(it, None) is None:
                    return False, p
        except OSError:
            return False, p
    return True, ""


def _start_stash_job(stash, job):
    """Queue one of Stash's tasks: (job id or None, note)."""
    if job == "scan":
        i = stash_defaults(stash, "scan")
        return stash.call("mutation($i: ScanMetadataInput!) { metadataScan(input: $i) }", {"i": i})["metadataScan"], ""
    if job == "clean":
        ok, where = _library_folders_ok(stash)
        if not ok:
            return None, f"skipped: the library folder {where} is missing or empty tonight"
        return stash.call("mutation { metadataClean(input: {dryRun: false}) }")["metadataClean"], ""
    if job == "identify":
        i = stash_defaults(stash, "identify")
        if not i.get("sources"):
            return None, "skipped: no Identify sources are set up (Stash → Settings → Tasks → Identify)"
        return stash.call("mutation($i: IdentifyMetadataInput!) { metadataIdentify(input: $i) }", {"i": i})["metadataIdentify"], ""
    if job == "autotag":
        i = stash_defaults(stash, "autoTag") or {}
        if not any(i.get(k) for k in ("performers", "studios", "tags")):
            i = dict(i, performers=["*"], studios=["*"], tags=["*"])
        return stash.call("mutation($i: AutoTagMetadataInput!) { metadataAutoTag(input: $i) }", {"i": i})["metadataAutoTag"], ""
    if job == "generate":
        i = stash_defaults(stash, "generate")
        if isinstance(i.get("previewOptions"), dict):
            # 0 / empty there means "not set": leave them out so Stash uses
            # the preview settings from its main settings
            i["previewOptions"] = {k: v for k, v in i["previewOptions"].items() if v not in (0, "", None)}
        if not any(v for v in i.values() if v is True):
            return None, "skipped: nothing is ticked under Generate on Stash's Tasks page"
        return stash.call("mutation($i: GenerateMetadataInput!) { metadataGenerate(input: $i) }", {"i": i})["metadataGenerate"], ""
    if job == "clean_generated":
        fields = _gql_type(stash, "CleanGeneratedInput")
        i = {k: True for k, (kind, _) in fields.items() if kind == "SCALAR" and k != "dryRun"}
        if "dryRun" in fields:
            i["dryRun"] = False
        return stash.call("mutation($i: CleanGeneratedInput!) { metadataCleanGenerated(input: $i) }", {"i": i})["metadataCleanGenerated"], ""
    if job == "optimise":
        return stash.call("mutation { optimiseDatabase }")["optimiseDatabase"], ""
    raise ValueError(job)


# The two job lists: what they're called, where they're kept, their task
JOB_LISTS = {
    "night": {"setting": "nightJobs", "last": NIGHT_LAST, "task": NIGHT_JOBS_TASK, "name": "Nightly jobs"},
    "scan": {"setting": "afterScanJobs", "last": "scan_jobs_last.json", "task": "Schedules: run the after-scan jobs now",
             "name": "After-scan jobs"},
}
# never after a scan: it would start itself again and again
NOT_AFTER_SCAN = {"scan"}


def run_job_list(stash, plugin_dir, which="night"):
    """Tasks "Schedules: run the nightly jobs now" / "Schedules: run the after-scan jobs now": queue the
    list's jobs in order, then start the tracker that records how they went."""
    meta = JOB_LISTS[which]
    cfg = settings(stash, plugin_dir)
    nj = cfg["ai"]["night_jobs" if which == "night" else "scan_jobs"]
    if night_wanted(cfg):
        ensure_night_runner(stash, plugin_dir, cfg)
    order = [i for i in nj["order"] if not (which == "scan" and i.get("stash") in NOT_AFTER_SCAN)]
    if which == "night":
        # new items saved for tonight are always done, even if not picked:
        # after the list's Stash tasks that come first (scan, identify...)
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, "ai_night.json")) as f:
                night = json.load(f)
            if (night.get("scenes") or night.get("images")) and not any(
                    i.get("plugin") == AI_ID and i.get("task") == NIGHT_TASK for i in order):
                at = 0
                while at < len(order) and STASH_NIGHT_JOBS.get(order[at].get("stash"), ("", "", ""))[2] == "before":
                    at += 1
                order.insert(at, {"plugin": AI_ID, "task": NIGHT_TASK})
    plan = [("stash", i["stash"]) if "stash" in i else ("task", i) for i in order]
    if not plan:
        log("info", f"{meta['name']}: nothing is chosen (pick them on the page {JOBS_PAGE}).")
        return
    plugins = stash.call("{ plugins { id name enabled tasks { name } } }")["plugins"]
    names = {p["id"]: p["name"] for p in plugins}
    # one job at a time: the next starts once the one before has finished and
    # what it set off (a scan's new items: titles, tags...) has run too - so
    # that comes before the slow jobs later in the list (see night_track_main)
    rows = []
    for kind, j in plan:
        row = {"kind": kind, "label": STASH_NIGHT_JOBS[j][0] if kind == "stash" else f"{j['task']} ({names.get(j['plugin'], j['plugin'])})",
               "status": "waiting", "job": None, "note": "", "spec": {"stash": j} if kind == "stash" else dict(j)}
        if kind == "stash":
            row["stash_job"] = j
        rows.append(row)
    _start_next_rows(stash, rows, meta)
    with locked_json(os.path.join(plugin_dir, meta["last"])) as last:
        last.clear()
        last.update(started=time.strftime("%Y-%m-%dT%H:%M:%S%z"), jobs=rows,
                    done=not any(r["job"] or r["status"] == "waiting" for r in rows))
    if any(r["job"] or r["status"] == "waiting" for r in rows):
        _spawn_self(plugin_dir, "--night-track", which)
    log("info", f"{meta['name']}: {len(rows)} job(s); Stash runs them one after another, each once the one before "
        "and what it set off (e.g. a scan's new items) are done.")


JOB_LIST_SETTLE_MAX = 45 * 60  # longest wait for other jobs (e.g. a scan's follow-ups) before the next
JOB_LIST_SETTLE_MIN = 20       # seconds after a job before the queue counts as clear


def _start_row(stash, row, installed):
    """Start one job of a list (row["spec"]); updates the row."""
    spec = row.get("spec") or {}
    try:
        if "stash" in spec and spec["stash"] == "backup":
            path = stash.call("mutation { backupDatabase(input: {download: false}) }")["backupDatabase"]
            row.update(status="finished", note=f"saved {os.path.basename(path or '')}".strip())
        elif "stash" in spec:
            row["job"], row["note"] = _start_stash_job(stash, spec["stash"])
            row["status"] = "queued" if row["job"] is not None else "skipped"
        elif spec.get("task") not in installed.get(spec.get("plugin"), set()):
            row.update(status="skipped", note="that plugin or task isn't installed / turned on any more")
        else:
            row["job"] = stash.call("mutation($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }",
                                    {"p": spec["plugin"], "t": spec["task"]})["runPluginTask"]
            row["status"] = "queued"
    except Exception as e:  # one job failing to start mustn't stop the rest
        row.update(status="failed", note=f"couldn't start: {str(e)[:200]}")


def _start_next_rows(stash, rows, meta):
    """Start the next waiting job of a list (and any that finish at once, like
    a backup). Returns whether one was started."""
    installed = None
    for row in rows:
        if row["status"] != "waiting":
            continue
        if installed is None:
            plugins = stash.call("{ plugins { id enabled tasks { name } } }")["plugins"]
            installed = {p["id"]: {t["name"] for t in p.get("tasks") or []} for p in plugins if p.get("enabled")}
        _start_row(stash, row, installed)
        log("info", f"{meta['name']}: {row['label']} - {row['status']}" + (f" ({row['note']})" if row["note"] else ""))
        if row["job"]:
            return True
    return False


def run_nightly_jobs(stash, plugin_dir):
    run_job_list(stash, plugin_dir, "night")


def _spawn_self(plugin_dir, *args):
    """Start better_tagging_ai.py <args> in the background (it outlives this
    task)."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "better_tagging_ai.py")
    if not os.path.exists(script):
        script = os.path.join(plugin_dir, "better_tagging_ai.py")
    flag, rest = args[0], list(args[1:])
    extra = ({"creationflags": 0x00000200 | 0x08000000} if os.name == "nt" else {"start_new_session": True})
    with open(_night_files(plugin_dir)["log"], "a") as logf:
        return subprocess.Popen([sys.executable, script, flag, plugin_dir, *rest], stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=logf, close_fds=True, **extra)


def night_track_main(plugin_dir, which="night"):
    """Background: follow a list's queued jobs until each one finishes, and
    note how it went. Stash keeps only its last few finished jobs, so it
    checks every 15 s."""
    with open(_night_files(plugin_dir)["json"]) as f:
        stash = Stash(json.load(f)["conn"])
    path = os.path.join(plugin_dir, JOB_LISTS.get(which, JOB_LISTS["night"])["last"])
    give_up = time.time() + 20 * 3600
    final = {"FINISHED": "finished", "FAILED": "failed", "CANCELLED": "stopped"}
    while time.time() < give_up:
        with locked_json(path) as last:
            if not last.get("jobs"):  # (the results were cleared meanwhile: nothing to follow)
                return
            pending = [r for r in last.get("jobs", []) if r.get("job") and r["status"] in ("queued", "running")]
            for r in pending:
                try:
                    j = stash.call("query($i: ID!) { findJob(input: {id: $i}) { status error startTime endTime } }",
                                   {"i": str(r["job"])})["findJob"]
                except Exception:
                    j = None
                if j is None:
                    if r["status"] == "running":  # gone before we saw it end
                        r["status"] = "finished"
                        r["note"] = r.get("note") or "finished (its result wasn't recorded)"
                    continue
                if j["status"] in final:
                    r["status"] = final[j["status"]]
                    if j.get("error"):
                        r["note"] = j["error"][:300]
                    r["ended"] = j.get("endTime")
                elif j["status"] in ("RUNNING", "STOPPING"):
                    r["status"] = "running"
                    r["began"] = j.get("startTime")
            if not any(r.get("job") and r["status"] in ("queued", "running") for r in last.get("jobs", [])):
                waiting = [r for r in last.get("jobs", []) if r["status"] == "waiting"]
                if waiting:
                    # the next one once Stash's queue is clear (what the last
                    # job set off has run), or after JOB_LIST_SETTLE_MAX
                    idle_since = last.setdefault("idle_since", time.time())
                    busy = []
                    with contextlib.suppress(Exception):
                        busy = stash.call("{ jobQueue { id } }")["jobQueue"] or []
                    settled = time.time() - idle_since >= JOB_LIST_SETTLE_MIN  # (follow-ups can take a moment to appear)
                    if settled and not busy or time.time() - idle_since > JOB_LIST_SETTLE_MAX:
                        last.pop("idle_since", None)
                        if _start_next_rows(stash, last["jobs"], JOB_LISTS.get(which, JOB_LISTS["night"])):
                            continue
                    if not any(r["status"] == "waiting" for r in last["jobs"]):
                        last["done"] = True
                        last["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                        return
                else:
                    last["done"] = True
                    last["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    return
        time.sleep(15)


def run_nightly_info(stash, plugin_dir):
    """For the jobs page: both lists, what's installed, the next night run
    and how each list's last run went."""
    from datetime import datetime, timedelta  # noqa: PLC0415
    cfg = settings(stash, plugin_dir)
    ai = cfg["ai"]
    nxt = None
    with contextlib.suppress(Exception):
        from zoneinfo import ZoneInfo  # noqa: PLC0415
        tz = ZoneInfo(ai["time_zone"])
        now = datetime.now(tz)
        hh, mm = (int(x) for x in ai["night_time"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        nxt = (at if at > now else at + timedelta(days=1)).isoformat()

    def last_of(which):
        try:
            with open(os.path.join(plugin_dir, JOB_LISTS[which]["last"])) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    try:
        with open(_night_files(plugin_dir)["pid"]) as f:
            alive = pid_running(int(f.read().strip()), "--night-runner")
    except (OSError, ValueError):
        alive = False
    try:
        with open(os.path.join(plugin_dir, "ai_night.json")) as f:
            night = json.load(f)
        waiting = len(night.get("scenes") or []) + len(night.get("images") or [])
    except (OSError, ValueError):
        waiting = 0
    return {"time": ai["night_time"], "tz": ai["time_zone"], "next": nxt, "runner": alive, "waiting": waiting,
            "nightly_items": ai["nightly"],
            "chosen": ai["night_jobs"], "last": last_of("night"),
            "scan_chosen": ai["scan_jobs"], "scan_last": last_of("scan"),
            "not_after_scan": sorted(NOT_AFTER_SCAN),
            "saved_at": _job_lists_saved_at(),
            "stash_jobs": [{"id": k, "label": STASH_NIGHT_JOBS[k][0], "about": STASH_NIGHT_JOBS[k][1],
                            "when": STASH_NIGHT_JOBS[k][2]} for k in STASH_NIGHT_ORDER],
            "suggested": [{"plugin": AI_ID, "task": t} for t in NIGHT_SUGGESTED]}


# --- before a task starts (Settings → Tasks) ----------------------------------
# The page asks this when one of the add-on's tasks is pressed: whether what it
# needs is set up (else the page points to the setup page), and roughly how
# long it will take (a long one gets a warning with Run / Add to the nightly
# run / Don't show again). Times are rough: seconds per item measured on a Mac
# mini with gemma3:4b, times the items still to do.
TASK_LONG = 10 * 60   # seconds: longer than this gets the warning
# mode: (needs, how to count what's left, seconds per scene, seconds per photo / other item)
TASK_COST = {
    "ai_untagged": ("ollama", "untagged_ai", 6, 4),
    "ai_all": ("ollama", "ai_left", 6, 4),
    "night_run": ("ollama", None, 0, 0),
    "looks_scan": ("helper", "looks_left", 1.5, 0.4),
    "looks_untagged": ("helper looks", "untagged", 0.05, 0.05),
    "body_all": ("helper", "body_left", 3, 1),
    "body_redo": ("helper", "all", 3, 1),
    "body_looks": ("helper looks", None, 0, 0),
    "pose_scan": ("apple", "poses_left", 0, 0.3),
    "faces_scan": ("helper", "faces_left", 3, 1),
    "faces_learn": ("helper", "performers", 0, 0.2),
    "faces_learn_tagged": ("helper", "solo", 0.3, 0.3),
    "faces_scenes": ("helper", "no_performer_scenes", 1, 0),
    "faces_images": ("helper", "no_performer_images", 0, 1),
    "faces_duplicates": ("helper", None, 0, 0),
    "video_markers_all": ("video", "no_marker_scenes", 40, 0),
    "video_markers_redo": ("video", "ai_marker_scenes", 40, 0),
    "video_scan": ("video", "video_left", 10, 0),
    "ai_titles_scenes": ("ollama", "untitled_scenes", 5, 0),
    "ai_titles_images": ("ollama", "untitled_images", 0, 4),
    "handle_studios": ("ollama", "studio_left", 10, 0),
    "photo_studios": ("ollama", "no_studio_loose_photos", 0, 4),
    "photo_studios_all": ("ollama", "no_studio_photos", 0, 4),
    "typical_missing": ("helper looks", "tags_no_picture", 0, 0.5),
    "typical_all": ("helper looks", "tags", 0, 0.5),
    "better_tag_pictures": ("apple", "tags_with_picture", 0, 1),
    "better_marker_pictures": ("apple", "markers", 0, 1),
    "picture_facts": ("apple", "photos_no_score", 0, 0.3),
    "better_covers": ("apple", "covers_left", 2, 0),
    "screenshot_crop": ("helper", "photos", 0, 0.02),
    "perf_details": ("ollama", "performers_no_colours", 0, 3),
}


def _count(stash, kind, flt=None):
    q, arg, typ = {"scene": ("findScenes", "scene_filter", "SceneFilterType"), "image": ("findImages", "image_filter", "ImageFilterType"),
                   "performer": ("findPerformers", "performer_filter", "PerformerFilterType"), "tag": ("findTags", "tag_filter", "TagFilterType"),
                   "marker": ("findSceneMarkers", "scene_marker_filter", "SceneMarkerFilterType")}[kind]
    return stash.call(f"query($f: {typ}) {{ {q}({arg}: $f, filter: {{per_page: 0}}) {{ count }} }}", {"f": flt or {}})[q]["count"]


def _task_left(stash, plugin_dir, cfg, what):
    """(scenes, other items) still to do for TASK_COST's `what`."""
    def done(name, key):
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, name)) as fh:
                return len(json.load(fh).get(key) or [])
        return 0

    def keys(name, prefix):
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, name)) as fh:
                return sum(1 for k in json.load(fh) if k.startswith(prefix))
        return 0

    tag_kinds = cfg["ai"]["tag_kinds"]
    sc = lambda f=None: _count(stash, "scene", f)  # noqa: E731
    im = lambda f=None: _count(stash, "image", f)  # noqa: E731
    none = {"value": 0, "modifier": "EQUALS"}
    if what == "untagged_ai":
        return (sc({"tag_count": none}) if "scene" in tag_kinds else 0, im({"tag_count": none}) if "image" in tag_kinds else 0)
    if what == "ai_left":
        return (max(0, sc() - done("ai_done.json", "scenes")) if "scene" in tag_kinds else 0,
                max(0, im() - done("ai_done.json", "images")) if "image" in tag_kinds else 0)
    if what == "untagged":
        return sc({"tag_count": none}), im({"tag_count": none})
    if what == "all":
        return sc(), im()
    if what == "looks_left":
        return max(0, sc() - keys("look_items.json", "scene:")), max(0, im() - keys("look_items.json", "image:"))
    if what == "faces_left":
        return max(0, sc() - keys("face_items.json", "scene:")), max(0, im() - keys("face_items.json", "image:"))
    if what == "body_left":
        return max(0, sc() - done("body_done.json", "scenes")), max(0, im() - done("body_done.json", "images"))
    if what == "photos":
        return 0, im()
    if what == "photos_no_score":
        return 0, im({"custom_fields": [{"field": "aesthetic", "modifier": "IS_NULL"}]})
    if what == "performers":
        return 0, _count(stash, "performer")
    if what == "solo":
        one = {"performer_count": {"value": 1, "modifier": "EQUALS"}}
        return sc(one), im(one)
    if what in ("no_performer_scenes", "no_performer_images"):
        # (faces scanned already are only compared: quick; the rest are scanned)
        kind = what.split("_")[-1][:-1]
        n = (sc if kind == "scene" else im)({"performer_count": none})
        total = (sc if kind == "scene" else im)()
        left = max(0.0, 1 - keys("face_items.json", kind + ":") / max(total, 1))
        n = round(n * (left + 0.03))
        return (n, 0) if kind == "scene" else (0, n)
    if what == "poses_left":
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, "poses.json")) as fh:
                return 0, max(0, im() - len(json.load(fh)))
        return 0, im()
    if what == "performers_no_colours":
        return 0, _count(stash, "performer", {"hair_color": {"value": "", "modifier": "IS_NULL"}})
    if what == "no_marker_scenes":
        return sc({"has_markers": "false"}), 0
    if what == "ai_marker_scenes":
        tag = ai_marker_tag(stash, create=False)[0]
        return (sc({"has_markers": "true"}) if not tag else
                _count(stash, "scene", {"scene_tags": {"value": [tag], "modifier": "INCLUDES"}}) or sc({"has_markers": "true"})), 0
    if what == "video_left":
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, "video_acts.json")) as fh:
                return max(0, sc() - len(json.load(fh))), 0
        return sc(), 0
    if what == "untitled_scenes":
        return sc({"title": {"value": "", "modifier": "IS_NULL"}}), 0
    if what == "untitled_images":
        return 0, im({"title": {"value": "", "modifier": "IS_NULL"}})
    if what == "studio_left":
        return max(0, sc({"studios": {"value": [], "modifier": "IS_NULL"}}) - done("handle_scan.json", "scenes")), 0
    if what == "no_studio_photos":
        return 0, im({"studios": {"value": [], "modifier": "IS_NULL"}})
    if what == "no_studio_loose_photos":
        return 0, im({"studios": {"value": [], "modifier": "IS_NULL"}, "galleries": {"value": [], "modifier": "IS_NULL"}})
    if what == "tags":
        return 0, _count(stash, "tag")
    if what == "tags_no_picture":
        return 0, _count(stash, "tag", {"is_missing": "image"})
    if what == "tags_with_picture":
        return 0, max(0, _count(stash, "tag") - _count(stash, "tag", {"is_missing": "image"}))
    if what == "markers":
        return 0, _count(stash, "marker")
    if what == "covers_left":
        with contextlib.suppress(OSError, ValueError):
            with open(_cover_files(stash)[0]) as fh:
                return max(0, sc() - len(json.load(fh))), 0
        return sc(), 0
    return 0, 0


def _task_needs(stash, plugin_dir, cfg, needs):
    """[{"what", "ok", "fix"}] for TASK_COST's needs (quick checks)."""
    ai, out = cfg["ai"], []
    for need in needs.split():
        if need == "ollama":
            ok, fix = False, f"Start Ollama (it should answer at {ai['url']})."
            with contextlib.suppress(Exception):
                with urllib.request.urlopen(ai["url"] + "/api/tags", timeout=4) as resp:
                    names = {m.get("name") for m in json.loads(resp.read()).get("models") or []}
                want = ai["model"] if ":" in ai["model"] else ai["model"] + ":latest"
                ok = want in names or ai["model"] in names
                fix = "" if ok else f"Download the AI model: ollama pull {ai['model']}"
            out.append({"what": f"The AI model ({ai['model']} in Ollama)", "ok": ok, "fix": fix})
        elif need in ("helper", "apple"):
            health = {}
            with contextlib.suppress(Exception):
                with urllib.request.urlopen(ai["face_url"] + "/health", timeout=4) as resp:
                    health = json.loads(resp.read())
            if need == "helper":
                out.append({"what": "The face helper", "ok": bool(health),
                            "fix": "" if health else f"Start the face helper (it should answer at {ai['face_url']}); see face-helper/setup.sh."})
            else:
                out.append({"what": "Apple's picture scores (the face helper on a Mac)", "ok": bool(health.get("apple")),
                            "fix": "" if health.get("apple") else ("This needs the face helper running on a Mac (Apple's aesthetics score)."
                                                                    if health else f"Start the face helper on a Mac (it should answer at {ai['face_url']}).")})
        elif need == "video":
            ok = _video_model_ready(video_model_dir(plugin_dir))
            out.append({"what": "The video model", "ok": ok,
                        "fix": "" if ok else 'Run "Markers: download the video model (needed first)" first (Markers & positions).'})
        elif need == "looks":
            n = 0
            with contextlib.suppress(OSError, ValueError):
                with open(os.path.join(plugin_dir, "look_items.json")) as fh:
                    n = len(json.load(fh))
            ok = n >= 50
            out.append({"what": "The look-alike scan", "ok": ok,
                        "fix": "" if ok else 'Run "Tags: scan scenes and images for look-alikes (do this first)" first.'})
    return out


def run_task_check(stash, plugin_dir, task):
    """Settings → Tasks, before one of the add-on's tasks starts: {"task",
    "needs": [{what, ok, fix}], "ready", "scenes", "items", "seconds", "long",
    "nightly" (already in the nightly run), "night_time"}."""
    mode = None
    with contextlib.suppress(OSError):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "BetterTaggingAI.yml"), encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r'- name: "?' + re.escape(task) + r'"?\s*\n(?:.*\n){0,3}?\s*mode: "?([a-z_]+)', text)
        mode = m and m.group(1)
    cost = TASK_COST.get(mode or "")
    cfg = settings(stash, plugin_dir)
    out = {"task": task, "mode": mode, "needs": [], "ready": True, "scenes": 0, "items": 0, "seconds": 0, "long": False}
    if not cost:
        return out
    needs, what, per_scene, per_item = cost
    out["needs"] = _task_needs(stash, plugin_dir, cfg, needs)
    out["ready"] = all(n["ok"] for n in out["needs"])
    if what:
        with contextlib.suppress(Exception):
            scenes, items = _task_left(stash, plugin_dir, cfg, what)
            out.update(scenes=scenes, items=items, seconds=round(scenes * per_scene + items * per_item))
            out["noun"] = ("performers" if what.startswith("performers") else "tags" if what.startswith("tags")
                           else "markers" if what == "markers" else "photos")
    out["long"] = out["seconds"] >= TASK_LONG
    order = cfg["ai"]["night_jobs"]["order"]
    out["nightly"] = any(i.get("plugin") == AI_ID and i.get("task") == task for i in order)
    out["night_time"] = cfg["ai"]["night_time"]
    return out


def run_task_add_nightly(stash, plugin_dir, task):
    """Adds one of the add-on's tasks to the end of the nightly run."""
    order = list(settings(stash, plugin_dir)["ai"]["night_jobs"]["order"])
    if not any(i.get("plugin") == AI_ID and i.get("task") == task for i in order):
        order.append({"plugin": AI_ID, "task": task})
    run_nightly_save(stash, plugin_dir, {"which": "night", "order": order})
    return {"night_time": settings(stash, plugin_dir)["ai"]["night_time"], "position": len(order)}


def run_nightly_save(stash, plugin_dir, data):
    """Save the jobs page: one list (data["which"]: night / scan), and for the
    night the time and time zone (merged into the add-on's settings, which
    Stash replaces as a whole)."""
    data = data or {}
    which = data.get("which") if data.get("which") in JOB_LISTS else "night"
    order = (_clean_order(data["order"]) if "order" in data
             else _old_order(data.get("stash") or [], data.get("tasks") or []))
    # never: a list's own task in it (it would start itself), Scan after a scan
    own = {JOB_LISTS[w]["task"] for w in JOB_LISTS}
    order = [i for i in order if not (i.get("plugin") == AI_ID and i.get("task") in own)
             and not (which == "scan" and i.get("stash") in NOT_AFTER_SCAN)]
    with locked_json(_job_lists_path()) as kept:
        # a page that loaded the list before it was saved elsewhere (another
        # device or tab) mustn't put its older list back
        now_at = (kept.get(which) or {}).get("at", "")
        if "base" in data and data["base"] != now_at and now_at:
            raise NotReady(f"this list was changed on another device or tab (saved {now_at}) since this page loaded it - "
                           "the page now shows that list; make your changes again and save")
        kept[which] = {"order": order, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    cur = (stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {}).get(AI_ID) or {}
    new = dict(cur, **{JOB_LISTS[which]["setting"]: json.dumps({"order": order})})
    if which == "night":
        if data.get("time"):
            new["aiNightTime"] = _clock(data["time"], cur.get("aiNightTime") or "03:00")
        if data.get("tz"):
            from zoneinfo import ZoneInfo  # noqa: PLC0415
            try:
                ZoneInfo(str(data["tz"]).strip())
            except Exception:
                raise NotReady(f'"{data["tz"]}" isn\'t a time zone Stash knows - e.g. America/New_York or Europe/London') from None
            new["aiTimeZone"] = str(data["tz"]).strip()
        new.pop("aiNightBody", None)  # (now a nightly job of its own)
    stash.call("mutation($id: ID!, $i: Map!) { configurePlugin(plugin_id: $id, input: $i) }", {"id": AI_ID, "i": new})
    cfg = settings(stash, plugin_dir)
    if night_wanted(cfg):
        ensure_night_runner(stash, plugin_dir, cfg)
    return run_nightly_info(stash, plugin_dir)


# bumped when the night runner's code changes: a runner started with older
# code is replaced (Python keeps running the code it started with)
NIGHT_RUNNER_VERSION = 3


def _night_files(plugin_dir):
    return {k: os.path.join(plugin_dir, "night_runner." + k) for k in ("json", "pid", "log", "version")}


def ensure_night_runner(stash, plugin_dir, cfg):
    """Start the background process that starts the nightly and after-scan
    jobs (unless it's running already); it gets the current settings and
    connection."""
    files = _night_files(plugin_dir)
    tmp = files["json"] + ".tmp"
    with open(tmp, "w") as f:
        nj, sj = cfg["ai"]["night_jobs"], cfg["ai"]["scan_jobs"]
        json.dump({"conn": stash.conn, "time": cfg["ai"]["night_time"], "tz": cfg["ai"]["time_zone"],
                   "plugin_id": AI_ID, "task": NIGHT_JOBS_TASK, "jobs": bool(nj["stash"] or nj["tasks"]),
                   "scan_task": JOB_LISTS["scan"]["task"], "after_scan": bool(sj["stash"] or sj["tasks"])}, f)
    _replace(tmp, files["json"])
    with contextlib.suppress(OSError, ValueError):
        with open(files["pid"]) as f:
            pid = int(f.read().strip())
        with open(files["version"]) as f:
            current = f.read().strip() == str(NIGHT_RUNNER_VERSION)
        if current and pid_running(pid, "--night-runner"):  # still there, up to date
            return
    proc = _spawn_self(plugin_dir, "--night-runner")
    with open(files["pid"], "w") as f:  # straight away, so a second call sees it
        f.write(str(proc.pid))


# after a scan you started finishes: wait this long for more scans before
# starting the after-scan jobs, and never start them more often than this
AFTER_SCAN_SETTLE = 30
AFTER_SCAN_MIN_GAP = 120


def night_runner_main(plugin_dir):
    """The background process: at the night time it starts the nightly jobs;
    with after-scan jobs chosen, it also watches Stash's job queue and starts
    them once a scan you started has finished. Exits if another one took
    over."""
    from datetime import datetime, timedelta  # noqa: PLC0415
    files = _night_files(plugin_dir)
    with open(files["pid"], "w") as f:
        f.write(str(os.getpid()))
    with open(files["version"], "w") as f:
        f.write(str(NIGHT_RUNNER_VERSION))

    def say(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

    def start(conf, task, what):
        stash = Stash(conf["conn"])
        job = stash.call("mutation($p: ID!, $t: String!) { runPluginTask(plugin_id: $p, task_name: $t) }",
                         {"p": conf["plugin_id"], "t": task})
        say(f"started {what} (job {job.get('runPluginTask')})")

    def ours():
        """Scans the nightly list started: not "a scan you started"."""
        ids = set()
        with contextlib.suppress(OSError, ValueError):
            with open(os.path.join(plugin_dir, NIGHT_LAST)) as f:
                ids = {str(r.get("job")) for r in json.load(f).get("jobs", []) if r.get("stash_job") == "scan"}
        return ids

    say("night runner started")
    conf, at, tz = None, None, None
    watching, settled_since, last_after = set(), None, 0.0
    while True:
        try:
            with open(files["pid"]) as f:
                if f.read().strip() != str(os.getpid()):
                    say("another night runner took over; stopping")
                    return
            with open(files["json"]) as f:
                new = json.load(f)
            if new != conf:  # settings or connection changed: work out the time again
                conf = new
                try:
                    from zoneinfo import ZoneInfo  # noqa: PLC0415
                    tz = ZoneInfo(conf.get("tz") or "UTC")
                except Exception:
                    tz = None
                now = datetime.now(tz)
                hh, mm = (int(x) for x in (conf.get("time") or "03:00").split(":"))
                at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if at <= now:
                    at += timedelta(days=1)
            # the night
            if datetime.now(tz) >= at:
                at += timedelta(days=1)
                try:
                    with open(os.path.join(plugin_dir, "ai_night.json")) as f:
                        night = json.load(f)
                except (OSError, ValueError):
                    night = {}
                if night.get("scenes") or night.get("images") or conf.get("jobs"):
                    start(conf, conf.get("task") or NIGHT_JOBS_TASK, "tonight's jobs")
                else:
                    say("nothing to do tonight")
            # after a scan you started
            if conf.get("after_scan"):
                stash = Stash(conf["conn"])
                queue = stash.call("{ jobQueue { id status description } }")["jobQueue"] or []
                scanning = {str(j["id"]) for j in queue if (j.get("description") or "").startswith("Scanning")}
                skip = ours()
                for jid in list(watching - scanning):  # gone from the queue: how did it end?
                    watching.discard(jid)
                    j = stash.call("query($i: ID!) { findJob(input: {id: $i}) { status } }", {"i": jid})["findJob"]
                    if j is None or j["status"] == "FINISHED":
                        settled_since = time.time()
                        say(f"a scan finished (job {jid})")
                watching |= scanning - skip
                if settled_since and not scanning and time.time() - settled_since >= AFTER_SCAN_SETTLE:
                    settled_since = None
                    if time.time() - last_after >= AFTER_SCAN_MIN_GAP:
                        last_after = time.time()
                        start(conf, conf.get("scan_task") or JOB_LISTS["scan"]["task"], "the after-scan jobs")
                time.sleep(15)
            else:
                watching, settled_since = set(), None
                left = (at - datetime.now(tz)).total_seconds()
                time.sleep(max(1, min(left, 300)))
        except Exception as e:
            say(f"problem: {e}; trying again in 2 minutes")
            time.sleep(120)


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
        + (". The AI takes a few seconds each, so this can take a while; you can cancel it under Settings → Tasks."
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
    _replace(tmp, ctx["pending_path"])


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
    confirmed = set()  # handles read in another item too (pending_handle)
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
                    confirmed.add(handle)
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
    # the accounts' profile links (and links written in the title or
    # description) go in its URLs
    links = item_links(scene, handles, known, confirmed)
    if links:
        parts.append(("link " if len(links) == 1 else "links ") + ", ".join(links))
        inp["urls"] = list(scene.get("urls") or []) + links
    if not parts:
        return None, False
    if apply and len(inp) > 1:
        if kind == "image":
            stash.call("mutation($i: ImageUpdateInput!) { imageUpdate(input: $i) { id } }", {"i": inp})
        else:
            stash.call("mutation($i: SceneUpdateInput!) { sceneUpdate(input: $i) { id } }", {"i": inp})
    others = ", ".join(f"{plat_name(p)} @{h}" for p, h, _ in handles[1:])
    return f'{"Would give" if not apply else "Gave"} {name} ' + " and ".join(parts) + (f"; also saw {others}" if others else ""), new


LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _same_link(u):
    u = u.strip().lower().rstrip("/")
    return re.sub(r"^https?://(www\.|m\.)?", "", u)


def item_links(item, handles, known, confirmed):
    """Links to add to a scene's (or photo's) URLs that it doesn't have yet:
    links written in its title or description (not a bare home page), and the profile link of each
    account it shows - from its file name, title, description or links as
    written; from a watermark only when it's the name of a studio or
    performer you have, or was read in another item too (a misread name
    mustn't become a link)."""
    have = {_same_link(u) for u in item.get("urls") or []}
    out = []

    def add(u):
        u = u.rstrip(".,;:!?")
        if _same_link(u) not in have:
            have.add(_same_link(u))
            out.append(u)

    for u in LINK_RE.findall(" ".join([item.get("title") or "", item.get("details") or ""])):
        # (a site's home page alone is a credit - "metadata provided by
        # https://…" - not a page about this item)
        if "/" in _same_link(u.rstrip(".,;:!?")).strip("/"):
            add(u)
    for plat, handle, where in handles:
        if not plat:
            continue
        fmt = PLATFORMS[plat][1]
        if where.startswith("text") or handle in confirmed:
            add(fmt.format(handle))
            continue
        hit, thing = known.get(handle, (None, None))
        if hit == "studio":
            dom = _same_link(fmt.format("")).split("/")[0]
            mine = [u for u in thing.get("urls") or [] if _same_link(u).startswith(dom)]
            if mine:
                add(mine[0])
            elif HANDLE_RE.match(thing["name"]):
                add(fmt.format(thing["name"]))
        elif hit == "performer" and HANDLE_RE.match(thing["name"]):
            add(fmt.format(thing["name"]))
    return out


def run_item_links(stash, plugin_dir, preview=False, limit=None):
    """Task: every scene and photo gets the links its file name, title,
    description and links show (accounts' profile links, and web links
    written in the title or description) in its URLs. No AI: quick."""
    total = items = 0
    for kind, q, f, mut in (("scene", "findScenes", "scenes", "sceneUpdate(input: $i) { id }"),
                            ("image", "findImages", "images", "imageUpdate(input: $i) { id }")):
        fields = "id title details urls " + ("files { path }" if kind == "scene" else "visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } }")
        page = 1
        while True:
            got = stash.call(f"query($f: FindFilterType) {{ {q}(filter: $f) {{ {f} {{ {fields} }} }} }}",
                             {"f": {"page": page, "per_page": 1000, "sort": "id"}})[q][f]
            for it in got:
                text = " ".join([it.get("title") or "", it.get("details") or "", " ".join(it.get("urls") or []), item_path(kind, it)])
                handles = [(plat, h, "text") for (plat, _), h in handles_in_text(text).items() if plat]
                links = item_links(it, handles, {}, set())
                if not links:
                    continue
                items += 1
                total += len(links)
                name = it.get("title") or os.path.basename(item_path(kind, it)) or f"{kind} {it['id']}"
                log("info", f'{"Would add" if preview else "Added"} {", ".join(links)} to {kind} {it["id"]} "{name}"')
                if not preview:
                    stash.call(f"mutation($i: {'SceneUpdateInput' if kind == 'scene' else 'ImageUpdateInput'}!) {{ {mut} }}",
                               {"i": {"id": it["id"], "urls": list(it.get("urls") or []) + links}})
                if limit and items >= limit:
                    break
            if len(got) < 1000 or (limit and items >= limit):
                break
            page += 1
        if limit and items >= limit:
            break
    log("info", f'{"Preview: " if preview else "Done: "}{total} link(s) {"would be" if preview else ""} added to {items} item(s)'.replace("  ", " "))
    return {"items": items, "links": total}


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
            out.append(grab_frame(stash, f["path"], content_end(stash, f) * at, width=1280)[0])
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
        (None if it isn't scanned yet and compute is off). A scene's pictures
        are its frames, then its markers' pictures (add_markers)."""
        key, sig = f"{kind}:{item['id']}", item_path(kind, item)
        hit = self.new.get(key) or self.data.get(key)
        if hit and hit.get("p") == sig:
            return [[unq8(x) for x in g] for g in hit["f"]]
        if not compute:
            return None
        pics = item_face_pictures(stash, kind, item)
        found = face_helper(cfg, pics) if pics else []
        groups = [[f["emb"] for f in faces] for faces in found]
        entry = {"p": sig, "f": [[q8(e) for e in g] for g in groups]}
        f0 = (item.get("files") or [None])[0] or {}
        dur = content_end(stash, f0) if kind == "scene" and f0.get("path") and f0.get("duration") else 0
        if kind == "scene" and dur and len(groups) == len(FACE_FRAME_POINTS):
            entry["t"] = [round(dur * x) for x in FACE_FRAME_POINTS]  # (when each picture is)
        if kind == "scene":
            entry["n"] = len(groups)  # (frames; markers' pictures follow)
            old = hit or {}
            if old.get("m") and len(old.get("f", [])) > old.get("n", len(old.get("f", []))):
                n0 = old["n"]  # (the file changed: its markers' faces are kept)
                entry["f"] += old["f"][n0:]
                entry["m"] = old["m"]
                if "t" in entry and len(old.get("t", [])) == len(old["f"]):
                    entry["t"] += old["t"][n0:]
        self.new[key] = entry
        if len(self.new) >= 25:
            self.save()
        return groups

    def add_markers(self, stash, cfg, scene, markers):
        """The faces in a scene's markers' pictures, added after its frames
        (each marker's looked at once, again only if its picture changes).
        markers: [{"id", "seconds", "screenshot"}]. Returns how many were
        looked at now."""
        key = f"scene:{scene['id']}"
        entry = self.new.get(key) or self.data.get(key)
        if not entry or entry.get("p") != item_path("scene", scene):
            return 0  # (its frames first: the face scan)
        n = entry.get("n", len(entry["f"]))
        old = {m[0]: (m[1], entry["f"][n + i]) for i, m in enumerate(entry.get("m") or []) if n + i < len(entry["f"])}
        wanted = [m for m in sorted(markers, key=lambda m: float(m["seconds"])) if m.get("screenshot")]
        if not wanted and not old:
            return 0
        todo = [m for m in wanted if old.get(m["id"], (None,))[0] != m["screenshot"]]
        got = {}
        if todo:
            pics, ok = [], []
            for m in todo:
                with contextlib.suppress(Exception):
                    data, ctype = stash.fetch(m["screenshot"])
                    if ctype.startswith("image/"):
                        pics.append(data)
                        ok.append(m)
            for m, faces in zip(ok, face_helper(cfg, pics) if pics else []):
                got[m["id"]] = [q8(f["emb"]) for f in faces]
        new_m, new_f, new_t = [], [], []
        for m in wanted:
            if m["id"] in got:
                faces = got[m["id"]]
            elif m["id"] in old and old[m["id"]][0] == m["screenshot"]:
                faces = old[m["id"]][1]
            else:
                continue  # (its picture couldn't be read)
            new_m.append([m["id"], m["screenshot"]])
            new_f.append(faces)
            new_t.append(round(float(m["seconds"])))
        if new_m == (entry.get("m") or []):
            return 0
        entry = dict(entry, f=entry["f"][:n] + new_f, n=n, m=new_m)
        dur = ((scene.get("files") or [None])[0] or {}).get("duration") or 0
        if len(entry.get("t", [])) < n and n == len(FACE_FRAME_POINTS) and dur:
            entry["t"] = [round(dur * x) for x in FACE_FRAME_POINTS]  # (scanned before times were kept)
        if len(entry.get("t", [])) >= n:
            entry["t"] = entry["t"][:n] + new_t
        else:
            entry.pop("t", None)
        self.new[key] = entry
        if len(self.new) >= 25:
            self.save()
        return len(todo)

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


def face_targets(stash, cfg):
    """The performers faces are suggested and added for (settings "Faces:
    only favourite performers" / "...at least this many scenes and images"):
    a set of ids, or None for everyone. Faces are still compared with every
    known performer, so a look-alike who isn't chosen still counts as
    "someone else" - it's only who gets suggested or added that's limited."""
    favs, least = cfg["ai"]["face_only_favs"], cfg["ai"]["face_min_items"]
    if not favs and not least:
        return None
    flt = {"filter_favorites": True} if favs else {}
    ps = stash.call("query($f: PerformerFilterType) { findPerformers(performer_filter: $f, filter: {per_page: -1}) "
                    "{ performers { id scene_count image_count } } }", {"f": flt})["findPerformers"]["performers"]
    return {p["id"] for p in ps if (p.get("scene_count") or 0) + (p.get("image_count") or 0) >= least}


def face_targets_text(cfg):
    """What the face settings limit suggestions to, in words ("" for everyone)."""
    favs, least = cfg["ai"]["face_only_favs"], cfg["ai"]["face_min_items"]
    if not favs and not least:
        return ""
    who = "favourite performers" if favs else "performers"
    return who + (f" in at least {least} scenes and images" if least else "")


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
        raise NotReady('not enough look-alikes yet - run "Tags: scan scenes and images for look-alikes (do this first)" first')
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
    _replace(tmp, path)
    return found, names


REVIEW_LOOKS_WEIGHT = 0.3  # how much a photo's aesthetic score moves it up or down Review's suggestions


TAG_REVIEW_FIELDS = {
    "scene": "id title tags { id } files { basename } paths { screenshot }",
    "image": "id title tags { id } visual_files { ... on BaseFile { basename } } paths { image }",
}


def run_tag_review(stash, plugin_dir, tag=None, limit=40, skip=(), fresh=False):
    """Tag review page: confident look-alike tags (the ones the tasks would
    add, 50%+ of the closest look-alikes' vote) that items don't have yet and
    you haven't turned down. Body tags are left out (the body detector does
    those), as are "Top tags: tags to ignore", gallery-name tags and tags you've
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
        if str(t) in removed_tags(*key.split(":"), have.get(key, ())):
            continue  # (you took it off this item)
        picks.append((sc, n, key, t))
    # positions seen in the video (Auto Seek's model), when that scan was run:
    # one card when look-alikes suggest the same tag, else a card of their own
    video = {}
    try:
        for key, t, vinfo in _video_picks(stash, plugin_dir, have, nots, skip, want):
            if t["id"] in excluded:
                continue
            names.setdefault(t["id"], t["name"])
            video[(key, t["id"])] = vinfo
    except Exception as e:  # (no Auto Seek, or its model isn't there: look-alikes only)
        log("debug", f"positions in videos left out: {e}")
    # order: both agree first, then look-alikes with every 5th card from the
    # video (look-alike scores run higher, so sorting them together would
    # bury the video ones)
    both, looks_only = [], []
    for sc, n, key, t in picks:
        v = video.pop((key, t), None)
        (both if v else looks_only).append((sc, n, key, t, v))
    # what a scene's markers look like: evidence for its suggestions (the
    # marker's picture and time), and suggestions of their own
    marker_only = []
    for sid, tags in marker_scene_tags(stash, cfg, plugin_dir, excluded).items():
        key = f"scene:{sid}"
        for t, e in tags.items():
            if (want and t != want) or t in excluded or names.get(t, "").strip().lower() in BODY_TAG_NAMES:
                continue
            if t in have.get(key, ()) or t in nots.get(key, ()) or f"{key}:{t}" in skip:
                continue
            if str(t) in removed_tags("scene", sid, have.get(key, ())):
                continue
            for lst in (both, looks_only):
                hit = next((i for i, x in enumerate(lst) if x[2] == key and x[3] == t), None)
                if hit is not None:
                    sc0, n0, k0, t0, v0 = lst[hit]
                    lst[hit] = (sc0, n0, k0, t0, dict(v0 or {}, marker=e))
                    break
            else:
                if (key, t) not in video:
                    marker_only.append((e["score"], e["near"], key, t, {"marker": e}))
    # (seen in more of its markers first, then how sure)
    marker_only.sort(key=lambda x: -(x[0] + 0.05 * min(x[4]["marker"]["count"] - 1, 6)))
    # better-looking photos first (their aesthetic score, when known), within
    # how sure the suggestion is; screenshot-like ones last
    facts = picture_facts(plugin_dir)

    def looks(key):
        kind, _, iid = key.partition(":")
        f = facts.get(iid) if kind == "image" else None
        if not f:
            return 0.0
        return -0.5 if f.get("u") else REVIEW_LOOKS_WEIGHT * ((f.get("a") if f.get("a") is not None else 50) / 100 - 0.5)
    both.sort(key=lambda x: (-(x[0] + looks(x[2])), -x[1]))
    looks_only.sort(key=lambda x: (-(x[0] + looks(x[2])), -x[1]))
    video_only = sorted(((v["rank"], 0, key, t, v) for (key, t), v in video.items()), key=lambda x: -x[0])
    # body tags just under what the body detector needs, for items with few
    # tags (kept by the body tags task): fewest tags first
    near_only = []
    with contextlib.suppress(OSError, ValueError):
        with open(os.path.join(plugin_dir, "body_near.json")) as fh:
            near_saved = json.load(fh)
        taken = {(k, t) for _, _, k, t, _ in both + looks_only + video_only + marker_only}
        for key, entries in near_saved.items():
            tags_now = have.get(key, set())
            if len(tags_now) > BODY_NEAR_MAX_TAGS:
                continue
            for t, why in entries:
                if (want and t != want) or t in excluded or t in tags_now or t in nots.get(key, ()) \
                        or f"{key}:{t}" in skip or (key, t) in taken:
                    continue
                near_only.append((0.0, -len(tags_now), key, t, {"body": f"just under the body detector's bar: {why}"}))
        near_only.sort(key=lambda x: (-x[1], -looks(x[2])))
        missing = {t for _, _, _, t, _ in near_only if t not in names}
        if missing:
            for tg in stash.call("query($i: [ID!]) { findTags(ids: $i, filter: {per_page: -1}) { tags { id name } } }", {"i": sorted(missing)})["findTags"]["tags"]:
                names[tg["id"]] = tg["name"]
    # poses learnt from the photos you've tagged with one (see "poses" below)
    taken = {(k, t) for _, _, k, t, _ in both + looks_only + video_only + near_only + marker_only}
    pose_only = []
    pose_found = pose_suggestions(stash, cfg, plugin_dir, excluded, fresh, kind="image")
    pose_names = pose_tags(stash) if pose_found else {}
    for key, t, sc, why in pose_found:
        if (want and t != want) or t in have.get(key, ()) or t in nots.get(key, ()) or f"{key}:{t}" in skip or (key, t) in taken:
            continue
        names.setdefault(t, pose_names.get(t, t))
        pose_only.append((sc, 0, key, t, {"body": why}))
    pose_only.sort(key=lambda x: -(x[0] + looks(x[2])))
    picks = list(both)
    while looks_only or video_only or near_only or pose_only or marker_only:
        picks += looks_only[:4]
        del looks_only[:4]
        picks += marker_only[:2]
        del marker_only[:2]
        picks += video_only[:1]
        del video_only[:1]
        picks += near_only[:1]
        del near_only[:1]
        picks += pose_only[:1]
        del pose_only[:1]
    # a scene's markers first: when one of its markers is suggested the same
    # tag, the card is that marker's (a clearer idea of what's going on, and
    # tagging the marker tags the scene too - marker_tags_to_scene)
    by_marker = {}
    if not plugin_settings(stash, plugin_dir, AI_ID).get("markerTagsStayOnMarker"):
        with contextlib.suppress(Exception):
            for mi in run_marker_tag_review(stash, plugin_dir, tag=want, limit=10 ** 6, skip=skip)["items"]:
                k = (mi["scene"], mi["tag"])
                if k not in by_marker or mi["score"] > by_marker[k]["score"]:
                    by_marker[k] = mi
    out = []
    for at in range(0, len(picks), 150):
        chunk = picks[at:at + 150]
        items = {}
        for kind, query, field in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            ids = sorted({k.split(":")[1] for _, _, k, _, _ in chunk if k.startswith(kind + ":")}, key=int)
            if ids:
                got = find_by_ids(stash, query, field, ids, f"{TAG_REVIEW_FIELDS[kind]}")
                items.update({f"{kind}:{x['id']}": x for x in got})
        for sc, n, key, t, v in chunk:
            it = items.get(key)
            if not it or t in {x["id"] for x in it.get("tags") or []}:
                continue
            kind, iid = key.split(":")
            mi = by_marker.get((iid, t)) if kind == "scene" else None
            if mi:
                note = f"the marker at {_marker_clock(mi['start'])} - tagging it tags the scene too"
                out.append({**mi, "for_scene": True, "scene_title": it.get("title") or "",
                            "why": " · ".join(x for x in (mi.get("why"), note) if x)})
                if len(out) >= limit:
                    break
                continue
            files = it.get("files") or it.get("visual_files") or [{}]
            link = (it.get("paths") or {}).get("screenshot" if kind == "scene" else "image") or ""
            mk = (v or {}).get("marker")
            if mk and mk.get("picture"):
                link = mk["picture"]  # (the marker that looks like it: better evidence than the cover)
            out.append({
                "kind": kind, "id": iid, "tag": t, "name": names.get(t, ""), "score": round(sc * 100), "near": n,
                "title": it.get("title") or (files[0] or {}).get("basename") or "",
                # the page is opened from the phone too: no localhost address
                "picture": re.sub(r"^https?://[^/]+", "", link), "tags": len(it.get("tags") or []),
                **({"frames": v["frames"], "looked": v["looked"], "times": v["times"]} if v and "frames" in v else {}),
                **({"why": v["body"]} if v and "body" in v else {}),
                **({"times": mk["times"][:6], "markers": mk["count"],
                    "why": (f"its marker at {_marker_clock(mk['times'][0])} looks like it" if mk["count"] == 1
                            else f"seen in {mk['count']} of its markers")} if mk and not (v and "frames" in v) else {}),
                **({"aesthetic": facts[iid]["a"]} if kind == "image" and (facts.get(iid) or {}).get("a") is not None else {}),
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # the Batch view: how many there are for each tag, biggest first
    per = Counter(t for _, _, _, t, _ in picks)
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


def face_cluster(cfg, faces, threshold=NEW_PERF_SAME, dim=128, center=False):
    body = json.dumps({"faces": faces, "threshold": threshold, "dim": dim, "center": center}).encode()
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
            raise NotReady('nothing is scanned yet - run "Faces: scan scenes and images" first')

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
        _replace(tmp, path)

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
        studio_names = {x["studio"]["id"]: x["studio"]["name"] for x in xs if x.get("studio")}
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
                    pics = [grab_frame(stash, f["path"], content_end(stash, f) * at, width=1280)[0] for at in (0.15, 0.6)]
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
    _replace(tmp, path)
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


def run_face_review(stash, plugin_dir, performer=None, limit=40, skip=(), favourites=False):
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
        raise NotReady('nothing is scanned yet - run "Faces: scan scenes and images" first')
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
    targets = face_targets(stash, cfg)  # (the Faces settings: favourites / at least N items)
    favs = None
    if favourites:
        favs = {p["id"] for p in stash.call("{ findPerformers(performer_filter: {filter_favorites: true}, filter: {per_page: -1}) "
                                            "{ performers { id } } }")["findPerformers"]["performers"]}
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
        if favs is not None and pid not in favs:  # ("Favourites only": faces are still compared with everyone)
            continue
        if targets is not None and pid not in targets:
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
            times, best_at, marker_pic = [], None, None
            dur = (files[0] or {}).get("duration") or 0
            ent = cache.data.get(f"{kind}:{iid}") or {}
            if kind == "scene" and len(ent.get("t", [])) == len(ent.get("f", [])) and ent.get("t"):
                times = sorted({ent["t"][g] for g in h["pics"]})
                best_at = ent["t"][h["best_g"]]
                n0 = ent.get("n", len(ent["f"]))
                if h["best_g"] >= n0 and h["best_g"] - n0 < len(ent.get("m") or []):
                    marker_pic = ent["m"][h["best_g"] - n0][1]  # (seen best in a marker: show its picture)
            elif kind == "scene" and dur and groups_of.get(f"{kind}:{iid}") == len(FACE_FRAME_POINTS):
                times = [round(dur * FACE_FRAME_POINTS[g]) for g in sorted(h["pics"])]
                best_at = round(dur * FACE_FRAME_POINTS[h["best_g"]])
            out.append({
                "times": times, "best_at": best_at,
                "kind": kind, "id": iid, "performer": pid, "name": (index.get(pid) or {}).get("name", ""),
                "score": round(score * 100), "rival": round(h["rival"] * 100), "pictures": len(h["pics"]),
                "emb": h["emb"], "title": it.get("title") or (files[0] or {}).get("basename") or "",
                "picture": (re.sub(r"^https?://[^/]+", "", marker_pic) if marker_pic else
                            (it.get("paths") or {}).get("screenshot")) if kind == "scene" else (it.get("paths") or {}).get("image"),
                "others": len(it.get("performers") or []),
            })
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # the Batch view: how many there are for each performer, biggest first
    per = Counter(pid for _, _, _, pid, _ in strong)
    groups = [{"id": pid, "name": (index.get(pid) or {}).get("name", pid), "count": c} for pid, c in per.most_common()]
    limited = face_targets_text(cfg)
    return {"items": out, "total": len(strong), "groups": groups,
            **({"note": f"Only {limited} (Faces settings)."} if limited else {})}


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
    matches are added when "New items: add confident face matches" is
    on; otherwise (or in a preview) they're only written to the log."""
    cfg = settings(stash, plugin_dir)
    index = update_face_index(stash, cfg, plugin_dir, quiet=ids is not None)
    if not any(known_embs(v) for v in index.values()):
        log("warning", "No performer faces known yet - performers need a profile picture with a face in it.")
        return
    apply = cfg["ai"]["face_auto"] and not preview
    targets = face_targets(stash, cfg)  # (only these are added; faces are compared with everyone)
    if targets is not None and ids is None:
        log("info", f"Faces: only {face_targets_text(cfg)} are added ({len(targets)} of them) - see the Faces settings.")
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
               " - only writing suggestions to the log, as \"New items: add confident face matches\" is off"))
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
        sure = [c for c in cands if confident(cfg, kind, c, cands) and (targets is None or c["id"] in targets)]
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
        progress(0.8 * n / max(len(left), 1) if "scene" in kinds else n / max(len(left), 1))
        try:
            faces += sum(len(g) for g in cache.groups(stash, cfg, kind, item))
        except RuntimeError:
            cache.save()
            raise
        except Exception as e:
            log("debug", f"{kind} {item['id']}: {e}")
    cache.save()
    if "scene" in kinds:
        # markers' pictures: a clear frame of every part of a scene
        by_scene = defaultdict(list)
        for m in stash.call("{ findSceneMarkers(filter: {per_page: -1}) { scene_markers { id seconds screenshot scene { id } } } }")[
                "findSceneMarkers"]["scene_markers"]:
            by_scene[m["scene"]["id"]].append(m)
        scenes = {i["id"]: i for k, i in todo if k == "scene" and i["id"] in by_scene}
        looked = 0
        for n, (sid, item) in enumerate(scenes.items()):
            progress(0.8 + 0.2 * n / max(len(scenes), 1))
            try:
                looked += cache.add_markers(stash, cfg, item, by_scene[sid])
            except RuntimeError:
                cache.save()
                raise
            except Exception as e:
                log("debug", f"scene {sid} markers: {e}")
        cache.save()
        if looked:
            log("info", f"Looked at {looked} marker picture(s) for faces.")
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
        raise NotReady('nothing is scanned yet - run "Faces: scan scenes and images" first')
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
                raise NotReady("give it a name first")
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
        raise NotReady('no photos have look-alike fingerprints yet - run "Tags: scan scenes and images for look-alikes (do this first)" first')
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
    _replace(tmp, path)
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
    _replace(tmp, path)
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
            raise NotReady("give it a name first")
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


# ---------------------------------------------------------------------------
# Review page, Tags → New: groups of pictures that look alike and that no
# tag covers yet - to make into a tag, or add to one you have. Tags →
# Similar: pairs of tags that are on the same items (or are named alike), to
# merge or to make one a sub-tag of the other.

TAG_GROUP_SAME = 0.6      # pictures this alike (fingerprints less the library's average) are grouped
TAG_GROUP_MIN = 6         # items a group needs
TAG_GROUP_COVERED = 0.6   # a tag on this share of a group already covers it
TAG_GROUP_ONE_PLACE = 0.7 # a group mostly from one gallery / folder is that gallery, not a tag
TAG_PAIR_SUB_SHARE = 0.3  # a tag named under another ("Solo Female" / "Solo") and with it this often: a sub-tag
TAG_PAIR_SAME = 0.8       # items in common / items with either: the same tag twice
TAG_PAIR_MIN = 4          # items the smaller tag needs
# words and phrases that mean the same in tag names (first = the one used);
# a tag's own aliases count as well. "Big Boobs" then sits under "Tits",
# "Doggystyle POV" under "Doggy Style", "Point of View Blow Job" under "Blowjob".
TAG_SYNONYMS = [
    ["doggy", "doggy style", "doggystyle", "doggie style", "doggiestyle", "doggie"],
    ["blowjob", "blow job", "bj", "blowie", "sucking cock", "cocksucking"],
    ["cumshot", "cum shot", "cum shots"],
    ["tits", "boobs", "breasts", "breast", "boob", "titties", "tit", "titty"],
    ["ass", "butt", "booty", "bum", "arse"],
    ["pussy", "vagina", "vulva"],
    ["cock", "dick", "penis"],
    ["pov", "point of view", "first person"],
    ["lesbian", "girl on girl", "girlgirl", "gg", "lesbians"],
    ["threesome", "3some", "three some"],
    ["foursome", "4some"],
    ["masturbation", "masturbating", "masturbate"],
    ["handjob", "hand job", "hj"],
    ["footjob", "foot job"],
    ["feet", "foot"],
    ["creampie", "cream pie"],
    ["outdoor", "outdoors", "outside"],
    ["toy", "toys", "sex toy", "sex toys"],
    ["stepmom", "step mom", "step mother", "stepmother"],
    ["stepsister", "step sister", "step sis", "stepsis"],
    ["stepdad", "step dad", "step father", "stepfather"],
    ["stepbrother", "step brother", "step bro", "stepbro"],
    ["tattoo", "tattoos", "tattooed", "inked"],
    ["piercing", "piercings", "pierced"],
    ["stockings", "thigh highs", "thighhighs", "nylons"],
    ["cunnilingus", "pussy licking", "eating pussy", "licking pussy"],
    ["interracial", "ir"],
    ["trans", "transgender", "ts", "tgirl", "t girl"],
    ["selfie", "selfies"],
    ["69", "sixty nine", "sixtynine"],
    ["deepthroat", "deep throat", "deepthroating"],
    ["facial", "facials", "cum on face"],
    ["bondage", "tied up", "restrained", "rope"],
    ["femdom", "female domination", "female dom"],
    ["cowgirl", "riding"],
    ["oral", "oral sex"],
]
# narrower -> broader: a tag about the first is a kind of the second
TAG_BROADER = {"blowjob": "oral", "cunnilingus": "oral", "rimming": "oral", "69": "oral", "face fuck": "oral",
               "deepthroat": "blowjob", "titjob": "tits", "titty fuck": "tits"}


def tag_word_rules(text):
    """The "Tags: synonyms" setting: lines (or ;) like "Oral = Blowjob = BJ"
    (the same thing) or "Blowjob < Oral" (a kind of). ([groups], {narrow: broad})."""
    groups, broader = [], {}
    for line in re.split(r"[;\n]", text or ""):
        line = line.strip().lower()
        if "<" in line:
            a, b = [x.strip() for x in line.split("<", 1)]
            if a and b:
                broader[a] = b
        elif ">" in line:
            a, b = [x.strip() for x in line.split(">", 1)]
            if a and b:
                broader[b] = a
        elif "=" in line:
            g = [x.strip() for x in line.split("=") if x.strip()]
            if len(g) > 1:
                groups.append(g)
    return groups, broader


def locked_tag_tidy(plugin_dir):
    return locked_json(os.path.join(plugin_dir, "tag_tidy.json"))


def _tidy_items(stash):
    """{key: item} for every scene and image, with what a group card shows."""
    items = {}
    for kind, q, f, fields in (("scene", "findScenes", "scenes", "id title tags { id name } galleries { id } files { path } paths { screenshot }"),
                               ("image", "findImages", "images", "id title tags { id name } galleries { id } visual_files { ... on BaseFile { path } } paths { thumbnail }")):
        for x in stash.call(f"{{ {q}(filter: {{per_page: -1}}) {{ {f} {{ {fields} }} }} }}")[q][f]:
            items[f"{kind}:{x['id']}"] = x
    return items


def _tidy_pic(key, x):
    kind, iid = key.split(":")
    path = (((x.get("files") or x.get("visual_files") or [{}])[0]) or {}).get("path") or ""
    link = (x.get("paths") or {}).get("screenshot" if kind == "scene" else "thumbnail") or ""
    return {"key": key, "kind": kind, "id": iid, "title": x.get("title") or os.path.basename(path),
            "picture": re.sub(r"^https?://[^/]+", "", link)}


def run_tag_groups(stash, plugin_dir, limit=30, skip=(), fresh=False):
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    excluded = set(excluded) | gallery_name_tag_ids(plugin_dir)
    path = os.path.join(plugin_dir, "tag_groups_cache.json")
    found = None
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                found = saved["found"]
    if found is None:
        cache = LookCache(plugin_dir)
        keys = [k for k, v in cache.data.items() if v.get("e")]
        if not keys:
            raise NotReady('nothing is scanned yet - run "Tags: scan scenes and images for look-alikes (do this first)" first')
        try:
            res = face_cluster(cfg, [cache.data[k]["e"] for k in keys], threshold=TAG_GROUP_SAME, dim=512, center=True)
        except RuntimeError:
            raise
        if len(res) != len(keys):
            raise RuntimeError("the face helper is too old to group pictures - run face-helper/setup.sh again (see the README)")
        groups = defaultdict(list)
        for k, (g, sim) in zip(keys, res):
            if g >= 0:
                groups[g].append((sim, k))
        found = [[k for _, k in sorted(m, reverse=True)] for m in groups.values() if len(m) >= TAG_GROUP_MIN]
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"at": time.time(), "found": found}, f)
        _replace(tmp, path)
    items = _tidy_items(stash)
    tag_names = {t["id"]: t["name"] for x in items.values() for t in x.get("tags") or []}
    with locked_tag_tidy(plugin_dir) as d:
        skip = set(skip or ()) | set(d.get("no", []))
    out = []
    for keys in found:
        keys = [k for k in keys if k in items]
        if len(keys) < TAG_GROUP_MIN:
            continue
        sig = "tg:" + hashlib.sha1("|".join(sorted(keys)[:20]).encode()).hexdigest()[:12]
        if sig in skip:
            continue
        n = len(keys)
        places = Counter()
        for k in keys:
            x = items[k]
            gs = [g["id"] for g in x.get("galleries") or []]
            p = (((x.get("files") or x.get("visual_files") or [{}])[0]) or {}).get("path") or ""
            places["g" + gs[0] if gs else "f" + os.path.dirname(p)] += 1
        if places.most_common(1)[0][1] >= TAG_GROUP_ONE_PLACE * n:
            continue  # one gallery / folder: it's that gallery, not a new tag
        tc = Counter(t["id"] for k in keys for t in items[k].get("tags") or []
                     if t["id"] not in excluded and t["name"].strip().lower() not in BODY_TAG_NAMES)
        if tc and tc.most_common(1)[0][1] >= TAG_GROUP_COVERED * n:
            continue  # a tag you have already says what these are
        near = [{"id": t, "name": tag_names[t], "count": c} for t, c in tc.most_common(8) if c >= 2]
        untagged = sum(1 for k in keys if not items[k].get("tags"))
        reasons = [f"{n} scenes and photos that look alike", f"{untagged} have no tags" if untagged else "no tag is on most of them"]
        out.append({"sig": sig, "tg": True, "type": "tagnew", "keys": keys, "count": n, "near": near,
                    "samples": [_tidy_pic(k, items[k]) for k in keys[:6]], "all": [_tidy_pic(k, items[k]) for k in keys],
                    "reasons": reasons})
    out.sort(key=lambda x: -x["count"])
    return {"items": out[:limit], "total": len(out)}


def run_tag_pairs(stash, plugin_dir, limit=30, skip=()):
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    excluded = set(excluded)  # (gallery-only tags are in it; a gallery's name tag that's used everywhere, like "Tits", counts)
    tags = {t["id"]: t for t in stash.call("{ findTags(filter: {per_page: -1}) { tags { id name aliases image_path "
                                           "scene_count image_count parents { id } children { id } } } }")["findTags"]["tags"]}
    # where each tag is, counting a gallery as one place (a 1,000-page comic
    # whose pages all carry the same tags is one place, not 1,000 items)
    gal_of = {f"image:{x['id']}": "g" + x["galleries"][0]["id"] for x in stash.call(
        "{ findImages(image_filter: {galleries: {modifier: NOT_NULL}}, filter: {per_page: -1}) { images { id galleries { id } } } }"
    )["findImages"]["images"] if x.get("galleries")}
    have = defaultdict(set)
    for k, ts in all_item_tags(stash).items():
        for t in ts:
            have[t].add(gal_of.get(k, k))
    with locked_tag_tidy(plugin_dir) as d:
        skip = set(skip or ()) | set(d.get("no", []))
    norm = lambda n: re.sub(r"s$", "", re.sub(r"[^a-z0-9]", "", (n or "").lower()))
    ids = [t for t in tags if t not in excluded and len(have.get(t, ())) >= 1]
    by_item = defaultdict(set)
    for t in ids:
        for k in have[t]:
            by_item[k].add(t)
    both = Counter()
    for k, ts in by_item.items():
        if len(ts) > 60:
            continue
        ts = sorted(ts)
        for i, a in enumerate(ts):
            for b in ts[i + 1:]:
                both[(a, b)] += 1
    out, seen = [], set()

    def card(t):
        t = tags[t]
        n = (t.get("scene_count") or 0) + (t.get("image_count") or 0)
        return {"id": t["id"], "name": t["name"], "count": n, "aliases": t.get("aliases") or [],
                "picture": f"/tag/{t['id']}/image"}

    raw = (stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {}).get(AI_ID) or {}
    mine, mine_broader = tag_word_rules(raw.get("tagSynonyms"))
    syn = {}
    for group in TAG_SYNONYMS:
        for w in group:
            syn[w] = group[0]
    for group in mine:  # yours win: they can join or override the built-in ones
        head = syn.get(group[0], group[0])
        for w in group:
            syn[w] = head
    broader = {syn.get(k, k): syn.get(v, v) for k, v in {**TAG_BROADER, **mine_broader}.items()}
    phrases = sorted((w for w in syn if " " in w), key=len, reverse=True)

    def seq(text):
        """A name as its meanings in order: synonyms and plurals made one."""
        t = " " + re.sub(r"[^a-z0-9+]+", " ", (text or "").lower()) + " "
        for ph in phrases:
            t = t.replace(" " + ph + " ", " " + syn[ph].replace(" ", "_") + " ")
        out = []
        for w in t.split():
            w = w.replace("_", " ")
            w = syn.get(w, w)
            if w not in syn.values() and len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
                w = syn.get(w[:-1], w[:-1])
            out.append(w)
        return tuple(out)

    def base(text):
        """Without its qualifier: "Brown Hair (Female)", "Doggy Style - Standing" -> the first part."""
        return re.split(r"\s+-\s+|\s*\(", text or "")[0].strip()

    names_of = lambda t: [n for n in [tags[t]["name"]] + (tags[t].get("aliases") or []) if n]
    spellings = {t: {frozenset(seq(n)) for n in names_of(t)} for t in tags}
    seqs = {t: {seq(n) for n in names_of(t)} for t in tags}
    bases = {t: {seq(base(n)) for n in names_of(t)} for t in tags}
    DIRECTION = {"on", "by", "to", "fucks", "fucked", "into", "over"}
    NOT_KIND = DIRECTION | {"no", "non", "not", "without", "and", "&", "or", "vs", "with"}

    def named_under(child, parent):
        """"Solo Female" under "Solo", "Big Boobs" under "Tits": every word of
        the parent's name (or an alias of it, or a synonym) is in the child's."""
        return any(pw and pw < cw for pw in spellings[parent] for cw in spellings[child])

    def wider(word, steps=3):
        out = []
        while word in broader and steps:
            word = broader[word]
            out.append(word)
            steps -= 1
        return out

    def broadly(child, parent):
        """Blowjob is a kind of Oral (the built-in list and your setting)."""
        for ps in seqs[parent]:
            if len(ps) != 1:
                continue
            for cs in bases[child]:
                if cs and not set(cs) & NOT_KIND and ps[0] in wider(cs[-1]):
                    return True
        return False

    def kind_of(child, parent):
        """The parent is what the child is a kind of: the end of its name
        ("Standing Missionary", "Big Nipples") or its name before a
        qualifier ("Brown Hair (Female)", "Doggy Style - Standing")."""
        if broadly(child, parent):
            return True
        for ps in seqs[parent]:
            if not ps:
                continue
            for cs in bases[child]:  # (the words in a qualifier don't count)
                if set(cs) & NOT_KIND:
                    continue  # "No Bra", "Black and White", "White on Black"
                if len(cs) > len(ps) and cs[-len(ps):] == ps:
                    return True
            for cb in bases[child]:
                if cb == ps and cb not in seqs[child]:
                    return True
        return False

    def related(a, b):
        return b in {p["id"] for p in tags[a].get("parents") or []} or a in {p["id"] for p in tags[b].get("parents") or []}

    # named alike: the same tag written two ways (Outdoor / Outdoors)
    groups = defaultdict(list)
    for t in ids:
        groups[norm(tags[t]["name"])].append(t)
    # ... and the same meaning in other words (synonyms, aliases)
    by_meaning = defaultdict(set)
    for t in ids:
        for sp in spellings[t]:
            if sp:
                by_meaning[sp].add(t)
    for sp, ts in by_meaning.items():
        if sp & DIRECTION:
            continue  # word order matters ("Black on White" isn't "White on Black")
        ts = [t for t in ts if not any(t in g for g in groups.values() if len(g) > 1)]
        if len(ts) > 1:
            groups[" ".join(sorted(sp)) + "#syn"] = sorted(ts)
    for key, ts in groups.items():
        if len(ts) < 2 or not key:
            continue
        ts.sort(key=lambda t: -len(have[t]))
        for b in ts[1:]:
            a = ts[0]
            pair = tuple(sorted((a, b)))
            sig = "tm:" + ":".join(pair)
            if sig in skip or pair in seen:
                continue
            seen.add(pair)
            out.append({"sig": sig, "tp": True, "type": "tagmerge", "tags": [card(a), card(b)], "keep": a,
                        "count": len(have[a] | have[b]),
                        "reasons": ["The same thing in other words" if key.endswith("#syn") else "The same name written differently"]})
    # on the same items
    for (a, b), c in both.items():
        pair = (a, b)
        if pair in seen or related(a, b):
            continue
        na, nb = len(have[a]), len(have[b])
        small, big = (a, b) if na <= nb else (b, a)
        ns = min(na, nb)
        if ns < TAG_PAIR_MIN:
            continue
        union = na + nb - c
        if c / union >= TAG_PAIR_SAME and ns >= 6:
            sig = "tm:" + ":".join(pair)
            if sig in skip:
                continue
            seen.add(pair)
            out.append({"sig": sig, "tp": True, "type": "tagmerge", "tags": [card(big), card(small)], "keep": big,
                        "count": union, "reasons": [f"Almost always together: {c} of the {union} places (galleries, scenes, loose photos) with either have both"]})
        elif kind_of(small, big) and c / ns >= TAG_PAIR_SUB_SHARE:
            sig = f"ts:{small}:{big}"
            if sig in skip:
                continue
            seen.add(pair)
            out.append({"sig": sig, "tp": True, "type": "tagsub", "child": card(small), "parent": card(big),
                        "count": ns, "reasons": [f"{c} of the {ns} places (galleries, scenes, loose photos) with {tags[small]['name']} also have {tags[big]['name']}"]})
    # named under another (synonyms included) - even when they're seldom on
    # the same items, the name says it ("Big Boobs" is a kind of "Tits")
    by_word = defaultdict(set)
    for t in ids:
        for sp in spellings[t]:
            for w in sp:
                by_word[w].add(t)
    for child in ids:
        if tags[child].get("parents"):
            continue
        cands = set()
        for sp in spellings[child]:
            for w in sp:
                cands |= by_word[w]
                for b in wider(w):
                    cands |= by_word[b]
        best = None
        for parent in cands:
            if parent == child or tuple(sorted((child, parent))) in seen or related(child, parent):
                continue
            if not kind_of(child, parent):
                continue
            if len(have[parent]) < len(have[child]) and not broadly(child, parent):
                continue
            # the most specific parent ("Big Tits" over "Tits" for "Big Natural Tits")
            size = max(len(pw) for pw in spellings[parent])
            if not best or size > best[0] or (size == best[0] and len(have[parent]) > len(have[best[1]])):
                best = (size, parent)
        if not best:
            continue
        parent = best[1]
        pair = tuple(sorted((child, parent)))
        sig = f"ts:{child}:{parent}"
        if sig in skip:
            continue
        seen.add(pair)
        c = both.get(pair, 0)
        out.append({"sig": sig, "tp": True, "type": "tagsub", "child": card(child), "parent": card(parent), "count": len(have[child]),
                    "reasons": [(f'"{tags[parent]["name"]}" is the broader term' if broadly(child, parent) else
                                 f'"{tags[child]["name"]}" is a kind of "{tags[parent]["name"]}"')
                                + (f" - {c} of its {len(have[child])} places also have it" if c else "")]})
    order = {"tagmerge": 0, "tagsub": 1}
    out.sort(key=lambda x: (order[x["type"]], -x["count"]))
    return {"items": out[:limit], "total": len(out)}


TAG_SNAP_FIELDS = "id name sort_name description aliases favorite ignore_auto_tag image_path parents { id } children { id }"


def _tag_items(stash, tid):
    """Everything with the tag: {"scene": ids, "image": ids, "gallery": ids, "performer": ids, "studio": ids}."""
    out = {}
    flt = {"tags": {"value": [str(tid)], "modifier": "INCLUDES", "depth": 0}}
    for kind, q, f, typ, arg in (("scene", "findScenes", "scenes", "SceneFilterType", "scene_filter"),
                                 ("image", "findImages", "images", "ImageFilterType", "image_filter"),
                                 ("gallery", "findGalleries", "galleries", "GalleryFilterType", "gallery_filter"),
                                 ("performer", "findPerformers", "performers", "PerformerFilterType", "performer_filter"),
                                 ("studio", "findStudios", "studios", "StudioFilterType", "studio_filter")):
        out[kind] = [x["id"] for x in stash.call(
            f"query($f: {typ}) {{ {q}({arg}: $f, filter: {{per_page: -1}}) {{ {f} {{ id }} }} }}", {"f": flt})[q][f]]
    return out


BULK_TAG = {"scene": ("bulkSceneUpdate", "BulkSceneUpdateInput"), "image": ("bulkImageUpdate", "BulkImageUpdateInput"),
            "gallery": ("bulkGalleryUpdate", "BulkGalleryUpdateInput"), "performer": ("bulkPerformerUpdate", "BulkPerformerUpdateInput"),
            "studio": ("bulkStudioUpdate", "BulkStudioUpdateInput")}


def _tag_on(stash, kind_ids, tid, mode):
    for kind, ids in kind_ids.items():
        if not ids:
            continue
        mut, typ = BULK_TAG[kind]
        for at in range(0, len(ids), 500):
            stash.call(f"mutation($i: {typ}!) {{ {mut}(input: $i) {{ id }} }}",
                       {"i": {"ids": [str(i) for i in ids[at:at + 500]], "tag_ids": {"ids": [str(tid)], "mode": mode}}})


def run_tag_tidy_apply(stash, plugin_dir, action, data):
    data = data or {}
    if action == "no":
        with locked_tag_tidy(plugin_dir) as d:
            d["no"] = sorted(set(d.get("no", [])) | {data["sig"]})
        return {"action": "no", "sig": data["sig"]}
    if action in ("tagnew", "tagjoin"):
        kinds = _keys_by_kind(data.get("keys"))
        made = None
        if action == "tagnew":
            name = (data.get("name") or "").strip()
            if not name:
                raise NotReady("give the tag a name first")
            got = stash.call("query($n: String!) { findTags(tag_filter: {name: {value: $n, modifier: EQUALS}}) { tags { id } } }",
                             {"n": name})["findTags"]["tags"]
            if got:
                tid = got[0]["id"]
            else:
                tid = made = stash.call("mutation($i: TagCreateInput!) { tagCreate(input: $i) { id } }", {"i": {"name": name}})["tagCreate"]["id"]
        else:
            tid = str(data["tag"])
        # only the ones without it yet (so Undo takes off only what this added)
        added = {}
        for kind, q, f in (("scene", "findScenes", "scenes"), ("image", "findImages", "images")):
            got = find_by_ids(stash, q, f, kinds[kind], "id tags { id }") if kinds[kind] else []
            added[kind] = [x["id"] for x in got if tid not in {t["id"] for t in x.get("tags") or []}]
        _tag_on(stash, added, tid, "ADD")
        return {"action": action, "tag": tid, "added": added, "made": made}
    if action == "tagsub":
        child = stash.call(f"query($id: ID!) {{ findTag(id: $id) {{ {TAG_SNAP_FIELDS} }} }}", {"id": str(data["child"])})["findTag"]
        before = [p["id"] for p in child.get("parents") or []]
        stash.call("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }",
                   {"i": {"id": child["id"], "parent_ids": sorted(set(before) | {str(data["parent"])})}})
        return {"action": "tagsub", "child": child["id"], "before": before}
    if action == "tagmerge":
        keep = str(data["keep"])
        snaps = []
        for sid in data.get("others") or []:
            sid = str(sid)
            if sid == keep:
                continue
            t = stash.call(f"query($id: ID!) {{ findTag(id: $id) {{ {TAG_SNAP_FIELDS} }} }}", {"id": sid})["findTag"]
            if not t:
                continue
            snap = {"tag": t, "items": _tag_items(stash, sid)}
            if "default=true" not in (t.get("image_path") or "default=true"):
                with contextlib.suppress(Exception):
                    pic, ctype = stash.fetch(t["image_path"])
                    if ctype.startswith("image/") and len(pic) < 8 * 1024 * 1024:
                        snap["image"] = f"data:{ctype};base64," + base64.b64encode(pic).decode()
            snaps.append(snap)
        kept = stash.call(f"query($id: ID!) {{ findTag(id: $id) {{ {TAG_SNAP_FIELDS} }} }}", {"id": keep})["findTag"]
        keep_items = _tag_items(stash, keep)
        if snaps:
            stash.call("mutation($s: [ID!]!, $d: ID!) { tagsMerge(input: {source: $s, destination: $d}) { id } }",
                       {"s": [s["tag"]["id"] for s in snaps], "d": keep})
        name = (data.get("name") or "").strip()
        if name and name != kept["name"]:
            stash.call("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }", {"i": {"id": keep, "name": name}})
        return {"action": "tagmerge", "keep": keep, "kept": {"name": kept["name"], "aliases": kept.get("aliases") or []},
                "keep_items": keep_items, "removed": snaps}
    raise RuntimeError(f"unknown tag action {action!r}")


def run_tag_tidy_undo(stash, plugin_dir, undo):
    action = undo.get("action")
    if action == "no":
        with locked_tag_tidy(plugin_dir) as d:
            d["no"] = [s for s in d.get("no", []) if s != undo["sig"]]
    elif action in ("tagnew", "tagjoin"):
        _tag_on(stash, undo.get("added") or {}, undo["tag"], "REMOVE")
        if undo.get("made") and not any(_tag_items(stash, undo["made"]).values()):
            with contextlib.suppress(Exception):
                stash.call("mutation($i: TagDestroyInput!) { tagDestroy(input: $i) }", {"i": {"id": str(undo["made"])}})
    elif action == "tagsub":
        stash.call("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }",
                   {"i": {"id": undo["child"], "parent_ids": undo["before"]}})
    elif action == "tagmerge":
        keep = undo["keep"]
        # the kept tag as it was (its name; the merged names leave its aliases)
        stash.call("mutation($i: TagUpdateInput!) { tagUpdate(input: $i) { id } }",
                   {"i": {"id": keep, "name": undo["kept"]["name"], "aliases": undo["kept"]["aliases"]}})
        had = {k: set(v) for k, v in (undo.get("keep_items") or {}).items()}
        for snap in undo.get("removed") or []:
            t = snap["tag"]
            inp = {"name": t["name"], "aliases": t.get("aliases") or [], "description": t.get("description") or "",
                   "favorite": bool(t.get("favorite")), "ignore_auto_tag": bool(t.get("ignore_auto_tag")),
                   "parent_ids": [p["id"] for p in t.get("parents") or []], "child_ids": [c["id"] for c in t.get("children") or []]}
            if t.get("sort_name"):
                inp["sort_name"] = t["sort_name"]
            if snap.get("image"):
                inp["image"] = snap["image"]
            new = stash.call("mutation($i: TagCreateInput!) { tagCreate(input: $i) { id } }", {"i": inp})["tagCreate"]["id"]
            _tag_on(stash, snap["items"], new, "ADD")
            # the kept tag comes off what only had the merged one
            _tag_on(stash, {k: [i for i in ids if i not in had.get(k, set())] for k, ids in snap["items"].items()}, keep, "REMOVE")
    return True


# ---------------------------------------------------------------------------
# "Setup & troubleshooting" page: every part the add-on needs, checked, with
# what to do when something's wrong. Nothing is changed.

def _check(group, title, status, detail="", fix="", cid=None):
    return {"id": cid or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"), "group": group,
            "title": title, "status": status, "detail": detail, "fix": fix}


def _get_json(url, timeout=6, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def run_capabilities(stash, plugin_dir):
    """What's installed, for the page to show only the features that work:
    the face helper and its models, Apple's Vision, CLIP's text model, the
    AI model in Ollama, Auto Seek's model, and whether look-alikes are scanned."""
    cfg = settings(stash, plugin_dir)
    ai = cfg["ai"]
    out = {k: False for k in ("helper", "faces", "embed", "body", "apple", "ocr", "text", "ollama", "auto_seek", "looks")}
    with contextlib.suppress(Exception):
        h = _get_json(ai["face_url"] + "/health", timeout=3)
        out["helper"] = bool(h.get("ok"))
        out["faces"] = out["helper"]
        for k in ("embed", "body", "apple", "ocr", "text"):
            out[k] = bool(h.get(k))
    with contextlib.suppress(Exception):
        names = [m.get("name") for m in _get_json(ai["url"] + "/api/tags", timeout=3).get("models", [])]
        want = ai["model"] if ":" in ai["model"] else ai["model"] + ":latest"
        out["ollama"] = want in names
    with contextlib.suppress(Exception):
        out["auto_seek"] = _video_model_ready(video_model_dir(plugin_dir)) or bool(auto_seek_dir(plugin_dir))
    with contextlib.suppress(OSError):
        out["looks"] = os.path.getsize(os.path.join(plugin_dir, "look_items.json")) > 1000
    # for the setup guide: where things are looked for
    out["docker"] = os.path.exists("/.dockerenv")
    out["face_url"], out["ollama_url"], out["model"] = ai["face_url"], ai["url"], ai["model"]
    return out


def run_diagnose(stash, plugin_dir):
    out = []
    cfg = settings(stash, plugin_dir)
    ai = cfg["ai"]
    docker = os.path.exists("/.dockerenv")
    local_host = "host.docker.internal" if docker else "localhost"

    # --- Stash and this plugin
    out.append(_check("Stash", "Python", "ok", f"Python {sys.version.split()[0]} runs the add-on's tasks."))
    try:
        test = os.path.join(plugin_dir, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        out.append(_check("Stash", "Plugin folder can be written", "ok", plugin_dir))
    except OSError as e:
        out.append(_check("Stash", "Plugin folder can be written", "fail", f"{plugin_dir}: {e}",
                          "The add-on keeps what it's done in small files in its own folder. Give Stash write access to its plugins folder."))
    try:
        usage = shutil.disk_usage(plugin_dir)
        free_gb = usage.free / 1e9
        size = sum(os.path.getsize(os.path.join(plugin_dir, f)) for f in os.listdir(plugin_dir) if f.endswith(".json"))
        out.append(_check("Stash", "Disk space", "ok" if free_gb > 2 else "warn",
                          f"{free_gb:.1f} GB free; the add-on's saved data uses {size / 1e6:.1f} MB.",
                          "" if free_gb > 2 else "Free up some space: scans and the saved fingerprints need room to grow."))
    except OSError:
        pass
    try:
        out.append(_check("Stash", "ffmpeg (video frames)", "ok", ffmpeg_path(stash)))
    except Exception as e:
        out.append(_check("Stash", "ffmpeg (video frames)", "fail", str(e),
                          "Scenes need ffmpeg for their frames. Stash's Docker image has it; otherwise set its path under Settings → System → Application paths."))
    # pictures: can the add-on fetch one (a login page instead means the API key is missing)
    try:
        img = stash.call("{ findImages(filter: {per_page: 1, sort: \"random\"}) { images { id paths { thumbnail } visual_files { ... on BaseFile { path } } } } }")["findImages"]["images"]
        if img:
            data, ctype = stash.fetch(img[0]["paths"]["thumbnail"])
            if ctype.startswith("image/"):
                key = "with Stash's API key" if "ApiKey" in stash.headers else "with the task's login"
                out.append(_check("Stash", "Pictures can be fetched", "ok", f"A thumbnail came back ({len(data) // 1024} KB, {key})."))
            else:
                out.append(_check("Stash", "Pictures can be fetched", "fail", f"Stash sent {ctype} instead of a picture.",
                                  "Long tasks lose their login after about an hour. Generate an API key under Settings → Security; the add-on uses it from then on."))
            path = ((img[0].get("visual_files") or [{}])[0] or {}).get("path") or ""
            if path:
                # a picture inside a zip / cbz is no file of its own: check the archive
                arch = re.match(r"(.+?\.(?:zip|cbz|cbr|rar|7z))(?:[\\/]|$)", path, re.I)
                path = arch.group(1) if arch else path
                ok = os.path.exists(path)
                out.append(_check("Stash", "Library files reachable", "ok" if ok else "fail",
                                  f"{path} {'is there' if ok else 'can not be opened'}.",
                                  "" if ok else "Stash can't see your library files (a network drive that disconnected, or a Docker volume that went stale). "
                                                 "Reconnect the drive, then restart Stash (e.g. docker restart stash)."))
    except Exception as e:
        out.append(_check("Stash", "Pictures can be fetched", "fail", str(e),
                          "Check that Stash is running and that the add-on can log in (Settings → Security → API key)."))

    # --- Ollama
    try:
        tags = _get_json(ai["url"] + "/api/tags")
        names = [m.get("name") for m in tags.get("models", [])]
        out.append(_check("AI model (Ollama)", "Ollama is reachable", "ok", f"{ai['url']} answers; {len(names)} model(s) installed."))
        want = ai["model"] if ":" in ai["model"] else ai["model"] + ":latest"
        if want in names:
            size = next((m.get("size") for m in tags["models"] if m.get("name") == want), 0) or 0
            out.append(_check("AI model (Ollama)", f"Model {ai['model']} is installed", "ok", f"{size / 1e9:.1f} GB."))
            try:
                info = _get_json(ai["url"] + "/api/show", body={"model": ai["model"]})
                caps = info.get("capabilities") or []
                vision = "vision" in caps or "clip" in json.dumps(info.get("model_info") or {}).lower() or "projector_info" in info
                out.append(_check("AI model (Ollama)", "Model can see pictures", "ok" if vision else "fail",
                                  ("It can." if vision else f"{ai['model']} only reads text, so it can't tag pictures."),
                                  "" if vision else "Use a vision model: set Setup: AI model (in Ollama) to gemma3:4b and run: ollama pull gemma3:4b"))
            except Exception:
                pass
        else:
            out.append(_check("AI model (Ollama)", f"Model {ai['model']} is installed", "fail",
                              "Installed: " + (", ".join(n for n in names if n) or "none"),
                              f"On the computer running Ollama: ollama pull {ai['model']}  (or set Setup: AI model (in Ollama) to one of the installed vision models)"))
        try:
            ps = _get_json(ai["url"] + "/api/ps")
            loaded = [m.get("name") for m in ps.get("models", [])]
            out.append(_check("AI model (Ollama)", "Model in memory", "ok" if want in loaded else "info",
                              "Loaded, so the next request is quick." if want in loaded else
                              "Not loaded right now: the first request takes a little longer (it loads by itself)."))
        except Exception:
            pass
    except Exception as e:
        out.append(_check("AI model (Ollama)", "Ollama is reachable", "fail", f"{ai['url']}: {e}",
                          f"Start Ollama (Mac: brew services start ollama). From Stash in Docker the address is http://{local_host}:11434; "
                          "on Linux Docker, run Stash with network_mode: host and use http://127.0.0.1:11434. Set it under Setup: Ollama address."))

    # --- face helper
    try:
        health = _get_json(ai["face_url"] + "/health")
        out.append(_check("Face helper", "Face helper is reachable", "ok", f"{ai['face_url']} answers."))
        parts = [("embed", "Picture fingerprints (CLIP)", "look-alike tags, galleries and suggestions"),
                 ("body", "Body-part detector (NudeNet)", "body tags"),
                 ("match", "Face matching", "the Review page's faces"),
                 ("knn", "Look-alike search", "Review → Tags and Check"),
                 ("cluster", "Grouping", "Review → New performers and Tags → New")]
        old = []
        for key, title, use in parts:
            if health.get(key):
                out.append(_check("Face helper", title, "ok", f"Ready ({use})."))
            else:
                old.append(title)
                out.append(_check("Face helper", title, "fail" if key in ("embed", "match") else "warn", f"Missing: {use} won't work.",
                                  "Run face-helper/setup.sh again from the add-on's folder: it updates the helper and its models."))
        # picture grouping needs the newer helper (pictures, not just faces)
        if health.get("cluster"):
            try:
                one = base64.b64encode(bytes(512)).decode()
                res = _get_json(ai["face_url"] + "/cluster", body={"faces": [one, one], "threshold": 0.5, "dim": 512, "center": True})["results"]
                ok = len(res) == 2
            except Exception:
                ok = False
            out.append(_check("Face helper", "Groups pictures (not just faces)", "ok" if ok else "warn",
                              "Ready." if ok else "This helper only groups faces, so Review → Tags → New can't work.",
                              "" if ok else "Run face-helper/setup.sh again to update the helper."))
    except Exception as e:
        out.append(_check("Face helper", "Face helper is reachable", "fail", f"{ai['face_url']}: {e}",
                          "Install it: from the add-on's folder run  sh face-helper/setup.sh  (Mac / Linux). On a Mac it starts at login; "
                          f"check it with curl http://127.0.0.1:11535/health. From Stash in Docker the address is http://{local_host}:11535."))

    # --- what's been scanned
    try:
        n_items = stash.call("{ findScenes(filter: {per_page: 0}) { count } findImages(filter: {per_page: 0}) { count } }")
        total = n_items["findScenes"]["count"] + n_items["findImages"]["count"]
        looks = sum(1 for v in LookCache(plugin_dir).data.values() if v.get("e"))
        share = looks / total if total else 0
        out.append(_check("Scans", "Look-alike scan", "ok" if share > 0.8 else "warn" if looks else "fail",
                          f"{looks:,} of {total:,} scenes and images have a picture fingerprint ({share:.0%}).",
                          "" if share > 0.8 else 'Run "Tags: scan scenes and images for look-alikes (do this first)" (Settings → Tasks). It carries on where it stopped.'))
        faces = len(FaceCache(plugin_dir).data)
        fshare = faces / total if total else 0
        out.append(_check("Scans", "Face scan", "ok" if fshare > 0.8 else "warn" if faces else "fail",
                          f"{faces:,} of {total:,} scenes and images scanned for faces ({fshare:.0%}).",
                          "" if fshare > 0.8 else 'Run "Faces: scan scenes and images" (Settings → Tasks).'))
        index = load_face_index(plugin_dir)
        known = sum(1 for e in index.values() if known_embs(e))
        n_perf = stash.call("{ findPerformers(filter: {per_page: 0}) { count } }")["findPerformers"]["count"]
        out.append(_check("Scans", "Performers' faces learned", "ok" if known else "warn",
                          f"{known:,} of {n_perf:,} performers have a face the add-on knows.",
                          "" if known else 'Run "Faces: learn performers\' faces" - it uses their profile pictures.'))
    except Exception as e:
        out.append(_check("Scans", "Scans", "warn", str(e)))

    # --- settings
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(ai["time_zone"])
        tz_ok = True
    except Exception:
        tz_ok = False
    out.append(_check("Settings", "Time zone", "ok" if tz_ok else "warn", ai["time_zone"],
                      "" if tz_ok else "Schedules: time zone isn't a known zone; use one like America/New_York or Europe/London."))
    on = [n for n, v in (("AI tags for new items", ai["auto"] or ai["new_all"]), ("watermarks on new photos", ai["photo_studios"]),
                         ("faces added automatically", ai["face_auto"]), ("wait until night", ai["nightly"])) if v]
    out.append(_check("Settings", "After a scan", "info", ("On: " + ", ".join(on)) if on else "Nothing runs by itself after a scan.",
                      "" if on else "Turn on the ones you want under Settings → Plugins → Better Organizing AI Add-on."))
    if night_wanted(cfg):
        try:
            with open(os.path.join(plugin_dir, "ai_night.json")) as f:
                waiting = sum(len(v) for v in (json.load(f) or {}).values() if isinstance(v, list))
        except (OSError, ValueError):
            waiting = 0
        try:
            with open(os.path.join(plugin_dir, "night_runner.pid")) as f:
                alive = pid_running(int(f.read().strip()), "--night-runner")
        except (OSError, ValueError):
            alive = False
        out.append(_check("Settings", "Tonight's run", "info" if alive or not waiting else "warn",
                          f"{waiting} item(s) waiting for {ai['night_time']} ({ai['time_zone']}), {len(ai['night_jobs']['stash']) + len(ai['night_jobs']['tasks'])} nightly job(s) chosen; the night timer is {'running' if alive else 'not running'}.",
                          "" if alive or not waiting else "Stash was probably restarted. It starts again with the next scan, or run "
                          "\"Schedules: run tonight's waiting steps for new items now\" to do them straight away."))
    env = _stash_environment()
    out.insert(0, _check("Stash", "Where Stash runs", "info",
                         env["where"][0].upper() + env["where"][1:] + (f" (inside the container: {env['os']})." if env["docker"] else ".")))
    return {"checks": out, "model": ai["model"], "ollama": ai["url"], "helper": ai["face_url"], "docker": docker, "where": env["where"]}


def run_diagnose_ai(stash, plugin_dir):
    """One real request: a random photo shown to the model, timed."""
    cfg = settings(stash, plugin_dir)
    ai_check(cfg)
    img = stash.call("{ findImages(filter: {per_page: 1, sort: \"random\"}) { images { id paths { thumbnail } } } }")["findImages"]["images"]
    if not img:
        raise RuntimeError("no images in the library to try it on")
    pic, _ = stash.fetch(img[0]["paths"]["thumbnail"])
    t0 = time.time()
    body = {"model": cfg["ai"]["model"], "stream": False, "keep_alive": "15m", "options": {"temperature": 0},
            "messages": [{"role": "user", "content": "Describe this picture in at most 8 words.",
                          "images": [base64.b64encode(pic).decode()]}]}
    reply = _get_json(cfg["ai"]["url"] + "/api/chat", timeout=300, body=body)
    secs = time.time() - t0
    return {"id": img[0]["id"], "seconds": round(secs, 1), "answer": ((reply.get("message") or {}).get("content") or "").strip()[:200]}


def run_diagnose_models(stash, plugin_dir):
    """Every model Ollama has, and whether each can see pictures."""
    cfg = settings(stash, plugin_dir)
    ai = cfg["ai"]
    tags = _get_json(ai["url"] + "/api/tags")
    out = []
    for m in tags.get("models", []):
        name = m.get("name")
        vision = None
        with contextlib.suppress(Exception):
            info = _get_json(ai["url"] + "/api/show", body={"model": name})
            caps = info.get("capabilities") or []
            vision = "vision" in caps or "projector_info" in info
        out.append({"name": name, "size_gb": round((m.get("size") or 0) / 1e9, 1), "vision": vision,
                    "chosen": name == (ai["model"] if ":" in ai["model"] else ai["model"] + ":latest")})
    return {"models": out, "setting": ai["model"], "url": ai["url"]}


def run_diagnose_files(stash, plugin_dir, n=24):
    """Random scenes and images: can their files be opened? Grouped by the
    folder they're under, so a drive that's gone shows up as one line."""
    items = []
    for q, f, fields in (("findScenes", "scenes", "files { path }"), ("findImages", "images", "visual_files { ... on BaseFile { path } }")):
        got = stash.call(f'{{ {q}(filter: {{per_page: {n // 2}, sort: "random"}}) {{ {f} {{ {fields} }} }} }}')[q][f]
        for x in got:
            p = (((x.get("files") or x.get("visual_files") or [{}])[0]) or {}).get("path")
            if p:
                items.append(p)
    roots = defaultdict(lambda: [0, 0])
    for p in items:
        arch = re.match(r"(.+?\.(?:zip|cbz|cbr|rar|7z))(?:[\\/]|$)", p, re.I)
        real = arch.group(1) if arch else p
        parts = [x for x in re.split(r"[\\/]", real) if x]
        root = "/" + "/".join(parts[:2]) if len(parts) > 2 else os.path.dirname(real)
        roots[root][0] += 1
        if os.path.exists(real):
            roots[root][1] += 1
    try:
        libs = [x["path"] for x in stash.call("{ configuration { general { stashes { path } } } }")["configuration"]["general"]["stashes"]]
    except Exception:
        libs = []
    lib_ok = [{"path": l, "ok": os.path.isdir(l) and bool(os.listdir(l) if os.path.isdir(l) else [])} for l in libs]
    return {"roots": [{"root": r, "checked": c, "ok": ok} for r, (c, ok) in sorted(roots.items())], "libraries": lib_ok}


def _stash_environment():
    """Where Stash (and so this script) runs: Docker or not, and on what."""
    import platform
    import socket
    docker = os.path.exists("/.dockerenv")
    if not docker:
        with contextlib.suppress(OSError):
            with open("/proc/1/cgroup") as f:
                docker = any(x in f.read() for x in ("docker", "containerd", "kubepods"))
    kernel = platform.release().lower()
    system = platform.system()
    desktop = docker and ("linuxkit" in kernel or "microsoft" in kernel or "wsl" in kernel)
    try:
        socket.gethostbyname("host.docker.internal")
        hdi = True
    except OSError:
        hdi = False
    gateway = None
    with contextlib.suppress(OSError, ValueError, IndexError):
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts[1] == "00000000":
                    gw = int(parts[2], 16)
                    gateway = ".".join(str((gw >> (8 * i)) & 255) for i in range(4))
                    break
    os_name = system
    with contextlib.suppress(OSError):
        with open("/etc/os-release") as f:
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)', f.read(), re.M)
            if m:
                os_name = m.group(1)
    if docker:
        where = ("in Docker Desktop, on a Mac or Windows computer" if desktop or (hdi and "linuxkit" in kernel)
                 else "in Docker on Linux" + (" (host.docker.internal is set up)" if hdi else ""))
    else:
        where = {"Darwin": "installed directly on a Mac", "Windows": "installed directly on Windows"}.get(system, f"installed directly on {os_name}")
    return {"docker": docker, "desktop": bool(desktop), "hdi": hdi, "gateway": gateway, "where": where,
            "os": os_name, "kernel": platform.release()}


def run_find_ollama(stash, plugin_dir):
    """Every address Ollama is usually at, tried from where Stash runs."""
    cfg = settings(stash, plugin_dir)
    env = _stash_environment()
    cands = [cfg["ai"]["url"], "http://host.docker.internal:11434", "http://localhost:11434", "http://127.0.0.1:11434"]
    if env["gateway"]:
        cands.append(f"http://{env['gateway']}:11434")
    cands += ["http://172.17.0.1:11434", "http://ollama:11434"]
    tried, seen = [], set()
    for url in cands:
        if url in seen:
            continue
        seen.add(url)
        try:
            ver = _get_json(url + "/api/version", timeout=3).get("version")
            models = [m.get("name") for m in _get_json(url + "/api/tags", timeout=3).get("models", [])]
            tried.append({"url": url, "ok": True, "version": ver, "models": models, "current": url == cfg["ai"]["url"]})
        except Exception as e:
            reason = str(getattr(e, "reason", e))
            tried.append({"url": url, "ok": False, "error": reason[:120], "current": url == cfg["ai"]["url"]})
    # what to do when none answers, for this setup
    if env["docker"] and not env["desktop"] and not env["hdi"]:
        advice = ("Stash is in Docker on Linux, where containers can't reach programs on the computer itself by default. "
                  "Either run the Stash container with network_mode: host (then use http://127.0.0.1:11434), or add "
                  "extra_hosts: [\"host.docker.internal:host-gateway\"] to it and start Ollama with OLLAMA_HOST=0.0.0.0 "
                  "(then use http://host.docker.internal:11434). Or run Ollama in Docker on the same network "
                  "(then use http://ollama:11434).")
    elif env["docker"]:
        advice = ("Stash is in Docker Desktop: Ollama on the same computer is at http://host.docker.internal:11434. "
                  "Make sure Ollama is running there (Mac: brew services start ollama, or open the Ollama app).")
    else:
        advice = ("Stash runs directly on the computer: Ollama on the same computer is at http://localhost:11434. "
                  "Make sure it's running (Mac: brew services start ollama; Linux: systemctl start ollama).")
    return {"env": env, "tried": tried, "advice": advice, "setting": cfg["ai"]["url"]}


# --- positions seen in the video (Auto Seek's model) ----------------------
# The Auto Seek plugin's model (CLIP ViT-B/32, MIT) scores each frame for
# "has it started" and for 10 positions. Tried on 60 well-tagged scenes, it
# was right often enough only for these four (the others were right 25-60% of
# the time), so only these are suggested. Each: the tag names it can be in
# your library (the first one you have is suggested; having any of them
# counts), and when a scene counts as showing it: that position's share of a
# frame at least `share`, in at least `frames` of the 24 frames looked at.
VIDEO_ACTS = {
    "cowgirl": {"names": ["Cowgirl", "Riding", "Reverse Cowgirl"], "share": 0.5, "frames": 2},
    "oral on him": {"names": ["Blowjob", "Blow Job", "Deepthroat", "Oral"], "share": 0.5, "frames": 2},
    "from behind": {"names": ["Doggy Style", "Doggystyle", "Doggy"], "share": 0.6, "frames": 2},
    "missionary": {"names": ["Missionary"], "share": 0.6, "frames": 1},
}
VIDEO_FRAMES = 24
VIDEO_STARTED = 0.5   # frames the model thinks are before the action are left out
VIDEO_MIN_SECONDS = 60


AUTO_SEEK_COPY = "auto-seek-copy"  # where the copy used to be: in this plugin's folder
VIDEO_MODEL_TASK = "Markers: download the video model (needed first)"


def video_model_dir(plugin_dir):
    """The add-on's own copy of Auto Seek's model, runtime and code: in Stash's
    config folder (plugin-data/BetterTaggingAI/auto-seek), so updating or
    reinstalling the add-on - or removing Auto Seek - keeps it. A copy in the
    plugin folder (where it used to be) is moved there."""
    config = os.path.dirname(os.path.dirname(os.path.abspath(plugin_dir)))
    new = os.path.join(config, "plugin-data", AI_ID, "auto-seek")
    old = os.path.join(os.path.abspath(plugin_dir), AUTO_SEEK_COPY)
    if not os.path.isfile(os.path.join(new, "auto_seek.py")) and os.path.isfile(os.path.join(old, "auto_seek.py")):
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(new), exist_ok=True)
            shutil.rmtree(new, ignore_errors=True)
            shutil.move(old, new)
            _video_model_readme(new)
    return new


def _video_model_readme(where):
    with contextlib.suppress(OSError):
        with open(os.path.join(where, "README.txt"), "w") as f:
            f.write("Better Organizing AI's copy of the Auto Seek video model, its runtime and code, for AI markers\n"
                    "and positions in videos - so they work without the Auto Seek plugin. The model is MIT\n"
                    "(huggingface.co/mrx-31415/auto-seek); Auto Seek's code is AGPL-3.0 (LICENSE); ONNX Runtime is MIT.\n"
                    "If it's deleted, the task \"" + VIDEO_MODEL_TASK + "\" gets it again.\n")


def auto_seek_dir(plugin_dir):
    """The Auto Seek plugin's folder next to this one, or None."""
    root = os.path.dirname(os.path.abspath(plugin_dir))
    for name in ["auto-seek"] + sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, name, "auto_seek.py")):
            return os.path.join(root, name)
    return None


def keep_auto_seek_copy(plugin_dir, src):
    """A copy of what's needed from Auto Seek (its code, model and runtime,
    and the record that they were checked), kept in this plugin's folder so
    positions and AI markers keep working if Auto Seek is removed. Made once,
    and again when Auto Seek gets a new model or runtime."""
    import shutil  # noqa: PLC0415
    dst = video_model_dir(plugin_dir)
    try:
        with open(os.path.join(src, "data", "verified.json")) as f:
            theirs = f.read()
    except OSError:
        return  # not downloaded yet
    with contextlib.suppress(OSError):
        with open(os.path.join(dst, "data", "verified.json")) as f:
            if f.read() == theirs and os.path.isfile(os.path.join(dst, "auto_seek.py")):
                return
    tmp = dst + f".{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, "data"))
    for name in ("auto_seek.py", "ort_api.py", "LICENSE"):
        if os.path.isfile(os.path.join(src, name)):
            shutil.copy2(os.path.join(src, name), tmp)
    for sub in ("models", "runtime"):
        if os.path.isdir(os.path.join(src, "data", "files", sub)):
            shutil.copytree(os.path.join(src, "data", "files", sub), os.path.join(tmp, "data", "files", sub), symlinks=True)
    with open(os.path.join(tmp, "data", "verified.json"), "w") as f:
        f.write(theirs)
    _video_model_readme(tmp)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.rmtree(dst, ignore_errors=True)
    _replace(tmp, dst)


VIDEO_MODEL_MIRROR = "https://raw.githubusercontent.com/OpenSourceLuffy/stash-plugins/main/extras/video-model/"


def _video_code_dir():
    """Auto Seek's code (0.3.1, AGPL-3.0), shipped with this add-on in its
    video-model folder - so the Auto Seek plugin isn't needed."""
    return os.path.join(_ai_data_dir(), "video-model")


def _import_video_code(data_dir, code_dir=None):
    """Import Auto Seek's code with its data (model, runtime) in data_dir."""
    code_dir = code_dir or _video_code_dir()
    if not os.path.isfile(os.path.join(code_dir, "auto_seek.py")):
        raise NotReady("the add-on's video-model folder is missing - reinstall the add-on")
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)
    os.makedirs(data_dir, exist_ok=True)
    argv = sys.argv
    sys.argv = [argv[0], data_dir]  # (Auto Seek finds its data folder from its arguments)
    try:
        import auto_seek  # noqa: PLC0415
    finally:
        sys.argv = argv
    return auto_seek


def _video_model_ready(data_dir):
    with contextlib.suppress(OSError, ValueError):
        with open(os.path.join(data_dir, "data", "verified.json")) as f:
            return any(k.endswith(".onnx") for k in json.load(f))
    return False


def run_get_video_model(stash, plugin_dir):
    """Task: get the video model for AI markers and positions. The code ships
    with the add-on; the model and Alpine (Docker) runtimes come from this
    add-on's own repository (extras/video-model), other runtimes from
    Microsoft - each checked against the SHA-256 pinned in the code. If the
    repository can't be reached, the original addresses are tried."""
    where = auto_seek_dir(plugin_dir)
    if where:  # (the Auto Seek plugin is installed: copy its files instead of downloading)
        with contextlib.suppress(Exception):
            keep_auto_seek_copy(plugin_dir, where)
    dst = video_model_dir(plugin_dir)
    A = _import_video_code(dst)
    if A.status().get("ready"):
        log("info", f"The video model is already there (in {dst}).")
        return
    original = {id(e): e["url"] for e in A.FILES}
    mirrored = [e for e in A.FILES if e is A.MODEL or "musl" in e["url"] or "alpine" in e["url"]]
    for e in mirrored:
        e["url"] = VIDEO_MODEL_MIRROR + e["url"].rsplit("/", 1)[-1].split("?")[0]
    log("info", f"Downloading the video model (about {sum(e.get('size', 0) for e in A.FILES) // 1048576} MB, "
        "each file checked against its SHA-256)...")
    progress(0.1)
    for attempt in ("this add-on's repository", "the original addresses"):
        A.download()
        state = A.read_json(A.STATE_PATH, {}) if hasattr(A, "read_json") else {}
        if A.status().get("ready"):
            break
        log("warning", f"Couldn't download from {attempt}: {state.get('error') or 'no answer'}")
        for e in mirrored:  # then Auto Seek's own addresses
            e["url"] = original[id(e)]
    else:
        raise RuntimeError("the video model couldn't be downloaded (see the warnings above)")
    progress(1)
    log("info", f"The video model is ready (in {dst}): AI markers and positions in videos can run.")


def load_auto_seek(plugin_dir):
    """Auto Seek's code (shipped with the add-on), with the model and runtime
    in Stash's config folder. If the Auto Seek plugin is installed, its
    downloaded files are copied there first (saves a download)."""
    where = auto_seek_dir(plugin_dir)
    if where:
        try:
            keep_auto_seek_copy(plugin_dir, where)
        except Exception as e:
            log("debug", f"couldn't copy Auto Seek's model: {e}")
    dst = video_model_dir(plugin_dir)
    if not _video_model_ready(dst):
        raise NotReady(f'the video model isn\'t there yet - run the task "{VIDEO_MODEL_TASK}" once '
                       "(about 100 MB; the Auto Seek plugin isn't needed)")
    return _import_video_code(dst)


def video_acts_path(plugin_dir):
    return os.path.join(plugin_dir, "video_acts.json")


def load_video_acts(plugin_dir):
    """{"scene id": {"d": duration, "s": file size, "f": [[time, started, [10 shares]]]}}"""
    try:
        with open(video_acts_path(plugin_dir)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_video_acts(plugin_dir, data):
    path = video_acts_path(plugin_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    _replace(tmp, path)


def run_video_scan(stash, plugin_dir, limit=None):
    """Task: Auto Seek's model on 24 frames of every scene not done yet
    (a few seconds each). Only saves what it sees; nothing is tagged."""
    A = load_auto_seek(plugin_dir)
    try:
        sess = A.scorer(4)
    except Exception as e:
        raise RuntimeError(f"Auto Seek's model isn't ready: {e}")
    general = stash.call("{ configuration { general { ffmpegPath } } }")["configuration"]["general"]
    ff = A.find_ffmpeg(general.get("ffmpegPath") or "", {"Dir": stash.config_dir})
    data = load_video_acts(plugin_dir)
    scenes = stash.call("{ findScenes(filter: {per_page: -1}) { scenes { id files { path size duration } } } }")["findScenes"]["scenes"]
    todo = []
    for s in scenes:
        f = (s.get("files") or [None])[0]
        if not f or (f.get("duration") or 0) < VIDEO_MIN_SECONDS:
            continue
        old = data.get(s["id"])
        if old and old.get("s") == f.get("size"):
            continue
        todo.append((s["id"], f))
    if limit:
        todo = todo[:limit]
    log("info", f"Looking at {len(todo)} scene(s) with Auto Seek's model ({len(data)} done before). "
        "A few seconds each; you can cancel and run it again to carry on.")
    started = time.time()
    done = 0
    for n, (sid, f) in enumerate(todo):
        progress(n / max(len(todo), 1))
        dur = float(f["duration"])
        cend = content_end(stash, f)  # (not an end card)
        times = [cend * (0.04 + 0.92 * k / (VIDEO_FRAMES - 1)) for k in range(VIDEO_FRAMES)]
        try:
            frames = []
            for g in range(0, VIDEO_FRAMES, 3):
                frames += A.grab_frames(ff, f["path"], times[g:g + 3])
            rows = []
            for t, px in zip(times, frames):
                if px is None or A.blank(px):
                    continue
                st, acts = sess.score(px, [t / dur, t / 60, dur / 3600])
                rows.append([round(t, 1), round(st, 2), [round(a, 2) for a in acts]])
        except Exception as e:
            log("debug", f"scene {sid}: {e}")
            continue
        data[sid] = {"d": round(dur, 1), "s": f.get("size"), "f": rows}
        done += 1
        if done % 25 == 0:
            save_video_acts(plugin_dir, data)
    save_video_acts(plugin_dir, data)
    progress(1)
    log("info", f"Done: {done} scene(s) in {time.time() - started:.0f}s. Suggestions are under Review → Tags → Suggested.")


def _video_tag_ids(stash):
    """For each position: (the tag to suggest, {every tag that counts as having it})."""
    tags = stash.call("{ allTags { id name aliases } }")["allTags"]
    by_name = {}
    for t in tags:
        for n in [t["name"]] + (t.get("aliases") or []):
            by_name.setdefault(n.strip().lower(), t)
    out = {}
    for act, rule in VIDEO_ACTS.items():
        found = [by_name[n.lower()] for n in rule["names"] if n.lower() in by_name]
        if found:
            out[act] = (found[0], {t["id"] for t in found})
    return out


def video_seen(entry, acts_order, act):
    """(frames that show the position, their mean share) for one scene."""
    rule = VIDEO_ACTS[act]
    ai = acts_order.index(act)
    hits = [(t, ac[ai]) for t, st, ac in entry.get("f", []) if st >= VIDEO_STARTED and ac[ai] >= rule["share"]]
    if len(hits) < rule["frames"]:
        return None
    return hits


def _video_picks(stash, plugin_dir, have, nots, skip, want=None):
    """Positions seen in videos that the scene isn't tagged with and you
    haven't turned down: [(item key, tag, {frames, looked, times, rank})].
    rank: how sure, on the same 0-1 scale as look-alike scores (all 24
    frames = 1)."""
    data = load_video_acts(plugin_dir)
    if not data:
        return []
    acts_order = list(load_auto_seek(plugin_dir).ACTS)
    out = []
    for act, (t, counts) in _video_tag_ids(stash).items():
        if want and t["id"] != want:
            continue
        for sid, entry in data.items():
            key = f"scene:{sid}"
            if have.get(key, set()) & counts or t["id"] in nots.get(key, ()) or f"{key}:{t['id']}" in skip:
                continue
            hits = video_seen(entry, acts_order, act)
            if not hits:
                continue
            looked = sum(1 for _, st, _ in entry["f"] if st >= VIDEO_STARTED) or 1
            out.append((key, t, {"frames": len(hits), "looked": looked, "times": [round(x) for x, _ in hits[:6]],
                                 "rank": round(0.5 + 0.5 * len(hits) / looked, 3)}))
    return out


# --- AI markers for one scene (scene page → Markers → AI markers) ---------
# A closer look than the library scan: a frame every MARKER_STEP seconds (at
# most MARKER_MAX_FRAMES), scored by Auto Seek's model and given a CLIP picture
# fingerprint by the face helper. A shift is either
#  - a big change in the positions Auto Seek's model sees (the 3 frames after
#    a point against the 3 before), unless the pictures barely change; or
#  - frames after a point that look like none of the frames just before it
#    (and the other way round): a new setting, activity or people. Comparing
#    each frame with its closest match, not the average look, keeps a camera
#    cutting between a close-up and a wide shot from counting as a shift.
# Checked by eye on 30 scenes: this kept every real shift the positions alone
# found and caught 3 of 4 changes they missed (lesbian, bondage, intros), for
# about as many camera-angle mistakes. The pictures are also compared over the
# whole scene, not just the action (with a higher bar), for intros, interviews
# and talking parts before and after it. A marker over 12 minutes gets a
# second look: the strongest change inside it becomes a shift if it's nearly
# big enough (scenes in one place, like bondage in the same room, change less).
# A marker starts at the start of the scene, where the action starts, and at
# each shift, and runs until the next.
MARKER_STEP = 8
MARKER_MAX_FRAMES = 360
MARKER_WINDOW = 3
MARKER_SHIFT = 0.55   # how different the positions (0-1: share of the scores that moved)
MARKER_NEW_LOOK = 0.25  # how unlike the frames just before the frames after are (1 - closest CLIP match)
MARKER_LOOK_FLOOR = 0.06  # a change in positions only counts if the pictures change at least this much
MARKER_NEW_LOOK_WHOLE = 0.30  # the same, over the whole scene (before and after the action too: intros,
                              # interviews...), where it takes a bigger change
MARKER_MIN_GAP = 40   # seconds between shifts
MARKER_LONG = 720     # a marker this long gets another look: the strongest change inside it
MARKER_LONG_BAR = 0.8  # ...if it's at least this share of the usual bar
MARKER_FEW = 2        # a scene that gets this many markers or fewer is split at regular times too


def marker_interval(dur):
    """How far apart the regular markers of a scene with few shifts are."""
    if dur < 600:
        return 120
    if dur < 1440:
        return 180
    return min(360, max(180, dur / 8))


def _marker_frames(stash, plugin_dir, A, f):
    """[[time, started, [10 shares]]] every few seconds through the file, and
    each frame's CLIP fingerprint (base64 int8; [] if the face helper can't be
    reached - the shifts then come from the positions alone)."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    dur = float(f["duration"])
    step = max(MARKER_STEP, dur / MARKER_MAX_FRAMES)
    times = [t for t in (step / 2 + i * step for i in range(int(dur / step) + 1)) if t < dur - 1]
    general = stash.call("{ configuration { general { ffmpegPath } } }")["configuration"]["general"]
    ff = A.find_ffmpeg(general.get("ffmpegPath") or "", {"Dir": stash.config_dir})
    sess = A.scorer(4)
    groups = [times[i:i + 3] for i in range(0, len(times), 3)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        grabbed = list(pool.map(lambda ts: A.grab_frames(ff, f["path"], ts), groups))
    rows, pics = [], []
    for ts, frames in zip(groups, grabbed):
        for t, px in zip(ts, frames):
            if px is None or A.blank(px):
                continue
            st, acts = sess.score(px, [t / dur, t / 60, dur / 3600])
            rows.append([round(t, 1), round(st, 2), [round(a, 2) for a in acts]])
            pics.append(b"P6\n224 224\n255\n" + px)  # (a PPM: the helper reads it as it is)
    clips = []
    try:
        cfg = settings(stash, plugin_dir)
        for i in range(0, len(pics), 48):
            for e in picture_helper(cfg, pics[i:i + 48]):
                n = math.sqrt(sum(x * x for x in e)) or 1
                clips.append(base64.b64encode(bytes((round(x / n * 127) & 0xFF) for x in e)).decode())
    except Exception as e:
        log("debug", f"no picture fingerprints for the markers ({e}): positions only")
        clips = []
    return rows, step, clips


def _unclip(b64):
    v = [x - 256 if x > 127 else x for x in base64.b64decode(b64)]
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


def video_shifts(rows, step, dur, clips=None, scores_out=None):
    """Where the scene shifts: [{start, end, change}] (change: how big, 1+ is
    a shift; None for the start of the action). `clips`: the frames' CLIP
    fingerprints (see _marker_frames); without them, positions alone."""
    keep = [k for k, x in enumerate(rows) if x[1] >= VIDEO_STARTED]
    if not keep:
        return []
    r = [rows[k] for k in keep]
    vec = [_unclip(clips[k]) for k in keep] if clips and len(clips) == len(rows) else None
    out = [{"start": max(0.0, r[0][0] - step / 2), "change": None}]
    w = MARKER_WINDOW
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    scores = []
    for i in range(w, len(r) - w + 1):
        before = [sum(x[2][k] for x in r[i - w:i]) / w for k in range(10)]
        after = [sum(x[2][k] for x in r[i:i + w]) / w for k in range(10)]
        act = 0.5 * sum(abs(a - b) for a, b in zip(before, after))
        if vec is None:
            scores.append((i, act / MARKER_SHIFT))
            continue
        bv, av = vec[i - w:i], vec[i:i + w]
        sims = [[dot(a, b) for b in bv] for a in av]
        new_after = sum(1 - max(row) for row in sims) / len(av)
        new_before = sum(1 - max(sims[a][b] for a in range(len(av))) for b in range(len(bv))) / len(bv)
        look = 0.5 * (new_after + new_before)
        scores.append((i, max(act / MARKER_SHIFT if look >= MARKER_LOOK_FLOOR else 0, look / MARKER_NEW_LOOK)))
    near_miss = {}  # time -> how big a change (1 = the bar), for long markers' second look
    for i, d in scores:
        near_miss[r[i][0] - step / 2] = d
    for j, (i, d) in enumerate(scores):
        if d < 1 or any(d < x[1] for x in scores[max(0, j - 3):j + 4]):
            continue  # not big enough, or a bigger one is right next to it
        t = r[i][0] - step / 2
        if t - out[-1]["start"] >= MARKER_MIN_GAP:
            out.append({"start": t, "change": round(d, 2)})
    last = r[-1][0] + step
    if clips and len(clips) == len(rows):
        # the whole scene, before and after the action too: new looks only
        allv = [_unclip(x) for x in clips]
        extra = []
        for i in range(w, len(rows) - w + 1):
            bv, av = allv[i - w:i], allv[i:i + w]
            sims = [[dot(a, b) for b in bv] for a in av]
            look = 0.5 * (sum(1 - max(row) for row in sims) / len(av)
                          + sum(1 - max(sims[a][b] for a in range(len(av))) for b in range(len(bv))) / len(bv))
            extra.append((i, look / MARKER_NEW_LOOK_WHOLE))
            k = rows[i][0] - step / 2
            near_miss[k] = max(near_miss.get(k, 0), look / MARKER_NEW_LOOK_WHOLE)
        for j, (i, d) in enumerate(extra):
            if d < 1 or any(d < x[1] for x in extra[max(0, j - 3):j + 4]):
                continue
            t = rows[i][0] - step / 2
            if all(abs(t - m["start"]) >= MARKER_MIN_GAP for m in out):
                out.append({"start": t, "change": round(d, 2)})
        # the scene's own start, when the action starts later
        first = min(m["start"] for m in out)
        if first >= MARKER_MIN_GAP:
            out.append({"start": 0.0, "change": None})
        elif first > 0:  # (a shift right at the beginning: just start that marker at 0:00)
            min(out, key=lambda m: m["start"])["start"] = 0.0
        out.sort(key=lambda m: m["start"])
        last = rows[-1][0] + step
    # long markers: the strongest change inside, if it's nearly big enough
    while True:
        edges = [m["start"] for m in out] + [last]
        best = None
        for a, b in zip(edges, edges[1:]):
            if b - a <= MARKER_LONG:
                continue
            inside = [(d, t) for t, d in near_miss.items()
                      if a + MARKER_MIN_GAP <= t <= b - MARKER_MIN_GAP and d >= MARKER_LONG_BAR]
            if inside:
                best = max(inside)
                break
        if not best:
            break
        out.append({"start": best[1], "change": round(best[0], 2)})
        out.sort(key=lambda m: m["start"])
    # few shifts (one person talking, one long take...): markers at regular
    # times too, each moved to the biggest change near it
    if len(out) <= MARKER_FEW and dur >= 300:
        every = marker_interval(dur)
        edges = [m["start"] for m in out] + [last]
        for a, b in zip(edges, edges[1:]):
            parts = round((b - a) / every)
            for k in range(1, parts):
                t = a + (b - a) * k / parts
                near = [(d, x) for x, d in near_miss.items() if abs(x - t) <= every / 4 and d >= 0.4]
                if near:
                    t = max(near)[1]
                if all(abs(t - m["start"]) >= MARKER_MIN_GAP for m in out) and last - t >= MARKER_MIN_GAP:
                    out.append({"start": t, "change": 0.0})
        out.sort(key=lambda m: m["start"])
    if scores_out is not None:
        scores_out.update(near_miss)
    for m, nxt in zip(out, out[1:]):
        m["end"] = nxt["start"]
    out[-1]["end"] = min(dur, last)
    for m in out:
        m["start"], m["end"] = int(round(m["start"])), int(round(m["end"]))
    return [m for m in out if m["end"] > m["start"]]


MARKER_PICTURE_TRIES = (0, 3, 6)  # seconds after the picture's time tried for a good-looking frame
MARKER_PICTURE_BEFORE = (2, 4)    # then seconds before the marker's start (when it's given)


def _marker_pictures(stash, A, path, times, starts=None):
    """A small JPEG (base64) of the video at each time (ffmpeg only - `A`,
    Auto Seek's code, isn't needed and is ignored). On a Mac, a frame
    Apple scores low is swapped for a better one: a few seconds on, then
    (with `starts`, each marker's start) a few seconds before the marker."""
    import base64  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    ff = _ffmpeg(stash)

    def grab(t):
        with tempfile.TemporaryDirectory(prefix="bta-marker-") as folder:
            out = os.path.join(folder, "f.jpg")
            cmd = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1", "-ss", f"{max(t, 0):.2f}",
                   "-i", path, "-frames:v", "1", "-an", "-sn", "-vf", "scale=640:-2", "-q:v", "4", out]
            with _FFMPEG_SLOTS, contextlib.suppress(subprocess.TimeoutExpired):
                subprocess.run(cmd, capture_output=True, timeout=30, check=False)
            try:
                with open(out, "rb") as f:
                    return f.read() or None
            except OSError:
                return None

    def one(t, start=None):
        # on a Mac, a frame Apple scores low (blurry, dark, mid-move) is
        # swapped for one a few seconds on (or just before the marker), the
        # best of them if none is good
        best = None
        at = [t + off for off in MARKER_PICTURE_TRIES]
        if start is not None:
            at += [start - b for b in MARKER_PICTURE_BEFORE if start - b >= 0]
        for when in at:
            data = grab(when)
            if not data:
                continue
            q = picture_quality(stash, data, people=True)
            if q is None or q >= PICTURE_NICE:
                return base64.b64encode(card_picture(stash, data)).decode()
            if best is None or q > best[0]:
                best = (q, data)
        return base64.b64encode(card_picture(stash, best[1])).decode() if best else None

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(one, times, starts if starts is not None else [None] * len(times)))


def run_video_markers(stash, plugin_dir, scene_id, fresh=False, pictures=True):
    """Scene page → Markers → AI markers: where the scene shifts, with a
    picture of each, as markers to add (nothing is saved to the scene). Its frames are kept, so asking again is
    instant."""
    scene_id = str(scene_id)
    sc = stash.call("query($id: ID!) { findScene(id: $id) { id files { path size duration } } }", {"id": scene_id})["findScene"]
    f = ((sc or {}).get("files") or [None])[0]
    if not f:
        raise RuntimeError("this scene has no video file")
    A = load_auto_seek(plugin_dir)
    # each scene's frames in a file of its own (the fingerprints make them big)
    folder = os.path.join(plugin_dir, "video_frames")
    path = os.path.join(folder, f"{scene_id}.json")
    old = None
    with contextlib.suppress(OSError, ValueError):
        with open(path) as fh:
            old = json.load(fh)
    if old and old.get("s") == f.get("size") and old.get("c") and not fresh:
        rows, step, clips = old["f"], old["step"], old["c"]
    else:
        try:
            rows, step, clips = _marker_frames(stash, plugin_dir, A, f)
        except getattr(A, "FilesMissing", ()) as e:
            raise RuntimeError(f"Auto Seek's model isn't ready: {e}")
        os.makedirs(folder, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"s": f.get("size"), "step": step, "f": rows, "c": clips}, fh, separators=(",", ":"))
        _replace(tmp, path)
    dur = float(f["duration"])
    shifts = video_shifts(rows, step, dur, clips)
    # nothing from an end card (TikTok's, Instagram's...): no marker starts
    # in it, and the last one ends where it starts
    card = content_end(stash, f)
    if card < dur - min(VIDEO_END_SKIP, dur * 0.1) - 0.01:
        shifts = [dict(m, end=min(m["end"], round(card))) for m in shifts if m["start"] < card - 1]
    # the picture: a moment into the new part, so it shows what it shifted to
    pics = (_marker_pictures(stash, A, f["path"], [min(m["end"] - 1, m["start"] + step * 1.5) for m in shifts],
                             [m["start"] for m in shifts])
            if pictures else [None] * len(shifts))
    return {"markers": [{**m, "picture": p} for m, p in zip(shifts, pics)], "looked": len(rows), "step": round(step, 1)}


def _marker_picture_path(stash, scene_id, seconds):
    """Where Stash keeps a marker's picture: generated/markers/<the scene's file
    hash>/<whole seconds>.jpg (None if that can't be worked out)."""
    cfg = stash.call("{ configuration { general { generatedPath videoFileNamingAlgorithm } } }")["configuration"]["general"]
    sc = stash.call("query($id: ID!) { findScene(id: $id) { files { fingerprints { type value } } } }", {"id": str(scene_id)})["findScene"]
    prints = {fp["type"]: fp["value"] for fp in ((sc or {}).get("files") or [{}])[0].get("fingerprints") or []}
    h = prints.get("md5" if cfg.get("videoFileNamingAlgorithm") == "MD5" else "oshash")
    if not h or not cfg.get("generatedPath"):
        return None
    return os.path.join(cfg["generatedPath"], "markers", h, f"{int(float(seconds))}.jpg")


@contextlib.contextmanager
def keeping_marker_picture(stash, scene_id, seconds):
    """Stash deletes a marker's picture when its times change (so Generate
    makes a new one): the picture is kept and put back afterwards."""
    path = None
    data = None
    with contextlib.suppress(Exception):
        path = _marker_picture_path(stash, scene_id, seconds)
        with open(path, "rb") as f:
            data = f.read()
    yield
    if path and data and not os.path.exists(path):
        with contextlib.suppress(OSError):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)


def ensure_marker_picture(stash, plugin_dir, marker_id, wait=0):
    """Gives a marker a picture if it has none: the frame AI markers would
    use (a moment into the marker), cut from the video."""
    if wait:
        time.sleep(wait)  # (the add-on saves its own picture right after making a marker)
    got = stash.call("query($ids: [ID!]) { findSceneMarkers(ids: $ids) { scene_markers { id seconds end_seconds scene { id files { path } } } } }",
                     {"ids": [str(marker_id)]})["findSceneMarkers"]["scene_markers"]
    if not got:
        return False
    m = got[0]
    sid = m["scene"]["id"]
    path = _marker_picture_path(stash, sid, m["seconds"])
    if not path or os.path.exists(path):
        return False
    at = m["seconds"] + 2
    with contextlib.suppress(OSError, ValueError, KeyError):
        with open(os.path.join(plugin_dir, "video_frames", f"{sid}.json")) as f:
            step = json.load(f)["step"]
        at = m["seconds"] + step * 1.5  # (where AI markers take theirs)
    if m.get("end_seconds"):
        at = min(at, max(m["seconds"], m["end_seconds"] - 1))
    pic = _marker_pictures(stash, None, m["scene"]["files"][0]["path"], [at], [float(m["seconds"])])[0]
    return run_marker_picture(stash, sid, m["seconds"], pic)


def run_marker_picture(stash, scene_id, seconds, picture):
    """Save a marker's picture where Stash looks for it (generated/markers/
    <the scene's file hash>/<whole seconds>.jpg), so it shows without running
    Generate. A picture already there is kept."""
    import base64  # noqa: PLC0415
    cfg = stash.call("{ configuration { general { generatedPath videoFileNamingAlgorithm } } }")["configuration"]["general"]
    sc = stash.call("query($id: ID!) { findScene(id: $id) { files { fingerprints { type value } } } }", {"id": str(scene_id)})["findScene"]
    prints = {fp["type"]: fp["value"] for fp in ((sc or {}).get("files") or [{}])[0].get("fingerprints") or []}
    want = "md5" if cfg.get("videoFileNamingAlgorithm") == "MD5" else "oshash"
    h = prints.get(want)
    if not h or not cfg.get("generatedPath") or not picture:
        return False
    folder = os.path.join(cfg["generatedPath"], "markers", h)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{int(float(seconds))}.jpg")
    if os.path.exists(path):
        return False
    with open(path, "wb") as f:
        f.write(base64.b64decode(picture))
    return True


def _marker_clock(sec):
    sec = max(0, int(round(sec)))
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


def ai_marker_tag(stash, create=True):
    """The tag AI markers get (the setting "Markers: tag for AI markers",
    Scene Shift unless changed): (id, name). Made if missing and `create`."""
    name = (plugin_settings(stash, None, AI_ID).get("aiMarkerTag") or "").strip() or "Scene Shift"
    for field in ("name", "aliases"):
        got = stash.call("query($f: TagFilterType) { findTags(tag_filter: $f) { tags { id name } } }",
                         {"f": {field: {"value": name, "modifier": "EQUALS"}}})["findTags"]["tags"]
        if got:
            return got[0]["id"], got[0]["name"]
    if not create:
        return None, name
    made = stash.call("mutation($i: TagCreateInput!) { tagCreate(input: $i) { id } }",
                      {"i": {"name": name, "description": "Markers where a scene shifts, added with Better Organizing AI's AI markers."}})
    return made["tagCreate"]["id"], name


def scene_marker_name(scene):
    """The scene's title, else its file name: what AI markers are named after."""
    name = (scene.get("title") or "").strip()
    if not name:
        path = ((scene.get("files") or [{}])[0] or {}).get("path") or f"Scene {scene['id']}"
        name = os.path.splitext(os.path.basename(path))[0]
    return " ".join(name.split())


def run_add_markers(stash, scene_id, markers, tag=None):
    """Adds AI markers to a scene: named "time - scene title", tagged Scene
    Shift (or the setting's tag), each with its picture.
    markers: [{start, end, picture}]. Returns how many were added."""
    scene = stash.call("query($id: ID!) { findScene(id: $id) { id title files { path } tags { id } } }", {"id": str(scene_id)})["findScene"]
    if not scene or not markers:
        return 0
    tag_id = tag or ai_marker_tag(stash)[0]
    name = scene_marker_name(scene)
    extra = []  # (markers start without the scene's tags: you tag each one)
    made = 0
    # one add at a time (e.g. Add all tapped twice, or two open pages): the
    # second waits, then finds the markers already there and skips them
    with locked_json(os.path.join(_ai_data_dir(), "markers_adding.json")):
        have = stash.call("query($id: ID!) { findScene(id: $id) { scene_markers { seconds primary_tag { id } } } }",
                          {"id": str(scene_id)})["findScene"]["scene_markers"]
        taken = [(float(x["seconds"]), (x.get("primary_tag") or {}).get("id")) for x in have]
        for m in markers:
            if any(abs(sec - float(m["start"])) < 1 and tid == str(tag_id) for sec, tid in taken):
                continue  # already there
            stash.call("mutation($i: SceneMarkerCreateInput!) { sceneMarkerCreate(input: $i) { id } }",
                       {"i": {"scene_id": str(scene_id), "title": f"{_marker_clock(m['start'])} - {name}", "seconds": m["start"],
                              "end_seconds": m["end"], "primary_tag_id": tag_id, "tag_ids": extra}})
            taken.append((float(m["start"]), str(tag_id)))
            made += 1
            with contextlib.suppress(Exception):
                run_marker_picture(stash, scene_id, m["start"], m.get("picture"))
    if made < len(markers):
        log("info", f"Scene {scene_id}: {len(markers) - made} AI marker(s) were there already - not added again")
    return made


def run_video_markers_all(stash, plugin_dir, preview=False, limit=None):
    """Task: AI markers (where the scene shifts, each with its picture) for
    every scene that has no markers yet. Scenes you've given markers are
    never touched. The frames looked at are kept, so cancelling and running
    it again carries on quickly."""
    load_auto_seek(plugin_dir)  # fails early if Auto Seek isn't there
    tag_id, tag_name = ai_marker_tag(stash, create=not preview)
    scenes = stash.call('{ findScenes(scene_filter: {has_markers: "false"}, filter: {per_page: -1}) '
                        '{ scenes { id title files { duration } } } }')["findScenes"]["scenes"]
    scenes = [x for x in scenes if x["files"] and (x["files"][0].get("duration") or 0) >= VIDEO_MIN_SECONDS]
    if preview:
        import random  # noqa: PLC0415
        scenes = random.sample(scenes, min(limit or 5, len(scenes)))
    elif limit:
        scenes = scenes[:limit]
    log("info", f"{'Preview: ' if preview else ''}AI markers for {len(scenes)} scene(s) without markers, tagged {tag_name}"
        + (" (nothing is saved)" if preview else ". Up to a minute each for a long scene; you can cancel and run it again to carry on."))
    started, done, made = time.time(), 0, 0
    for n, x in enumerate(scenes):
        progress(n / max(len(scenes), 1))
        label = f'scene {x["id"]}' + (f' "{x["title"]}"' if x.get("title") else "")
        try:
            out = run_video_markers(stash, plugin_dir, x["id"], pictures=not preview)
        except Exception as e:
            log("debug", f"{label}: {e}")
            continue
        ms = out["markers"]
        if preview:
            log("info", f"{label}: " + (", ".join(f'{_marker_clock(m["start"])}-{_marker_clock(m["end"])}' for m in ms) or "no shifts found"))
            continue
        # (you may have added markers since the list was made)
        have = stash.call("query($id: ID!) { findScene(id: $id) { scene_markers { id } } }", {"id": x["id"]})["findScene"]
        if (have or {}).get("scene_markers"):
            continue
        if ms:
            run_add_markers(stash, x["id"], ms, tag_id)
        done += 1
        made += len(ms)
        log("debug", f"{label}: {len(ms)} marker(s)")
    progress(1)
    if not preview:
        log("info", f"Done: {made} marker(s) on {done} scene(s) in {time.time() - started:.0f}s.")


def run_video_markers_redo(stash, plugin_dir, only=None):
    """Task: AI markers made with an older method, made again: for every scene
    with markers under the AI marker tag (Scene Shift), those markers are
    replaced with new ones. Other markers are kept, and no new one is added
    within a few seconds of one of them. Scenes are done one at a time, so
    cancelling leaves each scene either as it was or redone."""
    load_auto_seek(plugin_dir)
    tag_id, tag_name = ai_marker_tag(stash, create=False)
    if not tag_id:
        log("info", f"No markers tagged {tag_name}: nothing to redo.")
        return
    got = stash.call("query($t: [ID!]) { findSceneMarkers(scene_marker_filter: {tags: {value: $t, modifier: INCLUDES}}, "
                     "filter: {per_page: -1}) { scene_markers { scene { id } } } }", {"t": [tag_id]})["findSceneMarkers"]["scene_markers"]
    scenes = sorted({m["scene"]["id"] for m in got}, key=int)
    if only:
        scenes = [x for x in scenes if x in {str(o) for o in only}]
    log("info", f"Redoing the {tag_name} markers of {len(scenes)} scene(s) with the newest method. "
        "Up to a minute or two each; you can cancel and run it again (scenes already redone are done again).")
    started, before_n, after_n, done = time.time(), 0, 0, 0
    failed = {}
    # (a scene that fails - the video or the face helper busy - is tried again at the end)
    for n, sid in enumerate(scenes + [None]):
        if sid is None:
            if not failed:
                break
            log("info", f"Trying {len(failed)} scene(s) again that couldn't be looked at the first time.")
            retry, failed = list(failed), {}
            for sid2 in retry:
                totals = {}
                failed.update(_redo_one(stash, plugin_dir, sid2, tag_id, totals))
                before_n += totals.get("before", 0)
                after_n += totals.get("after", 0)
                done += totals.get("done", 0)
            break
        progress(n / max(len(scenes), 1))
        totals = {}
        failed.update(_redo_one(stash, plugin_dir, sid, tag_id, totals))
        before_n += totals.get("before", 0)
        after_n += totals.get("after", 0)
        done += totals.get("done", 0)
    progress(1)
    if failed:
        log("warning", f"{len(failed)} scene(s) kept their old markers (couldn't be looked at): "
            + "; ".join(f"scene {k}: {v}" for k, v in list(failed.items())[:10]) + ". Run the task again to retry them.")
    log("info", f"Done: {done} scene(s) redone, {before_n} marker(s) replaced by {after_n}, in {time.time() - started:.0f}s.")


def _redo_one(stash, plugin_dir, sid, tag_id, totals):
    """One scene of run_video_markers_redo; {scene: reason} if it couldn't be looked at."""
    try:
        out = run_video_markers(stash, plugin_dir, sid, fresh=False)
    except Exception as e:
        log("debug", f"scene {sid}: {e}")
        return {sid: str(e)[:120]}
    have = stash.call("query($id: ID!) { findScene(id: $id) { scene_markers { id seconds primary_tag { id } } } }",
                      {"id": sid})["findScene"]["scene_markers"]
    mine = [m for m in have if m["primary_tag"]["id"] == tag_id]
    others = [m["seconds"] for m in have if m["primary_tag"]["id"] != tag_id]
    new = [m for m in out["markers"] if all(abs(m["start"] - s) > 16 for s in others)]
    # an old marker at the same second is updated, not deleted and made again:
    # deleting it also deletes its picture file, which is the new one's too
    by_second = {int(m["seconds"]): m for m in mine}
    reuse = {m["start"]: by_second[m["start"]] for m in new if m["start"] in by_second}
    for m in mine:
        if all(m is not o for o in reuse.values()):
            stash.call("mutation($id: ID!) { sceneMarkerDestroy(id: $id) }", {"id": m["id"]})
    if reuse:
        scene = stash.call("query($id: ID!) { findScene(id: $id) { id title files { path } tags { id } } }", {"id": sid})["findScene"]
        name = scene_marker_name(scene)
        for m in new:
            old = reuse.get(m["start"])
            if old:
                # (its tags stay: you may have tagged it; so does its picture)
                with keeping_marker_picture(stash, sid, m["start"]):
                    stash.call("mutation($i: SceneMarkerUpdateInput!) { sceneMarkerUpdate(input: $i) { id } }",
                               {"i": {"id": old["id"], "title": f"{_marker_clock(m['start'])} - {name}", "seconds": m["start"],
                                      "end_seconds": m["end"], "primary_tag_id": tag_id}})
                with contextlib.suppress(Exception):  # (puts its picture back if it went missing)
                    run_marker_picture(stash, sid, m["start"], m.get("picture"))
    fresh_new = [m for m in new if m["start"] not in reuse]
    if fresh_new:
        run_add_markers(stash, sid, fresh_new, tag_id)
    totals.update(before=len(mine), after=len(new), done=1)
    log("debug", f"scene {sid}: {len(mine)} marker(s) -> {len(new)}")
    return {}


# --- adding a marker by hand (scene page → Create Marker) -----------------
def run_marker_suggest(stash, plugin_dir, scene_id, start):
    """For a new marker starting at `start`: the marker it falls inside (to
    end it there), where the next one starts, the next big changes the AI saw
    after it (from the frames AI markers looked at, if any), and the name."""
    scene_id, start = str(scene_id), float(start)
    sc = stash.call("query($id: ID!) { findScene(id: $id) { id title files { path size duration } "
                    "scene_markers { id seconds end_seconds title primary_tag { id name } } } }", {"id": scene_id})["findScene"]
    f = (sc.get("files") or [{}])[0]
    dur = float(f.get("duration") or 0)
    marks = sorted(sc.get("scene_markers") or [], key=lambda m: m["seconds"])
    before = [m for m in marks if m["seconds"] < start - 1]
    prev = before[-1] if before else None
    if prev and prev.get("end_seconds") is not None and prev["end_seconds"] <= start:
        prev = None  # it ends before the new one starts: nothing to change
    nxt = next((m for m in marks if m["seconds"] > start + 1), None)
    changes = []
    with contextlib.suppress(OSError, ValueError, KeyError):
        with open(os.path.join(plugin_dir, "video_frames", f"{scene_id}.json")) as fh:
            saved = json.load(fh)
        scores = {}
        video_shifts(saved["f"], saved["step"], dur, saved.get("c"), scores_out=scores)
        # the strongest changes from 30 s after the start, a minute apart, up to the next marker
        limit = nxt["seconds"] if nxt else dur
        cand = sorted(((d, t) for t, d in scores.items() if start + 30 <= t < limit - 10 and d >= 0.6), reverse=True)
        for d, t in cand:
            if all(abs(t - c["at"]) >= 60 for c in changes):
                changes.append({"at": int(round(t)), "strength": round(d, 2)})
            if len(changes) >= 3:
                break
        changes.sort(key=lambda c: c["at"])
    return {
        "name": f"{_marker_clock(start)} - {scene_marker_name(sc)}",
        "title": scene_marker_name(sc),
        "duration": dur,
        "prev": {"id": prev["id"], "seconds": prev["seconds"], "end": prev.get("end_seconds"),
                 "title": prev.get("title") or prev["primary_tag"]["name"]} if prev else None,
        "next": {"seconds": nxt["seconds"], "title": nxt.get("title") or nxt["primary_tag"]["name"]} if nxt else None,
        "changes": changes,
        "tag": ai_marker_tag(stash, create=False)[1],
    }


def run_marker_add_manual(stash, plugin_dir, scene_id, start, end=None, title=None, trim_prev=True):
    """Adds a marker by hand: tagged like AI markers (Scene Shift), with its
    picture, and (trim_prev) the marker it falls
    inside ends where it starts. Returns what Undo needs."""
    scene_id, start = str(scene_id), int(round(float(start)))
    end = int(round(float(end))) if end not in (None, "") else None
    if end is not None and end <= start:
        raise RuntimeError("the end has to be after the start")
    info = run_marker_suggest(stash, plugin_dir, scene_id, start)
    tag_id, _ = ai_marker_tag(stash)
    scene = stash.call("query($id: ID!) { findScene(id: $id) { id title files { path } tags { id } } }", {"id": scene_id})["findScene"]
    extra = []  # (markers start without the scene's tags: you tag each one)
    name = (title or "").strip() or info["name"]
    made = stash.call("mutation($i: SceneMarkerCreateInput!) { sceneMarkerCreate(input: $i) { id } }",
                      {"i": {"scene_id": scene_id, "title": name, "seconds": start, "end_seconds": end,
                             "primary_tag_id": tag_id, "tag_ids": extra}})["sceneMarkerCreate"]["id"]
    undo = {"id": made}
    prev = info["prev"]
    if trim_prev and prev:
        with keeping_marker_picture(stash, scene_id, prev["seconds"]):
            stash.call("mutation($i: SceneMarkerUpdateInput!) { sceneMarkerUpdate(input: $i) { id } }",
                       {"i": {"id": prev["id"], "end_seconds": start}})
        undo["prev"] = {"id": prev["id"], "end": prev["end"], "scene": scene_id, "seconds": prev["seconds"]}
    with contextlib.suppress(Exception):  # its picture (the slow part: reading the video)
        pic = _marker_pictures(stash, None, scene["files"][0]["path"], [start + 2], [start])[0]
        run_marker_picture(stash, scene_id, start, pic)
    return undo


def run_marker_add_undo(stash, undo):
    """Takes back run_marker_add_manual."""
    undo = undo or {}
    if undo.get("id"):
        with contextlib.suppress(Exception):
            stash.call("mutation($id: ID!) { sceneMarkerDestroy(id: $id) }", {"id": undo["id"]})
    p = undo.get("prev")
    if p:
        with keeping_marker_picture(stash, p.get("scene"), p.get("seconds", 0)) if p.get("scene") else contextlib.nullcontext():
            stash.call("mutation($i: SceneMarkerUpdateInput!) { sceneMarkerUpdate(input: $i) { id } }",
                       {"i": {"id": p["id"], "end_seconds": p.get("end")}})
    return True


# --- a marker's tags go on its scene too ---------------------------------
# A marker is a part of its scene, so what's in it is in the scene: a tag you
# give a marker that the scene doesn't have is added to the scene (by the
# "Marker pictures" hook as you tag, and the task for markers you tagged
# before). Not the AI markers' own tag (Scene Shift), excluded tags, or tags
# you took off that scene; recorded like the add-on's other tags, so taking
# one off the scene keeps it off.
def marker_tags_to_scene(stash, plugin_dir, marker_ids=None, preview=False):
    """Adds markers' tags their scenes lack (all markers when marker_ids is
    None). Returns {scene id: [tag ids added]}."""
    cfg = settings(stash, plugin_dir)
    names, excluded = tag_lookup(stash, cfg, plugin_dir)
    ai_tag = ai_marker_tag(stash, create=False)[0]
    fields = "id primary_tag { id } tags { id } scene { id tags { id } }"
    if marker_ids is None:
        markers = stash.call(f"{{ findSceneMarkers(filter: {{per_page: -1}}) {{ scene_markers {{ {fields} }} }} }}")[
            "findSceneMarkers"]["scene_markers"]
    else:
        markers = stash.call(f"query($ids: [ID!]) {{ findSceneMarkers(ids: $ids) {{ scene_markers {{ {fields} }} }} }}",
                             {"ids": [str(i) for i in marker_ids]})["findSceneMarkers"]["scene_markers"]
    want = {}
    for m in markers:
        sid = m["scene"]["id"]
        have = {x["id"] for x in m["scene"].get("tags") or []}
        tags = {m["primary_tag"]["id"]} | {x["id"] for x in m.get("tags") or []}
        tags -= have | set(excluded) | {ai_tag} | removed_tags("scene", sid, have)
        if tags:
            want.setdefault(sid, set()).update(tags)
    out = {sid: sorted(t, key=int) for sid, t in want.items()}
    for sid, tags in out.items():
        label = ", ".join(names.get(t, t) for t in tags)
        if preview:
            log("info", f"scene {sid} would get {label}")
            continue
        stash.call("mutation($i: BulkSceneUpdateInput!) { bulkSceneUpdate(input: $i) { id } }",
                   {"i": {"ids": [sid], "tag_ids": {"ids": tags, "mode": "ADD"}}})
        record_machine_tags(plugin_dir, "scene", sid, tags)
        log("info", f"scene {sid}: added {label} (from its markers)")
    return out


def run_marker_tags_to_scenes(stash, plugin_dir, preview=False):
    out = marker_tags_to_scene(stash, plugin_dir, None, preview)
    n = sum(len(v) for v in out.values())
    log("info", f"Markers' tags: {n} tag(s) {'would be added' if preview else 'added'} to {len(out)} scene(s)"
        if out else "Markers' tags: every scene already has its markers' tags")


# --- tags for markers (Review → Tags → Markers) ----------------------------
# Each marker is one part of a scene, so its look-alikes say more about it than
# the whole scene's do. A marker's picture fingerprint is the average of the
# fingerprints of the frames AI markers looked at inside it (video_frames/);
# its look-alikes are the tagged scenes and photos and the markers you've
# tagged - never its own scene, which would only hand back the scene's tags.
# Markers you tag teach the next suggestions. Positions come from Auto Seek's
# model's readings of the same frames.
MARKER_POS_SHARE = 0.5  # a position is suggested when it's in at least half the marker's frames
# tags about the people rather than what's happening (that's the scene's job,
# and a moment's look-alikes get them wrong): not suggested for markers
MARKER_SKIP_TAGS = re.compile(
    r"(?i)\b(hair|haired|hairy|hairless|shaved|trimmed|bald|blonde?|brunette|redhead|ginger|eyes?|"
    r"skin|tan|tanned|pale|freckles?|asian|latina|latino|ebony|caucasian|white|black|native|indian|arab|"
    r"tattoo(s|ed)?|pierc\w*|petite|curvy|thick|skinny|slim|chubby|bbw|athletic|muscular|fit|average|"
    r"short|tall|milf|teen|mature|granny|cougar|young|old|natural|fake)\b")


def marker_looks(stash, plugin_dir, markers):
    """{marker id: {"e": q8 fingerprint or None, "looked": frames, "acts": {position: frames}}},
    from the frames saved for AI markers; kept in marker_looks.json."""
    path = os.path.join(plugin_dir, "marker_looks.json")
    try:
        with open(path) as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    by_scene = defaultdict(list)
    for m in markers:
        by_scene[m["scene"]["id"]].append(m)
    out, changed, order = {}, False, None
    for sid, ms in by_scene.items():
        ms.sort(key=lambda m: m["seconds"])
        saved = None
        fpath = os.path.join(plugin_dir, "video_frames", f"{sid}.json")
        size = None
        with contextlib.suppress(OSError):
            size = os.path.getsize(fpath)
        for k, m in enumerate(ms):
            end = m.get("end_seconds") or (ms[k + 1]["seconds"] if k + 1 < len(ms) else m["seconds"] + 60)
            key = f"{m['seconds']}|{end}|{size}"
            hit = cache.get(m["id"])
            if hit and hit.get("k") == key:
                out[m["id"]] = hit
                continue
            if size is None:
                continue  # no frames saved for this scene (yet)
            if saved is None:
                try:
                    with open(fpath) as f:
                        saved = json.load(f)
                except (OSError, ValueError):
                    saved = {}
                if order is None:
                    order = list(load_auto_seek(plugin_dir).ACTS)
            rows, clips = saved.get("f") or [], saved.get("c") or []
            inside = [n for n, r in enumerate(rows) if m["seconds"] <= r[0] < end]
            e = None
            if clips and len(clips) == len(rows) and inside:
                vecs = [_unclip(clips[n]) for n in inside]
                mean = [sum(col) / len(vecs) for col in zip(*vecs)]
                norm = math.sqrt(sum(x * x for x in mean)) or 1
                e = q8([x / norm for x in mean])
            started = [rows[n] for n in inside if rows[n][1] >= VIDEO_STARTED]
            acts = {}
            for act, rule in VIDEO_ACTS.items():
                i = order.index(act)
                acts[act] = sum(1 for r in started if r[2][i] >= rule["share"])
            ent = {"k": key, "e": e, "looked": len(started), "acts": acts}
            cache[m["id"]] = ent
            out[m["id"]] = ent
            changed = True
    if changed:
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, separators=(",", ":"))
        _replace(tmp, path)
    return out


MARKER_REVIEW_FIELDS = "id title seconds end_seconds scene { id tags { id } } primary_tag { id } tags { id }"
# A marker is part of its scene, so the scene's tags hint at what's in it: a
# tag the scene has is suggested for a marker with less look-alike support
# (this share of LOOK_SURE, or of MARKER_POS_SHARE for positions), and its
# score is lifted this much of the way to 100%. The scene's tags alone are
# never enough - the marker has to look like it too.
MARKER_SCENE_EASE = 0.6
MARKER_SCENE_LIFT = 0.3


def marker_scene_tags(stash, cfg, plugin_dir, excluded, fresh=False):
    """What each scene's markers look like, as tags for the scene: {scene id:
    {tag: {"score": best marker's 0-1, "count": markers sure of it, "times":
    [seconds], "picture": the best marker's picture, "near": its look-alikes
    with the tag}}} - from the markers' own look-alikes (your tagged scenes,
    photos and markers; positions are left to the video). {} without markers
    or look-alikes."""
    try:
        ai_tag = ai_marker_tag(stash, create=False)[0]
        markers = stash.call(f"{{ findSceneMarkers(filter: {{per_page: -1}}) {{ scene_markers {{ {MARKER_REVIEW_FIELDS} screenshot }} }} }}"
                             )["findSceneMarkers"]["scene_markers"]
        if not markers:
            return {}
        found, _ = _marker_tag_candidates(stash, cfg, plugin_dir, excluded, markers, ai_tag, fresh)
    except Exception as e:
        log("debug", f"tags from markers left out: {e}")
        return {}
    by_id = {m["id"]: m for m in markers}
    out = {}
    for mid, t, sc, near, frames, looked, from_scene in found:
        m = by_id.get(mid)
        if not m or frames or from_scene:  # (positions: the scene's own video picks cover those)
            continue
        e = out.setdefault(m["scene"]["id"], {}).setdefault(t, {"score": 0.0, "count": 0, "times": [], "picture": "", "near": 0})
        e["count"] += 1
        e["times"].append(round(float(m["seconds"])))
        if sc > e["score"]:
            e.update(score=sc, picture=m.get("screenshot") or "", near=near)
    for tags in out.values():
        for e in tags.values():
            e["times"].sort()
    return out


def _marker_tag_candidates(stash, cfg, plugin_dir, excluded, markers, ai_tag, fresh=False):
    """[[marker id, tag, score 0-1, look-alikes with it, frames, looked, 1 if
    its scene has the tag]] and tag names, kept 15 minutes."""
    path = os.path.join(plugin_dir, "marker_review_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS and saved.get("v") == 2:
                return saved["found"], saved["names"]
    looks = LookIndex(stash, cfg, plugin_dir, excluded)
    if not looks.ready():
        raise NotReady('not enough look-alikes yet - run "Tags: scan scenes and images for look-alikes (do this first)" first')
    ml = marker_looks(stash, plugin_dir, markers)
    scene_of = {m["id"]: m["scene"]["id"] for m in markers}
    scene_tags = {m["id"]: {x["id"] for x in (m["scene"].get("tags") or [])} for m in markers}
    # what to learn from: your tagged scenes and photos, and your tagged markers
    train = [(r[0], looks.cache.data[r[0]]["e"], r[2]) for r in looks.rows]
    for m in markers:
        t = {x["id"] for x in m.get("tags") or []} - {ai_tag} - set(excluded)
        e = (ml.get(m["id"]) or {}).get("e")
        if t and e:
            train.append((f"marker:{m['id']}", e, frozenset(t)))
    queries = [m for m in markers if (ml.get(m["id"]) or {}).get("e")]
    found, names = [], dict(looks.names)
    if queries and train:
        vectors = [x[1] for x in train] + [ml[m["id"]]["e"] for m in queries]
        nn = look_knn(cfg, vectors, list(range(len(train))), LOOK_K + 15)[len(train):]

        def same_scene(key, sid):
            kind, _, xid = key.partition(":")
            return (kind == "scene" and xid == sid) or (kind == "marker" and scene_of.get(xid) == sid)

        for m, near in zip(queries, nn):
            sid = m["scene"]["id"]
            near = [(j, d) for j, d in near if not same_scene(train[j][0], sid)][:LOOK_K]
            if not near:
                continue
            top = near[0][1] / 16129
            votes, total, count = defaultdict(float), 0.0, Counter()
            for j, dot in near:
                w = math.exp((dot / 16129 - top) / LOOK_TEMP)
                total += w
                for t in train[j][2]:
                    votes[t] += w
                    count[t] += 1
            in_scene = scene_tags.get(m["id"], set())
            for t, w in votes.items():
                sc, has = w / total, t in in_scene
                if sc >= (LOOK_SURE * MARKER_SCENE_EASE if has else LOOK_SURE) and t in looks.names and t not in looks.rejected:
                    if has:
                        sc += (1 - sc) * MARKER_SCENE_LIFT
                    found.append([m["id"], t, round(sc, 3), count[t], 0, 0, int(has)])
    # positions the video model saw in the marker
    with contextlib.suppress(Exception):
        for act, (t, _) in _video_tag_ids(stash).items():
            names.setdefault(t["id"], t["name"])
            for m in markers:
                ent = ml.get(m["id"]) or {}
                looked, n = ent.get("looked") or 0, (ent.get("acts") or {}).get(act, 0)
                has = t["id"] in scene_tags.get(m["id"], set())
                if looked >= 2 and n >= max(2, looked * MARKER_POS_SHARE * (MARKER_SCENE_EASE if has else 1)):
                    sc = 0.5 + 0.5 * n / looked
                    if has:
                        sc += (1 - sc) * MARKER_SCENE_LIFT
                    found.append([m["id"], t["id"], round(sc, 3), 0, n, looked, int(has)])
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "v": 2, "found": found, "names": names}, f)
    _replace(tmp, path)
    return found, names


def run_marker_tag_review(stash, plugin_dir, tag=None, limit=40, skip=(), fresh=False):
    """Review page, Tags → Markers: tags for markers, from the marker's own
    look-alikes and the positions seen in it. Same answers as Tags →
    Suggested (No is remembered). Best first; one card per marker and tag."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    ai_tag = ai_marker_tag(stash, create=False)[0]
    markers = stash.call(f"{{ findSceneMarkers(filter: {{per_page: -1}}) {{ scene_markers {{ {MARKER_REVIEW_FIELDS} }} }} }}")["findSceneMarkers"]["scene_markers"]
    if not markers:
        raise RuntimeError('no markers yet - add some with "AI markers" on a scene, or the AI markers task')
    found, names = _marker_tag_candidates(stash, cfg, plugin_dir, excluded, markers, ai_tag, fresh)
    by_id = {m["id"]: m for m in markers}
    position_kin = {}  # a position tag -> every tag that counts as having it
    with contextlib.suppress(Exception):
        for t, counts in _video_tag_ids(stash).values():
            position_kin[t["id"]] = counts
    with locked_tag_review_not(plugin_dir) as nots:
        nots = {k: set(v) for k, v in nots.items()}
    skip, want = set(skip or ()), (str(tag) if tag else None)
    merged = {}
    for mid, t, sc, n, frames, looked, from_scene in found:
        m = by_id.get(mid)
        if not m or (want and t != want):
            continue
        key = f"marker:{mid}"
        have = {x["id"] for x in m.get("tags") or []} | {m["primary_tag"]["id"]}
        tname = names.get(t, "").strip()
        if t in excluded or t == ai_tag or tname.lower() in BODY_TAG_NAMES or MARKER_SKIP_TAGS.search(tname):
            continue
        if t in have or have & position_kin.get(t, set()) or t in nots.get(key, ()) or f"{key}:{t}" in skip:
            continue
        cur = merged.setdefault((mid, t), {"sc": 0.0, "n": 0, "frames": 0, "looked": 0})
        cur["sc"] = max(cur["sc"], sc)
        cur["scene"] = cur.get("scene") or bool(from_scene)
        cur["n"] = max(cur["n"], n)
        if frames:
            cur["frames"], cur["looked"] = frames, looked
    # poses learnt from the markers and photos you've tagged with one
    pose_found = pose_suggestions(stash, cfg, plugin_dir, excluded, fresh, kind="marker")
    pose_names = pose_tags(stash) if pose_found else {}
    for key, t, sc, why in pose_found:
        mid = key.split(":")[1]
        m = by_id.get(mid)
        if not m or (want and t != want) or (mid, t) in merged:
            continue
        have = {x["id"] for x in m.get("tags") or []} | {m["primary_tag"]["id"]}
        if t in have or t in nots.get(key, ()) or f"{key}:{t}" in skip:
            continue
        names.setdefault(t, pose_names.get(t, t))
        merged[(mid, t)] = {"sc": sc, "n": 0, "frames": 0, "looked": 0, "why": why}
    # both the look-alikes and the video agree: first
    picks = sorted(merged.items(), key=lambda x: (-(x[1]["n"] > 0 and x[1]["frames"] > 0), -x[1]["sc"], -x[1]["n"]))
    out = []
    for (mid, t), v in picks[:limit]:
        m = by_id[mid]
        sid = m["scene"]["id"]
        out.append({
            "kind": "marker", "id": mid, "scene": sid, "start": m["seconds"], "tag": t, "name": names.get(t, ""),
            "score": round(v["sc"] * 100), "near": v["n"], "times": [round(m["seconds"])],
            **({"frames": v["frames"], "looked": v["looked"]} if v["frames"] else {}),
            **({"why": v["why"]} if v.get("why") else {}),
            **({"scene_has": True} if v.get("scene") else {}),
            "title": m.get("title") or f"Marker at {_marker_clock(m['seconds'])}",
            "picture": f"/scene/{sid}/scene_marker/{mid}/screenshot",
            "tags": len([x for x in m.get("tags") or [] if x["id"] != ai_tag]),
        })
    per = Counter(t for (_, t) in merged)
    groups = [{"id": t, "name": names.get(t, t), "count": c} for t, c in per.most_common()]
    return {"items": out, "total": len(merged), "groups": groups}


# --- poses (Review → Tags → Suggested / Markers): learnt from the pictures you tag
# Neither Apple's body pose nor CLIP's descriptions can tell "face down, ass
# up" or "frog pose" on their own (checked on 700 of your pictures: Apple
# finds a whole body in about 1 in 6, CLIP's descriptions mostly find
# bottoms). So poses are suggested from examples you tag: photos and markers
# whose body shape (Apple's joints, on a Mac) or look (picture fingerprints)
# is closest to ones with the pose. Suggestions only, never added by a task;
# Yes adds an example, No is remembered and counts against it.
POSE_TAGS_DEFAULT = ("Face Down Ass Up, All Fours, Frog Pose, Kneeling, Squatting, Spread Legs, Legs Up, "
                     "Bent Over, Lying on Stomach")
POSE_MIN_EXAMPLES = 3      # tagged examples needed before a pose is suggested
POSE_JOINTS = ("head", "neck", "left_shoulder", "right_shoulder", "left_forearm", "right_forearm", "left_hand",
               "right_hand", "left_upLeg", "right_upLeg", "left_leg", "right_leg", "left_foot", "right_foot")
POSE_JOINT_SURE = 0.3
POSE_MIN_TORSO = 0.1       # a person smaller than this (torso / longer side) is in the background
POSE_SHARED = 8            # joints both bodies need, a knee among them
POSE_K = 10                # closest bodies that vote
POSE_TEMP = 0.1            # how fast a vote fades with distance (torso lengths)
POSE_BACKGROUND = 500      # bodies without the pose they're compared with
POSE_SUGGEST = 0.3         # shown when the body shape or the look-alikes give this much
POSE_SCAN_BATCH = 16


def pose_tag_names(stash):
    raw = plugin_settings(stash, None, AI_ID).get("poseTags")
    raw = raw if isinstance(raw, str) and raw.strip() else POSE_TAGS_DEFAULT
    return [n.strip() for n in raw.split(",") if n.strip()]


def pose_tags(stash):
    """{tag id: name} for the pose tags you have (by name or alias)."""
    want = {n.lower() for n in pose_tag_names(stash)}
    tags = stash.call("{ findTags(filter: {per_page: -1}) { tags { id name aliases } } }")["findTags"]["tags"]
    return {t["id"]: t["name"] for t in tags
            if t["name"].strip().lower() in want or any(a.strip().lower() in want for a in t.get("aliases") or [])}


def pose_vector(person):
    """One person's joints around the hips, in torso lengths (None if the
    torso isn't seen or the person is small, i.e. in the background)."""
    def g(n):
        v = person.get(n)
        return (v[0], v[1]) if v and v[2] >= POSE_JOINT_SURE else None

    hips = (g("left_upLeg"), g("right_upLeg"))
    hip = ((hips[0][0] + hips[1][0]) / 2, (hips[0][1] + hips[1][1]) / 2) if all(hips) else g("root")
    sh = (g("left_shoulder"), g("right_shoulder"))
    top = g("neck") or (((sh[0][0] + sh[1][0]) / 2, (sh[0][1] + sh[1][1]) / 2) if all(sh) else None)
    if not hip or not top:
        return None
    size = math.hypot(top[0] - hip[0], top[1] - hip[1])
    if size < POSE_MIN_TORSO:
        return None
    return {n: [round((v[0] - hip[0]) / size, 3), round((v[1] - hip[1]) / size, 3)] for n in POSE_JOINTS if (v := g(n))}


def _pose_mirror(v):
    swap = lambda n: "right_" + n[5:] if n.startswith("left_") else "left_" + n[6:] if n.startswith("right_") else n
    return {swap(n): (-x, y) for n, (x, y) in v.items()}


def pose_distance(a, b, b_mirror=None):
    """How far apart two bodies are (torso lengths, either way round), or None
    when too few joints are seen in both."""
    best = None
    for bb in (b, b_mirror if b_mirror is not None else _pose_mirror(b)):
        shared = [n for n in a if n in bb]
        if len(shared) < POSE_SHARED or not ("left_leg" in shared or "right_leg" in shared):
            continue
        d = sum(math.hypot(a[n][0] - bb[n][0], a[n][1] - bb[n][1]) for n in shared) / len(shared)
        best = d if best is None else min(best, d)
    return best


def _pose_cache(plugin_dir):
    try:
        with open(os.path.join(plugin_dir, "poses.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pose_save(plugin_dir, cache):
    path = os.path.join(plugin_dir, "poses.json")
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, separators=(",", ":"))
    _replace(tmp, path)


def apple_poses(cfg, pictures):
    """Everyone's body joints in each picture (Apple's Vision via the face helper)."""
    body = json.dumps({"images": [base64.b64encode(p).decode() for p in pictures], "pose_only": True}).encode()
    req = urllib.request.Request(cfg["ai"]["face_url"] + "/apple", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as e:
        if e.code in (404, 501):
            raise RuntimeError("the face helper can't read body poses: poses need Apple's Vision on a Mac "
                               "(run face-helper/setup.sh again there)")
        raise


def _pose_targets(stash):
    """[(key, signature, picture url)] for every photo (not drawn) and marker."""
    out = []
    page = 1
    while True:
        items = stash.call("query($f: FindFilterType) { findImages(filter: $f) { images { id tags { id name } "
                           "paths { thumbnail } visual_files { __typename ... on ImageFile { path } ... on VideoFile { path } } } } }",
                           {"f": {"page": page, "per_page": 1000, "sort": "id"}})["findImages"]["images"]
        for i in items:
            files = i.get("visual_files") or []
            if files and all(f["__typename"] == "ImageFile" for f in files) and not is_drawn(i):
                out.append((f"image:{i['id']}", files[0]["path"], i["paths"]["thumbnail"]))
        if len(items) < 1000:
            break
        page += 1
    markers = stash.call("{ findSceneMarkers(filter: {per_page: -1}) { scene_markers { id seconds screenshot scene { id } } } }")
    for m in markers["findSceneMarkers"]["scene_markers"]:
        out.append((f"marker:{m['id']}", f"{m['scene']['id']}@{int(float(m['seconds']))}", m["screenshot"]))
    return out


def run_pose_scan(stash, plugin_dir):
    """Task: Apple's body pose for every photo and marker not done yet (kept in
    poses.json; a picture that changes is done again)."""
    cfg = settings(stash, plugin_dir)
    cache = _pose_cache(plugin_dir)
    targets = _pose_targets(stash)
    keys = {k for k, _, _ in targets}
    for k in [k for k in cache if k not in keys]:
        del cache[k]  # (deleted since)
    todo = [t for t in targets if (cache.get(t[0]) or {}).get("s") != t[1]]
    if not todo:
        _pose_save(plugin_dir, cache)
        log("info", f"Nothing to do: all {len(targets)} photos and markers have their body poses.")
        return
    log("info", f"Body poses for {len(todo)} photo(s) and marker(s) ({len(targets) - len(todo)} done before); "
        "cancel and run again to carry on.")
    found = failed = 0
    for at in range(0, len(todo), POSE_SCAN_BATCH):
        progress(at / len(todo))
        batch, pictures = [], []
        for key, sig, url in todo[at:at + POSE_SCAN_BATCH]:
            try:
                data, ctype = stash.fetch(url)
            except Exception as e:
                failed += 1
                log("debug", f"{key}: no picture ({e})")
                continue
            if ctype == "text/html":
                raise RuntimeError("Stash sent a web page instead of the picture (signed out?)")
            if ctype.startswith("image/"):
                batch.append((key, sig))
                pictures.append(data)
        if not pictures:
            continue
        for (key, sig), res in zip(batch, apple_poses(cfg, pictures)):
            vecs = [v for p in (res or {}).get("people") or [] if (v := pose_vector(p))]
            cache[key] = {"s": sig, "v": vecs}
            found += bool(vecs)
        if (at // POSE_SCAN_BATCH) % 20 == 19:
            _pose_save(plugin_dir, cache)
    _pose_save(plugin_dir, cache)
    progress(1)
    log("info", f"Body poses done: a whole enough body in {found} of {len(todo)} picture(s)"
        + (f"; {failed} couldn't be fetched" if failed else "") + ". Tag a few with each pose to get suggestions "
        "(Review → Tags → Suggested, and Markers).")


def _pose_candidates(stash, cfg, plugin_dir, excluded, fresh=False):
    """[[key, tag, score, body share, look share, examples]] for every photo and
    marker, kept 15 minutes. key: image:ID / marker:ID."""
    path = os.path.join(plugin_dir, "pose_review_cache.json")
    if not fresh:
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path) as f:
                saved = json.load(f)
            if time.time() - saved.get("at", 0) < TAG_REVIEW_CACHE_SECONDS:
                return saved["found"], saved["names"], saved["few"]
    names = {t: n for t, n in pose_tags(stash).items() if t not in excluded}
    if not names:
        raise RuntimeError("no pose tags yet: make tags named " + ", ".join(pose_tag_names(stash)[:5])
                           + "… (or set your own in the add-on's settings, \"Poses: pose tags to suggest\"), then tag a few pictures with each")
    markers = stash.call(f"{{ findSceneMarkers(filter: {{per_page: -1}}) {{ scene_markers {{ {MARKER_REVIEW_FIELDS} }} }} }}")[
        "findSceneMarkers"]["scene_markers"]
    scene_of = {f"marker:{m['id']}": m["scene"]["id"] for m in markers}
    have = {k: v for k, v in all_item_tags(stash).items() if k.startswith("image:")}
    for m in markers:
        have[f"marker:{m['id']}"] = {x["id"] for x in m.get("tags") or []} | {m["primary_tag"]["id"]}
    with locked_tag_review_not(plugin_dir) as nots:
        nots = {k: set(v) for k, v in nots.items()}
    examples = {t: {k for k, ts in have.items() if t in ts} for t in names}
    against = {t: {k for k, ts in nots.items() if t in ts} for t in names}
    few = {names[t]: len(ex) for t, ex in examples.items() if len(ex) < POSE_MIN_EXAMPLES}
    ready = [t for t in names if len(examples[t]) >= POSE_MIN_EXAMPLES]
    found = []
    if ready:
        # body shape: each body's closest among the examples, the ones turned
        # down and a sample of everyone else
        poses = _pose_cache(plugin_dir)
        bodies = [(k, v) for k, ent in poses.items() for v in ent.get("v") or []]
        import random
        rnd = random.Random(7)
        body_score = defaultdict(float)
        if bodies:
            labelled = {k for t in ready for k in examples[t] | against[t]}
            sample = rnd.sample(bodies, min(POSE_BACKGROUND, len(bodies)))
            base = [(k, v, _pose_mirror(v)) for k, v in bodies if k in labelled]
            base += [(k, v, _pose_mirror(v)) for k, v in sample if k not in labelled]
            for key, v in bodies:
                near = sorted((d, k) for k, b, bm in base if k != key and (d := pose_distance(v, b, bm)) is not None)[:POSE_K]
                if not near:
                    continue
                w = [math.exp(-(d - near[0][0]) / POSE_TEMP) for d, _ in near]
                total = sum(w)
                for t in ready:
                    yes = sum(wi for wi, (_, k) in zip(w, near) if k in examples[t])
                    no = sum(wi for wi, (_, k) in zip(w, near) if k in against[t])
                    sc = max(0.0, yes - no) / total
                    if sc > body_score[(key, t)]:
                        body_score[(key, t)] = sc
        # look-alikes: the photo / marker's closest picture fingerprints
        look_score = {}
        cache = LookCache(plugin_dir)
        ml = marker_looks(stash, plugin_dir, markers)
        vec_keys, vectors = [], []
        for k, ent in cache.data.items():
            if k.startswith("image:") and ent.get("e"):
                vec_keys.append(k)
                vectors.append(ent["e"])
        for m in markers:
            e = (ml.get(m["id"]) or {}).get("e")
            if e:
                vec_keys.append(f"marker:{m['id']}")
                vectors.append(e)
        if vectors:
            nn = look_knn(cfg, vectors, list(range(len(vectors))), LOOK_K + 10)
            for key, near in zip(vec_keys, nn):
                sid = scene_of.get(key)
                near = [(j, d) for j, d in near if vec_keys[j] != key and not (sid and scene_of.get(vec_keys[j]) == sid)][:LOOK_K]
                if not near:
                    continue
                top = near[0][1] / 16129
                w = [math.exp((d / 16129 - top) / LOOK_TEMP) for _, d in near]
                total = sum(w)
                for t in ready:
                    yes = sum(wi for wi, (j, _) in zip(w, near) if vec_keys[j] in examples[t])
                    no = sum(wi for wi, (j, _) in zip(w, near) if vec_keys[j] in against[t])
                    sc = max(0.0, yes - no) / total
                    if sc > 0:
                        look_score[(key, t)] = (sc, sum(1 for j, _ in near if vec_keys[j] in examples[t]))
        for (key, t) in set(body_score) | set(look_score):
            b = body_score.get((key, t), 0.0)
            lk, n = look_score.get((key, t), (0.0, 0))
            sc = 1 - (1 - b) * (1 - lk)
            if sc >= POSE_SUGGEST and (key in scene_of or key.startswith("image:")):
                found.append([key, t, round(sc, 3), round(b, 2), round(lk, 2), n])
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump({"at": time.time(), "found": found, "names": names, "few": few}, f)
    _replace(tmp, path)
    return found, names, few


def pose_suggestions(stash, cfg, plugin_dir, excluded, fresh=False, kind=None):
    """[(key, tag, score 0-1, why)] pose suggestions for photos and/or markers,
    for Review → Tags → Suggested (photos) and → Markers (markers); [] when
    there are no pose tags or examples yet."""
    try:
        found, _, _ = _pose_candidates(stash, cfg, plugin_dir, excluded, fresh)
    except Exception as e:
        log("debug", f"poses left out: {e}")
        return []
    out = []
    for key, t, sc, b, lk, n in found:
        if kind and not key.startswith(kind + ":"):
            continue
        why = " · ".join(x for x in (f"pose: body shape {round(b * 100)}% like the ones you tagged" if b else "",
                                     f"{n} of its 20 closest look-alikes have the pose" if n else "") if x) or "pose"
        out.append((key, t, sc, why))
    return out


def run_pose_review(stash, plugin_dir, tag=None, limit=40, skip=(), fresh=False):
    """Review page, Tags → Poses: poses for photos and markers, from the body
    shapes and looks of the ones you've tagged. Same answers as Tags →
    Suggested (No is remembered, and counts against the pose next time)."""
    cfg = settings(stash, plugin_dir)
    _, excluded = tag_lookup(stash, cfg, plugin_dir)
    found, names, few = _pose_candidates(stash, cfg, plugin_dir, excluded, fresh)
    with locked_tag_review_not(plugin_dir) as nots:
        nots = {k: set(v) for k, v in nots.items()}
    skip, want = set(skip or ()), (str(tag) if tag else None)
    picks = sorted((x for x in found if (not want or x[1] == want) and x[1] not in nots.get(x[0], ())
                    and f"{x[0]}:{x[1]}" not in skip), key=lambda x: (-x[2], -x[5]))
    out = []
    for at in range(0, len(picks), 150):
        chunk = picks[at:at + 150]
        ids = {kind: sorted({k.split(":")[1] for k, *_ in chunk if k.startswith(kind + ":")}, key=int) for kind in ("image", "marker")}
        images = {x["id"]: x for x in find_by_ids(stash, "findImages", "images", ids["image"], TAG_REVIEW_FIELDS["image"])} if ids["image"] else {}
        markers = {}
        if ids["marker"]:
            got = stash.call(f"query($f: SceneMarkerFilterType) {{ findSceneMarkers(scene_marker_filter: $f, filter: {{per_page: -1}}) "
                             f"{{ scene_markers {{ {MARKER_REVIEW_FIELDS} }} }} }}", {"f": {}})["findSceneMarkers"]["scene_markers"]
            markers = {m["id"]: m for m in got if m["id"] in set(ids["marker"])}
        for key, t, sc, b, lk, n in chunk:
            kind, iid = key.split(":")
            why = " · ".join(x for x in (f"body shape {round(b * 100)}% like your examples" if b else "",
                                         f"{n} of its 20 closest look-alikes have it" if n else "") if x)
            if kind == "image":
                it = images.get(iid)
                if not it or t in {x["id"] for x in it.get("tags") or []}:
                    continue
                files = it.get("visual_files") or [{}]
                card = {"title": it.get("title") or (files[0] or {}).get("basename") or "",
                        "picture": re.sub(r"^https?://[^/]+", "", (it.get("paths") or {}).get("image") or ""),
                        "tags": len(it.get("tags") or [])}
            else:
                m = markers.get(iid)
                if not m or t in {x["id"] for x in m.get("tags") or []} | {m["primary_tag"]["id"]}:
                    continue
                sid = m["scene"]["id"]
                card = {"scene": sid, "start": m["seconds"], "times": [round(m["seconds"])],
                        "title": m.get("title") or f"Marker at {_marker_clock(m['seconds'])}",
                        "picture": f"/scene/{sid}/scene_marker/{iid}/screenshot", "tags": len(m.get("tags") or [])}
            out.append({"kind": kind, "id": iid, "tag": t, "name": names.get(t, ""), "score": round(sc * 100),
                        "pose": True, "why": why, **card})
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    per = Counter(x[1] for x in picks)
    groups = [{"id": t, "name": names.get(t, t), "count": c} for t, c in per.most_common()]
    note = ""
    if few:
        note = "Tag at least " + str(POSE_MIN_EXAMPLES) + " pictures or markers with a pose to get suggestions for it - still needed: " + \
            ", ".join(f"{n} ({c} so far)" for n, c in sorted(few.items()))
    have_names = {n.strip().lower() for n in names.values()}
    missing = [n for n in pose_tag_names(stash) if n.lower() not in have_names]
    if missing:
        note += (". " if note else "") + "Make a tag to use these too: " + ", ".join(missing)
    return {"items": out, "total": len(picks), "groups": groups, "note": note}


# --- chapters for galleries (a gallery's Chapters tab → Suggest chapters) ---
# Stash's chapters point at an image's place in the gallery sorted by file
# path (checked: chapter 5 opens the 5th image by path). Between each pair of
# neighbouring images, these say a new chapter starts: a different folder,
# file names that change ("beach_" to "pool_") or start counting again, a
# different look (the picture fingerprints: images after it like none just
# before it, so mixed close-ups don't count), different performers or studio,
# much changed tags, or a day or more between their dates.
CHAPTER_WINDOW = 3
CHAPTER_NEW_LOOK = 0.30
CHAPTER_GENERIC = re.compile(r"(?i)^(img|image|dsc|dscn|dcim|photo|pic|pict|screenshot|screen shot|scan|page|p|_?macosx|"
                             r"locked( folder)?( \d+)?|\d+_locked folder( \d+)?|untitled|new folder|folder|copy|ok|edit(ed)?|"
                             r"img_e|full|large|original|hd|uhd|\W*)$")


def _name_stem(path):
    """A file name without its numbers and punctuation: "Beach_012 (2).jpg" -> "beach"."""
    base = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[\d_\-.()\[\]\s]+", " ", base).strip().lower()
    return re.sub(r"\s+", " ", stem)


def _name_number(path):
    nums = re.findall(r"\d+", os.path.splitext(os.path.basename(path))[0])
    return int(nums[-1]) if nums else None


def _folder_label(folder):
    last = os.path.basename(folder.rstrip("/")) or folder
    last = re.sub(r"\.(zip|cbz|rar|7z)$", "", last, flags=re.I).strip()
    # camera rolls, phone exports, dates and the like say nothing about what's in it
    if CHAPTER_GENERIC.match(last) or re.search(r"(?i)locked|dcim|camera|export|__macosx|^\d{4}[\W_]|^\d+$|^[\W\d_]+$", last):
        return None
    return re.sub(r"[_]+", " ", last).strip()


# Comics (a zip / cbz of pages): their chapters say so on the page ("CHAPTER
# 2: FED UP"). The face helper reads the text (Apple's text reader, on a Mac:
# about 0.2 s a page); a heading counts when its number follows the last one,
# so "chapter" in a speech bubble doesn't start one. What was read is kept
# (comic_text.json), a few hundred pages per request.
COMIC_HEADING = re.compile(r"(?i)^\W*(chapter|chap\.?|ch\.|episode|ep\.|act|part)\s*(\d{1,3})\b\s*[:.\-–—]?\s*(.*)$")
COMIC_CREDITS = re.compile(r"(?i)\b(tl:|ts:|pr:|translat\w*|scanlat\w*|support the artist|typeset\w*|proofread\w*|raws?:|redraw\w*)")
COMIC_PAGES_PER_CALL = 200


def _comic_reader(cfg):
    """True when the face helper can read text."""
    with contextlib.suppress(Exception):
        with urllib.request.urlopen(cfg["ai"]["face_url"] + "/health", timeout=5) as resp:
            return bool(json.loads(resp.read()).get("ocr"))
    return False


def _read_pages(cfg, pictures):
    body = json.dumps({"images": [base64.b64encode(p).decode() for p in pictures]}).encode()
    req = urllib.request.Request(cfg["ai"]["face_url"] + "/ocr", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())["results"]


def _page_heading(lines):
    """(chapter number, title or "") from a page's text, or None; and whether it's a credits page."""
    found = None
    for k, (h, x, y, text) in enumerate(lines):
        m = COMIC_HEADING.match(text.strip())
        if not m:
            continue
        title = m.group(3).strip(" :.-–—")
        # the rest of the title on the lines below (as big, close under it)
        at, parts = y, [title] if title else []
        for _ in range(3 - len(parts)):
            below = sorted((y2, t2) for h2, x2, y2, t2 in lines
                           if at < y2 <= at + h * 1.8 and h2 >= h * 0.75 and t2.strip() and not COMIC_HEADING.match(t2.strip()))
            if not below:
                break
            at = below[0][0]
            parts.append(below[0][1].strip())
        title = " ".join(parts).strip(" :.-–—")
        cand = (h, int(m.group(2)), title)
        if not found or cand[0] > found[0]:
            found = cand  # the biggest heading on the page
    credits = any(COMIC_CREDITS.search(t) for _, _, _, t in lines)
    return ((found[1], found[2]) if found else None), credits


def comic_headings(stash, cfg, plugin_dir, imgs, paths, budget=COMIC_PAGES_PER_CALL):
    """{page index: (number, title)} and how many pages are still unread."""
    import zipfile  # noqa: PLC0415
    path = os.path.join(plugin_dir, "comic_text.json")
    try:
        with open(path) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    todo = [k for k, i in enumerate(imgs) if i["id"] not in seen or "t" not in seen[i["id"]]]
    if todo and budget:
        archives = {}
        batch, keys = [], []

        def flush():
            if not batch:
                return
            for key, lines in zip(keys, _read_pages(cfg, batch)):
                head, credits = _page_heading(lines)
                # (the page's bigger text is kept, so a better reading of it needs no new look)
                seen[key] = {"h": list(head) if head else None, "c": credits,
                             "t": [ln for ln in lines if ln[0] >= 0.015][:40]}
            batch.clear()
            keys.clear()

        for k in todo[:budget]:
            p = paths[k]
            m = re.match(r"(?i)^(.*?\.(?:zip|cbz))/(.+)$", p)
            data = None
            with contextlib.suppress(Exception):
                if m:
                    z = archives.get(m.group(1)) or archives.setdefault(m.group(1), zipfile.ZipFile(m.group(1)))
                    data = z.read(m.group(2))
                else:
                    with open(p, "rb") as f:
                        data = f.read()
            if not data:
                seen[imgs[k]["id"]] = {"h": None, "c": False}
                continue
            batch.append(data)
            keys.append(imgs[k]["id"])
            if len(batch) >= 8:
                flush()
        flush()
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(seen, f, separators=(",", ":"))
        _replace(tmp, path)
    heads, last = {}, 0
    for k, i in enumerate(imgs):
        ent = seen.get(i["id"]) or {}
        h = (_page_heading(ent["t"])[0] if ent.get("t") else None) or (tuple(ent["h"]) if ent.get("h") else None)
        if h and h[0] > last and h[0] <= last + 3:  # (the next chapter, maybe one skipped)
            heads[k] = (h[0], h[1])
            last = h[0]
    # a chapter whose heading couldn't be read: where the numbers skip one
    # (or at the end), it starts after the translator's credits page inside
    credit = [bool((seen.get(i["id"]) or {}).get("c")) for i in imgs]
    starts = sorted(heads)
    sizes = sorted(b - a for a, b in zip(starts, starts[1:])) or [0]
    usual = sizes[len(sizes) // 2]
    for a, b in zip(starts, starts[1:] + [len(imgs)]):
        gap = heads[b][0] - heads[a][0] - 1 if b in heads else (1 if usual and b - a >= usual * 1.6 else 0)
        if gap < 1:
            continue
        cuts = [k + 1 for k in range(a + 3, b - 3) if credit[k] and not credit[k + 1]]
        for n, k in enumerate(cuts[:gap]):
            heads[k] = (heads[a][0] + 1 + n, "")
    if len(heads) < 2:
        # no chapter headings: a compilation's stories end with the translator's
        # credits, so a new one starts after each credits page
        heads = {}
        for k in range(1, len(imgs) - 3):
            if credit[k - 1] and not credit[k] and k >= 3 and all(abs(k - x) >= 4 for x in heads):
                big = sorted(((ln[0], ln[3].strip()) for ln in (seen.get(imgs[k]["id"]) or {}).get("t") or []), reverse=True)
                # (big text that reads like a title: two words or more)
                title = next((t for h, t in big[:2] if h >= 0.04 and 6 <= len(t) <= 40 and len(re.findall(r"[A-Za-z]{2,}", t)) >= 2), "")
                heads[k] = (None, title)
    return heads, sum(1 for i in imgs if i["id"] not in seen or "t" not in seen[i["id"]])


def run_gallery_chapters(stash, plugin_dir, gallery_id, read=True):
    """Suggested chapters for a gallery: [{index (1 = first image), title,
    why: [...], image (id), count}], best place for each, plus its chapters."""
    gid = str(gallery_id)
    g = stash.call("query($id: ID!) { findGallery(id: $id) { id title chapters { id title image_index } } }", {"id": gid})["findGallery"]
    if not g:
        raise RuntimeError("no such gallery")
    imgs = stash.call("query($g: [ID!]) { findImages(image_filter: {galleries: {value: $g, modifier: INCLUDES}}, "
                      "filter: {per_page: -1, sort: \"path\", direction: ASC}) { images { id date "
                      "tags { id name } performers { id name } studio { id name } visual_files { ... on BaseFile { path } } } } }",
                      {"g": [gid]})["findImages"]["images"]
    n = len(imgs)
    if n < 6:
        return {"chapters": [], "have": g["chapters"], "count": n, "images": [i["id"] for i in imgs]}
    paths = [((i.get("visual_files") or [{}])[0] or {}).get("path") or "" for i in imgs]
    folders = [os.path.dirname(p) for p in paths]
    stems = [_name_stem(p) for p in paths]
    numbers = [_name_number(p) for p in paths]
    cache = LookCache(plugin_dir).data
    vecs = []
    for i in imgs:
        e = (cache.get(f"image:{i['id']}") or {}).get("e")
        if e:
            v = _ints(e)
            nrm = math.sqrt(sum(x * x for x in v)) or 1
            vecs.append([x / nrm for x in v])
        else:
            vecs.append(None)
    perf = [frozenset(p["id"] for p in i.get("performers") or []) for i in imgs]
    studio = [(i.get("studio") or {}).get("id") for i in imgs]
    tags = [frozenset(t["id"] for t in i.get("tags") or []) for i in imgs]
    names = {}
    for i in imgs:
        for x in (i.get("tags") or []) + (i.get("performers") or []):
            names[x["id"]] = x["name"]
        if i.get("studio"):
            names["s" + i["studio"]["id"]] = i["studio"]["name"]
    # a comic: its chapter headings, when the face helper can read them
    is_comic = any(re.search(r"(?i)\.(zip|cbz)/", p) for p in paths[:3])
    reader = None
    if is_comic and read:
        cfg = settings(stash, plugin_dir)
        reader = _comic_reader(cfg)
        # (pages read before count even when the reader isn't there now)
        heads, unread = comic_headings(stash, cfg, plugin_dir, imgs, paths, budget=COMIC_PAGES_PER_CALL if reader else 0)
        if not unread:
            reader = True
        if reader:
            if unread:
                return {"chapters": [], "have": g["chapters"], "count": n, "images": [i["id"] for i in imgs], "reading": n - unread, "unread": unread}
            if len(heads) >= 2:
                import string  # noqa: PLC0415
                out = []
                starts = sorted(set([0] + list(heads)))
                by_credits = all(v[0] is None for v in heads.values())
                for k, (a, b) in enumerate(zip(starts, starts[1:] + [n])):
                    num, title = heads.get(a, (None, ""))
                    if num:
                        t = f"Chapter {num}" + (f": {string.capwords(title.strip())}" if title else "")
                        why = ["chapter heading on the page"]
                    elif a in heads:
                        t = string.capwords(title) if title else f"Part {k + 1}"
                        why = ["after the translator's credits page"]
                    else:
                        t = "Part 1" if by_credits else "Opening pages"
                        why = ["the start"]
                    out.append({"index": a + 1, "title": t[:120], "why": why, "image": imgs[a]["id"], "count": b - a})
                return {"chapters": out, "have": g["chapters"], "count": n, "images": [i["id"] for i in imgs], "comic": True}
    w = CHAPTER_WINDOW

    def common(sets, lo, hi, share=0.5):
        c = Counter(x for s in sets[lo:hi] for x in s)
        k = max(1, hi - lo)
        return {x for x, m in c.items() if m / k >= share}

    def mode(vals, lo, hi):
        c = Counter(v for v in vals[lo:hi] if v)
        return c.most_common(1)[0][0] if c else None

    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    scores = []  # (i: the first image of the new chapter, score, reasons)
    for i in range(1, n):
        why, sc = [], 0.0
        lo, hi = max(0, i - w), min(n, i + w)
        if folders[i] != folders[i - 1] and folders[i] in folders[i:hi] and folders[i] not in folders[lo:i]:
            sc = max(sc, 2.0)
            why.append("new folder")
        if stems[i] != stems[i - 1] and stems[i] and (i + 1 >= n or stems[i + 1] == stems[i]) and stems[i] not in stems[lo:i]:
            sc = max(sc, 1.5)
            why.append("file names change")
        elif stems[i] == stems[i - 1] and numbers[i] is not None and numbers[i - 1] is not None and numbers[i] < numbers[i - 1] - 3:
            sc = max(sc, 1.2)
            why.append("numbering starts again")
        bv = [v for v in vecs[lo:i] if v]
        av = [v for v in vecs[i:hi] if v]
        if bv and av:
            sims = [[dot(a, b) for b in bv] for a in av]
            look = 0.5 * (sum(1 - max(r) for r in sims) / len(av) + sum(1 - max(sims[a][b] for a in range(len(av))) for b in range(len(bv))) / len(bv))
            if look / CHAPTER_NEW_LOOK >= 1:
                why.append("different look")
            sc = max(sc, look / CHAPTER_NEW_LOOK)
        pb, pa = common(perf, lo, i), common(perf, i, hi)
        if pa and pb != pa and not (pa <= pb):
            sc = max(sc, 1.2)
            why.append("new performers")
        sb, sa = mode(studio, lo, i), mode(studio, i, hi)
        if sa and sb and sa != sb:
            sc = max(sc, 1.2)
            why.append("new studio")
        tb, ta = set().union(*tags[lo:i]), set().union(*tags[i:hi])
        if tb and ta and len(tb & ta) / len(tb | ta) < 0.25:
            sc = max(sc, 0.9 if sc < 0.9 else sc)
            why.append("different tags")
        d0, d1 = imgs[i - 1].get("date"), imgs[i].get("date")
        if d0 and d1 and d0 != d1:
            sc = max(sc, 1.2)
            why.append("a different day")
        scores.append((i, sc, why))
    # the strongest changes, at least a few images apart
    gap = max(3, n // 60)
    starts = [0]
    for j, (i, sc, why) in enumerate(scores):
        if sc < 1 or any(sc < x[1] for x in scores[max(0, j - gap):j + gap + 1]):
            continue
        if all(abs(i - s) >= gap for s in starts):
            starts.append(i)
    starts.sort()
    # a few pages at the very end (credits, extras) aren't a chapter of their own
    if len(starts) > 1 and n - starts[-1] < max(6, gap * 2):
        starts.pop()
    reasons = {i: why for i, sc, why in scores}
    # titles: what the chapter has that the others don't
    bounds = list(zip(starts, starts[1:] + [n]))
    every_perf = set.intersection(*[common(perf, a, b) for a, b in bounds]) if bounds else set()
    gallery_tags = Counter(t for s in tags for t in s)
    # tags that group other tags ("Categories") don't describe anything
    with contextlib.suppress(Exception):
        for t in stash.call("{ findTags(tag_filter: {child_count: {value: 0, modifier: GREATER_THAN}}, filter: {per_page: -1}) "
                            "{ tags { id } } }")["findTags"]["tags"]:
            names.pop(t["id"], None)
    labels = Counter(_folder_label(folders[a]) for a, _ in bounds)
    out = []
    for k, (a, b) in enumerate(bounds):
        title = None
        label = _folder_label(folders[a])
        # a folder's name, when it's the only chapter from that folder
        if label and labels[label] == 1 and len({folders[x] for x in range(a, b)}) == 1 and len({folders[x] for x, _ in bounds}) > 1:
            title = label
        if not title:
            st = Counter(stems[a:b]).most_common(1)
            if st and st[0][1] >= (b - a) * 0.6 and len(st[0][0]) >= 3 and not CHAPTER_GENERIC.match(st[0][0]):
                title = st[0][0].title()
        if not title:
            ps = common(perf, a, b) - every_perf
            if ps:
                title = " & ".join(sorted(names.get(p, "") for p in ps)[:3])
        if not title:
            here = Counter(t for s in tags[a:b] for t in s)
            stand = sorted(((m / (b - a)) / (gallery_tags[t] / n), t) for t, m in here.items()
                           if t in names and m / (b - a) >= 0.5 and (m / (b - a)) / (gallery_tags[t] / n) >= 1.3
                           and names.get(t, "").strip().lower() not in BODY_TAG_NAMES)
            if stand:
                title = " · ".join(names[t] for _, t in sorted(stand, reverse=True)[:2])
        if not title:
            s0 = mode(studio, a, b)
            if s0 and len({mode(studio, x, y) for x, y in bounds}) > 1:
                title = names.get("s" + s0)
        out.append({"index": a + 1, "title": title or f"Part {k + 1}", "why": reasons.get(a, []) if a else ["the start"],
                    "image": imgs[a]["id"], "count": b - a})
    # (a comic, but the face helper can't read text: say so)
    return {"chapters": out, "have": g["chapters"], "count": n, "images": [i["id"] for i in imgs], **({"no_reader": True} if is_comic and reader is False else {})}
