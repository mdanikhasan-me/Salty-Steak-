from pathlib import Path

from tools.snapshot_release_source import source_files


def test_release_snapshot_includes_packaged_desktop_helpers() -> None:
    root = Path(__file__).resolve().parents[1]

    relative_paths = {
        path.relative_to(root).as_posix() for path in source_files(root)
    }

    assert "app/desktop/browser/Program.cs" in relative_paths
    assert "app/desktop/browser/PageBridge.cs" in relative_paths
    assert "app/desktop/browser/SaltyBrowser.csproj" in relative_paths
    assert "app/desktop/uia/Program.cs" in relative_paths
    assert "app/desktop/uia/SaltyUia.csproj" in relative_paths
    assert not any("/bin/" in f"/{path}/" for path in relative_paths)
    assert not any("/obj/" in f"/{path}/" for path in relative_paths)


def test_desktop_build_uses_one_exact_cross_layer_build_identity() -> None:
    root = Path(__file__).resolve().parents[1]
    build_script = (root / "tools" / "build_desktop.ps1").read_text(encoding="utf-8")

    assert '"-p:InformationalVersion=$BuildId"' in build_script
    assert '"-p:IncludeSourceRevisionInInformationalVersion=false"' in build_script


def test_native_chat_save_uses_downloads_dialog_and_validated_local_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "desktop" / "native" / "Program.cs").read_text(
        encoding="utf-8"
    )

    assert 'messageType == "save_file"' in source
    assert "new SaveFileDialog" in source
    assert 'Environment.SpecialFolder.UserProfile' in source
    assert '"Downloads"' in source
    assert "SaveProgressWindow" in source
    assert "Only local Salty Steak artifacts can be saved" in source
    assert "File.WriteAllTextAsync" in source
    assert 'type = "save_completed"' in source
