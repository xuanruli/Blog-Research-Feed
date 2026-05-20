"""brf — RSS / Firecrawl / X / podcast / Slack tools for the curator agent."""

try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    try:
        __version__ = _pkg_version("blog-research-feed")
    except PackageNotFoundError:
        __version__ = "0.0.0+dev"
except ImportError:  # pragma: no cover — Python <3.8
    __version__ = "0.0.0+legacy"
