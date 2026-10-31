"""Read-only tools the investigator uses on the target checkout.

Ticket text is untrusted, so a ticket can try to steer the model. These tools limit what
that can achieve: they cannot write, cannot leave the root, and refuse common credential
paths. That last one is a denylist, so it reduces the risk rather than removing it. A
reviewer reading the proposal is the real control.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

from langchain_core.tools import BaseTool, tool

MAX_FILE_CHARS = 30_000
MAX_SEARCH_RESULTS = 40
MAX_LISTED_FILES = 500
GIT_TIMEOUT_SECONDS = 15

# Generated or vendored, so they add noise and hide nothing a reviewer would miss.
_NOISE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "cdk.out",
}
# Build output at the top level, but ordinary source deeper down, as in Go's cmd/build.
_ROOT_NOISE_DIRS = {"dist", "build"}
_SENSITIVE_DIRS = {".aws", ".ssh", ".gnupg", ".kube", ".docker"}
# Config inside these is probably secret, but source code inside them is just code.
_SECRET_DIR_NAMES = {"secret", "secrets", "credentials"}
# Code that handles secrets is what a ticket is often about, and rarely where they are stored.
_SOURCE_SUFFIXES = {
    ".c",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".rst",
    ".ts",
    ".tsx",
}
_SENSITIVE_NAMES = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*credentials*",
    "*secret*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    ".dockerconfigjson",
    ".dockercfg",
    "*.tfstate",
    "*.tfstate.*",
    "*.tfvars",
    "service-account*.json",
    "service_account*.json",
    "kubeconfig",
)


class _PathRefused(Exception):
    pass


def _is_noise(parts: tuple[str, ...]) -> bool:
    return bool(parts) and (
        parts[0] in _ROOT_NOISE_DIRS or any(part in _NOISE_DIRS for part in parts)
    )


def _is_sensitive(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if not parts:
        return False
    if any(part in _SENSITIVE_DIRS for part in parts):
        return True
    if Path(parts[-1]).suffix in _SOURCE_SUFFIXES:
        return False
    if any(part in _SECRET_DIR_NAMES for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], pattern) for pattern in _SENSITIVE_NAMES)


def _is_hidden_from_agent(relative: Path) -> bool:
    return _is_noise(relative.parts) or _is_sensitive(relative)


def _resolve(root: Path, path: str) -> Path:
    try:
        # resolve() follows symlinks, so a link pointing out of the root is caught here too.
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise _PathRefused(f"{path} is outside the repository")
        # Check where a link points and what it is called, so neither name can hide a secret.
        requested = Path(os.path.normpath(root / path)).relative_to(root)
        if _is_hidden_from_agent(resolved.relative_to(root)) or _is_hidden_from_agent(requested):
            raise _PathRefused(f"{path} is not available to the agent")
    except (OSError, ValueError, RuntimeError) as exc:
        # NUL bytes, over-long names, symlink loops. The model gets an error it can act on.
        raise _PathRefused(f"{path!r} is not a usable path") from exc
    return resolved


def _walk(root: Path, start: Path) -> tuple[list[Path], int]:
    """Return the regular files the agent may see, and how many sensitive paths were withheld."""
    visible: list[Path] = []
    withheld = 0
    for directory, subdirs, files in os.walk(start):
        here = Path(directory).relative_to(root)
        kept = []
        for name in sorted(subdirs):
            if _is_noise((here / name).parts):
                continue
            if name.lower() in _SENSITIVE_DIRS:
                withheld += 1
                continue
            kept.append(name)
        subdirs[:] = kept
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root)
            if _is_sensitive(relative):
                withheld += 1
            # is_file() also skips FIFOs, which would block a read forever.
            elif not _is_noise(relative.parts) and not path.is_symlink() and path.is_file():
                visible.append(path)
    return visible, withheld


def _withheld_note(withheld: int) -> str:
    # Otherwise "No matches." reads the same as the code not existing.
    return f"{withheld} file(s) hidden by policy were not included." if withheld else ""


MAX_READ_BYTES = 1_000_000


def _read_text(path: Path) -> str | None:
    # Cap the read, so one huge log or dump in the checkout cannot exhaust memory.
    with path.open("rb") as file:
        data = file.read(MAX_READ_BYTES)
        if file.read(1):
            data += b"\n[... file truncated at the read limit ...]"
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def make_read_only_tools(root: Path) -> list[BaseTool]:
    """Build the tools bound to one checkout."""
    root = root.resolve()

    @tool
    def list_files(path: str = ".") -> str:
        """List source files under a directory of the repository, relative to its root."""
        try:
            start = _resolve(root, path)
        except _PathRefused as exc:
            return f"ERROR: {exc}"
        if not start.is_dir():
            return f"ERROR: {path} is not a directory"
        files, withheld = _walk(root, start)
        listed = [str(file.relative_to(root)) for file in files[:MAX_LISTED_FILES]]
        if len(files) > MAX_LISTED_FILES:
            listed.append(f"... stopped at {MAX_LISTED_FILES} files, list a subdirectory")
        if note := _withheld_note(withheld):
            listed.append(note)
        return "\n".join(listed) or "No files."

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read a text file with line numbers. Optionally limit it to a line range."""
        try:
            resolved = _resolve(root, path)
        except _PathRefused as exc:
            return f"ERROR: {exc}"
        try:
            exists = resolved.is_file()
        except OSError:
            exists = False
        if not exists:
            return f"ERROR: {path} does not exist"
        try:
            text = _read_text(resolved)
        except OSError as exc:
            return f"ERROR: could not read {path}: {exc.strerror}"
        if text is None:
            return f"ERROR: {path} is a binary file"
        lines = text.splitlines()
        first = max(start_line, 1)
        if end_line is not None and end_line < first:
            return f"ERROR: end_line {end_line} is before start_line {first}"
        if first > len(lines):
            return f"ERROR: {path} has only {len(lines)} line(s)"
        last = len(lines) if end_line is None else min(end_line, len(lines))
        numbered = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
        if len(numbered) > MAX_FILE_CHARS:
            return numbered[:MAX_FILE_CHARS] + "\n... truncated, read a smaller line range"
        return numbered

    @tool
    def search_code(query: str) -> str:
        """Search the repository for literal text, ignoring case. Returns file:line: text."""
        needle = query.lower()
        matches: list[str] = []
        total = 0
        files, withheld = _walk(root, root)
        note = _withheld_note(withheld)
        for file in files:
            try:
                text = _read_text(file)
            except OSError:
                # One unreadable file must not hide the rest of the repository.
                continue
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    total += 1
                    if len(matches) < MAX_SEARCH_RESULTS:
                        matches.append(f"{file.relative_to(root)}:{number}: {line.strip()[:200]}")
        if not matches:
            return f"No matches. {note}".strip()
        if total > len(matches):
            matches.append(f"... {total - len(matches)} more matches, narrow the query")
        if note:
            matches.append(note)
        return "\n".join(matches)

    return [list_files, read_file, search_code]


def check_patch(root: Path, diff: str) -> bool | None:
    """Report whether `diff` applies cleanly, without changing any file.

    Returns None when git is unavailable, so "could not check" stays distinct from "does
    not apply".
    """
    root = root.resolve()
    # Stop git discovering a parent repository. From a subdirectory of one, `git apply` reads
    # `diff --git` paths as relative to the top level, skips them as out of scope, and exits 0.
    env = os.environ | {"GIT_CEILING_DIRECTORIES": str(root.parent)}
    try:
        result = subprocess.run(
            ["git", "apply", "--check", "-"],  # noqa: S607
            cwd=root,
            env=env,
            input=diff,
            text=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except UnicodeError:
        return False  # Text that cannot be encoded cannot be a patch for this checkout.
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0
