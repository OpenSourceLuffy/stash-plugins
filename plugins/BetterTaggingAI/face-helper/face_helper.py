"""Face helper for Better Tagging AI Add-on.

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
    GET /health  -> {"ok": true, "embed": true/false}

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
            self._send(200, {"ok": True, "embed": os.path.exists(CLIP_MODEL)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/faces", "/embed"):
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
            pictures = [base64.b64decode(b) for b in req.get("images") or []]
            results = embed(pictures, req.get("boxes")) if self.path == "/embed" else [faces_in(p) for p in pictures]
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
