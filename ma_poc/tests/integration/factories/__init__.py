"""Builder functions for integration test data.

Each factory produces a valid, minimal object with sensible defaults.
Override only the fields you care about — tests should express intent,
not boilerplate.
"""

from .fetch_result import build_fetch_result
from .run_directory import RunDirectoryFactory
from .scrape_profile import ScrapeProfileFactory, build_profile
from .unit_record import build_unit

__all__ = [
    "build_fetch_result",
    "build_profile",
    "build_unit",
    "RunDirectoryFactory",
    "ScrapeProfileFactory",
]
