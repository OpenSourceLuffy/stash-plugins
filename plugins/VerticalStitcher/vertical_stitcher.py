import base64
import importlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

# Windows: read and write Stash's pipes as UTF-8 (the console's code page
# otherwise), and give ffmpeg no console window of its own
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if os.name == "nt":
    _sp_popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000  # CREATE_NO_WINDOW
        _sp_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _popen_no_window

# Stash log levels are signalled by a prefix on stderr lines.
def log(level, msg):
    codes = {"trace": "t", "debug": "d", "info": "i", "warning": "w", "error": "e"}
    sys.stderr.write(f"\x01{codes[level]}\x02{msg}\n")
    sys.stderr.flush()


class Stash:
    def __init__(self, conn):
        self.conn = conn
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
        # the sign-in Stash hands a task can run out during a long one ("Stitch
        # all vertical scenes"); Stash's own API key, from its settings file,
        # doesn't: used when there is one
        key = self._api_key()
        if key:
            self.headers["ApiKey"] = key

    def _api_key(self):
        for d in (self.config_dir, os.path.expanduser("~/.stash")):
            if not d:
                continue
            try:
                with open(os.path.join(d, "config.yml"), encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("api_key:"):
                            key = line.split(":", 1)[1].strip().strip("'\"")
                            if key:
                                return key
            except OSError:
                pass
        return None

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


# --- where the video's own content ends ---------------------------------------
# Phone clips often end on something that isn't the video: TikTok's and
# Instagram's end cards (a dark screen with their logo and the account, 4-5 s),
# a creator's logo card, or Control Center pulled down to stop a screen
# recording. Frames are only taken from before that: never the last END_SKIP
# seconds, and not from an end card - after a cut, dark and flat frames that
# hardly change until the end. (The same check as Better Organizing AI's.)
END_SKIP = 2.0
END_LOOK = 10.0     # how far back end cards are looked for
CARD_DARK = 45      # an end card's frames: brightness under this...
CARD_FLAT = 40      # ...contrast under this...
CARD_STILL = 10     # ...changing less than this from one frame to the next
CARD_CUT = 25       # a cut into it: this much change in the half second before
CARD_MIN = 1.0      # seconds an end card lasts at least
_FPS, _W, _H = 4, 18, 32


def end_card_start(ffmpeg, path, duration):
    """When an end card starts (seconds), or None."""
    win = min(END_LOOK, duration * 0.7)
    s0 = duration - win
    try:
        raw = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-ss", f"{s0:.2f}",
                              "-i", path, "-an", "-sn", "-vf", f"fps={_FPS},scale={_W}:{_H},format=gray",
                              "-f", "rawvideo", "-"], capture_output=True, timeout=60, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    n = _W * _H
    frames = [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]
    if len(frames) < 6:
        return None

    def diff(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / n

    def card(x):
        return sum(x) / n < CARD_DARK and statistics.pstdev(x) < CARD_FLAT

    i = len(frames) - 1
    if not card(frames[i]):
        return None
    while i > 0 and card(frames[i - 1]) and diff(frames[i - 1], frames[i]) < CARD_STILL:
        i -= 1
    if i == 0 or (len(frames) - i) / _FPS < CARD_MIN:
        return None  # (all dark: a dark video, not a card)
    if diff(frames[max(i - 2, 0)], frames[i]) < CARD_CUT:
        return None
    return s0 + max(i - 2, 0) / _FPS


def content_end(stash, f, ffmpeg):
    """Up to when the video shows its own content (seconds)."""
    ai = better_organizing_ai(stash)
    if ai:  # (its check, and the record it keeps of every video's end)
        try:
            return ai[0].content_end(ai[1], f)
        except Exception:
            pass
    dur = float(f["duration"])
    end = dur - min(END_SKIP, dur * 0.1)
    card = end_card_start(ffmpeg, f["path"], dur)
    return min(end, card) if card and card > dur * 0.3 else end


# --- with Better Organizing AI (optional) -------------------------------------
# When the Better Organizing AI add-on is installed next to this plugin and its
# face helper runs on a Mac, each frame is the best-looking of three from its
# part of the video (Apple's aesthetics score, people in it preferred; text
# screens, blurry or dark frames score low). Without it, frames are evenly
# spaced.
_AI = []


def better_organizing_ai(stash):
    """(btt_core, its Stash connection) when Better Organizing AI is installed, else None."""
    if not _AI:
        found = None
        here = os.path.dirname(os.path.abspath(__file__))
        folder = os.path.join(os.path.dirname(here), "BetterTaggingAI")
        if os.path.isfile(os.path.join(folder, "btt_core.py")):
            try:
                sys.path.insert(0, folder)
                core = importlib.import_module("btt_core")
                core.log = lambda level, msg: log(level if level in ("debug", "info", "warning", "error") else "debug", msg)
                found = (core, core.Stash(stash.conn))
            except Exception as e:
                log("debug", f"Better Organizing AI not used: {e}")
            finally:
                with __import__("contextlib").suppress(ValueError):
                    sys.path.remove(folder)
        _AI.append(found)
    return _AI[0]


def pick_times(stash, f, count, ffmpeg):
    """When to take the frames: from the video's own content, spread out;
    with Better Organizing AI's scores, the best of three in each part."""
    end = content_end(stash, f, ffmpeg)
    start = min(0.5, end * 0.05)
    ai = better_organizing_ai(stash)
    if ai and ai[0].quality_ready(ai[1]):
        core, bstash = ai
        tries = 3
        n = count * tries
        times = [start + (end - start) * (i + 0.5) / n for i in range(n)]
        scores = []
        for t in times:
            score = None
            try:
                data = core._video_frame(ffmpeg, f["path"], t, width=480)
                if data:
                    score = core.picture_quality(bstash, data, people=True)
            except Exception:
                pass
            scores.append(-2 if score is None else score)
        if any(x > -2 for x in scores):
            picked = []
            for k in range(count):
                part = range(k * tries, (k + 1) * tries)
                picked.append(times[max(part, key=lambda i: scores[i])])
            return picked, True
    return [start + (end - start) * (i + 1) / (count + 1) for i in range(count)], False


def build_cover(video_path, times, ffmpeg="ffmpeg"):
    """The frames at `times` (seconds) side by side in a 1280x720 JPEG (bytes)."""
    count = len(times)
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
        cmd += ["-ss", f"{t:.2f}", "-i", video_path]
    cmd += ["-filter_complex", filter_graph, "-map", "[out]", "-frames:v", "1", "-q:v", "2", out_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip())
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)


def record_stitched(stash, scene_id, cover, ffmpeg, times):
    """Notes the cover in Stash's config folder (plugin-data/vertical_stitcher/
    stitched.json) as a tiny grey copy, so Better Organizing AI's covers tasks
    know it's a stitched cover and leave it alone."""
    if not stash.config_dir:
        return
    try:
        grey = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vf",
                               "scale=32:18,format=gray", "-f", "rawvideo", "-"], input=cover, capture_output=True,
                              timeout=30, check=False).stdout
        if len(grey) != 32 * 18:
            return
        folder = os.path.join(stash.config_dir, "plugin-data", PLUGIN_ID)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "stitched.json")
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            rec = {}
        rec[str(scene_id)] = {"grey": base64.b64encode(grey).decode(), "at": [round(t, 2) for t in times]}
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(rec, fh, separators=(",", ":"))
        for attempt in range(50):  # (Windows: not while someone has it open for a moment)
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 49:
                    raise
                time.sleep(0.1)
    except Exception as e:
        log("debug", f"Scene {scene_id}: stitched cover not recorded ({e})")


def process_scene(stash, scene, count=DEFAULT_FRAMES):
    if not scene or not scene.get("files"):
        return False
    f = scene["files"][0]
    width, height = f.get("width") or 0, f.get("height") or 0
    duration = f.get("duration") or 0
    if not (height > width and duration > 5):
        log("debug", f"Scene {scene['id']} is not vertical, skipping")
        return False

    ff = ffmpeg_path(stash)
    times, scored = pick_times(stash, f, count, ff)
    cover = build_cover(f["path"], times, ff)
    b64 = base64.b64encode(cover).decode()
    stash.call(
        "mutation($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }",
        {"input": {"id": scene["id"], "cover_image": f"data:image/jpeg;base64,{b64}"}},
    )
    record_stitched(stash, scene["id"], cover, ff, times)
    at = ", ".join(f"{t:.1f}s" for t in times)
    log("info", f"Stitched cover set for scene {scene['id']} (frames at {at}"
        + (", best-looking by Better Organizing AI's scores)" if scored else ")"))
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
