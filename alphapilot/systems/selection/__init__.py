"""Cross-sectional selection adapters for the unified trading pipeline."""

from alphapilot.systems.selection.definitions import strategy_definitions
from alphapilot.systems.selection.qlib_provider import QlibSelectionProvider

__all__ = ["QlibSelectionProvider", "strategy_definitions"]
