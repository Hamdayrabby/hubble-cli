from pathlib import Path
import pytest

from hubble.indexer import (
    SymbolIndex,
    extract_python_symbols,
    extract_js_ts_symbols,
    extract_go_symbols,
    extract_rust_symbols,
)
from hubble.tools import SymbolsTool, ToolContext, ToolError, run_tool


def test_extract_python_symbols():
    code = '''"""Module doc."""

class Database:
    """Represents a DB."""
    def __init__(self, url: str):
        self.url = url

    async def connect(self):
        pass

def calculate_total(a, b=10, *args, **kwargs):
    """Calculate sum."""
    return a + b
'''
    syms = extract_python_symbols(code, "db.py")
    names = [s.name for s in syms]
    assert "Database" in names
    assert "__init__" in names
    assert "connect" in names
    assert "calculate_total" in names

    db_sym = next(s for s in syms if s.name == "Database")
    assert db_sym.kind == "class"
    assert db_sym.docstring == "Represents a DB."

    connect_sym = next(s for s in syms if s.name == "connect")
    assert connect_sym.kind == "async method"
    assert connect_sym.parent == "Database"

    calc_sym = next(s for s in syms if s.name == "calculate_total")
    assert calc_sym.kind == "def"
    assert calc_sym.signature == "(a, b, *args, **kwargs)"
    assert calc_sym.docstring == "Calculate sum."


def test_extract_js_ts_symbols():
    code = """export interface User {
  id: string;
}

export type ID = string | number;

export class AuthManager extends BaseManager {
  login() {}
}

export async function verifyToken(token: string) {
  return true;
}

export const logout = async (userId: string) => {
  return;
};
"""
    syms = extract_js_ts_symbols(code, "auth.ts")
    names = [s.name for s in syms]
    assert "User" in names
    assert "ID" in names
    assert "AuthManager" in names
    assert "verifyToken" in names
    assert "logout" in names

    auth_cls = next(s for s in syms if s.name == "AuthManager")
    assert auth_cls.kind == "class"
    assert "BaseManager" in auth_cls.signature


def test_extract_go_symbols():
    code = """package main

type Server struct {
  port int
}

func NewServer(port int) *Server {
  return &Server{port: port}
}

func (s *Server) Start() error {
  return nil
}
"""
    syms = extract_go_symbols(code, "server.go")
    names = [s.name for s in syms]
    assert "Server" in names
    assert "NewServer" in names
    assert "Start" in names

    start_m = next(s for s in syms if s.name == "Start")
    assert start_m.kind == "method"
    assert start_m.parent == "Server"


def test_extract_rust_symbols():
    code = """pub struct Client;
pub enum Status { Active, Inactive }
pub trait Service {
    fn serve(&self);
}

impl Service for Client {
    fn serve(&self) {}
}

pub async fn run() {}
"""
    syms = extract_rust_symbols(code, "client.rs")
    names = [s.name for s in syms]
    assert "Client" in names
    assert "Status" in names
    assert "Service" in names
    assert "serve" in names
    assert "run" in names


def test_symbol_index_search_and_caching(tmp_path: Path):
    cache_dir = tmp_path / ".cache"
    idx = SymbolIndex(tmp_path, cache_dir=cache_dir)

    py_file = tmp_path / "models.py"
    py_file.write_text("class UserModel:\n    pass\n\ndef get_user(uid):\n    pass\n", encoding="utf-8")

    syms = idx.search("UserModel")
    assert len(syms) == 1
    assert syms[0].name == "UserModel"
    assert syms[0].kind == "class"

    # Search with partial query
    partial = idx.search("user")
    assert len(partial) == 2

    # Outline
    outline = idx.outline("models.py")
    assert len(outline) == 2
    assert outline[0].name == "UserModel"
    assert outline[1].name == "get_user"

    # Repo map
    rmap = idx.repo_map()
    assert "models.py:" in rmap
    assert "UserModel" in rmap


def test_symbols_tool(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    tool = SymbolsTool()

    f = tmp_path / "app.py"
    f.write_text("class AppRunner:\n    def start(self):\n        pass\n", encoding="utf-8")

    # 1. Search by query
    out, is_err = run_tool(tool, {"query": "AppRunner"}, ctx)
    assert not is_err
    assert "AppRunner" in out
    assert "app.py" in out

    # 2. Outline file
    out, is_err = run_tool(tool, {"path": "app.py"}, ctx)
    assert not is_err
    assert "app.py symbols" in out
    assert "AppRunner" in out

    # 3. Workspace outline (neither query nor path)
    out, is_err = run_tool(tool, {}, ctx)
    assert not is_err
    assert "Workspace symbol outline" in out
    assert "AppRunner" in out

    # 4. Non-matching query
    out, is_err = run_tool(tool, {"query": "NonExistentSymbolXYZ"}, ctx)
    assert not is_err
    assert "No symbols matching" in out

    # 5. Secret file protection
    secret = tmp_path / ".env"
    secret.write_text("SECRET=123", encoding="utf-8")
    out, is_err = run_tool(tool, {"path": ".env"}, ctx)
    assert is_err
    assert "it looks like a secrets file" in out


def test_python_syntax_error_fallback():
    # Intentionally broken Python with unmatched parenthesis
    broken_code = """class BrokenService:
    def good_method(self, x):
        return x

    def broken_syntax(self):
        this is ( unclosed parenthesis

    async def another_method(self, y):
        pass

def standalone_func(z):
    pass
"""
    syms = extract_python_symbols(broken_code, "broken.py")
    names = [s.name for s in syms]
    assert "BrokenService" in names
    assert "good_method" in names
    assert "broken_syntax" in names
    assert "another_method" in names
    assert "standalone_func" in names

    # Verify parent attribution
    method_sym = next(s for s in syms if s.name == "good_method")
    assert method_sym.kind == "method"
    assert method_sym.parent == "BrokenService"

    async_sym = next(s for s in syms if s.name == "another_method")
    assert async_sym.kind == "async method"
    assert async_sym.parent == "BrokenService"

    func_sym = next(s for s in syms if s.name == "standalone_func")
    assert func_sym.kind == "def"
    assert func_sym.parent == ""


def test_multiline_signatures_js_go_rust():
    # Multi-line JS/TS function
    js_code = """export async function calculateMetrics(
    userId: string,
    options: QueryOptions
): Promise<MetricResult> {
    return null;
}
"""
    js_syms = extract_js_ts_symbols(js_code, "metrics.ts")
    assert len(js_syms) == 1
    assert js_syms[0].name == "calculateMetrics"
    assert "userId: string" in js_syms[0].signature
    assert "options: QueryOptions" in js_syms[0].signature

    # Multi-line Go method
    go_code = """func (s *BatchServer) Process(
    ctx context.Context,
    batch []Item,
) error {
    return nil
}
"""
    go_syms = extract_go_symbols(go_code, "batch.go")
    assert len(go_syms) == 1
    assert go_syms[0].name == "Process"
    assert go_syms[0].parent == "BatchServer"
    assert "ctx context.Context" in go_syms[0].signature

    # Multi-line Rust function
    rs_code = """pub async fn execute_transaction(
    &mut self,
    tx: Transaction,
) -> Result<(), Error> {
    Ok(())
}
"""
    rs_syms = extract_rust_symbols(rs_code, "tx.rs")
    assert len(rs_syms) == 1
    assert rs_syms[0].name == "execute_transaction"
    assert "&mut self" in rs_syms[0].signature

