from pathlib import Path

from app.backend.system.config import resolve_relocated_project_path


def test_legacy_project_path_prefers_existing_relocated_target(tmp_path: Path) -> None:
    target = tmp_path / "validation" / "evidence.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    resolved = resolve_relocated_project_path(
        r"P:\Projects\Salty Potatoo Ai\validation\evidence.json",
        tmp_path,
    )

    assert resolved == target.resolve()


def test_missing_historical_target_is_not_falsely_relocated(tmp_path: Path) -> None:
    legacy = Path(r"P:\Projects\Salty Potatoo Ai\missing\checkpoint")

    resolved = resolve_relocated_project_path(legacy, tmp_path)

    assert resolved == legacy.resolve()
