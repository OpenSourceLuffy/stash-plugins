#!/bin/sh
# Sets up the face helper for Better Tagging AI Add-on on a Mac (or Linux):
# a Python environment with OpenCV, the face and picture models, and (on a Mac) a
# login service so it starts by itself. Run it once:  sh setup.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${FACE_HELPER_DIR:-$HOME/Library/Application Support/face-helper}"
[ "$(uname)" = "Darwin" ] || DEST="${FACE_HELPER_DIR:-$HOME/.local/share/face-helper}"
PY="${PYTHON:-python3}"

mkdir -p "$DEST/models"
cp "$HERE/face_helper.py" "$DEST/"
"$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q opencv-python-headless numpy onnxruntime
ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
curl -sSL -o "$DEST/models/face_detection_yunet_2023mar.onnx" "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx"
curl -sSL -o "$DEST/models/face_recognition_sface_2021dec.onnx" "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx"
# CLIP ViT-B/32 picture model (about 350 MB), for tags from look-alikes
curl -sSL -o "$DEST/models/clip_vit_b32_vision.onnx" "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx"

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
