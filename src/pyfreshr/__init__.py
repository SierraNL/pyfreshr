from .client import FreshrClient
from .exceptions import LoginError, ApiResponseError
from .models import DeviceReadings, DeviceSummary, DeviceType

try:
    from ._version import version as __version__
except ImportError:
    # Package not installed (e.g. running from source without build step).
    __version__ = "0.0.0+unknown"

__all__ = [
    "FreshrClient",
    "DeviceSummary",
    "DeviceReadings",
    "DeviceType",
    "LoginError",
    "ApiResponseError",
    "__version__",
]
