# Better Organizing AI Add-on changelog

Newest first. Versions are what Stash shows under Settings → Plugins. The add-on shares its code
(`btt_core.py`) with [Better Organizing Tools](../BetterTaggingTools), so some versions only bring
that plugin's changes.

## 1.8 (2026-11-24)

- **Markers help scenes**: a scene's markers now count towards its own tags, faces and AI pictures.
  - *Tags*: what a scene's markers look like (each marker picture compared with your tagged scenes,
    photos and markers) is added to the scene's look-alike tags. A tag seen in 2 or more of a scene's
    markers counts fully; one marker alone can suggest it in Review but never adds it on its own.
    Review's cards show the marker's picture, when it's seen, and why ("seen in 3 of its markers").
  - *Faces*: "Faces: scan" also looks at marker pictures, so a performer is found in more pictures of
    a scene (the face card shows the marker picture when that's the clearest).
  - *AI tagging*: a scene with markers sends the AI 4 of its marker pictures (the most different
    ones, skipping an intro marker) instead of 4 fixed frames - faster, and real moments.
- **A scene's tags help its markers**: in Review → Tags → Markers, a tag the scene already has needs
  less look-alike support to be suggested for one of its markers, and ranks higher ("its scene has
  this tag"). The scene's tags alone are never enough - the marker has to look like it too.
- **A marker's tags go on its scene**: a tag you give a marker (in Stash's marker form, or Yes in
  Review → Tags → Markers) that its scene doesn't have is added to the scene too - not Scene Shift,
  excluded tags, or tags you took off that scene. New task "Markers: add their tags to their scenes"
  (with a preview) does it for markers tagged before; turn it off with "Markers: don't add a marker's
  tags to its scene".
- **Review → Faces shows the face**: a scene's card showed its cover, which may not show that
  performer at all - you had to open the video to see the moment. It now shows the frame where the face
  was seen best, cut around that face (made as the card comes up, a moment ahead, and kept); photos
  where the face is small are cut around it too. Also in List and in the zoomed view.
- Fixed: Review could count suggestions for scenes and images deleted from Stash - e.g. a Faces Batch
  group of 374 that loaded nothing - and loading them made Stash log one "not found" error per deleted
  item while the add-on asked again and again (minutes, thousands of errors). Deleted items are now
  dropped from the add-on's face and look-alike records (at most hourly from Review, and at every
  scan), never counted, and lookups ask Stash which items still exist once instead of retrying.
  Review → Faces → Suggested could say "374 left" over an empty page this way; it now only counts
  what it can show, and if a batch ever comes back empty it says why instead.
- **Choose which performers faces are suggested and added for** (Faces settings): **Faces: only
  favourite performers**, and/or **Faces: only performers in at least this many scenes and images**.
  Review → Faces and the face tasks then only suggest or add those (Review says so at the top), so a
  library with thousands of performers isn't months of review. Faces are still compared with every
  performer you have, so a look-alike who isn't chosen isn't mistaken for one who is.
- **Review → Faces: Favourites only** (☆ at the top): only suggestions for your favourite performers.
  Faces are still compared with everyone, so the "next closest" check stays the same.
- **Big libraries no longer stop Stash**: asking Stash for every scene or image at once (Review's
  Tags, Studios, Galleries and New, and many tasks) could take more memory than it has on a library
  of tens of thousands of items. Such requests are now split into pages of 500, and long lists of
  ids into batches - the results are the same. Thanks to scruffware, who found it and sent a fix.
- **Face helper on NVIDIA graphics cards**: with `onnxruntime-gpu` installed, the picture
  fingerprints, search and body models run on CUDA, else on the CPU as before; `/health` says
  `"gpu": true` when it can. Thanks to scruffware for adding this in their fork.
- **Tasks renamed consistently**: each name starts with its group (Setup, Schedules, Tags, Faces,
  Markers, Titles, Studios, Links, Tag pictures, Marker pictures, Image scores, Scene covers,
  Screenshots, Performer details), then what it does; "scenes and images" throughout; the method in
  brackets only where there are two ways to the same result ("(AI model)" / "(look-alikes)"); Mac-only
  tasks say "(Mac)"; every preview ends "(no changes)" and comes last in its group. "AI" is no longer
  used as a label (every task uses AI of some kind); "AI markers" stays as the markers' name.
  Settings follow the same rule: each starts with its section (Setup, New items, Tags, Titles,
  Faces, Markers, Poses, Scene covers, Schedules) and the sections are Setup, New items, Tags & titles,
  Faces, Markers & poses and Scene covers - e.g. "AI: wait until night for new items" is now
  "New items: wait until night for the AI model steps". Only the names changed: your settings keep
  their values.
  AI tagging, look-alike tags, body tags and poses are one **Tags** group, and Scene covers a group of
  its own. Nightly and after-scan lists saved with the old names, and "Don't show this again"
  choices, carry over by themselves.
- **Nightly and after-scan jobs run one at a time**: each job starts once the one before it has
  finished and what it set off has run too (a scan's new items get their titles, dates and tags
  before Identify, Generate and the rest), instead of all being queued at once - a long Generate no
  longer holds up the new items. Other jobs in Stash's queue are waited for up to 45 minutes. The
  jobs page shows the ones still to come as "Waiting".
- Titles from file names leave out hashtags (`#amateur #solo`, from X / TikTok downloads); `#12`
  numbering stays.
- AI titles follow Better Organizing Tools' naming style for titles (2.4), when it's installed and set.
- **Windows fixes** (both plugins, from a check of everything for Windows):
  - Names with accents or in other alphabets, and the log's "→" / "✓", were garbled or shown as
    `\u2192` escapes - Python used the console's code page for Stash's pipes and the night runner's
    log. They're UTF-8 now, and text sent from the page (titles, names) arrives intact.
  - ffmpeg and the other tools get no console window of their own (no windows flashing up when Stash
    runs without a console).
  - Every record file is saved with a retry: on Windows replacing a file fails while another task
    is reading it for a moment (23 places didn't retry).
  - The night runner check gives Windows its exact types (64-bit handles).
- Fixed: the nightly and after-scan job lists could go back to an older version. Settings → Plugins
  saves all of a plugin's settings as the page loaded them, so changing any setting on a page opened
  before the list was changed put the old list back. The lists are now also kept in the add-on's own
  file (`job_lists.json`), which wins.
- The jobs page shows what's saved now whenever you come back to it (another app, tab or device may
  have changed the lists), and a page that loaded a list before it was saved elsewhere can't save its
  older copy over it - it says so and shows the newer list.
- **A check before a task starts** (Settings → Tasks): pressing one of the add-on's tasks first checks
  that what it needs is set up (the AI model in Ollama, the face helper, Apple's scores on a Mac, the
  video model, the look-alike scan) - if not, it says what's missing and opens the setup page. A task
  that would take 10 minutes or more shows roughly how long (from what's still to do) with **Run
  now**, **Add to the nightly run**, **Don't show this again** (for that task, in this browser) and
  **Close**. Quick, ready tasks start straight away as before.
- **Suggest profile pictures** (performer edit): their photos (best-scoring first) and frames of their
  scenes, cut to a 2:3 portrait around their face - only pictures where the face is theirs (their
  known faces; without those, pictures with one face). With Apple's score (a Mac) the best 9 first,
  else the closest faces; **Look at more pictures** keeps the best. **Use this picture** puts it in the
  form like Set image, so Save keeps it and Cancel doesn't.
- **Links in URLs**: the studios-from-accounts tasks (and new scenes and photos after a scan) also put
  the accounts' profile links in the item's URLs - from its file name, title, description or links,
  or from a watermark when it's a studio or performer you have or was read in another item too - and
  web links written in its title or description (not a bare home page like "metadata provided by
  https://…"). New task "Links: add account and written links to scenes and photos" does the quick,
  no-AI part for every scene and photo.
- **No frames from end cards**: phone clips often end on TikTok's or Instagram's end card (a dark
  screen with their logo), a creator's logo card, or Control Center. Frames for covers, Suggest
  covers, the AI's pictures, faces, look-alikes, body tags and positions now come from before that,
  and never from the last 2 seconds (227 of 571 vertical clips here had an end card). AI markers
  don't start on one. Each video's end is worked out once and kept (`video_ends.json`).
- **More varied cover suggestions**: Suggest covers no longer shows several near copies of the same
  shot. Each suggestion has to look different from the better ones before it (among covers scoring
  close to the best); near copies only fill places left when a video is mostly one shot. The same
  goes when Compare more frames merges rounds, and for stitched covers (which no longer reuse the same
  moments). Each Compare more frames round now shows 9 more (18, 27… up to 36), the best so far.
- **Stitched cover suggestions**: with [Vertical Stitcher](../VerticalStitcher) (1.4 or later)
  installed and turned on, Suggest covers on a vertical scene also shows covers stitched its way -
  from the best-scoring frames (on a Mac) and evenly spaced - each scored, above the single frames.
  A chosen one is saved as Vertical Stitcher's, so the covers tasks leave it alone; Undo works as usual.
- **Vertical Stitcher's covers are recognised**: the covers tasks call them "made by Vertical
  Stitcher" and always leave them alone; Vertical Stitcher picks its frames with this add-on's
  scores when both are installed.
- **Markers first in Review**: in Review → Tags → Suggested, when one of a scene's markers is
  suggested the same tag, the card is that marker's ("Tag this marker …?", playing from the marker) -
  a clearer view of what's going on, and Yes tags the scene too. Undo takes it off both. Scenes
  without such a marker keep their scene card.
- Fixed: tapping **+ Add all** on AI markers (or Suggested chapters on a gallery) several times
  quickly added the markers several times. The buttons now lock at once and show "Adding…" until
  it's done, and markers a scene already has at that time are never added again.
- Fixed: the plugins' file locks didn't work with Stash in Docker Desktop on a Mac (the plugins
  folder is shared into the container, where locks aren't kept), so records updated at the same
  moment - auto tags, queues, picture records - could overwrite each other. Locks are now kept in
  the computer's own temporary folder.
- **After-scan jobs**: the jobs page (now "Nightly & after-scan jobs", `/ai-nightly`) has a second
  list, **After a scan** - Stash's own tasks (all but Scan) and any plugin's tasks, in your order, run
  when a scan you start finishes. Scans the nightly run starts don't count, several scans close
  together start it once, and a cancelled scan doesn't. New task: "After-scan jobs: run now".
- The page has a tab for each list, each with its own Save, Run them now and Last run.
- **Any order**: each list is now one list you can put in any order - Stash's tasks too (e.g. Scan,
  Identify, AI markers, AI tagging, AI titles, or Auto Tag after the AI tagging), with ↑ Up / ↓ Down.
  Stash's tasks are added from the same picker as plugin tasks, or with Quick add. Lists saved before
  keep their order.
- **Tidier settings and tasks**: the settings are in sections (Connections, New items after a scan,
  AI tagging, Faces, Markers & poses, Schedules) instead of Stash's alphabetical order, and the night
  time and job lists are a link to the jobs page. Every task is in a group (11 groups, setup and
  schedules first), with the main task first and previews after. Descriptions are a sentence or two
  (about half as long). The old "AI: body tags every night" setting is gone (it's a nightly job now).
- **Better scene covers** (on a Mac): "Scene covers: replace low-scoring ones" gives a scene whose
  cover Apple scores low (blurry, dark, mid-move, nobody in it) the best of 9 frames from across the
  video, when it's clearly better and decent on its own. Only covers Stash generated are changed - a
  studio's or scraper's cover, or one you set, is recognised and left alone (vertical videos: see
  below). The originals are kept in `cover-originals` in Stash's config
  folder; "Scene covers: put back the originals" restores them. Each cover is checked once, so it
  suits the after-scan and nightly job lists (it's one of the suggestions).
- **Suggest covers** on a scene's Edit tab (under Cover Image): 24 frames from across the video are
  compared with Apple's score out of 100 and the best 9 shown, with the current cover's score and
  where it most likely came from; **Compare more frames** looks at 24 more in between and keeps the
  best 9 so far (newcomers marked New). Choosing one saves it, folds the suggestions away (Show the
  suggestions again opens them) and shows the new cover in the form at once - it stays when the
  form redraws. **Use this cover** saves it straight away (full
  size) and **Undo** puts back the cover from just before; the very first one stays kept for "Scene
  covers: put back the originals". Without the face helper on a Mac (e.g. Stash on Windows) the
  frames show without scores, in video order.
- **Vertical videos' covers**: the covers task and Suggest covers handle vertical scenes too, cutting
  each frame to a wide cover around the people like AI marker pictures (on a Mac; elsewhere a third
  of the way down), so they don't need Vertical Cover Stitcher. A stitched cover isn't one frame of
  the video, so it's left alone like a scraped one.
- **Cover options**: covers Stash made are told apart from scraped or uploaded ones and ones you picked
  in Stash's player, and three settings (off by default) decide whether those - and organized scenes'
  covers - may be replaced too. Stash doesn't record where a cover came from: its own are recognised
  for certain (its frame 20% into the video); scraped (the scene is linked to StashDB / ThePornDB, or
  the cover isn't a frame of the video) and picked (a frame of the video on an unlinked scene) are a
  best guess.
- **No Auto Seek needed**: Auto Seek's code (0.3.1, AGPL-3.0, unchanged) now ships with the add-on in
  `video-model`, and its model and Alpine runtimes are kept in this repository (`extras/video-model`).
  The new task "Video model: download it (for AI markers and positions)" downloads them from there
  (falling back to the original addresses), each checked against its SHA-256. The copy now lives in Stash's config
  folder (`plugin-data/BetterTaggingAI/auto-seek`; an existing `auto-seek-copy` is moved there), so
  updating or reinstalling the add-on keeps it. Marker pictures, Create Marker's picture and scene
  covers only use ffmpeg now - before, Create Marker's picture silently failed without Auto Seek.

## 1.7.1 (2026-11-23)

- Fixed: 1.7 didn't load ("yaml: line 248: mapping values are not allowed in this context"): two
  setting descriptions with a colon in them weren't quoted. Every plugin's YAML is now checked before
  the repository is published, so a file Stash can't read can't go out again. Thanks to the user who
  reported it with the fix.

## 1.7 (2026-11-21)

- **Nightly jobs** page (`/ai-nightly`, 🌙 on the AI setup page): choose what runs every night - Stash's own
  tasks (backup, scan, clean, identify, auto tag, generate, clean generated files, optimise) and any
  plugin's tasks in your order - with Run them now and how last night's jobs went. Scan, Identify,
  Auto Tag and Generate use the options chosen on Stash's Tasks page; Clean is skipped if a library
  folder is missing or empty. New task: "Nightly jobs: run now".
- "AI: body tags every night" is now one of the nightly jobs (it's carried over until the page is saved).
- The night timer replaces itself when the add-on is updated, so new night code is used straight away.
- With Stash running natively (not in Docker, e.g. on Windows), the Ollama and face helper addresses
  default to this computer (`127.0.0.1`) instead of `host.docker.internal`. Set addresses are kept.
- README: setting up the face helper on Windows (PowerShell steps, tested by a user on Windows 11).

## 1.6.1 (2026-11-20)

- Fixed: on Windows (Stash running natively) every task and button stopped with
  `ModuleNotFoundError: No module named 'fcntl'`. File locks now use `msvcrt` on Windows and `fcntl`
  elsewhere, as before. Thanks to the user who reported it with a fix.
- Fixed: checking whether the night timer runs no longer uses `os.kill(pid, 0)`, which on Windows ends
  the process instead of checking it, and works without `/proc` (Stash running natively on a Mac).
- Messages that ask you to do something first (e.g. "run Look-alike tags: scan scenes and images
  first") are logged as a short info line instead of an error with a traceback.

## 1.6 (2026-11-16)

- A welcome on first run, with an optional tour of the features (screenshots in `tour/`), remind-me-later
  and don't-show-again.
- When something isn't set up, the welcome offers a step-by-step setup guide (screenshots, commands to
  copy, the matching common problems, Check again) instead of the tour.
- Features hide until what they need is installed (checked with the new capabilities request).
- Fixed: the gallery Chapters button (and other page parts) could fail to appear on pages another
  plugin kept changing; they now check at least every 150-200 ms.
- **Search by description** (Search in Stash's menu): find photos, scenes and AI markers by describing
  them, with CLIP's text model in the face helper (run `face-helper/setup.sh` again).
  Better results: several wordings averaged, near-identical pictures once, at most two per scene, kind
  filters that fill the page, deleted markers skipped, a note when nothing matches well, and a message
  that names what's wrong when the face helper can't be reached.
- **Pictures: aesthetic score and labels**: Apple's aesthetic score and picture labels for every photo,
  saved as the custom fields `aesthetic` and `seen` (filterable in Stash); Review shows better-looking
  photos first.
- Fixed: a tag you removed from an item (or said No to in Review) could be added back to it by a later
  task. Removals in Review → Check, No answers, and tags the add-on added that you took off anywhere are
  now remembered per item and respected by every task and suggestion.
- **Ass** also for clothed butts sticking out (from the photo's look), in body tags and the new quick
  task **Body tags: clothing from the look (quick)**.
- Review → Tags → Suggested also shows body tags the detector was nearly sure of, for items with 3 tags
  or fewer (kept by the body tags task), so untagged photos get something to approve.
- **Screenshots: crop to the photo** (with a preview and an undo): phone screenshots of posts are cut
  down to the photo in them; the originals are kept in `screenshot-originals` in Stash's config folder.
- Tag pictures and AI marker pictures are checked with Apple's aesthetics score on a Mac (face
  helper): a blurry, dark or screenshot-like one is passed over for the next candidate (for a marker,
  a frame 3 or 6 seconds on, or 2 or 4 seconds before it), and the best is used if none scores well.
  New tasks **Pictures: replace blurry or dark tag pictures** and **Pictures: replace blurry, dark or
  empty marker pictures** (with a preview) do the same for pictures tags and markers already have. A
  marker picture with nobody in it (title card, logo, black frame) counts as a bad one.
- Marker pictures from vertical scenes are cut to a wide picture around the people in them (faces kept
  in) instead of being cropped in the middle by cards and the marker row.
- Tag pictures: a decent photo is always used before a scene screenshot, marker or video frame, and
  "Most typical picture" tries a tag's photos first.
- Pose suggestions (in **Review → Tags → Suggested** for photos and **→ Markers** for markers) (Face Down Ass Up, All Fours, Frog Pose, Kneeling…, set
  with **Poses: tags**) for photos and markers, learnt from at least 3 you've tagged with each: by body
  shape (Apple's body pose, new task **Poses: read body poses**) and by look. No answers count against a
  pose.
- The face helper's Apple request reads phone photos the right way up (their rotation) and can give
  each person's body joints.
- On a Mac, body tags use Apple's Vision through the face helper (run `face-helper/setup.sh` again):
  its picture classifier adds Woman's Heels, Boots, Sneakers, Socks, Shoes, Jeans, Glasses, Swimsuit /
  Bikini, Mask, Gloves, Costume and Hat, and its hand and body pose add **Hand Bra** (fingers over a
  breast, one person in the picture).
- Body tags add clothing to photos: Lingerie, Panties, Thong, Bikini, Latex, Leather, Crop Top, Skirt,
  Socks, Sneakers, Schoolgirl Outfit and Animal Ears (from the picture fingerprint, at a high bar; not on
  drawings or comics), **Shoes** from covered feet, **Bikini** instead of Bra / Panties for a bikini, , **Topless** (bare breasts, covered
  below) and **Nude** (bare breasts and below with nothing covered). Hands over the breasts no longer
  count as a bra. (Hand Bra isn't added automatically: neither model can tell it from a nude photo.)
- Body tags also add **Bra**, **Panties** and **Feet** (when you have those tags): covered breasts or
  covered genitals with a bare belly (so a shirt or jeans don't count), and bare feet. A single photo
  needs less to go on (0.05 over the bar, was 0.1), so a clearly shown part isn't missed. A body tag
  run with nothing to do says why.
- **Suggest chapters** on a gallery's Chapters tab: chapters where the gallery changes (folder, file
  names, look, performers, studio, tags, date), with titles to edit before adding.
  Comics (zip / cbz) get chapters from the chapter headings printed on their pages, or split after
  each translator's credits page; this uses a new text reader in the face helper (Apple's, on a Mac:
  run `face-helper/setup.sh` again).
  Tap a suggested chapter's picture to choose its images (it grows or shrinks, and the chapters next to it
  follow); added chapters are put away.
- **Review → Tags → Markers**: tag suggestions for each marker, from its own look-alikes (never its own
  scene) and the positions seen in it; markers you tag are learned from. Markers no longer get a copy of
  their scene's tags.
- **Review → Tags → Suggested** also suggests positions seen in a scene's video that it isn't tagged
  with, with the moments they were seen (one card when look-alikes agree). Uses the Auto Seek plugin's model (it must be installed); run
  **Positions in videos: look at scenes** first. Only the four positions it got right most often in
  a test on tagged scenes are suggested, and nothing is tagged without you.
- **AI markers** on a scene's page (**Markers → ✨ AI markers**): where the scene shifts, each with a
  picture, as markers named by their time and the scene's title (tagged *Scene Shift*, or the tag in the new setting **Markers: tag for AI markers**) that run until the next shift; the
  picture becomes the marker's picture, so it shows without running Generate.
- Keeps a copy of Auto Seek's model, runtime and frame code in its own folder (`auto-seek-copy`), so
  positions and AI markers keep working if Auto Seek is removed.
- AI markers find shifts with picture fingerprints too (CLIP, from the face helper), comparing each
  frame with its closest match so camera cuts don't count: they now catch changes in scenes the 10
  positions don't cover, which the positions alone missed. Frames are kept per scene in `video_frames/`. The pictures are compared over the whole scene too (intros, interviews),
  the scene's start gets a marker when the action starts later, and a marker over 12 minutes gets a second
  look, so long scenes in one room no longer end up with a single marker.
- **Create Marker** on a scene's page opens a quick panel: start from where the video is, suggested end
  times (next marker, the next big changes, +1 min, end of scene), the name filled in, and the marker it
  falls inside shortened to end there; with Undo. Stash's own form is a tap away.
- Changing a marker's times (shortening the one a new marker falls inside, Undo, redoing markers) keeps
  its picture: Stash deletes it whenever a marker's times change, so it's put back. Markers made or
  moved in Stash's own form get their picture straight away too (no Generate needed).
- A scene with only one or two AI markers is also split at regular times (every 2-6 minutes,
  depending on its length), each moved to the biggest change near it. Redoing markers keeps the tags
  you've given them.
- Task **AI markers: redo with the newest method**: replaces existing AI markers (other markers are kept). A scene
  that can't be looked at (the video or face helper busy) is tried again at the end, and any still left
  are named in the log instead of quietly keeping their old markers.
- Task **AI markers: scenes without markers** (and a preview): the same for every scene that has no
  markers yet.

## 1.5 (2026-11-11)

- Sub-tag cards have **⇅ Switch parent and sub-tag**, to put them the other way round (merging instead follows the switch too).
- Sub-tag cards have **…or merge into [tag] instead**. Batch view has **Select all** / **Deselect all** (on Check: **Mark all to remove** / **Keep all**). On phones the Review action row wraps instead of running off the screen.
- Review → Tags → Similar knows synonyms and broader terms: *Big Tits* / *Big Breasts* is a merge, *Big Tits* goes under *Tits*, *Blowjob* under *Oral*. Body tags are included now. Add your own with **Tags: synonyms**.
- Setup & troubleshooting shows where Stash runs (Docker Desktop, Docker on Linux, or installed directly), and **Find Ollama** tries every usual address from there and can save the one that works.
- Setup & troubleshooting: **Narrow it down** buttons under the common problems (check the connection, list the installed models, check library folders, run a preview task...), with the result shown in place.
- Setup & troubleshooting: buttons that run the task fixing a check, **Run a task** with progress and Stop, and **Report a problem** (a filled-in GitHub issue to review and submit).
- **AI setup & troubleshooting** page (`/ai-setup`, linked from Settings → Tasks and Settings → Plugins): checks Ollama, the model, the face helper and its models, picture and library access, scans and settings, with fixes; a timed AI test, recent errors, common problems and a copyable report. Also the task **Setup check: write a report to the log**.
- Review → Tags has **Similar** (tags to merge, or to nest as sub-tags) and **New** (groups of look-alike pictures no tag covers, to make into a tag or add to one). The bulk Edit window has look-alike tag suggestions for the selected items. The face helper can group picture fingerprints (run `setup.sh` again to update it).
- No suggestion rows in Stash's scrape dialog: on a computer the face row covered the scraped performers' box.
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
