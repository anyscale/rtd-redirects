"""Manage Read the Docs redirects as code."""

try:
    from rtd_redirects._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
