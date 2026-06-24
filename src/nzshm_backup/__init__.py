"""NSHM Backup Solution."""

# _version.py is hatch-vcs-generated at build time (see pyproject.toml).
# Fall back gracefully if it isn't present — e.g. when the package source
# is copied without invoking the wheel build (SAM Makefile artefacts do this).
try:
    from nzshm_backup._version import __version__
except ImportError:
    __version__ = "0.0.dev0+unknown"
