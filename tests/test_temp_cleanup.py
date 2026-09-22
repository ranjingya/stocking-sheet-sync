import os

from stocking_sheet_sync.temp_cleanup import cleanup_temp_files


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
