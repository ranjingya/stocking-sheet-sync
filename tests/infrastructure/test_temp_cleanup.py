import os

from stocking_sheet_sync.infrastructure.artifacts import cleanup_temp_files


def test_cleanup_keeps_newest_and_enforces_both_limits(tmp_path):
    for n in range(5):
        path = tmp_path / str(n)
        path.write_bytes(b"x" * 10)
        os.utime(path, (n + 1, n + 1))
    cleanup_temp_files(tmp_path, 3, 20)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["3", "4"]
    cleanup_temp_files(tmp_path, 1, 100)
    assert [p.name for p in tmp_path.iterdir()] == ["4"]


def test_cleanup_does_not_follow_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("data")
    root = tmp_path / "reports"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    cleanup_temp_files(root, 1, 1)
    assert (outside / "keep").read_text() == "data"


def test_completed_report_only_removes_its_own_directory(tmp_path):
    from stocking_sheet_sync.infrastructure.artifacts import cleanup_completed_report

    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "snapshot.xlsx").write_text("data")
    outside = tmp_path / "keep"
    outside.write_text("keep")
    (batch / "link").symlink_to(outside)
    cleanup_completed_report(tmp_path, batch)
    assert not batch.exists()
    assert outside.read_text() == "keep"
    cleanup_completed_report(tmp_path, batch)


def test_completed_report_rejects_root_external_and_symlink(tmp_path):
    import pytest

    from stocking_sheet_sync.infrastructure.artifacts import cleanup_completed_report

    root = tmp_path / "reports"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    link.symlink_to(outside, target_is_directory=True)
    for path in (root, outside, link):
        with pytest.raises(ValueError):
            cleanup_completed_report(root, path)
    assert outside.exists()
