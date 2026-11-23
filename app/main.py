import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.config import settings
from app.db import (
    create_target,
    delete_target,
    get_run,
    get_target,
    init_db,
    list_runs,
    list_targets,
)
from app.rclone_runner import browse_b2_files, execute_backup_target
from app.scheduler import reload_schedules, scheduler

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    scheduler.start()
    reload_schedules()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="GDrive -> Backblaze B2 Backup Service",
    version="0.1.0",
    lifespan=lifespan,
)


class TargetCreateInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    drive_folder_id: str = Field(..., min_length=1)
    is_shared_drive: bool = False
    impersonate_user: str = ""
    service_account_file: str = ""
    b2_prefix: str = Field(..., min_length=1)
    schedule_cron: str = "0 2 * * *"
    export_formats: str = "docx,xlsx,pptx"
    max_delete: int = 500
    enabled: bool = True


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    targets = list_targets()
    runs = list_runs(limit=25)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "targets": targets,
            "runs": runs,
            "b2_bucket": settings.b2_bucket or "(not configured)",
            "b2_configured": bool(settings.b2_key_id and settings.b2_application_key and settings.b2_bucket),
            "default_sa_file": settings.google_service_account_file,
        },
    )


@app.post("/ui/targets")
async def ui_create_target(
    name: str = Form(...),
    drive_folder_id: str = Form(...),
    b2_prefix: str = Form(...),
    schedule_cron: str = Form("0 2 * * *"),
    export_formats: str = Form("docx,xlsx,pptx"),
    max_delete: int = Form(500),
    impersonate_user: str = Form(""),
    is_shared_drive: bool = Form(False),
) -> RedirectResponse:
    create_target(
        {
            "name": name.strip(),
            "drive_folder_id": drive_folder_id.strip(),
            "b2_prefix": b2_prefix.strip(),
            "schedule_cron": schedule_cron.strip(),
            "export_formats": export_formats.strip(),
            "max_delete": max_delete,
            "impersonate_user": impersonate_user.strip(),
            "is_shared_drive": is_shared_drive,
            "enabled": True,
        }
    )
    reload_schedules()
    return RedirectResponse(url="/", status_code=303)


@app.post("/ui/targets/{target_id}/run")
async def ui_trigger_target(
    target_id: int,
    background_tasks: BackgroundTasks,
    dry_run: bool = Form(False),
) -> RedirectResponse:
    target = get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    background_tasks.add_task(execute_backup_target, target_id, dry_run)
    await asyncio.sleep(0.1)
    return RedirectResponse(url="/", status_code=303)


@app.post("/ui/targets/{target_id}/delete")
async def ui_delete_target(target_id: int) -> RedirectResponse:
    delete_target(target_id)
    reload_schedules()
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/targets")
async def api_list_targets() -> list[dict]:
    return list_targets()


@app.post("/api/targets", status_code=201)
async def api_create_target(payload: TargetCreateInput) -> dict:
    target_id = create_target(payload.model_dump())
    reload_schedules()
    return {"id": target_id, "status": "created"}


@app.post("/api/targets/{target_id}/run")
async def api_run_target(
    target_id: int, background_tasks: BackgroundTasks, dry_run: bool = False
) -> dict:
    target = get_target(target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    background_tasks.add_task(execute_backup_target, target_id, dry_run)
    return {"status": "triggered", "target_id": target_id, "dry_run": dry_run}


@app.get("/api/runs")
async def api_list_runs(limit: int = 30, target_id: int | None = None) -> list[dict]:
    return list_runs(limit=limit, target_id=target_id)


@app.get("/api/runs/{run_id}")
async def api_get_run(run_id: int) -> dict:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/targets/{target_id}/browse")
async def api_browse_b2(target_id: int, subpath: str = "current") -> list[dict]:
    try:
        return await browse_b2_files(target_id=target_id, subpath=subpath)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    targets = list_targets()
    runs = list_runs(limit=200)

    lines = [
        "# HELP gdrive_backup_target_status Last backup status per target (1=success, 0=other)",
        "# TYPE gdrive_backup_target_status gauge",
    ]
    for t in targets:
        val = 1 if t.get("last_status") == "success" else 0
        lines.append(
            f'gdrive_backup_target_status{{target_id="{t["id"]}",target_name="{t["name"]}"}} {val}'
        )

    lines.extend(
        [
            "# HELP gdrive_backup_last_files_transferred Files transferred in most recent run per target",
            "# TYPE gdrive_backup_last_files_transferred gauge",
            "# HELP gdrive_backup_last_bytes_transferred Bytes transferred in most recent run per target",
            "# TYPE gdrive_backup_last_bytes_transferred gauge",
            "# HELP gdrive_backup_last_duration_seconds Duration of most recent run per target",
            "# TYPE gdrive_backup_last_duration_seconds gauge",
        ]
    )

    seen_targets: set[int] = set()
    for r in runs:
        tid = r["target_id"]
        if tid in seen_targets:
            continue
        seen_targets.add(tid)
        tname = r["target_name"]
        lines.append(
            f'gdrive_backup_last_files_transferred{{target_id="{tid}",target_name="{tname}"}} {r.get("files_transferred") or 0}'
        )
        lines.append(
            f'gdrive_backup_last_bytes_transferred{{target_id="{tid}",target_name="{tname}"}} {r.get("bytes_transferred") or 0}'
        )
        lines.append(
            f'gdrive_backup_last_duration_seconds{{target_id="{tid}",target_name="{tname}"}} {r.get("duration_seconds") or 0.0}'
        )

    return PlainTextResponse("\n".join(lines) + "\n")
