#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, validator
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import uvicorn

DATA_DIR = Path(os.environ.get("BACKUP_SERVER_DATA", ".")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "backups.json"
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Mini Backup Server")
scheduler = BackgroundScheduler(timezone="Europe/Berlin")
scheduler.start()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# ---------- Models ----------

class JobIn(BaseModel):
    source: str = Field(..., description="Quellverzeichnis")
    target: str = Field(..., description="Zielverzeichnis")
    cron: str = Field(..., description="Crontab-Expression z.B. '0 2 * * *' für täglich 02:00")
    enabled: bool = True

    @validator("source", "target")
    def must_be_abs_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("Pfad muss absolut sein (beginnt mit /).")
        return v

class Job(JobIn):
    id: str
    last_run: Optional[str] = None
    last_result: Optional[str] = None
    last_change_detected: Optional[bool] = None

# ---------- Storage ----------

def load_jobs() -> Dict[str, Job]:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        jobs = {jid: Job(**data) for jid, data in raw.items()}
    else:
        jobs = {}
    return jobs

def save_jobs(jobs: Dict[str, Job]) -> None:
    serializable = {jid: job.dict() for jid, job in jobs.items()}
    tmp = DATA_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
    tmp.replace(DATA_FILE)

JOBS: Dict[str, Job] = load_jobs()

# ---------- Rsync helpers ----------

def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)

def _rsync_base_args() -> List[str]:
    # -a (archive), --delete (Ziel in Sync halten), -x optional wenn man Dateisystemgrenzen nicht überschreiten will
    return ["rsync", "-a", "--delete"]

def rsync_has_changes(src: str, dst: str) -> bool:
    ensure_dir(dst)
    cmd = _rsync_base_args() + ["--dry-run", "--itemize-changes", f"{src.rstrip('/')}/", f"{dst.rstrip('/')}/"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode not in (0, 23, 24):
        # 23/24 sind häufig harmlose Warncodes (z.B. vanished files). Nur bei anderen Codes hart abbrechen.
        raise RuntimeError(f"rsync dry-run Fehler: {res.stderr.strip()}")
    # Wenn es irgendeine Ausgabe gibt, gab es Änderungen
    return bool(res.stdout.strip())

def run_rsync(src: str, dst: str, job_id: str) -> str:
    ensure_dir(dst)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = LOG_DIR / f"{job_id}-{timestamp}.log"
    cmd = _rsync_base_args() + [f"{src.rstrip('/')}/", f"{dst.rstrip('/')}/"]
    with open(logfile, "w", encoding="utf-8") as lf:
        lf.write(f"# {datetime.now().isoformat()} rsync {src} -> {dst}\n\n")
        proc = subprocess.run(cmd, stdout=lf, stderr=lf, text=True)
        rc = proc.returncode
    if rc not in (0, 23, 24):
        return f"FEHLER (rc={rc}) – Details: {logfile}"
    return f"OK – Log: {logfile}"

# ---------- Scheduler ----------

def schedule_job(job: Job):
    # Ersatz falls ungültige Cron-Expression: 400 bei API-Aufruf
    trigger = CronTrigger.from_crontab(job.cron, timezone="Europe/Berlin")
    scheduler.add_job(
        func=execute_job,
        trigger=trigger,
        args=[job.id],
        id=job.id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

def unschedule_job(job_id: str):
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

def execute_job(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.enabled:
        return
    try:
        changed = rsync_has_changes(job.source, job.target)
        if changed:
            result = run_rsync(job.source, job.target, job_id)
        else:
            result = "Keine Änderungen – übersprungen"
        job.last_run = datetime.now().isoformat(timespec="seconds")
        job.last_result = result
        job.last_change_detected = changed
        save_jobs(JOBS)
    except Exception as e:
        job.last_run = datetime.now().isoformat(timespec="seconds")
        job.last_result = f"FEHLER: {e}"
        job.last_change_detected = None
        save_jobs(JOBS)

# (Re-)Schedule bestehender Jobs beim Start
for j in JOBS.values():
    if j.enabled:
        try:
            schedule_job(j)
        except Exception as e:
            j.last_result = f"FEHLER beim Planen: {e}"

# ---------- API ----------

CRON_LABELS = ["Minute", "Hour", "Day (Month)", "Month", "Day (Week)"]


def _cron_breakdown(cron_expression: str):
    parts = cron_expression.split()
    if len(parts) < 5:
        parts.extend(["*"] * (5 - len(parts)))
    fields = parts[:5]
    return [
        {"label": label, "value": value}
        for label, value in zip(CRON_LABELS, fields)
    ]


@app.get("/")
def index(request: Request):
    jobs = []
    for job in JOBS.values():
        jobs.append({
            "job": job,
            "cron_details": _cron_breakdown(job.cron),
        })
    context = {
        "request": request,
        "jobs": jobs,
    }
    return templates.TemplateResponse("index.html", context)

@app.get("/jobs", response_model=List[Job])
def list_jobs():
    return list(JOBS.values())

@app.post("/jobs", response_class=Response)
async def create_job(request: Request):
    form = await request.form()
    payload = {
        "source": form.get("source"),
        "target": form.get("target"),
        "cron": form.get("cron"),
        "enabled": True
    }
    try:
        job_in = JobIn(**payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not Path(job_in.source).exists():
        raise HTTPException(status_code=400, detail="Quelle existiert nicht.")
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, **job_in.dict())
    JOBS[job_id] = job
    save_jobs(JOBS)
    try:
        schedule_job(job)
    except Exception as e:
        job.last_result = f"FEHLER beim Planen: {e}"
        save_jobs(JOBS)
        raise HTTPException(status_code=400, detail=str(e))
    return Response(status_code=303, headers={"Location": "/"})

@app.post("/jobs/{job_id}/toggle")
def toggle_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    job.enabled = not job.enabled
    if job.enabled:
        schedule_job(job)
    else:
        unschedule_job(job_id)
    save_jobs(JOBS)
    return Response(status_code=303, headers={"Location": "/"})

@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: str):
    job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    unschedule_job(job_id)
    save_jobs(JOBS)
    return Response(status_code=303, headers={"Location": "/"})

@app.post("/jobs/{job_id}/run")
def run_now(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    # synchron ausführen
    execute_job(job_id)
    return Response(status_code=303, headers={"Location": "/"})

@app.put("/jobs/{job_id}", response_model=Job)
def update_job(job_id: str, body: JobIn):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job nicht gefunden.")
    job.source = body.source
    job.target = body.target
    job.cron = body.cron
    job.enabled = body.enabled
    save_jobs(JOBS)
    if job.enabled:
        schedule_job(job)
    else:
        unschedule_job(job_id)
    return job

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=False)