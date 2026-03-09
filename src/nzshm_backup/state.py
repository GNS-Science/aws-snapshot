"""Shared CLI state, accessible to all subcommand modules."""

from dataclasses import dataclass


@dataclass
class AppState:
    verbose: bool = False
    dry_run: bool = False
    output: str = "text"


_state = AppState()


def get_state() -> AppState:
    """Return the shared CLI state set by the root callback."""
    return _state
