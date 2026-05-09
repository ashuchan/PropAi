"""Post-merge yield gate.

Pure decision module: given a :class:`UnitDiff` returned by
``upsert_units``, decide whether more than half the captured units failed
to key (and therefore the extractor should be re-tried at the next tier).

Single Responsibility
---------------------
This module contains a single function and a single dataclass. It does
not log, emit events, write to disk, or call external services — those
concerns belong to the caller (typically ``validation.orchestrator`` or
the per-property post-processing loop in ``daily_runner``).

Design notes
------------
- The threshold (default 0.5) matches what
  ``ma_poc/validation/orchestrator.py`` already uses for its
  reject-ratio check, so the two gates produce consistent escalation
  signals.
- Only fires when the input is non-empty. An extractor that captured
  zero units is already covered by the existing ``UNITS_EMPTY`` issue;
  surfacing both would double-count.
"""

from __future__ import annotations

from dataclasses import dataclass

from data_provider.dtos import UnitDiff


@dataclass(frozen=True)
class YieldVerdict:
    """Outcome of :func:`evaluate`.

    Attributes
    ----------
    next_tier_requested:
        ``True`` when the keyless ratio exceeded ``threshold``. The caller
        is expected to forward this to its existing escalation path.
    keyless_ratio:
        Fraction of input records the SQL/FS upsert could not key into
        the merge table. ``0.0`` when ``input_count`` is zero.
    threshold:
        The threshold this verdict was compared against (echoed for
        logging convenience).
    """

    next_tier_requested: bool
    keyless_ratio: float
    threshold: float


def evaluate(diff: UnitDiff, *, threshold: float = 0.5) -> YieldVerdict:
    """Decide whether the merge yield is poor enough to retry the extractor.

    "Keyless" combines two buckets:

      * ``skipped_no_identity`` — historical drops (now always 0 under the
        no-drop contract; retained so old callers still feed the gate).
      * ``synthetic_key_used`` — records inserted under a payload-hash
        synthetic id because no natural / fingerprint / floor-plan anchor
        was available.

    Both buckets indicate the extractor produced records without a usable
    identity anchor — a clean "next tier please" signal regardless of
    whether the record was dropped (legacy) or rescued (current contract).

    Parameters
    ----------
    diff:
        The :class:`UnitDiff` returned by ``IUnitStateStore.upsert_units``.
        Old callers that don't populate the counter fields default to ``0``
        and the gate produces ``next_tier_requested=False`` (no signal).
    threshold:
        Strictly-greater-than threshold for the keyless ratio. ``0.5`` by
        default, matching the validation orchestrator.

    Returns
    -------
    :class:`YieldVerdict` — never raises.
    """
    n_in = max(0, diff.input_count)
    if n_in == 0:
        return YieldVerdict(next_tier_requested=False, keyless_ratio=0.0, threshold=threshold)

    keyless = max(0, diff.skipped_no_identity) + max(0, diff.synthetic_key_used)
    ratio = keyless / n_in if n_in else 0.0
    return YieldVerdict(
        next_tier_requested=ratio > threshold,
        keyless_ratio=ratio,
        threshold=threshold,
    )
