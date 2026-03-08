# Portland City Council Meeting Monitor

Automated pipeline that watches Portland City Council YouTube broadcasts, transcribes them with speaker identification, and produces structured summaries flagged for topics relevant to real estate development, climate policy, and economic development.

## How It Works

1. **GitHub Actions** checks the [eGov PDX YouTube channel](https://youtube.com/@egovpdx8714) every Wednesday and Thursday evening for new council meeting recordings
2. **yt-dlp** downloads the audio
3. **AssemblyAI** transcribes with speaker diarization and auto-chaptering
4. **Claude** produces a structured summary with topic flags and relevance scoring
5. **Google Drive** receives the finished summary as a Markdown file

## Summary Structure

Each summary includes:
- Executive summary of the meeting
- Key votes and official actions
- Topic flags (housing, SDCs, climate, zoning, etc.) with timestamps
- Public testimony highlights
- Upcoming items and deadlines
- Oakleaf Relevance Score (1–5)

## Manual Trigger

To process a specific video, go to **Actions → Council Meeting Monitor → Run workflow** and paste the YouTube URL.

## Costs

~$0.50–1.50 per meeting (AssemblyAI free tier + Claude API usage).
