import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings
from app.db import create_run, finish_run, get_target

logger = logging.getLogger("gdrive_backup")

# Prevent overlapping runs for the same target
_active_locks: dict[int, asyncio.Lock] = {}


def get_target_lock(target_id: int) -> asyncio.Lock:
    if target_id not in _active_locks:
        _active_locks[target_id] = asyncio.Lock()
    return _active_locks[target_id]


def build_rclone_env(target: dict[str, Any]) -> dict[str, str]:
    """
    Build isolated environment variables configuring rclone remotes on the fly:
    - RCLONE_CONFIG_GDRIVE_* (Read-Only Google Drive remote)
    - RCLONE_CONFIG_B2_* (Backblaze B2 native or S3 remote)
    """
    env = os.environ.copy()

    sa_file = target.get("service_account_file") or settings.google_service_account_file
    env["RCLONE_CONFIG_GDRIVE_TYPE"] = "drive"
    env["RCLONE_CONFIG_GDRIVE_SCOPE"] = "drive.readonly"
    env["RCLONE_CONFIG_GDRIVE_SERVICE_ACCOUNT_FILE"] = sa_file

    folder_id = target.get("drive_folder_id", "").strip()
    if target.get("is_shared_drive"):
        env["RCLONE_CONFIG_GDRIVE_TEAM_DRIVE"] = folder_id
    elif folder_id and folder_id.lower() != "root":
        env["RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID"] = folder_id

    impersonate = target.get("impersonate_user", "").strip()
    if impersonate:
        env["RCLONE_CONFIG_GDRIVE_IMPERSONATE"] = impersonate

    # Backblaze B2 configuration (supports either S3-compatible endpoint or native B2)
    if settings.b2_s3_endpoint:
        env["RCLONE_CONFIG_B2_TYPE"] = "s3"
        env["RCLONE_CONFIG_B2_PROVIDER"] = "Other"
        env["RCLONE_CONFIG_B2_ACCESS_KEY_ID"] = settings.b2_key_id
        env["RCLONE_CONFIG_B2_SECRET_ACCESS_KEY"] = settings.b2_application_key
        env["RCLONE_CONFIG_B2_ENDPOINT"] = settings.b2_s3_endpoint
    else:
        env["RCLONE_CONFIG_B2_TYPE"] = "b2"
        env["RCLONE_CONFIG_B2_ACCOUNT"] = settings.b2_key_id
        env["RCLONE_CONFIG_B2_KEY"] = settings.b2_application_key
        env["RCLONE_CONFIG_B2_HARD_DELETE"] = "false"

    return env


def build_rclone_sync_cmd(
    target: dict[str, Any], snapshot_id: str, dry_run: bool = False
) -> list[str]:
    bucket = settings.b2_bucket.strip("/")
    prefix = target["b2_prefix"].strip("/")
    current_dest = f"b2:{bucket}/{prefix}/current"
    archive_dest = f"b2:{bucket}/{prefix}/snapshots/{snapshot_id}"

    export_formats = target.get("export_formats") or settings.gdrive_export_formats
    max_delete = int(target.get("max_delete", settings.max_delete_threshold))

    cmd = [
        "rclone",
        "sync",
        "gdrive:",
        current_dest,
        "--backup-dir",
        archive_dest,
        "--drive-export-formats",
        export_formats,
        "--fast-list",
        "--transfers",
        str(settings.rclone_transfers),
        "--checkers",
        str(settings.rclone_checkers),
        "--use-json-log",
        "--stats",
        "5s",
        "--stats-log-level",
        "INFO",
    ]

    if max_delete > 0:
        cmd.extend(["--max-delete", str(max_delete)])

    if dry_run:
        cmd.append("--dry-run")

    return cmd


def parse_rclone_json_logs(raw_output: str) -> dict[str, int]:
    """Extract final transfer/check/delete counters from rclone JSON stats."""
    stats = {
        "files_transferred": 0,
        "files_checked": 0,
        "files_deleted": 0,
        "bytes_transferred": 0,
    }
    for line in raw_output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        stat_block = payload.get("stats")
        if isinstance(stat_block, dict):
            stats["files_transferred"] = int(stat_block.get("transfers", stats["files_transferred"]))
            stats["files_checked"] = int(stat_block.get("checks", stats["files_checked"]))
            stats["files_deleted"] = int(stat_block.get("deletes", stats["files_deleted"]))
            stats["bytes_transferred"] = int(stat_block.get("bytes", stats["bytes_transferred"]))
    return stats


async def send_alert_webhook(message: str) -> None:
    if not settings.alert_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                settings.alert_webhook_url,
                json={"text": message, "content": message},
            )
    except Exception as exc:
        logger.warning("Failed to send alert webhook: %s", exc)


async def execute_backup_target(target_id: int, dry_run: bool = False) -> int:
    target = get_target(target_id)
    if not target:
        raise ValueError(f"Target {target_id} not found")

    lock = get_target_lock(target_id)
    if lock.locked():
        raise RuntimeError(f"Backup for target '{target['name']}' is already running")

    snapshot_id = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    run_id = create_run(target_id=target_id, snapshot_id=snapshot_id, dry_run=dry_run)

    async with lock:
        start_time = time.monotonic()
        cmd = build_rclone_sync_cmd(target, snapshot_id=snapshot_id, dry_run=dry_run)
        env = build_rclone_env(target)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            stdout_bytes, _ = await proc.communicate()
            output_text = stdout_bytes.decode("utf-8", errors="replace")
            duration = time.monotonic() - start_time
            parsed = parse_rclone_json_logs(output_text)

            if proc.returncode == 0:
                finish_run(
                    run_id=run_id,
                    target_id=target_id,
                    status="success",
                    files_transferred=parsed["files_transferred"],
                    files_checked=parsed["files_checked"],
                    files_deleted=parsed["files_deleted"],
                    bytes_transferred=parsed["bytes_transferred"],
                    duration_seconds=duration,
                    log_output=output_text[-20000:],
                )
            else:
                err_msg = f"rclone exited with code {proc.returncode}"
                finish_run(
                    run_id=run_id,
                    target_id=target_id,
                    status="failed",
                    files_transferred=parsed["files_transferred"],
                    files_checked=parsed["files_checked"],
                    files_deleted=parsed["files_deleted"],
                    bytes_transferred=parsed["bytes_transferred"],
                    duration_seconds=duration,
                    error_message=err_msg,
                    log_output=output_text[-20000:],
                )
                await send_alert_webhook(
                    f"❌ [GDrive Backup Failed] Target: {target['name']} ({err_msg})"
                )
        except Exception as exc:
            duration = time.monotonic() - start_time
            finish_run(
                run_id=run_id,
                target_id=target_id,
                status="failed",
                duration_seconds=duration,
                error_message=str(exc),
                log_output=str(exc),
            )
            await send_alert_webhook(
                f"❌ [GDrive Backup Exception] Target: {target['name']} ({exc})"
            )

    return run_id


async def browse_b2_files(target_id: int, subpath: str = "current") -> list[dict[str, Any]]:
    """List files or snapshots inside the B2 bucket prefix for a target via `rclone lsjson`."""
    target = get_target(target_id)
    if not target:
        raise ValueError(f"Target {target_id} not found")

    bucket = settings.b2_bucket.strip("/")
    prefix = target["b2_prefix"].strip("/")
    clean_sub = subpath.strip("/")
    remote_path = f"b2:{bucket}/{prefix}/{clean_sub}" if clean_sub else f"b2:{bucket}/{prefix}"

    cmd = ["rclone", "lsjson", remote_path]
    env = build_rclone_env(target)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        err = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"rclone lsjson failed: {err}")

    return json.loads(stdout_bytes.decode("utf-8", errors="replace"))
