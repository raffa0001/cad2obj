"""A NESTED assembly - the shape real exports have, and the three things the flat fixture could not catch.

    top  ->  "WG 1001.000" placed at x = +200 mm  ->  "WG 1001.808" placed at x = +10 mm, y = +5 mm

1. PLACEMENT is the whole chain. Each part was placed in its own sub-assembly only, so every part of a
   nested assembly piled up at the sub-assembly's origin (a real one: 1014 of 1148 parts more than 10 mm
   off). The cube must land at 210..220 mm.
2. COLOUR is sRGB. OCCT keeps linear RGB; written as it was, every mid-tone came out darker. A file
   colour of sRGB 0.5 grey must come out 0.5.
3. NAMES reach the tracker: its part list is the MTL's material names, one token each, and the
   product's own name ("WG 1001.808") is the part's - not part_NNNN.
"""
import os

import pytest

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Trsf, gp_Vec
from OCP.Quantity import Quantity_Color, Quantity_TypeOfColor
from OCP.STEPCAFControl import STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_StepModelType
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.UnitsMethods import UnitsMethods_LengthUnit
from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool

from cad_convert.step_worker import Options, convert


def _name(label, text):
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


def _at(x, y=0.0, z=0.0):
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(x, y, z))
    return TopLoc_Location(t)


@pytest.fixture(scope="module")
def nested(tmp_path_factory):
    d = tmp_path_factory.mktemp("nested")
    path = str(d / "nested.step")
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 1.0, UnitsMethods_LengthUnit.UnitsMethods_LengthUnit_Millimeter)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    cube = st.AddShape(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), False)
    _name(cube, "WG 1001.808")
    ct.SetColor(cube, Quantity_Color(0.5, 0.5, 0.5, Quantity_TypeOfColor.Quantity_TOC_sRGB),
                XCAFDoc_ColorType.XCAFDoc_ColorGen)
    base = st.AddShape(BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape(), False)
    _name(base, "base")
    sub = st.NewShape()
    _name(sub, "WG 1001.000")
    st.AddComponent(sub, cube, _at(10.0, 5.0))
    st.AddComponent(sub, cube, _at(30.0, 5.0))
    top = st.NewShape()
    _name(top, "top")
    st.AddComponent(top, base, TopLoc_Location())
    st.AddComponent(top, sub, _at(200.0))
    st.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.SetColorMode(True)
    assert w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    assert int(w.Write(path)) == 1
    out = str(d / "out")
    return out, convert(path, out, Options())


def _cube(rep):
    return next(p for p in rep["parts"] if p["path"].endswith("WG 1001.808#1"))


def test_a_nested_part_lands_where_the_whole_chain_puts_it(nested):
    _, rep = nested
    b = _cube(rep)["bbox_m"]
    assert b[0] == pytest.approx(0.210, abs=1e-6) and b[3] == pytest.approx(0.220, abs=1e-6)
    assert b[1] == pytest.approx(0.005, abs=1e-6) and b[4] == pytest.approx(0.015, abs=1e-6)


def test_the_colour_comes_out_as_the_file_gave_it(nested):
    _, rep = nested
    assert _cube(rep)["color"] == pytest.approx((0.5, 0.5, 0.5), abs=2e-3)


def test_the_product_names_the_part_and_its_material(nested):
    out, rep = nested
    assert _cube(rep)["path"].endswith("WG 1001.000/WG 1001.808#1")
    mtl = open(os.path.join(out, "nested.mtl")).read().split("\n")
    names = [l.split(" ", 1)[1] for l in mtl if l.startswith("newmtl ")]
    assert "WG_1001.808#1" in names
    assert all(" " not in n for n in names)
    assert len(set(names)) == len(names)


def test_the_package_carries_its_face_ceiling(nested):
    out, _ = nested
    lines = [l.split("#")[0].strip() for l in open(os.path.join(out, "nested.settings"))]
    assert "tracking_max_faces = 100000" in lines


def test_the_sub_assembly_becomes_a_group(tmp_path_factory, nested):
    """Two parts under "WG 1001.000" (one level below the root): one `g` - one part to the tracker -
    with a member marker before each, counting its triangles; the lone base part a `g` of its own."""
    out, rep = nested
    obj = open(os.path.join(out, "nested.obj")).read().split("\n")
    assert "#crln:parts=groups" in obj[:5]
    gs = [l[2:] for l in obj if l.startswith("g ")]
    assert "WG 1001.000" in gs and "base" in gs and len(gs) == len(set(gs))
    members = [l for l in obj if l.startswith("#crln:member ")]
    assert [m.split(" ", 2)[2] for m in members] == ["WG_1001.808#1", "WG_1001.808#2"]
    assert all(int(m.split(" ")[1]) == 12 for m in members)
    groups = {p["name"]: p["group"] for p in rep["parts"]}
    assert groups["base"] == ""
