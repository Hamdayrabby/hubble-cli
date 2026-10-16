"""Lightweight AST and regex symbol indexer for Hubble.

Pre-indexes symbols (classes, functions, methods, types, interfaces) across
Python, TypeScript/JavaScript, Go, and Rust. Zero external dependencies.
Results are cached on disk with file mtime validation for near-instant (<5ms) lookups.
"""

import ast
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hubble.settings import HOME_DIR
from hubble.tools import IGNORED_DIRS

SUPPORTED_EXTS = {
    ".py",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go",
    ".rs",
}

MAX_FILE_BYTES = 512 * 1024  # Skip files larger than 512KB for indexing


@dataclass
class Symbol:
    name: str
    kind: str             # class | def | async def | method | async method | interface | type | struct | fn | enum | trait
    path: str             # workspace-relative path (e.g. "hubble/agent.py")
    line: int             # 1-based line number
    end_line: int         # 1-based end line
    signature: str = ""   # parameters, e.g. "(self, query: str)"
    docstring: str = ""   # first line of docstring
    parent: str = ""      # enclosing class/struct name if any

    def format_line(self) -> str:
        sig = self.signature or ""
        doc = f"  # {self.docstring}" if self.docstring else ""
        parent_prefix = f"{self.parent}." if self.parent else ""
        return f"{self.path}:{self.line}: [{self.kind}] {parent_prefix}{self.name}{sig}{doc}"


def extract_python_symbols(source: str, rel_path: str) -> List[Symbol]:
    """Extract classes, functions, and methods using Python's standard library ast."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    symbols: List[Symbol] = []

    def format_args(args: ast.arguments) -> str:
        parts: List[str] = []
        for a in getattr(args, "posonlyargs", []):
            parts.append(a.arg)
        if getattr(args, "posonlyargs", []):
            parts.append("/")
        for a in args.args:
            parts.append(a.arg)
        if args.vararg:
            parts.append(f"*{args.vararg.arg}")
        elif args.kwonlyargs:
            parts.append("*")
        for a in args.kwonlyargs:
            parts.append(a.arg)
        if args.kwarg:
            parts.append(f"**{args.kwarg.arg}")
        return f"({', '.join(parts)})"

    def get_doc(node: ast.AST) -> str:
        doc = ast.get_docstring(node) or ""
        return doc.strip().splitlines()[0] if doc.strip() else ""

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            base_sig = f"({', '.join(bases)})" if bases else ""
            symbols.append(Symbol(
                name=node.name,
                kind="class",
                path=rel_path,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=base_sig,
                docstring=get_doc(node),
            ))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "async method" if isinstance(item, ast.AsyncFunctionDef) else "method"
                    symbols.append(Symbol(
                        name=item.name,
                        kind=kind,
                        path=rel_path,
                        line=item.lineno,
                        end_line=getattr(item, "end_lineno", item.lineno),
                        signature=format_args(item.args),
                        docstring=get_doc(item),
                        parent=node.name,
                    ))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            symbols.append(Symbol(
                name=node.name,
                kind=kind,
                path=rel_path,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                signature=format_args(node.args),
                docstring=get_doc(node),
            ))

    return symbols


_JS_CLASS_RE = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z0-9_$]+)(?:\s+extends\s+([A-Za-z0-9_$.]+))?")
_JS_INTERFACE_RE = re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z0-9_$]+)")
_JS_TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z0-9_$]+)\s*=")
_JS_FUNC_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*([A-Za-z0-9_$]+)\s*(\([^)]*\))")
_JS_CONST_FUNC_RE = re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z0-9_$]+)\s*=\s*(?:async\s*)?(\([^)]*\)|[A-Za-z0-9_$]+)\s*=>")


def extract_js_ts_symbols(source: str, rel_path: str) -> List[Symbol]:
    """Extract classes, interfaces, types, and functions from JS/TS."""
    symbols: List[Symbol] = []
    lines = source.splitlines()

    for idx, line in enumerate(lines, 1):
        m = _JS_CLASS_RE.match(line)
        if m:
            base = f" extends {m.group(2)}" if m.group(2) else ""
            symbols.append(Symbol(name=m.group(1), kind="class", path=rel_path, line=idx, end_line=idx, signature=base))
            continue

        m = _JS_INTERFACE_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="interface", path=rel_path, line=idx, end_line=idx))
            continue

        m = _JS_TYPE_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="type", path=rel_path, line=idx, end_line=idx))
            continue

        m = _JS_FUNC_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="function", path=rel_path, line=idx, end_line=idx, signature=m.group(2)))
            continue

        m = _JS_CONST_FUNC_RE.match(line)
        if m:
            sig = m.group(2) if m.group(2).startswith("(") else f"({m.group(2)})"
            symbols.append(Symbol(name=m.group(1), kind="function", path=rel_path, line=idx, end_line=idx, signature=sig))
            continue

    return symbols


_GO_METHOD_RE = re.compile(r"^\s*func\s*\(([^)]+)\)\s*([A-Za-z0-9_]+)\s*(\([^)]*\))")
_GO_FUNC_RE = re.compile(r"^\s*func\s+([A-Za-z0-9_]+)\s*(\([^)]*\))")
_GO_TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z0-9_]+)\s+(struct|interface)")


def extract_go_symbols(source: str, rel_path: str) -> List[Symbol]:
    symbols: List[Symbol] = []
    for idx, line in enumerate(source.splitlines(), 1):
        m = _GO_METHOD_RE.match(line)
        if m:
            recv = m.group(1).split()[-1].lstrip("*")
            symbols.append(Symbol(name=m.group(2), kind="method", path=rel_path, line=idx, end_line=idx, signature=m.group(3), parent=recv))
            continue
        m = _GO_FUNC_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="func", path=rel_path, line=idx, end_line=idx, signature=m.group(2)))
            continue
        m = _GO_TYPE_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind=m.group(2), path=rel_path, line=idx, end_line=idx))
            continue
    return symbols


_RS_FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z0-9_]+)\s*(\([^)]*\))")
_RS_STRUCT_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?struct\s+([A-Za-z0-9_]+)")
_RS_ENUM_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?enum\s+([A-Za-z0-9_]+)")
_RS_TRAIT_RE = re.compile(r"^\s*(?:pub(?:\([^)]+\))?\s+)?trait\s+([A-Za-z0-9_]+)")
_RS_IMPL_RE = re.compile(r"^\s*impl(?:\s*<[^>]+>)?\s+(?:([A-Za-z0-9_]+)\s+for\s+)?([A-Za-z0-9_:]+)")


def extract_rust_symbols(source: str, rel_path: str) -> List[Symbol]:
    symbols: List[Symbol] = []
    current_impl = ""
    for idx, line in enumerate(source.splitlines(), 1):
        m = _RS_IMPL_RE.match(line)
        if m:
            current_impl = m.group(2)
        if line.strip() == "}":
            current_impl = ""

        m = _RS_FN_RE.match(line)
        if m:
            kind = "method" if current_impl else "fn"
            symbols.append(Symbol(name=m.group(1), kind=kind, path=rel_path, line=idx, end_line=idx, signature=m.group(2), parent=current_impl))
            continue
        m = _RS_STRUCT_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="struct", path=rel_path, line=idx, end_line=idx))
            continue
        m = _RS_ENUM_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="enum", path=rel_path, line=idx, end_line=idx))
            continue
        m = _RS_TRAIT_RE.match(line)
        if m:
            symbols.append(Symbol(name=m.group(1), kind="trait", path=rel_path, line=idx, end_line=idx))
            continue
    return symbols


class SymbolIndex:
    """Workspace-level symbol index with mtime-based caching."""

    def __init__(self, root: Path, cache_dir: Optional[Path] = None):
        self.root = root.resolve()
        self.cache_dir = cache_dir or (HOME_DIR / "cache" / "index")
        self._files: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def _cache_file(self) -> Path:
        h = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def _load_cache(self):
        if self._loaded:
            return
        self._loaded = True
        c = self._cache_file()
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._files = data.get("files", {})
            except Exception:
                self._files = {}

    def _save_cache(self):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file().write_text(json.dumps({"files": self._files}), encoding="utf-8")
        except Exception:
            pass

    def scan_file(self, path: Path) -> List[Symbol]:
        try:
            rel = str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            rel = str(path).replace("\\", "/")

        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            return []

        try:
            st = path.stat()
            if st.st_size > MAX_FILE_BYTES:
                return []
            mtime = st.st_mtime
        except OSError:
            return []

        cached = self._files.get(rel)
        if cached and cached.get("mtime") == mtime and cached.get("size") == st.st_size:
            return [Symbol(**s) for s in cached.get("symbols", [])]

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []

        if ext == ".py":
            syms = extract_python_symbols(source, rel)
        elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            syms = extract_js_ts_symbols(source, rel)
        elif ext == ".go":
            syms = extract_go_symbols(source, rel)
        elif ext == ".rs":
            syms = extract_rust_symbols(source, rel)
        else:
            syms = []

        self._files[rel] = {
            "mtime": mtime,
            "size": st.st_size,
            "symbols": [asdict(s) for s in syms],
        }
        return syms

    def refresh(self, max_files: int = 600) -> List[Symbol]:
        self._load_cache()
        all_symbols: List[Symbol] = []
        count = 0

        for dirpath, dirnames, filenames in os.walk(self.root):
            # Prune ignored directories in-place
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]

            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUPPORTED_EXTS:
                    p = Path(dirpath) / fname
                    syms = self.scan_file(p)
                    all_symbols.extend(syms)
                    count += 1
                    if count >= max_files:
                        break
            if count >= max_files:
                break

        self._save_cache()
        return all_symbols

    def search(self, query: str, limit: int = 30) -> List[Symbol]:
        all_symbols = self.refresh()
        q = query.strip().lower()
        if not q:
            return all_symbols[:limit]

        scored: List[Tuple[int, Symbol]] = []
        for s in all_symbols:
            name_lower = s.name.lower()
            if name_lower == q:
                score = 100
            elif name_lower.startswith(q):
                score = 80
            elif q in name_lower:
                score = 50
            elif q in s.path.lower():
                score = 30
            else:
                continue

            # Bias towards top-level classes and functions
            if s.kind in ("class", "def", "struct", "interface", "trait"):
                score += 10
            if not s.parent:
                score += 5

            scored.append((score, s))

        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].line))
        return [s for _, s in scored[:limit]]

    def outline(self, rel_path: str) -> List[Symbol]:
        self._load_cache()
        norm = rel_path.replace("\\", "/").strip().lstrip("./")
        p = self.root / norm
        if not p.is_file():
            return []
        syms = self.scan_file(p)
        return sorted(syms, key=lambda s: s.line)

    def repo_map(self, max_symbols: int = 80) -> str:
        """Condensed outline of the key classes and functions in the codebase."""
        symbols = self.refresh()
        if not symbols:
            return "(no symbols indexed)"

        # Group by path
        by_path: Dict[str, List[Symbol]] = {}
        for s in symbols:
            by_path.setdefault(s.path, []).append(s)

        lines: List[str] = []
        total = 0
        for path, syms in sorted(by_path.items()):
            lines.append(f"{path}:")
            for s in sorted(syms, key=lambda x: x.line):
                indent = "    " if s.parent else "  "
                sig = s.signature or ""
                lines.append(f"{indent}{s.kind} {s.name}{sig}")
                total += 1
                if total >= max_symbols:
                    break
            if total >= max_symbols:
                lines.append("  ...")
                break

        return "\n".join(lines)
