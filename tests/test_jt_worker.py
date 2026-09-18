"""The JT worker end to end: a real jt2pkg subprocess on a real JT file.

Skipped wherever the GPL worker is not built (it lives on the conversion host only).
The sample files on hand all carry one LOD per shape, so the budget step-down is covered
through jt2pkg's --self-test, which runs the real sort/select code on synthetic parts.
"""
import json
import os
import subprocess

import pytest


def _run(worker, *args):
    return subprocess.run([worker, *args], capture_output=True, text=True, timeout=300)


def test_lod_order_and_budget_selection(jt_worker):
    out = _run(jt_worker, "--self-test")
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    # Part A carries its LOD tags out of order; finest (LOD0) must come first.
    assert got["order"] == [7, 8, 9]
    by_budget = {c["budget"]: c for c in got["cases"]}
    # 1350 finest vs budget 1200: one round steps A 1000->500 and B 300->100 (C is single).
    assert by_budget[1200] == {"budget": 1200, "steps": 1, "choices": [1, 1, 0], "total": 650}
    assert by_budget[0] == {"budget": 0, "steps": 0, "choices": [0, 0, 0], "total": 1350}
    # Budget 10 is unreachable: two rounds, everything at its coarsest, 250 left.
    assert by_budget[10] == {"budget": 10, "steps": 2, "choices": [2, 1, 0], "total": 250}


def test_drag_knob_converts_to_a_package(jt_worker, jt_sample, tmp_path):
    out = _run(jt_worker, jt_sample, str(tmp_path / "pkg"))
    assert out.returncode == 0, out.stderr
    base = os.path.splitext(os.path.basename(jt_sample))[0]
    pkg = tmp_path / "pkg"
    rep = json.loads((pkg / "report.json").read_text())

    assert rep["package_version"] == 1
    assert rep["output_units"] == "metre"
    assert rep["file_length_units"] == []          # JT stores none - and says so
    assert rep["assumed_units"] == "millimetre"
    assert any("assumed millimetre" in w for w in rep["warnings"])
    assert rep["totals"]["parts"] == 2
    assert rep["totals"]["triangles"] > 0
    # A 12 x 27 x 27 mm knob: the report's size is the honesty check on units handling.
    assert rep["size_m"][0] == pytest.approx(0.012, abs=0.002)
    assert rep["files"] == [base + ".obj", base + ".mtl", base + ".settings", "report.json"]

    nv = nf = 0
    for line in (pkg / (base + ".obj")).read_text().splitlines():
        if line.startswith("v "):
            nv += 1
        elif line.startswith("f "):
            nf += 1
            for p in line.split()[1:]:
                assert 1 <= int(p) <= nv            # every face index names a vertex
    assert nf == rep["totals"]["triangles"]
    assert nv == rep["totals"]["vertices"]
    assert "world_scale_known_1000x = 1000" in (pkg / (base + ".settings")).read_text()


def test_garbage_fails_as_json_not_a_crash(jt_worker, tmp_path):
    bad = tmp_path / "bad.jt"
    bad.write_bytes(b"Version 9.5 JT garbage" + b"\x00" * 64)
    out = _run(jt_worker, str(bad), str(tmp_path / "pkg"))
    assert out.returncode == 1
    assert "error" in json.loads(out.stderr.strip().splitlines()[-1])   # the API reads this
