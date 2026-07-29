"""AST helpers for pinning ``appfolio.py`` call-site wiring structurally.

These tests assert facts about how the adapter is *wired* — which filter runs
on which units, with which context — rather than about what it computes. That
is a claim about the parse tree, so read it out of the parse tree.

The alternative, and what several of these assertions used to do, is to locate
an anchor string in the source and search a fixed character window around it::

    duda_idx = src.find('result.tier_used = "TIER_1_API_APPFOLIO_DUDA"')
    window = src[duda_idx - 2000:duda_idx]
    assert 'getattr(ctx, "address"' in window

That encodes *proximity in characters*, which is not the intent and is not
stable: on 2026-07-29 an explanatory comment plus one keyword argument at the
call site pushed the distance to 1987 of 2000, and the "fix" was to compact
unrelated code to buy characters back. The next comment would have broken it
again. Nothing about the adapter's behaviour had changed either time.

Everything here is comment- and formatting-proof by construction: comments are
not in the tree, and line breaks / parenthesisation do not change node shape.
"""
from __future__ import annotations

import ast
import inspect

import ma_poc.pms.adapters.appfolio as appfolio_mod

FILTER_FN = "filter_listings_by_property_address"


def appfolio_tree() -> ast.Module:
    """Parse ``ma_poc/pms/adapters/appfolio.py`` into an AST."""
    return ast.parse(inspect.getsource(appfolio_mod))


def parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """``child -> parent`` for every node in ``tree``.

    ``ast`` does not record parents, and every "which block is this call in"
    question needs them.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _called_name(node: ast.Call) -> str:
    """The bare function name of a call — ``f(...)`` or ``obj.f(...)``."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def find_filter_call(first_arg_name: str, tree: ast.Module | None = None) -> ast.Call:
    """The ``filter_listings_by_property_address`` call whose first positional
    argument is the Name ``first_arg_name`` (``duda_units``, ``ssr_units``, …).

    Raises ``AssertionError`` if no such call site exists — a path that stopped
    filtering is exactly the regression these tests exist to catch.
    """
    tree = appfolio_tree() if tree is None else tree
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called_name(node) != FILTER_FN:
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        if node.args[0].id == first_arg_name:
            return node
    raise AssertionError(f"no {FILTER_FN} call site taking {first_arg_name!r} found")


def filter_call_kwargs(first_arg_name: str) -> dict[str, str]:
    """The keyword arguments a production call site actually passes, unparsed
    to source text (so ``address_field="unit_name"`` reads as ``"'unit_name'"``).
    """
    return {
        kw.arg: ast.unparse(kw.value)
        for kw in find_filter_call(first_arg_name).keywords
        if kw.arg is not None
    }


def find_tier_emit(tier: str, tree: ast.Module) -> ast.Assign:
    """The ``result.tier_used = "<tier>"`` assignment — the point a path
    commits its units as the adapter's answer."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or node.value.value != tier:
            continue
        if any(
            isinstance(t, ast.Attribute) and t.attr == "tier_used"
            for t in node.targets
        ):
            return node
    raise AssertionError(f"no `result.tier_used = {tier!r}` assignment found")


def enclosing_if(target: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.If:
    """The innermost ``if`` whose *body* (not ``else``) contains ``target``."""
    child: ast.AST = target
    cur = parents.get(target)
    while cur is not None:
        if isinstance(cur, ast.If) and any(child is stmt for stmt in cur.body):
            return cur
        child, cur = cur, parents.get(cur)
    raise AssertionError(f"{ast.dump(target)[:80]} is not inside any `if` body")


def enclosing_function(
    target: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The innermost function definition containing ``target``."""
    cur = parents.get(target)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    raise AssertionError(f"{ast.dump(target)[:80]} is not inside any function")


def block_bindings(block: ast.If) -> dict[str, ast.expr]:
    """``name -> bound expression`` for plain ``name = <expr>`` statements
    directly in the block body. Tuple targets are skipped."""
    bound: dict[str, ast.expr] = {}
    for stmt in block.body:
        if not isinstance(stmt, ast.Assign):
            continue
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                bound[target.id] = stmt.value
    return bound


def reads_ctx_attr(value: ast.expr, attr: str) -> bool:
    """True if ``value`` reads ``ctx.<attr>`` — either as
    ``getattr(ctx, "<attr>", ...)`` or as a direct ``ctx.<attr>``.

    Both forms are accepted because both prove the same thing (the value came
    off the AdapterContext); pinning one spelling would re-introduce the
    brittleness this module exists to remove.
    """
    for node in ast.walk(value):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "getattr" or len(node.args) < 2:
                continue
            obj, name = node.args[0], node.args[1]
            if (
                isinstance(obj, ast.Name)
                and obj.id == "ctx"
                and isinstance(name, ast.Constant)
                and name.value == attr
            ):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == attr:
            if isinstance(node.value, ast.Name) and node.value.id == "ctx":
                return True
    return False


def str_constants(node: ast.AST) -> list[str]:
    """Every string constant under ``node``.

    Implicit concatenation of a plain string with f-strings — the shape every
    telemetry line in this adapter uses — parses to a ``JoinedStr`` whose first
    value is a plain ``Constant``, so the tag is reachable this way.
    """
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
