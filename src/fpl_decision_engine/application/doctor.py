"""Deterministic, read-only Touchline doctor diagnostics.

The doctor diagnoses environment, repository, path and prerequisite problems before a
Gameweek run. It never mutates the repository or operational state: every probe is a
read, and nothing is created, repaired or rewritten. Checks are collected in a fixed
order and every result carries a stable identifier, an explicit PASS/WARN/FAIL status,
a concise message and remediation guidance when relevant, so future orchestration
(#84) can invoke or adapt the report without scraping informal prose.

State-root placement follows the Issue #80 state-root/path invariant: operational state
subject to release isolation is validated against canonical resolved filesystem
identities (absolute, normalized, symlink-resolved), never path string spelling or
prefix heuristics, and equivalent logical paths always produce identical validity
decisions. When the doctor runs from an immutable release worktree (a git worktree with
detached HEAD exactly at a release tag), the configured state root must resolve outside
that worktree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import yaml

MINIMUM_PYTHON = (3, 12)


class DiagnosticStatus(StrEnum):
    """Explicit result of one deterministic doctor check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class DiagnosticCheck:
    """One deterministic doctor check result.

    ``identifier`` is the stable machine contract; message wording is human-facing and
    must not be parsed as the sole contract.
    """

    identifier: str
    status: DiagnosticStatus
    message: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "identifier": self.identifier,
            "status": self.status.value,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True)
class DoctorReport:
    """Deterministically ordered results of a full doctor run."""

    checks: tuple[DiagnosticCheck, ...]

    @property
    def ok(self) -> bool:
        """True when no check failed; WARNs are not failures."""
        return not any(check.status is DiagnosticStatus.FAIL for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [check.to_dict() for check in self.checks]}


@dataclass(frozen=True)
class GitCommandResult:
    """Captured output of one git invocation."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitRunner(Protocol):
    """Run git read-only against a working directory."""

    def run(self, args: Sequence[str], *, cwd: Path) -> GitCommandResult: ...


class SubprocessGitRunner:
    """Default git runner; a missing git binary is reported as returncode 127."""

    def run(self, args: Sequence[str], *, cwd: Path) -> GitCommandResult:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return GitCommandResult(returncode=127, stdout="", stderr=str(exc))
        return GitCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
        )


@dataclass(frozen=True)
class _GitContext:
    """Read-only snapshot of git-derived facts used by the checks."""

    repo_root: Path | None
    head_sha: str | None
    release_tag: str | None
    is_detached: bool
    is_release_worktree: bool
    porcelain: str


@dataclass(frozen=True)
class _StateRootResolution:
    """One canonical state-root resolution shared by every state-root check.

    Exactly one of ``resolved`` / ``error`` is set: either the configured state root
    resolved to a canonical ``Path``, or resolution failed with a message. All three
    ``paths.*`` checks consume this single result so every diagnostic reports the same
    filesystem identity (no TOCTOU between checks) and a resolution failure such as a
    symlink loop yields deterministic FAILs instead of raising out of ``run()``.
    """

    resolved: Path | None
    error: str | None


class DoctorService:
    """Run the deterministic doctor checks with injectable probes.

    All inputs are injectable so tests never depend on the developer's real machine:
    ``git`` replaces the git runner, ``which`` replaces tool discovery, and
    ``python_version`` replaces the interpreter version.
    """

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        state_root: Path | None = None,
        git: GitRunner | None = None,
        which: Callable[[str], str | None] | None = None,
        python_version: tuple[int, int] | None = None,
        required_directories: Sequence[Path] | None = None,
        config_file: Path | None = None,
    ) -> None:
        self.cwd = (cwd if cwd is not None else Path.cwd()).resolve()
        self.state_root = (
            state_root if state_root is not None else Path("state/run-records")
        )
        self.git = git if git is not None else SubprocessGitRunner()
        self.which = which if which is not None else shutil.which
        self.python_version = (
            python_version if python_version is not None else sys.version_info[:2]
        )
        self.required_directories = tuple(
            required_directories
            if required_directories is not None
            else (Path("data"), Path("configs"))
        )
        self.config_file = (
            config_file if config_file is not None else Path("configs/default.yaml")
        )

    def run(self) -> DoctorReport:
        """Collect every check in the fixed deterministic order."""
        git_available = self.which("git") is not None
        git_context = self._collect_git_context()
        state_root_resolution = self._resolve_state_root()
        return DoctorReport(
            (
                self._check_git_tool(),
                self._check_repository(git_context, git_available),
                self._check_commit_sha(git_context, git_available),
                self._check_release_tag(git_context, git_available),
                self._check_worktree_clean(git_context, git_available),
                self._check_state_root_resolution(state_root_resolution),
                self._check_state_root_exists(state_root_resolution),
                self._check_state_root_placement(state_root_resolution, git_context),
                self._check_required_directories(),
                self._check_uv_tool(),
                self._check_python_version(),
                self._check_config(),
            )
        )

    def _resolve_state_root(self) -> _StateRootResolution:
        """Resolve the configured state root once; failures are captured, never raised."""
        try:
            resolved = (self.cwd / self.state_root).resolve()
        except (OSError, RuntimeError) as exc:
            return _StateRootResolution(resolved=None, error=str(exc))
        return _StateRootResolution(resolved=resolved, error=None)

    def _state_root_failure(self, identifier: str, error: str) -> DiagnosticCheck:
        """Deterministic FAIL shared by all state-root checks when resolution fails."""
        return DiagnosticCheck(
            identifier,
            DiagnosticStatus.FAIL,
            f"cannot resolve state root {self.state_root}: {error}",
            "check the path spelling and the permissions of its parent directories",
        )

    # -- probes -------------------------------------------------------------

    def _collect_git_context(self) -> _GitContext:
        toplevel = self.git.run(["rev-parse", "--show-toplevel"], cwd=self.cwd)
        repo_root = Path(toplevel.stdout).resolve() if toplevel.ok else None
        head = self.git.run(["rev-parse", "HEAD"], cwd=self.cwd)
        head_sha = head.stdout if head.ok else None
        tag = self.git.run(["describe", "--exact-match", "--tags", "HEAD"], cwd=self.cwd)
        release_tag = tag.stdout if tag.ok else None
        symbolic = self.git.run(["symbolic-ref", "-q", "HEAD"], cwd=self.cwd)
        is_detached = not symbolic.ok
        porcelain = self.git.run(
            ["--no-optional-locks", "status", "--porcelain"], cwd=self.cwd
        ).stdout
        # An immutable release worktree is a checkout pinned at a release tag:
        # detached HEAD exactly at a tag. Branch checkouts, even at the same commit,
        # remain mutable development checkouts.
        is_release_worktree = (
            repo_root is not None and release_tag is not None and is_detached
        )
        return _GitContext(
            repo_root=repo_root,
            head_sha=head_sha,
            release_tag=release_tag,
            is_detached=is_detached,
            is_release_worktree=is_release_worktree,
            porcelain=porcelain,
        )

    # -- checks -------------------------------------------------------------

    def _check_git_tool(self) -> DiagnosticCheck:
        if self.which("git") is None:
            return DiagnosticCheck(
                "tools.git",
                DiagnosticStatus.FAIL,
                "required tool 'git' is not installed",
                "install git and ensure it is on PATH (e.g. 'apt install git' or "
                "'brew install git')",
            )
        return DiagnosticCheck(
            "tools.git", DiagnosticStatus.PASS, "required tool 'git' is available"
        )

    def _check_repository(self, ctx: _GitContext, git_available: bool) -> DiagnosticCheck:
        if not git_available:
            return DiagnosticCheck(
                "git.repository",
                DiagnosticStatus.FAIL,
                "cannot inspect the repository: git is unavailable",
                "install git and re-run doctor",
            )
        if ctx.repo_root is None:
            return DiagnosticCheck(
                "git.repository",
                DiagnosticStatus.FAIL,
                f"{self.cwd} is not inside a git repository",
                "run doctor from inside the Touchline checkout (the directory containing .git)",
            )
        return DiagnosticCheck(
            "git.repository",
            DiagnosticStatus.PASS,
            f"inside git repository at {ctx.repo_root}",
        )

    def _check_commit_sha(self, ctx: _GitContext, git_available: bool) -> DiagnosticCheck:
        if not git_available:
            return DiagnosticCheck(
                "git.commit_sha",
                DiagnosticStatus.FAIL,
                "cannot resolve the commit at HEAD: git is unavailable",
                "install git and re-run doctor",
            )
        if ctx.head_sha is None:
            return DiagnosticCheck(
                "git.commit_sha",
                DiagnosticStatus.FAIL,
                "cannot resolve a commit at HEAD",
                "make an initial commit or check out an existing branch or tag",
            )
        return DiagnosticCheck(
            "git.commit_sha", DiagnosticStatus.PASS, f"commit {ctx.head_sha}"
        )

    def _check_release_tag(self, ctx: _GitContext, git_available: bool) -> DiagnosticCheck:
        if not git_available:
            return DiagnosticCheck(
                "git.release_tag",
                DiagnosticStatus.WARN,
                "cannot determine release identity: git is unavailable",
                "install git and re-run doctor",
            )
        if ctx.repo_root is None:
            return DiagnosticCheck(
                "git.release_tag",
                DiagnosticStatus.WARN,
                "cannot determine release identity: not inside a git repository",
                "run doctor from inside the Touchline checkout",
            )
        if ctx.release_tag is not None:
            note = (
                " (immutable release worktree)" if ctx.is_release_worktree else ""
            )
            return DiagnosticCheck(
                "git.release_tag",
                DiagnosticStatus.PASS,
                f"release tag: {ctx.release_tag}{note}",
            )
        return DiagnosticCheck(
            "git.release_tag",
            DiagnosticStatus.PASS,
            "no release tag at HEAD (development checkout)",
        )

    def _check_worktree_clean(
        self, ctx: _GitContext, git_available: bool
    ) -> DiagnosticCheck:
        if not git_available:
            return DiagnosticCheck(
                "git.worktree_clean",
                DiagnosticStatus.WARN,
                "cannot determine working-tree state: git is unavailable",
                "install git and re-run doctor",
            )
        if ctx.repo_root is None:
            return DiagnosticCheck(
                "git.worktree_clean",
                DiagnosticStatus.WARN,
                "cannot determine working-tree state: not inside a git repository",
                "run doctor from inside the Touchline checkout",
            )
        changed = ctx.porcelain.splitlines()
        if not changed:
            return DiagnosticCheck(
                "git.worktree_clean", DiagnosticStatus.PASS, "working tree is clean"
            )
        summary = f"working tree is not clean ({len(changed)} changed path(s))"
        if ctx.is_release_worktree:
            return DiagnosticCheck(
                "git.worktree_clean",
                DiagnosticStatus.FAIL,
                summary,
                "an immutable release worktree must stay clean: commit, stash or revert "
                "the changes (or run from a separate worktree)",
            )
        return DiagnosticCheck(
            "git.worktree_clean",
            DiagnosticStatus.WARN,
            summary,
            "commit or stash changes before an authoritative Gameweek run",
        )

    def _check_state_root_resolution(
        self, rs: _StateRootResolution
    ) -> DiagnosticCheck:
        if rs.error is not None:
            return self._state_root_failure("paths.state_root_resolution", rs.error)
        resolved = rs.resolved
        assert resolved is not None
        if resolved.exists() and not resolved.is_dir():
            return DiagnosticCheck(
                "paths.state_root_resolution",
                DiagnosticStatus.FAIL,
                f"state root {resolved} exists but is not a directory",
                f"move or remove the file and create a directory at {resolved}",
            )
        return DiagnosticCheck(
            "paths.state_root_resolution",
            DiagnosticStatus.PASS,
            f"state root '{self.state_root}' resolves to {resolved}",
        )

    def _check_state_root_exists(self, rs: _StateRootResolution) -> DiagnosticCheck:
        if rs.error is not None:
            return self._state_root_failure("paths.state_root_exists", rs.error)
        resolved = rs.resolved
        assert resolved is not None
        if resolved.is_dir():
            return DiagnosticCheck(
                "paths.state_root_exists",
                DiagnosticStatus.PASS,
                f"state root exists at {resolved}",
            )
        if resolved.exists():
            # An existing non-directory was already flagged by the resolution check;
            # mirror that verdict instead of emitting the misleading "does not exist"
            # WARN for a path that clearly exists.
            return DiagnosticCheck(
                "paths.state_root_exists",
                DiagnosticStatus.FAIL,
                f"state root {resolved} exists but is not a directory",
                f"move or remove the file and create a directory at {resolved}",
            )
        return DiagnosticCheck(
            "paths.state_root_exists",
            DiagnosticStatus.WARN,
            f"state root {resolved} does not exist yet",
            f"create it with 'mkdir -p {resolved}' (it is also created automatically on "
            "first run-record write)",
        )

    def _check_state_root_placement(
        self, rs: _StateRootResolution, ctx: _GitContext
    ) -> DiagnosticCheck:
        if rs.error is not None:
            return self._state_root_failure("paths.state_root_placement", rs.error)
        resolved = rs.resolved
        assert resolved is not None
        if ctx.repo_root is None:
            return DiagnosticCheck(
                "paths.state_root_placement",
                DiagnosticStatus.PASS,
                f"state root {resolved}: no release-isolation check outside a git repository",
            )
        if ctx.is_release_worktree:
            if resolved == ctx.repo_root or resolved.is_relative_to(ctx.repo_root):
                return DiagnosticCheck(
                    "paths.state_root_placement",
                    DiagnosticStatus.FAIL,
                    f"state root {resolved} is inside the immutable release worktree "
                    f"{ctx.repo_root}",
                    "point --state-root outside the release worktree, for example a "
                    f"sibling directory such as {ctx.repo_root.parent / 'state'}, so "
                    "operational state never mutates the release tree",
                )
            return DiagnosticCheck(
                "paths.state_root_placement",
                DiagnosticStatus.PASS,
                f"state root {resolved} is outside the immutable release worktree "
                f"{ctx.repo_root}",
            )
        return DiagnosticCheck(
            "paths.state_root_placement",
            DiagnosticStatus.PASS,
            f"state root {resolved} is not inside an immutable release worktree "
            "(development checkout: repository-local state permitted)",
        )

    def _check_required_directories(self) -> DiagnosticCheck:
        missing = [
            path for path in self.required_directories if not (self.cwd / path).is_dir()
        ]
        if not missing:
            names = ", ".join(str(path) for path in self.required_directories)
            return DiagnosticCheck(
                "dirs.required",
                DiagnosticStatus.PASS,
                f"required directories present: {names}",
            )
        listing = ", ".join(str(path) for path in missing)
        commands = " && ".join(f"mkdir -p {self.cwd / path}" for path in missing)
        return DiagnosticCheck(
            "dirs.required",
            DiagnosticStatus.WARN,
            f"required directories missing: {listing}",
            f"create them with: {commands} (they are also created automatically on first "
            "write, but pre-creating them fails fast)",
        )

    def _check_uv_tool(self) -> DiagnosticCheck:
        if self.which("uv") is None:
            return DiagnosticCheck(
                "tools.uv",
                DiagnosticStatus.FAIL,
                "required tool 'uv' is not installed",
                "install uv (https://docs.astral.sh/uv/), the project's documented runner",
            )
        return DiagnosticCheck(
            "tools.uv", DiagnosticStatus.PASS, "required tool 'uv' is available"
        )

    def _check_python_version(self) -> DiagnosticCheck:
        reported = f"{self.python_version[0]}.{self.python_version[1]}"
        if self.python_version < MINIMUM_PYTHON:
            return DiagnosticCheck(
                "python.version",
                DiagnosticStatus.FAIL,
                f"Python {reported} is older than the required 3.12",
                'use Python 3.12+ (pyproject declares requires-python = ">=3.12")',
            )
        return DiagnosticCheck(
            "python.version",
            DiagnosticStatus.PASS,
            f"Python {reported} satisfies requires-python >=3.12",
        )

    def _check_config(self) -> DiagnosticCheck:
        path = self.cwd / self.config_file
        if not path.exists():
            return DiagnosticCheck(
                "config.default_file",
                DiagnosticStatus.PASS,
                f"no config file at {path}; Touchline currently requires only the "
                "--state-root value, validated by the paths checks above",
            )
        try:
            with path.open(encoding="utf-8") as stream:
                yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as exc:
            return DiagnosticCheck(
                "config.default_file",
                DiagnosticStatus.FAIL,
                f"config file {path} is not valid YAML: {exc}",
                f"restore a valid file ('git checkout -- {path}') or fix the reported "
                "YAML error",
            )
        return DiagnosticCheck(
            "config.default_file",
            DiagnosticStatus.PASS,
            f"config file {path} parses as valid YAML",
        )
