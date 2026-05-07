"""
DEPRECATED — canonical implementation moved to ma_poc/core/identity.py.

This shim re-exports from the canonical location to prevent import errors in callers
that reference the old path. Remove this file once all imports have been migrated to
ma_poc.core.identity.

Migration guide:
  Old: from ma_poc.validation.identity_fallback import compute_fallback_unit_id
  New: from ma_poc.core.identity import compute_fallback_unit_id
"""
from ma_poc.core.identity import (  # noqa: F401
    compute_fallback_unit_id,
    compute_fallback_id,
    _round_to,
)

__all__ = ["compute_fallback_unit_id", "compute_fallback_id"]
