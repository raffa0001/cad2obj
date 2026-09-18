"""STEP -> model package, for a model-based tracker.

Reads a STEP file (AP203 / AP214 / AP242) with Open CASCADE, tessellates it once, and writes the
package described in docs/PACKAGE.md: a mesh in METRES with one material per part, a `.settings`
sidecar the tracker reads beside the mesh, and a report.

Why a package and not "just a mesh": STEP knows things a mesh has lost, and three of them are the
causes of real tracking bugs -
  * units. Every length in the tracker's world maths is metric; a model in the wrong units makes
    every distance the tracker works out wrong by that factor. STEP declares its units,
    so the package states "this model is metric" and the tracker never has to measure it.
  * parts. The assembly tree becomes one material per part, which is how the tracker's OBJ loader
    already groups faces into parts.
  * what the file contained, in a report a person can read before trusting the result.

Licensing: Open CASCADE is LGPL-2.1 with an exception; the OCP bindings are Apache-2.0. This module
imports them and is safe to ship. The JT worker (GPL) must never be imported from here.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict

import numpy as np

import OCP.OCP.collections as _C
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDF import TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.UnitsMethods import UnitsMethods_LengthUnit
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

PACKAGE_VERSION = 1

# Coordinates closer than this (metres) are the same vertex. OCCT triangulates face by face, so
# every edge between two faces is written twice; the tracker welds on load, but a package that
# arrives welded has honest vertex counts in its report.
WELD_M = 1e-7


@dataclass
class Options:
    # Chordal deviation as a fraction of the model's bounding-box diagonal. 0.001 on a 130 mm
    # object is 0.19 mm - finer than any edge the tracker can resolve at arm's length.
    lin_rel: float = 0.001
    # Angular deviation, degrees. Curved silhouettes are what the tracker follows, so this bounds
    # how polygonal a cylinder's outline can look.
    ang_deg: float = 15.0
    # THE TRACKER'S BUDGET. It decimates anything over tracking_max_faces (20000 by default) as an
    # emergency guard and says so: "this is a safety net, not preparation". Measured on the Mac
    # mini: 87130 triangles at the defaults above. Re-tessellating coarser follows the true
    # surfaces; decimating afterwards guesses. 0 = no budget.
    max_triangles: int = 20000


@dataclass
class Part:
    name: str
    path: str
    triangles: int = 0
    vertices: int = 0
    color: tuple = (0.7, 0.7, 0.7)
    bbox_m: list = field(default_factory=list)   # [xmin, ymin, zmin, xmax, ymax, zmax]
    warnings: list = field(default_factory=list)


def _label_name(label: TDF_Label) -> str:
    attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def _file_length_units(reader: STEPCAFControl_Reader) -> list[str]:
    a = _C.Sequence_TCollection_AsciiString()
    b = _C.Sequence_TCollection_AsciiString()
    c = _C.Sequence_TCollection_AsciiString()
    reader.Reader().FileUnits(a, b, c)
    return [a.Value(i + 1).ToCString() for i in range(a.Length())]


def _bbox(shape: TopoDS_Shape) -> list[float]:
    b = Bnd_Box()
    BRepBndLib.Add_s(shape, b)
    if b.IsVoid():
        return []
    lo, hi = b.CornerMin(), b.CornerMax()
    return [lo.X(), lo.Y(), lo.Z(), hi.X(), hi.Y(), hi.Z()]


def _color_of(color_tool, shape: TopoDS_Shape, label: TDF_Label, ref: TDF_Label):
    """Surface colour, else generic colour, from the instance, its shape or its definition."""
    c = Quantity_Color()
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
        if color_tool.GetColor(shape, kind, c):
            return (c.Red(), c.Green(), c.Blue())
        for lab in (label, ref):
            if color_tool.GetColor_s(lab, kind, c) if hasattr(color_tool, "GetColor_s") else \
               color_tool.GetColor(lab, kind, c):
                return (c.Red(), c.Green(), c.Blue())
    return None


def _triangulate(shape: TopoDS_Shape):
    """Every face's triangulation, placed and oriented, as (vertices Nx3, triangles Mx3)."""
    verts, tris, base = [], [], 0
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    missing = 0
    while exp.More():
        face = TopoDS.Face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        exp.Next()
        if tri is None:
            missing += 1
            continue
        trsf = loc.Transformation()
        n = tri.NbNodes()
        pts = np.empty((n, 3))
        for i in range(n):
            p = tri.Node(i + 1).Transformed(trsf)
            pts[i] = (p.X(), p.Y(), p.Z())
        m = tri.NbTriangles()
        idx = np.empty((m, 3), dtype=np.int64)
        for i in range(m):
            a, b, c = tri.Triangle(i + 1).Get()
            idx[i] = (a - 1, b - 1, c - 1)
        # A reversed face's triangles wind the wrong way; the tracker's back-face and silhouette
        # tests depend on winding.
        if face.Orientation() == TopAbs_REVERSED:
            idx = idx[:, [0, 2, 1]]
        verts.append(pts)
        tris.append(idx + base)
        base += n
    if not verts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64), missing
    return np.vstack(verts), np.vstack(tris), missing


def _count_triangles(shape: TopoDS_Shape) -> int:
    n, exp = 0, TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        tri = BRep_Tool.Triangulation_s(TopoDS.Face(exp.Current()), TopLoc_Location())
        if tri is not None:
            n += tri.NbTriangles()
        exp.Next()
    return n


def _weld(v: np.ndarray, t: np.ndarray):
    if len(v) == 0:
        return v, t
    key = np.round(v / WELD_M).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    t = inverse.reshape(-1)[t]
    # Triangles that collapsed to a line or a point say nothing about the surface.
    keep = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    return v[first], t[keep]


def convert(step_path: str, out_dir: str, opts: Options | None = None) -> dict:
    opts = opts or Options()
    t0 = time.time()
    warnings: list[str] = []

    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    # METRES, SET BEFORE THE TRANSFER. OCCT converts every length into the document's unit while
    # transferring; measured on Mac-mini-2024.step (declared millimetre): 130 x 130 x 55.3 at the
    # default, 0.130 x 0.130 x 0.0553 with this line. The package is metric by construction.
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 1.0, UnitsMethods_LengthUnit.UnitsMethods_LengthUnit_Meter)

    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    if reader.ReadFile(step_path) != IFSelect_RetDone:
        raise RuntimeError(f"not a readable STEP file: {step_path}")
    file_units = _file_length_units(reader)
    if not reader.Transfer(doc):
        raise RuntimeError(f"STEP transfer failed: {step_path}")
    t_read = time.time() - t0

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    free = _C.Sequence_TDF_Label()
    shape_tool.GetFreeShapes(free)
    if free.Length() == 0:
        raise RuntimeError("the STEP file contains no shapes")

    # One mesh pass over everything, sized from the whole model. Instances share their geometry,
    # so meshing the roots once meshes every occurrence.
    roots = [shape_tool.GetShape_s(free.Value(i + 1)) for i in range(free.Length())]
    all_box = [math.inf] * 3 + [-math.inf] * 3
    for s in roots:
        b = _bbox(s)
        if b:
            all_box = [min(all_box[i], b[i]) for i in range(3)] + \
                      [max(all_box[i + 3], b[i + 3]) for i in range(3)]
    diag = math.dist(all_box[:3], all_box[3:])
    lin_rel, ang = opts.lin_rel, opts.ang_deg
    passes = []
    t1 = time.time()
    while True:
        lin = max(diag * lin_rel, 1e-6)
        for s in roots:
            BRepTools.Clean_s(s)
            BRepMesh_IncrementalMesh(s, lin, False, math.radians(ang), True)
        n = sum(_count_triangles(s) for s in roots)
        passes.append({"lin_rel": round(lin_rel, 6), "ang_deg": round(ang, 2), "triangles": n})
        if opts.max_triangles <= 0 or n <= opts.max_triangles or len(passes) >= 12:
            break
        # Coarser in both senses: chordal for flat-ish areas, angular for small radii.
        lin_rel *= 1.6
        ang = min(ang * 1.25, 45.0)
    t_mesh = time.time() - t1
    if opts.max_triangles > 0 and passes[-1]["triangles"] > opts.max_triangles:
        warnings.append(f"could not reach the {opts.max_triangles}-triangle budget "
                        f"({passes[-1]['triangles']} at the coarsest pass); the tracker will decimate")
    if lin_rel > 0.01:
        warnings.append(f"tessellation had to go coarse to fit the budget (chordal {lin_rel:.3%} of "
                        f"the diagonal, angular {ang:.0f} deg) - curved outlines will be polygonal")

    # Walk the assembly. A LEAF is a shape that is not an assembly; each placed occurrence of one
    # is a part (the same bolt used six times is six parts, because each can be present or not).
    parts: list[Part] = []
    meshes: list[tuple[np.ndarray, np.ndarray]] = []

    def walk(label: TDF_Label, path: str, inherited_color):
        ref = label
        if shape_tool.IsReference_s(label):
            ref = TDF_Label()
            shape_tool.GetReferredShape_s(label, ref)
        # THE PRODUCT'S NAME, then the instance's if it means something. Instances are often
        # unnamed in the file, and OCCT fills the gap with its own label entry ("=>[0:1:1:2]") -
        # its own writer even exports that. Real exports vary: some name instances well
        # ("Bolt:3"), some write junk like this. Repeats are numbered below.
        product, inst = _label_name(ref), _label_name(label)
        meaningful = inst and not inst.startswith("=>") and inst != product
        name = (inst if meaningful else product) or inst or "part"
        here = f"{path}/{name}" if path else name
        shape = shape_tool.GetShape_s(label)      # for a component: the placed shape
        color = _color_of(color_tool, shape, label, ref) or inherited_color
        if shape_tool.IsAssembly_s(ref):
            kids = _C.Sequence_TDF_Label()
            shape_tool.GetComponents_s(ref, kids)
            for i in range(kids.Length()):
                walk(kids.Value(i + 1), here, color)
            return
        v, t, missing = _triangulate(shape)
        v, t = _weld(v, t)
        # FROM THE MESH, not Bnd_Box: OCCT's box is padded by the geometric tolerance - the Mac
        # mini read 130 mm across from Bnd_Box and 127.0 mm (its real width) from its vertices.
        box = (v.min(0).tolist() + v.max(0).tolist()) if len(v) else []
        p = Part(name=name, path=here, triangles=int(len(t)), vertices=int(len(v)),
                 color=tuple(round(x, 4) for x in (color or (0.7, 0.7, 0.7))),
                 bbox_m=[round(x, 6) for x in box])
        if missing:
            p.warnings.append(f"{missing} face(s) could not be triangulated")
        if len(t) == 0:
            p.warnings.append("no triangles - left out of the mesh")
        parts.append(p)
        meshes.append((v, t))

    for i in range(free.Length()):
        walk(free.Value(i + 1), "", None)

    # The same bolt placed six times is six parts with one name; number them so every path is
    # unique (the OBJ object names and the editor's part list both need that).
    seen: dict[str, int] = {}
    for p in parts:
        seen[p.path] = seen.get(p.path, 0) + 1
    count: dict[str, int] = {}
    for p in parts:
        if seen[p.path] > 1:
            count[p.path] = count.get(p.path, 0) + 1
            p.path = f"{p.path}#{count[p.path]}"

    # HIDDEN PARTS, a first cut. A part whose bounding box sits entirely inside another part's is
    # probably internal - a camera cannot see it, and invisible geometry in the model is known to
    # drag the tracker's pose (see the tracker's model-fidelity notes). A HEURISTIC: it misses a
    # bolt poking through a cover and flags a part inside a hollow box that is open on one side.
    # Reported, never removed.
    for i, p in enumerate(parts):
        if not p.bbox_m:
            continue
        for j, q in enumerate(parts):
            if i == j or not q.bbox_m:
                continue
            if all(q.bbox_m[k] <= p.bbox_m[k] for k in range(3)) and \
               all(p.bbox_m[k + 3] <= q.bbox_m[k + 3] for k in range(3)) and q.triangles > 0:
                p.warnings.append(f"bounding box inside '{q.path}' - possibly hidden")
                break

    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(step_path))[0]
    written = _write_obj(out_dir, base, parts, meshes)
    _write_settings(out_dir, base)

    total_t = sum(p.triangles for p in parts)
    boxes = [p.bbox_m for p in parts if p.bbox_m]
    if boxes:
        all_box = [min(b[i] for b in boxes) for i in range(3)] + \
                  [max(b[i + 3] for b in boxes) for i in range(3)]
    if total_t == 0:
        warnings.append("the model produced no triangles")
    if total_t > 500_000:
        warnings.append(f"{total_t} triangles - the tracker will decimate; check the result")
    if not file_units:
        warnings.append("the file declares no length unit; OCCT assumed millimetres")

    report = {
        "package_version": PACKAGE_VERSION,
        "source": os.path.basename(step_path),
        "source_bytes": os.path.getsize(step_path),
        "file_length_units": file_units,
        "output_units": "metre",
        "bbox_m": [round(x, 6) for x in all_box],
        "size_m": [round(all_box[i + 3] - all_box[i], 6) for i in range(3)],
        "tessellation": {"linear_deflection_m": lin, "angular_deflection_deg": ang,
                         "lin_rel": lin_rel, "max_triangles": opts.max_triangles,
                         "passes": passes},
        "totals": {"parts": len(parts), "triangles": total_t,
                   "vertices": sum(p.vertices for p in parts),
                   "possibly_hidden_parts": sum(1 for p in parts
                                                if any("possibly hidden" in w for w in p.warnings))},
        "parts": [asdict(p) for p in parts],
        "files": written + [base + ".settings", "report.json"],
        "warnings": warnings,
        "seconds": {"read": round(t_read, 3), "mesh": round(t_mesh, 3),
                    "total": round(time.time() - t0, 3)},
    }
    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


def _write_obj(out_dir: str, base: str, parts: list[Part], meshes) -> list[str]:
    """OBJ + MTL. One `usemtl` per part: the tracker's OBJ loader gives every face the index of its
    material, and that index is the part boundary its multi-part features read."""
    obj, mtl = base + ".obj", base + ".mtl"
    with open(os.path.join(out_dir, mtl), "w") as f:
        f.write(f"# written by cad_convert step_worker, package v{PACKAGE_VERSION}\n")
        for i, p in enumerate(parts):
            r, g, b = p.color
            f.write(f"\nnewmtl part_{i:04d}\n# {p.path}\nKd {r:.4f} {g:.4f} {b:.4f}\n")
    with open(os.path.join(out_dir, obj), "w") as f:
        f.write(f"# written by cad_convert step_worker, package v{PACKAGE_VERSION}\n")
        f.write("# units: metres\n")
        f.write(f"mtllib {mtl}\n")
        offset = 1
        for i, (p, (v, t)) in enumerate(zip(parts, meshes)):
            if len(t) == 0:
                continue
            f.write(f"\no {_obj_safe(p.path)}\nusemtl part_{i:04d}\n")
            f.write("".join(f"v {x:.7f} {y:.7f} {z:.7f}\n" for x, y, z in v))
            f.write("".join(f"f {a + offset} {b + offset} {c + offset}\n" for a, b, c in t))
            offset += len(v)
    return [obj, mtl]


def _obj_safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-/" else "_" for ch in s) or "part"


def _write_settings(out_dir: str, base: str) -> None:
    """The tracker reads <model>.settings beside the mesh (applyModelSettingsFile). Every name must
    be a real setting - an unknown one makes the tracker REFUSE the load, by design."""
    with open(os.path.join(out_dir, base + ".settings"), "w") as f:
        f.write("# written by cad_convert step_worker\n")
        f.write("# The mesh is in metres, converted from the STEP file's declared unit, so the\n")
        f.write("# tracker's world maths can trust it from the first frame instead of measuring it.\n")
        f.write("world_scale_known_1000x = 1000\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="STEP -> OBJ model package")
    ap.add_argument("step")
    ap.add_argument("out_dir")
    ap.add_argument("--lin-rel", type=float, default=Options.lin_rel,
                    help="chordal deviation as a fraction of the bbox diagonal (default 0.001)")
    ap.add_argument("--ang-deg", type=float, default=Options.ang_deg,
                    help="angular deviation in degrees (default 15)")
    ap.add_argument("--max-triangles", type=int, default=Options.max_triangles,
                    help="tessellate coarser until under this (default 20000, the tracker's "
                         "tracking_max_faces); 0 = no budget")
    a = ap.parse_args(argv)
    try:
        rep = convert(a.step, a.out_dir, Options(lin_rel=a.lin_rel, ang_deg=a.ang_deg,
                                                 max_triangles=a.max_triangles))
    except Exception as e:  # the API layer maps this to a failed job
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    t = rep["totals"]
    print(f"{rep['source']}: {t['parts']} part(s), {t['triangles']} triangles, "
          f"size {rep['size_m']} m (file unit {rep['file_length_units']}), "
          f"{rep['seconds']['total']} s -> {a.out_dir}")
    for w in rep["warnings"]:
        print("warning:", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
