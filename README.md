# cad_convert

CAD -> OBJ model packages. Internal first, built to become a commercial API.

**Status:** STEP worker working (`cad_convert/step_worker.py`). API front working
(`cad_convert/api.py`). JT worker working for tessellated JT v8/v9
(`workers/jt/jt2pkg`, GPL, built on the conversion host only). 16/16 tests passing.

## Layout
- `cad_convert/step_worker.py` - STEP (AP203/214/242) via Open CASCADE. CLI and library.
- `cad_convert/api.py` - FastAPI front: upload a CAD file, poll the job, download the
  package. Each conversion runs in an isolated worker subprocess (memory/CPU/time
  limits). No Docker: sandboxing is process-level for now.
- `workers/jt/` - JT worker sources (`jt2pkg.cpp`, `jtinfo.cpp`, `CMakeLists.txt`) plus
  build/run notes. The binaries are GPL and git-ignored; build on the conversion host.
- `docs/PACKAGE.md` - the output format every worker writes.
- `docs/VENDOR.md` - third-party sources for the JT worker (git-ignored, local only).
- `tests/` - STEP assembly generated with known contents; JT tests run when `jt2pkg`
  is built and a sample file is present, otherwise skipped.

## Run
    python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m cad_convert.step_worker part.step out/part
    .venv/bin/python -m pytest -q tests

Options: `--max-triangles N` (default 20000, 0 = none), `--lin-rel`, `--ang-deg`.

## Licensing rules - read before adding a dependency
- **STEP worker:** Open CASCADE is LGPL-2.1 with an exception, OCP is Apache-2.0. Safe to host and
  safe to distribute (dynamically linked, licence text included).
- **JT worker (future):** the open JT reader (Open CASCADE TKJT / JT Assistant) is **GPL**. It may
  run behind the API; it must **never** be imported by, linked into or shipped with anything we
  distribute (the tracker, the app, the editor, a client tool). Its own image, its own process.
- Nothing AGPL, anywhere.

## Security
Every conversion parses an untrusted file with a large C++ library. Workers run in their own
container with no network, capped memory/CPU/pids and a time limit; inputs are read-only and
deleted after the job.
