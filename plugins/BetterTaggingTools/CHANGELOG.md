# Better Tagging Tools changelog

Newest first. Versions are what Stash shows under Settings → Plugins. Better Tagging Tools and the
[Better Tagging AI Add-on](../BetterTaggingAI) share their code (`btt_core.py`), so some versions
only bring the other plugin's changes.

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
