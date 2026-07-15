# DeReel

DeReel is a local-first short-form video studio. It has two connected surfaces:

- **Scout** stores discovery references and imports media that is owned, licensed, public-domain, or otherwise cleared for production.
- **Scout** can search Pexels and Pixabay when `PEXELS_API_KEY` or `PIXABAY_API_KEY` is configured. Provider imports are constrained to approved media hosts and enter `pending_review`.
- **Automations** can watch a local folder, copy new videos into the library, deduplicate them, pause for rights review, and resume the same batch after approval.
- **Forge** plans a themed edit, normalizes clips with FFmpeg, mixes optional cleared music, SFX, and voiceover tracks, adds title treatment, creates platform-shaped exports, and writes a manifest to `outputs/`.
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
2. Use Scout's library search and sort controls to narrow the board, open an HTTP(S) source directly when you need to revisit it, and inspect each source record to see its origin, source URL, local copy, automation ID, fingerprint, and review flags. Update descriptive metadata if needed, then review media individually or select a visible batch before marking production media as owned or licensed.
3. Create a Forge project, choose an optional looping music bed plus a sound effect or voiceover with volume/start-time controls, choose whether to request title treatment and a captions sidecar, and plan the edit automatically or add clips manually. Reopen the project inspector later to edit its brief, treatments, or audio mix. Delete a project only when no render or pending publish job is active; its generated package is removed while shared source assets remain in Scout.
4. Adjust clip order, trim points, enabled state, membership, or project settings in the Forge inspector, then render the project. Any timeline or settings change invalidates the previous output pointer and manifest, so an old package cannot be mistaken for the current edit. Publishing is gated on a freshly `ready` project. DeReel validates the master and each platform copy as readable 1080x1920 video before marking the project ready. Older output packages can be validated in place from Outputs. The output folder contains `final.mp4`, platform copies, `render-manifest.json`, and `credits.json`; both JSON records carry source provenance, while `credits.json.assets` remains the stable asset-ID list.
5. Check Publisher's delivery-readiness rows, then prepare publishing packages from Outputs. Each outbox payload keeps the credited asset IDs plus the output manifest and credits paths. The rows report which platform adapters have detected their environment token; unscheduled jobs remain approval-first in the outbox, while explicit schedules are checked by the local scheduler. Channel labels and Instagram public-media settings can be edited or removed, while pending jobs prevent accidental account deletion. Ready or failed jobs can be rescheduled, or cancelled without deleting their attempt history. A publish row is atomically claimed before any external upload, preventing duplicate local attempts from double-clicks or scheduler races, and the export is revalidated at attempt time before any platform request. Expand an outbox row's history to inspect numbered manual or scheduled attempts, their timestamps, remote IDs, and failure reasons; interrupted attempts are marked failed during startup recovery.

## Automations

Create a recipe in the Automations view as a watch-folder, provider-search, or combined source. Provider recipes use the configured Pexels/Pixabay API keys and import only approved provider-host media. Each run copies only new video files into `data/library/originals/`, records the recipe ID and source path, and rejects duplicate fingerprints. Set the rights default to `Needs review` for a review-first flow, or explicitly choose `Owned folder` / `Licensed folder` when the folder is already cleared. A pending batch is planned but not rendered; after approval, running the recipe again resumes that batch rather than creating another project. Recipes can also attach cleared music, a sound effect, and a voiceover with independent volume/start-time settings.

An interval of `0` means manual runs only. A positive interval enables the local scheduler. Destination selection creates approval-ready outbox jobs after rendering; a publish delay schedules them for the local publisher worker. Automation jobs retain their recipe ID in the activity feed and database, and each worker execution stores a durable run report with scan counts, result status, project linkage, and errors. If the local process restarts during a render or automation run, DeReel marks the interrupted job, linked recipe, and active run report failed with a retryable error instead of leaving stale `running` records.

Failed render and automation jobs can be retried from Overview. Retries are stored as new job attempts linked to the failed attempt; only the latest failed attempt is eligible, and a ready project or active duplicate is rejected.

Overview's preflight panel reports whether FFmpeg, FFprobe, optional text overlay support, and the Pexels/Pixabay provider keys are available before you start a run.

## Important boundary

A source URL is not treated as permission to download or publish a third-party social video. Scout references remain references until an approved local file is imported into the production library.

## Development checks

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile dereel/*.py
node --check web/app.js
```
