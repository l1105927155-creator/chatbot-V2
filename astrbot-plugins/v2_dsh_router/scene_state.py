"""Trusted scene selection, separate from persona and tool permissions."""
from __future__ import annotations

try:
    from .persona_state import PrivateSelectionStore
except ImportError:  # isolated contract tests
    from persona_state import PrivateSelectionStore


class SceneStateStore(PrivateSelectionStore):
    FIELD = "scene"
    DEFAULT = "casual-chat"
    CHOICES = frozenset({"casual-chat", "research", "development"})
