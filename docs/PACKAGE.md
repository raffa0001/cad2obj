# The model package (v1)

What every worker (STEP now, JT later) writes into its output directory. A tracker, an
editor or a device's model library can consume it as-is. `<name>` is the source file's base name - it
is part of the tracker's model identity (`<file>:shape:WxHxD`), so it is never renamed.

| file | contents |
|---|---|
| `<name>.obj` | Triangles in **metres**. One `o`/`usemtl` group per part, in assembly order. Welded per part. |
| `<name>.mtl` | One `newmtl part_NNNN` per part, `Kd` = the part's colour; the part's path in a comment. |
| `<name>.settings` | Tracker settings read beside the mesh (`name = value`). v1: `world_scale_known_1000x = 1000` - the mesh is metric, trust it. Every name must be a real tracker setting: the tracker refuses a load with an unknown one. |
| `report.json` | What the file contained and what was done: declared units, size, tessellation passes, per-part name / path / triangles / colour / bbox / warnings, timings. |

## Parts
A part is one PLACED occurrence of a non-assembly shape: a bolt used six times is six parts
(`.../bolt#1` ... `#6`), because each can be present or missing. Part names prefer the product
name; an instance name is used when it carries meaning (OCCT's placeholder `=>[0:1:1:2]` does not).

## Triangle budget
Default 100000 (the client may send another; 0 = no budget). The worker re-tessellates coarser until
it fits - each edge's deflection relative to its own size, never past 10% / 30 deg - and says so in
`warnings` when that made curved outlines polygonal. The same number goes into `<model>.settings` as
`tracking_max_faces`, the model's own face ceiling, so the tracker keeps what was tessellated instead
of decimating to its 20000 default (at 20000 a 1148-part assembly kept 596 parts; at 100000, 1128). Re-tessellating follows the true
surfaces; the tracker's own decimation (its emergency guard) can only guess.
Field result (Sep 2026, Hand Drill at ~76k tris and coarser): the tracker decimates hard on
these packages and tracking stays healthy - hitting the budget exactly is not required for beta.

## Warnings that need a human
- `possibly hidden` on a part: its box sits inside another part's. A heuristic - review before
  trusting, never removed automatically. Invisible geometry drags the tracker's pose.
- `tessellation had to go coarse`: consider a higher budget or removing small features.
