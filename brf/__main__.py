"""Make ``python -m brf`` work as a fallback when the console script isn't on PATH."""
from .main import cli

if __name__ == "__main__":  # pragma: no cover
    cli()
