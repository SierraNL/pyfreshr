from .client import FreshrClient
from .models import Device, DeviceCurrent

try:
    from ._version import version as __version__
except ImportError:
    # Package not installed (e.g. running from source without build step).
    __version__ = "0.0.0+unknown"

__all__ = ["FreshrClient", "Device", "DeviceCurrent", "__version__"]
