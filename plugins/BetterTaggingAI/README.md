# Better Tagging AI Add-on

An add-on for [Better Tagging Tools](../BetterTaggingTools) that does the work a local AI model is
good at: tagging scenes and images, writing titles, and finding a scene's studio and performers
from the accounts and studio watermarks it shows. The model runs on your own computer through
[Ollama](https://ollama.com); nothing is sent to the internet.

Install it next to Better Tagging Tools. It then uses that plugin's **Tags to ignore**, **Special
spellings** and gallery-name tags, and its rows sit under Better Tagging Tools' suggestions. It also
works on its own.

## Setup

1. Install Ollama (on a Mac: `brew install ollama`, then `brew services start ollama`).
2. Download the model: `ollama pull gemma3:4b` (about 3 GB).
3. If Stash runs in Docker on the same computer, the default address
   (`http://host.docker.internal:11434`) works as is. Otherwise set **AI: Ollama address**.

## While editing

- **AI** row under the **Tags** box (scenes and images): **Ask AI for tags** shows the model's picks
  as one-click chips. Nothing is added until you click them and save.
- **AI titles** row under the **Title** box (scenes, images and galleries): **Ask AI for titles**
  shows a few titles, written after looking at the picture (or a few frames) and its performers,
  studio, tags and file name. Click one to put it in the box. Ask again for different ones.

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

**AI titles**
- **Scenes without a title** / **images without a title**: each gets the title from its file name
  when that makes a good one, otherwise the AI's best idea. Titles you already have are never
  changed. (With Better Tagging Tools, AI titles aren't touched by its **Redo titles**.)

**Studios from accounts**
- **Also read watermarks with the AI**: for scenes **without a studio**. Account names are taken from
  links in the file name, title, details or URLs, and from the watermark and on-screen text the
  model reads in up to 6 frames. It only transcribes the text (asked for names directly, small models
  tend to make one up), and a name only counts if two frames agree, so one-off misreads are ignored.
- The names of studios you already have count too, e.g. `studioname.com` for *Studio Name*
  (symbols count, so *PORN+* needs the `+`); that studio is used first. Otherwise a studio is made for
  the account, with its profile link, under a parent studio for the platform (**OnlyFans**,
  **Fansly**, **Instagram** or **TikTok**). A scene that has a studio never gets a different one.
- Account names, and performers' names read in the watermark, that match a performer (name, alias
  or profile link) add that performer. Performers are only ever added.
- About 5–15 seconds per scene; scenes already looked at are skipped next time.

## New items after a scan

- **AI: tag new scenes and images automatically**: AI tags only.
- **AI: tag, title and find the studio of new scenes and images**: all three, in that order (tags,
  then a scene's studio and performers, then a title), so the title can use what was just found.

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

AI settings you set in Better Tagging Tools before the AI had a plugin of its own still count until
you change them here.

## Requirements and notes

- Python 3 (standard library only). Tested with Stash v0.31.
- Ollama with a vision model. Gemma 3 4B takes about 5 seconds per picture on an Apple M4 (a scene
  about 15), so doing thousands of items takes hours. It gets most things right but not all: check
  the previews, and use the **Ask AI** buttons where accuracy matters.
- ffmpeg for video frames (the one Stash uses; the official Docker image includes it).
- The code is in `btt_core.py`, the same file Better Tagging Tools ships, so each plugin works on
  its own. Small files in the plugin's folder remember what's been done; deleting them is safe.
- Works with any theme.
