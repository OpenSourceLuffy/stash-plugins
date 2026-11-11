# Better Tagging AI Add-on changelog

Newest first. Versions are what Stash shows under Settings → Plugins. The add-on shares its code
(`btt_core.py`) with [Better Tagging Tools](../BetterTaggingTools), so some versions only bring
that plugin's changes.

## 1.4.2 (2026-11-11)

- Shared code from Better Tagging Tools 2.2 (a gallery's tags, performers and studio on its
  images; gallery names without the studio).
- README: full setup guide for Ollama and the face helper, and why each model is used.

## 1.4.1 (2026-11-11)

- Shared code from Better Tagging Tools 2.1 (image titles from gallery names).

## 1.4 (2026-11-11)

- **AI: wait until night for new items:** after a scan, new items get only the quick steps (file
  names, links, look-alike tags, body tags, faces); the slow AI model steps run at **AI: night
  time** in **AI: time zone**. Task: **AI: run tonight's new items now**.

## 1.3 (2026-11-11)

- **Body tags:** Tits / Ass / Pussy / Cock when shown (NudeNet body-part detector, optional
  download), and sizes from the performer's usual tags or cup size.

## 1.2.2 (2026-11-11)

- Bigger performer picture on face suggestions: hover with a mouse, or tap on a phone (then
  **+ Add** or **✕ Close**).

## 1.2.1 (2026-11-11)

- Look-alike tags load by themselves when you open **Edit**.

## 1.2 (2026-11-11)

- **Look-alike tags:** learns from what you've tagged (CLIP picture fingerprints). AI tags are
  only kept when look-alikes back them up. Tags you keep removing aren't added any more.

## 1.1 (2026-11-11)

- **Studios from accounts:** a watermark read slightly wrong (`f1nnster` for *f1nn5ter*) is matched
  to the studio or performer you have, instead of making a new studio; a new name read only in
  watermarks needs a second scene before it gets a studio.

## 1.0 (2026-11-10)

- First release, split from Better Tagging Tools: AI tags, AI titles, studios and performers from
  accounts and watermarks, **Faces** (find performers by face, "Might also be in", face scan,
  learning from tagged items, possible duplicates), performer hair and eye colour.
- Huge images no longer stop the face tasks; the face helper is retried after brief network drops.
