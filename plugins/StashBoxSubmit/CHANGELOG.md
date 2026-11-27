# Stash-Box Submit changelog

Newest first.

## 0.1 (2026-11-25)

- First version: **Submit to FansDB / StashDB…** in a scene's ⋮ menu.
- Follows your theme or UI plugin: the check window is Stash's own dialog, so the theme colours it, and the
  Ready to submit page takes its colours (and its green, red and yellow) from the theme.
- **Checks** the scene against the site's own guidelines before sending:
  - whether your account can submit edits;
  - content the site doesn't accept;
  - the video fingerprint;
  - duplicates by fingerprint;
  - title, date and links.
- **Links** performers and studio to the site's entries (by your existing links, their links, or a
  name you confirm), and matches tags to the site's own names. No tags are ticked to start with.
- **Sends the draft** with its cover and fingerprints, and suggests the edit comment the site asks
  for.
- **Adding a performer the site doesn't have yet:** a preview of their draft following FansDB's
  rules (main name, gender, account links, usernames as aliases but no social media usernames,
  picture), what's still missing, a button to add the account link from the scene to them in your
  Stash, and a private draft to finish on the site.
- **Studio by posting account:** when a video was posted from a different account than its studio
  in your Stash, FansDB's studio is that account's, with a warning.
- **Adding a creator's account as a studio on FansDB**, named the way FansDB's guidelines name
  accounts (`username (OnlyFans)` under the network's studio). It's sent as a public studio edit
  when you click, and linked when it's accepted. Meanwhile the scene draft names it.
- **Remembers** the matches you confirm, linking them in your Stash.
- **Ready to submit page** (task: "Find scenes ready to submit"):
  - scenes with most of what a submission needs, best first, per site;
  - ones each site already has, found by fingerprint, are marked so you link them instead;
  - drafts you've sent are remembered;
  - each scene can be submitted to FansDB, StashDB or ThePornDB from there.
  - a **Before you submit** box lists the cautions (drafts expire after 24 hours, bulk drafts
    have no tags, StashDB's limit for new contributors, fingerprint matches to check, ThePornDB's
    basic checks, read-only accounts, submissions are public). Collapse it and it stays collapsed.
- **Final review** panel on the Ready to submit page: for each site, buttons to **Your drafts** and
  **Your edits** there, and a **Review draft** button for each draft waiting (with the time left
  before it expires; FansDB and StashDB list every draft you have, ThePornDB the ones sent from here).
  The check window and the bulk report have the same buttons once a draft is sent.
- **One page, four sections** - **Scenes**, **Performers**, **Studios**, **Pending** - with a row of
  site buttons (All sites, FansDB, StashDB, ThePornDB) that filters whichever section is open. The
  address remembers both (e.g. `?section=studios&site=fansdb`).
  - **Performers** (new): the performers your ready scenes need on each site - found by a link
    (**Link in my Stash**, or **Link all found**), a name match to check (**That's them**), or not
    there (**Send performer draft**, or what to add first).
  - **Studios**: every site you can submit to, not just FansDB (one-click creation stays FansDB's).
  - **Pending**: your drafts with time left, and your edits waiting for votes (studios, performers,
    scenes - made here or on the site).
- **Big libraries:**
  - **Check again** runs as Stash's task "Find scenes ready to submit" in the background, with its
    progress on the button and a bar (also under Settings → Tasks); you can leave the page - it
    picks the progress up again when you come back. Scenes are read 500 at a time instead of all at
    once (which can run Stash out of memory on a big library).
  - **Performers** and **Studios** show the saved answers at once, then look up the rest **20 at a
    time**, filling the list in after each round ("N still to look up", with **Stop** / **Carry
    on**); each request stays short. **Look up again** works the same way. Switching section stops
    it; coming back carries on.
  - **Scenes** shows 50 at a time, with **Show more**.
- Studios made in your Stash for FansDB-linked scenes follow Better Organizing Tools' **Naming:
  account studios with their platform** setting ("izzygreen" or "izzygreen (OnlyFans)").
- **Remembers what each site has:** performer and studio answers are kept per site - a match for a
  month, a name match for two weeks, "not there" for three days (it was an hour, so most were looked
  up again on every visit) - and dropped as soon as you act on one here. Performers and Studios load
  from them in a couple of seconds and say how old they are; **Look up again** checks the site now
  (just the site picked above). The Studios list also drops studios that have been added since.
- **Submit straight away (FansDB, and StashDB with edit rights):** no draft to finish on the site.
  - The check window has **Submit to FansDB…** next to **Send draft**: it asks for the edit comment
    (the suggested one filled in), says how many tags are ticked, then submits the public edit. Only
    when the studio and every performer are on the site (a draft can still name a missing studio).
  - **Performers → Not on site → Ready to send** has **Submit…** as well as **Send draft**: the
    performer is submitted with their photo, aliases, links and details from Stash.
  - In the check window, a performer the site doesn't have gets **Submit…** too, next to **Send
    draft** - no trip to the site.
  - **Pending** drafts have **Submit…** when the draft's studio and performers are already picked on
    the site (the draft is used up and becomes the edit).
  - Either way it then shows in Pending as waiting for votes. A video already submitted and waiting
    (anyone's) is refused, so nothing is sent twice. ThePornDB takes drafts only.
- **ThePornDB: a studio it doesn't have no longer blocks a scene.** Its drafts can name the studio
  (ThePornDB adds studios itself - its API has no way for anyone else to), so the draft sends it by
  name for ThePornDB's team to add when reviewing, with a warning to check the draft there. A scene
  with no studio at all is still flagged. 32 of the ready ThePornDB scenes had only this in the way.
- Fixed: links on ThePornDB showed "(null)" after them.
- **Performers, step by step** for each site, with photos: **1 Already on the site - link them**
  (found by the same link; **Link all**), **2 Is it them?** (your performer next to the site's
  look-alikes - photo, country, birth year, scene count, other names - with **That's them** or
  **None of these**, which is remembered), **3 Not on the site yet** (**Ready to send** a draft, or
  **Add info first** with what's missing) - each on its own tab with its count, opening on the first
  one with something in it (and remembering the one you were on).
- **What you send moves to Pending:** a scene, studio or performer you send (or link) leaves its
  section, with a note pointing to Pending - scenes you submitted on the site itself, or drafted with
  Stash's own button, too. **Pending** shows them as cards like the scenes: the picture from your
  Stash, what it is (scene draft, new studio, new scene...), and the time left - "20 h left - deleted
  Thu at 1:12 PM" for a draft, "Voting ends Mon, Nov 30 (in 4 days)" with the votes so far for an
  edit. A draft of something you've already submitted is marked as not needed.
- **Remembers what you've sent:** the list shows each sent scene after a reload (it used to forget
  until the next Check), as **Draft waiting** (time left), **Submitted - waiting for votes**, or
  no longer waiting (expired or voted on - with **send again…**). The check window says the same at
  the top, and now also finds submissions of the same video waiting for votes on the site (yours or
  anyone's) and stops a second one being sent.
- A page left open across an update now says "reload the page" instead of failing with an error.
- **Fewer requests:** which scenes each site already has is kept (a match for good, "not there" for
  14 days), so Check again only asks about new scenes. ThePornDB is asked one request at a time,
  spaced out; if it still says "too many requests" the plugin waits (5, 15, 30 s) and then stops
  for that run, and new ThePornDB lookups are limited to 25 a run (the rest on the next). Features
  a site doesn't have (ThePornDB: site list, drafts, edits) are remembered instead of asked again.
- **Studios to add page** (button on Ready to submit): the studios your ready scenes wait for on
  FansDB, the ones that unlock the most scenes first, each with **Create on FansDB…** (FansDB's
  "username (Network)" under the network, with the account link; a public edit, sent only when you
  confirm) - or, when the plugin can't tell the account, a note and **Add on FansDB**. Also
  **Missing in your Stash**: scenes linked to FansDB without a studio in Stash, with a button that
  creates it in your style ("izzygreen" under "OnlyFans") or links the one you have, and sets it.
- **Studios waiting for votes:** your pending studio edits on FansDB (made here or on the site) are
  shown on the Studios page with their votes and closing date, and their scenes on Ready to submit
  say "studio waiting for votes" instead of offering Submit (a scene needs its studio to exist).
- Studio names with a scraper's prefix ("FansDB: x (OnlyFans)") are matched without it.
- **Scenes whose studio is already on the site come first** (FansDB: the account the video was
  posted from). Each row says whether the studio is linked, on the site, a name match to check, or
  not there yet (it then has to be added first). With one site's tab picked, the list is in that
  site's order. "Check again" looks the studios up too, so the first one can take a few minutes;
  answers are remembered (a day, or an hour for "not there yet").
- **Checks are quicker:** the site lookups (studio, performers, tags, duplicates, links) run at the
  same time instead of one after another, and tag and site lists are remembered for a day - about
  3-5 seconds instead of 10-70 when FansDB is slow. The window counts the seconds, says when the site
  is answering slowly, and offers **Try again** if it fails or hangs.
- **Fixed:** ThePornDB checks failed for scenes whose performers have links ("page" not provided).
- **Modern UI light theme:** the check window follows it (light background, darker marks and links)
  instead of staying dark; dark is unchanged.
- **Ready to submit hides sites you can't submit to** (a read-only account) unless you tick it off or
  pick that site's tab, so scenes don't show "has everything" with nothing to press.
- **Ready to submit on a phone:** compact cards (picture beside the title, each site in its own box
  with a full-width Submit button), site tabs in one row, larger checkboxes, tidier review buttons.
- Scene pictures show on a phone or another computer (they pointed at the Mac's own address).
- The check window reminds you, above the send button, that the draft must be finished on the site
  within 24 hours.
- **Settings:**
  - **Only scenes not on any database:** starts the page with that filter on.
  - **Allow sending drafts in bulk:** up to 10 per click, for one site, only scenes the check passes
    with nothing to decide, and no tags. The report lists what was sent and what was skipped, and
    why.
- **ThePornDB** as a third site, with basic checks: its guidelines couldn't be read, and its API has
  fewer lookups.
