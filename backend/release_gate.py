"""Offline-first milestone audit, recovery, and protected-ref gate helpers.

The module intentionally has no function that writes a Git remote.  The final
gate proves that a single ordinary fast-forward refspec is safe; a separately
authorised operator must still perform that push.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HEX40 = re.compile(r"^[0-9a-f]{40}$")
PASS = "PASS"


class ReleaseGateError(RuntimeError):
    """A fail-closed release or recovery check failed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tempdir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    root = os.environ.get("ECHO_PACT_TEMP_ROOT")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=root or None)


def _run(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(args), cwd=cwd, env=env, text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleaseGateError(
            f"command failed ({completed.returncode}): {' '.join(args)}: {detail}"
        )
    return completed


def _git(repo: str | Path, *args: str, check: bool = True) -> str:
    return _run(
        ["git", "--no-optional-locks", "-C", str(repo), *args], check=check
    ).stdout.strip()


def _require_sha(value: str, label: str) -> str:
    value = value.strip().lower()
    if not HEX40.fullmatch(value):
        raise ReleaseGateError(f"{label} must be a full lowercase Git SHA-1")
    return value


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_new(path: str | Path, value: Any) -> None:
    target = Path(path)
    if target.exists():
        raise ReleaseGateError(f"refusing to overwrite: {target}")
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_text_new(path: str | Path, value: str) -> None:
    target = Path(path)
    if target.exists():
        raise ReleaseGateError(f"refusing to overwrite: {target}")
    target.write_text(value, encoding="utf-8", newline="\n")


def repo_state(repo: str | Path) -> dict[str, Any]:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    branch = _git(repo, "branch", "--show-current")
    head = _require_sha(_git(repo, "rev-parse", "HEAD"), "HEAD")
    tree = _require_sha(_git(repo, "rev-parse", "HEAD^{tree}"), "tree")
    parents = _git(repo, "show", "-s", "--format=%P", head).split()
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    upstream = _git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name",
        "@{upstream}", check=False,
    )
    return {
        "root": str(root), "branch": branch, "head": head, "tree": tree,
        "parents": parents, "clean": not bool(dirty),
        "status_porcelain": dirty.splitlines(), "upstream": upstream or None,
    }


def remote_heads(repo: str | Path, remote: str) -> dict[str, str]:
    command = ["git", "--no-optional-locks", "-C", str(repo)]
    proxy = os.environ.get("ECHO_PACT_GIT_HTTP_PROXY")
    if proxy:
        if not re.fullmatch(r"https?://127\.0\.0\.1:\d{1,5}", proxy):
            raise ReleaseGateError(
                "ECHO_PACT_GIT_HTTP_PROXY must be a loopback HTTP proxy"
            )
        command.extend(["-c", f"http.proxy={proxy}"])
    output = _run([*command, "ls-remote", "--heads", remote]).stdout.strip()
    heads: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        sha, ref = line.split(None, 1)
        heads[ref.strip()] = _require_sha(sha, f"remote {ref.strip()}")
    return dict(sorted(heads.items()))


def _checksums_for(directory: Path, excluded: Iterable[str] = ()) -> list[str]:
    excluded_set = set(excluded)
    lines = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        if rel in excluded_set:
            continue
        if path.stat().st_size <= 0:
            raise ReleaseGateError(f"recovery file is empty: {rel}")
        lines.append(f"{sha256_file(path)}  {rel}")
    return lines


def _safe_recovery_ref(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name:
        raise ReleaseGateError("unsafe recovery ref name")
    ref = f"refs/recovery-snapshot/{name.strip('/')}"
    if _run(["git", "check-ref-format", ref], check=False).returncode:
        raise ReleaseGateError(f"invalid recovery ref: {ref}")
    return ref


def create_recovery_package(
    repo: str | Path,
    output_dir: str | Path,
    *,
    head_sha: str,
    snapshot_name: str,
    remote: str | None = None,
) -> dict[str, Any]:
    """Create an atomic, independently verifiable full-history Git bundle."""
    repo = Path(repo).resolve()
    output = Path(output_dir).resolve()
    head_sha = _require_sha(head_sha, "recovery head")
    state = repo_state(repo)
    if not state["clean"]:
        raise ReleaseGateError("source worktree is not clean")
    if output.exists():
        raise ReleaseGateError(f"refusing to overwrite recovery package: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if _git(repo, "cat-file", "-t", head_sha) != "commit":
        raise ReleaseGateError("recovery head is not a commit")

    logical_ref = _safe_recovery_ref(snapshot_name)
    staged = output.parent / f".{output.name}.tmp-{uuid.uuid4().hex}"
    staged.mkdir()
    try:
        scratch = staged / "_scratch.git"
        _run(["git", "clone", "--bare", "--no-hardlinks", str(repo), str(scratch)])
        _git(scratch, "update-ref", logical_ref, head_sha)
        bundle = staged / "repository.bundle"
        _git(scratch, "bundle", "create", str(bundle), logical_ref)
        shutil.rmtree(scratch)

        parent_line = _git(repo, "show", "-s", "--format=%P", head_sha)
        source_remote_url = None
        actual_remote_heads: dict[str, str] = {}
        if remote:
            source_remote_url = _git(repo, "remote", "get-url", remote)
            if re.search(r"https?://[^/@]+@", source_remote_url, re.I):
                raise ReleaseGateError(
                    "remote URL contains embedded credentials; refusing to record it"
                )
            actual_remote_heads = remote_heads(repo, remote)
        manifest = {
            "schema": "echo-pact-recovery-manifest-v1",
            "created_at": _now(),
            "source_repo": str(repo),
            "source_branch": state["branch"],
            "source_remote": remote,
            "source_remote_url": source_remote_url,
            "remote_heads": actual_remote_heads,
            "snapshot_ref": logical_ref,
            "head": head_sha,
            "tree": _require_sha(_git(repo, "rev-parse", f"{head_sha}^{{tree}}"), "tree"),
            "parents": parent_line.split() if parent_line else [],
            "bundle": "repository.bundle",
            "bundle_sha256": sha256_file(bundle),
        }
        _write_json_new(staged / "manifest.json", manifest)
        _write_text_new(
            staged / "RECOVERY.md",
            "# Echo Pact recovery package\n\n"
            f"Snapshot ref: `{logical_ref}`  \n"
            f"Commit: `{head_sha}`  \n"
            f"Tree: `{manifest['tree']}`\n\n"
            "Verify: `python scripts/recovery_bundle.py verify --package <dir>`\n\n"
            "Restore into a new bare repository:\n\n"
            "```powershell\n"
            "git init --bare recovered.git\n"
            f"git -C recovered.git fetch <dir>/repository.bundle "
            f"{logical_ref}:{logical_ref}\n"
            f"git -C recovered.git fsck --full\n"
            "```\n\n"
            "To obtain a working tree, clone that recovered bare repository and "
            f"create a branch from `{head_sha}`. Never restore over the source repo.\n",
        )
        checksum_lines = _checksums_for(staged, excluded={"SHA256SUMS.txt"})
        _write_text_new(staged / "SHA256SUMS.txt", "\n".join(checksum_lines) + "\n")
        verify_recovery_package(staged)
        staged.rename(output)
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return verify_recovery_package(output)


def verify_recovery_package(package_dir: str | Path) -> dict[str, Any]:
    package = Path(package_dir).resolve()
    manifest_path = package / "manifest.json"
    sums_path = package / "SHA256SUMS.txt"
    if not package.is_dir() or not manifest_path.is_file() or not sums_path.is_file():
        raise ReleaseGateError("recovery package is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "echo-pact-recovery-manifest-v1":
        raise ReleaseGateError("unknown recovery manifest schema")
    head = _require_sha(manifest.get("head", ""), "manifest head")
    tree = _require_sha(manifest.get("tree", ""), "manifest tree")
    ref = manifest.get("snapshot_ref", "")
    if ref != _safe_recovery_ref(ref.removeprefix("refs/recovery-snapshot/")):
        raise ReleaseGateError("manifest recovery ref is invalid")
    bundle = package / manifest.get("bundle", "")
    if not bundle.is_file() or bundle.stat().st_size <= 0:
        raise ReleaseGateError("bundle is missing or empty")
    if sha256_file(bundle) != manifest.get("bundle_sha256"):
        raise ReleaseGateError("bundle SHA-256 mismatch")

    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            expected, rel = line.split("  ", 1)
        except ValueError as exc:
            raise ReleaseGateError("malformed SHA256SUMS.txt") from exc
        target = (package / rel).resolve()
        if package not in target.parents or not target.is_file():
            raise ReleaseGateError(f"unsafe or missing checksum target: {rel}")
        if target.stat().st_size <= 0 or sha256_file(target) != expected:
            raise ReleaseGateError(f"checksum mismatch: {rel}")

    with _tempdir("echopact-recovery-verify-") as tmp:
        restored = Path(tmp) / "restored.git"
        _run(["git", "init", "--bare", str(restored)])
        verify = _git(restored, "bundle", "verify", str(bundle))
        _git(restored, "fetch", str(bundle), f"{ref}:{ref}")
        restored_head = _require_sha(_git(restored, "rev-parse", ref), "restored head")
        restored_tree = _require_sha(
            _git(restored, "rev-parse", f"{ref}^{{tree}}"), "restored tree"
        )
        restored_parents = _git(restored, "show", "-s", "--format=%P", ref).split()
        fsck = _git(restored, "fsck", "--full", "--strict")
    if restored_head != head or restored_tree != tree:
        raise ReleaseGateError("independent restore does not match manifest")
    if restored_parents != manifest.get("parents", []):
        raise ReleaseGateError("restored parent list does not match manifest")
    return {
        "verdict": PASS, "package": str(package), "head": head, "tree": tree,
        "parents": restored_parents, "snapshot_ref": ref,
        "bundle_sha256": manifest["bundle_sha256"],
        "bundle_verify": PASS if verify is not None else PASS,
        "independent_restore": PASS, "git_fsck": PASS,
        "fsck_output": fsck.splitlines(),
    }


_ROUTE_PREFIX = re.compile(r"APIRouter\s*\(.*?prefix\s*=\s*['\"]([^'\"]*)", re.S)
_ROUTE = re.compile(r"@router\.(get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]*)")


def routes_at(repo: str | Path, commit: str, path: str = "backend/trigger/routes.py") -> list[str]:
    shown = _git(repo, "show", f"{commit}:{path}", check=False)
    if not shown:
        return []
    # ``routes.py`` is mounted under /api by trigger/main.py. Keep that stable
    # application prefix explicit rather than pretending router-local paths are
    # complete external endpoints.
    prefix_match = _ROUTE_PREFIX.search(shown)
    prefix = prefix_match.group(1).rstrip("/") if prefix_match else "/api"
    return sorted({f"{prefix}/{route.lstrip('/')}".rstrip("/") or "/"
                   for _, route in _ROUTE.findall(shown)})


def _probe_schema() -> dict[str, Any]:
    from backend.memory.records_v1 import migrate_records_db

    with _tempdir("echopact-schema-probe-") as tmp:
        db_path = Path(tmp) / "probe.db"
        result = migrate_records_db(str(db_path))
        conn = sqlite3.connect(db_path)
        try:
            tables = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) if not row[0].startswith("sqlite_"))
            triggers = sorted(row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            ))
            versions = [row[0] for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )]
        finally:
            conn.close()
    return {"migration": result, "versions": versions,
            "tables": tables, "triggers": triggers}


def _run_validation(repo: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    with _tempdir("echopact-milestone-validation-") as tmp:
        pytest_tmp = str(Path(tmp) / "pytest")
        tests = _run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p",
             "no:cacheprovider", "-p", "no:randomly", f"--basetemp={pytest_tmp}"],
            cwd=repo, env=env, check=False,
        )
        combined = (tests.stdout + "\n" + tests.stderr).strip()
        passed_match = re.search(r"(\d+) passed", combined)
        if tests.returncode or not passed_match:
            raise ReleaseGateError(f"full test suite failed:\n{combined[-4000:]}")
        rehearsal_path = Path(tmp) / "identity-rehearsal.json"
        rehearsal = _run(
            [sys.executable, "scripts/rehearsal_identity.py", "--out",
             str(rehearsal_path)], cwd=repo, env=env, check=False,
        )
        if rehearsal.returncode or not rehearsal_path.is_file():
            detail = (rehearsal.stdout + "\n" + rehearsal.stderr).strip()
            raise ReleaseGateError(f"identity rehearsal failed: {detail[-4000:]}")
        rehearsal_data = json.loads(rehearsal_path.read_text(encoding="utf-8"))
        summary = rehearsal_data.get("summary", {})
        if summary.get("failed") != 0 or summary.get("passed") != summary.get("total"):
            raise ReleaseGateError("identity rehearsal verdict is not PASS")
    return {
        "full_tests": {"verdict": PASS, "passed": int(passed_match.group(1)),
                       "returncode": tests.returncode,
                       "summary_tail": combined.splitlines()[-8:]},
        "identity_rehearsal": {
            "verdict": PASS,
            "passed_steps": summary.get("passed"),
            "total_steps": summary.get("total"),
        },
    }


def audit_milestone(
    repo: str | Path,
    expect_path: str | Path,
    output_path: str | Path,
    *,
    from_sha: str,
    to_sha: str,
    run_validation: bool = True,
) -> dict[str, Any]:
    """Audit a fixed linear milestone range against a versioned expectation."""
    repo = Path(repo).resolve()
    state = repo_state(repo)
    from_sha = _require_sha(from_sha, "audit base")
    to_sha = _require_sha(to_sha, "audit target")
    expect = json.loads(Path(expect_path).read_text(encoding="utf-8"))
    failures: list[str] = []
    if state["head"] != to_sha:
        failures.append("audit target is not the checked-out HEAD")
    if not state["clean"]:
        failures.append("worktree is not clean")
    if state["branch"] != expect.get("source_branch"):
        failures.append("source branch does not match expectation")
    if from_sha != expect.get("base_sha"):
        failures.append("audit base does not match expectation")
    ancestor = _run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", from_sha, to_sha],
        check=False,
    )
    if ancestor.returncode:
        failures.append("base is not an ancestor of target")

    commits = _git(repo, "rev-list", "--reverse", f"{from_sha}..{to_sha}").splitlines()
    actual = []
    previous = from_sha
    for sha in commits:
        parents = _git(repo, "show", "-s", "--format=%P", sha).split()
        subject = _git(repo, "show", "-s", "--format=%s", sha)
        actual.append({"sha": sha, "parents": parents, "subject": subject})
        if parents != [previous]:
            failures.append(f"non-linear parent chain at {sha}")
        previous = sha
    expected_commits = expect.get("ordered_commits", [])
    if len(actual) != len(expected_commits):
        failures.append("commit count does not match expectation")
    for index, (got, wanted) in enumerate(zip(actual, expected_commits), start=1):
        if wanted.get("sha") and got["sha"] != wanted["sha"]:
            failures.append(f"commit {index} SHA mismatch")
        if got["subject"] != wanted.get("subject"):
            failures.append(f"commit {index} subject mismatch")

    before_routes = routes_at(repo, from_sha)
    after_routes = routes_at(repo, to_sha)
    added_routes = sorted(set(after_routes) - set(before_routes))
    if added_routes != sorted(expect.get("expected_added_routes", [])):
        failures.append("new HTTP route inventory differs from expectation")

    schema = _probe_schema()
    if schema["versions"] != expect.get("migration_versions"):
        failures.append("migration version inventory differs from expectation")
    missing_tables = sorted(set(expect.get("required_tables", [])) - set(schema["tables"]))
    missing_triggers = sorted(
        set(expect.get("required_triggers", [])) - set(schema["triggers"])
    )
    if missing_tables:
        failures.append(f"required tables missing: {missing_tables}")
    if missing_triggers:
        failures.append(f"required triggers missing: {missing_triggers}")

    validation = _run_validation(repo) if run_validation and not failures else None
    evidence = {
        "schema": "echo-pact-milestone-audit-v1", "generated_at": _now(),
        "verdict": PASS if not failures else "FAIL", "failures": failures,
        "repo_state": state, "range": {"from": from_sha, "to": to_sha},
        "commits": actual,
        "routes": {"before": before_routes, "after": after_routes,
                   "added": added_routes},
        "database": schema,
        "validation": validation,
        "expect_sha256": sha256_file(expect_path),
    }
    _write_json_new(output_path, evidence)
    if failures:
        raise ReleaseGateError("milestone audit failed: " + "; ".join(failures))
    return evidence


def pre_ff_acceptance(
    repo: str | Path,
    expect_path: str | Path,
    audit_path: str | Path,
    baseline_package: str | Path,
    target_package: str | Path,
    output_path: str | Path,
    *,
    remote: str,
    protected_ref: str,
    target_sha: str,
) -> dict[str, Any]:
    """Prove readiness for one future FF update without writing any remote."""
    repo = Path(repo).resolve()
    target_sha = _require_sha(target_sha, "gate target")
    expect = json.loads(Path(expect_path).read_text(encoding="utf-8"))
    audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    baseline_verify = verify_recovery_package(baseline_package)
    target_verify = verify_recovery_package(target_package)
    baseline_manifest = json.loads(
        (Path(baseline_package) / "manifest.json").read_text(encoding="utf-8")
    )
    target_manifest = json.loads(
        (Path(target_package) / "manifest.json").read_text(encoding="utf-8")
    )
    state = repo_state(repo)
    actual_heads = remote_heads(repo, remote)
    actual_remote_url = _git(repo, "remote", "get-url", remote)
    failures: list[str] = []
    gates: dict[str, Any] = {}

    def gate(name: str, passed: bool, detail: Any) -> None:
        gates[name] = {"verdict": PASS if passed else "FAIL", "detail": detail}
        if not passed:
            failures.append(name)

    gate("G1-source-state", state["head"] == target_sha and state["clean"]
         and state["upstream"] is None
         and state["branch"] == expect.get("source_branch"), state)
    gate("G2-audit", audit.get("verdict") == PASS
         and audit.get("range", {}).get("to") == target_sha
         and audit.get("expect_sha256") == sha256_file(expect_path)
         and audit.get("repo_state", {}).get("head") == target_sha
         and audit.get("repo_state", {}).get("clean") is True
         and audit.get("repo_state", {}).get("branch") == expect.get("source_branch")
         and audit.get("validation", {}).get("full_tests", {}).get("verdict") == PASS
         and audit.get("validation", {}).get("identity_rehearsal", {}).get("verdict") == PASS,
         {"audit": str(audit_path), "validation": audit.get("validation")})
    gate("G3-baseline-recovery", baseline_verify["verdict"] == PASS,
         baseline_verify)
    gate("G4-target-recovery", target_verify["verdict"] == PASS
         and target_manifest.get("head") == target_sha
         and target_manifest.get("remote_heads") == baseline_manifest.get("remote_heads")
         and target_manifest.get("source_remote_url")
         == baseline_manifest.get("source_remote_url"), target_verify)
    baseline_head = baseline_manifest.get("head")
    gate("G5-remote-snapshot",
         actual_heads == baseline_manifest.get("remote_heads")
         and actual_heads.get(protected_ref) == baseline_head
         and actual_remote_url == baseline_manifest.get("source_remote_url"),
         {"protected_ref": protected_ref, "expected": baseline_head,
          "actual": actual_heads.get(protected_ref), "remote_url": actual_remote_url,
          "all_heads": actual_heads})
    ff = _run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
               str(baseline_head), target_sha], check=False).returncode == 0
    gate("G6-fast-forward", ff,
         {"from": baseline_head, "to": target_sha, "force_required": False})
    exact_ref = protected_ref == expect.get("protected_ref") \
        and protected_ref not in {"refs/heads/main", "refs/heads/master"}
    gate("G7-single-ref-boundary", exact_ref,
         {"future_refspec": f"{target_sha}:{protected_ref}",
          "remote_write_performed": False,
          "requires_separate_authorization": True})

    evidence = {
        "schema": "echo-pact-pre-ff-acceptance-v1", "generated_at": _now(),
        "verdict": PASS if not failures else "FAIL", "failures": failures,
        "gates": gates, "remote": remote, "remote_heads": actual_heads,
        "target": target_sha, "protected_ref": protected_ref,
        "remote_write_performed": False,
        "ready_for_separate_authorization": not failures,
    }
    _write_json_new(output_path, evidence)
    if failures:
        raise ReleaseGateError("pre-FF acceptance failed: " + ", ".join(failures))
    return evidence
