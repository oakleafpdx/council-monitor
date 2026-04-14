#!/usr/bin/env python3
"""
Council Meeting — Local Transcription Script (runs on your Windows PC)
======================================================================
Scheduled task that checks for new council meeting videos, downloads audio,
transcribes via AssemblyAI, and triggers GitHub Actions for summarization.

Setup (one-time):
    pip install -r requirements.txt
    winget install ffmpeg
    Copy .env.example to .env and fill in your API keys.

Usage:
    python local_transcribe.py --check-new
    python local_transcribe.py --url "https://www.youtube.com/watch?v=VIDEO_ID"
    python local_transcribe.py --url "URL" --no-trigger
    python local_transcribe.py --url "URL" --force       # re-process even if already done
"""

import httpx
import time
import os
import sys
import json
import argparse
import subprocess
import tempfile
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
AUDIO_DIR = DATA_DIR / "audio"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
LEDGER_PATH = DATA_DIR / "processed_videos.json"
CONFIG_PATH = SCRIPT_DIR / "config.json"


def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {
        "channel_id": "UCcPIUh7CWwtBXisMPHWG65g",
        "council_video_keywords": ["council", "meeting", "session", "committee", "hearing"],
    }


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    AUDIO_DIR.mkdir(exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)


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
        raise ValueError("YOUTUBE_API_KEY not set. Check your .env file.")
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


def fetch_latest_videos(channel_id: str, max_results: int = 10) -> list[dict]:
    service = get_youtube_service()
    response = service.search().list(
        part="snippet", channelId=channel_id,
        order="date", maxResults=max_results, type="video",
    ).execute()

    videos = []
    for item in response.get("items", []):
        vid_id = item["id"]["videoId"]
        snippet = item["snippet"]
        videos.append({
            "id": vid_id,
            "title": snippet["title"],
            "upload_date": snippet["publishedAt"][:10],
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return videos


# ---------------------------------------------------------------------------
# Download audio
# ---------------------------------------------------------------------------

def download_audio(youtube_url: str, video_id: str, safe_title: str, upload_date: str) -> str:
    """
    Download audio to the permanent AUDIO_DIR.
    Skips download if the file already exists locally.
    Returns the path to the MP3 file.
    """
    audio_filename = f"{upload_date}_{safe_title}.mp3"
    audio_path = AUDIO_DIR / audio_filename

    if audio_path.exists():
        file_size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"  [SKIP] Audio already exists locally ({file_size_mb:.1f} MB): {audio_path}")
        return str(audio_path)

    output_template = str(AUDIO_DIR / f"%(id)s.%(ext)s")
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "5",
        "-o", output_template,
        youtube_url,
    ]
    print(f"[INFO] Downloading audio from: {youtube_url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {result.stderr}")

    # yt-dlp saves as <video_id>.mp3 — find it by video_id and rename
    downloaded = AUDIO_DIR / f"{video_id}.mp3"
    if not downloaded.exists():
        # Fallback: look for any file containing the video_id
        for f in os.listdir(AUDIO_DIR):
            if video_id in f and f.endswith(".mp3"):
                downloaded = AUDIO_DIR / f
                break
    if not downloaded.exists():
        raise FileNotFoundError(f"Audio file not found after download. Expected: {downloaded}")

    downloaded.rename(audio_path)
    file_size_mb = audio_path.stat().st_size / (1024 * 1024)
    print(f"  Saved audio: {audio_path} ({file_size_mb:.1f} MB)")
    return str(audio_path)


# ---------------------------------------------------------------------------
# AssemblyAI Transcription — chunked approach
# ---------------------------------------------------------------------------

CHUNK_MINUTES = 60  # split audio into N-minute segments
MAX_ATTEMPTS  = 3   # retries per chunk on server error
RETRY_WAIT    = 60  # seconds between retries


def split_audio(audio_path: str, output_dir: str, chunk_minutes: int = CHUNK_MINUTES) -> list:
    """
    Use ffmpeg to split audio into fixed-length chunks.
    Returns list of dicts: [{path, start_ms}, ...]
    """
    chunk_secs = chunk_minutes * 60

    # Get total duration via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, timeout=30,
    )
    total_secs = float(probe.stdout.strip())
    total_mins = int(total_secs // 60)
    print(f"  Audio duration: {total_mins} min — splitting into {chunk_minutes}-min chunks")

    chunks = []
    start = 0
    idx = 0
    while start < total_secs:
        chunk_path = os.path.join(output_dir, f"chunk_{idx:03d}.mp3")
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ss", str(start),
            "-t", str(chunk_secs),
            "-acodec", "libmp3lame", "-q:a", "5",
            chunk_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg chunk {idx} failed: {result.stderr[-500:]}")
        chunks.append({"path": chunk_path, "start_ms": int(start * 1000)})
        start += chunk_secs
        idx += 1

    print(f"  Split into {len(chunks)} chunk(s).")
    return chunks


def _upload_and_submit(audio_path: str, headers: dict) -> str:
    """Upload one audio file to AssemblyAI and return the transcript ID."""
    with open(audio_path, "rb") as f:
        upload_resp = httpx.post(
            "https://api.assemblyai.com/v2/upload",
            headers=headers, content=f, timeout=300,
        )
    upload_resp.raise_for_status()
    upload_url = upload_resp.json()["upload_url"]

    transcript_req = httpx.post(
        "https://api.assemblyai.com/v2/transcript",
        headers=headers,
        json={
            "audio_url": upload_url,
            "speech_models": ["universal-3-pro"],
            "speaker_labels": True,
            "prompt": (
                "Transcribe this Portland City Council meeting. "
                "Speakers include Mayor Keith Wilson and City Councilors: "
                "Candace Avalos, Jamie Dunphy, Loretta Smith, Dan Ryan, "
                "Elana Pirtle-Guiney, Sameer Kanal, Angelita Morillo, "
                "Steve Novick, Tiffany Koyama Lane, Eric Zimmerman, "
                "Mitch Green, and Olivia Clark. "
                "Preserve all proper nouns, ordinance numbers, and policy terminology accurately. "
                "Common terms include: System Development Charges, SDCs, AMI, LIHTC, "
                "inclusionary housing, urban growth boundary, comprehensive plan."
            ),
        },
        timeout=30,
    )
    resp_json = transcript_req.json()
    if "id" not in resp_json:
        raise Exception(f"Submission error: {resp_json.get('error', resp_json)}")
    return resp_json["id"]


def _poll_until_done(transcript_id: str, headers: dict, chunk_label: str) -> dict:
    """Poll AssemblyAI until transcript is complete. Returns the result JSON."""
    poll_count = 0
    bad_resp_count = 0
    MAX_BAD_RESPONSES = 5
    while True:
        try:
            poll_resp = httpx.get(
                f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
                headers=headers, timeout=30,
            )
            if not poll_resp.content:
                raise ValueError("Empty response body from AssemblyAI")
            data = poll_resp.json()
            bad_resp_count = 0  # reset on a clean response
        except Exception as e:
            bad_resp_count += 1
            if bad_resp_count <= MAX_BAD_RESPONSES:
                print(f"\n[WARN] {chunk_label}: bad poll response #{bad_resp_count}/{MAX_BAD_RESPONSES}, retrying in 15s: {e}")
                time.sleep(15)
                continue
            else:
                raise Exception(f"{chunk_label}: too many bad poll responses — giving up: {e}")

        status = data.get("status")

        if status == "completed":
            print()  # newline after dots
            return data
        elif status == "error":
            print()
            raise Exception(data.get("error", "Unknown AssemblyAI error"))
        else:
            poll_count += 1
            if poll_count % 8 == 0:
                print(f"\n[INFO] {chunk_label}: still transcribing... ({poll_count * 15 // 60} min elapsed)")
            else:
                print(".", end="", flush=True)
            time.sleep(15)


def transcribe_chunk(audio_path: str, headers: dict, chunk_label: str) -> dict:
    """Transcribe a single audio file with retries on server errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            print(f"[INFO] {chunk_label}: retry {attempt}/{MAX_ATTEMPTS} (waiting {RETRY_WAIT}s)...")
            time.sleep(RETRY_WAIT)
        try:
            transcript_id = _upload_and_submit(audio_path, headers)
            print(f"[INFO] {chunk_label}: queued (ID: {transcript_id})")
            return _poll_until_done(transcript_id, headers, chunk_label)
        except Exception as e:
            err = str(e).lower()
            is_server_err = any(p in err for p in [
                "internal server error", "server error",
                "developers have been alerted", "please retry", "please contact support"
            ])
            if is_server_err and attempt < MAX_ATTEMPTS:
                print(f"[WARN] {chunk_label}: server error (attempt {attempt}/{MAX_ATTEMPTS}): {e}")
            else:
                raise Exception(f"{chunk_label} failed after {attempt} attempt(s): {e}")


def merge_chunks(chunk_results: list, chunk_offsets_ms: list) -> dict:
    """
    Merge multiple AssemblyAI result dicts into one unified result.
    - Offsets all timestamps by chunk start time
    - Normalises speaker labels globally across chunks
    - Merges utterances, chapters, and full text
    """
    speaker_map = {}
    next_label = [ord("A")]

    def global_speaker(chunk_idx, local_label):
        key = f"{chunk_idx}:{local_label}"
        if key not in speaker_map:
            speaker_map[key] = chr(next_label[0])
            next_label[0] += 1
        return speaker_map[key]

    merged_utterances = []
    merged_chapters   = []
    merged_text_parts = []

    for i, (result, offset_ms) in enumerate(zip(chunk_results, chunk_offsets_ms)):
        for u in result.get("utterances") or []:
            merged_utterances.append({
                **u,
                "start":   u["start"]   + offset_ms,
                "end":     u["end"]     + offset_ms,
                "speaker": global_speaker(i, u["speaker"]),
                "words": [
                    {**w, "start": w["start"] + offset_ms, "end": w["end"] + offset_ms}
                    for w in (u.get("words") or [])
                ],
            })

        for ch in result.get("chapters") or []:
            merged_chapters.append({
                **ch,
                "start": ch["start"] + offset_ms,
                "end":   ch["end"]   + offset_ms,
            })

        if result.get("text"):
            merged_text_parts.append(result["text"])

    merged_utterances.sort(key=lambda u: u["start"])
    merged_chapters.sort(key=lambda c: c["start"])

    return {
        "status":     "completed",
        "text":       " ".join(merged_text_parts),
        "utterances": merged_utterances,
        "chapters":   merged_chapters,
        "summary":    None,
        "entities":   [],
    }


def transcribe_audio(audio_path: str) -> dict:
    """
    Split audio into chunks, transcribe each, merge results.
    Falls back to single-file transcription for short files (< CHUNK_MINUTES).
    """
    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        raise ValueError("ASSEMBLYAI_API_KEY not set. Check your .env file.")
    headers = {"authorization": api_key}

    # Check duration to decide whether chunking is needed
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, timeout=30,
    )
    total_secs = float(probe.stdout.strip())

    if total_secs <= CHUNK_MINUTES * 60:
        print(f"[INFO] File is under {CHUNK_MINUTES} min — transcribing as single file.")
        return transcribe_chunk(audio_path, headers, "Chunk 1/1")

    # Long file — split into chunks
    chunk_dir = os.path.join(os.path.dirname(audio_path), "chunks")
    os.makedirs(chunk_dir, exist_ok=True)
    chunks = split_audio(audio_path, chunk_dir)

    chunk_results = []
    for idx, chunk in enumerate(chunks, 1):
        label = f"Chunk {idx}/{len(chunks)}"
        print(f"\n[INFO] Transcribing {label} (offset {chunk['start_ms'] // 1000 // 60} min)...")
        result = transcribe_chunk(chunk["path"], headers, label)
        chunk_results.append(result)

    print(f"\n[INFO] All {len(chunks)} chunks complete — merging...")
    offsets = [c["start_ms"] for c in chunks]
    merged = merge_chunks(chunk_results, offsets)
    print(f"  Merged {len(merged['utterances'])} utterances, {len(merged['chapters'])} chapters.")
    return merged


# ---------------------------------------------------------------------------
# Trigger GitHub Action
# ---------------------------------------------------------------------------

def trigger_github_action(video_id: str, transcript_path: str, metadata: dict,
                          chapters: list, assemblyai_summary: str):
    github_token = os.environ.get("GITHUB_PAT")
    github_repo = os.environ.get("GITHUB_REPO")

    if not github_token or not github_repo:
        print("[WARN] GITHUB_PAT or GITHUB_REPO not set. Skipping trigger.")
        print("  You can manually run the GitHub Action with the transcript.")
        return False

    import base64

    api_base = f"https://api.github.com/repos/{github_repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # --- Step 4a: Commit transcript + metadata to the repo ---
    print(f"[INFO] Uploading transcript to {github_repo}...")

    # Read the full transcript
    with open(transcript_path, encoding="utf-8") as f:
        transcript_text = f.read()

    # Build metadata JSON that the Action can read
    meta_payload = {
        "video_id": video_id,
        "title": metadata.get("title", "Unknown"),
        "upload_date": metadata.get("upload_date", "Unknown"),
        "duration": metadata.get("duration", 0),
        "chapters": chapters,
        "assemblyai_summary": assemblyai_summary or "",
    }

    # Upload files to repo under transcripts/<video_id>/
    files_to_upload = {
        f"transcripts/{video_id}/transcript.txt": transcript_text,
        f"transcripts/{video_id}/metadata.json": json.dumps(meta_payload, indent=2),
    }

    for file_path, content in files_to_upload.items():
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

        # Check if file already exists (need its SHA to update)
        existing = requests.get(
            f"{api_base}/contents/{file_path}",
            headers=headers, timeout=30,
        )
        put_body = {
            "message": f"Add transcript for {video_id}: {metadata.get('title', '')}",
            "content": encoded,
        }
        if existing.status_code == 200:
            put_body["sha"] = existing.json()["sha"]

        resp = requests.put(
            f"{api_base}/contents/{file_path}",
            headers=headers, json=put_body, timeout=30,
        )
        if resp.status_code in (200, 201):
            print(f"  [OK] Uploaded {file_path}")
        else:
            print(f"  [ERROR] Failed to upload {file_path}: {resp.status_code} {resp.text}")
            return False

    # --- Step 4b: Trigger the Action with a lightweight payload ---
    print(f"[INFO] Triggering GitHub Action...")
    dispatch_payload = {
        "event_type": "transcript-ready",
        "client_payload": {
            "video_id": video_id,
            "title": metadata.get("title", "Unknown"),
            "upload_date": metadata.get("upload_date", "Unknown"),
            "duration": metadata.get("duration", 0),
            "transcript_path": f"transcripts/{video_id}/transcript.txt",
            "metadata_path": f"transcripts/{video_id}/metadata.json",
        },
    }

    response = requests.post(
        f"{api_base}/dispatches",
        headers=headers, json=dispatch_payload, timeout=30,
    )

    if response.status_code == 204:
        print("[INFO] GitHub Action triggered successfully!")
        return True
    else:
        print(f"[ERROR] GitHub trigger failed: {response.status_code} {response.text}")
        return False


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

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
    }
    save_ledger(ledger)


# ---------------------------------------------------------------------------
# Format Transcript
# ---------------------------------------------------------------------------

def format_transcript(transcript_data: dict) -> str:
    lines = []
    lines.append("COUNCIL MEETING TRANSCRIPT")
    lines.append("=" * 60)

    # Summary
    summary = transcript_data.get("summary")
    if summary:
        lines.append("\nSUMMARY")
        lines.append("-" * 40)
        lines.append(summary)

    # Chapters
    chapters = transcript_data.get("chapters", [])
    if chapters:
        lines.append("\nCHAPTERS")
        lines.append("-" * 40)
        for i, ch in enumerate(chapters, 1):
            start = format_timestamp(ch["start"])
            end = format_timestamp(ch["end"])
            lines.append(f"\n  Chapter {i}: {ch.get('headline', 'Untitled')}")
            lines.append(f"  [{start} - {end}]")
            lines.append(f"  {ch.get('summary', '')}")

    # Speaker-labeled transcript
    lines.append("\n\nFULL TRANSCRIPT")
    lines.append("=" * 60)
    utterances = transcript_data.get("utterances", [])
    for u in utterances:
        ts = format_timestamp(u["start"])
        lines.append(f"\n[{ts}] Speaker {u['speaker']}:")
        lines.append(f"  {u['text']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

def process_video(youtube_url: str, trigger_github: bool = True, force: bool = False):
    ensure_dirs()
    video_id = extract_video_id(youtube_url)

    print(f"\n{'='*60}")
    print(f"  COUNCIL MEETING MONITOR — Processing Video")
    print(f"{'='*60}")

    # 1. Metadata
    print("\n--- Step 1: Fetching video metadata ---")
    metadata = get_video_metadata(video_id)
    print(f"  Title:    {metadata['title']}")
    print(f"  Date:     {metadata['upload_date']}")
    print(f"  Duration: {metadata['duration'] // 60} minutes")

    if is_processed(video_id) and not force:
        print(f"\n[SKIP] Video {video_id} already processed. Use --force to re-process.")
        return
    elif is_processed(video_id) and force:
        print(f"[INFO] Video {video_id} was already processed — re-processing (--force).")

    safe_title = re.sub(r'[<>:"/\\|?*]', '', metadata.get("title", video_id)).strip()

    # 2. Download (saved permanently to data/audio/ — skips if already exists)
    print("\n--- Step 2: Downloading audio ---")
    audio_path = download_audio(youtube_url, video_id, safe_title, metadata["upload_date"])

    # 3. Transcribe (skips if transcript + raw JSON already exist locally)
    print("\n--- Step 3: Transcribing with AssemblyAI ---")
    transcript_path = TRANSCRIPTS_DIR / f"{metadata['upload_date']}_{safe_title}_transcript.txt"
    raw_path = TRANSCRIPTS_DIR / f"{metadata['upload_date']}_{safe_title}_raw.json"

    if transcript_path.exists() and raw_path.exists() and not force:
        # Load cached transcript data from the raw JSON
        print(f"  [SKIP] Transcript already exists locally: {transcript_path}")
        with open(raw_path, encoding="utf-8") as f:
            transcript_data = json.load(f)
    else:
        if transcript_path.exists() and force:
            print(f"  [INFO] Transcript exists but --force specified — re-transcribing.")
        transcript_data = transcribe_audio(audio_path)

        # Format and save transcript
        formatted = format_transcript(transcript_data)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(formatted)

        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, indent=2)

    speakers = len(set(u["speaker"] for u in transcript_data.get("utterances", [])))
    print(f"  Transcript: {transcript_path}")
    print(f"  Speakers detected: {speakers}")
    print(f"  Chapters: {len(transcript_data.get('chapters', []))}")

    # 4. Trigger GitHub
    if trigger_github:
        print("\n--- Step 4: Triggering GitHub Action ---")
        trigger_github_action(
            video_id, str(transcript_path), metadata,
            transcript_data.get("chapters", []),
            transcript_data.get("summary", ""),
        )

    mark_processed(video_id, metadata)

    print(f"\n{'='*60}")
    print(f"  LOCAL PROCESSING COMPLETE")
    print(f"  Transcript: {transcript_path}")
    if trigger_github:
        print(f"  Summary will appear in Google Drive shortly.")
    print(f"{'='*60}\n")


def check_and_process_new(trigger_github: bool = True):
    ensure_dirs()
    config = load_config()
    channel_id = config.get("channel_id", "UCcPIUh7CWwtBXisMPHWG65g")
    keywords = config.get("council_video_keywords", ["council", "meeting"])

    print(f"[INFO] Checking for new videos...")
    videos = fetch_latest_videos(channel_id, max_results=10)

    if not videos:
        print("[INFO] No videos found on channel.")
        return

    new_count = 0
    for video in videos:
        title_lower = video["title"].lower()
        is_council = any(kw in title_lower for kw in keywords)

        if not is_council:
            print(f"  [SKIP] Not a council meeting: {video['title']}")
            continue

        if is_processed(video["id"]):
            print(f"  [SKIP] Already processed: {video['title']}")
            continue

        print(f"\n  [NEW] {video['title']}")
        try:
            process_video(video["url"], trigger_github=trigger_github)
            new_count += 1
        except Exception as e:
            print(f"  [ERROR] {e}")

    if new_count == 0:
        print("\n[INFO] No new council meetings to process.")
    else:
        print(f"\n[INFO] Processed {new_count} new meeting(s).")


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Council Meeting Local Transcription")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Process a specific YouTube video URL")
    group.add_argument("--check-new", action="store_true", help="Check for new meetings")
    parser.add_argument("--no-trigger", action="store_true", help="Don't trigger GitHub Action")
    parser.add_argument("--force", action="store_true", help="Re-process even if already done (re-transcribe and re-trigger)")

    args = parser.parse_args()
    trigger = not args.no_trigger

    if args.url:
        process_video(args.url, trigger_github=trigger, force=args.force)
    elif args.check_new:
        check_and_process_new(trigger_github=trigger)


if __name__ == "__main__":
    main()
