# Video model code (from Auto Seek)

The code Better Organizing AI uses to run the video model for **AI markers** and **positions in
videos**, so neither needs the Auto Seek plugin.

- `auto_seek.py` and `ort_api.py` are **unchanged copies** from the [Auto Seek](https://github.com/mrx-31415/stash-auto-seek)
  plugin, version **0.3.1** (its plugin package, `auto-seek.zip`, SHA-256
  `161d7e14a20f5d76de04e139d37d60c5b9633ce6a55dc39f6c285a5221ce182c`). They are licensed under the
  **GNU AGPL-3.0** ([`LICENSE`](LICENSE)), unlike the rest of this repository (MIT); this folder's
  source is the source of that part.
- The add-on runs them with their data in Stash's config folder
  (`plugin-data/BetterTaggingAI/auto-seek/data`). The task **Video model: download it** fills that in:
  the model and the Alpine (Docker) runtimes come from this repository's
  [`extras/video-model`](../../../extras/video-model) (each checked against its SHA-256), the other
  runtimes from Microsoft's ONNX Runtime releases; if this repository can't be reached, the original
  addresses in `auto_seek.py` are used.
- To update: copy `auto_seek.py`, `ort_api.py` and `LICENSE` from a newer Auto Seek release, check the
  model and runtime hashes in `auto_seek.py` still match `extras/video-model/SHA256SUMS` (or add the
  new files there), and note the version here.
