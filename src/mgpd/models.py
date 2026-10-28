"""Shared production model and supported seed range."""
from dataclasses import dataclass
from typing import Optional


MAX_SEED = 30080


@dataclass(frozen=True)
class PreparedProduction:
    """Resolved execution plan loaded from a saved production."""
    output_dir: str
    manifest: dict
    local_dirs: dict
    submit_file: Optional[str] = None

