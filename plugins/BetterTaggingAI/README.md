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
2. Download the model: `ollama pull gemma3:4b` (about 3.3 GB).
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
sh face-helper/setup.sh          # faces and look-alike tags
sh face-helper/setup.sh --body   # the same, plus body tags
```

- **Needs:** Python 3 and `curl` (both come with a Mac). Mac and Linux; on Windows it's untested
  (WSL may work).
- **Installs:** a Python environment with OpenCV, NumPy and ONNX Runtime, and the models (about
  650 MB in all) in `~/Library/Application Support/face-helper` (Mac) or
  `~/.local/share/face-helper` (Linux; set `FACE_HELPER_DIR` to choose another folder).
- **Runs:** on a Mac it's set up to start at login and restart if it stops. On Linux it prints the
  command to start it (put it in a systemd user service to keep it running).
- **Check it:** `curl http://127.0.0.1:11535/health` should answer
  `{"ok": true, "embed": true, "body": false}` (`"body": true` with `--body`).
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

**Licences:** Gemma 3 is under Google's Gemma terms, CLIP under MIT, and YuNet and SFace come from
OpenCV's model zoo. NudeNet is **AGPL-3.0**, so it isn't included: `setup.sh --body` downloads it
from NudeNet's official Python package (version 3.4.2) on your own computer.

## While editing

- **AI** row under the **Tags** box (scenes and images): **Ask AI for tags** shows the model's picks
  as one-click chips, checked against look-alikes (✓ = look-alikes back it up). **Tags from
  look-alikes** (quick, no AI model) shows the tags of the scenes and images you've tagged that look
  most like it, with how many of them have it, e.g. `+ Outdoors 72% ✓`; these load by themselves
  when you open **Edit** (once the look-alike scan has run). Nothing is added until you click them
  and save.
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

## Performer pages: "Might also be in"

A **Faces** row above the tabs on a performer's page: **Might also be in…** lists scanned scenes and
images that don't have them but where someone looks like them (and they're the closest match of
all your performers), with a thumbnail and how alike. Confident matches (✓) are ticked; tick the
right ones and press **Add them to … selected**. **✕ Not them** takes one off the list for good;
**✕ Close** puts the list away.

## Tasks

Under **Settings → Tasks → Plugin Tasks → Better Tagging AI Add-on**, in sections you can open and
close. Each section starts with a preview that only writes to the log.

**AI tagging**
- **Items with no tags** and **everything not done yet**. They only ever *add* tags, skip organized
  items (unless you change that), and remember what's done, so you can stop a run and start again.
- The model is shown a picture (or 3 frames from a scene) and your most used tags (250 by default)
  and can only answer with tags from that list, so it never invents tags. Before that, each item
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
  so its guesses don't reinforce themselves. A tag you've removed from most of the items it was
  added to (at least 4) isn't added any more; the tasks list such tags in the log.
- Accuracy depends on how consistently you tag: items in a folder or gallery you've already tagged
  get the best results; a completely new kind of picture gets fewer, less sure tags.

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
- **Scenes and images** does every item once (cancel and rerun to carry on). AI tagging, new items
  after a scan and **Ask AI for tags** add them too.

**AI titles**
- **Scenes without a title** / **images without a title**: each gets the title from its file name
  when that makes a good one, otherwise the AI's best idea. Titles you already have are never
  changed. (With Better Tagging Tools, AI titles aren't touched by its **Redo titles**.)

**Studios from accounts**
- **Also read watermarks with the AI**: for scenes **without a studio**. Account names are taken from
  links in the file name, title, details or URLs, and from the watermark and on-screen text the
  model reads in up to 6 frames. It only transcribes the text (asked for names directly, small models
  tend to make one up), and a name only counts if two frames agree, so one-off misreads are ignored.
- A name read in a watermark that's a near miss of a studio or performer you have (`f1nnster` or
  `flnn5ter` for *f1nn5ter*; letters that look alike, like 1/l/i, 0/o and 5/s, count as the same) is
  taken as that one, so misreads don't make extra studios. A new name read only in watermarks gets a
  studio once it's been read in a second scene too; the earlier scene then gets it as well. Names from
  links in the file name, title, details or URLs are used straight away.
- The names of studios you already have count too, e.g. `studioname.com` for *Studio Name*
  (symbols count, so *PORN+* needs the `+`); that studio is used first. Otherwise a studio is made for
  the account, with its profile link, under a parent studio for the platform (**OnlyFans**,
  **Fansly**, **Instagram** or **TikTok**). The platform studios are set to be ignored by Auto Tag, so
  Stash's Auto Tag and Identify don't give them to every file with "OnlyFans" in its name. A scene that
  has a studio never gets a different one.
- Account names, and performers' names read in the watermark, that match a performer (name, alias
  or profile link) add that performer. Performers are only ever added.
- About 5–15 seconds per scene; scenes already looked at are skipped next time.

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
| AI: Ollama address | Default `http://host.docker.internal:11434`. |
| AI: model | Default `gemma3:4b`. |
| AI: tags to choose from | How many of your most used tags the model picks from. Default 250. |
| AI: most tags per item | Default 6. |
| AI: don't auto tag from file names first | Skip the file-name matching before the AI looks at an item. |
| AI: include organized items | Also AI-tag items marked as organized. |
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
