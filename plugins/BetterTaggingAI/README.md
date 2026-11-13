# Better Tagging AI Add-on

An add-on for [Better Tagging Tools](../BetterTaggingTools) that does the work a local AI model is
good at: tagging scenes and images, writing titles, finding a scene's studio and performers from
the accounts and studio watermarks it shows, recognising your performers' faces, learning tags
from the scenes and images you've already tagged, and filling in performers' hair and eye colour. The model runs on your own computer through
[Ollama](https://ollama.com); nothing is sent to the internet.

Install it next to Better Tagging Tools. It then uses that plugin's **Tags to ignore**, **Special
spellings** and gallery-name tags, and its rows sit under Better Tagging Tools' suggestions. It also
works on its own.

## Setup

You need two things on the computer Stash runs on (or one it can reach): **Ollama** with the
**Gemma 3 4B** model, for everything that says "AI"; and the small **face helper**, for faces,
look-alike tags and body tags. Each part is optional: tasks that need a missing part say so in the
log and do nothing.

### 1. Ollama and the AI model

1. Install [Ollama](https://ollama.com):
   - **Mac:** `brew install ollama`, then `brew services start ollama` (it then starts at login).
     Or download the app from ollama.com.
   - **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`
   - **Windows:** the installer from ollama.com.
2. Download the model: `ollama pull gemma3:4b` (about 3.3 GB;
   [ollama.com/library/gemma3](https://ollama.com/library/gemma3)). It isn't kept in this repo.
3. Check it works: `curl http://localhost:11434/api/tags` should list `gemma3:4b`.
4. Tell the plugin where Ollama is (**AI: Ollama address**):
   - **Stash in Docker Desktop** (Mac or Windows) on the same computer: the default,
     `http://host.docker.internal:11434`, works as is.
   - **Stash installed directly** (not in Docker): `http://localhost:11434`.
   - **Stash in Docker on Linux:** containers can't reach programs that only listen on the
     computer itself (Ollama and the face helper both do, for safety). Easiest is to run the Stash
     container with `network_mode: host` and use `http://127.0.0.1:11434`.
5. In Stash, run **AI tagging: preview on 10 items (no changes)** under Settings → Tasks. The log
   shows the tags it would add; an error there says what's missing.

### 2. The face helper (faces, look-alike tags, body tags)

A tiny program that runs next to Stash and turns pictures into "fingerprints". From this plugin's
folder (Stash's `plugins/BetterTaggingAI`), run:

```sh
sh face-helper/setup.sh                      # faces, look-alike tags and body tags
sh face-helper/setup.sh --from /backup/dir   # the same, every model from a backup folder
```

- **Models:** the small ones (YuNet, SFace, NudeNet) come with the plugin in
  [`face-helper/models`](face-helper/models), each with its licence; the CLIP model (350 MB, too
  big for GitHub) is downloaded. Each file is checked against `models/SHA256SUMS`. `--from` takes
  them all, CLIP included, from a folder of your own (e.g. a copy on a NAS) and downloads nothing.
- **Needs:** Python 3.10-3.14 (ONNX Runtime doesn't support newer ones yet; the script picks one
  you have, or set `PYTHON=python3.13`) and `curl`. Mac and Linux; on Windows it's untested (WSL
  may work).
- **Installs:** a Python environment with OpenCV, NumPy and ONNX Runtime, and the models (about
  650 MB in all) in `~/Library/Application Support/face-helper` (Mac) or
  `~/.local/share/face-helper` (Linux; set `FACE_HELPER_DIR` to choose another folder).
- **Runs:** on a Mac it's set up to start at login and restart if it stops. On Linux it prints the
  command to start it (put it in a systemd user service to keep it running).
- **Check it:** `curl http://127.0.0.1:11535/health` should answer
  `{"ok": true, "embed": true, "body": true}`.
- It only listens on your own computer (port 11535; change it with `FACE_HELPER_PORT`), keeps
  nothing, and sends nothing anywhere. The plugin keeps the fingerprints in its own folder.
- If you installed it before look-alike tags or body tags existed, run it again; it updates in
  place.
- On a Mac, keep the helper out of `Documents`, `Desktop` and `Downloads`: programs started at login
  aren't allowed to read those folders. (The default folder is fine.)
- To remove it on a Mac: `launchctl bootout gui/$(id -u)/face-helper`, then delete
  `~/Library/LaunchAgents/face-helper.plist` and the `face-helper` folder. To remove the AI model:
  `ollama rm gemma3:4b`.

### 3. Then

- Run **Look-alike tags: scan scenes and images** and **Faces: scan scenes and images** once
  (each takes a while on a big library; cancel and rerun to carry on).
- Turn on the after-scan settings you want (see [New items after a scan](#new-items-after-a-scan)).

## Which models, and why

Everything runs on your own computer: these are personal libraries, so no picture is ever sent to
an online AI service. That means models small enough to run next to Stash on an ordinary computer,
and each job uses the smallest model that does it well.

| Job | Model | Size | Why this one |
| --- | --- | --- | --- |
| Tags, titles, reading watermarks, hair and eye colour | **Gemma 3 4B** (Google), through Ollama | 3.3 GB | The smallest model that can look at a picture and describe it well. About 5 seconds a picture on an Apple M4 (a scene about 15). |
| Finding faces | **YuNet** (OpenCV model zoo) | under 1 MB | Built only for finding faces; fast without a graphics card. |
| Telling faces apart | **SFace** (OpenCV model zoo) | 39 MB | Turns a face into 128 numbers; the same person's faces get similar numbers, so your performers can be recognised. A language model can't recognise *your* performers. |
| Look-alike tags | **CLIP ViT-B/32** (OpenAI), as a picture-only ONNX file | 350 MB | Turns a whole picture into 512 numbers describing what's in it (setting, clothes, poses, style), so the plugin can find the items you've already tagged that look most alike. The smallest standard CLIP model; well under a second a picture. |
| Body tags | **NudeNet 320n** | 12 MB | A detector made for exactly this: which body parts are shown, and where. Fast and much more consistent than asking a general model. |

**Why not a bigger AI model?** Bigger models (e.g. `gemma3:12b` or `27b`) are more accurate but 3–7
times slower and need 8–20+ GB of memory. With thousands of items, that turns hours into days. The
plugin instead keeps the small model to what it does well and checks its work:

- **Tags:** on its own, the AI's tags matched the ones you'd have given only 8–20% of the time
  (tested on 64 tagged items). So it can only pick from your own tag list, and its picks are only
  kept when look-alikes back them up: 70–78% right. Look-alike tags alone (no AI) were 79–85%
  right for items like ones you've tagged.
- **Account names in watermarks:** asked for an account name directly, small models make up
  plausible ones (even for a video with no text in it). So the model is only asked to copy out the
  text it sees, the names are picked out of that text by the plugin, and a name needs two frames
  to agree.
- **No example answers in its instructions:** small models copy them (e.g. reporting the example
  tattoo for everyone), so the instructions never include any.

You can try another model with **AI: model** (any Ollama model that can see pictures, e.g.
`gemma3:12b`); only `gemma3:4b` has been tested.

**Licences:** Gemma 3 is under Google's [Gemma terms](https://ai.google.dev/gemma/terms) (not
included here: `ollama pull gemma3:4b`). The models in `face-helper/models` keep their own
licences, in [`face-helper/models/licenses`](face-helper/models/licenses): YuNet (MIT) and SFace
(Apache 2.0) from OpenCV's model zoo, and NudeNet's 320n detector (AGPL-3.0, from its Python
package, version 3.4.2). CLIP (MIT) is downloaded by `setup.sh`. See
[`face-helper/models/README.md`](face-helper/models/README.md) for where each came from.

## While editing

- **AI** row under the **Tags** box (scenes and images): **Ask AI for tags** shows the model's picks
  as one-click chips, checked against look-alikes (✓ = look-alikes back it up). **Tags from
  look-alikes** (quick, no AI model) shows the tags of the scenes and images you've tagged that look
  most like it, with how many of them have it, e.g. `+ Outdoors 72% ✓`; these load by themselves
  when you open **Edit** (once the look-alike scan has run). Body tags aren't among them (the body
  detector adds those). Nothing is added until you click them and save.
- **Faces** row under the **Performers** box (scenes and images): **Find performers by face** compares
  the faces in it (6 frames of a scene, or the image) with your performers' and shows the closest as
  chips with their picture and how alike they are, e.g. `+ Jane Doe 56% ✓`. ✓ marks a confident match;
  without it, check it's really them. With a mouse, hover over one to see a bigger picture and click
  to add; on a phone, tap one to see the bigger picture, then **+ Add** (or **✕ Close**). Nothing is
  saved until you save; **✕ Hide** puts the suggestions away again.
- **Ask AI for details** when editing a performer: suggests hair colour, eye colour, tattoos and
  piercings from their pictures. Click one to put it in its box (then **Save**).
- **AI titles** row under the **Title** box (scenes, images and galleries): **Ask AI for titles**
  shows a few titles, written after looking at the picture (or a few frames) and its performers,
  studio, tags and file name. Click one to put it in the box. Ask again for different ones.

- **AI** row under the **Galleries** box (images): the galleries that the 20 photos looking most
  like this one are in, closer ones counting more, e.g. `+ Holiday 64%` (the share of the
  look-alikes' vote). Only galleries made in Stash, and only from 20%. Quick, no AI model, once
  **Look-alike tags: scan scenes and images** has run. Works with or without Better Tagging Tools
  (which adds its own **Suggested** galleries from folders, performers and tags).
- **Ask AI for tags** also lists ideas for tags you don't have yet (**Not tags yet: + Black Corset
  (new)**), in the model's own words; tapping one makes that tag and adds it. Ideas matching one of
  your tags or aliases are left out, they're never in **Add all**, and the tasks never make new tags.

## Stash Suggestions rows

With [Stash Suggestions](../StashSuggestions) 1.1 or later, its rows use what this add-on knows:
**Similar Performers**, **Similar photos** and **Suggested** count how alike performers' faces look
(from **Faces: learn performers' faces**), and Similar photos / Suggested also include the photos
and scenes that look most alike (from **Look-alike tags: scan scenes and images**; a new item gets
its fingerprint the first time it's shown).

## Performer pages: "Might also be in"

A **Faces** row above the tabs on a performer's page: **Might also be in…** lists scanned scenes and
images that don't have them but where someone looks like them (and they're the closest match of
all your performers), with a thumbnail and how alike. Confident matches (✓) are ticked; tick the
right ones and press **Add them to … selected**. **✕ Not them** takes one off the list for good;
**✕ Close** puts the list away. **Review one by one** opens the Review page's Faces tab (below) for just them.

## Review

<table><tr><td><img src="../../docs/screenshots/Review_Swipe.png" width="260" alt="A Tags → Suggested card: Tag it Outdoors?"></td><td><img src="../../docs/screenshots/Review_Batch.png" width="260" alt="Tags → Check in Batch view, two pictures marked to remove"></td><td><img src="../../docs/screenshots/Review_Edit.png" width="260" alt="Editing a scene by hand from the big view"></td></tr></table>

*A suggestion to swipe, Batch view (tap the ones that don't fit), and editing a scene by hand from the big view.* *(Pictures blurred, and every name, title and tag replaced with an example.)*

**Review** in Stash's menu (on a phone with [Modern UI](../ModernUI): **More → Review**) shows
suggestions one at a time. The tabs are what's reviewed - **Faces**, **Tags**, **Studios** and **Galleries** - and
under each, what to do: **Suggested** (what's likely missing), **Check** (what's already there that
may be wrong) and **New** (performers or studios you don't have yet). Faces → Check is the Check list
below for performers, Tags → Check for tags; Faces → New is New below; Studios and Galleries have all
three (below).
Tapping Faces, Tags, Studios or Galleries goes back to the one you used last there. Swipe right (or **✓ Yes, add**) to add it, left
(**✕ Not them** / **✕ No**) to never suggest it again, **Skip** to leave it, **↶ Undo** to take the
last answer back. On a computer: → yes, ← no, ↓ skip, Z undo. **Swipe | List | Batch** switches to a grid
of all of them, each with its own **✕** and **✓ Add** (and **Show more** at the bottom), or to
**Batch**: one performer's / tag's suggestions at a time (biggest first, or pick one from the list),
all ticked - tap the ones that don't fit (⤢ to see one big and swipe through them, with **✓ Keep /
✕ Leave out** at the bottom), then **✓ Add to
N** (the unticked ones are marked no), **✕ None** or **Skip**; Undo takes a whole batch back. The view
you pick is remembered. At the end of a tab (or a list): **↻ Check for new suggestions** works it all
out again straight away (new scans, tags and performers, instead of the copy kept for 15 minutes),
and **Show the skipped ones again** brings back what you skipped. Tap the performer's (or tag's)
picture to open their page: a **✕** in the top right (or swiping down with Modern UI, or Back) takes
you to the same card, with your answers and Undo kept (for 30 minutes). Tap the big picture to see
it full screen with its tags, performers, studio, galleries and date (tap one to open it). Swipe down
anywhere while it's scrolled to the top to close it (or ✕, or Escape). **✎ Edit this scene / photo** there
changes it by hand, whatever the card was suggesting: title, tags, performers, studio, galleries
(✕ on one takes it off, the search box adds more) and Organized; **✓ Save** saves it and shows
what's on it now. A scene shows its video instead,
ready (not playing) at just before the moment the face matched, and swiping down closes it even
while it plays; the Faces card says when they're seen ("seen at 5:56,
50:25") and the big view has a ▶ button for each of those times.

- **Faces**: strong face matches across the library: the scene or photo, the performer's picture,
  "Is this … ?", how alike and how alike the next closest performer is. "Strong" is the same bar as
  **Faces: add confident matches automatically** (a high likeness, clearly ahead of every other
  performer, and in a scene the best match in two or more frames). Yes also learns that face as
  theirs. A performer's **Review one by one** button opens it for just them.
- **Tags**: confident look-alike tags - the ones the tasks would add, where 50% or more of the vote of
  its 20 closest look-alikes you've tagged goes to that tag. "Tag it … ?" with the tag's picture.
  Body tags (the body detector adds those), **Tags to ignore**, gallery-name tags and tags you've
  mostly removed are left out. Your "No"s are kept in `tag_review_not.json`.

- **Check**: what's already on an item that might not belong. **Does [tag] fit?** - the item has
  it, but under 5% of its 20 closest look-alikes do; only tags the look-alikes judge well (25+ tagged
  items, usually seen on their own items, and only a few odd ones out - a tag like "Famous" isn't
  something a picture shows, so it isn't checked). **Is [performer] in this?** - their face is known,
  faces were found in it, and none look like them (a face turned away or a poor profile picture can
  cause this, so check). **✕ Remove** takes it off (Undo puts it back); **✓ It fits** keeps it and
  never asks again. Swipe, List or **Batch** - one tag / performer at a time, where everything starts
  kept and you tap the ones that really don't fit (they turn red), then **✕ Remove from N** (the rest
  are remembered as fitting), **✓ All fit** or **Skip**; Undo puts a whole batch back.
- **New**: people seen in 3 or more scenes / photos whose face matches none of your performers (the
  face helper groups the faces; comics and other drawn items are left out, as drawn faces all look
  alike to it). Only items **without a performer** are ever changed: one that has someone most likely
  shows them. When a quarter or more of the group already have the same performer and that
  performer's face isn't known yet (no usable profile picture), it asks **Is this [them]?** - Yes adds
  them to the untagged ones and learns that face. (If their face *is* known and didn't match, it isn't
  them, so nothing is suggested.) Tap the pictures (or **See all**) to look through every scene and
  photo it would change: tap one to leave it out (tap again to put it back), ⤢ to see it big (swipe
  left / right, the ‹ › buttons or the arrow keys go through the others), then
  **Done** - Create / Yes only use the ones still ticked ("Using 28 of 30"). Otherwise, with 3 or
  more untagged items, **New performer?** with up to 6 of the pictures (tap one to see it big), a
  name box and up to 3 name ideas to tap, each saying where it's from: an account named in their
  file names, titles or links (`onlyfans.com/…`), a studio a quarter of them share, a tag on most of
  them that looks like a name and isn't on lots of other people, or a named folder. While a card is
  showing, the local AI model also reads the text in up to 6 of its pictures (watermarks, captions)
  for an OnlyFans / Fansly / Patreon / Instagram / TikTok account or a studio / performer name you
  have ("🔍 Reading the pictures' text…", about 25-35 seconds, once per suggestion); what it finds
  is added as 🔍 name ideas, with the account's link. **✓ Create** (or
  a swipe right) always asks first: **Create a new performer?** shows how their card will look
  (updating as you fill it in), with the name to check or type, gender, tags (the ones common on
  those items to tap, or find any other) and **More details** - disambiguation, aliases, links (an
  account's link comes with its name idea), country, ethnicity, hair and eye colour, birthdate and a
  description. **…or add to a performer you already have** instead searches your performers (the
  name ideas are looked up straight away, and **Looks most like** lists the 8 performers whose faces
  are closest, with how alike - possibilities, since no one matched well), and after a check adds the one you pick to those items
  and learns this face as theirs; Undo takes it off again. Then it makes the performer with the first picture as their profile picture and adds them to all of
  those items; Undo deletes it again. **✕ No** never suggests that group again. No list view
  on this tab.
- **Studios**: tidying up studios, mostly the ones **Studios from watermarks** made. **Suggested**
  is "Add these to [studio]?", **Check** is merge and delete, **New** is "New studio?".
  - **Merge these into one studio?** Studios whose names are the same written differently
    ("FansDB: Somename (onlyfans)" and "somename"), or look like misreadings of each other.
    Each one shows how many scenes and photos it has, with a few pictures. **Keep this** picks
    which one stays (the one with the most is picked first); **Leave out** keeps one out of the
    merge. The name box (or tapping one of the names) sets the name it ends up with. Merging moves
    everything to the one you keep, and the other names become its aliases. Their links and StashDB
    / FansDB ids move over too, so a watermark misread the same way again finds it. Studios on two
    different platforms, or both from StashDB, are never suggested.
  - **Delete [studio]?** Ones that look made from a watermark (no picture, details or StashDB link):
    a website, email or repost-site name, an account read in just 1 item, or a studio with nothing
    in it. Its items are only left without a studio.
  - **Add these to [studio]?** Scenes and photos without a studio in galleries or folders where most
    of the others have one studio (80% of the ones with a studio, and at least a quarter of all of
    them), grouped per studio. The gallery gets it too if it has no studio. It also asks when an
    account name read once in a watermark is close to a studio you have.
  - **New studio?** An account name read in a watermark that's waiting for a second sighting (the
    task only makes a studio by itself once it's read twice), with the pictures it was read in.
    Check the spelling in them and fix it in the name box. It's made under its platform's studio
    with the account's link. **…or add to a studio you already have** instead searches your studios,
    with the 6 whose names are most like it listed first ("56% alike"). The one you pick gets those
    items, and the name read in the watermark becomes one of its aliases, so the watermark finds it
    next time. Undo takes both back.
  - Tap the pictures (or **See all**) to leave some out - its **✓ Create** / **✓ Add N** button does it
    from there - or a studio's picture to open its page.
    Undo takes back a merge or delete too: the studio is made again (name, aliases, links, picture,
    details) and gets its scenes, photos and galleries back. **✕ No** never suggests it again (kept
    in `studio_review.json`). One at a time, with no list view.

<table><tr><td><img src="../../docs/screenshots/Review_Studio_New.png" width="260" alt="Studios → New: New studio? with the name read in a watermark"></td><td><img src="../../docs/screenshots/Review_Studio_Pick.png" width="260" alt="…or add to a studio you already have, with the closest names listed"></td></tr></table>

  *A name read in a watermark, and adding it to a studio you have instead.* *(Pictures blurred, and every name, title and tag replaced with an example.)*

- **Galleries**:
  - **Suggested** has two kinds of card. **Add these to [gallery]?** offers photos that are in no
    gallery and either look like the gallery's photos (60%+ of their 20 closest look-alikes are in
    it, and its own photos mostly look alike) or sit in the folder its photos are in. The folder
    case only applies to galleries you made by hand, when the folder's photos went to that gallery
    alone. **Add to the gallery [gallery]?** offers the performers on half or more of its photos (3
    at least) and the studio on 80% of them, when the gallery doesn't have them yet. Tap one to
    leave it out.
  - **Check** asks **Does it belong in [gallery]?** about photos whose look-alikes are almost never
    in their gallery. It only judges galleries of 8+ photos that mostly look alike, and only when
    those photos are a few odd ones out. **✕ Remove** takes the photo out of the gallery (it isn't
    deleted). Swipe, List or Batch, like the other Check tabs.
  - **New gallery?** offers a folder of 5+ photos that are in no gallery. The name box starts with
    the folder's name, and its studio and performers come along when most of the photos have them.
  - Look-alikes need **Look-alike tags: scan scenes and images** to have run. Undo takes anything
    back: added photos come out again, a new gallery is deleted (its photos are kept), and details
    go back to what they were. **✕ No** is kept in `gallery_review.json`.

<table><tr><td><img src="../../docs/screenshots/Review_Galleries_Check.png" width="260" alt="Galleries → Check: Does it belong in Outdoors?"></td><td><img src="../../docs/screenshots/Review_Galleries_New.png" width="260" alt="Galleries → New: New gallery? from a folder of loose photos"></td></tr></table>

  *A photo that doesn't look like the rest of its gallery, and a folder of loose photos that could be one.* *(Pictures blurred, and every name, title and tag replaced with an example.)*

Only items the face scan and look-alike scan have looked at count, and what's already on an item isn't
shown. Comparing every face with every performer, and every picture with every tagged one, is done by
the face helper in a second or two; a face helper from before this version needs its `face_helper.py`
replaced with the new one and a restart.
## Tasks

Under **Settings → Tasks → Plugin Tasks → Better Tagging AI Add-on**, in sections you can open and
close. Each section starts with a preview that only writes to the log.

**AI tagging**
- **Items with no tags** and **everything not done yet**. They only ever *add* tags, skip organized
  items (unless you change that), and remember what's done, so you can stop a run and start again.
- The model is shown a picture (for a scene, one picture of 4 of its frames in a 2×2 grid) and your most used tags (250 by default)
  and can only answer with tags from that list, so it never invents tags. Items that already have more than
  5 tags (setting **AI: skip items with more tags than**) skip this step; they get body tags only. Before that, each item
  gets the performers, studio and tags whose names are in its file path (the same matching as
  Stash's Auto Tag).

**Look-alike tags** (learning from what you've tagged)
- **Scan scenes and images** makes a picture fingerprint of every scene (its cover and 4 frames
  from its preview sprite) and image, once. Well under a second each; cancel and rerun to carry on.
- After that, an item's tags are guessed from the 20 most alike items you've tagged: a tag scores by
  how many of them have it, the closest counting most. It uses your own tags the way you use them,
  including ones the AI model has never heard of, and it keeps learning as you tag.
- **AI tagging** then keeps only the AI's picks that look-alikes back up, and adds the tags
  look-alikes are sure of (50% or more). On a test of 64 tagged items, the AI on its own matched the
  tags you'd given 8–20% of the time; checked against look-alikes, 70–78%.
- **Items with no tags** adds only the tags look-alikes are sure of (no AI model needed).
- **Corrections**: tags the add-on adds are remembered (`machine_tags.json`) and never learned from,
  so its guesses don't reinforce themselves - except on **organized** items: you've looked those
  over, so the add-on's tags still on them count as yours. A tag you've removed from most of the items it was
  added to (at least 4) isn't added any more; the tasks list such tags in the log.
- Accuracy depends on how consistently you tag: items in a folder or gallery you've already tagged
  get the best results; a completely new kind of picture gets fewer, less sure tags.

**Tag pictures** (the most typical one, from the look-alike fingerprints)
- A tag's "typical look" is worked out from the fingerprints of the scenes and photos that carry it
  (not ones where the add-on added that tag itself), with the library's overall average look taken
  away so it reflects what's special about the tag. The tag's photo or scene closest to that is its
  most typical picture. Photos and scenes are ranked apart, and the kind the tag is mostly on comes first.
- Items with fewer other tags are preferred, as they're more likely to be about that tag (a photo
  with 3 tags beats a scene with 50 that looks a little more typical). Gallery-name tags don't count.
- **Tag pictures: preview the most typical ones** writes each tag's pick to the log (with a link).
- **Tag pictures: most typical one for tags without a picture** sets them; tags on fewer than 3
  fingerprinted items get Better Tagging Tools' usual pick instead.
- **Tag pictures: most typical one for every tag** replaces existing pictures too (make a backup first).
- **Tags list → select tags → ••• → Most typical picture (AI)** does just those tags, and the cards
  update straight away.
- A picture is never used for two tags, so a tag can get its next most typical picture, or none if
  every one it has is already another tag's. Pictures that look the same (variants of one comic
  page, near-identical shots; 96% alike or more) count as the same picture. Tags used on only a few items share most of them with other tags, so their pick is
  limited to those few. Run **Look-alike tags: scan scenes and images** first so newer items count.

**Body tags** (what's shown, and how big)
- What's shown comes from a body-part detector (4 frames of a scene, or the image): **Tits**, **Ass**,
  **Pussy** and **Cock**, using your tags with those names (make a tag to have that part added). It's
  good at breasts and buttocks (in testing it found them in about 90% of items tagged so), weaker at
  genitals. When an item's performers are all men it never adds Tits or Pussy (it can take a man's
  chest for breasts), and Cock not when they're all women.
- How big comes from the performer, and only when that part is shown: the size you usually tag them
  with (on 2+ of their items, 70% of them: e.g. **Big Ass**), else for breasts their cup size (B and
  under **Small Tits**, D and up **Big Tits**; C is skipped), plus **Natural Tits** / **Fake Tits**
  from their details. **Defined Abs** / **Muscular** when a belly is shown and they're usually tagged
  so. An item that has a size tag already never gets another. In testing, sizes from the performer's
  own tags were right 79–100% of the time and cup sizes 83%.
- **Scenes and images** does every item once (cancel and rerun to carry on). **Check everything
  again** goes over every scene and image once more, organized ones and ones done before included
  (e.g. after making a new body tag); it also carries on where it stopped if cancelled.
- Items without performers get the parts that are shown, but no sizes (those come from the performer).
- Items that already have a body tag (a part, a size, natural / enhanced, a build) are
  skipped, by the task and during AI tagging; **Check everything again** still checks them. AI tagging, new items
  after a scan and **Ask AI for tags** add them too.

**AI titles**
- **Scenes without a title** / **images without a title**: each gets the title from its file name
  when that makes a good one, otherwise the AI's best idea. Titles you already have are never
  changed. (With Better Tagging Tools, AI titles aren't touched by its **Redo titles**.)

**Studios from accounts**
- **Also read watermarks with the AI**: for scenes **without a studio**. Account names are taken from
  links in the file name, title, details or URLs, and from the watermark and on-screen text the
  model reads in up to 6 frames. Patreon also counts as written in comic watermarks (`patreon/name`),
  but only in a watermark: in a file path, "…patreon/…" is just a folder. It only transcribes the text (asked for names directly, small models
  tend to make one up), and a name only counts if two frames agree, so one-off misreads are ignored.
- A name read in a watermark that's a near miss of a studio or performer you have (`f1nnster` or
  `flnn5ter` for *f1nn5ter*; letters that look alike, like 1/l/i, 0/o and 5/s, count as the same) is
  taken as that one, so misreads don't make extra studios. A new name read only in watermarks gets a
  studio once it's been read in a second scene too; the earlier scene then gets it as well. Names from
  links in the file name, title, details or URLs are used straight away.
- The names of studios you already have count too, e.g. `studioname.com` for *Studio Name*
  (symbols count, so *PORN+* needs the `+`); that studio is used first. Otherwise a studio is made for
  the account, with its profile link, under a parent studio for the platform (**OnlyFans**,
  **Fansly**, **Patreon**, **Instagram** or **TikTok**). The platform studios are set to be ignored by Auto Tag, so
  Stash's Auto Tag and Identify don't give them to every file with "OnlyFans" in its name. A scene that
  has a studio never gets a different one.
- Account names, and performers' names read in the watermark, that match a performer (name, alias
  or profile link) add that performer. Performers are only ever added.
- About 5–15 seconds per scene; scenes already looked at are skipped next time.
- **Studios from watermarks** does the same for **photos** without a studio: the model reads the
  photo once (one read counts, as there's only one picture), and the same rules apply, so a new
  account only gets a studio once it's been read in a second photo or scene, and the earlier ones then
  get it too. **Photos without a studio, not in a gallery** is the usual pick; **…galleries too** also reads
  gallery photos (comic pages, sets), which rarely have an account watermark but may show a studio's name.
  **Preview on 10 photos** changes nothing. A few seconds per photo; photos already read are skipped
  next time.

**Faces**
- **Scan scenes and images** finds the faces in every scene (6 frames) and image once and remembers
  them (a few seconds per scene, well under one per image; cancel and rerun to carry on). Needed
  for **Might also be in** and makes the other face tasks quicker.
- **Learn from what you've tagged** learns up to 12 more faces per performer from scenes and images
  where they're the only performer (only faces that look like their profile picture), so other
  angles and lighting match better. Matches you accept are learned straight away.
- **Find performers who may be the same person** lists the most alike pairs of performers in the
  log. Look-alikes score high too, so check them; merge real duplicates in Stash.
- **Learn performers' faces** turns each performer's profile picture (and up to 3 photos where
  they're the only performer, if they look like the same person) into a face fingerprint. Only new
  or changed performers are looked at; the other face tasks do this first anyway.
- **Scenes without performers** / **images without performers** find who they look like. With
  **Faces: add confident matches automatically** on, confident matches are added; otherwise the
  suggestions are only written to the log, for you to pick with **Find performers by face**.
- A *confident* match is very alike (50% by default, images 55%), at least 5 points ahead of anyone
  else who looks similar (look-alikes score close together), and in a scene the closest match in at
  least two frames and in the most. In testing, scenes got the right person first about 2 times in
  3 and in the top three 5 times in 6; wrong confident matches were caught by the gap rule. Small,
  side-on or blurry faces aren't matched at all.

**Performer details**
- **Fill in empty hair and eye colour**: the model looks at each performer's profile picture and
  photos where they're alone, twice with different photos, and only fills a field (if it's empty)
  when both looks agree. Tattoos and piercings are too hit-and-miss to save unseen, so they're only
  offered by **Ask AI for details**. Height, measurements and the like can't be seen in a picture;
  use Stash's scrapers (StashDB etc.) for those.

## New items after a scan

- **AI: tag new scenes and images automatically**: AI tags (checked against look-alikes) and body tags.
- **AI: tag, title and find the studio of new scenes and images**: tags, then a scene's studio and
  performers, then a title (so the title can use what was just found).
- **Faces: add confident matches automatically** also checks new scenes and images.
- **AI: read watermarks on new photos**: new photos without a studio get one from their watermark
  (as above); with **AI: wait until night for new items** on, it's done in tonight's run.
- **AI: wait until night for new items**: the AI model is slow (seconds to a minute per item), and
  Stash runs one task at a time, so a big scan's AI work can hold everything else up for hours.
  With this on, new items get only the quick steps right after the scan (about a second each, no AI
  model): tags from their file names, studios from links in them, look-alike tags, body tags and
  faces. The AI model's steps (AI tags, studios from watermarks, AI titles) are saved and done once
  at **AI: night time** (default 03:00, in **AI: time zone**). A small background process in Stash
  waits for that time; it's started again by the next scan (or the task **AI: run tonight's new
  items now**, which also does the saved work straight away) if Stash restarts.
- Better Tagging Tools' own after-scan steps (dates and titles from file names, gallery-name tags,
  Auto Tag for new performers/studios/tags) are quick and always run straight away.

## Settings

Under **Settings → Plugins → Better Tagging AI Add-on**:

| Setting | What it does |
| --- | --- |
| AI: tag new scenes and images automatically | AI-tag new items after a scan. |
| AI: tag, title and find the studio of new scenes and images | All three AI steps for each new item after a scan. |
| AI tagging: which items | `scenes`, `images` or both (empty): which items the AI model tags, in the tasks, after a scan and at night. The quick steps still run on all. |
| AI titles: which items | `scenes`, `images` or both (empty): which new items get an AI title after a scan and at night. |
| AI: read watermarks on new photos | Studio (and performers) from the watermark of each new photo without a studio. |
| AI: Ollama address | Default `http://host.docker.internal:11434`. |
| AI: model | Default `gemma3:4b`. |
| AI: tags to choose from | How many of your most used tags the model picks from. Default 250. |
| AI: most tags per item | Default 6. |
| AI: don't auto tag from file names first | Skip the file-name matching before the AI looks at an item. |
| AI: include organized items | Also AI-tag items marked as organized. |
| AI: body tags every night | At the night time, run the body tags task for everything not checked yet (items with a body tag are skipped). Works whether or not the AI tags new items. |
| AI: look-alike suggestions from (%) | Edit page "Tags from look-alikes": show tags at least this share of the closest look-alikes have (default 20). The tasks only add at 50%+. |
| AI: skip items with more tags than | Items with more tags than this (default 5) skip the AI model; body tags still run. 0 never skips. |
| AI: wait until night for new items | New items get the quick steps after a scan; the AI model's steps wait for the night time. |
| AI: night time | When the saved AI work runs (HH:MM). Default 03:00. |
| AI: time zone | Time zone of the night time, e.g. `America/New_York`. Default UTC. |
| Faces: add confident matches automatically | Let the Faces tasks (and new scans) add confident matches. |
| Faces: likeness needed to add automatically (%) | Default 50 (images 5 more). |
| Faces: likeness needed to suggest (%) | Default 36. |
| Faces: helper address | Default `http://host.docker.internal:11535`. |

AI settings you set in Better Tagging Tools before the AI had a plugin of its own still count until
you change them here.

## Requirements and notes

- Python 3 (standard library only). Tested with Stash v0.31.
- Ollama with a vision model. Gemma 3 4B takes about 5 seconds per picture on an Apple M4 (a scene
  about 15), so doing thousands of items takes hours. It gets most things right but not all: check
  the previews, and use the **Ask AI** buttons where accuracy matters.
- ffmpeg for video frames (the one Stash uses; the official Docker image includes it).
- The face helper (see [Setup](#2-the-face-helper-faces-look-alike-tags-body-tags)), only for
  faces, look-alike tags and body tags.
- The code is in `btt_core.py`, the same file Better Tagging Tools ships, so each plugin works on
  its own. Small files in the plugin's folder remember what's been done; deleting them is safe.
- Works with any theme.
- Changes are listed in the [changelog](CHANGELOG.md).
