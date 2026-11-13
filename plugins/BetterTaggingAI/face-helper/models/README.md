# Models

The face helper's small models are kept here, so `setup.sh` works even if the places they came
from move or go away. Each one is an unchanged copy of the original file (checked against
`SHA256SUMS`), shared under its own licence, found in [`licenses/`](licenses).

| File | What it does | From | Licence |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` (0.2 MB) | Finds faces in a picture | [OpenCV Zoo: YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT ([LICENSE-YuNet.txt](licenses/LICENSE-YuNet.txt)) |
| `face_recognition_sface_2021dec.onnx` (39 MB) | A fingerprint of each face, to match it with performers | [OpenCV Zoo: SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | Apache 2.0 ([LICENSE-SFace.txt](licenses/LICENSE-SFace.txt)) |
| `nudenet_320n.onnx` (12 MB) | Finds body parts, for body tags | [NudeNet 3.4.2](https://github.com/notAI-tech/NudeNet) (`nudenet/320n.onnx` in its Python package) | AGPL-3.0 ([LICENSE-NudeNet.txt](licenses/LICENSE-NudeNet.txt), the licence file shipped in that package) |

## Not kept here (too big for GitHub)

- **CLIP ViT-B/32 picture model** (`clip_vit_b32_vision.onnx`, 352 MB, used for look-alikes):
  `setup.sh` downloads it from
  [Hugging Face: Xenova/clip-vit-base-patch32](https://huggingface.co/Xenova/clip-vit-base-patch32/blob/main/onnx/vision_model.onnx),
  an ONNX export of [OpenAI's CLIP](https://github.com/openai/CLIP), MIT
  ([LICENSE-CLIP.txt](licenses/LICENSE-CLIP.txt)). Its checksum is in `SHA256SUMS` too.
- **The AI model for tags, titles and reading watermarks** runs in [Ollama](https://ollama.com),
  not in the face helper. The add-on is set up for **Gemma 3 4B**:
  [ollama.com/library/gemma3](https://ollama.com/library/gemma3), installed with
  `ollama pull gemma3:4b` (about 3.4 GB). It's shared under Google's
  [Gemma Terms of Use](https://ai.google.dev/gemma/terms).

## Restoring from a backup

`sh setup.sh --from /path/to/backup` takes every model (CLIP included) from a folder with the same
file names instead of downloading anything, e.g. a copy kept on a NAS.
