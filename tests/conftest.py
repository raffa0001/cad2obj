"""Shared fixtures: a STEP assembly whose contents are known exactly."""
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


