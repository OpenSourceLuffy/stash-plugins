# Vertical Cover Stitcher changelog

Newest first.

## 1.4 (2026-11-23)

- **No more end cards**: frames are only taken from the video's own content. TikTok's and
  Instagram's end cards (a dark screen with their logo and the account), a creator's logo card on
  black, and the last 2 seconds (where screen recordings show Control Center) are left out.
  Checked on 571 vertical clips: 227 had an end card, and the old evenly spaced frames often landed on it.
- **Better-looking frames with Better Organizing AI**: when the [AI add-on](../BetterTaggingAI) is
  installed and its face helper runs on a Mac, each frame is the best of three from its part of the
  video by Apple's aesthetics score (people preferred; text screens, blurry or dark frames score low).
  Without it, frames are evenly spaced as before.
- Each stitched cover is noted in Stash's config folder (`plugin-data/vertical_stitcher/stitched.json`),
  so the AI add-on's cover tasks know it's a stitched cover and leave it alone.
- The log says when each frame was taken.
- Windows: no console window flashes up for each ffmpeg run; names and "→" in the log come out right
  (UTF-8 pipes); the stitched-cover record is saved even while another process is reading it.
- Long runs ("Stitch all vertical scenes") use Stash's API key when it has one, so they don't stop
  when the sign-in Stash handed the task runs out.

## 1.3 (2026-11-04)

- First release in this repository.
