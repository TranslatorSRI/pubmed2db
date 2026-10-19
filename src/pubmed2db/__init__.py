"""pubmed2db: download, store, and export PubMed abstracts."""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml is the one place the version is written; releases set it there
# (docs/releasing.md). Read back from the installed metadata so the two cannot
# drift, which is how this sat at 0.1.0 through the v1.0 and v1.1 releases.
try:
    __version__ = version("pubmed2db")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "unknown"
