"""Face helper for Better Tagging AI Add-on.

A tiny local web service: send it pictures, get back the faces in them and a
"fingerprint" (128 numbers) for each face. Two faces with similar
fingerprints are likely the same person. It keeps nothing; the plugin keeps
the performers' fingerprints itself.

Models (OpenCV model zoo): YuNet finds faces, SFace makes the fingerprints.

    POST /faces  {"images": ["<base64 JPEG/PNG>", ...]}
      -> {"results": [[{"box": [x, y, w, h], "score": 0.98, "emb": [...]}, ...], ...]}
    GET /health  -> {"ok": true}

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
MAX_SIDE = 1280      # bigger pictures are scaled down first (faster, same result)
MIN_FACE = 40        # px; smaller faces are too blurry to recognise
MAX_FACES = 8        # per picture, biggest first
MAX_BODY = 60 * 1024 * 1024

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
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/faces":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "no body, or too big"})
        try:
            req = json.loads(self.rfile.read(length))
            results = [faces_in(base64.b64decode(b)) for b in req.get("images") or []]
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
