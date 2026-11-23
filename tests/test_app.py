import json
from app.rclone_runner import build_rclone_env, build_rclone_sync_cmd, parse_rclone_json_logs


def test_build_rclone_sync_cmd() -> None:
    target = {
        "name": "Company Shared Drive",
        "drive_folder_id": "0A1B2C3D4E",
        "is_shared_drive": 1,
        "b2_prefix": "gdrive/company-shared",
        "export_formats": "docx,xlsx,pptx",
        "max_delete": 500,
    }
    cmd = build_rclone_sync_cmd(target, snapshot_id="2026-11-23_22-00-00", dry_run=True)
    assert cmd[0:3] == ["rclone", "sync", "gdrive:"]
    assert "--backup-dir" in cmd
    assert "--fast-list" in cmd
    assert "--max-delete" in cmd
    assert "--dry-run" in cmd


def test_build_rclone_env_readonly() -> None:
    target = {
        "name": "Folder Target",
        "drive_folder_id": "12345folder",
        "is_shared_drive": 0,
        "impersonate_user": "user@example.com",
    }
    env = build_rclone_env(target)
    assert env["RCLONE_CONFIG_GDRIVE_TYPE"] == "drive"
    assert env["RCLONE_CONFIG_GDRIVE_SCOPE"] == "drive.readonly"
    assert env["RCLONE_CONFIG_GDRIVE_ROOT_FOLDER_ID"] == "12345folder"
    assert env["RCLONE_CONFIG_GDRIVE_IMPERSONATE"] == "user@example.com"


def test_parse_rclone_json_logs() -> None:
    sample_logs = "\n".join(
        [
            json.dumps({"level": "info", "msg": "Starting sync"}),
            json.dumps(
                {
                    "level": "info",
                    "msg": "Transferred",
                    "stats": {
                        "transfers": 42,
                        "checks": 7120,
                        "deletes": 3,
                        "bytes": 104857600,
                    },
                }
            ),
        ]
    )
    stats = parse_rclone_json_logs(sample_logs)
    assert stats["files_transferred"] == 42
    assert stats["files_checked"] == 7120
    assert stats["files_deleted"] == 3
    assert stats["bytes_transferred"] == 104857600
