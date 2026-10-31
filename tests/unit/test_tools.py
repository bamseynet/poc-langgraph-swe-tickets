from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".git/config",
        "secrets/.env.local",
        "certs/key.pem",
        "deploy/secrets/prod.yaml",
        "config/secret/db.ini",
        ".envrc",
        "prod.env",
        ".git-credentials",
        "aws_credentials",
        "terraform.tfstate",
        "prod.tfvars",
        "service-account.json",
        ".pgpass",
        "id_ecdsa",
        ".dockerconfigjson",
        "keystore.jks",
        ".htpasswd",
    ],
)
def test_read_file_refuses_sensitive_paths(repo: Path, path: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=hunter2\n")
    output = _call(repo, "read_file", path=path)
    assert output.startswith("ERROR")
    assert "hunter2" not in output


@pytest.mark.parametrize(
    "path",
    ["a\x00b", "x" * 5000, "../../" * 400],
)
def test_read_file_reports_unusable_paths_instead_of_raising(repo: Path, path: str) -> None:
    assert _call(repo, "read_file", path=path).startswith("ERROR")


def test_read_file_reports_a_symlink_loop_instead_of_raising(repo: Path) -> None:
    os.symlink(repo / "loop", repo / "loop")
    assert _call(repo, "read_file", path="loop").startswith("ERROR")


def test_search_skips_an_unreadable_file(repo: Path) -> None:
    os.symlink(repo / "src" / "app.py", repo / "src" / "ok-link.py")
    locked = repo / "src" / "locked.py"
    locked.write_text("def add\n")
    locked.chmod(0)
    try:
        assert "src/app.py:1" in _call(repo, "search_code", query="def add")
    finally:
        locked.chmod(0o644)


@pytest.mark.parametrize(
    "path",
    [
        "src/auth/credentials.py",
        "internal/credentials/store.go",
        "tests/test_credentials.py",
        "web/src/credentials-form.tsx",
        "src/secrets_manager/rotate.py",
        "app/models/client_secret.py",
        "docs/secrets.md",
        "cmd/build/main.go",
        "lib/dist/stats.py",
        "Build/Makefile",
    ],
)
def test_ordinary_source_files_are_not_hidden(repo: Path, path: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("findme = 1\n")
    assert "findme" in _call(repo, "read_file", path=path)
    assert path in _call(repo, "search_code", query="findme")
    assert path in _call(repo, "list_files", path=".")


def test_generated_output_at_the_root_is_still_skipped(repo: Path) -> None:
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("findme\n")
    assert _call(repo, "search_code", query="findme").startswith("No matches.")


def test_a_search_says_when_policy_hid_files(repo: Path) -> None:
    # Otherwise "No matches." looks the same as the code not existing.
    assert "1 file" in _call(repo, "search_code", query="nothing-matches-this")
    assert "hidden" in _call(repo, "search_code", query="nothing-matches-this")
    assert "hidden" in _call(repo, "search_code", query="def add")
    assert "hidden" in _call(repo, "list_files", path=".")


def test_a_search_does_not_mention_hiding_when_nothing_is_hidden(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n")
    assert _call(tmp_path, "search_code", query="zzz") == "No matches."


def test_search_skips_a_fifo_instead_of_blocking_on_it(repo: Path) -> None:
    os.mkfifo(repo / "pipe")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_call, repo, "search_code", query="def add")
        try:
            assert "src/app.py" in future.result(timeout=10)
        finally:
            # Opening the write end releases a blocked reader, so a regression fails, not hangs.
            if not future.done():
                os.close(os.open(repo / "pipe", os.O_WRONLY | os.O_NONBLOCK))


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
    hidden = _call(repo, "search_code", query="hunter2")
    assert hidden.startswith("No matches.")
    assert "hunter2" not in hidden


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


GIT_STYLE_PATCH = "diff --git a/src/app.py b/src/app.py\n" + GOOD_PATCH


def test_check_patch_works_when_the_root_is_inside_another_git_repo(
    repo: Path, tmp_path: Path
) -> None:
    # The demo target lives inside this repository, so this is the `make dev` setup.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert check_patch(repo, GOOD_PATCH) is True
    assert check_patch(repo, GIT_STYLE_PATCH) is True
    assert check_patch(repo, GOOD_PATCH.replace("src/app.py", "src/missing.py")) is False


def test_check_patch_is_not_fooled_by_git_headers_inside_another_repo(
    repo: Path, tmp_path: Path
) -> None:
    # With `diff --git` headers, git in a subdirectory of a repo skips the file and exits 0.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert check_patch(repo, GIT_STYLE_PATCH.replace("a - b", "a * b")) is False
    assert check_patch(repo, GIT_STYLE_PATCH.replace("src/app.py", "src/missing.py")) is False


def test_check_patch_treats_undecodable_text_as_not_applying(repo: Path) -> None:
    assert check_patch(repo, GOOD_PATCH + "+\udc80\n") is False


def test_read_file_reports_a_start_line_past_the_end(repo: Path) -> None:
    output = _call(repo, "read_file", path="src/app.py", start_line=500, end_line=600)
    assert output.startswith("ERROR")


def test_a_huge_file_is_read_only_up_to_the_cap(repo: Path) -> None:
    (repo / "huge.log").write_text("needle\n" + "x" * 3_000_000)
    assert "huge.log:1: needle" in _call(repo, "search_code", query="needle")


def test_read_file_rejects_an_inverted_range(repo: Path) -> None:
    output = _call(repo, "read_file", path="src/app.py", start_line=5, end_line=2)
    assert output.startswith("ERROR")


def test_read_file_says_when_a_big_file_was_cut_off(repo: Path) -> None:
    (repo / "big.txt").write_text("a\n" * 700_000)
    output = _call(repo, "read_file", path="big.txt", start_line=499_999, end_line=600_000)
    assert "truncated at the read limit" in output


def test_reading_an_empty_file_says_so(repo: Path) -> None:
    (repo / "empty.py").write_text("")
    assert _call(repo, "read_file", path="empty.py") == "empty.py is empty"


@pytest.mark.parametrize("name", [".env", "secrets/prod.yaml", ".aws/credentials", "../x.py"])
def test_a_patch_against_a_hidden_path_is_never_checked(repo: Path, name: str) -> None:
    (repo / ".env").write_text("TOKEN=abc\n")
    diff = f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-TOKEN=abc\n+TOKEN=x\n"
    assert check_patch(repo, diff) is False


def test_a_patch_against_an_ordinary_file_is_still_checked(repo: Path) -> None:
    diff = (
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    assert check_patch(repo, diff) is True
