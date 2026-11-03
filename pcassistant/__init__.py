"""AI PC Assistant.

A small, plugin-driven assistant core.  The package deliberately has no
third-party runtime dependencies: plugins can add optional extractors
(``pypdf``) but the host must always import cleanly.
"""

from .config import Config
from .models import Action, Citation, Health, PluginResult, Query

__all__ = [
    "Action",
    "Citation",
    "Config",
    "Health",
    "PluginResult",
    "Query",
]

__version__ = "1.0.0"
