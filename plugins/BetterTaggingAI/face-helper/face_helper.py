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
    POST /cluster {"faces": ["<q8>", ...], "threshold": 0.45}
      -> {"results": [[group number, likeness to the group], ...]}: the same
         person's faces get the same number (for "new performers")
    GET /health  -> {"ok": true, "embed": true/false, "body": true/false, "match": true, "knn": true, "cluster": true}

Runs on 127.0.0.1 only (Stash's Docker container reaches it as
host.docker.internal). Port: FACE_HELPER_PORT, default 11535.
"""
import base64
import json
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


def clip_session():
    """The picture model, loaded the first time it's needed (None if it
    isn't installed)."""
    global _clip
    if _clip is None:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 4
            _clip = ort.InferenceSession(CLIP_MODEL, opts, providers=["CPUExecutionProvider"])
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
            _body = ort.InferenceSession(BODY_MODEL, providers=["CPUExecutionProvider"])
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


def _q8(list_b64):
    raw = np.frombuffer(b"".join(base64.b64decode(x) for x in list_b64), dtype=np.int8)
    return raw.reshape(-1, 128).astype(np.float32) / 127.0


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


def cluster_faces(faces, threshold):
    """Group faces of the same person: each face joins the most alike group
    (the average of its faces) if it's at least `threshold` alike, else
    starts a new one; then every face is checked again against the final
    groups. Returns [group, likeness] per face (group -1: alone)."""
    if not faces:
        return []
    F = _q8(faces)
    F /= np.linalg.norm(F, axis=1, keepdims=True) + 1e-9
    n = len(F)
    sums = np.zeros((n, F.shape[1]), dtype=np.float32)  # each group's summed faces
    count = np.zeros(n, dtype=np.int32)
    groups = 0
    for i in range(n):
        if groups:
            C = sums[:groups]
            sims = (C @ F[i]) / (np.linalg.norm(C, axis=1) + 1e-9)
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                sums[j] += F[i]
                count[j] += 1
                continue
        sums[groups] = F[i]
        count[groups] = 1
        groups += 1
    C = sums[:groups] / (np.linalg.norm(sums[:groups], axis=1, keepdims=True) + 1e-9)
    out = []
    for at in range(0, n, 4000):
        S = F[at:at + 4000] @ C.T
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
                             "match": True, "knn": True, "cluster": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/faces", "/embed", "/body", "/match", "/knn", "/cluster"):
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
                return self._send(200, {"results": cluster_faces(req.get("faces") or [], float(req.get("threshold") or 0.45))})
            if self.path == "/knn":
                return self._send(200, {"results": nearest(req.get("vectors") or [], req.get("base") or [],
                                                           int(req.get("k") or 20))})
            pictures = [base64.b64decode(b) for b in req.get("images") or []]
            if self.path == "/embed":
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


class Server(ThreadingHTTPServer):
    # The standard server looks up this computer's name when it starts; that
    # can hang for minutes (e.g. with a VPN's DNS), so skip it
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


if __name__ == "__main__":
    port = int(os.environ.get("FACE_HELPER_PORT", "11535"))
    Server(("127.0.0.1", port), Handler).serve_forever()
