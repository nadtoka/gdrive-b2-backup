# GDrive -> Backblaze B2 Backup Microservice (`gdrive-b2-backup`)

Self-hosted Python/FastAPI microservice that periodically backs up Google Drive folders, Shared Drives (Team Drives), or Google Workspace user drives directly to **Backblaze B2** (or any S3-compatible bucket) using `rclone` with `--backup-dir` snapshot archiving.

---

## Features

- **100% Read-Only Google Drive Access (`drive.readonly`)**: The backup service cannot modify or delete anything on your live Google Drive.
- **Zero Local Disk Staging Required**: Streams directly from Google Drive API to Backblaze B2 (`--fast-list` optimized for 7,000+ files).
- **Incremental Mirror + Point-in-Time Snapshots**:
  - `<b2_prefix>/current/` always holds the latest mirror of the Google Drive folder.
  - `<b2_prefix>/snapshots/<YYYY-MM-DD_HH-MM-SS>/` automatically archives any modified or deleted files before overwriting/removing them from `current/`.
- **Mass-Deletion Protection (`--max-delete`)**: Aborts sync and sends a webhook alert if more than `N` files (default `500`) were deleted on Google Drive in a single run (ransomware / sabotage guard).
- **Automatic Google Docs Conversion**: Exports native Google Docs, Sheets, and Slides to `docx,xlsx,pptx`.
- **Web UI Dashboard**: Add multiple Google Drive folders or Shared Drives, configure per-target Cron schedules, trigger manual or `Dry-Run` backups, and inspect run logs.
- **Prometheus `/metrics` Endpoint**: Ready to scrape into Prometheus / Grafana (`gdrive_backup_target_status`, `gdrive_backup_last_files_transferred`, `gdrive_backup_last_bytes_transferred`, `gdrive_backup_last_duration_seconds`).

---

## Quick Start

### 1. Create a Google Cloud Service Account (No Workspace Admin required!)

1. Open [Google Cloud Console](https://console.cloud.google.com/), create or select a project, and enable the **Google Drive API**.
2. Go to **IAM & Admin -> Service Accounts -> Create Service Account** (e.g. `gdrive-backup-bot`).
3. Under the **Keys** tab, click **Add Key -> Create new key -> JSON** and save it to `./secrets/service-account.json`.
4. Copy the Service Account's email address (e.g. `gdrive-backup-bot@my-project.iam.gserviceaccount.com`) and **Share** your target Google Drive folder(s) or Shared Drive(s) with this email with **Viewer (Read-Only)** permissions.
   *(Optional: If you have Google Workspace Domain-Wide Delegation enabled, you can also specify `impersonate_user` per target in the Web UI to back up any user's root drive without manual sharing).*

### 2. Create an Isolated Backblaze B2 Bucket & Application Key

1. In Backblaze B2, create a dedicated bucket (e.g. `company-gdrive-backups`).
2. Create an **Application Key** restricted only to that bucket.
3. *(Optional)* Set a B2 Lifecycle Rule on the `snapshots/` prefix to automatically expire archived versions older than 90/180/365 days.

### 3. Configure `.env` and Run via Docker Compose

```bash
cp .env.example .env
# Edit .env with your B2_KEY_ID, B2_APPLICATION_KEY, and B2_BUCKET
docker compose up -d --build
```

Open **`http://localhost:8080`** in your browser to add your Google Drive targets and run a Dry-Run or live backup.

---

## Project Structure

```text
gdrive-b2-backup/
├── app/
│   ├── config.py          # Environment & settings via pydantic-settings
│   ├── db.py              # SQLite persistence for targets and backup_runs
│   ├── main.py            # FastAPI routes, Web UI, REST API, and Prometheus /metrics
│   ├── rclone_runner.py   # Dynamic rclone env builder, subprocess runner, JSON log parser
│   ├── scheduler.py       # APScheduler cron job manager per target
│   └── templates/
│       └── index.html     # TailwindCSS dashboard UI
├── tests/
│   └── test_app.py        # Unit tests for command builder, log parser, and DB
├── Dockerfile             # Python 3.12 + rclone binary
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```
