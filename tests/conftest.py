"""Shared fixtures: a STEP assembly whose contents are known exactly."""
import os
from pathlib import Path

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


def _name(label, text):
    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))


@pytest.fixture(scope="session")
def assembly_step(tmp_path_factory):
    d = tmp_path_factory.mktemp("fixture")
    path = str(d / "housing_asm.step")
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 1.0, UnitsMethods_LengthUnit.UnitsMethods_LengthUnit_Millimeter)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    housing = st.AddShape(BRepPrimAPI_MakeBox(100.0, 60.0, 40.0).Shape(), False)
    _name(housing, "housing")
    ct.SetColor(housing, Quantity_Color(1.0, 0.0, 0.0, Quantity_TypeOfColor.Quantity_TOC_RGB),
                XCAFDoc_ColorType.XCAFDoc_ColorGen)
    cube = st.AddShape(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), False)
    _name(cube, "inner_cube")
    ct.SetColor(cube, Quantity_Color(0.0, 0.0, 1.0, Quantity_TypeOfColor.Quantity_TOC_RGB),
                XCAFDoc_ColorType.XCAFDoc_ColorGen)

    asm = st.NewShape()
    _name(asm, "asm")
    st.AddComponent(asm, housing, TopLoc_Location())
    move = gp_Trsf()
    move.SetTranslation(gp_Vec(45.0, 25.0, 15.0))       # fully inside the housing
    st.AddComponent(asm, cube, TopLoc_Location(move))
    st.UpdateAssemblies()

    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.SetColorMode(True)
    assert w.Transfer(doc, STEPControl_StepModelType.STEPControl_AsIs)
    assert int(w.Write(path)) == 1                      # IFSelect_RetDone
    return path


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def jt_worker():
    """The jt2pkg executable, or a skip: it is GPL and built on the conversion host
    (workers/jt/), never shipped, so test machines without it skip JT tests."""
    cand = os.environ.get("CAD2OBJ_JT_WORKER", str(ROOT / "workers" / "jt" / "build" / "jt2pkg"))
    if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
        pytest.skip(f"jt2pkg not built at {cand} (see workers/jt/README.md)")
    return cand


@pytest.fixture(scope="session")
def jt_sample():
    """A small monolithic JT file with known contents (2 parts, mm-scale knob).
    Committed fixtures in tests/assets/ win; otherwise the PyOpenJt samples in the
    git-ignored vendor/ tree (their fishing_reel set converts standalone)."""
    cands = [ROOT / "tests" / "assets" / "drag_knob.jt",
             ROOT / "vendor" / "PyOpenJt" / "Data" / "ExampleFiles" / "fishing_reel" / "drag_knob.jt"]
    for cand in cands:
        if cand.is_file():
            return str(cand)
    pytest.skip("no JT sample file available")


