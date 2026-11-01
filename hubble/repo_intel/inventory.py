"""Repository inventory: layout, symbols (via hubble.indexer), and report."""

from pathlib import Path
from typing import Any, Dict

from hubble.indexer import SymbolIndex
from hubble.repo_intel import python_ast


def inventory(root: Path) -> Dict[str, Any]:
    root = Path(root)
    index = SymbolIndex(root)
    symbols = index.repo_map() if hasattr(index, "repo_map") else {}
    dirs = {"source": [], "tests": [], "docs": [], "config": []}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in ("tests", "test"):
            dirs["tests"].append(child.name)
        elif child.name in ("docs", "doc"):
            dirs["docs"].append(child.name)
        else:
            dirs["source"].append(child.name)
    for cfg in ("pyproject.toml", "setup.py", "requirements.txt", "pytest.ini", "tox.ini"):
        if (root / cfg).is_file():
            dirs["config"].append(cfg)
    return {
        "root": str(root),
        "languages": ["python"] if list(root.rglob("*.py")) else [],
        "directories": dirs,
        "entry_points": [e.to_dict() for e in python_ast.entry_points(root)],
        "import_edges": [e.to_dict() for e in python_ast.import_edges(root)],
        "test_mapping": python_ast.test_mapping(root),
        "symbols": symbols,
        "unresolved": ["dynamic imports and runtime-generated code are not resolved statically"],
    }
