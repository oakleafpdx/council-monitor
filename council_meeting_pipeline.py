#!/usr/bin/env python3
"""
Council Meeting Pipeline — GitHub Actions Side
===============================================
Receives transcripts from the local script (via repository_dispatch) or
falls back to YouTube captions. Runs Claude summarization and uploads
to Google Drive.

Config is externalized:
  - config.json: watch topics, video keywords, channel ID
  - prompt_template.md: Claude prompt (edit to refine summaries)
"""

import os
import sys
import json
import argparse
import re
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TRANSCRIPT_CHARS = 120_000
SCRIPT_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    config_path = SCRIPT_DIR / "config.json"
    with open(config_path) as f:
        return json.load(f)


def load_prompt_template() -> str:
    prompt_path = SCRIPT_DIR / "prompt_template.md"
    with open(prompt_path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str:
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'(?:live/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from URL: {url}")


def format_timestamp(ms: int) -> str:
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# ---------------------------------------------------------------------------
# YouTube Data API
# ---------------------------------------------------------------------------

def get_youtube_service():
    from googleapiclient.discovery import build
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise ValueError("YOUTUBE_API_KEY not set.")
    return build("youtube", "v3", developerKey=api_key)


def get_video_metadata(video_id: str) -> dict:
    service = get_youtube_service()
    response = service.videos().list(
        part="snippet,contentDetails", id=video_id
    ).execute()

    if not response.get("items"):
        return {"id": video_id, "title": "Unknown", "upload_date": "Unknown", "duration": 0}

    item = response["items"][0]
    snippet = item["snippet"]
    duration_str = item["contentDetails"]["duration"]
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    duration = 0
    if match:
        duration = (int(match.group(1) or 0) * 3600 +
                    int(match.group(2) or 0) * 60 +
                    int(match.group(3) or 0))

    return {
        "id": video_id,
        "title": snippet["title"],
        "upload_date": snippet["publishedAt"][:10],
        "duration": duration,
    }


def fetch_youtube_captions(video_id: str) -> str:
    """Fallback: fetch YouTube auto-captions."""
    from youtube_transcript_api import YouTubeTranscriptApi

    print(f"[INFO] Fetching YouTube captions for: {video_id}")
    transcript_list = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])

    lines = []
    for entry in transcript_list:
        ts = format_timestamp(int(entry["start"] * 1000))
        text = entry["text"].replace("\n", " ")
        lines.append(f"[{ts}] {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Summarization
# ---------------------------------------------------------------------------

def build_prompt(
    transcript_text: str,
    video_metadata: dict,
    watch_topics: list[str],
    chapters_text: str = "",
    assemblyai_summary: str = "",
) -> str:
    """Build prompt from template and data."""
    template = load_prompt_template()
    topics_list = "\n".join(f"  - {t}" for t in watch_topics)

    assemblyai_section = ""
    if assemblyai_summary:
        assemblyai_section = f"ASSEMBLYAI AUTO-SUMMARY:\n{assemblyai_summary}\n"

    chapters_section = ""
    if chapters_text:
        chapters_section = f"AUTO-GENERATED CHAPTERS:\n{chapters_text}\n"

    return template.format(
        title=video_metadata.get("title", "Portland City Council Meeting"),
        upload_date=video_metadata.get("upload_date", "Unknown"),
        duration_min=video_metadata.get("duration", 0) // 60,
        assemblyai_section=assemblyai_section,
        chapters_section=chapters_section,
        transcript=transcript_text[:MAX_TRANSCRIPT_CHARS],
        topics_list=topics_list,
    )


def summarize_with_claude(prompt: str) -> str:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set.")

    client = anthropic.Anthropic(api_key=api_key)
    print("[INFO] Sending transcript to Claude for summarization...")

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def get_drive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        return None

    sa_info = json.loads(sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=credentials)


def upload_to_drive(service, filename: str, content: str, folder_id: str) -> str:
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown", resumable=True)
    
    # Try creating without a parent folder first (in service account's own Drive)
    try:
        file = service.files().create(
            body={"name": filename},
            media_body=media,
            fields="id, webViewLink",
        ).execute()
        file_id = file.get("id")
        print(f"[INFO] Created file: {file_id}")
        
        # Share with you
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "writer", "emailAddress": "robert@oakleaf.dev"},
        ).execute()
        
        print(f"[INFO] Uploaded to Google Drive: {file.get('webViewLink')}")
        return file.get("webViewLink", "")
    except Exception as e:
        print(f"[DEBUG] Upload error details: {e}")
        raise


# ---------------------------------------------------------------------------
# Ledger
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

def mark_processed(video_id: str, metadata: dict):
    ledger = load_ledger()
    ledger["processed"][video_id] = {
        "title": metadata.get("title", ""),
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "drive_link": metadata.get("drive_link", ""),
    }
    save_ledger(ledger)


# ---------------------------------------------------------------------------
# Build summary document
# ---------------------------------------------------------------------------

def build_summary_doc(
    summary: str,
    metadata: dict,
    video_id: str,
    transcription_method: str,
) -> str:
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    return f"""# Portland City Council Meeting Summary
**Date:** {metadata['upload_date']}
**Title:** {metadata['title']}
**Duration:** {metadata['duration'] // 60} minutes
**Source:** {youtube_url}
**Transcription:** {transcription_method}
**Processed:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

---

{summary}
"""


def save_and_upload(full_summary: str, video_id: str, date_str: str, metadata: dict) -> str:
    """Save locally and upload to Google Drive."""
    summary_dir = Path("summaries")
    summary_dir.mkdir(exist_ok=True)
    summary_filename = f"{date_str}_{video_id}_summary.md"
    summary_path = summary_dir / summary_filename
    with open(summary_path, "w") as f:
        f.write(full_summary)
    print(f"  Summary saved: {summary_path}")

    drive_link = ""
    drive_folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if drive_folder_id:
        try:
            service = get_drive_service()
            if service:
                drive_link = upload_to_drive(service, summary_filename, full_summary, drive_folder_id)
        except Exception as e:
            print(f"[WARN] Drive upload failed: {e}")

    mark_processed(video_id, {"title": metadata["title"], "drive_link": drive_link})
    return drive_link


# ---------------------------------------------------------------------------
# Process from dispatch (AssemblyAI transcript committed to repo)
# ---------------------------------------------------------------------------

def process_from_dispatch():
    config = load_config()

    payload_json = os.environ.get("TRANSCRIPT_PAYLOAD", "{}")
    payload = json.loads(payload_json)

    video_id = payload.get("video_id")
    if not video_id:
        print("[ERROR] No video_id in dispatch payload.")
        sys.exit(1)

    # Read transcript and metadata from repo files (committed by local script)
    transcript_path = payload.get("transcript_path", f"transcripts/{video_id}/transcript.txt")
    metadata_path = payload.get("metadata_path", f"transcripts/{video_id}/metadata.json")

    repo_root = SCRIPT_DIR
    transcript_file = repo_root / transcript_path
    metadata_file = repo_root / metadata_path

    if not transcript_file.exists():
        print(f"[ERROR] Transcript file not found: {transcript_file}")
        print("  Make sure the local script committed it to the repo.")
        sys.exit(1)

    with open(transcript_file, encoding="utf-8") as f:
        transcript = f.read()

    if not transcript:
        print("[ERROR] Transcript file is empty.")
        sys.exit(1)

    # Load metadata from committed file, with fallbacks to dispatch payload
    if metadata_file.exists():
        with open(metadata_file, encoding="utf-8") as f:
            meta_from_file = json.load(f)
    else:
        print(f"[WARN] Metadata file not found: {metadata_file}, using dispatch payload.")
        meta_from_file = {}

    metadata = {
        "id": video_id,
        "title": meta_from_file.get("title", payload.get("title", "Unknown")),
        "upload_date": meta_from_file.get("upload_date", payload.get("upload_date", "Unknown")),
        "duration": meta_from_file.get("duration", payload.get("duration", 0)),
    }

    # Parse chapters from metadata file
    chapters_text = ""
    chapters = meta_from_file.get("chapters", [])
    if chapters:
        ch_lines = [
            f"  [{format_timestamp(ch.get('start', 0))}] {ch.get('headline', '')}: {ch.get('summary', '')}"
            for ch in chapters
        ]
        chapters_text = "\n".join(ch_lines)

    assemblyai_summary = meta_from_file.get("assemblyai_summary", "")

    print(f"\n=== Processing transcript from repo ===")
    print(f"  Video: {metadata['title']}")
    print(f"  Date:  {metadata['upload_date']}")
    print(f"  Transcript: {len(transcript)} chars")

    prompt = build_prompt(transcript, metadata, config["watch_topics"], chapters_text, assemblyai_summary)
    summary = summarize_with_claude(prompt)
    full_summary = build_summary_doc(summary, metadata, video_id, "AssemblyAI (speaker diarization enabled)")
    drive_link = save_and_upload(full_summary, video_id, metadata["upload_date"], metadata)

    print(f"\n=== DONE ===")
    if drive_link:
        print(f"  Drive: {drive_link}")


# ---------------------------------------------------------------------------
# Process from URL (YouTube captions fallback)
# ---------------------------------------------------------------------------

def process_from_url(youtube_url: str):
    config = load_config()
    video_id = extract_video_id(youtube_url)

    print("\n=== STEP 1: Fetching video metadata ===")
    metadata = get_video_metadata(video_id)
    print(f"  Title: {metadata['title']}")
    print(f"  Date:  {metadata['upload_date']}")

    print("\n=== STEP 2: Fetching YouTube captions ===")
    transcript = fetch_youtube_captions(video_id)
    print(f"  Transcript: {len(transcript)} chars")

    print("\n=== STEP 3: Summarizing with Claude ===")
    prompt = build_prompt(transcript, metadata, config["watch_topics"])
    summary = summarize_with_claude(prompt)
    full_summary = build_summary_doc(summary, metadata, video_id, "YouTube auto-captions (no speaker diarization)")
    drive_link = save_and_upload(full_summary, video_id, metadata["upload_date"], metadata)

    print(f"\n=== DONE ===")
    if drive_link:
        print(f"  Drive: {drive_link}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Council Meeting Summarizer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from-dispatch", action="store_true")
    group.add_argument("--url", help="Process a YouTube URL (uses captions)")

    args = parser.parse_args()

    if args.from_dispatch:
        process_from_dispatch()
    elif args.url:
        process_from_url(args.url)


if __name__ == "__main__":
    main()
