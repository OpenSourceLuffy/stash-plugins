# Vertical Cover Stitcher

Stash's covers are 16:9, so a single frame from a portrait (vertical) video ends up
as a thin strip with big black bars. This plugin builds a proper horizontal cover
instead by taking several evenly spaced frames from the video and placing them side
by side.

<!-- Add a before/after screenshot here -->

## How it works

- **Automatically**: when a scan adds a new scene, it checks whether the video is
  vertical (taller than it is wide, and longer than 5 seconds), and if so sets a
  stitched cover.
- **On demand**: under **Settings → Tasks → Plugin Tasks**:
  - **Stitch all vertical scenes**: redo the cover of every vertical scene.
  - **Stitch unorganized vertical scenes**: only the ones not marked as organized.

Horizontal videos are never touched.

## Settings

| Setting | What it does |
| --- | --- |
| Frames per cover | How many frames to stitch side by side: 2, 3 (default) or 4. |

## Requirements

- Python 3 (only the standard library; no extra packages).
- ffmpeg. It uses the one Stash uses: the path set in **Settings → System →
  Application Paths**, the system PATH, or the copy Stash downloads into its config
  folder. The official Docker image already includes it.

## Notes

- It replaces the scene's current cover. Scraping or identifying the scene later
  may replace it again with the site's own cover.
- Covers are 1280×720 JPEGs.
