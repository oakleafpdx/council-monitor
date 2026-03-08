#!/usr/bin/env python3
"""
Portland City Council Meeting Monitor
======================================
Fetches YouTube videos of Portland City Council meetings, transcribes them
via AssemblyAI (with speaker diarization), summarizes via Claude API, and
saves structured summaries to Google Drive.

Designed to run as a GitHub Actions workflow or locally.

Usage:
    python council_meeting_pipeline.py --url "https://www.youtube.com/watch?v=VIDEO_ID"
    python council_meeting_pipeline.py --latest
    python council_meeting_pipeline.py --check-new
"""

import os
import sys
import json
import argparse
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

YOUTUBE_CHANNEL_ID = os.environ.get(
    "YOUTUBE_CHANNEL_ID", "UCcPIUh7CWwtBXisMPHWG65g"
)

# Topics to flag in summaries
WATCH_TOPICS = [
    "Economic Development",
    "Real Estate & Housing",
    "Climate & Sustainability Policy",
    "Land Use & Zoning",
    "System Development Charges (SDCs) & Permitting Fees",
    "Infrastructure & Transportation",
    "Homelessness & Shelter Policy",
    "City Budget & Finance",
    "Public Safety",
]

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TRANSCRIPT_CHARS = 120_000

# Path to YouTube cookies file (set via env var for GitHub Actions)
COOKIES_PATH = os.environ.get("YOUTUBE_COOKIES_PATH", "")


def _cookies_args() -> list[str]:
    """Return yt-dlp cookies arguments if a cookies file is available."""
    if COOKIES_PATH and Path(COOKIES_PATH).exists():
        return ["--cookies", COOKIES_PATH]
    return []


# ---------------------------------------------------------------------------
# Step 1: YouTube Video Discovery & Audio Download
# ---------------------------------------------------------------------------

def fetch_latest_videos(channel_id: str, max_results: int = 5) -> list[dict]:
    """Use yt-dlp to list recent videos from a channel."""
    cmd = [
        "yt-dlp",
        *_cookies_args(),
        "--flat-playlist",
        "--playlist-end", str(max_results),
        "--print", "%(id)s|||%(title)s|||%(upload_date)s",
        f"https://www.youtube.com/channel/{channel_id}/videos",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"[ERROR] yt-dlp listing failed: {result.stderr}", file=sys.stderr)
        return []

    videos = []
    for line in result.stdout.strip().split("\n"):
        if "|||" not in line:
            continue
        vid_id, title, upload_date = line.split("|||", 2)
        videos.append({
            "id": vid_id.strip(),
            "title": title.strip(),
            "upload_date": upload_date.strip(),
            "url": f"https://www.youtube.com/watch?v={vid_id.strip()}",
        })
    return videos


def download_audio(youtube_url: str, output_dir: str) -> str:
    """Download audio from a YouTube video using yt-dlp."""
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
    cmd = [
        "yt-dlp",
        *_cookies_args(),
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "3",
        "-o", output_template,
        youtube_url,
    ]
    print(f"[INFO] Downloading audio from: {youtube_url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {result.stderr}")

    for f in os.listdir(output_dir):
        if f.endswith(".mp3"):
            return os.path.join(output_dir, f)
    raise FileNotFoundError("Audio file not found after yt-dlp download.")


def get_video_metadata(youtube_url: str) -> dict:
    """Fetch video title and upload date via yt-dlp."""
    cmd = [
        "yt-dlp",
        *_cookies_args(),
        "--print", "%(title)s|||%(upload_date)s|||%(duration)s|||%(id)s",
        "--no-download",
        youtube_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return {"title": "Unknown", "upload_date": "Unknown", "duration": 0, "id": "unknown"}

    parts = result.stdout.strip().split("|||")
    return {
        "title": parts[0] if len(parts) > 0 else "Unknown",
        "upload_date": parts[1] if len(parts) > 1 else "Unknown",
        "duration": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
        "id": parts[3] if len(parts) > 3 else "unknown",
    }


# ---------------------------------------------------------------------------
# Step 2: Transcription via AssemblyAI
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path: str, api_key: str) -> dict:
    """Transcribe audio using AssemblyAI with speaker diarization."""
    import assemblyai as aai

    aai.settings.api_key = api_key

    config = aai.TranscriptionConfig(
        speaker_labels=True,
        auto_chapters=True,
        entity_detection=True,
        summarization=True,
        summary_model=aai.SummarizationModel.informative,
        summary_type=aai.SummarizationType.bullets,
    )

    transcriber = aai.Transcriber()
    print(f"[INFO] Uploading and transcribing: {audio_path}")
    print("[INFO] This may take 10-30 minutes for a multi-hour meeting...")

    transcript = transcriber.transcribe(audio_path, config=config)

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"Transcription failed: {transcript.error}")

    utterances = []
    if transcript.utterances:
        for u in transcript.utterances:
            utterances.append({
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
            })

    chapters = []
    if transcript.chapters:
        for ch in transcript.chapters:
            chapters.append({
                "headline": ch.headline,
                "summary": ch.summary,
                "start": ch.start,
                "end": ch.end,
            })

    return {
        "text": transcript.text,
        "utterances": utterances,
        "chapters": chapters,
        "summary": transcript.summary if transcript.summary else "",
        "entities": [
            {"text": e.text, "entity_type": e.entity_type}
            for e in (transcript.entities or [])
        ],
    }


def format_transcript_with_speakers(utterances: list[dict]) -> str:
    """Format utterances into a readable speaker-labeled transcript."""
    lines = []
    for u in utterances:
        timestamp = format_timestamp(u["start"])
        lines.append(f"[{timestamp}] Speaker {u['speaker']}: {u['text']}")
    return "\n\n".join(lines)


def format_timestamp(ms: int) -> str:
    """Convert milliseconds to HH:MM:SS."""
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Step 3: Summarization via Claude API
# ---------------------------------------------------------------------------

def build_summary_prompt(
    transcript_text: str,
    video_metadata: dict,
    chapters: list[dict],
    assemblyai_summary: str,
    watch_topics: list[str],
) -> str:
    """Build the Claude API prompt for structured summarization."""

    topics_list = "\n".join(f"  - {t}" for t in watch_topics)

    chapters_text = ""
    if chapters:
        ch_lines = []
        for ch in chapters:
            ts = format_timestamp(ch["start"])
            ch_lines.append(f"  [{ts}] {ch['headline']}: {ch['summary']}")
        chapters_text = "\n".join(ch_lines)

    prompt = f"""You are an expert analyst covering Portland, Oregon city government for a real estate developer and NAIOP Public Affairs Committee member focused on net-zero infill multifamily development.

MEETING INFORMATION:
- Title: {video_metadata.get('title', 'Portland City Council Meeting')}
- Date: {video_metadata.get('upload_date', 'Unknown')}
- Duration: {video_metadata.get('duration', 0) // 60} minutes

{f"ASSEMBLYAI AUTO-SUMMARY:{chr(10)}{assemblyai_summary}{chr(10)}" if assemblyai_summary else ""}
{f"AUTO-GENERATED CHAPTERS:{chr(10)}{chapters_text}{chr(10)}" if chapters_text else ""}

FULL TRANSCRIPT (with speaker labels):
{transcript_text[:MAX_TRANSCRIPT_CHARS]}

---

Please produce a structured summary with the following sections:

## 1. EXECUTIVE SUMMARY
A 3-5 paragraph overview of the meeting: what was on the agenda, major decisions made, notable votes, and overall tone/dynamics. Identify any Councilors by name if possible from context.

## 2. KEY VOTES & ACTIONS
List each formal vote, resolution, ordinance, or official action taken. Include:
- What was voted on
- The outcome (passed/failed, vote count if stated)
- Brief context on significance

## 3. TOPIC FLAGS
For each of the following topics, provide a section ONLY if it was discussed during the meeting. For each flagged topic, include:
- What was discussed
- Who raised it (speaker label or name if identifiable)
- Any decisions or next steps
- Timestamp reference (approximate)
- Relevance/implications for Portland real estate development

Topics to watch:
{topics_list}

## 4. PUBLIC TESTIMONY HIGHLIGHTS
Summarize notable public testimony, especially from:
- Developers, builders, or real estate industry representatives
- Neighborhood associations
- Advocacy organizations
- Business owners or chambers of commerce

## 5. UPCOMING & FOLLOW-UP
Note any items that were:
- Continued/tabled to a future meeting
- Referred to committee
- Scheduled for future public hearings
- Deadlines mentioned

## 6. OAKLEAF RELEVANCE SCORE
Rate this meeting's relevance to Oakleaf reDevelopment on a 1-5 scale:
1 = Nothing relevant
2 = Minor mentions of relevant topics
3 = Moderate discussion of relevant policy areas
4 = Significant policy action affecting development
5 = Critical — direct impact on Oakleaf's projects or pipeline

Explain the rating briefly.

Format the output in clean Markdown suitable for saving as a .md file.
"""
    return prompt


def summarize_with_claude(prompt: str, api_key: str) -> str:
    """Send the transcript and prompt to Claude for summarization."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    print("[INFO] Sending transcript to Claude for summarization...")
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text


# ---------------------------------------------------------------------------
# Step 4: Save to Google Drive (Service Account auth for headless/CI)
# ---------------------------------------------------------------------------

def get_drive_service():
    """
    Authenticate via Google Service Account for headless environments.
    Reads credentials from GOOGLE_SERVICE_ACCOUNT_JSON env var.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        print("[WARN] GOOGLE_SERVICE_ACCOUNT_JSON not set. Skipping Drive upload.")
        return None

    try:
        sa_info = json.loads(sa_json)
    except json.JSONDecodeError:
        print("[ERROR] GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.")
        return None

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=SCOPES
    )

    return build("drive", "v3", credentials=credentials)


def upload_to_drive(service, filename: str, content: str, folder_id: str) -> str:
    """Upload a Markdown file to Google Drive. Returns the web view link."""
    from googleapiclient.http import MediaInMemoryUpload

    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }
    media = MediaInMemoryUpload(
        content.encode("utf-8"),
        mimetype="text/markdown",
        resumable=True,
    )

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    print(f"[INFO] Uploaded to Google Drive: {file.get('webViewLink')}")
    return file.get("webViewLink", "")


# ---------------------------------------------------------------------------
# Tracking processed videos (simple JSON ledger)
# ---------------------------------------------------------------------------

LEDGER_PATH = Path("processed_videos.json")


def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        with open(LEDGER_PATH) as f:
            return json.load(f)
    return {"processed": {}}


def save_ledger(ledger: dict):
    with open(LEDGER_PATH, "w") as f:
        json.dump(ledger, f, indent=2)


def is_processed(video_id: str) -> bool:
    return video_id in load_ledger().get("processed", {})


def mark_processed(video_id: str, metadata: dict):
    ledger = load_ledger()
    ledger["processed"][video_id] = {
        "title": metadata.get("title", ""),
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "drive_link": metadata.get("drive_link", ""),
    }
    save_ledger(ledger)


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def process_video(youtube_url: str) -> str:
    """Full pipeline: download → transcribe → summarize → upload."""

    assemblyai_key = os.environ.get("ASSEMBLYAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    drive_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")

    if not assemblyai_key:
        raise ValueError("ASSEMBLYAI_API_KEY not set.")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")

    # 1. Get video metadata
    print("\n=== STEP 1: Fetching video metadata ===")
    metadata = get_video_metadata(youtube_url)
    print(f"  Title: {metadata['title']}")
    print(f"  Date:  {metadata['upload_date']}")
    print(f"  Duration: {metadata['duration'] // 60} minutes")

    if is_processed(metadata["id"]):
        print(f"[INFO] Video {metadata['id']} already processed. Skipping.")
        return ""

    # 2. Download audio
    print("\n=== STEP 2: Downloading audio ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = download_audio(youtube_url, tmpdir)
        print(f"  Audio saved to: {audio_path}")

        # 3. Transcribe
        print("\n=== STEP 3: Transcribing with AssemblyAI ===")
        transcript_data = transcribe_audio(audio_path, assemblyai_key)

    # Format transcript
    if transcript_data["utterances"]:
        formatted_transcript = format_transcript_with_speakers(
            transcript_data["utterances"]
        )
    else:
        formatted_transcript = transcript_data["text"]

    # Save raw transcript locally (available as GitHub Actions artifact)
    transcript_dir = Path("transcripts")
    transcript_dir.mkdir(exist_ok=True)
    transcript_path = transcript_dir / f"{metadata['id']}_transcript.txt"
    with open(transcript_path, "w") as f:
        f.write(formatted_transcript)
    print(f"  Transcript saved: {transcript_path}")

    # 4. Summarize with Claude
    print("\n=== STEP 4: Summarizing with Claude ===")
    prompt = build_summary_prompt(
        transcript_text=formatted_transcript,
        video_metadata=metadata,
        chapters=transcript_data.get("chapters", []),
        assemblyai_summary=transcript_data.get("summary", ""),
        watch_topics=WATCH_TOPICS,
    )
    summary = summarize_with_claude(prompt, anthropic_key)

    # Build full summary document
    date_str = metadata["upload_date"]
    if len(date_str) == 8:
        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    full_summary = f"""# Portland City Council Meeting Summary
**Date:** {date_str}
**Title:** {metadata['title']}
**Duration:** {metadata['duration'] // 60} minutes
**Source:** {youtube_url}
**Processed:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

{summary}
"""

    # Save locally
    summary_dir = Path("summaries")
    summary_dir.mkdir(exist_ok=True)
    summary_filename = f"{date_str}_{metadata['id']}_summary.md"
    summary_path = summary_dir / summary_filename
    with open(summary_path, "w") as f:
        f.write(full_summary)
    print(f"  Summary saved: {summary_path}")

    # 5. Upload to Google Drive
    drive_link = ""
    if drive_folder_id:
        print("\n=== STEP 5: Uploading to Google Drive ===")
        try:
            service = get_drive_service()
            if service:
                drive_link = upload_to_drive(
                    service, summary_filename, full_summary, drive_folder_id
                )
        except Exception as e:
            print(f"[WARN] Google Drive upload failed: {e}")
            print("  Summary is still saved locally as a GitHub Actions artifact.")

    mark_processed(metadata["id"], {
        "title": metadata["title"],
        "drive_link": drive_link,
    })

    print("\n=== DONE ===")
    print(f"  Summary: {summary_path}")
    if drive_link:
        print(f"  Drive:   {drive_link}")

    return drive_link


def check_and_process_new():
    """Check for new videos on the channel and process unprocessed ones."""
    print(f"[INFO] Checking for new videos on channel: {YOUTUBE_CHANNEL_ID}")

    videos = fetch_latest_videos(YOUTUBE_CHANNEL_ID, max_results=5)
    if not videos:
        print("[INFO] No videos found.")
        return

    # Filter to likely council meetings (skip short clips, etc.)
    council_keywords = ["council", "meeting", "session", "committee", "hearing"]

    new_count = 0
    for video in videos:
        title_lower = video["title"].lower()
        is_council = any(kw in title_lower for kw in council_keywords)

        if not is_council:
            print(f"[SKIP] Not a council meeting: {video['title']}")
            continue

        if is_processed(video["id"]):
            print(f"[SKIP] Already processed: {video['title']}")
            continue

        print(f"\n[NEW] Processing: {video['title']}")
        try:
            process_video(video["url"])
            new_count += 1
        except Exception as e:
            print(f"[ERROR] Failed to process {video['id']}: {e}")

    if new_count == 0:
        print("[INFO] No new council meetings to process.")
    else:
        print(f"\n[INFO] Processed {new_count} new meeting(s).")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Portland City Council Meeting Monitor"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Process a specific YouTube video URL")
    group.add_argument("--latest", action="store_true",
                       help="Process the latest video from the channel")
    group.add_argument("--check-new", action="store_true",
                       help="Check for and process new/unprocessed videos")

    args = parser.parse_args()

    if args.url:
        process_video(args.url)
    elif args.latest:
        videos = fetch_latest_videos(YOUTUBE_CHANNEL_ID, max_results=1)
        if videos:
            process_video(videos[0]["url"])
        else:
            print("[ERROR] Could not fetch latest video.")
    elif args.check_new:
        check_and_process_new()


if __name__ == "__main__":
    main()
