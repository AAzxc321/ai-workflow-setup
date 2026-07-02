from __future__ import annotations

import csv
import html
import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "videos.csv"


@dataclass
class TranscriptResult:
    method: str
    language: str
    auto_generated: str
    text: str


def extract_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        video_id = parsed.path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/embed/", "/shorts/")):
            video_id = parsed.path.strip("/").split("/")[1]
        else:
            video_id = ""
    else:
        video_id = ""

    if not video_id:
        raise ValueError(f"Could not extract video ID from URL: {url}")
    return video_id


def safe_filename(title: str, max_length: int = 80) -> str:
    name = title.lower()
    name = re.sub(r"[^\w\s-]", "", name, flags=re.ASCII)
    name = re.sub(r"\s+", "-", name.strip())
    name = re.sub(r"-+", "-", name)
    return (name[:max_length].rstrip("-") or "untitled") + ".md"


def format_timestamp(seconds: float | int | None) -> str:
    total = int(float(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def normalize_api_items(items) -> list[dict]:
    normalized = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            normalized.append(
                {
                    "start": getattr(item, "start", 0),
                    "duration": getattr(item, "duration", 0),
                    "text": getattr(item, "text", ""),
                }
            )
    return normalized


def transcript_items_to_text(items: list[dict]) -> str:
    lines = []
    for item in items:
        text = html.unescape(str(item.get("text", ""))).replace("\n", " ").strip()
        if text:
            lines.append(f"[{format_timestamp(item.get('start'))}] {text}")
    return "\n".join(lines).strip()


def fetch_with_api(video_id: str) -> TranscriptResult:
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    else:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    transcript = None
    auto_generated = "unknown"

    try:
        transcript = transcript_list.find_manually_created_transcript(
            ["en", "en-US", "en-GB", "en-CA", "en-AU"]
        )
        auto_generated = "no"
    except Exception:
        transcript = transcript_list.find_generated_transcript(
            ["en", "en-US", "en-GB", "en-CA", "en-AU"]
        )
        auto_generated = "yes"

    fetched = transcript.fetch()
    text = transcript_items_to_text(normalize_api_items(fetched))
    if not text:
        raise RuntimeError("YouTube transcript API returned an empty transcript.")

    return TranscriptResult(
        method="YouTube transcript API",
        language="English",
        auto_generated=auto_generated,
        text=text,
    )


def parse_vtt_timestamp(value: str) -> float:
    match = re.match(
        r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})",
        value.strip(),
    )
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def parse_vtt(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    output = []
    current_time = None
    text_parts = []

    def flush():
        nonlocal current_time, text_parts
        if current_time is not None and text_parts:
            text = " ".join(text_parts)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                output.append(f"[{format_timestamp(current_time)}] {text}")
        current_time = None
        text_parts = []

    for raw in lines:
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE")):
            flush()
            continue
        if "-->" in line:
            flush()
            current_time = parse_vtt_timestamp(line.split("-->", 1)[0])
            continue
        if current_time is not None and not line.isdigit():
            text_parts.append(line)

    flush()
    return "\n".join(output).strip()


def parse_json3(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    lines = []
    for event in data.get("events", []):
        segments = event.get("segs") or []
        text = "".join(segment.get("utf8", "") for segment in segments)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        if text:
            lines.append(f"[{format_timestamp(event.get('tStartMs', 0) / 1000)}] {text}")
    return "\n".join(lines).strip()


def read_subtitle_file(path: Path) -> str:
    if path.suffix.lower() == ".json3":
        return parse_json3(path)
    return parse_vtt(path)


def fetch_with_ytdlp(video_id: str, url: str) -> TranscriptResult:
    from yt_dlp import YoutubeDL

    with tempfile.TemporaryDirectory(prefix="yt-subs-") as tmp:
        tmp_dir = Path(tmp)
        options = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en.*", "en"],
            "subtitlesformat": "vtt/json3/best",
            "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            ydl.download([url])

        candidates = sorted(
            [
                p
                for p in tmp_dir.iterdir()
                if p.is_file()
                and p.stem.startswith(video_id)
                and re.search(r"\.en(?:[-.A-Za-z]*)?\.(?:vtt|json3)$", p.name)
            ]
        )
        if not candidates:
            raise RuntimeError("yt-dlp did not find English subtitles or auto subtitles.")

        text = ""
        chosen = candidates[0]
        for candidate in candidates:
            parsed = read_subtitle_file(candidate)
            if parsed:
                text = parsed
                chosen = candidate
                break

        if not text:
            raise RuntimeError("yt-dlp subtitle file was empty.")

        subtitles = info.get("subtitles") or {}
        automatic = info.get("automatic_captions") or {}
        lang = chosen.name.split(f"{video_id}.", 1)[-1].rsplit(".", 1)[0]
        auto_generated = "yes" if lang in automatic and lang not in subtitles else "no"

        return TranscriptResult(
            method="yt-dlp",
            language="English",
            auto_generated=auto_generated,
            text=text,
        )


def metadata_block(row: dict, result: TranscriptResult) -> str:
    return "\n".join(
        [
            f"# {row['title']}",
            "",
            "## Metadata",
            "",
            f"* **Expert:** {row['expert_slug']}",
            f"* **URL:** {row['url']}",
            f"* **Date:** {row['date']}",
            f"* **Source type:** {row['source_type']}",
            f"* **Transcript method:** {result.method}",
            f"* **Transcript language:** {result.language}",
            f"* **Auto-generated captions:** {result.auto_generated}",
            f"* **Annotation:** {row['annotation']}",
            "",
        ]
    )


def available_markdown(row: dict, result: TranscriptResult) -> str:
    return metadata_block(row, result) + "## Transcript\n\n" + result.text.strip() + "\n"


def unavailable_markdown(row: dict) -> str:
    result = TranscriptResult(
        method="unavailable",
        language="unknown",
        auto_generated="unknown",
        text="",
    )
    return (
        metadata_block(row, result)
        + "## Transcript Status\n\n"
        + "Transcript unavailable through automated methods.\n"
    )


def write_markdown(row: dict, content: str) -> Path:
    folder = BASE_DIR / row["expert_slug"]
    folder.mkdir(parents=True, exist_ok=True)
    output_path = folder / safe_filename(row["title"])
    output_path.write_text(content, encoding="utf-8")
    return output_path


def fetch_transcript(video_id: str, url: str) -> tuple[TranscriptResult | None, list[str]]:
    errors = []
    try:
        return fetch_with_api(video_id), errors
    except Exception as exc:
        errors.append(f"YouTube transcript API: {exc}")

    try:
        return fetch_with_ytdlp(video_id, url), errors
    except Exception as exc:
        errors.append(f"yt-dlp: {exc}")

    return None, errors


def main() -> int:
    successes = []
    unavailable = []
    errors = []

    with CSV_PATH.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    for row in rows:
        title = row["title"]
        try:
            video_id = extract_video_id(row["url"])
            result, fetch_errors = fetch_transcript(video_id, row["url"])
            if result:
                output_path = write_markdown(row, available_markdown(row, result))
                successes.append((title, result.method, output_path))
                print(f"SUCCESS: {title} [{result.method}] -> {output_path.relative_to(BASE_DIR)}")
            else:
                output_path = write_markdown(row, unavailable_markdown(row))
                unavailable.append((title, output_path))
                errors.append((title, fetch_errors))
                print(f"UNAVAILABLE: {title} -> {output_path.relative_to(BASE_DIR)}")
        except Exception as exc:
            try:
                output_path = write_markdown(row, unavailable_markdown(row))
                unavailable.append((title, output_path))
                errors.append((title, [f"unexpected: {exc}"]))
                print(f"ERROR: {title} -> {output_path.relative_to(BASE_DIR)}")
            except Exception:
                errors.append((title, [f"unexpected: {exc}"]))
                print(f"ERROR: {title}")

    print("\nFinal report")
    print("============")
    print(f"Successful transcripts: {len(successes)}")
    for title, method, output_path in successes:
        print(f"- {title} ({method}) -> {output_path.relative_to(BASE_DIR)}")

    print(f"\nUnavailable transcripts: {len(unavailable)}")
    for title, output_path in unavailable:
        print(f"- {title} -> {output_path.relative_to(BASE_DIR)}")

    print(f"\nErrors: {len(errors)}")
    for title, messages in errors:
        print(f"- {title}")
        for message in messages:
            print(f"  - {message}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
