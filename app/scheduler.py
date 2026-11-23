import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import list_targets
from app.rclone_runner import execute_backup_target

logger = logging.getLogger("gdrive_backup.scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")


async def _scheduled_job(target_id: int) -> None:
    try:
        await execute_backup_target(target_id=target_id, dry_run=False)
    except Exception as exc:
        logger.error("Scheduled backup failed for target %s: %s", target_id, exc)


def reload_schedules() -> None:
    scheduler.remove_all_jobs()
    targets = list_targets()
    for target in targets:
        if not target.get("enabled"):
            continue
        cron_expr = (target.get("schedule_cron") or "0 2 * * *").strip()
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone="UTC")
            scheduler.add_job(
                _scheduled_job,
                trigger=trigger,
                args=[target["id"]],
                id=f"target_{target['id']}",
                replace_existing=True,
            )
            logger.info(
                "Scheduled target '%s' (#%s) with cron '%s'",
                target["name"],
                target["id"],
                cron_expr,
            )
        except Exception as exc:
            logger.warning(
                "Invalid cron '%s' for target '%s': %s",
                cron_expr,
                target["name"],
                exc,
            )
