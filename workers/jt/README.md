# The JT worker (`jt2pkg`)

JT -> model package, the way `cad_convert/step_worker.py` does STEP -> model package.
GPL-2.0: it links Open CASCADE's TKJT reader, so this binary stays behind the API and is
never shipped with anything (see the repo README licensing rules).

## Build

Needs the vendor tree from `docs/VENDOR.md` (JT-Assistant sources + local OCCT 7.8.1 build):

    cmake -S workers/jt -B workers/jt/build
    cmake --build workers/jt/build -j$(nproc)

The executable is `workers/jt/build/jt2pkg` (git-ignored build dir; the API finds it there
by default, or set `CAD_CONVERT_JT_WORKER`).

## Run

    workers/jt/build/jt2pkg part.jt out/part [--max-triangles N] [--assume-units mm|cm|m|in|ft]

Same package as the STEP worker (`docs/PACKAGE.md`): `<name>.obj` in metres, one material
per placed part, `<name>.settings`, `report.json`. Two JT-specific differences, both stated
in the report:

- **Units are assumed, never declared.** JT stores no length unit; default `--assume-units mm`
  (Siemens NX convention).
- **LODs are authored, not computed.** JT carries fixed tessellations, so the budget loop
  steps down through authored LODs (`tessellation.policy: authored LODs, finest within
  budget`) instead of re-meshing. Range-LOD / LOD switch nodes keep their first (finest)
  alternative only - the alternatives are the same geometry at different viewing distances,
  and exporting all of them would stack overlapping shells.

## Coverage (measured with `jtinfo` on the PyOpenJt sample set, JT v8-v10)

| file | result |
|---|---|
| CoffeeMaker.jt (9.5, monolithic) | 64 parts, 72k tris |
| fishing_reel.jt (8.0, shattered, +12 children) | 17 parts, 9.9k tris (external refs, case-insensitive match, self-identifying roots) |
| fishing_reel children (8.0, XT-BRep + LOD) | convert standalone, mm-plausible sizes |
| example_block 8.1/9.5, opening_protection_plate 8.0/9.5, SampleJT_Server, san2 | **missing children** (`*_nx4.jt`, `san666.jt`, ...) - correctly refused, not guessed |
| Warehouse.jt (8.1), example_block_jt10.3.jt | **reader gap**: the LSG segment is present but TKJT mis-parses it (Warehouse over-reads the segment end; 10.3 fails on an element). Needs spec-level reader work - see below |

`jtinfo file.jt [...]` prints version, segment inventory and an LSG node census without
converting; run it on any new JT collection first.

## Known reader gaps (TKJT, both forks)

- Some LSG variants fail to parse although their segments are present (see table). The
  failure is detected and reported as an error, never as a silent short model.
- XT-BRep-only files (exact Parasolid geometry, no tessellation) have no triangles to
  export; the JtToTopoDS path that could convert them is deliberately not built.
- Never call `JtNode_Partition::Load()`: it copies the child's handle vector with a raw
  memcpy that bypasses OCCT refcounts, freeing the subtree while still referenced
  (segfault). `jt2pkg` opens external files itself and keeps every model handle alive.
