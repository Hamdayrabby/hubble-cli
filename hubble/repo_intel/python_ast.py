"""Python AST inventory: imports, entry points, and test mapping.

Built on the standard library only. Local vs external imports are resolved
against the repository layout; anything dynamic is reported as unresolved.
"""

import ast
from pathlib import Path
from typing import Dict, List, Set

from hubble.repo_intel.evidence import Evidence, ImportEdge

ENTRY_NAMES = ("__main__.py", "main.py", "cli.py", "app.py", "manage.py", "run.py")


def _local_modules(root: Path) -> Set[str]:
    mods: Set[str] = set()
    for py in root.rglob("*.py"):
        rel = py.relative_to(root)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][:-3]
        if parts:
            mods.add(".".join(parts))
    return mods


def import_edges(root: Path) -> List[ImportEdge]:
    root = Path(root)
    local = _local_modules(root)
    edges: List[ImportEdge] = []
    for py in sorted(root.rglob("*.py")):
        rel = str(py.relative_to(root)).replace("\\", "/")
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets: List[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    targets = [node.module or ""]
                else:
                    targets = [node.module or ""]
            for target in targets:
                if not target:
                    continue
                top = target.split(".")[0]
                kind = "local" if (target in local or top in {m.split(".")[0] for m in local}) else "external"
                if isinstance(node, ast.ImportFrom) and node.level:
                    kind = "local"
                edges.append(ImportEdge(
                    source=rel, target=target, kind=kind,
                    evidence=Evidence(path=rel, start_line=getattr(node, "lineno", 0),
                                      evidence_type="import", confidence="high")))
    return edges


def entry_points(root: Path) -> List[Evidence]:
    root = Path(root)
    out: List[Evidence] = []
    for name in ENTRY_NAMES:
        for hit in sorted(root.rglob(name)):
            rel = str(hit.relative_to(root)).replace("\\", "/")
            note = "console entry candidate"
            try:
                text = hit.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            if "__main__" in text or "def main(" in text or "app =" in text:
                note = "entry point (main/app detected)"
            out.append(Evidence(path=rel, start_line=1, evidence_type="ast",
                                confidence="medium", note=note))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        for line_no, line in enumerate(text.splitlines(), 1):
            if "[project.scripts]" in line or ('=' in line and "hubble" in line and ":" in line):
                out.append(Evidence(path="pyproject.toml", start_line=line_no,
                                    evidence_type="search", confidence="medium",
                                    note=f"script entry: {line.strip()[:80]}"))
    return out


def test_mapping(root: Path) -> Dict[str, List[str]]:
    root = Path(root)
    tests = [str(p.relative_to(root)).replace("\\", "/")
             for p in sorted(root.rglob("test_*.py"))]
    mapping: Dict[str, List[str]] = {}
    for test in tests:
        stem = Path(test).stem[5:]
        candidates = [str(p.relative_to(root)).replace("\\", "/")
                      for p in root.rglob(f"{stem}.py") if "test" not in p.name]
        if candidates:
            mapping[test] = candidates
    return mapping
