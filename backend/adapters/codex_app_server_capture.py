"""Bounded local capture client for Codex app-server thread/read.

This collector never enumerates all threads.  Callers must provide exact thread
ids, and one capture is capped so a typo cannot silently turn into an
unreviewed bulk archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from backend.adapters.codex_rollout_v1 import (
    CODEX_ROLLOUT_FORMAT,
    CODEX_ROLLOUT_FORMAT_VERSION,
    SOURCE_METHOD,
    codex_rollout_checksum,
)


MAX_THREADS_PER_CAPTURE = 50


class CodexCaptureError(RuntimeError):
    """A bounded local Codex capture could not be completed safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validated_thread_ids(thread_ids: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in thread_ids:
        if not isinstance(value, str) or not value.strip():
            raise CodexCaptureError("thread ids must be non-empty strings")
        item = value.strip()
        if item in seen:
            raise CodexCaptureError("thread ids must not be repeated")
        seen.add(item)
        result.append(item)
    if not result:
        raise CodexCaptureError("at least one exact thread id is required")
    if len(result) > MAX_THREADS_PER_CAPTURE:
        raise CodexCaptureError(
            f"one capture is limited to {MAX_THREADS_PER_CAPTURE} reviewed threads"
        )
    return result


def build_codex_rollout_snapshot(
    threads: Iterable[Mapping[str, Any]],
    *,
    codex_cli_version: str,
    captured_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Wrap complete thread/read results in the adapter's checksummed envelope."""

    if not isinstance(codex_cli_version, str) or not codex_cli_version.strip():
        raise CodexCaptureError("Codex CLI version is required")
    thread_list = [dict(thread) for thread in threads]
    snapshot = {
        "format": CODEX_ROLLOUT_FORMAT,
        "formatVersion": CODEX_ROLLOUT_FORMAT_VERSION,
        "capturedAt": captured_at or _utc_now(),
        "codexCliVersion": codex_cli_version.strip(),
        "sourceMethod": SOURCE_METHOD,
        "checksumAlgorithm": "SHA-256",
        "checksum": codex_rollout_checksum(thread_list),
        "threads": thread_list,
    }
    return snapshot


def serialize_codex_rollout_snapshot(snapshot: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(snapshot, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode("utf-8")


def write_codex_rollout_snapshot(
    snapshot: Mapping[str, Any],
    output_path: str,
) -> Dict[str, Any]:
    """Atomically create a private snapshot without overwriting an older copy."""

    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(f"capture output already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(
            f"capture output parent does not exist: {destination.parent}"
        )
    output_bytes = serialize_codex_rollout_snapshot(snapshot)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary.write(output_bytes)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "output_path": str(destination),
        "threads": len(snapshot.get("threads", [])),
        "output_bytes": len(output_bytes),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "message_text_reported": False,
    }


class AppServerClient:
    """Small newline-delimited JSON-RPC client for local read-only requests."""

    def __init__(self, codex_executable: str = "codex", *, timeout_seconds: int = 30):
        if timeout_seconds < 1 or timeout_seconds > 300:
            raise CodexCaptureError("timeout_seconds must be between 1 and 300")
        self.timeout_seconds = timeout_seconds
        try:
            self.process = subprocess.Popen(
                [codex_executable, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexCaptureError("Codex app-server could not be started") from exc
        if self.process.stdin is None or self.process.stdout is None:
            self.close()
            raise CodexCaptureError("Codex app-server stdio is unavailable")
        self._messages: queue.Queue[Any] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._next_id = 1

    def _read_loop(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                try:
                    self._messages.put(json.loads(line))
                except json.JSONDecodeError:
                    self._messages.put(CodexCaptureError("app-server emitted invalid JSON"))
                    return
        except (OSError, UnicodeError) as exc:
            self._messages.put(CodexCaptureError("app-server output became unreadable"))
        finally:
            self._messages.put(None)

    def _send(self, message: Mapping[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise CodexCaptureError("Codex app-server exited unexpectedly")
        try:
            self.process.stdin.write(
                json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n"
            )
            self.process.stdin.flush()
        except (OSError, UnicodeError) as exc:
            raise CodexCaptureError("Codex app-server request could not be sent") from exc

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._send({"method": method, "params": dict(params)})

    def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": dict(params)})
        while True:
            try:
                message = self._messages.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise CodexCaptureError(
                    f"Codex app-server timed out during {method}"
                ) from exc
            if isinstance(message, Exception):
                raise message
            if message is None:
                raise CodexCaptureError("Codex app-server closed before replying")
            if not isinstance(message, dict) or message.get("id") != request_id:
                # Notifications and unrelated server requests are irrelevant to
                # this read-only, single-request-at-a-time client.
                continue
            if "error" in message:
                error = message.get("error")
                detail = error.get("message") if isinstance(error, dict) else "unknown error"
                raise CodexCaptureError(f"Codex app-server rejected {method}: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexCaptureError(f"Codex app-server returned no result for {method}")
            return result

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "echo_pact_local_capture",
                    "title": "Echo Pact Local Capture",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": [
                        "thread/started",
                        "item/agentMessage/delta",
                    ]
                },
            },
        )
        self.notify("initialized", {})

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def __enter__(self) -> "AppServerClient":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def _read_codex_version(codex_executable: str, timeout_seconds: int) -> str:
    try:
        result = subprocess.run(
            [codex_executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise CodexCaptureError("Codex CLI version could not be read") from exc
    version = (result.stdout or result.stderr).strip()
    if result.returncode != 0 or not version:
        raise CodexCaptureError("Codex CLI version could not be read")
    return version


def capture_codex_threads(
    thread_ids: Iterable[str],
    *,
    codex_executable: str = "codex",
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Read exact stored threads without resuming them and build a snapshot."""

    reviewed_ids = _validated_thread_ids(thread_ids)
    if timeout_seconds < 1 or timeout_seconds > 300:
        raise CodexCaptureError("timeout_seconds must be between 1 and 300")
    version = _read_codex_version(codex_executable, timeout_seconds)
    threads: List[Mapping[str, Any]] = []
    with AppServerClient(
        codex_executable,
        timeout_seconds=timeout_seconds,
    ) as client:
        client.initialize()
        for thread_id in reviewed_ids:
            result = client.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
            )
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise CodexCaptureError("thread/read returned no thread object")
            if thread.get("id") != thread_id:
                raise CodexCaptureError("thread/read returned a different thread id")
            threads.append(thread)
    return build_codex_rollout_snapshot(
        threads,
        codex_cli_version=version,
    )
