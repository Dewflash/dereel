# DeReel

DeReel is a local-first short-form video studio. It has two connected surfaces:

- **Scout** stores discovery references and imports media that is owned, licensed, public-domain, or otherwise cleared for production.
- **Forge** plans a themed edit, normalizes clips with FFmpeg, adds title treatment, creates platform-shaped exports, and writes a manifest to `outputs/`.
- **Publisher** creates an outbox package for YouTube Shorts, Instagram Reels, and TikTok. Account OAuth integrations are intentionally represented as explicit adapters rather than browser login automation.

## Run locally

```bash
cd /Users/kevinyongcj/programming/dereel
python3 -m dereel
```

Open <http://127.0.0.1:8765>. FFmpeg and ffprobe must be available on `PATH`.

The app uses only Python's standard library for its first local runtime. SQLite lives in `data/dereel.sqlite3`, imported originals live in `data/library/originals/`, and rendered packages live in `outputs/`.

## Workflow

1. Save online items as Scout references or import cleared local media.
2. Mark production media as owned or licensed after review.
3. Create a Forge project and plan the edit automatically, or add clips manually.
4. Render the project. The output folder contains `final.mp4`, platform copies, `render-manifest.json`, and `credits.json`.
5. Prepare publishing packages from Outputs. Connectors can later replace the outbox step without changing the asset or project model.

## Important boundary

A source URL is not treated as permission to download or publish a third-party social video. Scout references remain references until an approved local file is imported into the production library.

## Development checks

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile dereel/*.py
node --check web/app.js
```
