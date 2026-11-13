# Modern UI changelog

Newest first. Versions are what Stash shows under Settings → Plugins.

## 0.56 (2026-11-13)

- The dashed **Filters (n)** button under the quick filters no longer disappears when no filter is
  in use: it shows **Filters (0)** then, and tapping it opens the filter panel.
- The filter-panel button at the start of the list toolbar is checked every second, so it no longer
  sometimes goes missing (leaving Stash's own sidebar tab) on Safari.

## 0.55 (2026-11-12)

- Phones: swiping past the end (or start) of a list on an image, scene, gallery, performer, tag,
  studio or group page goes back to where you opened it from, like swiping down; in the image page's
  full-screen viewer it closes the viewer. (A slightly longer swipe than going to the next one.)
- Once you select a scene, image or gallery, every card in the list shows a strip along the bottom of the picture: whether
  it's organized (green) or not (amber), how many tags it has, and its performers (the first two,
  then "+n"). Handy before a bulk edit. Turn it off with **Hide details on selected cards**.

## 0.54 (2026-11-11)

- Phones: swipe down on a picture (at the top of the page) to go back to where you opened it from
  — a list, gallery, performer… — scrolled to the same spot, even after swiping through other
  images. Works for every image, not just ones in galleries. The **← gallery** button is now
  hidden on phones.
- Phones: the same swipes on scene, gallery, performer, tag, studio and group pages: left/right
  for the next/previous one in the list or row you opened it from, down to go back there. Swipe
  on the top part of the page (a scene's video or title box, a gallery's title box, the header
  of a performer, tag, studio or group). The **← Galleries** button is hidden on phones too.
- Home page **Customize**: a **⠿ Move** handle on each row to drag it up or down, which works on
  phones (Stash's own drag and drop only works with a mouse). The rows are also readable in the
  light theme now (they were dark grey with dark text).
- Full-screen picture viewer: **swipe down** to close it (when not zoomed in).
- Gallery pages: the cover is bigger and shows the whole picture (not cropped square), with the
  name under it, like a tag page.
- Tag, studio and group pages: no grey frame around the picture any more, just rounded corners
  and a soft shadow (the gallery cover has the same).
- Scene pages: **Show timeline / Hide timeline** in the ⋮ menu for the strip of thumbnails and
  markers under the video, remembered separately for phones and bigger screens. On phones (where
  Stash never shows it) you can now turn it on, drag it with a finger and tap a thumbnail to jump
  there.
- Saving a gallery's Edit tab takes you back to its Details tab, like scenes and images.
- Swiping on a scene's page is off while its video is playing; pause it to swipe again.
- Scenes: in full screen (playing or paused), **swipe up on it for the next scene in the
  queue** (down for the previous one), which starts playing straight away (no queue: the
  next / previous one in the list you came from).
  On iPhones, full screen now fills the page instead of opening the phone's own video player, so
  the swipe works there too. **Double-tap the video** (or tap **Native Player** in the top right in
  full screen) to switch to the iPhone's own player; Done there brings you back. **✕ Exit** in
  the top left leaves full screen.
- Fixed: swiping quickly between scenes could leave the previous scene's picture on the player
  (vertical videos).
- Fixed: on phones the scene player sat 15px off to the left; it's now edge to edge and centred.
- Edit dialog for several selected items: the chosen **Overwrite / Add / Remove** button is now
  highlighted (all three used to look the same).
- **Remove** now lists everything the selected items have (tags, performers, galleries, groups),
  most common first with how many have it; tap to pick what to remove, or **All**. Going from
  Remove to another mode empties the box, so those aren't added by mistake.
- Scene, image and gallery pages (phones and computers): the studio moved out of the title box to
  a **Studio:** line just above the performers. The title box no longer has a background
  of its own (it was grey so white studio logos would show up); the title, date and buttons line up with the tabs, which sit just
  under them on a thin line, with the details right below.

## 0.53 (2026-11-11)

- **Quick filters:** your saved filters as a row of buttons under the list toolbar, on scene,
  image, gallery, performer, studio, tag, group and marker lists. The one in use is highlighted.
  Setting: **Hide quick filters**.
- The filters in use are tucked away under a **Filters (n)** button, with **Hide** and **Clear**
  buttons once shown, and scroll sideways on one line.
- Fixed: tapping a second quick filter often didn't load it (or highlighted it without loading it).
- Fixed: on phones, the button next to the one you tapped could stay grey.

## 0.52 (2026-11-11)

- The **Save** button in the saved-filter menu says "Save", stays faded until you've typed a name,
  and lights up when it can be used.

## 0.51 (2026-11-11)

- Gallery pages: the gallery's cover next to its name, and a **← Galleries** button back to the
  list as you left it.
- Image pages: a **← gallery name** button back to the image's gallery.

## 0.50 (2026-11-10)

- Full-screen picture viewer on image pages: zoom with the wheel, a pinch or a double-tap, drag to
  move, and swipe (or arrow keys) to the next image.
- Phones: hold a card and slide over others to select them all at once (also once some are
  selected).
- Saving an edited scene or image goes back to its **Details** tab. Setting: **Stay on Edit after
  saving**.
- Vertical videos with a stitched cover show one frame in the tall phone player before playing.
- Login page (`extras/login.css`): the sign-in box stays near the top on phones, above the keyboard.

## 0.49 (2026-11-09)

- The Suggested / Similar rows moved to their own plugin,
  [Stash Suggestions](../StashSuggestions). Install it to keep them.
- **Count** sort on the Tags, Performers and Studios pages (scenes + images + galleries).
- Tag cards look like gallery cards, with the tag's picture filling the card.

## 0.48 (2026-11-05)

- Image details sit under the picture on wide screens. Setting: **Image details beside the
  picture**.
- Better Similar photos, More from [gallery or row] and More with [performer] rows on image pages.

## 0.33 (2026-11-05)

- The Suggested / Similar rows are hidden while an edit form is open.
- Screenshots in the README.

## 0.32 (2026-11-04)

- First release: cleaner cards, dark / light / auto themes, phone layout with a bottom tab bar,
  scene details under the video, A–Z tag index.
