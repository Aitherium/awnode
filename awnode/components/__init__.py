"""awnode's component manifests, discovered by awdk through the `aither.components`
entry point (see pyproject.toml). These nine were awnode's hardcoded BUILTIN_CATALOG
until 2026-09-06; as manifests they are read by every host -- awdk, the aitheros
launcher, awsh, awdesk and the Living Desktop -- not only by `awnode ext`.
"""
from pathlib import Path


def manifest_dir() -> Path:
    """The directory of *.yaml manifests this package ships (entry-point target)."""
    return Path(__file__).resolve().parent
