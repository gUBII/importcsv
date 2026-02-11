import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import Declutter as declutter  # noqa: E402


def test_declutter_removes_build_cache_and_temp_files(tmp_path):
    (tmp_path / "build").mkdir()
    (tmp_path / "dist").mkdir()
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".mypy_cache").mkdir()
    (tmp_path / ".ruff_cache").mkdir()
    (tmp_path / "module.pyc").write_bytes(b"pyc")
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")
    (tmp_path / ".coverage").write_text("x", encoding="utf-8")
    (tmp_path / "scratch.tmp").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("KEEP=1", encoding="utf-8")

    log = declutter.declutter(tmp_path)

    assert any("Removed directory" in entry for entry in log)
    assert any("Removed file" in entry for entry in log)
    assert not (tmp_path / "build").exists()
    assert not (tmp_path / "dist").exists()
    assert not (tmp_path / "htmlcov").exists()
    assert not (tmp_path / "__pycache__").exists()
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / ".mypy_cache").exists()
    assert not (tmp_path / ".ruff_cache").exists()
    assert not (tmp_path / "module.pyc").exists()
    assert not (tmp_path / ".DS_Store").exists()
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / "scratch.tmp").exists()
    assert (tmp_path / ".env").exists()


def test_declutter_skip_temp_files_preserves_tmp_files(tmp_path):
    (tmp_path / ".DS_Store").write_text("x", encoding="utf-8")
    (tmp_path / "scratch.tmp").write_text("x", encoding="utf-8")

    declutter.declutter(tmp_path, include_temp_files=False)

    assert not (tmp_path / ".DS_Store").exists()
    assert (tmp_path / "scratch.tmp").exists()


def test_declutter_skips_git_metadata(tmp_path):
    git_cache = tmp_path / ".git" / "__pycache__"
    git_cache.mkdir(parents=True)
    (git_cache / "cache.pyc").write_bytes(b"pyc")

    declutter.declutter(tmp_path)

    assert (git_cache / "cache.pyc").exists()
