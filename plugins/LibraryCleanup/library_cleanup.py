"""Library Cleanup - find and delete tags, performers and studios nothing is
connected to.

Unused tag: not on any scene, image, gallery, scene marker, performer, studio
or group. Unused performer or studio: no scenes, images, galleries or groups.
Always kept: favourites, tags and studios that are the parent of others, the tags
listed under "Tags to always keep", and the parent tag Better
Tagging Tools files its gallery-name tags under.
"""
import json
import sys
import urllib.request

PLUGIN_ID = "LibraryCleanup"
BATCH = 200

TAG_USES = ("scene_count", "scene_marker_count", "image_count", "gallery_count",
            "performer_count", "studio_count", "group_count")
PERFORMER_USES = ("scene_count", "image_count", "gallery_count", "group_count")
STUDIO_USES = ("scene_count", "image_count", "gallery_count", "group_count")


def log(level, msg):
    codes = {"trace": "t", "debug": "d", "info": "i", "warning": "w", "error": "e"}
    sys.stderr.write(f"\x01{codes[level]}\x02{msg}\n")
    sys.stderr.flush()


class Stash:
    def __init__(self, conn):
        scheme = conn.get("Scheme", "http")
        host = conn.get("Host", "localhost")
        if host in ("0.0.0.0", ""):
            host = "localhost"
        port = conn.get("Port", 9999)
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
            raise RuntimeError(result["errors"][0].get("message", result["errors"]))
        return result["data"]


def kept_tag_names(stash):
    try:
        plugins = stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {}
    except Exception:
        plugins = {}
    own = plugins.get(PLUGIN_ID) or {}
    names = {n.strip().lower() for n in (own.get("keepTags") or "").split(",") if n.strip()}
    # Better Organizing Tools' parent tag for gallery-name tags
    parent = ((plugins.get("BetterTaggingTools") or {}).get("galleryTagParent") or "").strip()
    if parent:
        names.add(parent.lower())
    return names


def unused_tags(stash):
    tags = stash.call(
        "{ findTags(filter: {per_page: -1}) { tags { id name favorite child_count "
        + " ".join(TAG_USES) + " } } }"
    )["findTags"]["tags"]
    keep = kept_tag_names(stash)
    unused = [
        t for t in tags
        if not any(t[k] for k in TAG_USES)
        and not t["child_count"]
        and not t["favorite"]
        and t["name"].lower() not in keep
    ]
    return unused, len(tags)


def unused_performers(stash):
    perfs = stash.call(
        "{ findPerformers(filter: {per_page: -1}) { performers { id name favorite "
        + " ".join(PERFORMER_USES) + " } } }"
    )["findPerformers"]["performers"]
    unused = [p for p in perfs if not any(p[k] for k in PERFORMER_USES) and not p["favorite"]]
    return unused, len(perfs)


def unused_studios(stash):
    studios = stash.call(
        "{ findStudios(filter: {per_page: -1}) { studios { id name favorite child_studios { id } "
        + " ".join(STUDIO_USES) + " } } }"
    )["findStudios"]["studios"]
    # A parent studio is kept while it has a child studio that stays; if all
    # its children are unused too, the parent goes with them.
    empty = {s["id"] for s in studios if not any(s[k] for k in STUDIO_USES) and not s["favorite"]}
    children = {s["id"]: {c["id"] for c in s["child_studios"]} for s in studios}
    gone = {sid for sid in empty if not children[sid]}
    while True:
        more = {sid for sid in empty - gone if children[sid] <= gone}
        if not more:
            break
        gone |= more
    # children first, then their parents
    return sorted((s for s in studios if s["id"] in gone), key=lambda s: bool(s["child_studios"])), len(studios)


def name_list(items, limit=60):
    names = sorted(i["name"] for i in items)
    more = f" ... and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def delete(stash, mutation, ids):
    for i in range(0, len(ids), BATCH):
        stash.call(f"mutation($ids: [ID!]!) {{ {mutation}(ids: $ids) }}", {"ids": ids[i:i + BATCH]})


def run(stash, mode):
    if mode in ("preview", "tags"):
        tags, total = unused_tags(stash)
        if not tags:
            log("info", f"Tags: all {total} are in use (or kept).")
        elif mode == "preview":
            log("info", f"Tags: {len(tags)} of {total} are unused and would be deleted: {name_list(tags)}")
        else:
            delete(stash, "tagsDestroy", [t["id"] for t in tags])
            log("info", f"Deleted {len(tags)} unused tag(s): {name_list(tags)}")

    if mode in ("preview", "performers"):
        perfs, total = unused_performers(stash)
        if not perfs:
            log("info", f"Performers: all {total} are in use (or kept).")
        elif mode == "preview":
            log("info", f"Performers: {len(perfs)} of {total} are unused and would be deleted: {name_list(perfs)}")
        else:
            delete(stash, "performersDestroy", [p["id"] for p in perfs])
            log("info", f"Deleted {len(perfs)} unused performer(s): {name_list(perfs)}")

    if mode in ("preview", "studios"):
        studios, total = unused_studios(stash)
        if not studios:
            log("info", f"Studios: all {total} are in use (or kept).")
        elif mode == "preview":
            log("info", f"Studios: {len(studios)} of {total} are unused and would be deleted: {name_list(studios)}")
        else:
            # children before parents (separate batches)
            delete(stash, "studiosDestroy", [s["id"] for s in studios if not s["child_studios"]])
            delete(stash, "studiosDestroy", [s["id"] for s in studios if s["child_studios"]])
            log("info", f"Deleted {len(studios)} unused studio(s): {name_list(studios)}")

    if mode == "preview":
        log("info", "Preview only - nothing was deleted. Favourites, parent tags and studios, and the tags you chose to keep are never deleted.")


def main():
    payload = json.loads(sys.stdin.read())
    stash = Stash(payload.get("server_connection", {}))
    mode = (payload.get("args") or {}).get("mode", "preview")
    try:
        run(stash, mode)
    except Exception as e:
        log("error", f"Library Cleanup failed: {e}")
        raise


if __name__ == "__main__":
    main()
