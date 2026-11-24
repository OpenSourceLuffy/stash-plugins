"""Face helper for Better Organizing AI Add-on.

A tiny local web service: send it pictures, get back the faces in them and a
"fingerprint" (128 numbers) for each face. Two faces with similar
fingerprints are likely the same person. It keeps nothing; the plugin keeps
the performers' fingerprints itself.

It also makes a fingerprint of a whole picture (512 numbers, CLIP), so the
add-on can find pictures that look alike and suggest the tags you gave them.

Models: YuNet finds faces, SFace makes the face fingerprints (OpenCV model
zoo); CLIP ViT-B/32 (OpenAI, MIT licence) the picture fingerprints.

    POST /faces  {"images": ["<base64 JPEG/PNG>", ...]}
      -> {"results": [[{"box": [x, y, w, h], "score": 0.98, "emb": [...]}, ...], ...]}
    POST /embed  {"images": [...], "boxes": [null or [[x, y, w, h], ...], ...]}
      -> {"results": [[512 numbers] or null, ...]}; with boxes, each picture's
         result is a list: one fingerprint per box cut out of it (e.g. the
         frames in a scene's preview sprite)
    POST /body   {"images": [...]}
      -> {"results": [[{"part": "FEMALE_BREAST_EXPOSED", "score": 0.8, "box": [x, y, w, h]}, ...], ...]}
         (NudeNet 320n, AGPL-3.0, only if downloaded: setup.sh --body)
    POST /match  {"faces": ["<q8 fingerprint>", ...], "known": {"<performer id>": ["<q8>", ...], ...}}
      -> {"results": [["<best performer id>", best, second best (anyone else)], ...]}
         (q8: 128 bytes, each number x127, base64 - as the add-on stores them;
         the likeness is the plain dot product, like the add-on's own)
    POST /knn    {"vectors": ["<base64 int8 picture fingerprint>", ...], "base": [indexes], "k": 20}
      -> {"results": [[[base index, dot], ...], ...]}: for every vector, the k
         most alike of the base ones (dot product of the raw numbers, itself left out)
    POST /cluster {"faces": ["<q8>", ...], "threshold": 0.45, "dim": 128, "center": false}
                 (dim 512 + center for picture fingerprints)
      -> {"results": [[group number, likeness to the group], ...]}: the same
         person's faces get the same number (for "new performers")
    POST /ocr    {"images": [...]}
      -> {"results": [[[height, x, y, "text"], ...], ...]}: the text in each
         picture (Apple's Vision text reader, on a Mac only; the sizes are
         shares of the picture, y from the top), e.g. a comic's chapter headings
    POST /apple  {"images": [...]}
      -> {"results": [{"size": [w, h], "labels": {"high_heel": 0.8, ...}, "hands": [[[x, y], ...], ...],
                       "chests": [[x0, y0, x1, y1], ...]}, ...]}: Apple's Vision on a Mac - its picture
         classifier (labels at 0.2 or more), the points of each hand, and each person's chest area from
         their body pose (all as shares of the picture, y from the top), and "people": each person's
         body joints {"left_leg": [x, y, confidence], ...} (x and y as shares of the picture's longer
         side, y from the top). With "pose_only": true, just the size and people (quicker); with
         "aesthetics_only": true, just {"score": -1..1, "utility": true/false} - Apple's aesthetics
         score (how good a photo it is: sharp, lit, framed; macOS 15+) and whether it's a screenshot,
         document or the like; with "people": true as well, "person": whether anyone is in it.
         With "card": true, a tall picture (a vertical video's frame) cut to a wide 16:9 one around
         the people in it: {"crop": "<base64 JPEG>" or null when it's not tall, "top": share cut off above}
    POST /shotcrop {"images": [...], "format": "jpg" / "png" / "webp" (one per picture)}
      -> {"results": [{"box": [x0, y0, x1, y1], "crop": "<base64>"} or {"box": null}, ...]}: a
         phone screenshot of a post (Instagram, OnlyFans, Reddit...) cut down to the photo in it
    POST /index  {"name": "looks", "version": "...", "keys": [...], "vectors": ["<q8 fingerprint>", ...]}
    POST /search {"name": "looks", "version": "...", "texts": ["a photo of ...", ...], "k": 60, "kinds": ["image", ...],
                  "per_group": 2}   (/index can also take "groups": one per key, e.g. a marker's scene)
      -> {"results": [[key, likeness], ...]} best first (near-identical ones once, at most per_group from
         one group), or {"missing": true} (send /index first)
    POST /text   {"texts": ["a woman in a red dress at the beach", ...]}
      -> {"results": [[512 numbers], ...]}: CLIP text fingerprints, to compare with /embed's
         picture fingerprints (search pictures by a description)
    POST /apple  {"images": [...], "facts": true}
      -> {"results": [{"score": -1..1, "utility": bool, "labels": [["beach", 0.9], ...]}, ...]}
         Apple's aesthetics score and picture labels in one go
    GET /health  -> {"ok": true, "embed": true/false, "body": true/false, "match": true, "knn": true, "cluster": true,
                     "ocr": true/false, "apple": true/false}

Runs on 127.0.0.1 only (Stash's Docker container reaches it as
host.docker.internal on a Mac), unless --host / FACE_HELPER_HOST says otherwise
(e.g. 0.0.0.0 when Stash runs elsewhere). Port: --port / FACE_HELPER_PORT,
default 11535.
"""
import base64
import contextlib
import json
import sys
import os
import threading
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DETECTOR = os.path.join(HERE, "models", "face_detection_yunet_2023mar.onnx")
RECOGNIZER = os.path.join(HERE, "models", "face_recognition_sface_2021dec.onnx")
CLIP_MODEL = os.path.join(HERE, "models", "clip_vit_b32_vision.onnx")
# CLIP's text half (quantized), for searching pictures by a description
CLIP_TEXT_MODEL = os.path.join(HERE, "models", "clip_vit_b32_text_quantized.onnx")
CLIP_TOKENIZER = os.path.join(HERE, "models", "clip_tokenizer.json")
BODY_MODEL = os.path.join(HERE, "models", "nudenet_320n.onnx")
BODY_PARTS = [
    "FEMALE_GENITALIA_COVERED", "FACE_FEMALE", "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED", "ANUS_EXPOSED", "FEET_EXPOSED", "BELLY_COVERED",
    "FEET_COVERED", "ARMPITS_COVERED", "ARMPITS_EXPOSED", "FACE_MALE", "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED", "ANUS_COVERED", "FEMALE_BREAST_COVERED", "BUTTOCKS_COVERED",
]
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
MAX_SIDE = 1280      # bigger pictures are scaled down first (faster, same result)
MIN_FACE = 40        # px; smaller faces are too blurry to recognise
MAX_FACES = 8        # per picture, biggest first
MAX_BODY = 300 * 1024 * 1024

_lock = threading.Lock()
_detector = cv2.FaceDetectorYN.create(DETECTOR, "", (320, 320), 0.8, 0.3, 5000)
_recognizer = cv2.FaceRecognizerSF.create(RECOGNIZER, "")


def _gpu():
    """Whether the models can run on an NVIDIA graphics card (shown in /health)."""
    try:
        import onnxruntime as ort
        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _providers(ort):
    """Where the ONNX models run: an NVIDIA graphics card (CUDA) when this
    onnxruntime can use one (onnxruntime-gpu), else the CPU. With thanks to
    scruffware, who added CUDA in their fork."""
    have = set(ort.get_available_providers())
    return [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in have] or ["CPUExecutionProvider"]


def faces_in(data):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    scale = min(1.0, MAX_SIDE / max(h, w))
    if scale < 1.0:
        img = cv2.resize(img, (int(w * scale), int(h * scale)))
        h, w = img.shape[:2]
    with _lock:
        _detector.setInputSize((w, h))
        _, found = _detector.detect(img)
        if found is None:
            return []
        found = sorted(found, key=lambda f: -f[2] * f[3])[:MAX_FACES]
        out = []
        for f in found:
            if min(f[2], f[3]) < MIN_FACE * scale:
                continue
            emb = _recognizer.feature(_recognizer.alignCrop(img, f)).flatten()
            emb = emb / (np.linalg.norm(emb) or 1.0)
            out.append({
                "box": [round(float(v) / scale) for v in f[:4]],
                "score": round(float(f[14]), 3),
                "emb": [round(float(v), 5) for v in emb],
            })
    return out


_clip = None
_clip_lock = threading.Lock()


_clip_text = None


def clip_text(texts):
    """CLIP text fingerprints (unit length) for descriptions: compare them
    with the picture fingerprints from /embed to find pictures by words."""
    global _clip_text
    if _clip_text is None:
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(CLIP_TOKENIZER)
            tok.enable_padding(length=77, pad_id=49407)
            tok.enable_truncation(77)
            _clip_text = (tok, ort.InferenceSession(CLIP_TEXT_MODEL, providers=_providers(ort)))
        except Exception:
            _clip_text = False
    if not _clip_text:
        raise RuntimeError("the text model isn't installed (run setup.sh again)")
    tok, sess = _clip_text
    enc = tok.encode_batch([t or "" for t in texts])
    feeds = {"input_ids": np.array([e.ids for e in enc], np.int64)}
    if any(i.name == "attention_mask" for i in sess.get_inputs()):
        feeds["attention_mask"] = np.array([e.attention_mask for e in enc], np.int64)
    out = sess.run(None, feeds)[0]
    out = out / np.linalg.norm(out, axis=1, keepdims=True)
    return [[round(float(v), 5) for v in row] for row in out]


# Search by description: the add-on hands over its picture fingerprints once
# (kept here in memory, by name and version), then each search only sends
# the words.
_indexes = {}


def index_put(name, version, keys, vectors, groups=None):
    m = np.stack([np.frombuffer(base64.b64decode(v), np.int8).astype(np.float32) for v in vectors]) if vectors else np.zeros((0, 512), np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
    keys = list(keys)
    _indexes[name] = (version, keys, m, list(groups) if groups else keys, [k.split(":")[0] for k in keys])
    return {"ok": True, "count": len(keys)}


SEARCH_SAME = 0.95  # results this alike are the same picture (frames of one take): only the best one shows


def index_search(name, version, texts, k=60, kinds=None, per_group=2):
    """The pictures most like the words (several wordings are averaged), best
    first: only the kinds asked for, near-identical ones once, and at most
    per_group from one group (a scene and its markers)."""
    ix = _indexes.get(name)
    if not ix or ix[0] != version:
        return {"missing": True}
    _, keys, m, groups, kind_of = ix
    q = np.mean(np.array(clip_text(texts), np.float32), axis=0)
    q /= np.linalg.norm(q) + 1e-9
    sims = m @ q
    kinds = set(kinds or [])
    out, chosen, per = [], [], {}
    for i in np.argsort(-sims):
        if kinds and kind_of[i] not in kinds:
            continue
        g = groups[i]
        if per.get(g, 0) >= per_group:
            continue
        if chosen and float(np.max(m[chosen] @ m[i])) >= SEARCH_SAME:
            continue
        chosen.append(i)
        per[g] = per.get(g, 0) + 1
        out.append([keys[i], round(float(sims[i]), 4)])
        if len(out) >= int(k):
            break
    return {"results": out}


def clip_session():
    """The picture model, loaded the first time it's needed (None if it
    isn't installed)."""
    global _clip
    if _clip is None:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            _clip = ort.InferenceSession(CLIP_MODEL, opts, providers=_providers(ort))
        except Exception:
            _clip = False
    return _clip or None


def clip_input(img):
    """CLIP's input: shortest side 224, the middle square, RGB, normalised."""
    if img is None or min(img.shape[:2]) < 8:
        return None
    h, w = img.shape[:2]
    s = 224 / min(h, w)
    img = cv2.resize(img, (max(224, round(w * s)), max(224, round(h * s))), interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    y, x = (h - 224) // 2, (w - 224) // 2
    img = cv2.cvtColor(img[y:y + 224, x:x + 224], cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return ((img - CLIP_MEAN) / CLIP_STD).transpose(2, 0, 1)


def embed(pictures, boxes=None):
    sess = clip_session()
    if sess is None:
        raise RuntimeError("the picture model isn't installed (run setup.sh again)")
    boxes = boxes or [None] * len(pictures)
    crops, owner = [], []  # every picture (or box) to run, and where its result goes
    for i, (data, bx) in enumerate(zip(pictures, boxes)):
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        for b in (bx if bx else [None]):
            if b:
                x, y, w, h = (int(v) for v in b)
                img_b = img[max(0, y):y + h, max(0, x):x + w]
            else:
                img_b = img
            crops.append(clip_input(img_b))
            owner.append(i)
    out = [([] if bx else None) for bx in boxes]
    ok = [k for k, c in enumerate(crops) if c is not None]
    for start in range(0, len(ok), 32):
        part = ok[start:start + 32]
        with _clip_lock:
            embs = sess.run(None, {"pixel_values": np.stack([crops[k] for k in part])})[0]
        for k, e in zip(part, embs):
            e = [round(float(v), 5) for v in e / (np.linalg.norm(e) or 1.0)]
            if boxes[owner[k]]:
                out[owner[k]].append(e)
            else:
                out[owner[k]] = e
    return out


_body = None
_body_lock = threading.Lock()


def body_parts(data):
    """Body parts in a picture (NudeNet's detector, as its own package runs
    it): pad to a square, 320x320, then keep boxes scoring 0.2+ after NMS."""
    global _body
    if _body is None:
        try:
            import onnxruntime as ort
            _body = ort.InferenceSession(BODY_MODEL, providers=_providers(ort))
        except Exception:
            _body = False
    if not _body:
        raise RuntimeError("the body-part model isn't installed (run setup.sh --body)")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    h, w = img.shape[:2]
    side = max(h, w)
    padded = cv2.copyMakeBorder(img, 0, side - h, 0, side - w, cv2.BORDER_CONSTANT)
    blob = cv2.dnn.blobFromImage(padded, 1 / 255.0, (320, 320), (0, 0, 0), swapRB=True, crop=False)
    with _body_lock:
        out = _body.run(None, {_body.get_inputs()[0].name: blob})[0]
    rows = np.transpose(np.squeeze(out))
    cls_scores = rows[:, 4:]
    best = cls_scores.argmax(1)
    score = cls_scores.max(1)
    keep = score >= 0.2
    boxes, scores, ids = [], [], []
    for (cx, cy, bw, bh), sc, ci in zip(rows[keep, :4], score[keep], best[keep]):
        x, y = (cx - bw / 2) * side / 320, (cy - bh / 2) * side / 320
        bw, bh = bw * side / 320, bh * side / 320
        x, y = max(0, min(x, w)), max(0, min(y, h))
        boxes.append([float(x), float(y), float(min(bw, w - x)), float(min(bh, h - y))])
        scores.append(float(sc))
        ids.append(int(ci))
    found = []
    for i in (cv2.dnn.NMSBoxes(boxes, scores, 0.25, 0.45) if boxes else []):
        i = int(i)
        found.append({"part": BODY_PARTS[ids[i]], "score": round(scores[i], 3),
                      "box": [round(v) for v in boxes[i]]})
    return found


def _q8(list_b64, dim=128):
    raw = np.frombuffer(b"".join(base64.b64decode(x) for x in list_b64), dtype=np.int8)
    return raw.reshape(-1, dim).astype(np.float32) / 127.0


def match_faces(faces, known):
    """For each face: the performer it looks most like, how alike (0-1), and
    how alike the closest *other* performer is. Many faces at once (the
    add-on's face review), in chunks to keep memory small."""
    owners, rows = [], []
    for pid, embs in known.items():
        for e in embs:
            owners.append(pid)
            rows.append(e)
    if not faces or not rows:
        return [[None, 0.0, 0.0] for _ in faces]
    K = _q8(rows)                      # (k, 128)
    order = np.argsort(np.array(owners, dtype=object).astype(str), kind="stable")
    K = K[order]
    owners = [owners[i] for i in order]
    starts = [0] + [i for i in range(1, len(owners)) if owners[i] != owners[i - 1]]
    pids = [owners[i] for i in starts]
    out = []
    for at in range(0, len(faces), 2000):
        F = _q8(faces[at:at + 2000])   # (f, 128)
        S = F @ K.T                    # (f, k)
        per = np.maximum.reduceat(S, starts, axis=1)  # (f, performers): best fingerprint of each
        top2 = np.argsort(-per, axis=1)[:, :2]
        for r in range(per.shape[0]):
            i1 = top2[r, 0]
            i2 = top2[r, 1] if per.shape[1] > 1 else None
            out.append([pids[i1], round(float(per[r, i1]), 4), round(float(per[r, i2]), 4) if i2 is not None else 0.0])
    return out


def nearest(vectors, base, k):
    """The add-on's look-alike tags for many items at once: each vector's k
    most alike base vectors (raw int8 dot products, as the add-on scores
    them), leaving the vector itself out."""
    if not vectors or not base:
        return [[] for _ in vectors]
    V = np.frombuffer(b"".join(base64.b64decode(x) for x in vectors), dtype=np.int8)
    V = V.reshape(len(vectors), -1).astype(np.float32)
    base = np.asarray(base, dtype=np.int64)
    B = V[base]                                   # (b, d)
    where = {int(b): j for j, b in enumerate(base)}  # vector index -> its column
    k = max(1, min(k, len(base)))
    out = []
    for at in range(0, len(vectors), 2000):
        S = V[at:at + 2000] @ B.T                 # (q, b)
        for r in range(S.shape[0]):
            j = where.get(at + r)
            if j is not None:
                S[r, j] = -np.inf
        top = np.argpartition(-S, k - 1, axis=1)[:, :k] if k < S.shape[1] else np.tile(np.arange(S.shape[1]), (S.shape[0], 1))
        for r in range(S.shape[0]):
            idx = top[r][np.argsort(-S[r, top[r]])]
            out.append([[int(base[i]), int(round(float(S[r, i])))] for i in idx if np.isfinite(S[r, i])])
    return out


def cluster_faces(faces, threshold, dim=128, center=False, batch=256):
    """Group faces of the same person (or pictures that look alike): each
    joins the most alike group (the average of its members) if it's at least
    `threshold` alike, else starts a new one; then every one is checked again
    against the final groups. Returns [group, likeness] each (group -1: alone).
    Works through them `batch` at a time against the groups so far (one matrix
    product each), so 100,000 pictures take seconds, not hours."""
    if not faces:
        return []
    F = _q8(faces, dim)
    F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
    if center:
        # picture fingerprints: compare what's special about each picture
        # (the library's average look taken away), or everything looks alike
        F -= F.mean(axis=0, keepdims=True)
        F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
    n, d = F.shape
    cap = min(n, 4096)
    sums = np.zeros((cap, d), dtype=np.float32)  # each group's summed members
    C = np.zeros((cap, d), dtype=np.float32)     # ...and their average, as a unit vector
    groups = 0
    for at in range(0, n, batch):
        X = F[at:at + batch]
        hit = np.zeros(len(X), dtype=bool)
        j = np.zeros(len(X), dtype=np.int64)
        if groups:
            S = X @ C[:groups].T
            j = np.argmax(S, axis=1)
            hit = S[np.arange(len(X)), j] >= threshold
            np.add.at(sums, j[hit], X[hit])
        touched = set(np.unique(j[hit]).tolist())
        start = groups
        for r in np.nonzero(~hit)[0]:  # the rest: against the groups this batch started
            x = X[r]
            if groups > start:
                local = sums[start:groups]
                sims = (local @ x) / (np.linalg.norm(local, axis=1) + 1e-9)
                k = int(np.argmax(sims))
                if sims[k] >= threshold:
                    sums[start + k] += x
                    continue
            if groups == cap:
                cap *= 2
                sums = np.vstack([sums, np.zeros_like(sums)])
                C = np.vstack([C, np.zeros_like(C)])
            sums[groups] = x
            groups += 1
        idx = np.array(sorted(touched | set(range(start, groups))), dtype=np.int64)
        if len(idx):
            C[idx] = sums[idx] / (np.linalg.norm(sums[idx], axis=1, keepdims=True) + 1e-9)
    C = C[:groups]
    out = []
    for at in range(0, n, 512):
        S = F[at:at + 512] @ C.T
        best = np.argmax(S, axis=1)
        for r, j in enumerate(best):
            sim = float(S[r, j])
            out.append([int(j) if sim >= threshold else -1, round(sim, 4)])
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "embed": os.path.exists(CLIP_MODEL), "body": os.path.exists(BODY_MODEL),
                             "match": True, "knn": True, "cluster": True, "cluster_fast": True, "ocr": HAVE_OCR, "apple": HAVE_OCR, "shotcrop": True,
                             "text": os.path.exists(CLIP_TEXT_MODEL) and os.path.exists(CLIP_TOKENIZER),
                             "gpu": _gpu()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/faces", "/embed", "/body", "/match", "/knn", "/cluster", "/ocr", "/apple", "/shotcrop", "/text", "/index", "/search"):
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            # read (and drop) what was sent first, so the sender gets the answer
            # instead of a broken connection
            left = length
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
            return self._send(413, {"error": "no body, or too big"})
        try:
            req = json.loads(self.rfile.read(length))
            if self.path == "/match":
                return self._send(200, {"results": match_faces(req.get("faces") or [], req.get("known") or {})})
            if self.path == "/cluster":
                return self._send(200, {"results": cluster_faces(req.get("faces") or [], float(req.get("threshold") or 0.45),
                                                                 int(req.get("dim") or 128), bool(req.get("center")))})
            if self.path == "/knn":
                return self._send(200, {"results": nearest(req.get("vectors") or [], req.get("base") or [],
                                                           int(req.get("k") or 20))})
            if self.path == "/index":
                return self._send(200, index_put(req["name"], req["version"], req.get("keys") or [], req.get("vectors") or [], req.get("groups")))
            if self.path == "/search":
                texts = req.get("texts") or [req.get("text") or ""]
                return self._send(200, index_search(req["name"], req["version"], texts, req.get("k") or 60, req.get("kinds"), int(req.get("per_group") or 2)))
            if self.path == "/text":
                return self._send(200, {"results": clip_text(req.get("texts") or [])})
            pictures = [base64.b64decode(b) for b in req.get("images") or []]
            if self.path == "/shotcrop":
                fmts = req.get("format") or []
                results = [shot_crop(p, fmts[i] if i < len(fmts) else "jpg") for i, p in enumerate(pictures)]
            elif self.path == "/apple":
                if not HAVE_OCR:
                    return self._send(501, {"error": "no Apple Vision here (it needs a Mac; run setup.sh again there)"})
                if req.get("facts"):
                    results = [apple_facts(p) for p in pictures]
                elif req.get("card"):
                    results = [card_crop(p) for p in pictures]
                elif req.get("aesthetics_only"):
                    results = [apple_aesthetics(p, bool(req.get("people"))) for p in pictures]
                else:
                    results = [(apple_pose if req.get("pose_only") else apple_look)(p) for p in pictures]
            elif self.path == "/ocr":
                if not HAVE_OCR:
                    return self._send(501, {"error": "no text reader here (it needs a Mac; run setup.sh again there)"})
                results = [read_text(p) for p in pictures]
            elif self.path == "/embed":
                results = embed(pictures, req.get("boxes"))
            elif self.path == "/body":
                results = [body_parts(p) for p in pictures]
            else:
                results = [faces_in(p) for p in pictures]
        except Exception as e:  # bad input: say so, keep running
            return self._send(400, {"error": str(e)})
        self._send(200, {"results": results})

    def log_message(self, fmt, *args):  # quiet
        pass


# --- reading text (Apple's Vision, macOS only) ------------------------------
try:
    import Quartz
    import Vision
    from Foundation import NSData
    HAVE_OCR = True
except Exception:  # not a Mac, or pyobjc isn't installed (setup.sh installs it on a Mac)
    HAVE_OCR = False


def read_text(picture):
    """[[height, x, y from the top, text], ...] for one picture (bytes)."""
    if not HAVE_OCR:
        raise RuntimeError("no text reader here (it needs a Mac; run setup.sh again there)")
    data = NSData.dataWithBytes_length_(picture, len(picture))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None
    if img is None:
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    handler.performRequests_error_([req], None)
    out = []
    for o in req.results() or []:
        box = o.boundingBox()
        cand = o.topCandidates_(1)
        if cand:
            out.append([round(box.size.height, 4), round(box.origin.x, 4),
                        round(1 - box.origin.y - box.size.height, 4), str(cand[0].string())])
    return out


def _apple_picture(picture):
    """The picture for Apple's Vision, the way it's meant to be seen (a phone
    photo's rotation applied, as OpenCV does): (image, orientation, width, height)."""
    data = NSData.dataWithBytes_length_(picture, len(picture))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None) if src else None
    if img is None:
        return None
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(src, 0, None) or {}
    orient = int(props.get("Orientation") or 1)
    w, h = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    if orient in (5, 6, 7, 8):
        w, h = h, w
    return img, orient, w, h


def _apple_run(pic, requests):
    img, orient = pic[0], pic[1]
    Vision.VNImageRequestHandler.alloc().initWithCGImage_orientation_options_(img, orient, None).performRequests_error_(requests, None)


def _people(pose, w, h):
    """Each person's joints: {"left_leg": [x, y, confidence]} in shares of the longer side, y from the top."""
    side = max(w, h) or 1
    out = []
    for obs in pose.results() or []:
        pts, _ = obs.recognizedPointsForGroupKey_error_(Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None)
        person = {str(k).replace("_joint", "").replace("_1", ""):
                  [round(v.location().x * w / side, 4), round((1 - v.location().y) * h / side, 4), round(float(v.confidence()), 2)]
                  for k, v in (pts or {}).items() if v.confidence() > 0.1}
        if person:
            out.append(person)
    return out


def apple_pose(picture):
    """Just the people's body joints in one picture (bytes)."""
    pic = _apple_picture(picture)
    if pic is None:
        return None
    pose = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
    _apple_run(pic, [pose])
    return {"size": [pic[2], pic[3]], "people": _people(pose, pic[2], pic[3])}


PERSON_LABELS = ("people", "adult", "child", "baby", "teen", "selfie", "portrait", "crowd", "wedding")


def apple_aesthetics(picture, people=False):
    """Apple's aesthetics score for one picture (bytes): {"score": -1..1,
    "utility": bool}, and with people, "person": whether anyone's in it - a
    person or face (Apple), a "people" label, a body pose, or a body part
    (NudeNet, for close-ups). On 300 marker pictures it only missed people in
    a close-up of feet and one of a tied-up back; title cards, logos and empty
    rooms have nobody."""
    pic = _apple_picture(picture)
    if pic is None or not hasattr(Vision, "VNCalculateImageAestheticsScoresRequest"):
        return None
    req = Vision.VNCalculateImageAestheticsScoresRequest.alloc().init()
    more = []
    if people:
        human = Vision.VNDetectHumanRectanglesRequest.alloc().init()
        human.setUpperBodyOnly_(False)
        more = [human, Vision.VNDetectFaceRectanglesRequest.alloc().init(), Vision.VNClassifyImageRequest.alloc().init(),
                Vision.VNDetectHumanBodyPoseRequest.alloc().init()]
    _apple_run(pic, [req] + more)
    res = req.results()
    if not res:
        return None
    out = {"score": round(float(res[0].overallScore()), 3), "utility": bool(res[0].isUtility())}
    if people:
        human, face, cls, pose = more
        seen = (any(o.confidence() > 0.3 and o.boundingBox().size.width * o.boundingBox().size.height >= 0.03
                    for o in human.results() or [])
                or bool(face.results())
                or any(str(o.identifier()) in PERSON_LABELS and o.confidence() >= 0.3 for o in cls.results() or [])
                or any(sum(1 for v in pp.values() if v[2] >= 0.3) >= 4 for pp in _people(pose, 1, 1)))
        if not seen and os.path.exists(BODY_MODEL):
            with contextlib.suppress(Exception):
                seen = any(d["score"] >= 0.3 for d in body_parts(picture))
        # a logo or title card with a drawn figure (a face is found, but Apple
        # calls it art and screenshot-like): nobody
        drawn = any(str(o.identifier()) in ("art", "illustrations", "cartoon", "drawing", "comics") and o.confidence() >= 0.3
                    for o in cls.results() or [])
        out["person"] = bool(seen) and not (out["utility"] and drawn)
    return out


CARD_ASPECT = 16 / 9


def card_crop(picture):
    """A tall picture cut to a wide (16:9) one around the people: faces kept in
    (and the rest of them when it fits), from Apple's face and person
    detection and the body-part detector. None when the picture isn't tall."""
    img = cv2.imdecode(np.frombuffer(picture, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    ch = int(round(w / CARD_ASPECT))
    if h <= w * 1.05 or ch >= h:
        return {"crop": None}
    faces, boxes = [], []  # (top, bottom) in pixels
    pic = _apple_picture(picture) if HAVE_OCR else None
    if pic is not None:
        human = Vision.VNDetectHumanRectanglesRequest.alloc().init()
        human.setUpperBodyOnly_(False)
        face = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        _apple_run(pic, [human, face])
        def span(o):
            b = o.boundingBox()
            return ((1 - b.origin.y - b.size.height) * h, (1 - b.origin.y) * h)
        faces = [span(o) for o in face.results() or []]
        boxes += [span(o) for o in human.results() or [] if o.confidence() > 0.3]
    if os.path.exists(BODY_MODEL):
        with contextlib.suppress(Exception):
            boxes += [(d["box"][1], d["box"][1] + d["box"][3]) for d in body_parts(picture) if d["score"] >= 0.35]
    every = faces + boxes
    if every:
        top, bottom = min(t for t, _ in every), max(b for _, b in every)
        if bottom - top <= ch:
            y0 = (top + bottom) / 2 - ch / 2      # everyone fits: centred on them
        else:
            # too tall to fit: from just above the faces (or the people's top)
            y0 = (min(t for t, _ in faces) if faces else top) - 0.08 * ch
    else:
        y0 = (h - ch) * 0.35                     # nobody found: a little above the middle
    y0 = int(max(0, min(h - ch, y0)))
    ok, jpg = cv2.imencode(".jpg", img[y0:y0 + ch], [cv2.IMWRITE_JPEG_QUALITY, 90])
    return {"crop": base64.b64encode(jpg.tobytes()).decode() if ok else None, "top": round(y0 / h, 3)}


def _runs(flags, gap):
    """[(start, end)] of runs of True, joined across gaps up to `gap`."""
    out, start, last = [], None, None
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            elif i - last > gap + 1:
                out.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        out.append((start, last + 1))
    return out


SHOT_SOLID = 0.8        # a row this flat is the app's bar (the photo stops there)
SHOT_SOLID_COLS = 0.97  # the same for a column (margins beside a photo)


def shot_box(img):
    """The photo in a phone screenshot of a post, (x0, y0, x1, y1) in pixels,
    or None when it isn't one. A screenshot is phone-screen sized (700+ wide,
    tall) with the phone's flat status bar on top; the app around the photo
    is rows mostly flat in the app's own colours, the photo rows aren't, and
    the crop then grows out to the app's nearly solid bars, so it errs a
    little loose (a header line kept) rather than cutting into the photo.
    Checked by eye on 83 screenshots: none cut into the photo."""
    h, w = img.shape[:2]
    if h / w < 1.75 or w < 700:
        return None
    sw = 240
    sh = int(h * sw / w)
    small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.int16)

    def flat_share(lines):
        bg = np.median(np.concatenate([lines[:, :6], lines[:, -6:]], 1), axis=1, keepdims=True)
        return (np.abs(lines - bg).max(axis=2) < 14).mean(axis=1), bg[:, 0, :]
    rows, row_bg = flat_share(small)
    if rows[:max(2, int(sh * 0.03))].mean() < 0.8:
        return None  # no status bar: a photo, not a screenshot
    # the app's own colours: its flat bars at the top and bottom of the screen
    edge = np.r_[0:int(sh * 0.12), int(sh * 0.85):sh]
    ui = [row_bg[i] for i in edge if rows[i] >= 0.85]
    ui = np.unique(np.round(np.array(ui) / 8) * 8, axis=0) if ui else np.zeros((0, 3))
    near_ui = (np.abs(row_bg[:, None, :] - ui[None, :, :]).max(axis=2).min(axis=1) < 20) if len(ui) else np.zeros(sh, bool)
    # a row is the app when it's mostly flat in the app's colour (bars,
    # captions); flat rows of another colour (sky, a wall) are the photo
    app_rows = (rows >= 0.6) & near_ui
    band = max(_runs(~app_rows & (rows < 0.97), max(2, sh // 80)), key=lambda r: r[1] - r[0], default=None)
    if not band or band[1] - band[0] < 0.2 * sh:
        return None
    y0, y1 = band
    # grow it out to the app's bars: only rows that are almost all one flat
    # colour stop it (a plain part of the photo near its edge doesn't), so a
    # crop errs a little loose rather than cutting into the photo
    while y0 > 0 and rows[y0 - 1] < SHOT_SOLID:
        y0 -= 1
    while y1 < sh and rows[y1] < SHOT_SOLID:
        y1 += 1
    cols = flat_share(small[y0:y1].transpose(1, 0, 2))[0]
    x0, x1 = max(_runs(cols < 0.45, 2), key=lambda r: r[1] - r[0], default=(0, sw))
    while x0 > 0 and cols[x0 - 1] < SHOT_SOLID_COLS:
        x0 -= 1
    while x1 < sw and cols[x1] < SHOT_SOLID_COLS:
        x1 += 1
    # posts fill the screen's width, unless set in the middle with even margins
    if not (x0 >= 0.03 * sw and abs(x0 - (sw - x1)) <= 0.04 * sw):
        x0, x1 = 0, sw
    if x1 - x0 < 0.4 * sw or (x1 - x0) * (y1 - y0) > 0.9 * sw * sh:
        return None
    f = w / sw
    return int(x0 * f), int(y0 * f), min(w, int(round(x1 * f))), min(h, int(round(y1 * f)))


def shot_crop(picture, fmt="jpg"):
    img = cv2.imdecode(np.frombuffer(picture, np.uint8), cv2.IMREAD_COLOR)
    box = shot_box(img) if img is not None else None
    if not box:
        return {"box": None}
    x0, y0, x1, y1 = box
    ext = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp"}.get((fmt or "jpg").lower(), ".jpg")
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext == ".jpg" else [cv2.IMWRITE_WEBP_QUALITY, 95] if ext == ".webp" else []
    ok, data = cv2.imencode(ext, img[y0:y1, x0:x1], params)
    return {"box": list(box), "crop": base64.b64encode(data.tobytes()).decode()} if ok else {"box": None}


# Apple's labels that say little about a picture (they're on nearly all)
FACT_SKIP = {"people", "adult", "structure", "material", "textile", "wood_processed", "housewares", "furniture",
             "clothing", "indoor", "art", "illustrations", "document", "screenshot", "blue_sky", "liquid",
             "water_body", "land", "tool", "conveyance", "consumable", "container", "decoration", "machine"}
FACT_MIN = 0.15  # Apple's scene labels run low (a beach photo: beach 0.24), the hierarchy above them high


def apple_facts(picture):
    """Apple's aesthetics score and picture labels in one go: {"score": -1..1,
    "utility": bool, "labels": [[label, confidence], ...] (FACT_MIN+, most sure first)}."""
    pic = _apple_picture(picture)
    if pic is None:
        return None
    reqs = [Vision.VNClassifyImageRequest.alloc().init()]
    if hasattr(Vision, "VNCalculateImageAestheticsScoresRequest"):
        reqs.append(Vision.VNCalculateImageAestheticsScoresRequest.alloc().init())
    _apple_run(pic, reqs)
    labels = sorted(((str(o.identifier()), round(float(o.confidence()), 3)) for o in reqs[0].results() or []
                     if o.confidence() >= FACT_MIN and str(o.identifier()) not in FACT_SKIP), key=lambda x: -x[1])
    out = {"labels": [list(x) for x in labels[:12]], "score": None, "utility": None}
    if len(reqs) > 1 and reqs[1].results():
        r = reqs[1].results()[0]
        out["score"], out["utility"] = round(float(r.overallScore()), 3), bool(r.isUtility())
    return out


def apple_look(picture):
    """Apple's picture classifier, hands and chest areas (and everyone's body
    joints) for one picture (bytes)."""
    if not HAVE_OCR:
        raise RuntimeError("no Apple Vision here (it needs a Mac; run setup.sh again there)")
    pic = _apple_picture(picture)
    if pic is None:
        return None
    cls = Vision.VNClassifyImageRequest.alloc().init()
    hand = Vision.VNDetectHumanHandPoseRequest.alloc().init()
    hand.setMaximumHandCount_(6)
    pose = Vision.VNDetectHumanBodyPoseRequest.alloc().init()
    _apple_run(pic, [cls, hand, pose])
    labels = {str(o.identifier()): round(float(o.confidence()), 3) for o in cls.results() or [] if o.confidence() >= 0.2}
    hands = []
    for obs in hand.results() or []:
        pts, _ = obs.recognizedPointsForGroupKey_error_(Vision.VNHumanHandPoseObservationJointsGroupNameAll, None)
        hands.append([[round(p.location().x, 4), round(1 - p.location().y, 4)] for p in (pts or {}).values() if p.confidence() > 0.3])
    chests = []
    for obs in pose.results() or []:
        pts, _ = obs.recognizedPointsForGroupKey_error_(Vision.VNHumanBodyPoseObservationJointsGroupNameAll, None)
        P = {str(k): (v.location().x, 1 - v.location().y) for k, v in (pts or {}).items() if v.confidence() > 0.3}
        ls, rs = P.get("left_shoulder_1_joint"), P.get("right_shoulder_1_joint")
        hips = [q[1] for q in (P.get("left_upLeg_joint"), P.get("right_upLeg_joint")) if q]
        if ls and rs:
            top = min(ls[1], rs[1])
            hip = max(hips) if hips else top + abs(ls[0] - rs[0]) * 1.6
            chests.append([round(min(ls[0], rs[0]), 4), round(top, 4), round(max(ls[0], rs[0]), 4), round(top + (hip - top) * 0.5, 4)])
    return {"size": [pic[2], pic[3]], "labels": labels, "hands": hands, "chests": chests,
            "people": _people(pose, pic[2], pic[3])}


class Server(ThreadingHTTPServer):
    # The standard server looks up this computer's name when it starts; that
    # can hang for minutes (e.g. with a VPN's DNS), so skip it
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


if __name__ == "__main__":
    # where it listens: 127.0.0.1 (this computer only) unless told otherwise -
    # --host 0.0.0.0 (or FACE_HELPER_HOST) for Stash in Docker on Windows, or
    # on another computer; --port (or FACE_HELPER_PORT), default 11535
    args = sys.argv[1:]

    def arg(name, default):
        return args[args.index(name) + 1] if name in args[:-1] else default

    host = arg("--host", os.environ.get("FACE_HELPER_HOST", "127.0.0.1"))
    port = int(arg("--port", os.environ.get("FACE_HELPER_PORT", "11535")))
    Server((host, port), Handler).serve_forever()
