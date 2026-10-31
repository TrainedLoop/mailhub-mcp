"""Who may import the admin web code: the credential-writing module stays inside the admin code
path, and no MCP tool code (core/, providers/, prompts.py) can reach the admin package."""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[2] / "src" / "mailhub_mcp"
ADMIN_WEB = PKG / "admin_web"
CREDENTIALS = "mailhub_mcp.admin_web.credentials"
CREDENTIAL_IMPORTERS = {"admin_web/routes.py", "admin_web/accounts_routes.py", "admin_web/context.py"}


def _module_name(path: Path) -> str:
    parts = path.relative_to(PKG.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _package(path: Path) -> str:
    name = _module_name(path)
    return name if path.name == "__init__.py" else name.rsplit(".", 1)[0]


def _imports_in(source: str, package: str) -> set[str]:
    """Every module the source imports, `from a import b` counted as both `a` and `a.b`."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return found


def _imports(path: Path) -> set[str]:
    return _imports_in(path.read_text(encoding="utf-8"), _package(path))


def _files() -> list[Path]:
    files = sorted(PKG.rglob("*.py"))
    assert files
    return files


def _rel(path: Path) -> str:
    return path.relative_to(PKG).as_posix()


def test_only_admin_web_and_server_import_admin_web():
    offenders = {
        _rel(p): sorted(m for m in _imports(p) if m == "mailhub_mcp.admin_web" or m.startswith("mailhub_mcp.admin_web."))
        for p in _files()
        if ADMIN_WEB not in p.parents and _rel(p) != "server.py"
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_server_imports_only_the_admin_entry_points():
    admin = {m for m in _imports(PKG / "server.py") if m.startswith("mailhub_mcp.admin_web")}
    assert admin <= {
        "mailhub_mcp.admin_web.context", "mailhub_mcp.admin_web.context.build_admin_context",
        "mailhub_mcp.admin_web.routes", "mailhub_mcp.admin_web.routes.admin_routes",
    }, admin


def test_only_the_admin_routes_and_context_import_credentials():
    importers = {_rel(p) for p in _files() if CREDENTIALS in _imports(p)}
    assert importers <= CREDENTIAL_IMPORTERS, importers - CREDENTIAL_IMPORTERS
    assert "admin_web/accounts_routes.py" in importers  # the check is looking at real imports


def test_core_never_imports_admin_web():
    core = [p for p in _files() if (PKG / "core") in p.parents]
    assert core
    assert [_rel(p) for p in core if any(m.startswith("mailhub_mcp.admin_web") for m in _imports(p))] == []


def test_import_scanner_sees_every_import_shape():
    pkg = "mailhub_mcp.core"
    assert CREDENTIALS in _imports_in("import mailhub_mcp.admin_web.credentials", pkg)
    assert CREDENTIALS in _imports_in("from mailhub_mcp.admin_web import credentials", pkg)
    assert CREDENTIALS in _imports_in("from mailhub_mcp.admin_web.credentials import write_credential", pkg)
    assert CREDENTIALS in _imports_in("from ..admin_web import credentials", pkg)
    assert CREDENTIALS in _imports_in("def f():\n    from mailhub_mcp.admin_web.credentials import x\n", pkg)
    assert _package(PKG / "core" / "accounts.py") == pkg
