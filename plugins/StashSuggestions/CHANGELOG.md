# Stash Suggestions changelog

Newest first.

## 1.5.1 (2026-11-27)

- Works with light themes and other UI plugins: the Suggested rows' scroll arrows and the studio
  cards' backgrounds take their colour from the theme's text, so they show on light and dark themes
  alike (on a light theme the arrows were nearly invisible), and "See all" and the current photo's
  outline use the theme's main colour when Modern UI isn't on.
- The update window (shown when one of your OpenSourceLuffy plugins has a new version) follows your
  theme or UI plugin: it's now Stash's own dialog, so the theme colours it.


## 1.5 (2026-11-24)

- **Windows**: when Stash is installed directly on Windows and runs the Microsoft Store's stand-in for
  Python (tasks fail with "exit status 9009"), the plugin's pages now say what that means and how to
  fix it: Stash's **Settings → System → Python executable path**.
- **Settings** (Settings → Plugins → Stash Suggestions) for how each row picks:
  - How much each thing counts, as a percentage: 100 = as before, 0 = left out, 200 = counts double.
    Scenes: shared tags, same studio, look-alike performers, scenes that look alike, similar length,
    same shape. Photos: shared tags, same gallery, look-alike performers, photos that look alike,
    better-looking photos. Similar Performers: shared tags, details in common, alike faces, women
    first. Similar galleries: shared tags, same performers, same studio.
  - New things to count, off until you set them: rating (scenes and photos), favourite performers
    (scenes, photos and Similar Performers) and scenes you haven't watched yet.
  - How many each row shows (default 12, up to 60); for Similar photos, also how many **Load more**
    adds.
  - With nothing set, every row works exactly as before.

## 1.4.1 (2026-11-24)

- **Update check**: when a newer version of any of these plugins is out, Stash shows what's new
  (from the changelogs) with **Yes, install** (for plugins installed through the plugin source; ones
  copied in by hand are told where to update), **Later** (a day) and **Don't show again**. It looks
  twice a day at most, reading the published plugin list and changelogs from GitHub - nothing is
  sent. Shared by Better Organizing Tools / AI, Modern UI, Privacy Blur and Stash Suggestions: one
  check covers all seven plugins.

## 1.4 (2026-11-23)

- **Similar photos favour good-looking photos**: with the [Better Organizing AI Add-on](../BetterTaggingAI)'s
  photo scores (Apple's aesthetics score, saved on each photo on a Mac), better-looking photos rank
  higher among the matches (up to 3 points either way, next to 2 per shared tag or gallery), and the
  fill-up photos come best-looking first. Without the scores, nothing changes.

## 1.3 (2026-11-16)

- **Similar galleries** under a gallery's pictures.

## 1.2 (2026-11-12)

- **Performer pages:** two more rows above Similar Performers: **Galleries** they're in (tagged on the
  gallery, or with their photos in it; most of their photos first) and **Studios** of their scenes,
  photos and galleries (most of their items first).

## 1.1 (2026-11-11)

- **Similar photos:** a tag that's just the photo's gallery's name (a comic's title) doesn't count as a
  shared tag - the shared gallery already does.
- **With the [Better Tagging AI Add-on](../BetterTaggingAI)** (all optional; without it, or if it fails,
  everything works as before):
  - **Similar photos** and **Suggested** scenes also include the photos / scenes that look most alike
    (its picture fingerprints), worth up to 8 points - so a photo with no tags, performers or gallery
    gets look-alikes instead of random photos. A brand-new item gets its fingerprint on the spot.
  - Their "look-alike performer" points use how alike faces look, when that's closer than the
    profile details.
- **Similar Performers** with the [Better Tagging AI Add-on](../BetterTaggingAI): how alike their faces
  look counts too, and the closest look-alikes join the row even with nothing else in common. Each performer's matches are judged against their own spread (only the top
  5% count), as how alike faces score differs a lot between performers. Without the add-on nothing
  changes.
- **Similar Performers:** each shared tag now counts 1 point instead of 2, so looks and details weigh
  more against tags.

## 1.0 (2026-11-09)

- First release: the Suggested, Similar photos, More with… and Similar Performers rows, moved out
  of Modern UI so they work with any theme.
