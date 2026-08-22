"""Tests for the deterministic Touchline doctor diagnostics service.

The service-level tests build controlled temporary git repositories and inject the
tool/python probes so they never depend on the developer's real machine configuration.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from fpl_decision_engine.application.doctor import (
    DiagnosticCheck,
    DiagnosticStatus,
    DoctorReport,
    DoctorService,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    _git(repo, "add", ".")
    assert _git(repo, "commit", "-qm", "initial").returncode == 0
    return repo


def _tag_release_and_detach(repo: Path, tag: str = "v1.0.0") -> None:
    """Turn the checkout into an immutable release worktree (detached at a tag)."""
    assert _git(repo, "tag", tag).returncode == 0
    assert _git(repo, "checkout", "-q", tag).returncode == 0
    assert _git(repo, "symbolic-ref", "-q", "HEAD").returncode != 0


def _doctor(
    repo: Path,
    *,
    state_root: str | Path = "state/run-records",
    python_version: tuple[int, int] = (3, 12),
    required_directories: tuple[Path, ...] = (),
    which=None,
) -> DoctorService:
    return DoctorService(
        cwd=repo,
        state_root=Path(state_root),
        which=which if which is not None else (lambda name: f"/usr/bin/{name}"),
        python_version=python_version,
        required_directories=required_directories,
    )


def _check(report: DoctorReport, identifier: str) -> DiagnosticCheck:
    return next(check for check in report.checks if check.identifier == identifier)


def _status(report: DoctorReport, identifier: str) -> DiagnosticStatus:
    return _check(report, identifier).status


def _snapshot(root: Path) -> dict[str, str]:
    """Content snapshot of a tree; symlinked directories are not followed."""
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        relative = Path(dirpath).relative_to(root)
        prefix = "" if str(relative) == "." else f"{relative}/"
        for name in sorted(dirnames):
            entry = Path(dirpath) / name
            key = f"{prefix}{name}"
            if entry.is_symlink():
                result[key] = "-> dir " + str(Path(os.readlink(entry)).resolve())
            else:
                result[key] = "dir"
        for name in sorted(filenames):
            entry = Path(dirpath) / name
            key = f"{prefix}{name}"
            if entry.is_symlink():
                result[key] = "-> " + str(Path(os.readlink(entry)).resolve())
            else:
                result[key] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return result


def test_healthy_doctor_on_clean_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "data").mkdir()
    (repo / "configs").mkdir()
    (repo / "state").mkdir()

    report = _doctor(repo, state_root="state").run()

    assert report.ok
    assert _status(report, "tools.git") is DiagnosticStatus.PASS
    assert _status(report, "git.repository") is DiagnosticStatus.PASS
    assert _status(report, "git.commit_sha") is DiagnosticStatus.PASS
    assert "commit" in _check(report, "git.commit_sha").message
    assert _status(report, "git.release_tag") is DiagnosticStatus.PASS
    assert "development checkout" in _check(report, "git.release_tag").message
    assert _status(report, "git.worktree_clean") is DiagnosticStatus.PASS
    assert _status(report, "paths.state_root_resolution") is DiagnosticStatus.PASS
    assert _status(report, "paths.state_root_exists") is DiagnosticStatus.PASS
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS
    assert _status(report, "dirs.required") is DiagnosticStatus.PASS
    assert _status(report, "tools.uv") is DiagnosticStatus.PASS
    assert _status(report, "python.version") is DiagnosticStatus.PASS
    assert _status(report, "config.default_file") is DiagnosticStatus.PASS


def test_dirty_worktree_warns_but_stays_healthy(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    report = _doctor(repo).run()

    assert report.ok
    check = _check(report, "git.worktree_clean")
    assert check.status is DiagnosticStatus.WARN
    assert "not clean" in check.message
    assert check.remediation is not None


def test_dirty_release_worktree_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    report = _doctor(repo).run()

    assert not report.ok
    assert _status(report, "git.release_tag") is DiagnosticStatus.PASS
    assert "v1.0.0" in _check(report, "git.release_tag").message
    assert _status(report, "git.worktree_clean") is DiagnosticStatus.FAIL


def test_release_worktree_with_clean_tree_is_healthy(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    (tmp_path / "external-state").mkdir()

    report = _doctor(repo, state_root="../external-state").run()

    assert report.ok
    assert _status(report, "git.worktree_clean") is DiagnosticStatus.PASS
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS


def test_valid_external_state_root_in_release_worktree_is_not_rejected(
    tmp_path: Path,
) -> None:
    """Regression for the GW1 Stage 6 failure: a valid external STATE_ROOT was
    rejected because of path-resolution sensitivity around git rev-parse/is_relative_to.
    Equivalent spellings of the same external root must all pass identically.
    """
    repo = _make_repo(tmp_path, name="release")
    _tag_release_and_detach(repo)
    external = tmp_path / "state"
    external.mkdir()

    spellings = ["../state", ".././state", "../release/../state", str(external)]
    reports = [_doctor(repo, state_root=spelling).run() for spelling in spellings]

    for spelling, report in zip(spellings, reports, strict=True):
        assert report.ok, spelling
        placement = _check(report, "paths.state_root_placement")
        assert placement.status is DiagnosticStatus.PASS, spelling
        assert "outside the immutable release worktree" in placement.message, spelling
    resolved_paths = {
        _check(report, "paths.state_root_resolution").message.split("resolves to ", 1)[1]
        for report in reports
    }
    assert len(resolved_paths) == 1


def test_state_root_inside_release_worktree_fails_with_remediation(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    (repo / "state").mkdir()

    report = _doctor(repo, state_root="state").run()

    assert not report.ok
    check = _check(report, "paths.state_root_placement")
    assert check.status is DiagnosticStatus.FAIL
    assert "inside the immutable release worktree" in check.message
    assert check.remediation is not None
    assert "--state-root" in check.remediation


def test_sibling_with_shared_prefix_is_not_inside_worktree(tmp_path: Path) -> None:
    """String-prefix heuristics must not reject a sibling whose name starts with the
    worktree name; only canonical resolved identity decides placement."""
    repo = _make_repo(tmp_path, name="release")
    _tag_release_and_detach(repo)
    sibling = tmp_path / "release-state"
    sibling.mkdir()

    report = _doctor(repo, state_root="../release-state").run()

    assert report.ok
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS


def test_equivalent_normalized_paths_resolve_identically(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    (repo / "state").mkdir()
    outside = tmp_path / "state-outside"
    outside.mkdir()

    inside_spellings = ["state", "./state", "state/./", "state/../state"]
    outside_spellings = ["../state-outside", ".././state-outside"]

    inside = [_doctor(repo, state_root=s).run() for s in inside_spellings]
    for report in inside:
        assert not report.ok
        assert _status(report, "paths.state_root_placement") is DiagnosticStatus.FAIL
    outside_reports = [_doctor(repo, state_root=s).run() for s in outside_spellings]
    for report in outside_reports:
        assert report.ok
        assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS


def test_symlinked_state_root_resolves_to_its_target(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    external = tmp_path / "external-state"
    external.mkdir()
    # The symlink is tracked so the release worktree stays clean.
    os.symlink(external, repo / "state-link")
    _git(repo, "add", "state-link")
    assert _git(repo, "commit", "-qm", "add state symlink").returncode == 0
    _tag_release_and_detach(repo)

    # Spelled inside the worktree but resolving outside: must pass.
    report = _doctor(repo, state_root="state-link/run-records").run()
    assert report.ok
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS
    assert "outside the immutable release worktree" in _check(
        report, "paths.state_root_placement"
    ).message

    # A symlink that resolves back into the worktree: must fail.
    os.symlink(repo, tmp_path / "link-into-repo")
    report = _doctor(repo, state_root="../link-into-repo/state").run()
    assert not report.ok
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.FAIL


def test_state_root_exists_as_file_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "state").write_text("not a directory\n", encoding="utf-8")

    report = _doctor(repo, state_root="state").run()

    assert not report.ok
    check = _check(report, "paths.state_root_resolution")
    assert check.status is DiagnosticStatus.FAIL
    assert "not a directory" in check.message


def test_missing_state_root_warns_and_is_not_created(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    missing = repo / "state" / "run-records"

    report = _doctor(repo, state_root="state/run-records").run()

    assert report.ok
    check = _check(report, "paths.state_root_exists")
    assert check.status is DiagnosticStatus.WARN
    assert "does not exist" in check.message
    assert not missing.exists()


def test_missing_required_directory_warns(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    report = _doctor(
        repo, required_directories=(Path("data"), Path("configs"))
    ).run()

    assert report.ok
    check = _check(report, "dirs.required")
    assert check.status is DiagnosticStatus.WARN
    assert "data" in check.message and "configs" in check.message
    assert check.remediation is not None


def test_missing_git_tool_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    report = _doctor(repo, which=lambda name: None).run()

    assert not report.ok
    assert _status(report, "tools.git") is DiagnosticStatus.FAIL
    assert _status(report, "git.repository") is DiagnosticStatus.FAIL
    assert _status(report, "git.commit_sha") is DiagnosticStatus.FAIL


def test_missing_uv_tool_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    report = _doctor(
        repo,
        which=lambda name: None if name == "uv" else f"/usr/bin/{name}",
    ).run()

    assert not report.ok
    assert _status(report, "tools.uv") is DiagnosticStatus.FAIL


def test_python_below_required_version_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)

    report = _doctor(repo, python_version=(3, 11)).run()

    assert not report.ok
    assert _status(report, "python.version") is DiagnosticStatus.FAIL


def test_invalid_config_file_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    configs = repo / "configs"
    configs.mkdir()
    (configs / "default.yaml").write_text("season: [unclosed\n", encoding="utf-8")

    report = _doctor(repo).run()

    assert not report.ok
    assert _status(report, "config.default_file") is DiagnosticStatus.FAIL


def test_not_a_git_directory_fails(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    report = _doctor(plain).run()

    assert not report.ok
    assert _status(report, "git.repository") is DiagnosticStatus.FAIL
    assert _status(report, "git.worktree_clean") is DiagnosticStatus.WARN


def test_doctor_does_not_mutate_repository_or_operational_state(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    external = tmp_path / "external-state"
    external.mkdir()
    (external / "existing.txt").write_text("kept\n", encoding="utf-8")
    # A second worktree exercising linked-worktree detection.
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "dev-work", str(linked))

    before = _snapshot(tmp_path)

    # A healthy run and a failing run (missing state root) must both be read-only.
    healthy = _doctor(repo, state_root="../external-state").run()
    failing = _doctor(repo, state_root="state/run-records").run()
    linked_report = _doctor(linked, state_root="state").run()

    assert healthy.ok
    assert not failing.ok
    assert _status(linked_report, "git.repository") is DiagnosticStatus.PASS
    assert not (repo / "state" / "run-records").exists()
    assert _snapshot(tmp_path) == before


def test_linked_worktree_is_not_a_release_worktree(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _tag_release_and_detach(repo)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "dev-work", str(linked))
    (linked / "state").mkdir()

    report = _doctor(linked, state_root="state").run()

    assert report.ok
    tag_check = _check(report, "git.release_tag")
    assert tag_check.status is DiagnosticStatus.PASS
    # The linked worktree sits on a branch at the tagged commit: it reports the tag
    # but is a mutable development checkout, not an immutable release worktree.
    assert "v1.0.0" in tag_check.message
    assert "immutable release worktree" not in tag_check.message
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS


def _detach_at_head_commit(repo: Path) -> None:
    """Detach HEAD at the (untagged) current commit."""
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert _git(repo, "checkout", "-q", sha).returncode == 0
    assert _git(repo, "symbolic-ref", "-q", "HEAD").returncode != 0


def test_detached_head_without_release_tag_is_ordinary_checkout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _detach_at_head_commit(repo)

    report = _doctor(repo).run()

    assert report.ok
    tag = _check(report, "git.release_tag")
    assert tag.status is DiagnosticStatus.PASS
    assert "no release tag" in tag.message
    assert "immutable release worktree" not in tag.message
    assert _status(report, "git.worktree_clean") is DiagnosticStatus.PASS
    assert _status(report, "paths.state_root_placement") is DiagnosticStatus.PASS


def test_dirty_detached_head_without_tag_warns_not_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _detach_at_head_commit(repo)
    (repo / "file.txt").write_text("changed\n", encoding="utf-8")

    report = _doctor(repo).run()

    assert report.ok
    check = _check(report, "git.worktree_clean")
    assert check.status is DiagnosticStatus.WARN
    assert "not clean" in check.message
    assert _status(report, "git.release_tag") is DiagnosticStatus.PASS


def _patch_state_root_resolution_failure(
    monkeypatch, target: Path, exc: Exception
) -> list[str]:
    """Make Path.resolve raise ``exc`` for the state-root path; record every call.

    The doctor must resolve the state root exactly once and capture the failure; any
    independent re-resolution inside a later check would raise again out of run().
    """
    target_str = str(target)
    calls: list[str] = []
    original_resolve = Path.resolve

    def failing_resolve(self: Path, strict: bool = False) -> Path:
        self_str = str(self)
        calls.append(self_str)
        if self_str == target_str:
            raise exc
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", failing_resolve)
    return calls


def test_state_root_symlink_loop_resolution_failure_is_deterministic_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """A symlink-loop RuntimeError during resolution must yield deterministic FAILs,
    never crash run(), and must not be re-resolved by later checks."""
    repo = _make_repo(tmp_path)
    target = (repo / "state").resolve()
    calls = _patch_state_root_resolution_failure(
        monkeypatch, target, RuntimeError(f"Symlink loop from {target}")
    )

    report = _doctor(repo, state_root="state").run()

    assert not report.ok
    for identifier in (
        "paths.state_root_resolution",
        "paths.state_root_exists",
        "paths.state_root_placement",
    ):
        check = _check(report, identifier)
        assert check.status is DiagnosticStatus.FAIL, identifier
        assert "cannot resolve state root" in check.message, identifier
        assert check.remediation is not None, identifier
    assert calls.count(str(target)) == 1


def test_state_root_oserror_resolution_failure_is_deterministic_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """An OSError (e.g. ELOOP from the kernel) during resolution behaves identically."""
    repo = _make_repo(tmp_path)
    target = (repo / "state").resolve()
    calls = _patch_state_root_resolution_failure(
        monkeypatch, target, OSError(40, "Too many levels of symbolic links")
    )

    report = _doctor(repo, state_root="state").run()

    assert not report.ok
    for identifier in (
        "paths.state_root_resolution",
        "paths.state_root_exists",
        "paths.state_root_placement",
    ):
        check = _check(report, identifier)
        assert check.status is DiagnosticStatus.FAIL, identifier
        assert "cannot resolve state root" in check.message, identifier
    assert calls.count(str(target)) == 1


def test_state_root_existing_file_has_consistent_diagnostics(tmp_path: Path) -> None:
    """An existing file used as state root is invalid everywhere: never a misleading
    "does not exist" WARN from the existence check."""
    repo = _make_repo(tmp_path)
    (repo / "state").write_text("not a directory\n", encoding="utf-8")

    report = _doctor(repo, state_root="state").run()

    assert not report.ok
    resolution = _check(report, "paths.state_root_resolution")
    exists = _check(report, "paths.state_root_exists")
    assert resolution.status is DiagnosticStatus.FAIL
    assert "not a directory" in resolution.message
    assert exists.status is DiagnosticStatus.FAIL
    assert "not a directory" in exists.message
    assert "does not exist" not in exists.message
    assert exists.remediation is not None
