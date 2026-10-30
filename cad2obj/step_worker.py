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
import re
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
    # Chordal deviation RELATIVE TO EACH EDGE'S OWN SIZE (OCCT's relative deflection), not to the
    # whole model. It was a fraction of the model's diagonal, one value for every part: fine on a
    # 130 mm object, and on a 3 m assembly (1148 parts) the budget loop pushed it to 0.7 m -
    # a 20 mm bolt meshed to a 700 mm tolerance is not a bolt any more. Relative, a small part keeps
    # its shape at the same setting that keeps a large one smooth.
    lin_rel: float = 0.005
    # Angular deviation, degrees. Curved silhouettes are what the tracker follows, so this bounds
    # how polygonal a cylinder's outline can look.
    ang_deg: float = 15.0
    # THE TRACKER'S BUDGET. It decimates anything over tracking_max_faces (20000 by default) as an
    # emergency guard and says so: "this is a safety net, not preparation". Measured on the Mac
    # mini: 87130 triangles at the defaults above. Re-tessellating coarser follows the true
    # surfaces; decimating afterwards guesses. 0 = no budget.
    # 100000, not the tracker's 20000 default: a CAD mesh is clean (no scan noise for the silhouette
    # walk to read as edges, which is what the default guards against), and at 20000 a 1148-part
    # assembly kept 596 parts. The package's .settings carries the same number as the model's own
    # tracking_max_faces, so the tracker keeps what was tessellated - and it can be changed per model.
    max_triangles: int = 100000
    # THE COARSEST IT MAY GO to meet the budget: past this a part stops looking like itself, and a
    # model over the budget is better left to the tracker's guard (which follows the silhouette)
    # than turned into slivers here.
    lin_rel_max: float = 0.1
    ang_max_deg: float = 30.0
    # THE FILE'S SUB-ASSEMBLIES AS PART GROUPS: the parts under each assembly this many
    # levels down (1 = the root, 2 = the root's own sub-assemblies) become one group - one part to the
    # tracker, its members listed under it by an editor. 0 = no groups, every part on its own.
    # A 3 m machine at 2: 8 groups (351, 209, 209, 135, 134, 43 ... parts), the machine's major units; at 3 a
    # catch-all of 629 loose parts. A group of one part is no group.
    group_level: int = 2


@dataclass
class Part:
    name: str
    path: str
    triangles: int = 0
    vertices: int = 0
    color: tuple = (0.7, 0.7, 0.7)
    bbox_m: list = field(default_factory=list)   # [xmin, ymin, zmin, xmax, ymax, zmax]
    warnings: list = field(default_factory=list)
    group: str = ""                               # the part group it is a member of - see Options


# NAMES THAT ARE NOT NAMES: what exporters write when an instance has none. OCCT's own label entry
# ("=>[0:1:1:2]") and the STEP record a placement comes from ("NAUO473" - Next Assembly Usage
# Occurrence; SolidWorks and others write it). A 1148-part assembly named all its parts this way while its 156
# products carried real names ("WG 1001.808").
_PLACEHOLDER = re.compile(r"^(=>\[.*\]|NAUO\d+)$", re.IGNORECASE)


def _meaningful(name: str) -> bool:
    return bool(name) and not _PLACEHOLDER.match(name.strip())


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


def _srgb(c: Quantity_Color) -> tuple:
    """THE FILE'S COLOUR, as the MTL means it. OCCT keeps colours in linear RGB (Red()/Green()/Blue()
    return linear values), and an MTL's Kd is sRGB like everything else that shows a colour: written
    as they were, every colour came out darker and duller - the file's 0.79 light grey-blue as 0.59,
    its 0.75 red as 0.53."""
    def enc(x: float) -> float:
        x = min(max(x, 0.0), 1.0)
        return 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return (enc(c.Red()), enc(c.Green()), enc(c.Blue()))


def _color_of(color_tool, shape: TopoDS_Shape, label: TDF_Label, ref: TDF_Label):
    """Surface colour, else generic colour, from the instance, its shape or its definition."""
    c = Quantity_Color()
    for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
        if color_tool.GetColor(shape, kind, c):
            return _srgb(c)
        for lab in (label, ref):
            if color_tool.GetColor_s(lab, kind, c) if hasattr(color_tool, "GetColor_s") else \
               color_tool.GetColor(lab, kind, c):
                return _srgb(c)
    return None


def _face_color(color_tool, shape: TopoDS_Shape):
    """A PART COLOURED ON ITS FACES only: the colour most of its faces carry. The package gives a part
    one material, so faces of several colours keep the commonest - better than the assembly's colour
    it used to inherit (orange parts came out grey-blue)."""
    counts: dict = {}
    c = Quantity_Color()
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        f = exp.Current()
        exp.Next()
        for kind in (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen):
            if color_tool.GetColor(f, kind, c):
                k = tuple(round(x, 4) for x in _srgb(c))
                counts[k] = counts.get(k, 0) + 1
                break
    return max(counts, key=counts.get) if counts else None


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
    v, t = v[first], t[keep]
    # AND SLIVERS THE TRACKER WOULD DROP. It skips a face whose cross product is under 1e-12 of its
    # longest edge squared (model_io.cpp); an editor rebuilds a group only when its members'
    # triangle counts add up to the group's, so a face dropped there and not here split nothing back.
    # A thousand times stricter here, so whatever the tracker would drop is already gone.
    if len(t):
        p0, p1, p2 = v[t[:, 0]], v[t[:, 1]], v[t[:, 2]]
        u, w = p1 - p0, p2 - p0
        n = np.linalg.norm(np.cross(u, w), axis=1)
        ref = np.maximum(np.maximum((u * u).sum(1), (w * w).sum(1)), 1e-30)
        t = t[n >= 1e-6 * ref]
    return v, t


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
        # RELATIVE: each edge's deflection is lin_rel of its own size (OCCT isRelative)
        for s in roots:
            BRepTools.Clean_s(s)
            BRepMesh_IncrementalMesh(s, lin_rel, True, math.radians(ang), True)
        n = sum(_count_triangles(s) for s in roots)
        passes.append({"lin_rel": round(lin_rel, 6), "ang_deg": round(ang, 2), "triangles": n})
        if opts.max_triangles <= 0 or n <= opts.max_triangles:
            break
        # Coarser in both senses, never past the floor where parts stop looking like themselves.
        if lin_rel >= opts.lin_rel_max and ang >= opts.ang_max_deg:
            break
        lin_rel = min(lin_rel * 1.6, opts.lin_rel_max)
        ang = min(ang * 1.25, opts.ang_max_deg)
    lin = None                             # relative now: no one absolute deflection to report
    t_mesh = time.time() - t1
    if opts.max_triangles > 0 and passes[-1]["triangles"] > opts.max_triangles:
        warnings.append(f"could not reach the {opts.max_triangles}-triangle budget "
                        f"({passes[-1]['triangles']} at the coarsest pass); the tracker will decimate")
    if lin_rel > 0.02:
        warnings.append(f"tessellation went coarse to fit the budget (chordal {lin_rel:.1%} of each "
                        f"edge, angular {ang:.0f} deg) - curved outlines will be polygonal")

    # Walk the assembly. A LEAF is a shape that is not an assembly; each placed occurrence of one
    # is a part (the same bolt used six times is six parts, because each can be present or not).
    parts: list[Part] = []
    meshes: list[tuple[np.ndarray, np.ndarray]] = []

    # THE PLACEMENT IS THE WHOLE CHAIN. A component's shape (GetShape_s) is placed in its PARENT
    # assembly only; where that sub-assembly sits in its own parent, and so on to the root, has to be
    # composed on the way down. Without it every part of a nested assembly landed at its
    # sub-assembly's local origin - an assembly with parts 3-4 levels deep came out as a pile at the centre.
    def walk(label: TDF_Label, path: str, inherited_color, above: TopLoc_Location):
        ref = label
        if shape_tool.IsReference_s(label):
            ref = TDF_Label()
            shape_tool.GetReferredShape_s(label, ref)
        # THE PRODUCT'S NAME, then the instance's if it means something. Instances are often
        # unnamed in the file, and OCCT fills the gap with its own label entry ("=>[0:1:1:2]") -
        # its own writer even exports that. Real exports vary: some name instances well
        # ("Bolt:3"), some write junk like this. Repeats are numbered below.
        product, inst = _label_name(ref), _label_name(label)
        meaningful = _meaningful(inst) and inst != product
        name = (inst if meaningful else product) if (_meaningful(product) or meaningful) else (
            product or inst or "part")
        here = f"{path}/{name}" if path else name
        shape = shape_tool.GetShape_s(label)      # placed in its parent, and no further
        own = _color_of(color_tool, shape, label, ref)
        if shape_tool.IsAssembly_s(ref):
            color = own or inherited_color
            here_loc = above.Multiplied(shape.Location())
            kids = _C.Sequence_TDF_Label()
            shape_tool.GetComponents_s(ref, kids)
            for i in range(kids.Length()):
                walk(kids.Value(i + 1), here, color, here_loc)
            return
        # colours by the shape as the file registers it; triangles from the shape where it stands
        color = own or _face_color(color_tool, shape) or inherited_color
        v, t, missing = _triangulate(shape.Moved(above))
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
        walk(free.Value(i + 1), "", None, TopLoc_Location())

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

    _assign_groups(parts, opts.group_level)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(step_path))[0]
    written = _write_obj(out_dir, base, parts, meshes)
    _write_settings(out_dir, base, opts.max_triangles)

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
        "tessellation": {"relative": True, "lin_rel": lin_rel, "angular_deflection_deg": ang,
                         "max_triangles": opts.max_triangles, "passes": passes},
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


def _assign_groups(parts: list[Part], level: int) -> None:
    """Each part's group: its assembly `level` steps down the path (see Options.group_level), named
    after that assembly's product, unique. Parts above that level, and groups of one, stay alone."""
    if level <= 0:
        return
    key_of = {}
    count: dict = {}
    for p in parts:
        segs = p.path.split("/")
        if len(segs) > level and p.triangles > 0:
            k = "/".join(segs[:level])
            key_of[id(p)] = k
            count[k] = count.get(k, 0) + 1
    names: dict = {}
    used = {_one_token(p.path.rsplit("/", 1)[-1]) for p in parts}
    for p in parts:
        k = key_of.get(id(p))
        if k is None or count[k] < 2:
            continue
        if k not in names:
            nm = base = k.rsplit("/", 1)[-1].strip() or "group"
            n = 2
            while nm in used:
                nm = f"{base} ({n})"
                n += 1
            used.add(nm)
            names[k] = nm
        p.group = names[k]


def _one_token(s: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "._-#") else "_" for ch in s).strip("_")


def _write_obj(out_dir: str, base: str, parts: list[Part], meshes) -> list[str]:
    """OBJ + MTL. One `usemtl` per part: the tracker's OBJ loader gives every face the index of its
    material, and that index is the part boundary its multi-part features read.

    GROUPS (Part.group), in a format editors can read back: `#crln:parts=groups`
    before any face makes each `g` one part to the tracker; a group's members follow one another, each
    after `#crln:member <triangles> <name>`, which lets an editor list them under the group again.
    A part in no group is a `g` of its own. Members keep their own material - their colour and name."""
    obj, mtl = base + ".obj", base + ".mtl"
    # A PART'S MATERIAL IS ITS NAME in the tracker and the editor: the OBJ loader splits parts by
    # usemtl and lists them by material name. It was part_NNNN, so no product name ever reached the
    # editor's part list. One token (newmtl is read as one word), unique (a part per material).
    mat = _material_names(parts)
    with open(os.path.join(out_dir, mtl), "w") as f:
        f.write(f"# written by cad2obj step_worker, package v{PACKAGE_VERSION}\n")
        for i, p in enumerate(parts):
            r, g, b = p.color
            f.write(f"\nnewmtl {mat[i]}\n# {p.path}\nKd {r:.4f} {g:.4f} {b:.4f}\n")
    grouped = any(p.group for p in parts)
    # a group's members one after another, in the order its first member came
    order, seen = [], set()
    for i, p in enumerate(parts):
        if p.group and p.group not in seen:
            seen.add(p.group)
            order += [j for j, q in enumerate(parts) if q.group == p.group]
        elif not p.group:
            order.append(i)
    with open(os.path.join(out_dir, obj), "w") as f:
        f.write(f"# written by cad2obj step_worker, package v{PACKAGE_VERSION}\n")
        f.write("# units: metres\n")
        if grouped:
            f.write("#crln:parts=groups\n")
        f.write(f"mtllib {mtl}\n")
        offset = 1
        current = None
        for i in order:
            p, (v, t) = parts[i], meshes[i]
            if len(t) == 0:
                continue
            if grouped:
                g = p.group or mat[i]
                if g != current:
                    f.write(f"\ng {g}\n")
                    current = g
                if p.group:
                    f.write(f"#crln:member {len(t)} {mat[i]}\n")
            f.write(f"\no {_obj_safe(p.path)}\nusemtl {mat[i]}\n")
            f.write("".join(f"v {x:.7f} {y:.7f} {z:.7f}\n" for x, y, z in v))
            f.write("".join(f"f {a + offset} {b + offset} {c + offset}\n" for a, b, c in t))
            offset += len(v)
    return [obj, mtl]


def _material_names(parts: list[Part]) -> list[str]:
    """Each part's name as a material: its leaf (the product, numbered when placed more than once),
    spaces made underscores, unique across the file."""
    out, used = [], set()
    for i, p in enumerate(parts):
        leaf = p.path.rsplit("/", 1)[-1]
        nm = _one_token(leaf) or f"part_{i:04d}"
        base, k = nm, 2
        while nm in used:
            nm = f"{base}_{k}"
            k += 1
        used.add(nm)
        out.append(nm)
    return out


def _obj_safe(s: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-/" else "_" for ch in s) or "part"


def _write_settings(out_dir: str, base: str, max_faces: int = 0) -> None:
    """The tracker reads <model>.settings beside the mesh (applyModelSettingsFile). Every name must
    be a real setting - an unknown one makes the tracker REFUSE the load, by design."""
    with open(os.path.join(out_dir, base + ".settings"), "w") as f:
        f.write("# written by cad2obj step_worker\n")
        f.write("# The mesh is in metres, converted from the STEP file's declared unit, so the\n")
        f.write("# tracker's world maths can trust it from the first frame instead of measuring it.\n")
        f.write("world_scale_known_1000x = 1000\n")
        f.write("# The tracker keeps up to this many faces of THIS model (0 = all) - the budget it was\n")
        f.write("# tessellated to, instead of the 20000 every model gets by default.\n")
        f.write(f"tracking_max_faces = {max(0, min(int(max_faces), 200000))}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="STEP -> OBJ model package")
    ap.add_argument("step")
    ap.add_argument("out_dir")
    ap.add_argument("--lin-rel", type=float, default=Options.lin_rel,
                    help="chordal deviation as a fraction of the bbox diagonal (default 0.001)")
    ap.add_argument("--ang-deg", type=float, default=Options.ang_deg,
                    help="angular deviation in degrees (default 15)")
    ap.add_argument("--max-triangles", type=int, default=Options.max_triangles,
                    help="tessellate coarser until under this, and the model's own tracking_max_faces "
                         "(default 100000); 0 = no budget")
    ap.add_argument("--group-level", type=int, default=Options.group_level,
                    help="the sub-assemblies this many levels down become part groups "
                         "(default 2: the root's own; 0 = no groups)")
    a = ap.parse_args(argv)
    try:
        rep = convert(a.step, a.out_dir, Options(lin_rel=a.lin_rel, ang_deg=a.ang_deg,
                                                 max_triangles=a.max_triangles,
                                                 group_level=a.group_level))
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
