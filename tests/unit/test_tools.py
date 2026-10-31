from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ticketgate.tools import check_patch, make_read_only_tools


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "src" / "notes.md").write_text("Remember to add docs\n")
    (root / ".env").write_text("SECRET=hunter2\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\nsecret-in-git = add\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("add()\n")
    (root / "image.bin").write_bytes(b"\x00\x01add\x00")
    (tmp_path / "outside.txt").write_text("outside add\n")
    return root


def _call(root: Path, name: str, **args: object) -> str:
    tools = {t.name: t for t in make_read_only_tools(root)}
    return str(tools[name].invoke(args))


def test_only_read_tools_exist(repo: Path) -> None:
    # A new tool must be added here deliberately. No tool may write.
    assert {t.name for t in make_read_only_tools(repo)} == {
        "list_files",
        "read_file",
        "search_code",
    }


def test_list_files_shows_source_and_hides_ignored_paths(repo: Path) -> None:
    output = _call(repo, "list_files", path=".")
    assert "src/app.py" in output
    assert ".env" not in output
    assert ".git" not in output
    assert "node_modules" not in output


def test_read_file_returns_numbered_lines(repo: Path) -> None:
    output = _call(repo, "read_file", path="src/app.py")
    assert "1: def add(a, b):" in output
    assert "2:     return a - b" in output


def test_read_file_honours_a_line_range(repo: Path) -> None:
    output = _call(repo, "read_file", path="src/app.py", start_line=2, end_line=2)
    assert "return a - b" in output
    assert "def add" not in output


def test_read_file_truncates_long_files(repo: Path) -> None:
    (repo / "big.py").write_text("x = 1\n" * 5000)
    output = _call(repo, "read_file", path="big.py")
    assert "truncated" in output
    assert len(output) < 40_000


@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "/etc/passwd", "src/../../outside.txt"],
)
def test_read_file_refuses_paths_outside_the_root(repo: Path, path: str) -> None:
    output = _call(repo, "read_file", path=path)
    assert output.startswith("ERROR")
    assert "outside add" not in output
    assert "root:" not in output


def test_read_file_refuses_a_symlink_that_escapes_the_root(repo: Path) -> None:
    os.symlink(repo.parent / "outside.txt", repo / "link.txt")
    output = _call(repo, "read_file", path="link.txt")
    assert output.startswith("ERROR")
    assert "outside add" not in output


@pytest.mark.parametrize("path", [".env", ".git/config", "secrets/.env.local", "certs/key.pem"])
def test_read_file_refuses_sensitive_paths(repo: Path, path: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=hunter2\n")
    output = _call(repo, "read_file", path=path)
    assert output.startswith("ERROR")
    assert "hunter2" not in output


def test_read_file_reports_a_missing_file(repo: Path) -> None:
    assert _call(repo, "read_file", path="nope.py").startswith("ERROR")


def test_read_file_refuses_binary_content(repo: Path) -> None:
    assert _call(repo, "read_file", path="image.bin").startswith("ERROR")


def test_search_finds_matches_with_file_and_line(repo: Path) -> None:
    assert "src/app.py:1: def add(a, b):" in _call(repo, "search_code", query="def add")


def test_search_is_case_insensitive(repo: Path) -> None:
    assert "src/app.py" in _call(repo, "search_code", query="DEF ADD")


def test_search_skips_sensitive_ignored_and_binary_files(repo: Path) -> None:
    output = _call(repo, "search_code", query="add")
    assert ".git" not in output
    assert "node_modules" not in output
    assert "image.bin" not in output
    assert _call(repo, "search_code", query="hunter2") == "No matches."


def test_search_treats_the_query_as_literal_text(repo: Path) -> None:
    assert _call(repo, "search_code", query="(a, b)").startswith("src/app.py:1")


def test_search_limits_results(repo: Path) -> None:
    (repo / "many.py").write_text("needle\n" * 500)
    output = _call(repo, "search_code", query="needle")
    assert output.count("needle") <= 50
    assert "more matches" in output


GOOD_PATCH = """\
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def test_check_patch_accepts_a_patch_that_applies_without_changing_files(repo: Path) -> None:
    before = (repo / "src" / "app.py").read_text()
    assert check_patch(repo, GOOD_PATCH) is True
    assert (repo / "src" / "app.py").read_text() == before


def test_check_patch_rejects_a_patch_with_wrong_context(repo: Path) -> None:
    assert check_patch(repo, GOOD_PATCH.replace("a - b", "a * b")) is False


def test_check_patch_rejects_text_that_is_not_a_patch(repo: Path) -> None:
    assert check_patch(repo, "just change the minus to a plus") is False


def test_check_patch_works_when_the_root_is_inside_another_git_repo(
    repo: Path, tmp_path: Path
) -> None:
    # The demo target lives inside this repository, so this is the `make dev` setup.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert check_patch(repo, GOOD_PATCH) is True
    assert check_patch(repo, GOOD_PATCH.replace("src/app.py", "src/missing.py")) is False
