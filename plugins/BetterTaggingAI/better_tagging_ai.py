"""Better Tagging AI - the tagging helpers that ask a local AI model
(Ollama) or the face helper: AI tags (checked against look-alikes you've
tagged), AI titles, studios and performers from watermarks, faces, and all
of it for new items after a scan. The work is in btt_core.py, which
Better Tagging Tools ships too; this script only picks what to run.
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btt_core as c  # noqa: E402

# what's been done, kept by Better Tagging Tools before the AI had a plugin
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
    c._plugin_dir = c.base_dir(plugin_dir)  # titles made from file names: Better Tagging Tools' record
    carry_over(plugin_dir)
    mode = args.get("mode")
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
        elif mode == "night_run":
            c.run_night(stash, plugin_dir)
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
        elif mode == "face_lookalikes":
            print(json.dumps({"output": c.run_face_lookalikes(stash, plugin_dir, args["id"])}))
        elif mode == "face_reject":
            print(json.dumps({"output": c.run_face_reject(plugin_dir, args["performer"], args.get("key"))}))
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
        c.log("error", f"Better Tagging AI Add-on failed: {e}")
        raise


if __name__ == "__main__":
    main()
