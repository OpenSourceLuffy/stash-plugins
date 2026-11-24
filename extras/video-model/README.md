# Video model files (mirror)

Files Better Organizing AI downloads for **AI markers** and **positions in videos** (the task
**Video model: download it**), kept here so the add-on doesn't depend on the Auto Seek plugin or its
author's hosting. They are not part of any plugin's download; the add-on fetches only what the
computer running Stash needs, and checks each against the SHA-256 in [`SHA256SUMS`](SHA256SUMS) (the
same hashes pinned in Auto Seek 0.3.1's `auto_seek.py`).

| File | What | From | Licence |
|---|---|---|---|
| `auto-seek-clip3-v2.onnx` (89 MB) | Auto Seek's video model (a CLIP model tuned on videos; based on LAION's CLIP ViT-B/32 and ViT-B/16) | [huggingface.co/mrx-31415/auto-seek](https://huggingface.co/mrx-31415/auto-seek) | MIT |
| `onnxruntime-alpine-3.23-{x86_64,aarch64,armv7}.tar.gz` (8-9 MB each) | ONNX Runtime 1.31.1 built for Alpine (musl), as Stash's Docker image uses | Auto Seek's [runtime release](https://github.com/mrx-31415/stash-curator/releases/tag/auto-seek-runtime-v1) | MIT (ONNX Runtime); the libraries bundled in each archive are listed with their licences in its `NOTICE.txt` and `PACKAGES.txt` |

Windows, macOS and ordinary (glibc) Linux use Microsoft's official ONNX Runtime release builds,
downloaded from Microsoft. The code that runs the model is in
[`plugins/BetterTaggingAI/video-model`](../../plugins/BetterTaggingAI/video-model) (AGPL-3.0).
