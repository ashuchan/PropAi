"""DUDA path address-filter wiring (87b837b follow-up).

The orchestrator ``filter_listings_by_property_address`` is already
test-covered. This module pins the *wiring* into the DUDA path so
a future refactor doesn't silently re-introduce the 87b837b
contamination (axiomproperties.com 455 units, gbatx.com 440 units).

Every assertion here reads the parse tree, not the source text. These same
claims used to be encoded as "the anchor string appears within 2000 characters
of ``result.tier_used = ...``", which made them fail on comments and line
wrapping — see the module docstring of ``_appfolio_source_ast`` for the
incident that motivated the rewrite.
"""
from __future__ import annotations

import ast

from ma_poc.tests.pms.adapters._appfolio_source_ast import (
    FILTER_FN,
    appfolio_tree,
    block_bindings,
    enclosing_function,
    enclosing_if,
    find_filter_call,
    find_tier_emit,
    parent_map,
    reads_ctx_attr,
    str_constants,
)

DUDA_TIER = "TIER_1_API_APPFOLIO_DUDA"
TELEMETRY_TAG = "appfolio-duda-address-filter"


def test_duda_path_invokes_address_filter() -> None:
    """The TIER_1_API_APPFOLIO_DUDA branch must call
    filter_listings_by_property_address on duda_units before the
    final emit — same as the VANITY path."""
    tree = appfolio_tree()
    parents = parent_map(tree)
    # `find_filter_call` raises if no call site takes `duda_units` at all.
    call = find_filter_call("duda_units", tree)
    emit = find_tier_emit(DUDA_TIER, tree)

    assert enclosing_function(call, parents) is enclosing_function(emit, parents), (
        f"the {FILTER_FN} call on duda_units and the {DUDA_TIER} emit are in "
        "different functions — the emit is no longer downstream of the filter. "
        "This was the 87b837b contamination fix — axiomproperties/gbatx leaked "
        "the full PMC inventory without it."
    )
    assert call.lineno < emit.lineno, (
        f"{FILTER_FN}(duda_units, …) now runs AFTER the {DUDA_TIER} emit, so "
        "the units shipped are the unfiltered ones."
    )


def test_duda_filter_emits_activation_telemetry() -> None:
    """The guarded block must report the filter firing, so run reports can
    quantify the contamination filter activations."""
    tree = appfolio_tree()
    parents = parent_map(tree)
    guard = enclosing_if(find_filter_call("duda_units", tree), parents)

    appends = [
        node
        for node in ast.walk(guard)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and any(s.startswith(TELEMETRY_TAG) for s in str_constants(node))
    ]
    assert appends, (
        f"the DUDA filter block no longer appends telemetry tagged "
        f"{TELEMETRY_TAG!r}; run reports cannot quantify filter activations."
    )


def test_duda_filter_fires_unconditionally() -> None:
    """2026-07-18: the DUDA address filter must fire even when property_group
    IS set. The URL-level propertyGroup filter proved unreliable — 94 props
    leaked the whole PMC despite it — so the address filter is the safety net
    and must NOT be gated on ``not duda_property_group``."""
    tree = appfolio_tree()
    guard = enclosing_if(find_filter_call("duda_units", tree), parent_map(tree))
    assert isinstance(guard.test, ast.Name) and guard.test.id == "duda_units", (
        "the DUDA address filter must be guarded by a bare `if duda_units:` and "
        f"nothing else — found `if {ast.unparse(guard.test)}:`. In particular it "
        "must not be gated on `not duda_property_group`: propertyGroup "
        "URL-scoping is unreliable, so the address filter always runs."
    )


def test_duda_filter_passes_ctx_address_and_zip() -> None:
    """The filter signature requires (units, ctx_address, ctx_zip).
    Both must be read from the AdapterContext."""
    tree = appfolio_tree()
    call = find_filter_call("duda_units", tree)
    guard = enclosing_if(call, parent_map(tree))

    assert len(call.args) >= 3, (
        f"{FILTER_FN}(duda_units, …) passes {len(call.args)} positional args; "
        "the address and ZIP must be passed positionally."
    )
    addr_arg, zip_arg = call.args[1], call.args[2]
    assert isinstance(addr_arg, ast.Name) and isinstance(zip_arg, ast.Name), (
        "the DUDA call site must pass named locals for address and ZIP, got "
        f"({ast.unparse(addr_arg)}, {ast.unparse(zip_arg)})"
    )

    bound = block_bindings(guard)
    for arg, attr in ((addr_arg, "address"), (zip_arg, "zip_code")):
        assert arg.id in bound, (
            f"{arg.id!r} is passed to {FILTER_FN} but is not bound inside the "
            f"`if duda_units:` block — it cannot be shown to come from ctx.{attr}"
        )
        assert reads_ctx_attr(bound[arg.id], attr), (
            f"DUDA filter must read ctx.{attr}; {arg.id} is bound from "
            f"`{ast.unparse(bound[arg.id])}`"
        )
