# cad2obj

Converts CAD files (STEP, JT) into a mesh package: OBJ + MTL in metres, one material per part,
sub-assemblies kept as part groups, with each part's name, placement and colour. Made for
model-based object tracking, usable by anything that reads OBJ. A command-line converter and an
HTTP API.

**Status:** STEP worker working (`cad2obj/step_worker.py`). API front working
(`cad2obj/api.py`). JT worker working for tessellated JT v8/v9
(`workers/jt/jt2pkg`, GPL, built on the conversion host only). 16/16 tests passing.

## Layout
- `cad2obj/step_worker.py` - STEP (AP203/214/242) via Open CASCADE. CLI and library.
- `cad2obj/api.py` - FastAPI front: upload a CAD file, poll the job, download the
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
    .venv/bin/python -m cad2obj.step_worker part.step out/part
    .venv/bin/python -m pytest -q tests

Options: `--max-triangles N` (default 20000, 0 = none), `--lin-rel`, `--ang-deg`.

## License
cad2obj is free software under the **GNU General Public License v3.0 or later** (see `LICENSE`).

Third-party parts, and what they ask:
- **STEP worker:** Open CASCADE is LGPL-2.1 with an exception, OCP is Apache-2.0.
- **JT worker:** compiles in the open JT reader (TKJT from PyOpenJt / Open CASCADE JT Assistant),
  **GPL-2.0-or-later** - one reason this repository is GPL.
- Programs that only talk to cad2obj over HTTP or read its output files are not linked to it,
  and are not covered by its license.
- Rule for new dependencies: nothing AGPL, nothing GPL-incompatible.

## Security
Every conversion parses an untrusted file with a large C++ library. Workers run in their own
container with no network, capped memory/CPU/pids and a time limit; inputs are read-only and
deleted after the job.
