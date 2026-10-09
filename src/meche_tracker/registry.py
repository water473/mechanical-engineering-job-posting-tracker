"""Employer configuration is the only change needed to add a supported source."""

from pathlib import Path

from .models import Employer


def load_employers(root: Path) -> list[Employer]:
    directory = root / "config" / "employers"
    if not directory.is_dir():
        raise ValueError(f"employer directory not found: {directory}")
    employers = []
    employer_ids: set[str] = set()
    source_ids: set[str] = set()
    for path in sorted(directory.glob("*.json")):
        try:
            employer = Employer.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid employer configuration {path.name}: {exc}") from exc
        if employer.id in employer_ids:
            raise ValueError(f"duplicate employer ID: {employer.id}")
        employer_ids.add(employer.id)
        for source in employer.sources:
            if source.id in source_ids:
                raise ValueError(f"duplicate source ID: {source.id}")
            source_ids.add(source.id)
        employers.append(employer)
    if not employers:
        raise ValueError("no employer configurations found")
    return employers
