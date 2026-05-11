"""Extraction primitives — canonical accessors, inference, sanity, post-processing.

Modules in this package are PURE: deterministic, side-effect free, never raise
on unexpected input. They form the pipeline applied to every unit dict between
adapter emit and downstream consumption:

    raw_dict → normalize (canonical) → infer → sanity → is_valid_unit → route

See docs/2026_05_11_regressions_fix_design.md for the design.
"""
