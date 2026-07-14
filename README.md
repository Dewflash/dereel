# DeReel

DeReel is a local-first short-form video studio. It has two connected surfaces:

- **Scout** stores discovery references and imports media that is owned, licensed, public-domain, or otherwise cleared for production.
- **Scout** can search Pexels and Pixabay when `PEXELS_API_KEY` or `PIXABAY_API_KEY` is configured. Provider imports are constrained to approved media hosts and enter `pending_review`.
- **Automations** can watch a local folder, copy new videos into the library, deduplicate them, pause for rights review, and resume the same batch after approval.
- **Forge** plans a themed edit, normalizes clips with FFmpeg, mixes an optional cleared music bed, adds title treatment, creates platform-shaped exports, and writes a manifest to `outputs/`.
- **Publisher** creates an outbox package for YouTube Shorts, Instagram Reels, and TikTok. Account OAuth integrations are intentionally represented as explicit adapters rather than browser login automation.

## Run locally

```bash
cd /Users/kevinyongcj/programming/dereel
python3 -m dereel
```

Open <http://127.0.0.1:8765>. FFmpeg and ffprobe must be available on `PATH`.

The app uses only Python's standard library for its first local runtime. SQLite lives in `data/dereel.sqlite3`, imported originals live in `data/library/originals/`, and rendered packages live in `outputs/`.

Optional provider search uses the official provider APIs. Set a key in the shell before starting DeReel:

```bash
export PEXELS_API_KEY="..."
export PIXABAY_API_KEY="..."
```

Publishing adapters read tokens from the environment and never write them into SQLite:

```bash
export DEREEL_YOUTUBE_ACCESS_TOKEN="..."
export DEREEL_TIKTOK_ACCESS_TOKEN="..."
export DEREEL_INSTAGRAM_ACCESS_TOKEN="..."
```

The YouTube adapter uses the video upload API and defaults to private visibility. TikTok uses the supported upload-to-inbox draft path when configured, so the creator still completes the final TikTok editing/posting action. Instagram uses the professional-account container flow when its channel has an Instagram user ID and HTTPS public media base URL configured in Publisher. Override the Graph API version with `DEREEL_INSTAGRAM_GRAPH_VERSION` when needed.

Instagram's public base URL must serve the local app's output route, for example `https://media.example.com/api/projects/<project_id>/output/instagram.mp4`. DeReel does not expose local files to the internet by itself; a deliberately configured reverse proxy, tunnel, or storage host is required.

## Workflow

1. Save online items as Scout references or import cleared local media.
2. Mark production media as owned or licensed after review.
3. Create a Forge project and plan the edit automatically, or add clips manually.
4. Adjust clip trim points or disable individual clips in the Forge inspector, then render the project. The output folder contains `final.mp4`, platform copies, `render-manifest.json`, and `credits.json`.
5. Prepare publishing packages from Outputs. Explicitly scheduled jobs are checked by the local scheduler; unscheduled jobs remain approval-first in the outbox.

## Automations

Create a recipe in the Automations view with an existing watch folder. Each run copies only new video files into `data/library/originals/`, records the recipe ID and source path, and rejects duplicate fingerprints. Set the rights default to `Needs review` for a review-first flow, or explicitly choose `Owned folder` / `Licensed folder` when the folder is already cleared. A pending batch is planned but not rendered; after approval, running the recipe again resumes that batch rather than creating another project.

An interval of `0` means manual runs only. A positive interval enables the local scheduler. Destination selection creates approval-ready outbox jobs after rendering; a publish delay schedules them for the local publisher worker.

## Important boundary

A source URL is not treated as permission to download or publish a third-party social video. Scout references remain references until an approved local file is imported into the production library.

## Development checks

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile dereel/*.py
node --check web/app.js
```
