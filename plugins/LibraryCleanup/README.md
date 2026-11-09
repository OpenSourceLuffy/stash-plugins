# Library Cleanup

Finds tags, performers and studios that nothing is connected to, and deletes them when you ask it to.

## Using it

Under **Settings → Tasks → Plugin Tasks → Library Cleanup**:

1. **Preview unused tags, performers and studios**: lists everything that would be deleted in the log. Nothing is changed.
2. Make a backup (**Settings → Tasks → Backup**). Deleting can't be undone any other way.
3. **Delete unused tags**, **Delete unused performers** and/or **Delete unused studios**.

## What counts as unused

- **Tags**: not on any scene, image, gallery, scene marker, performer, studio or group.
- **Performers**: no scenes, images, galleries or groups.
- **Studios**: no scenes, images, galleries or groups. A parent studio (a network with studios under it) is kept while any of its child studios is in use; if they're all unused, it's deleted with them.

**Always kept:** favourites, tags that are the parent of other tags, parent studios with a child studio in use, the tags listed in
**Tags to always keep**, and the parent tag
[Better Tagging Tools](../BetterTaggingTools) files its gallery-name tags under.

## Settings

| Setting | What it does |
| --- | --- |
| Tags to always keep | Comma-separated tag names never deleted, e.g. tags you're about to use or that your saved filters rely on. |

## Notes

- Python 3 (standard library only). Tested with Stash v0.31.
- Performers with no content are often ones a scraper or StashDB created. Check the preview list, and mark any you want to keep as a favourite.
- Face-recognition plugins that learned from a performer's picture will lose that performer once it's deleted.
