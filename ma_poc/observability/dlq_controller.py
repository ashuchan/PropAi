"""DLQ controller — policy layer over J2's DLQ primitive.

Decides retry schedule and parking rules. Keeps the DLQ data structure pure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from ..discovery.dlq import Dlq

log = logging.getLogger(__name__)


class DlqController:
    """Policy controller for the dead-letter queue.

    Args:
        dlq: The underlying DLQ data store.
        emit_fn: Event emission function.
    """

    def __init__(self, dlq: Dlq, emit_fn: Callable[..., Any]) -> None:
        self._dlq = dlq
        self._emit = emit_fn

    def schedule_retries_for(
        self, run_date: datetime | None = None
    ) -> list[str]:
        """Get property IDs due for retry in this run.

        Args:
            run_date: Current run datetime (default: UTC now).

        Returns:
            List of property_ids to retry.
        """
        if run_date is None:
            run_date = datetime.now(timezone.utc)
        due = self._dlq.due_for_retry(run_date)
        ids = [e.property_id for e in due]
        for pid in ids:
            self._emit("discovery.dlq_retry_scheduled", pid)
        return ids

    def park_after_validation_failure(
        self,
        property_id: str,
        consecutive_unreachable: int,
        error_signature: str = "",
    ) -> bool:
        """Decide whether to park a property after repeated failures.

        Parks when consecutive_unreachable >= 3. Parse failures alone
        do NOT park — only fetch-level unreachable errors.

        Args:
            property_id: The property to evaluate.
            consecutive_unreachable: Count of consecutive fetch failures.
            error_signature: Last error signature.

        Returns:
            True if the property was parked.
        """
        if consecutive_unreachable >= 3:
            self._dlq.park(property_id, "consecutive_unreachable", error_signature)
            self._emit("discovery.dlq_parked", property_id, reason="consecutive_unreachable")
            return True
        return False
