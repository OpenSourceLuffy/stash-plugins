"""Better Tagging Tools - tagging helpers without AI (see btt_core.py).

The work is in btt_core.py, which Better Tagging AI ships too; this script
only picks what to run.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btt_core as c  # noqa: E402


def main():
    payload = json.loads(sys.stdin.read())
    conn = payload.get("server_connection", {})
    stash = c.Stash(conn)
    args = payload.get("args", {})
    plugin_dir = conn.get("PluginDir") or os.path.dirname(os.path.abspath(__file__))
    c.PLUGIN_ID = c.BASE_ID
    c._plugin_dir = plugin_dir
    mode = args.get("mode")
    try:
        hook = args.get("hookContext")
        if hook:
            c.run_hook(stash, plugin_dir, hook)
        elif mode == "queued":
            c.run_queued(stash, plugin_dir)
        elif mode == "bulktag":
            c.run_bulktag(stash, args)
        elif mode == "gallerynames":
            c.run_gallery_all(stash, plugin_dir)
        elif mode in ("gallerytags", "gallerytags_preview"):
            ids = [i for i in str(args.get("gallery_ids") or "").split(",") if i]
            c.run_gallery_tags_to_images(stash, plugin_dir, preview=mode == "gallerytags_preview", gallery_ids=ids or None)
        elif mode == "tagpictures":
            c.run_tag_pictures(stash, plugin_dir)
        elif mode == "uniquepictures":
            c.run_unique_pictures(stash, plugin_dir)
        elif mode == "newpictures":
            c.run_new_pictures(stash, plugin_dir)
        elif mode == "newpicture":
            print(json.dumps({"output": c.run_new_picture(stash, plugin_dir, args["tag_id"])}))
        elif mode == "frame":
            print(json.dumps({"output": c.run_frame(stash, args)}))
        elif mode == "titlecase":
            c.run_title_case(stash, dry_run=False)
        elif mode == "titlecase_preview":
            c.run_title_case(stash, dry_run=True)
        elif mode == "handle_studios_text":
            c.run_handle_studios(stash, plugin_dir, use_ai=False)
        elif mode == "title_ideas":
            print(json.dumps({"output": c.run_title_ideas(stash, plugin_dir, args["kind"], args["id"], False)}))
        elif mode in ("retitles", "retitles_preview"):
            c.run_redo_titles(stash, preview=mode == "retitles_preview")
        elif mode in ("titles", "titles_preview"):
            c.run_titles(stash, preview=mode == "titles_preview")
        elif mode in ("dates", "dates_preview"):
            c.run_dates(stash, preview=mode == "dates_preview")
        elif mode == "autotag":
            ids = [i for i in str(args.get("ids") or "").split(",") if i]
            result = c.run_autotag_items(stash, plugin_dir, args["kind"], ids, bool(args.get("with_images")))
            print(json.dumps({"output": result}))
        elif mode == "gallerytagids":
            # Asked by the page (runPluginOperation): the plugin's
            # gallery-name tags, so suggestions can leave them out
            print(json.dumps({"output": sorted(c.gallery_name_tag_ids(plugin_dir), key=int)}))
        elif mode in ("apply_studios", "apply_galleries"):
            c.run_all(stash, plugin_dir, dry_run=False, owners=(mode.split("_")[1],))
        elif mode == "dryrun_all":
            c.run_all(stash, plugin_dir, dry_run=True, owners=("performers", "studios", "galleries"))
        elif mode in (None, "apply", "dryrun"):
            c.run_all(stash, plugin_dir, dry_run=mode == "dryrun")
        else:
            raise RuntimeError(f'unknown task "{mode}" (AI tasks are in the Better Tagging AI plugin)')
    except Exception as e:
        c.log("error", f"Better Tagging Tools failed: {e}")
        raise


if __name__ == "__main__":
    main()
