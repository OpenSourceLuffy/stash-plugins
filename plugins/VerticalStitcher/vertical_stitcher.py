import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request

# Stash log levels are signalled by a prefix on stderr lines.
def log(level, msg):
    codes = {"trace": "t", "debug": "d", "info": "i", "warning": "w", "error": "e"}
    sys.stderr.write(f"\x01{codes[level]}\x02{msg}\n")
    sys.stderr.flush()


class Stash:
    def __init__(self, conn):
        self.config_dir = conn.get("Dir") or ""
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
            raise RuntimeError(result["errors"])
        return result["data"]


SCENE_FIELDS = "id files { path width height duration }"


PLUGIN_ID = "vertical_stitcher"
DEFAULT_FRAMES = 3


def frame_count(stash):
    """Number of frames to stitch, from the plugin setting (2-4, default 3)."""
    try:
        cfg = stash.call("{ configuration { plugins } }")["configuration"]["plugins"] or {}
        n = int((cfg.get(PLUGIN_ID) or {}).get("frameCount") or DEFAULT_FRAMES)
    except Exception:
        n = DEFAULT_FRAMES
    return min(max(n, 2), 4)


_FFMPEG = None


def ffmpeg_path(stash):
    """Stash's own ffmpeg: the path set in Settings, then the system PATH,
    then the copy Stash downloads into its config folder."""
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    candidates = []
    try:
        configured = stash.call("{ configuration { general { ffmpegPath } } }")["configuration"]["general"]["ffmpegPath"]
        if configured:
            candidates.append(configured)
    except Exception:
        pass
    candidates.append(shutil.which("ffmpeg"))
    if stash.config_dir:
        candidates += [os.path.join(stash.config_dir, "ffmpeg"), os.path.join(stash.config_dir, "ffmpeg.exe")]
    for c in candidates:
        if c and os.path.isfile(c):
            _FFMPEG = c
            return c
    raise RuntimeError("ffmpeg not found - set its path in Settings > System > Application Paths")


def build_cover(video_path, duration, count=DEFAULT_FRAMES, ffmpeg="ffmpeg"):
    # Evenly spaced frames, e.g. 3 frames -> 25%, 50%, 75% of the video
    times = [f"{duration * (i + 1) / (count + 1):.2f}" for i in range(count)]
    fd, out_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    scaled = "".join(f"[{i}:v]scale=-2:720,setsar=1[f{i}];" for i in range(count))
    stacked = "".join(f"[f{i}]" for i in range(count))
    filter_graph = (
        scaled
        + f"{stacked}hstack=inputs={count},"
        "scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black[out]"
    )
    cmd = [ffmpeg, "-y", "-loglevel", "error"]
    for t in times:
        cmd += ["-ss", t, "-i", video_path]
    cmd += ["-filter_complex", filter_graph, "-map", "[out]", "-frames:v", "1", "-q:v", "2", out_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        with open(out_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


def process_scene(stash, scene, count=DEFAULT_FRAMES):
    if not scene or not scene.get("files"):
        return False
    f = scene["files"][0]
    width, height = f.get("width") or 0, f.get("height") or 0
    duration = f.get("duration") or 0
    if not (height > width and duration > 5):
        log("debug", f"Scene {scene['id']} is not vertical, skipping")
        return False

    b64 = build_cover(f["path"], duration, count, ffmpeg_path(stash))
    stash.call(
        "mutation($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
        {"input": {"id": scene["id"], "cover_image": f"data:image/jpeg;base64,{b64}"}},
    )
    log("info", f"Stitched cover set for scene {scene['id']}")
    return True


def run_hook(stash, scene_id):
    data = stash.call(
        f"query($id: ID!) {{ findScene(id: $id) {{ {SCENE_FIELDS} }} }}", {"id": scene_id}
    )
    process_scene(stash, data["findScene"], frame_count(stash))


def run_all(stash, unorganized_only=False):
    page, per_page, done = 1, 100, 0
    query = (
        "query($filter: FindFilterType, $scene_filter: SceneFilterType) "
        "{ findScenes(filter: $filter, scene_filter: $scene_filter) "
        f"{{ count scenes {{ {SCENE_FIELDS} }} }} }}"
    )
    scene_filter = {"organized": False} if unorganized_only else {}
    count = frame_count(stash)
    log("info", f"Stitching {count} frames per cover")
    if unorganized_only:
        log("info", "Only processing scenes not marked as organized")
    while True:
        data = stash.call(query, {
            "filter": {"page": page, "per_page": per_page, "sort": "id", "direction": "ASC"},
            "scene_filter": scene_filter,
        })
        scenes = data["findScenes"]["scenes"]
        total = data["findScenes"]["count"]
        if not scenes:
            break
        for i, scene in enumerate(scenes):
            try:
                if process_scene(stash, scene, count):
                    done += 1
            except Exception as e:
                log("error", f"Scene {scene['id']}: {e}")
            sys.stderr.write(f"\x01p\x02{((page - 1) * per_page + i + 1) / max(total, 1)}\n")
        page += 1
    log("info", f"Done. Stitched {done} vertical scene covers.")


def main():
    payload = json.loads(sys.stdin.read())
    stash = Stash(payload.get("server_connection", {}))
    args = payload.get("args", {})

    try:
        hook = args.get("hookContext")
        if hook:
            run_hook(stash, hook["id"])
        elif args.get("mode") == "all":
            run_all(stash)
        elif args.get("mode") == "unorganized":
            run_all(stash, unorganized_only=True)
    except Exception as e:
        log("error", f"Vertical stitcher failed: {e}")

    print(json.dumps({"output": "ok"}))


if __name__ == "__main__":
    main()
