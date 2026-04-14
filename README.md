Portland City Council Meeting Monitor
Automated pipeline that transcribes Portland City Council meetings with speaker identification and produces structured summaries flagged for real estate, climate, and economic development topics.
Architecture
Your Windows PC (scheduled task)         GitHub Actions (triggered automatically)
────────────────────────────────         ──────────────────────────────────────
1. Check for new meetings (YT API)
2. Download audio (yt-dlp)
3. Transcribe (AssemblyAI)
4. Send transcript ─────────────────────> 5. Summarize (Claude)
                                          6. Upload to Google Drive
Editing Topics & Summary Format
To change which topics are flagged: Edit config.json in this repo.
To refine the summary structure: Edit prompt_template.md in this repo. This is the exact prompt sent to Claude — adjust sections, add instructions, change emphasis.
Fallback
Trigger the Action manually with a YouTube URL to use YouTube auto-captions (less accurate, no speaker labels).
Costs
~$0.50–1.50 per meeting (AssemblyAI free tier + Claude API).
