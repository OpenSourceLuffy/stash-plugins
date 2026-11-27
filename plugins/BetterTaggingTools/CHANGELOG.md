# Better Organizing Tools changelog

Newest first. Versions are what Stash shows under Settings → Plugins. Better Organizing Tools and the
[Better Organizing AI Add-on](../BetterTaggingAI) share their code (`btt_core.py`), so some versions
only bring the other plugin's changes.

## 2.4.3 (2026-11-25)

- The update window (shown when one of your OpenSourceLuffy plugins has a new version) follows the
  theme: it's now Stash's own dialog, so its colours come from the theme or UI plugin you use.
- **Auto tag from descriptions** (new settings): **Auto tag this / Auto tag selected** can also add
  performers named in an item's description (**Auto tag: performers named in descriptions**), and
  optionally tags too (**Auto tag: tags named in descriptions too**). Matched the same way as file
  names; the studio still comes only from the file path. Tags are a separate setting because
  descriptions are sentences: everyday words that are also tag names match very often.
- Shared code (`btt_core.py`) brought up to date with Better Organizing AI 1.8.3 (watermark
  reading).
- **Naming: account studios with their platform** (new setting): an account's studio is its name
  under the platform's studio (OnlyFans > name), or with the platform after it (OnlyFans >
  name (OnlyFans)) - as FansDB names them. Studios made from watermarks, links and the AI add-on
  follow it; both styles are matched either way. New tasks **Naming: rename account studios to the
  chosen style** (and a preview) rename the ones you have.
- Writes to Stash as UTF-8 whatever the system's encoding, and sends its answer before Python closes
  (if it can't, the log says why instead of "exit status 120") - shared code with Better Organizing
  AI 1.8.3.

## 2.4.2 (2026-11-24)

- Shared code (`btt_core.py`) brought up to date with Better Organizing AI 1.8.2; nothing changes in
  this plugin.

## 2.4.1 (2026-11-24)

- **Windows**: when Stash is installed directly on Windows and runs the Microsoft Store's stand-in for
  Python (tasks fail with "exit status 9009"), the plugin's pages now say what that means and how to
  fix it: Stash's **Settings → System → Python executable path**.
- **Update check**: when a newer version of any of these plugins is out, Stash shows what's new
  (from the changelogs) with **Yes, install** (for plugins installed through the plugin source; ones
  copied in by hand are told where to update), **Later** (a day) and **Don't show again**. It looks
  twice a day at most, reading the published plugin list and changelogs from GitHub - nothing is
  sent. Shared by Better Organizing Tools / AI, Modern UI, Privacy Blur and Stash Suggestions: one
  check covers all seven plugins.

## 2.4 (2026-11-24)

- **Naming conventions** for performers, studios, galleries and scene / image titles, like tags'
  capitalisation: a style for each (**Naming: ...** settings, in a new Naming section) - Title Case,
  Capitalise Every Word or Sentence case, or off (the default). Spaces are tidied, capitals are never
  lowered (except small words in the middle of a Title Case title: "Relaxing By The Pool" → "Relaxing
  by the Pool"), and Special spellings win, along with built-in ones (POV, JOI, WIP, BF, OnlyFans,
  ManyVids, TikTok...). Performers keep name particles as written (Lena de Vries) and get Mary-Jane,
  O'Neil; account names (studios under OnlyFans, Fansly...; handles with _, @ or a dot) are left alone,
  as are small words in romanised Japanese titles and text that was garbled before.
- New tasks: **Naming: apply to performers, studios, galleries and titles** and its preview. With
  **Naming: apply to new performers, studios and galleries** on, new ones get their style at once.
- Titles the plugins set (from file names, and the AI add-on's) follow the titles' style.
- **Tasks and settings renamed consistently** (like the AI add-on's): each starts with its group or
  section - Top tags, Titles, Dates, Studios, Tag pictures, Naming, Tag descriptions, Gallery images;
  New items, Titles, Tags, Naming - with previews last in each group ("Preview top tags (no
  changes)" is now "Top tags: preview (no changes)"). "Special spellings" is in the Naming section (it
  applies to tags too). Settings keep their values, and nightly / after-scan lists saved with the old
  task names still find them.

## 2.3.3 (2026-11-23)

- Shared code updated for the AI add-on's after-scan jobs.
- Windows: names with accents or in other alphabets come out right in the log and from the page
  (UTF-8 pipes, not the console's code page); no console windows flash up for ffmpeg; record files
  are saved with a retry when another task is reading them; the night timer check uses exact types.
- Fixed: the plugins' file locks didn't work with Stash in Docker Desktop on a Mac (the plugins
  folder is shared into the container, where locks aren't kept), so records updated at the same
  moment - auto tags, queues, picture records - could overwrite each other. Locks are now kept in
  the computer's own temporary folder.
- **Tidier settings and tasks**: the settings are in sections (Top tags, New items, Titles, Tags)
  instead of Stash's alphabetical order; tasks are in 6 groups, main task first and previews after;
  descriptions are a sentence or two (about half as long).
- Fixed: the description of "Don't title images after their gallery" was cut off at "Beach Trip".

## 2.3.2 (2026-11-21)

- Shared code updated for the AI add-on's nightly jobs.
- With Stash running natively (not in Docker), the face helper address defaults to this computer
  (`127.0.0.1`) instead of `host.docker.internal`.

## 2.3.1 (2026-11-20)

- Fixed: on Windows (Stash running natively) every task and button stopped with
  `ModuleNotFoundError: No module named 'fcntl'`. File locks now use `msvcrt` on Windows and `fcntl`
  elsewhere, as before. Thanks to the user who reported it with a fix.
- Fixed: checking whether the night timer runs no longer uses `os.kill(pid, 0)`, which on Windows ends
  the process instead of checking it, and works without `/proc` (Stash running natively on a Mac).
- Messages that ask you to do something first (e.g. "run Look-alike tags: scan scenes and images
  first") are logged as a short info line instead of an error with a traceback.

## 2.3 (2026-11-11)

- Tag pictures skip blurry, dark and screenshot-like candidates when the AI add-on's face helper runs
  on a Mac (Apple's aesthetics score), using the best of the next few if none scores well.
- Tag pictures prefer photos: a decent photo is used before any scene screenshot, marker or video frame.
- No suggestion rows in Stash's scrape dialog.
- Renamed **Better Organizing Tools** (it was Better Tagging Tools), to match the AI add-on. Its ID and folder are unchanged, so settings and saved data carry over.
- **Patreon** accounts count for studios from accounts: `patreon.com/name`, `patreon.com/c/name` or
  `Patreon: name` in a file name, title, details, URL or watermark, and `patreon/name` in a
  watermark. A new account goes under a **Patreon** parent studio.
- With Better Tagging AI installed, body tags (and their size tags) are left out of the suggestion
  rows, both as suggestions and as tags for **Goes with** to go by: the AI add-on's detector adds
  them from what's shown.
- **Tags to ignore** are no longer suggested while editing, and aren't used for **Goes with**.
- Suggestion rows: a row with only tags the item already has no longer stops the next kind of row
  (e.g. **Most used image tags**) from being shown, which could leave no suggestions at all.
- **Suggested galleries** under the Galleries box when editing an image: galleries made in Stash
  that the photos in the same folder (and the folder around it) and the same performers' photos are
  in, or named like one of its tags. Tap to add. With Better Tagging AI, an AI row adds galleries
  its look-alikes are in.
- **Gallery-name tags:** a tag counts as just a gallery's name (left out of top tags, suggestions,
  look-alike learning, typical pictures...) when it's named after a gallery and (almost) only on that
  gallery's photos - like a comic's title - whoever made it. Category folders whose tag is also used
  elsewhere ("Outdoor", "Cosplay", "Selfie"...) are real tags and count again; before, any gallery-name tag the
  plugin had made was left out, including those.
- **Tags to ignore** (Settings → Plugins) suggests big tags that look like housekeeping - by their name
  ("Categories", "Site_Process", "To Review"...) or because they group other tags, backed up (with
  Better Tagging AI) by their items not looking alike. Tap one to add it.
- **Fill in empty tag descriptions** (and a preview on 30 tags): from the stash-box a tag is linked to
  (StashDB...), else StashDB by exact name or alias, else the E-Hentai wiki for comic and doujinshi
  tags, else Danbooru's wiki (general tags only). Only empty descriptions are filled; names, aliases
  and your own descriptions are never changed.
- **Top tags respect gender:** a performer marked Female no longer gets tags about men's bodies or men
  (male body parts, "(Male)" tags...) just because her scenes have them, and one marked Male no
  longer gets female body tags and the like. Tags naming both (an age-gap pairing) and action
  tags are fine for anyone; trans, non-binary and
  unknown-gender performers are left as they are. Re-run **Tag performers with their top tags** to take
  such tags the plugin added off; tags you added yourself stay.
- Settings → Tasks: the studio-tags tasks are in the **Top tags** section.
- **Goes with** suggests more: instead of only learning from items that have *every* tag already
  chosen (few, once an item has several tags), it looks at each chosen tag's usual companions and
  ranks tags that go with several of them first. Hover over one to see why.
- **A studio's tags on its items:** a **Tag all items with this studio's tags…** button on studio
  pages (shows each tag and how many scenes, images and galleries would get it; untick any you
  don't want) and a task for every studio, with a preview task. Only adds; only the studio's own
  items, not its sub-studios'.
- Shared code for the AI add-on's **most typical tag pictures** and **Body tags: check everything
  again** (Better Tagging AI 1.5).
- **A tag picture is never used for two tags.** If every picture a tag has is already another tag's,
  it's left without one (or keeps its old one) instead of sharing. The "replace every tag's picture"
  tasks also keep each tag's current picture its own until that tag has moved to a new one.
- With the AI add-on's face helper running, pictures that **look** the same count as the same too
  (variants of one comic page, near-identical shots), and **Give tags that share a picture their own**
  finds those as well.
- Tags that are only on a gallery can now get a picture from the pages inside it, not just its
  cover (so a comic's tags no longer all get the same cover).

## 2.2 (2026-11-11)

- **A gallery's tags, performers and studio on its images:** a task for every gallery, and menu
  entries for one gallery (its page) or several (selected in the Galleries list). Only adds; an
  image's own studio is kept; galleries tagged as sexual content involving minors are skipped.
- Gallery-name tags and image titles made from gallery names leave out the gallery's studio
  (`[Studio] Beach Day` → `Beach Day`), unless the name is only the studio.

## 2.1 (2026-11-11)

- Images whose file name makes no title get one from their gallery: `Beach Trip #12`. Setting:
  **Don't title images after their gallery**. **Redo titles** leaves these alone.

## 2.0.1 – 2.0.4 (2026-11-11)

- 2.0.1: the OnlyFans / Fansly / Instagram / TikTok parent studios are set to **Ignore Auto Tag**,
  so Stash's Auto Tag and Identify don't put every file with "OnlyFans" in its name under them.
- 2.0.2 – 2.0.4: shared code for the AI add-on (look-alike tags, body tags, night-time AI work);
  nothing new in this plugin.

## 2.0 (2026-11-10)

- The AI features (AI tags, AI titles, reading watermarks) moved to the new
  [Better Tagging AI Add-on](../BetterTaggingAI). This plugin no longer needs Ollama.
- **Studios from accounts:** a studio (and performer) for the OnlyFans / Fansly / Instagram /
  TikTok account in a scene's file name, title, details or links.
- **Titles from file names**, with **Keep performer and studio names in titles** and **Redo
  titles**; **Title ideas** under the Title box.
- Top tags for **studios** and **galleries** too, with settings to refresh them on edit.
- Suggested tags: a performer's common tags on untagged items, and studio and whole-library
  fallbacks.
- Tasks grouped into sections you can open and close.

## 1.4 (2026-11-10)

- **Goes with** suggestions: the tags most often found together with the ones already chosen.

## 1.3 (2026-11-10)

- **Dates from file names** (task and setting for new items).

## 1.2 (2026-11-10)

- **Auto tag new performers, studios and tags**, **Auto tag this** on a page and **Auto tag
  selected** in lists, and auto tag before AI tagging.

## 1.1 (2026-11-09)

- AI tagging with a local model (moved to the AI add-on in 2.0).

## 1.0 (2026-11-09)

- First release: performers' top tags, suggested tags while editing, bulk tagging on performer and
  gallery pages, gallery-name tags, tag pictures from your library, tag name capitalisation.
