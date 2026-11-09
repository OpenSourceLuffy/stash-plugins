# Better Tagging Tools

Tags each performer with the tags used most often on their scenes, images and galleries, then
uses those tags to suggest tags while you edit. It also adds bulk tagging to performer and
gallery pages, can tag a gallery's photos with the gallery's name, can give tags without
a picture one from your library, and keeps tag names consistently capitalised.

![Suggested tags under the Tags box](../../docs/screenshots/Tag_Suggestions.png)

*Editing a photo: one click adds a suggested tag. (Shown with example names and tags.)*

## Suggested tags

Under the **Tags** box when you edit a scene, image or gallery:

| The item has… | You see |
| --- | --- |
| Performers | **[Name]'s top tags**: the tags on the performers you've picked |
| A gallery | **Common in [gallery]**: the tags used most on that gallery's photos |
| Neither | **Most used image tags** (or scene tags) across your library |

- Click a tag to add it, or **Add all**. They go into the form like any other tag, so you still press **Save**.
- The rows update as soon as you change the performers or galleries in the form.
- Tags already on the item aren't suggested.

## Performers' top tags

The **Tag performers with their top tags** task counts the tags on every performer's scenes,
images and galleries and adds their most used ones (10 by default) to the performer.

- After that, whenever you save a change to the tags or performers of a scene, image or gallery,
  the performers in it are recounted. Batch edits are handled once at the end, in a background job.
- Tags the plugin added are taken off again when they drop out of a performer's top list.
  **Tags you added yourself are never removed.**
- **Preview performer top tags** writes what would change to the log without changing anything.

## Bulk tagging

<img src="../../docs/screenshots/Bulk_Tag.png" width="420" alt="The bulk tag window">

- **Performer pages:** a **Bulk tag scenes, images & galleries…** button adds (or removes) tags on everything that performer is in.
- **Gallery pages:** the tags button in the image toolbar (also in its **…** menu) tags every photo in the gallery, just the ones you've selected, and/or the gallery itself.
- The performer's top tags are suggested in the window, and you can create new tags there.
- It runs as a background job, so you can follow it under **Settings → Tasks**.

## Gallery-name tags

Turn on **Tag gallery images with the gallery's name** and every photo in a gallery gets a tag
named after it (its title, or the zip or folder name). New galleries, renamed ones, scanned
photos and photos you move into a gallery are kept up to date. Run the **Tag gallery images with
the gallery's name** task once for the galleries you already have.

- An existing tag with the same name is reused.
- Renaming a gallery swaps its photos over to the new name's tag.
- These tags never count towards performers' top tags or "most used" suggestions.
- Photos added from a gallery's **Add** tab don't trigger anything in Stash, so re-run the task to pick those up.

## Tag pictures

The **Give tags without a picture one from your library** task gives every tag without a picture
the photo that's most "about" that tag (the one with the fewest other tags, then the highest
rated). If there's no photo, it uses a scene screenshot, then a marker or gallery cover. Turn
on **Give tags a picture automatically** to do the same whenever a tag without a picture gets used.
Tags that already have a picture are never changed.

Every tag gets a picture of its own where possible. Related tags often share their best photo, so
the plugin compares each candidate with the pictures other tags already have and moves on to the
next one if it's taken. The rarest tags pick first, since they have the fewest pictures to choose
from. When a tag only appears on scenes that lots of other tags share, it takes a different frame
from one of those videos instead (using Stash's own ffmpeg).

### Don't like a tag's picture?

On the **Tags** page, select one or more tags and choose **••• → Pick a new picture**. Each one
gets its next-best picture by the same rules (never one another tag has), and the cards update
straight away. Press it again for another; it remembers a tag's recent pictures, and cycles back
through them once there's nothing new left.

### From a scene

On a scene's page, **⋮ → Use a frame as a tag's picture…** (next to Stash's own thumbnail
options) lets you pick any moment of the video as a tag's picture:

<img src="../../docs/screenshots/Frame_Tag_Picture.png" width="420" alt="Choosing a frame as a tag's picture">

1. It opens at the player's current position. Type a time instead (`1:23`, `1:02:03` or `83.5`), or scrub the video and press **Use current position**.
2. Choose the tag: one of this scene's tags with one click, or any tag from the picker.
3. Compare the tag's current picture with the new frame, then press **Set**.

The frame is taken from the video file at full quality (using Stash's own ffmpeg), so typing a
time doesn't move the video you're watching.

### Pictures for tags that share one

Already have tags sharing a picture? **Give tags that share a picture their own** fixes them: in
each group the rarest tag keeps the picture and the others get a new one. The first run compares
every tag's picture once, which can take a few minutes.

### A fresh picture for every tag

**Give every tag a new picture (replaces existing ones)** gives *every* tag a different picture
from the one it has now, picked the same way (unique, rarest tags first). This includes pictures
you set yourself, and the old ones can't be brought back, so make a backup first
(**Settings → Tasks → Backup**). A tag with no other picture available keeps the one it has.

## Tag names

Keeps tag names capitalised the same way: `dating app` becomes `Dating App`, while small words
stay lowercase in the middle of a name (`behind the scenes` → `Behind the Scenes`).

- **Preview tag name capitalisation**: lists the renames in the log without changing anything. Run this first.
- **Capitalise tag names**: renames every tag that needs it.
- Turn on **Capitalise new tags automatically** to capitalise tags as soon as they're created (from the tag picker, a scraper, another plugin…).

| Before | After | Why |
| --- | --- | --- |
| `soft spoken` | `Soft Spoken` | Each word gets a capital. |
| `in the shower` | `In the Shower` | `a, an, and, as, at, but, by, for, from, in, into, nor, of, on, onto, or, per, the, to, via, vs, with` stay lowercase in the middle. |
| `T-shirt` | `T-shirt` | Hyphenated words count as one word. |
| `POV`, `McKenzie` | unchanged | Capitals already there are never lowered. |
| `3rd person` | `3rd Person` | Words starting with a number are left alone. |
| `cnn` | `CNN` | With `CNN` in **Special spellings**. |

Stash treats tag names that differ only in case as the same tag, so renaming never creates duplicates.

## Settings

Under **Settings → Plugins → Better Tagging Tools**:

| Setting | What it does |
| --- | --- |
| Top tags per performer | How many tags to add to each performer. Default 10. |
| Minimum uses | A tag must be on at least this many of the performer's items. Default 3. |
| Don't count images | Count only scenes and galleries. |
| Tags to ignore | Comma-separated tags never added to performers, e.g. `Organized, To Review`. |
| Don't refresh on edit | Only update performers when you run the task. |
| Tag gallery images with the gallery's name | See above. |
| Parent tag for gallery-name tags | Optional; files the gallery-name tags under one tag, e.g. `Galleries`. |
| Give tags a picture automatically | See above. |
| Capitalise new tags automatically | See "Tag names" above. |
| Special spellings | Comma-separated words or tag names to always write exactly as given when capitalising, e.g. `CNN, POV, HD, iPhone`. |
| Suggested tags to show | The most suggestions per row. Default 15. |

## Requirements and notes

- Python 3 (standard library only). Tested with Stash v0.31.
- ffmpeg, only for video frames used as tag pictures. It uses the one Stash uses; the official Docker image includes it.
- The plugin keeps a few small files in its own folder (which tags it added, a short work queue). Deleting them is safe; it just forgets which performer tags it added.
- Works with any theme.
