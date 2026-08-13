import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from backend.release_gate import (
    ReleaseGateError,
    audit_milestone,
    create_recovery_package,
    pre_ff_acceptance,
    verify_recovery_package,
)


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8",
        errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def synthetic_release_repo(tmp_path):
    repo = tmp_path / "source"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    (repo / "state.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")

    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "origin", f"{baseline}:refs/heads/main")
    _git(repo, "push", "origin", f"{baseline}:refs/heads/v1-core-2026-08-08")

    _git(repo, "switch", "-c", "m45-hardening-2026-08-11")
    (repo / "state.txt").write_text("target\n", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-m", "target")
    target = _git(repo, "rev-parse", "HEAD")
    return repo, remote, baseline, target


def test_recovery_package_is_complete_and_independently_restorable(
    tmp_path, synthetic_release_repo
):
    repo, _remote, baseline, _target = synthetic_release_repo
    package = tmp_path / "baseline-package"
    created = create_recovery_package(
        repo, package, head_sha=baseline, snapshot_name="baseline", remote="origin"
    )
    verified = verify_recovery_package(package)

    assert created["verdict"] == "PASS"
    assert verified["head"] == baseline
    assert verified["independent_restore"] == "PASS"
    assert verified["git_fsck"] == "PASS"
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["remote_heads"]["refs/heads/v1-core-2026-08-08"] == baseline
    assert (package / "repository.bundle").stat().st_size > 0

    with pytest.raises(ReleaseGateError, match="overwrite"):
        create_recovery_package(
            repo, package, head_sha=baseline, snapshot_name="again", remote="origin"
        )


def test_recovery_package_detects_tampering(tmp_path, synthetic_release_repo):
    repo, _remote, baseline, _target = synthetic_release_repo
    package = tmp_path / "tamper-package"
    create_recovery_package(
        repo, package, head_sha=baseline, snapshot_name="tamper", remote="origin"
    )
    with (package / "repository.bundle").open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ReleaseGateError, match="SHA-256"):
        verify_recovery_package(package)


def test_pre_ff_gate_passes_then_refuses_remote_drift(
    tmp_path, synthetic_release_repo
):
    repo, remote, baseline, target = synthetic_release_repo
    baseline_package = tmp_path / "rollback"
    target_package = tmp_path / "target"
    create_recovery_package(
        repo, baseline_package, head_sha=baseline,
        snapshot_name="rollback", remote="origin",
    )
    create_recovery_package(
        repo, target_package, head_sha=target,
        snapshot_name="target", remote="origin",
    )
    expect = tmp_path / "expect.json"
    expect.write_text(json.dumps({
        "source_branch": "m45-hardening-2026-08-11",
        "protected_ref": "refs/heads/v1-core-2026-08-08",
    }), encoding="utf-8")
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({
        "verdict": "PASS", "range": {"to": target},
        "expect_sha256": hashlib.sha256(expect.read_bytes()).hexdigest(),
        "repo_state": {
            "head": target, "clean": True,
            "branch": "m45-hardening-2026-08-11",
        },
        "validation": {
            "full_tests": {"verdict": "PASS"},
            "identity_rehearsal": {"verdict": "PASS"},
        },
    }), encoding="utf-8")

    passed = pre_ff_acceptance(
        repo, expect, audit, baseline_package, target_package,
        tmp_path / "pass.json", remote="origin",
        protected_ref="refs/heads/v1-core-2026-08-08", target_sha=target,
    )
    assert passed["verdict"] == "PASS"
    assert all(gate["verdict"] == "PASS" for gate in passed["gates"].values())
    assert passed["remote_write_performed"] is False

    # Simulate another operator advancing the protected branch after the
    # rollback snapshot was created. This is an ordinary local-bare push; the
    # test never connects to a network.
    _git(repo, "push", "origin", f"{target}:refs/heads/v1-core-2026-08-08")
    with pytest.raises(ReleaseGateError, match="G5-remote-snapshot"):
        pre_ff_acceptance(
            repo, expect, audit, baseline_package, target_package,
            tmp_path / "drift.json", remote="origin",
            protected_ref="refs/heads/v1-core-2026-08-08", target_sha=target,
        )
    drift = json.loads((tmp_path / "drift.json").read_text(encoding="utf-8"))
    assert drift["verdict"] == "FAIL"
    assert drift["gates"]["G5-remote-snapshot"]["verdict"] == "FAIL"


def test_milestone_audit_checks_linear_ledger_routes_and_v6(tmp_path):
    repo = tmp_path / "audit-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "work")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    routes = repo / "backend" / "trigger" / "routes.py"
    routes.parent.mkdir(parents=True)
    routes.write_text(
        'router = APIRouter()\n@router.post("/recall")\ndef recall(): pass\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    routes.write_text(
        routes.read_text(encoding="utf-8")
        + '@router.post("/v1/recall/projected")\ndef projected(): pass\n',
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add projected recall")
    target = _git(repo, "rev-parse", "HEAD")
    expect = tmp_path / "audit-expect.json"
    expect.write_text(json.dumps({
        "source_branch": "work",
        "base_sha": base,
        "ordered_commits": [{"sha": target, "subject": "add projected recall"}],
        "expected_added_routes": ["/api/v1/recall/projected"],
        "migration_versions": [1, 2, 3, 4, 5, 6],
        "required_tables": ["projection_claims", "records_v1"],
        "required_triggers": [
            "projection_claims_guard_update", "records_v1_immutable_update"
        ],
    }), encoding="utf-8")

    evidence = audit_milestone(
        repo, expect, tmp_path / "audit-evidence.json",
        from_sha=base, to_sha=target, run_validation=False,
    )
    assert evidence["verdict"] == "PASS"
    assert evidence["routes"]["added"] == ["/api/v1/recall/projected"]
    assert evidence["database"]["versions"] == [1, 2, 3, 4, 5, 6]


def test_validation_uses_short_non_nested_temp_root(tmp_path, monkeypatch):
    from backend import release_gate

    very_long = tmp_path.joinpath(*(["nested-directory"] * 8))
    monkeypatch.setenv("ECHO_PACT_TEMP_ROOT", str(very_long))
    seen = []

    class Result:
        returncode = 0
        stdout = "268 passed, 4 warnings"
        stderr = ""

    def fake_run(args, **kwargs):
        env = kwargs["env"]
        seen.append((args, Path(env["ECHO_PACT_TEMP_ROOT"])))
        if any(str(arg).endswith("rehearsal_identity.py") for arg in args):
            out = Path(args[args.index("--out") + 1])
            out.write_text(json.dumps({
                "summary": {"total": 10, "passed": 10, "failed": 0},
                "steps": [],
            }), encoding="utf-8")
        return Result()

    monkeypatch.setattr(release_gate, "_run", fake_run)
    result = release_gate._run_validation(tmp_path)
    assert result["full_tests"]["passed"] == 268
    assert result["identity_rehearsal"]["passed_steps"] == 10
    assert seen and all(very_long not in root.parents and root != very_long
                        for _args, root in seen)
