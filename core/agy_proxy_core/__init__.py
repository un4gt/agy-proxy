"""agy-proxy core package.

Phase 0 skeleton: the old ``app`` package is still the running application.
"""

from .api import (
    CoreCallContext,
    CoreError,
    CoreServices,
    KeyRef,
    Principal,
    authenticate,
    create_app,
    create_core_only_app,
    get_services,
    resolve_context,
)

__version__ = "0.0.17"

__all__ = [
    "__version__",
    "CoreCallContext",
    "CoreError",
    "CoreServices",
    "KeyRef",
    "Principal",
    "authenticate",
    "create_app",
    "create_core_only_app",
    "get_services",
    "resolve_context",
]
