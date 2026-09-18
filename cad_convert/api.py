"""The conversion API. Upload a CAD file, get a model package back.

    uvicorn cad_convert.api:app --host 127.0.0.1 --port 8200

Internal today, shaped for a commercial service later:
  * every conversion runs in a WORKER SUBPROCESS with memory, CPU and output-size limits and a wall
    clock timeout - a CAD file is untrusted input to a large C++ parser, and a crash or a runaway
    tessellation must cost one job, never the service;
  * the JT worker is a separate executable (GPL); this process only starts it and reads its files,
    which is what keeps the GPL out of everything we distribute (see README);
  * an API key is enforced when CAD_CONVERT_API_KEY is set, and jobs expire.

Configuration (environment):
  CAD_CONVERT_JOBS          job directory                        (default ./jobs)
  CAD_CONVERT_API_KEY       required X-API-Key when set           (default: none, bind to localhost)
  CAD_CONVERT_JT_WORKER     path to the jt2pkg executable         (default: vendor/jt/build/jt2pkg)
  CAD_CONVERT_MAX_UPLOAD_MB (default 512)   CAD_CONVERT_TIMEOUT_S (default 900)
  CAD_CONVERT_MEM_GB        (default 6)     CAD_CONVERT_WORKERS   (default 2)
  CAD_CONVERT_KEEP_HOURS    (default 24)
"""
from __future__ import annotations

import io
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
JOBS = Path(os.environ.get("CAD_CONVERT_JOBS", ROOT / "jobs"))
API_KEY = os.environ.get("CAD_CONVERT_API_KEY", "")
JT_WORKER = Path(os.environ.get("CAD_CONVERT_JT_WORKER", ROOT / "workers" / "jt" / "build" / "jt2pkg"))
MAX_UPLOAD = int(os.environ.get("CAD_CONVERT_MAX_UPLOAD_MB", "512")) * 1024 * 1024
TIMEOUT_S = int(os.environ.get("CAD_CONVERT_TIMEOUT_S", "900"))
MEM_BYTES = int(float(os.environ.get("CAD_CONVERT_MEM_GB", "6")) * 1024 ** 3)
WORKERS = int(os.environ.get("CAD_CONVERT_WORKERS", "2"))
KEEP_S = float(os.environ.get("CAD_CONVERT_KEEP_HOURS", "24")) * 3600
OUTPUT_CAP = 2 * 1024 ** 3        # no single output file larger than this

JOBS.mkdir(parents=True, exist_ok=True)
_pool = ThreadPoolExecutor(max_workers=WORKERS)

app = FastAPI(title="cad_convert", version="0.1.0",
              description="CAD (STEP, JT) -> OBJ model packages")


# --- formats ------------------------------------------------------------------------------------

def sniff(head: bytes) -> tuple[str, str]:
    """(format, detail) from the first bytes. The CONTENT decides, not the extension: a .stp
    renamed .txt is still STEP, and a .jt that is really a zip is not JT."""
    text = head[:120].decode("latin-1", "replace")
    if text.lstrip().startswith("ISO-10303-21"):
        return "step", ""
    m = re.match(r"Version (\d+\.\d+)\s+JT", text)
    if m:
        return "jt", m.group(1)
    return "unknown", text[:40]


def jt_available() -> bool:
    return JT_WORKER.is_file() and os.access(JT_WORKER, os.X_OK)


# --- jobs ---------------------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "no such job")
    d = JOBS / job_id
    if not (d / "job.json").exists():
        raise HTTPException(404, "no such job")
    return d


def _read(d: Path) -> dict:
    return json.loads((d / "job.json").read_text())


def _write(d: Path, job: dict) -> None:
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(d / "job.json")                 # atomic: a reader never sees half a file


def _limits():
    """Runs in the child before exec. Address space bounds memory (a tessellation that explodes
    fails with MemoryError instead of swapping the machine to death - see cap-memory notes); CPU
    time bounds a spin; FSIZE bounds a writer gone wrong."""
    resource.setrlimit(resource.RLIMIT_AS, (MEM_BYTES, MEM_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_S, TIMEOUT_S + 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (OUTPUT_CAP, OUTPUT_CAP))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _run(job_id: str) -> None:
    d = JOBS / job_id
    job = _read(d)
    job.update(status="running", started=time.time())
    _write(d, job)
    src, out = d / "input" / job["filename"], d / "package"
    if job["format"] == "step":
        cmd = [sys.executable, "-m", "cad_convert.step_worker", str(src), str(out)]
    else:
        cmd = [str(JT_WORKER), str(src), str(out)]
    cmd += ["--max-triangles", str(job["max_triangles"])]
    try:
        # A new session, so a timeout kills the whole process GROUP - subprocess.run's own timeout
        # kills only the direct child and would leave anything it spawned running.
        p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, preexec_fn=_limits, start_new_session=True)
        try:
            stdout, stderr = p.communicate(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.communicate()
            raise
        job["log"] = (stdout + stderr)[-8000:]
        if p.returncode == 0 and (out / "report.json").exists():
            job["status"] = "done"
            job["report"] = json.loads((out / "report.json").read_text())
        else:
            job["status"] = "failed"
            job["error"] = _worker_error(p.returncode, stderr)
    except subprocess.TimeoutExpired:
        job["status"] = "failed"
        job["error"] = f"timed out after {TIMEOUT_S} s"
    except Exception as e:                       # the service survives whatever the worker did
        job["status"] = "failed"
        job["error"] = f"{type(e).__name__}: {e}"
    job["finished"] = time.time()
    _write(d, job)


def _worker_error(code: int, stderr: str) -> str:
    for line in reversed(stderr.strip().splitlines()):
        try:
            return json.loads(line)["error"]
        except Exception:
            continue
    if code < 0:
        return f"worker killed by signal {signal.Signals(-code).name} (memory or CPU limit?)"
    return f"worker exited {code}: {stderr.strip()[-300:]}"


def _cleanup() -> None:
    now = time.time()
    for d in JOBS.iterdir():
        try:
            if d.is_dir() and now - (d / "job.json").stat().st_mtime > KEEP_S:
                shutil.rmtree(d, ignore_errors=True)
        except FileNotFoundError:
            pass


# --- auth ---------------------------------------------------------------------------------------

def _auth(x_api_key: str | None = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "missing or wrong X-API-Key")


# --- routes -------------------------------------------------------------------------------------

@app.get("/v1/health")
def health():
    return {"ok": True, "jt_worker": jt_available(), "workers": WORKERS}


@app.get("/v1/formats", dependencies=[Depends(_auth)])
def formats():
    return {"step": {"available": True, "extensions": [".step", ".stp"]},
            "jt": {"available": jt_available(), "extensions": [".jt"]}}


@app.post("/v1/convert", status_code=202, dependencies=[Depends(_auth)])
async def convert(file: UploadFile = File(...), max_triangles: int = Form(20000)):
    _cleanup()
    name = os.path.basename(file.filename or "model")
    # Keep the base name - it is part of the tracker's model identity - but nothing a shell or a
    # path could misread.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "model"
    job_id = uuid.uuid4().hex
    d = JOBS / job_id
    (d / "input").mkdir(parents=True)
    dst = d / "input" / name
    size = 0
    with open(dst, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"larger than {MAX_UPLOAD >> 20} MB")
            f.write(chunk)
    with open(dst, "rb") as f:
        fmt, detail = sniff(f.read(256))
    if fmt == "unknown":
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(415, "not a STEP or JT file (content starts with "
                                 f"{detail!r})")
    if fmt == "jt" and not jt_available():
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(503, "JT conversion is not installed on this server")
    job = {"id": job_id, "status": "queued", "format": fmt, "format_version": detail,
           "filename": name, "bytes": size, "max_triangles": max(0, int(max_triangles)),
           "created": time.time()}
    _write(d, job)
    _pool.submit(_run, job_id)
    return {"id": job_id, "status": "queued", "format": fmt, "format_version": detail,
            "status_url": f"/v1/jobs/{job_id}"}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(_auth)])
def job_status(job_id: str):
    job = _read(_job_dir(job_id))
    if job["status"] == "done":
        job["package_url"] = f"/v1/jobs/{job_id}/package.zip"
    return job


@app.get("/v1/jobs/{job_id}/package.zip", dependencies=[Depends(_auth)])
def job_package(job_id: str):
    d = _job_dir(job_id)
    job = _read(d)
    if job["status"] != "done":
        raise HTTPException(409, f"job is {job['status']}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted((d / "package").iterdir()):
            z.write(p, p.name)
    buf.seek(0)
    base = os.path.splitext(job["filename"])[0]
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{base}.zip"'})


@app.get("/v1/jobs/{job_id}/files/{name}", dependencies=[Depends(_auth)])
def job_file(job_id: str, name: str):
    d = _job_dir(job_id)
    p = (d / "package" / name).resolve()
    if p.parent != (d / "package").resolve() or not p.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(p)


@app.delete("/v1/jobs/{job_id}", dependencies=[Depends(_auth)])
def job_delete(job_id: str):
    d = _job_dir(job_id)
    if _read(d)["status"] in ("queued", "running"):
        raise HTTPException(409, "job is still running")
    shutil.rmtree(d, ignore_errors=True)
    return {"deleted": job_id}
