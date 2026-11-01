"""RepoIntel tests against a fixture repository (deterministic, offline)."""

from hubble.repo_intel.evidence import Evidence
from hubble.repo_intel.inventory import inventory
from hubble.repo_intel.python_ast import entry_points, import_edges
from hubble.repo_intel.python_ast import test_mapping as map_tests


def _fixture(tmp_path):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "import os\nfrom mypkg import util\n\n\ndef run():\n    return util.help_()\n",
        encoding="utf-8")
    (pkg / "util.py").write_text("def help_():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from mypkg.core import run\n\nif __name__ == '__main__':\n    run()\n",
        encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_import_edges_distinguish_local_external(tmp_path):
    root = _fixture(tmp_path)
    edges = import_edges(root)
    by_target = {(e.source, e.target): e.kind for e in edges}
    assert by_target[("mypkg/core.py", "os")] == "external"
    assert by_target[("mypkg/core.py", "mypkg")] == "local"
    assert all(e.evidence.path == e.source and e.evidence.start_line >= 1 for e in edges)


def test_entry_points_and_test_mapping(tmp_path):
    root = _fixture(tmp_path)
    eps = entry_points(root)
    assert any(e.path == "main.py" for e in eps)
    mapping = map_tests(root)
    assert mapping.get("tests/test_core.py") == ["mypkg/core.py"]


def test_inventory_is_serializable_with_line_evidence(tmp_path):
    import json
    root = _fixture(tmp_path)
    report = inventory(root)
    json.dumps(report)  # must not raise
    assert report["languages"] == ["python"]
    assert report["import_edges"] and report["entry_points"]
    ev = Evidence(path="a.py", start_line=1, evidence_type="ast")
    assert ev.to_dict()["confidence"] == "high"
