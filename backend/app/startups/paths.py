from pathlib import Path


def startups_data_dir() -> Path:
    """Catalog + assets live under backend/datasets/startups (Docker build context)."""
    here = Path(__file__).resolve()
    backend = here.parents[2]  # backend/app/startups -> backend
    return backend / "datasets" / "startups"


def catalog_path() -> Path:
    return startups_data_dir() / "catalog.json"


def asset_path(rel: str) -> Path:
    """Resolve a catalog-relative asset path under the startups data dir."""
    cleaned = (rel or "").lstrip("/")
    return startups_data_dir() / cleaned
