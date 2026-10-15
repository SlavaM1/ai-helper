"""Bounded file tools and isolated, explicitly managed Git workspaces."""

from .files import Files
from .workspace import WorkspaceManager

__all__ = ["Files", "WorkspaceManager"]
