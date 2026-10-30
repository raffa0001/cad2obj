# cad2obj

Converts CAD files (STEP, JT) into a mesh package: OBJ + MTL in metres, one material per part,
sub-assemblies kept as part groups, with each part's name, placement and colour. Made for
model-based object tracking, usable by anything that reads OBJ. A command-line converter and an
HTTP API.

## What is in the repository

| path | what it is |
|---|---|
| `cad2obj/` | the Python package |
| `cad2obj/step_worker.py` | STEP (AP203/214/242) -> package, through Open CASCADE. A command and a library. |
| `cad2obj/api.py` | the HTTP API (FastAPI): upload a file, poll the job, download the package. Every conversion runs in its own subprocess with memory, CPU and time limits. |
| `workers/jt/` | the JT converter: a separate C++ program (`jt2pkg`, plus `jtinfo` to inspect a JT file). Optional - you build it yourself, see below. |
| `requirements.txt` | what the converter and the API need to run |
| `requirements-dev.txt` | the same, plus what the tests need (pytest, httpx) |
| `install.sh` / `start.sh` | make the environment / start the API, with plain Python |
| `docs/PACKAGE.md` | the output format |
| `docs/VENDOR.md` | the third-party sources the JT converter is built from |
| `tests/` | tests; they make their own STEP files, the JT ones run only once `jt2pkg` is built |

## Install

Python 3.11 to 3.14 (what the Open CASCADE bindings are built for; tested on 3.12). Pick one:

**With uv**

    uv venv
    uv pip install -r requirements.txt          # or requirements-dev.txt to run the tests

**With plain Python**

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt   # or requirements-dev.txt to run the tests

or `./install.sh`, which does the same (`REQS=requirements-dev.txt ./install.sh` for the tests).

## Convert a STEP file

    uv run --no-project python -m cad2obj.step_worker part.step out/part     # uv
    .venv/bin/python -m cad2obj.step_worker part.step out/part               # plain Python

`out/part/` then holds `part.obj`, `part.mtl`, `part.settings` and `report.json`.

Options:
- `--max-triangles N` - tessellate coarser until the mesh is under N triangles (default 20000, 0 = no limit)
- `--lin-rel F` - chordal deviation as a fraction of the model's size (default 0.001)
- `--ang-deg D` - angular deviation in degrees (default 15)
- `--group-level N` - sub-assemblies this many levels down become part groups (default 2)

## Run the API

    uv run --no-project uvicorn cad2obj.api:app --port 8200                 # uv
    ./start.sh                                                              # plain Python (port 8200, localhost)

`port=9000 host=0.0.0.0 ./start.sh` changes the address. Then:

    curl localhost:8200/v1/health
    curl -F file=@part.step localhost:8200/v1/convert                       # -> {"id": "...", "status": "queued", ...}
    curl localhost:8200/v1/jobs/<id>                                        # until "status": "done"
    curl -o part.zip localhost:8200/v1/jobs/<id>/package.zip

`/v1/convert` also takes `-F max_triangles=N` (default 100000) and `-F group_level=N` (default 2).
The interactive API docs are at `http://localhost:8200/docs`.

Settings, as environment variables:

| variable | default | |
|---|---|---|
| `CAD2OBJ_API_KEY` | none | when set, every request needs the header `X-API-Key: <key>` |
| `CAD2OBJ_JOBS` | `./jobs` | where uploads and packages are kept |
| `CAD2OBJ_JT_WORKER` | `workers/jt/build/jt2pkg` | the JT converter |
| `CAD2OBJ_MAX_UPLOAD_MB` | 512 | largest upload |
| `CAD2OBJ_TIMEOUT_S` | 900 | longest conversion |
| `CAD2OBJ_MEM_GB` | 6 | memory per conversion |
| `CAD2OBJ_WORKERS` | 2 | conversions at once |
| `CAD2OBJ_KEEP_HOURS` | 24 | how long finished jobs are kept |

Without an API key, keep the server on localhost.

## JT files (optional)

JT needs the C++ converter in `workers/jt/`. It is built against a local Open CASCADE 7.8.1 and
the open JT reader; `docs/VENDOR.md` has the exact sources and commands, then:

    cmake -S workers/jt -B workers/jt/build -DCMAKE_BUILD_TYPE=Release
    cmake --build workers/jt/build -j$(nproc)

The API finds `workers/jt/build/jt2pkg` by itself (`/v1/health` says `"jt_worker": true`). It can
also be run alone: `workers/jt/build/jt2pkg part.jt out/part [--assume-units mm|cm|m|in|ft]`.
JT files carry no length unit, so millimetres are assumed unless told otherwise.
`workers/jt/README.md` lists what it reads and what it cannot.

## Tests

    uv pip install -r requirements-dev.txt && uv run --no-project python -m pytest -q tests     # uv
    .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m pytest -q tests        # plain Python

The JT tests are skipped until `jt2pkg` is built.

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
Every conversion parses an untrusted file with a large C++ library. Each one runs in its own
subprocess with capped memory, CPU and time, so a crash or a runaway file costs one job, not the
server. For a public server, also run it in a container with no network and a read-only input.
