"""The API end to end: upload, job, package - with a real worker subprocess, not a mock."""
import io
import json
import os
import time
import zipfile

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_CONVERT_JOBS", str(tmp_path / "jobs"))
    monkeypatch.delenv("CAD_CONVERT_API_KEY", raising=False)
    import importlib
    import cad_convert.api as api
    importlib.reload(api)                  # pick up the job directory
    from fastapi.testclient import TestClient
    return TestClient(api.app), api


def _wait(c, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get(f"/v1/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_step_upload_converts_to_a_package(client, assembly_step):
    c, _ = client
    with open(assembly_step, "rb") as f:
        r = c.post("/v1/convert", files={"file": ("housing asm.step", f)})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["format"] == "step"
    job = _wait(c, body["id"])
    assert job["status"] == "done", job.get("error")
    assert job["filename"] == "housing_asm.step"          # sanitised, base name kept
    assert job["report"]["totals"]["parts"] == 2
    z = zipfile.ZipFile(io.BytesIO(c.get(job["package_url"]).content))
    assert set(z.namelist()) == {"housing_asm.obj", "housing_asm.mtl",
                                 "housing_asm.settings", "report.json"}
    assert c.get(f"/v1/jobs/{body['id']}/files/report.json").status_code == 200
    assert c.get(f"/v1/jobs/{body['id']}/files/..%2Fjob.json").status_code == 404
    assert c.delete(f"/v1/jobs/{body['id']}").status_code == 200
    assert c.get(f"/v1/jobs/{body['id']}").status_code == 404


def test_format_is_decided_by_content_not_extension(client, tmp_path):
    c, api = client
    r = c.post("/v1/convert", files={"file": ("looks.step", b"PK\x03\x04 a zip, not STEP")})
    assert r.status_code == 415
    assert api.sniff(b"Version 9.5 JT      \n") == ("jt", "9.5")
    assert api.sniff(b"  ISO-10303-21;\nHEADER;") == ("step", "")


def test_a_broken_step_fails_the_job_not_the_service(client):
    c, _ = client
    r = c.post("/v1/convert", files={"file": ("bad.step", b"ISO-10303-21;\nHEADER;\ngarbage")})
    assert r.status_code == 202
    job = _wait(c, r.json()["id"])
    assert job["status"] == "failed"
    assert job["error"]
    assert c.get("/v1/health").json()["ok"] is True


def test_the_api_key_is_enforced_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD_CONVERT_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("CAD_CONVERT_API_KEY", "s3cret")
    import importlib
    import cad_convert.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    assert c.get("/v1/formats").status_code == 401
    assert c.get("/v1/formats", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert c.get("/v1/health").status_code == 200        # health stays open for monitoring


def test_jt_upload_converts_to_a_package(tmp_path, monkeypatch, jt_worker, jt_sample):
    monkeypatch.setenv("CAD_CONVERT_JOBS", str(tmp_path / "jobs"))
    monkeypatch.setenv("CAD_CONVERT_JT_WORKER", jt_worker)
    monkeypatch.delenv("CAD_CONVERT_API_KEY", raising=False)
    import importlib
    import cad_convert.api as api
    importlib.reload(api)                  # pick up the worker path
    assert api.jt_available()
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    assert c.get("/v1/formats").json()["jt"]["available"] is True
    with open(jt_sample, "rb") as f:
        r = c.post("/v1/convert", files={"file": ("drag knob.jt", f)})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["format"] == "jt"
    job = _wait(c, body["id"])
    assert job["status"] == "done", job.get("error")
    assert job["report"]["totals"]["parts"] == 2
    assert job["report"]["assumed_units"] == "millimetre"
    z = zipfile.ZipFile(io.BytesIO(c.get(job["package_url"]).content))
    assert set(z.namelist()) == {"drag_knob.obj", "drag_knob.mtl",
                                 "drag_knob.settings", "report.json"}
