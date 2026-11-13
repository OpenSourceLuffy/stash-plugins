#!/bin/sh
# Sets up the face helper for Better Tagging AI Add-on on a Mac (or Linux):
# a Python environment with OpenCV, the face and picture models, and (on a Mac) a
# login service so it starts by itself. Run it once:  sh setup.sh
#
# The small models (YuNet, SFace, NudeNet) come with it, in models/ (see
# models/README.md for where each is from and its licence). The CLIP picture
# model (about 350 MB) is downloaded. Options:
#   --from DIR   take every model, CLIP included, from DIR instead (a backup,
#                e.g. on a NAS); nothing is downloaded
#   --body       (kept for older instructions; NudeNet is always set up now)
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${FACE_HELPER_DIR:-$HOME/Library/Application Support/face-helper}"
[ "$(uname)" = "Darwin" ] || DEST="${FACE_HELPER_DIR:-$HOME/.local/share/face-helper}"
# a Python that onnxruntime has packages for (it lags behind the newest
# Python); PYTHON=... picks one yourself
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for v in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$v" >/dev/null 2>&1 && { PY="$v"; break; }
  done
fi
FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --body) shift ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

SMALL="face_detection_yunet_2023mar.onnx face_recognition_sface_2021dec.onnx nudenet_320n.onnx"
CLIP=clip_vit_b32_vision.onnx
CLIP_URL="https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx"

# a model file is right if its checksum is the one in models/SHA256SUMS
check() {
  want="$(grep " $2\$" "$HERE/models/SHA256SUMS" | cut -d' ' -f1)"
  got="$( (shasum -a 256 "$1" 2>/dev/null || sha256sum "$1") | cut -d' ' -f1)"
  [ -n "$want" ] && [ "$want" = "$got" ]
}

mkdir -p "$DEST/models"
cp "$HERE/face_helper.py" "$DEST/"
"$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q opencv-python-headless numpy onnxruntime || {
  echo "Couldn't install the Python packages with $("$PY" --version). onnxruntime may not support it yet:"
  echo "install an older Python (e.g. brew install python@3.13) and run: PYTHON=python3.13 sh setup.sh"
  exit 1
}

SRC="${FROM:-$HERE/models}"
for f in $SMALL; do
  [ -f "$SRC/$f" ] || { echo "Missing $SRC/$f"; exit 1; }
  check "$SRC/$f" "$f" || { echo "$SRC/$f isn't the expected file (checksum)"; exit 1; }
  cp "$SRC/$f" "$DEST/models/$f"
done
if [ -n "$FROM" ]; then
  [ -f "$FROM/$CLIP" ] || { echo "Missing $FROM/$CLIP"; exit 1; }
  cp "$FROM/$CLIP" "$DEST/models/$CLIP"
elif ! check "$DEST/models/$CLIP" "$CLIP"; then
  # CLIP ViT-B/32 picture model (about 350 MB), for tags from look-alikes
  curl -sSLf -o "$DEST/models/$CLIP" "$CLIP_URL"
fi
check "$DEST/models/$CLIP" "$CLIP" || echo "Note: $CLIP isn't the checked version (it still works if it's a CLIP ViT-B/32 vision model)."

if [ "$(uname)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/face-helper.plist"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>face-helper</string>
  <key>ProgramArguments</key><array>
    <string>$DEST/venv/bin/python</string><string>$DEST/face_helper.py</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DEST/face-helper.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/face-helper" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Face helper installed in $DEST and started (it starts at login too)."
else
  echo "Face helper installed in $DEST. Start it with: $DEST/venv/bin/python $DEST/face_helper.py"
fi
echo "Check it: curl http://127.0.0.1:11535/health"
echo "The AI model for tags and titles runs in Ollama (https://ollama.com): ollama pull gemma3:4b"
echo "  (Gemma 3: https://ollama.com/library/gemma3)"
