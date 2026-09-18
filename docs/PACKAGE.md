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
Default 20000, the tracker's `tracking_max_faces`. The worker re-tessellates coarser until it fits,
and says so in `warnings` when that made curved outlines polygonal. Re-tessellating follows the true
surfaces; the tracker's own decimation (its emergency guard) can only guess.

## Warnings that need a human
- `possibly hidden` on a part: its box sits inside another part's. A heuristic - review before
  trusting, never removed automatically. Invisible geometry drags the tracker's pose.
- `tessellation had to go coarse`: consider a higher budget or removing small features.
