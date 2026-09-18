"""The STEP worker against a STEP file whose contents are known exactly.

The fixture (conftest.py) is written with Open CASCADE itself: an assembly in MILLIMETRES holding a named, red
100 x 60 x 40 housing and a named, blue 10 mm cube placed entirely inside it - the "internal part a
camera can never see" case. Every assertion below is a fact about that file, so a failure is the
worker's fault and not the fixture's.
"""
import json
import os

import pytest

from cad_convert.step_worker import Options, convert


@pytest.fixture(scope="module")
def package(assembly_step, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("out"))
    return out, convert(assembly_step, out, Options())


def test_units_come_out_in_metres(package):
    _, rep = package
    assert rep["file_length_units"] == ["millimetre"]
    assert rep["output_units"] == "metre"
    assert rep["size_m"] == pytest.approx([0.100, 0.060, 0.040], abs=1e-6)


def test_every_placed_part_is_a_part_with_its_name_and_colour(package):
    _, rep = package
    parts = {p["name"]: p for p in rep["parts"]}
    assert set(parts) == {"housing", "inner_cube"}
    assert parts["housing"]["color"] == pytest.approx((1.0, 0.0, 0.0), abs=1e-3)
    assert parts["inner_cube"]["color"] == pytest.approx((0.0, 0.0, 1.0), abs=1e-3)
    # The cube's placement survives: 45..55 mm in x.
    assert parts["inner_cube"]["bbox_m"][0] == pytest.approx(0.045, abs=1e-6)
    assert parts["inner_cube"]["bbox_m"][3] == pytest.approx(0.055, abs=1e-6)
    for p in parts.values():
        assert p["triangles"] == 12        # a box is six faces of two triangles
        assert p["vertices"] == 8          # welded: the tessellator writes 24


def test_the_internal_part_is_flagged_and_the_housing_is_not(package):
    _, rep = package
    parts = {p["name"]: p for p in rep["parts"]}
    assert any("possibly hidden" in w for w in parts["inner_cube"]["warnings"])
    assert not any("possibly hidden" in w for w in parts["housing"]["warnings"])
    assert rep["totals"]["possibly_hidden_parts"] == 1


def test_the_obj_has_one_material_per_part_and_metric_vertices(package):
    out, _ = package
    obj = open(os.path.join(out, "housing_asm.obj")).read().splitlines()
    assert "mtllib housing_asm.mtl" in obj
    assert sum(1 for l in obj if l.startswith("usemtl ")) == 2
    xs = [float(l.split()[1]) for l in obj if l.startswith("v ")]
    assert max(xs) == pytest.approx(0.100, abs=1e-6)
    mtl = open(os.path.join(out, "housing_asm.mtl")).read()
    assert mtl.count("newmtl ") == 2


def test_the_settings_sidecar_states_metric_units(package):
    out, _ = package
    lines = [l.split("#")[0].strip() for l in open(os.path.join(out, "housing_asm.settings"))]
    assert [l for l in lines if l] == ["world_scale_known_1000x = 1000"]


def test_the_report_is_written_and_lists_the_files(package):
    out, rep = package
    on_disk = json.load(open(os.path.join(out, "report.json")))
    assert on_disk["totals"] == rep["totals"]
    for f in rep["files"]:
        assert os.path.exists(os.path.join(out, f)), f


def test_the_triangle_budget_is_met_by_coarser_tessellation(assembly_step, tmp_path):
    # Two boxes can not be made smaller than 24 triangles; a budget of 10 must say so, not lie.
    rep = convert(assembly_step, str(tmp_path), Options(max_triangles=10))
    assert rep["totals"]["triangles"] == 24
    assert any("budget" in w for w in rep["warnings"])


def test_garbage_is_refused_with_an_error(tmp_path):
    bad = tmp_path / "not_a_step.step"
    bad.write_text("this is not ISO-10303-21")
    with pytest.raises(RuntimeError):
        convert(str(bad), str(tmp_path / "out"))
