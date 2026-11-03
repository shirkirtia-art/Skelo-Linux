"""
Skelo-Linux: Deterministic Linux desktop automation for AI agents.
No screenshots. No pixel-guessing. Direct AT-SPI accessibility tree and X11 access.
"""

from .desktop import Desktop, Window, Element
from .version import __version__

__all__ = ["Desktop", "Window", "Element", "__version__"]
