# Better Organizing AI Add-on changelog

Newest first. Versions are what Stash shows under Settings → Plugins. The add-on shares its code
(`btt_core.py`) with [Better Organizing Tools](../BetterTaggingTools), so some versions only bring
that plugin's changes.

## 1.5 (2026-11-11)

- Renamed **Better Organizing AI Add-on (local AI)** (it was Better Tagging AI Add-on): it does a lot more than tagging now. Its ID and folder are unchanged, so settings and saved data carry over.
- Now called **Better Tagging AI Add-on (local AI)**, with a shorter README: features first, setup at the end, and how it differs from other Stash AI plugins.
- The face helper's small models (YuNet, SFace, NudeNet) now come with the plugin, with their
  licences, so setup doesn't depend on those downloads; NudeNet (body tags) is always set up.
  `setup.sh --from DIR` restores every model from a backup folder. `setup.sh` picks a Python that
  ONNX Runtime supports (3.10-3.14) instead of failing on a newer default `python3`.
- Review has a **Studios** tab with studios to merge (the same name written differently, or
  misread), to delete (junk, empty, or read in just 1 item) and to add. Adding covers items in a
  gallery or folder whose others mostly have a studio, and account names read in a watermark only
  once. Undo takes any of them back, merges and deletes included.
- Review's tabs are now **Faces**, **Tags** and **Studios**, each with **Suggested**, **Check** and
  **New** under it (Tags has no New). Check is split into performers (under Faces) and tags (under
  Tags); old links to the Check tab open Tags → Check.
- Review has a **Galleries** tab:
  - **Suggested**: photos in no gallery that belong in one (they look like its photos, or they're
    in its folder), and performers / studio most of a gallery's photos have.
  - **Check**: photos that look like none of the rest of their gallery.
  - **New**: folders of photos in no gallery, to make into one.
- Review's big view (tap any picture) has **✎ Edit this scene / photo**: change its title, tags,
  performers, studio, galleries and Organized by hand, whatever the suggestion was.
- Studios → New: **…or add to a studio you already have**, like on New performers: search your
  studios (the closest names listed first); the one you pick gets the items and the watermark's
  name as an alias.
- Review on a phone: the tabs get a line of their own under the title.
- Review's edit form: the **Organized** label is readable again (it was dark on the dark big view with
  Modern UI's light theme).
- Studios → New / Suggested: the **See all** view's button is **✓ Create** / **✓ Add N** and does it
  straight away (with only the ticked items), instead of **Done**.
- Review's Batch view on a phone: the pictures scroll in their own area above the buttons, so the
  buttons no longer cover the bottom row.
- Studios from watermarks makes far fewer bad studios: a name read off a picture that's about the
  same as a studio you have, or as another name waiting for a second sighting, counts as that one
  (misreads like KUSUJOINN for KUSUJINN); a new studio takes the spelling of a tag or gallery you
  already have by about that name; email / web addresses, leak and repost sites, and names under 4
  letters aren't taken as accounts.
- Settings **AI tagging: which items** and **AI titles: which items** (scenes, images or both): only
  those get the AI model - in the AI tagging tasks, for new items after a scan and in the nightly run.
- Long tasks no longer stop working after about an hour: the login a task is given runs out, and
  Stash then sent its login page instead of pictures (the photo watermark task skipped everything
  after that and said "Done"). Both plugins now use Stash's API key from its settings when there is
  one.
- Studios from watermarks no longer adds performers just because a one-word name or alias of theirs
  ("Christian", "Summer", "Chanel") turned up in a photo's text (a magazine page, a caption): only
  names of two or more words count, and on photos only an account name does (a scene's name still
  needs two frames).
- Studios from watermarks: a bare **@name** watermark (no platform next to it) counts too - many photo
  watermarks are just that. A new one still needs to be read in a second photo or scene; it then
  becomes a studio of its own (under no platform). The photo task writes its progress to the log every
  50 photos.
- Review's full-size view: swiping past the first or last picture closes it, like swiping down.
- Review (and new items after a scan) no longer fail when a scan remembered a scene or photo that has
  since been deleted: it's left out instead of the whole group not loading.
- **Review** page (in Stash's menu; on a phone under More), swiping through suggestions one at a time -
  right / **Yes** adds it, left / **No** never shows it again, **Skip**, **Undo** - in two tabs:
  **Faces** (strong face matches across the library; a performer's "Might also be in" row has
  **Review one by one** for just them) and **Tags** (confident look-alike tags, 50%+, without body
  tags and tags to ignore). **Swipe | List | Batch** switches between one at a time, a grid of
  all of them with their own buttons, and one performer's / tag's suggestions at a time to untick and
  add in one go. A **Check** tab goes the other way: tags on items that their look-alikes say
  don't fit, and performers whose face isn't found in the item - remove them or keep them (Swipe, List
  or Batch, where everything starts kept and you pick what to remove). When you
  reach the end: **↻ Check for new suggestions** or **Show the skipped ones
  again**. A **New** tab suggests performers to create for faces seen in 3+ items
  that match no one (the AI reads their pictures' text for account names to suggest), only ever
  changing items without a performer (or "Is this [them]?" when a
  performer whose face isn't known yet is already on a quarter of them), with name
  ideas from accounts, studios, name-like tags and folders, and always a "Create a new performer?"
  check first that shows the card it will make (or add a performer you already have instead) and takes gender, tags and other details, and **See all** to look through every item and leave some out; the
  face helper gets a `/cluster` endpoint for it. Tap the performer's or tag's picture to see their page (a ✕ in the top
  right, swipe down or Back returns to the same card); tap the big picture to see it full screen with
  its tags, performers, studio and galleries; a scene plays from where the face was seen, with a ▶
  button for each time they're seen. The face helper gets `/match` and `/knn` endpoints for it (replace
  `face_helper.py` and restart it).
- **Patreon** accounts count for studios from accounts: `patreon.com/name`, `patreon.com/c/name` or
  `Patreon: name` in a file name, title, details, URL or watermark, and `patreon/name` in a
  watermark. A new account goes under a **Patreon** parent studio.
- **Studios from watermarks** for photos: tasks for photos without a studio (not in a gallery, or galleries too)
  and a preview on 10; plus the setting **AI: read watermarks on new photos** for new ones after a scan (at night
  with "wait until night"). Same rules as scenes: an existing studio or performer whose name is read
  is used straight away; a new account gets a studio once it's read in a second photo or scene.
- **Tags from look-alikes** leaves out body tags and their size tags: the body-part detector adds
  those from what's actually shown.
- **AI titles** (and AI tags) work on short clips such as phone Live Photos (2-3 seconds): before,
  clips of 2 seconds or less were skipped, and clips a little shorter than they say lost all their
  frames when one near the end couldn't be taken. Titles where the model ran on past the title
  (`...", 4 words, <start_of_image>`) are cleaned up or dropped.
- **Body tags** no longer finish quietly with nothing found when items can't be checked: a dropped
  connection to the face helper is retried (and stops the task with a message if it stays down),
  the first few items that fail are shown as warnings, and the summary says how many couldn't be
  checked.
- **AI** row under the Galleries box when editing an image: the galleries made in Stash that its
  20 closest look-alikes are in, with their share of the vote (from 20%). Tap to add.
- Tags that are just a gallery's name (a comic's title...) don't count towards **AI: skip items with
  more tags than**, and aren't learned by look-alikes (Better Tagging Tools 2.3).
- **Tags from look-alikes** (edit page) shows tags from 20% instead of 30% (setting: **AI: look-alike
  suggestions from (%)**), so tags that are close - often the ones you'd expect - appear. The tasks
  still only add tags at 50% or more.
- Tags the add-on added that are still on **organized** items count as yours and are learned from
  (before, its own tags were never learned from, even ones you'd kept).
- **Ask AI for tags** (edit page) also suggests tags you don't have yet, in the model's own words
  ("Not tags yet: + Black Corset (new) ..."); tapping one makes the tag and adds it. Ones you already
  have (by name or alias, ignoring hyphens: Close-up = Close Up) are left out, and they're never in
  **Add all**. The tasks never make new tags.
- **For Stash Suggestions 1.1:** its rows count how alike performers' faces look, and Similar photos /
  Suggested also include the photos and scenes that look most alike (from the look-alike
  fingerprints; a new item gets one on the spot).
- Settings → Tasks: every task is in a section again (new: Tonight's run, Look-alike tags, Tag
  pictures, Body tags).
- **AI: body tags every night** (setting): the body tags task runs at the night time for everything
  not checked yet, whether or not the AI tags new items.
- **Body tags skip items that already have one** (a body part, a size,
  natural / enhanced, a build), without looking at their frames. **Check everything again** still checks them.
- **AI tagging skips well-tagged items:** scenes and images with more than 5 tags (counting ones just
  added from the file name) don't go through the AI model; body tags still run. Setting: **AI: skip
  items with more tags than** (0 never skips).
- **Faster AI tagging and titles for scenes:** the model is shown one picture of 4 frames in a 2×2
  grid instead of 3 separate frames. Tagging a scene took about half as long on a test of your
  library (11 s → 6 s), making the nightly run about 40% faster.
- New items: reading watermarks (studios from accounts) is skipped for scenes that already have a
  studio and performers - nothing left for it to find.
- **Body tags: check everything again:** a task that goes over every scene and image once more,
  organized ones and ones done before included. Cancel and rerun to carry on.

- **Most typical tag pictures:** each tag can get the scene or photo that looks most like everything
  else with that tag (from the look-alike fingerprints, with the library's average look taken away).
  Three tasks (preview, tags without a picture, every tag) and **Most typical picture (AI)** in the
  Tags list's ••• menu for the selected tags. A picture is never used for two tags. Items with fewer
  other tags are preferred, as they're more likely to be about that tag. Pictures that look the same
  (variants of one page, near-identical shots) count as the same picture.
- Shared code from Better Tagging Tools 2.3 (a studio's tags on its items).

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
