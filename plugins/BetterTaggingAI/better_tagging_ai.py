"""Better Organizing AI - the tagging helpers that ask a local AI model
(Ollama) or the face helper: AI tags (checked against look-alikes you've
tagged), AI titles, studios and performers from watermarks, faces, and all
of it for new items after a scan. The work is in btt_core.py, which
Better Organizing Tools ships too; this script only picks what to run.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btt_core as c  # noqa: E402

# what's been done, kept by Better Organizing Tools before the AI had a plugin
# of its own: copied over once, so finished work isn't done again
CARRY_OVER = ("ai_done.json", "handle_scan.json")


def carry_over(plugin_dir):
    old = os.path.join(os.path.dirname(os.path.abspath(plugin_dir)), c.BASE_ID)
    for name in CARRY_OVER:
        mine, theirs = os.path.join(plugin_dir, name), os.path.join(old, name)
        if not os.path.exists(mine) and os.path.exists(theirs):
            try:
                shutil.copyfile(theirs, mine)
            except OSError:
                pass


def main():
    if sys.argv[1:2] == ["--night-runner"]:  # the background process for tonight's run
        c.night_runner_main(sys.argv[2])
        return
    payload = json.loads(sys.stdin.read())
    conn = payload.get("server_connection", {})
    stash = c.Stash(conn)
    args = payload.get("args", {})
    plugin_dir = conn.get("PluginDir") or os.path.dirname(os.path.abspath(__file__))
    c.PLUGIN_ID = c.AI_ID
    c._plugin_dir = c.base_dir(plugin_dir)  # titles made from file names: Better Organizing Tools' record
    carry_over(plugin_dir)
    mode = args.get("mode")
    # keep the night runner going when a night option is on (e.g. "Body tags
    # every night" was just turned on, or Stash restarted)
    try:
        cfg = c.settings(stash, plugin_dir)
        if cfg["ai"]["night_body"] or cfg["ai"]["nightly"]:
            c.ensure_night_runner(stash, plugin_dir, cfg)
    except Exception:
        pass
    try:
        hook = args.get("hookContext")
        if hook:
            c.run_hook(stash, plugin_dir, hook)
        elif mode == "queued":
            c.run_queued(stash, plugin_dir)
        elif mode == "ai_untagged":
            c.run_ai(stash, plugin_dir, only_untagged=True)
        elif mode == "ai_all":
            c.run_ai(stash, plugin_dir)
        elif mode == "ai_preview":
            c.run_ai(stash, plugin_dir, preview=True)
        elif mode == "ai_suggest":
            print(json.dumps({"output": c.run_ai_suggest(stash, plugin_dir, args["kind"], args["id"])}))
        elif mode in ("ai_titles_scenes", "ai_titles_images"):
            c.run_ai_titles(stash, plugin_dir, [mode.rsplit("_", 1)[1][:-1]])
        elif mode == "ai_titles_preview":
            c.run_ai_titles(stash, plugin_dir, ["scene", "image"], preview=True, limit=10)
        elif mode == "handle_studios":
            c.run_handle_studios(stash, plugin_dir, use_ai=True)
        elif mode == "handle_studios_preview":
            c.run_handle_studios(stash, plugin_dir, preview=True, limit=10)
        elif mode == "photo_studios_preview":
            c.run_photo_studios(stash, plugin_dir, in_galleries=True, preview=True, limit=10)
        elif mode == "photo_studios":
            c.run_photo_studios(stash, plugin_dir)
        elif mode == "photo_studios_all":
            c.run_photo_studios(stash, plugin_dir, in_galleries=True)
        elif mode == "night_run":
            c.run_night(stash, plugin_dir)
        elif mode in ("typical_preview", "typical_missing", "typical_all"):
            c.run_typical_pictures(stash, plugin_dir, mode.split("_", 1)[1])
        elif mode == "typical_one":
            # Asked by the Tags list (runPluginOperation)
            print(json.dumps({"output": c.run_typical_picture(stash, plugin_dir, args["tag_id"])}))
        elif mode == "looks_scan":
            c.run_look_scan(stash, plugin_dir)
        elif mode == "looks_preview":
            c.run_looks(stash, plugin_dir, preview=True, limit=10)
        elif mode == "looks_untagged":
            c.run_looks(stash, plugin_dir)
        elif mode == "look_suggest":
            print(json.dumps({"output": c.run_look_suggest(stash, plugin_dir, args["kind"], args["id"])}))
        elif mode == "body_preview":
            c.run_body(stash, plugin_dir, preview=True, limit=10)
        elif mode == "body_all":
            c.run_body(stash, plugin_dir)
        elif mode == "body_redo":
            c.run_body(stash, plugin_dir, redo=True)
        elif mode == "faces_learn":
            c.update_face_index(stash, c.settings(stash, plugin_dir), plugin_dir)
        elif mode == "faces_preview":
            c.run_faces(stash, plugin_dir, ["scene", "image"], preview=True, limit=10)
        elif mode in ("faces_scenes", "faces_images"):
            c.run_faces(stash, plugin_dir, [mode.split("_")[1][:-1]])
        elif mode == "faces_scan":
            c.run_face_scan(stash, plugin_dir)
        elif mode == "faces_learn_tagged":
            c.run_face_learn_tagged(stash, plugin_dir)
        elif mode == "faces_duplicates":
            c.run_face_duplicates(stash, plugin_dir)
        elif mode == "look_similar":
            # Asked by Stash Suggestions (runPluginOperation)
            print(json.dumps({"output": c.run_look_similar(stash, plugin_dir, args["kind"], args["id"])}))
        elif mode == "face_similar":
            # Asked by Stash Suggestions (runPluginOperation)
            print(json.dumps({"output": c.run_face_similar(plugin_dir, args["id"])}))
        elif mode == "face_lookalikes":
            print(json.dumps({"output": c.run_face_lookalikes(stash, plugin_dir, args["id"])}))
        elif mode == "face_reject":
            print(json.dumps({"output": c.run_face_reject(plugin_dir, args["performer"], args.get("key"))}))
        elif mode == "face_review":
            print(json.dumps({"output": c.run_face_review(stash, plugin_dir, args.get("performer"),
                                                          int(args.get("limit") or 40), args.get("skip") or [])}))
        elif mode == "tag_review":
            print(json.dumps({"output": c.run_tag_review(stash, plugin_dir, args.get("tag"),
                                                         int(args.get("limit") or 40), args.get("skip") or [],
                                                         fresh=bool(args.get("fresh")))}))
        elif mode == "tag_review_answer":
            print(json.dumps({"output": c.run_tag_review_answer(plugin_dir, args["key"], args["tag"], bool(args.get("no", True)))}))
        elif mode == "new_performers":
            print(json.dumps({"output": c.run_new_performers(stash, plugin_dir, int(args.get("limit") or 30), args.get("skip") or [],
                                                             fresh=bool(args.get("fresh")))}))
        elif mode == "new_perf_read":
            print(json.dumps({"output": c.run_new_perf_read(stash, plugin_dir, args.get("keys") or [])}))
        elif mode == "check":
            print(json.dumps({"output": c.run_check(stash, plugin_dir, int(args.get("limit") or 40), args.get("skip") or [],
                                                    fresh=bool(args.get("fresh")), group=args.get("group"),
                                                    only=args.get("only"))}))
        elif mode == "check_keep_many":
            print(json.dumps({"output": c.run_check_keep_many(plugin_dir, args.get("pairs") or [], bool(args.get("keep", True)))}))
        elif mode == "check_answer":
            print(json.dumps({"output": c.run_check_answer(plugin_dir, args["key"], args["what"], bool(args.get("keep", True)))}))
        elif mode == "review_no_many":
            print(json.dumps({"output": c.run_review_no_many(plugin_dir, args.get("kind"), args.get("pairs") or [], bool(args.get("no", True)))}))
        elif mode == "new_perf_lookalikes":
            print(json.dumps({"output": c.run_new_perf_lookalikes(plugin_dir, args.get("faces") or [])}))
        elif mode == "new_perf_answer":
            print(json.dumps({"output": c.run_new_perf_answer(plugin_dir, args.get("keys") or [], bool(args.get("no", True)))}))
        elif mode == "studio_review":
            print(json.dumps({"output": c.run_studio_review(stash, plugin_dir, int(args.get("limit") or 30), args.get("skip") or [],
                                                             kinds=args.get("kinds"))}))
        elif mode == "studio_apply":
            print(json.dumps({"output": c.run_studio_apply(stash, plugin_dir, args.get("action"), args.get("data"))}))
        elif mode == "studio_undo":
            print(json.dumps({"output": c.run_studio_undo(stash, plugin_dir, args.get("undo") or {})}))
        elif mode == "gallery_review":
            print(json.dumps({"output": c.run_gallery_review(stash, plugin_dir, int(args.get("limit") or 30), args.get("skip") or [],
                                                             kinds=args.get("kinds"), fresh=bool(args.get("fresh")))}))
        elif mode == "gallery_apply":
            print(json.dumps({"output": c.run_gallery_apply(stash, plugin_dir, args.get("action"), args.get("data"))}))
        elif mode == "gallery_undo":
            print(json.dumps({"output": c.run_gallery_undo(stash, plugin_dir, args.get("undo") or {})}))
        elif mode == "face_unreject":
            print(json.dumps({"output": c.run_face_unreject(plugin_dir, args["performer"], args.get("key"))}))
        elif mode == "face_confirm":
            print(json.dumps({"output": c.run_face_confirm(plugin_dir, args["performer"], args.get("emb"))}))
        elif mode == "face_suggest":
            print(json.dumps({"output": c.run_face_suggest(stash, plugin_dir, args["kind"], args["id"])}))
        elif mode == "perf_details":
            c.run_performer_details(stash, plugin_dir)
        elif mode == "perf_details_preview":
            c.run_performer_details(stash, plugin_dir, preview=True, limit=10)
        elif mode == "perf_details_one":
            print(json.dumps({"output": c.run_performer_details_one(stash, plugin_dir, args["id"])}))
        elif mode == "title_ideas":
            print(json.dumps({"output": c.run_title_ideas(stash, plugin_dir, args["kind"], args["id"], True)}))
        else:
            raise RuntimeError(f'unknown task "{mode}"')
    except Exception as e:
        c.log("error", f"Better Organizing AI Add-on failed: {e}")
        raise


if __name__ == "__main__":
    main()
