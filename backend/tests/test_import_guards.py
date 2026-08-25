"""Pre-deploy guards: catch missing imports / unbound names before they 500 in prod.

Runtime NameErrors like ``SearchResultFile`` / ``get_settings`` slip past plain
``importlib.import_module`` because the names are only resolved when the function runs.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import pkgutil
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Names that are always available at runtime but may not appear as imports.
_EXTRA_BUILTINS = frozenset({"__file__", "__name__", "__package__", "__doc__", "__annotations__"})


def _top_level_binds(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(_bind_names(target))
        elif isinstance(node, ast.AnnAssign) and node.target is not None:
            names.update(_bind_names(node.target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    names.add(alias.asname or alias.name)
    return names


def _bind_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        out: set[str] = set()
        for elt in target.elts:
            out.update(_bind_names(elt))
        return out
    if isinstance(target, ast.Starred):
        return _bind_names(target.value)
    return set()


class _UnboundNameChecker(ast.NodeVisitor):
    def __init__(self, toplevel: set[str]) -> None:
        self.issues: list[tuple[int, str]] = []
        self._scopes: list[set[str]] = [set(toplevel)]
        self._star_import = False
        self._builtin = set(dir(builtins)) | _EXTRA_BUILTINS

    def _define(self, name: str) -> None:
        self._scopes[-1].add(name)

    def _has(self, name: str) -> bool:
        return any(name in scope for scope in self._scopes)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._define(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self._star_import = True
            else:
                self._define(alias.asname or alias.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._define(node.name)
        self._scopes.append(set())
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self._define(arg.arg)
        if node.args.vararg:
            self._define(node.args.vararg.arg)
        if node.args.kwarg:
            self._define(node.args.kwarg.arg)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        for child in node.body:
            self.visit(child)
        self._scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[misc]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._define(node.name)
        self._scopes.append(set())
        for child in node.body:
            self.visit(child)
        self._scopes.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name in _bind_names(target):
                self._define(name)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.target is not None:
            for name in _bind_names(node.target):
                self._define(name)
        if node.value is not None:
            self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        for name in _bind_names(node.target):
            self._define(name)
        self.visit(node.iter)
        for child in [*node.body, *node.orelse]:
            self.visit(child)

    visit_AsyncFor = visit_For  # type: ignore[misc]

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                for name in _bind_names(item.optional_vars):
                    self._define(name)
            self.visit(item.context_expr)
        for child in node.body:
            self.visit(child)

    visit_AsyncWith = visit_With  # type: ignore[misc]

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._define(node.name)
        if node.type is not None:
            self.visit(node.type)
        for child in node.body:
            self.visit(child)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._scopes.append(set())
        for gen in node.generators:
            for name in _bind_names(gen.target):
                self._define(name)
            self.visit(gen.iter)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        self.visit(node.elt)
        self._scopes.pop()

    visit_SetComp = visit_ListComp  # type: ignore[misc]
    visit_GeneratorExp = visit_ListComp  # type: ignore[misc]

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._scopes.append(set())
        for gen in node.generators:
            for name in _bind_names(gen.target):
                self._define(name)
            self.visit(gen.iter)
            for if_clause in gen.ifs:
                self.visit(if_clause)
        self.visit(node.key)
        self.visit(node.value)
        self._scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scopes.append(set())
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self._define(arg.arg)
        self.visit(node.body)
        self._scopes.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            if (
                node.id not in self._builtin
                and not self._has(node.id)
                and not self._star_import
            ):
                self.issues.append((node.lineno, node.id))


def _unbound_names_in_file(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert isinstance(tree, ast.Module)
    checker = _UnboundNameChecker(_top_level_binds(tree))
    checker.visit(tree)
    # Unique by name (first line wins).
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for line, name in checker.issues:
        if name in seen:
            continue
        seen.add(name)
        out.append((line, name))
    return out


def test_app_modules_import() -> None:
    """Every app.* submodule must import without raising."""
    import app

    failures: list[str] = []
    for module in pkgutil.walk_packages(app.__path__, app.__name__ + "."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
    assert not failures, "Import failures:\n" + "\n".join(failures)


def test_app_has_no_unbound_runtime_names() -> None:
    """Catch NameErrors that only appear when a function runs (missing imports)."""
    problems: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            issues = _unbound_names_in_file(path)
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(APP_ROOT.parent)}: SyntaxError: {exc}")
            continue
        for line, name in issues:
            rel = path.relative_to(APP_ROOT.parent)
            problems.append(f"{rel}:{line}: undefined name {name!r}")
    assert not problems, "Unbound names (fix imports):\n" + "\n".join(problems)


def test_search_modules_resolve_critical_symbols() -> None:
    """Search path symbols that previously 500'd production must resolve at import time."""
    local = importlib.import_module("app.search.local")
    moments = importlib.import_module("app.search.moments")
    images = importlib.import_module("app.search.images")

    assert getattr(local, "SearchResultFile") is not None or "SearchResultFile" in dir(
        importlib.import_module("app.schemas")
    )
    # local constructs SearchResultFile at runtime — must be in its globals.
    assert "SearchResultFile" in local.__dict__, (
        "app.search.local must import SearchResultFile (production NameError)"
    )
    assert "get_settings" in moments.__dict__, (
        "app.search.moments must import get_settings (production NameError)"
    )
    assert "get_settings" in images.__dict__
    assert callable(local.find_matching_files)
    assert callable(moments.search_video_moments)
    assert callable(images.search_image_files)
