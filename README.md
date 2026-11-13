# Stash Plugins

Plugins for [Stash](https://stashapp.cc):

| Plugin | What it does | Changes |
| --- | --- | --- |
| [Modern UI](plugins/ModernUI) | A cleaner, app-like look for Stash, with a phone-friendly layout, light and dark themes, picture-first cards (tags included), quick filters for your saved filters and an A–Z tag index. | [Changelog](plugins/ModernUI/CHANGELOG.md) |
| [Stash Suggestions](plugins/StashSuggestions) | "Suggested", "Similar photos", "More with…" and "Similar Performers" rows at the bottom of scene, image and performer pages. Works with any theme. | [Changelog](plugins/StashSuggestions/CHANGELOG.md) |
| [Better Organizing Tools](plugins/BetterTaggingTools) | Tags performers, studios and galleries with their most used tags and suggests tags and titles while you edit. Also bulk tagging on performer and gallery pages, gallery-name tags, pictures for tags from your own library, consistent tag name capitalisation (`dating app` → `Dating App`), auto tag, and dates, titles and studios from file names. No AI. | [Changelog](plugins/BetterTaggingTools/CHANGELOG.md) |
| [Better Organizing AI Add-on (local AI)](plugins/BetterTaggingAI) | Local AI for Stash, so nothing leaves your computer: tags from your own tag list (checked against look-alikes of what you've already tagged), body tags, titles, studios from the accounts in watermarks, performers recognised by face, and tidying up studios and galleries. A Review page to accept or undo suggestions with a swipe, "Ask AI" buttons while editing, and tasks (the slow AI steps can wait until night). Runs on Ollama and a small local helper, on a Mac or any ordinary computer (no NVIDIA graphics card needed). An add-on for Better Organizing Tools that also works alone. Needs some setup: see [its README](plugins/BetterTaggingAI#setup). | [Changelog](plugins/BetterTaggingAI/CHANGELOG.md) |
| [Library Cleanup](plugins/LibraryCleanup) | Finds and deletes tags, performers and studios that nothing is connected to, with a preview first. | [Changelog](plugins/LibraryCleanup/CHANGELOG.md) |
| [Vertical Cover Stitcher](plugins/VerticalStitcher) | Turns portrait (vertical) videos' covers into proper 16:9 covers by stitching several frames side by side. | [Changelog](plugins/VerticalStitcher/CHANGELOG.md) |
| [Privacy Blur](plugins/PrivacyBlur) | One switch in its settings (or Shift+Alt+B) blurs every picture and video in Stash, and optionally names and titles. Good for screenshots or browsing in company. | [Changelog](plugins/PrivacyBlur/CHANGELOG.md) |

![Modern UI's home page in the dark theme, with Privacy Blur on](docs/screenshots/Dark_Mode_Home.png)

*Modern UI's home page on a computer, with Privacy Blur turned on.*

## Installing

1. In Stash, go to **Settings → Plugins → Available Plugins** and click **Add Source**.
2. Enter:
   - **Name:** anything you like, e.g. `My Plugins`
   - **Source URL:** `https://OpenSourceLuffy.github.io/stash-plugins/main/index.yml`
3. Open the new source, tick the plugins you want and click **Install**.
4. Reload the page (or click **Reload plugins** under Settings → Plugins).

Updates show up in the same place whenever a new version is published. Each plugin's changelog
(linked above) lists what changed.

The **Better Organizing AI Add-on** also needs a local AI model and a small helper program installed
on your computer; its README walks through the
[setup](plugins/BetterTaggingAI#setup) and explains
[which models it uses and why](plugins/BetterTaggingAI#which-models-and-why).

## For maintainers

Plugins live in [`plugins/`](plugins), one folder per plugin. The plugin's ID is the name of its `.yml` file.

Every push to `main` that changes something under `plugins/` runs the
[deploy workflow](.github/workflows/deploy.yml), which runs
[`build_site.sh`](build_site.sh) to zip each plugin, write `index.yml` and publish
it to GitHub Pages. One-time setup: **Settings → Pages → Build and deployment →
Source: GitHub Actions**.

To release an update, change the plugin's files, bump `version:` in its `.yml`,
add an entry at the top of its `CHANGELOG.md`, and push. Better Organizing Tools and the AI add-on
ship the same `btt_core.py`: keep the two copies identical, and bump both plugins when it changes.

To check the build locally:

```sh
./build_site.sh _site/main
cat _site/main/index.yml
```

## License

The plugins are released under the [MIT License](LICENSE).
`build_site.sh` comes from Stash's
[plugin repository template](https://github.com/stashapp/plugins-repo-template)
and stays under its original AGPL-3.0 license.
