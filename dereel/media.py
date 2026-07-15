from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
TEXT_STYLES = {"kinetic", "clean", "narrated"}

TEXT_STYLE_PRESETS = {
    "kinetic": {"title_size": 64, "title_spacing": 8, "title_border": 22, "title_alpha": "0.56", "title_x": "(w-text_w)/2", "title_y": "120", "subtitle_size": 40, "subtitle_spacing": 6, "subtitle_border": 16, "subtitle_alpha": "0.52", "subtitle_x": "(w-text_w)/2", "subtitle_y": "h-300"},
    "clean": {"title_size": 50, "title_spacing": 4, "title_border": 12, "title_alpha": "0.42", "title_x": "80", "title_y": "140", "subtitle_size": 34, "subtitle_spacing": 4, "subtitle_border": 10, "subtitle_alpha": "0.36", "subtitle_x": "80", "subtitle_y": "h-240"},
    "narrated": {"title_size": 44, "title_spacing": 4, "title_border": 14, "title_alpha": "0.48", "title_x": "(w-text_w)/2", "title_y": "100", "subtitle_size": 48, "subtitle_spacing": 8, "subtitle_border": 18, "subtitle_alpha": "0.58", "subtitle_x": "(w-text_w)/2", "subtitle_y": "h-260"},
}


def normalize_text_style(style: str | None) -> str:
    value = str(style or "kinetic").strip().lower()
    return value if value in TEXT_STYLES else "kinetic"


def safe_extension(filename: str, mime_type: str | None = None) -> str:
    extension = Path(filename).suffix.lower()
    if extension in MEDIA_EXTENSIONS:
        return extension
    guessed = mimetypes.guess_extension(mime_type or "") or ".mp4"
    return guessed if guessed in MEDIA_EXTENSIONS else ".mp4"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name:stream=index,codec_type,width,height,codec_name,avg_frame_rate",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe could not inspect the media")
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    return {
        "duration": float(payload.get("format", {}).get("duration") or 0),
        "file_size": int(payload.get("format", {}).get("size") or path.stat().st_size),
        "format": payload.get("format", {}).get("format_name", ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
        "video_codec": video.get("codec_name", ""),
        "frame_rate": video.get("avg_frame_rate", ""),
    }


def validate_video(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("Media file does not exist")
    if path.stat().st_size <= 0:
        raise ValueError("Media file is empty")
    metadata = probe(path)
    if metadata["duration"] <= 0 or metadata["width"] <= 0 or metadata["height"] <= 0:
        raise ValueError("Media file has no usable video stream")
    return metadata


def validate_audio(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("Audio file does not exist")
    if path.stat().st_size <= 0:
        raise ValueError("Audio file is empty")
    metadata = probe(path)
    if metadata["duration"] <= 0 or not metadata["has_audio"]:
        raise ValueError("Audio file has no usable audio stream")
    return metadata


@lru_cache(maxsize=1)
def available_filters() -> set[str]:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        return set()
    filters: set[str] = set()
    for line in result.stdout.splitlines():
        match = line.strip().split()
        if len(match) >= 2 and match[0].startswith("T"):
            filters.add(match[1])
    return filters


def quote_concat_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "'\\''")


def _font_path(bold: bool) -> Path | None:
    configured = str(os.getenv("DEREEL_FONT_PATH") or "").strip()
    candidates = [Path(configured)] if configured else []
    if bold:
        candidates.extend(
            [
                Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                Path("/Library/Fonts/Arial Bold.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                Path("/usr/local/share/fonts/DejaVuSans-Bold.ttf"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                Path("/Library/Fonts/Arial.ttf"),
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/local/share/fonts/DejaVuSans.ttf"),
            ]
        )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _load_overlay_font(size: int, bold: bool) -> Any | None:
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    path = _font_path(bold)
    if not path:
        return None
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return None


def pillow_text_overlay_available() -> bool:
    return bool(_load_overlay_font(16, True) and _load_overlay_font(16, False))


def text_overlay_backend() -> str:
    filters = available_filters()
    if "drawtext" in filters:
        return "drawtext"
    if "overlay" in filters and pillow_text_overlay_available():
        return "pillow"
    return "unavailable"


def _wrap_overlay_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    words = " ".join(text.split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(word, font=font) <= max_width and (not current or draw.textlength(candidate, font=font) <= max_width):
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        fragment = ""
        for character in word:
            candidate = fragment + character
            if fragment and draw.textlength(candidate, font=font) > max_width:
                lines.append(fragment)
                fragment = character
            else:
                fragment = candidate
        current = fragment
    if current:
        lines.append(current)
    return "\n".join(lines)


def _pillow_text_overlay(source: Path, destination: Path, title: str, subtitle: str, style: str) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    metadata = probe(source)
    width = max(int(metadata["width"]), 1)
    height = max(int(metadata["height"]), 1)
    preset = TEXT_STYLE_PRESETS[normalize_text_style(style)]
    title_font = _load_overlay_font(int(preset["title_size"]), True)
    subtitle_font = _load_overlay_font(int(preset["subtitle_size"]), False)
    if not title_font or not subtitle_font:
        return False
    overlay_path = destination.with_suffix(".overlay.png")
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        def draw_copy(text: str, font: Any, spacing: int, padding: int, alpha: str, x_value: str, y_value: str) -> None:
            normalized = text.strip()
            if not normalized:
                return
            wrapped = _wrap_overlay_text(draw, normalized[:140], font, max(width - 160, 240))
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            box_width = text_width + (padding * 2)
            box_height = text_height + (padding * 2)
            x = (width - box_width) // 2 if x_value == "(w-text_w)/2" else int(x_value)
            y = height - int(y_value[2:]) if y_value.startswith("h-") else int(y_value)
            x = max(0, min(x, width - box_width))
            y = max(0, min(y, height - box_height))
            color = (0, 0, 0, max(0, min(int(float(alpha) * 255), 255)))
            draw.rounded_rectangle((x, y, x + box_width, y + box_height), radius=min(18, padding), fill=color)
            draw.multiline_text((x + padding - bbox[0], y + padding - bbox[1]), wrapped, font=font, fill="white", spacing=spacing)

        draw_copy(title, title_font, int(preset["title_spacing"]), int(preset["title_border"]), preset["title_alpha"], preset["title_x"], preset["title_y"])
        draw_copy(subtitle, subtitle_font, int(preset["subtitle_spacing"]), int(preset["subtitle_border"]), preset["subtitle_alpha"], preset["subtitle_x"], preset["subtitle_y"])
        canvas.save(overlay_path)
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-loop",
            "1",
            "-i",
            str(overlay_path),
            "-filter_complex",
            "[0:v][1:v]overlay=0:0:format=auto:shortest=1[v]",
            "-map",
            "[v]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "21",
            "-c:a",
            "copy",
            "-shortest",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "FFmpeg could not add the Pillow text overlay")
        return True
    finally:
        overlay_path.unlink(missing_ok=True)


def normalize_segment(source: Path, destination: Path, in_point: float, duration: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_has_audio = bool(probe(source)["has_audio"])
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(in_point, 0):.3f}",
        "-i",
        str(source),
    ]
    if source_has_audio:
        audio_map = "0:a:0"
    else:
        command.extend([
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
        ])
        audio_map = "1:a:0"
    command.extend([
        "-t",
        f"{max(duration, 0.1):.3f}",
        "-map",
        "0:v:0",
        "-map",
        audio_map,
    ])
    if source_has_audio:
        # Preserve clip dialogue/ambience and fill a short source track to the clip window.
        command.extend(["-af", "apad"])
    command.extend([
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(destination),
    ])
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFmpeg could not normalize the segment")


def concat_segments(segments: list[Path], destination: Path) -> None:
    concat_file = destination.with_suffix(".concat.txt")
    concat_file.write_text("\n".join(f"file '{quote_concat_path(path)}'" for path in segments) + "\n")
    try:
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "FFmpeg could not concatenate segments")
    finally:
        concat_file.unlink(missing_ok=True)


def add_text_overlay(source: Path, destination: Path, title: str, subtitle: str = "", style: str = "kinetic") -> bool:
    backend = text_overlay_backend()
    if backend == "unavailable":
        # Some system FFmpeg builds omit text filters. The edit is still valid;
        # callers receive a false result and can record that title treatment was skipped.
        shutil.copy2(source, destination)
        return False
    if backend == "pillow":
        return _pillow_text_overlay(source, destination, title, subtitle, style)
    title_file = destination.with_suffix(".title.txt")
    subtitle_file = destination.with_suffix(".subtitle.txt")
    title_file.write_text(title.strip()[:80])
    subtitle_file.write_text(subtitle.strip()[:140])
    preset = TEXT_STYLE_PRESETS[normalize_text_style(style)]
    # drawtext uses text files to avoid shell escaping user-provided copy.
    filters = [
        f"drawtext=textfile='{quote_concat_path(title_file)}':fontcolor=white:fontsize={preset['title_size']}:line_spacing={preset['title_spacing']}:box=1:boxcolor=black@{preset['title_alpha']}:boxborderw={preset['title_border']}:x={preset['title_x']}:y={preset['title_y']}",
    ]
    if subtitle.strip():
        filters.append(
            f"drawtext=textfile='{quote_concat_path(subtitle_file)}':fontcolor=white:fontsize={preset['subtitle_size']}:line_spacing={preset['subtitle_spacing']}:box=1:boxcolor=black@{preset['subtitle_alpha']}:boxborderw={preset['subtitle_border']}:x={preset['subtitle_x']}:y={preset['subtitle_y']}"
        )
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "21",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "FFmpeg could not add the text overlay")
        return True
    finally:
        title_file.unlink(missing_ok=True)
        subtitle_file.unlink(missing_ok=True)


def mix_audio_tracks(source: Path, tracks: list[dict[str, Any]], destination: Path) -> None:
    if not tracks:
        raise ValueError("At least one audio track is required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    inputs = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source)]
    filters: list[str] = []
    labels = ["[0:a]"]
    for index, track in enumerate(tracks, start=1):
        role = str(track.get("role") or "sfx")
        if role == "music":
            inputs.extend(["-stream_loop", "-1"])
        inputs.extend(["-i", str(track["source"])])
        level = max(0.0, min(float(track.get("volume", 0.16)), 1.0))
        delay = max(0, min(int(round(float(track.get("start_time", 0)) * 1000)), 3_600_000))
        filter_spec = f"[{index}:a]volume={level:.3f}"
        if delay:
            filter_spec += f",adelay={delay}|{delay}"
        label = f"track{index}"
        filters.append(f"{filter_spec}[{label}]")
        labels.append(f"[{label}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=2[a]")
    command = inputs + [
        "-filter_complex", ";".join(filters),
        "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "FFmpeg could not mix the audio tracks")


def mix_audio_bed(source: Path, audio: Path, destination: Path, volume: float = 0.16) -> None:
    mix_audio_tracks(source, [{"source": audio, "role": "music", "volume": volume, "start_time": 0}], destination)


def write_caption_track(destination: Path, text: str, duration: float) -> bool:
    cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())[:500]
    if not cleaned:
        destination.write_text("")
        return False
    end = max(0.1, float(duration))
    whole_seconds = int(end)
    milliseconds = int(round((end - whole_seconds) * 1000))
    if milliseconds >= 1000:
        whole_seconds += 1
        milliseconds = 0
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
    destination.write_text(f"1\n00:00:00,000 --> {timestamp}\n{cleaned}\n")
    return True
