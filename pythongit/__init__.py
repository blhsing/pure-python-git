"""A pure-Python reimplementation of a useful subset of git."""
from .repo import Repository

__all__ = ["Repository"]


def _resolve_version() -> str:
    """Single source of truth: the installed distribution's version (from
    pyproject), falling back to a literal when running from an unbuilt source
    tree where no distribution metadata is present."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("pure-python-git")
        except PackageNotFoundError:
            return "0+unknown"
    except Exception:
        return "0+unknown"


__version__ = _resolve_version()
