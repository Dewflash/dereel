from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


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


def normalize_segment(source: Path, destination: Path, in_point: float, duration: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-t",
        f"{max(duration, 0.1):.3f}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
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
        "-shortest",
        "-movflags",
        "+faststart",
        str(destination),
    ]
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


def add_text_overlay(source: Path, destination: Path, title: str, subtitle: str = "") -> bool:
    if "drawtext" not in available_filters():
        # Some system FFmpeg builds omit libfreetype/drawtext. The edit is still valid;
        # callers receive a false result and can record that title treatment was skipped.
        import shutil

        shutil.copy2(source, destination)
        return False
    title_file = destination.with_suffix(".title.txt")
    subtitle_file = destination.with_suffix(".subtitle.txt")
    title_file.write_text(title.strip()[:80])
    subtitle_file.write_text(subtitle.strip()[:140])
    # drawtext uses text files to avoid shell escaping user-provided copy.
    filters = [
        f"drawtext=textfile='{quote_concat_path(title_file)}':fontcolor=white:fontsize=64:line_spacing=8:box=1:boxcolor=black@0.56:boxborderw=22:x=(w-text_w)/2:y=120",
    ]
    if subtitle.strip():
        filters.append(
            f"drawtext=textfile='{quote_concat_path(subtitle_file)}':fontcolor=white:fontsize=40:line_spacing=6:box=1:boxcolor=black@0.52:boxborderw=16:x=(w-text_w)/2:y=h-300"
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
