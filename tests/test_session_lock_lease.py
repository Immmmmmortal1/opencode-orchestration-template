from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from orchagent import session as session_module
from orchagent.session import (
    acquire_lease,
    acquire_lock,
    create_session,
    finalize_session,
    lease_path,
    load_session,
    lock_file,
    new_session_id,
    recover_expired_lease,
    release_lock,
    renew_lease,
    release_lease,
    save_session,
    session_dir,
    session_path,
)

from tests.helpers import IsolatedEnv, PROJECT_ROOT


class SessionLockLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()

    def tearDown(self) -> None:
        self.env.tearDown()

    def create(self, *, now_ns: int = 1_000_000_000) -> dict:
        result = create_session(
            self.env.home,
            summary="Phase 3 session tests",
            task_type="feature",
            now_ns=now_ns,
        )
        self.assertEqual("ok", result["status"])
        return result

    def acquire(self, session_id: str, *, now_ns: int = 2_000_000_000, ttl_ms: int = 1_000) -> dict:
        result = acquire_lease(
            self.env.home,
            session_id,
            now_ns=now_ns,
            ttl_ms=ttl_ms,
        )
        self.assertEqual("ok", result["status"])
        return result

    def succeed(self, home: Path, acquired: dict) -> dict:
        current = acquired["session"]
        for status_value in ("active", "completing", "succeeded"):
            saved = save_session(
                home,
                dict(current, status=status_value),
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                now_ns=2_500_000_000,
            )
            self.assertEqual("ok", saved["status"])
            current = saved["session"]
        return current

    def start_lock_process(self, resource: str, mode: str) -> subprocess.Popen[str]:
        script = self.env.root / "hold_lock.py"
        script.write_text(
            """from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from orchagent.session import acquire_lock

result = acquire_lock(Path(sys.argv[1]), sys.argv[2])
if result.get("status") != "ok":
    print(result, flush=True)
    raise SystemExit(2)
print("LOCKED", flush=True)
if sys.argv[3] == "crash":
    os._exit(9)
time.sleep(60)
""",
            encoding="utf-8",
        )
        return subprocess.Popen(
            [sys.executable, str(script), str(self.env.home), resource, mode],
            cwd=PROJECT_ROOT,
            env=self.env.subprocess_env({"PYTHONPATH": str(PROJECT_ROOT)}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def wait_for_locked(self, process: subprocess.Popen[str]) -> None:
        self.assertIsNotNone(process.stdout)
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            events = selector.select(timeout=10)
            self.assertTrue(events, "子进程等待锁就绪超时")
            self.assertEqual("LOCKED", process.stdout.readline().strip())
        finally:
            selector.close()

    def test_create_session_writes_complete_private_state(self) -> None:
        now = 1_234_567_890
        result = self.create(now_ns=now)
        session = result["session"]
        session_id = session["id"]
        directory = session_dir(self.env.home, session_id)
        path = session_path(self.env.home, session_id)

        self.assertTrue(directory.is_dir())
        self.assertTrue(path.is_file())
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(
            {
                "schemaVersion",
                "id",
                "status",
                "version",
                "leaseEpoch",
                "leaseToken",
                "createdAtNs",
                "updatedAtNs",
                "createdBy",
                "task",
                "currentStep",
                "result",
                "error",
            },
            set(session),
        )
        self.assertEqual("created", session["status"])
        self.assertEqual(1, session["version"])
        self.assertEqual(now, session["createdAtNs"])
        self.assertEqual(now, session["updatedAtNs"])
        self.assertEqual({"summary": "Phase 3 session tests", "type": "feature"}, session["task"])
        self.assertEqual({"status": "pending", "refs": []}, session["result"])
        self.assertEqual(os.getpid(), session["createdBy"]["pid"])
        self.assertTrue(session["createdBy"]["hostname"])

    def test_legal_status_transition_chain_is_persisted(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        current = acquired["session"]
        token = acquired["lease"]["token"]
        epoch = acquired["lease"]["epoch"]

        for index, target in enumerate(
            ("active", "waiting", "active", "completing", "succeeded"),
            start=1,
        ):
            candidate = dict(current)
            candidate["status"] = target
            saved = save_session(
                self.env.home,
                candidate,
                lease_token=token,
                lease_epoch=epoch,
                now_ns=3_000_000_000 + index,
            )
            self.assertEqual("ok", saved["status"])
            self.assertEqual(target, saved["session"]["status"])
            current = saved["session"]

        persisted = load_session(self.env.home, current["id"])
        self.assertEqual("succeeded", persisted["session"]["status"])

    def test_illegal_status_transitions_are_rejected(self) -> None:
        for source, target in (("created", "succeeded"), ("succeeded", "active")):
            with self.subTest(source=source, target=target), IsolatedEnv() as env:
                created = create_session(
                    env.home,
                    summary=f"invalid transition {source}",
                    task_type="feature",
                    now_ns=1_000_000_000,
                )
                acquired = acquire_lease(
                    env.home,
                    created["session"]["id"],
                    now_ns=2_000_000_000,
                    ttl_ms=10_000,
                )
                current = acquired["session"]
                if source == "succeeded":
                    for status_value in ("active", "completing", "succeeded"):
                        candidate = dict(current)
                        candidate["status"] = status_value
                        saved = save_session(
                            env.home,
                            candidate,
                            lease_token=acquired["lease"]["token"],
                            lease_epoch=acquired["lease"]["epoch"],
                            now_ns=2_000_000_001,
                        )
                        self.assertEqual("ok", saved["status"])
                        current = saved["session"]

                candidate = dict(current)
                candidate["status"] = target
                rejected = save_session(
                    env.home,
                    candidate,
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )
                self.assertEqual("error", rejected["status"])
                expected_error = "session_terminal" if source == "succeeded" else "invalid_status_transition"
                self.assertEqual(expected_error, rejected["error"])

    def test_temporary_file_is_ignored_when_loading_session(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        temporary = session_path(self.env.home, session_id).with_name("session.json.tmp.crashed")
        temporary.write_text("not json\n", encoding="utf-8")

        loaded = load_session(self.env.home, session_id)

        self.assertEqual("ok", loaded["status"])
        self.assertEqual(created["session"], loaded["session"])
        self.assertTrue(temporary.exists())

    def test_corrupt_session_returns_error_without_repair(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        path = session_path(self.env.home, session_id)

        for raw in ("not json\n", "[]\n"):
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                loaded = load_session(self.env.home, session_id)
                self.assertEqual("error", loaded["status"])
                self.assertIn(loaded["error"], {"session_invalid"})
                self.assertEqual(raw, path.read_text(encoding="utf-8"))

    def test_lock_is_exclusive_within_process_and_reacquires_after_release(self) -> None:
        resource = "session.test-lock"
        first = acquire_lock(self.env.home, resource)
        second = acquire_lock(self.env.home, resource)

        self.assertEqual("ok", first["status"])
        self.assertEqual("lock_busy", second["error"])
        self.assertTrue(lock_file(self.env.home, resource).is_file())

        released = release_lock(self.env.home, resource, first["token"])
        reacquired = acquire_lock(self.env.home, resource)
        self.assertEqual("ok", released["status"])
        self.assertEqual("ok", reacquired["status"])
        self.assertEqual("ok", release_lock(self.env.home, resource, reacquired["token"])["status"])

    def test_lock_token_generation_failure_does_not_acquire_lock(self) -> None:
        resource = "session.token-generation-failure"
        with patch("orchagent.session.uuid.uuid4", side_effect=RuntimeError("random source failure")):
            with self.assertRaisesRegex(RuntimeError, "random source failure"):
                acquire_lock(self.env.home, resource)

        self.assertEqual({}, session_module._LOCK_FDS)
        self.assertEqual({}, session_module._LOCK_OWNERS)
        acquired = acquire_lock(self.env.home, resource)
        self.assertEqual("ok", acquired["status"])
        self.assertEqual("ok", release_lock(self.env.home, resource, acquired["token"])["status"])

    def test_lock_is_exclusive_across_processes_and_released_after_termination(self) -> None:
        resource = "session.cross-process-termination"
        process = self.start_lock_process(resource, "hold")
        try:
            self.wait_for_locked(process)
            busy = acquire_lock(self.env.home, resource)
            self.assertEqual("lock_busy", busy["error"])

            process.terminate()
            process.wait(timeout=10)
            reacquired = acquire_lock(self.env.home, resource)
            self.assertEqual("ok", reacquired["status"])
            self.assertEqual("ok", release_lock(self.env.home, resource, reacquired["token"])["status"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_lock_is_released_by_kernel_after_process_crash(self) -> None:
        resource = "session.cross-process-crash"
        process = self.start_lock_process(resource, "crash")
        try:
            self.wait_for_locked(process)
            self.assertEqual(9, process.wait(timeout=10))

            reacquired = acquire_lock(self.env.home, resource)
            self.assertEqual("ok", reacquired["status"])
            self.assertEqual("ok", release_lock(self.env.home, resource, reacquired["token"])["status"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def test_wrong_token_release_keeps_lock_held(self) -> None:
        resource = "session.token-conflict"
        first = acquire_lock(self.env.home, resource)
        conflict = release_lock(self.env.home, resource, "wrong-token")
        still_busy = acquire_lock(self.env.home, resource)

        self.assertEqual("ok", first["status"])
        self.assertEqual("conflict", conflict["status"])
        self.assertEqual("lock_conflict", conflict["error"])
        self.assertEqual("lock_busy", still_busy["error"])
        self.assertEqual("ok", release_lock(self.env.home, resource, first["token"])["status"])

    def test_release_lock_cleans_registration_when_unlock_fails(self) -> None:
        resource = "session.unlock-failure"
        acquired = acquire_lock(self.env.home, resource)
        token = acquired["token"]
        descriptor = session_module._LOCK_FDS[token]

        with patch("orchagent.session.fcntl.flock", side_effect=OSError("injected unlock failure")):
            released = release_lock(self.env.home, resource, token)

        self.assertEqual("error", released["status"])
        self.assertEqual("lock_release_failed", released["error"])
        self.assertNotIn(token, session_module._LOCK_FDS)
        self.assertNotIn(token, session_module._LOCK_OWNERS)
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        reacquired = acquire_lock(self.env.home, resource)
        self.assertEqual("ok", reacquired["status"])
        self.assertEqual("ok", release_lock(self.env.home, resource, reacquired["token"])["status"])

    def test_acquire_lease_updates_session_and_writes_lease_file(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        acquired = self.acquire(session_id)

        self.assertEqual("created", acquired["session"]["status"])
        self.assertEqual(acquired["lease"]["token"], acquired["session"]["leaseToken"])
        self.assertEqual(acquired["lease"]["epoch"], acquired["session"]["leaseEpoch"])
        self.assertEqual(2, acquired["session"]["version"])
        self.assertTrue(lease_path(self.env.home, session_id).is_file())
        persisted = load_session(self.env.home, session_id)
        self.assertEqual(acquired["session"], persisted["session"])

    def test_renew_lease_updates_expiry_and_session_version(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], now_ns=2_000_000_000, ttl_ms=1_000)
        renewed = renew_lease(
            self.env.home,
            created["session"]["id"],
            acquired["lease"]["token"],
            now_ns=2_500_000_000,
            ttl_ms=2_000,
        )

        self.assertEqual("ok", renewed["status"])
        self.assertEqual(4_500_000_000, renewed["lease"]["expiresAtNs"])
        self.assertEqual(acquired["session"]["version"] + 1, renewed["session"]["version"])

    def test_terminal_session_rejects_all_lease_mutations_without_changing_files(self) -> None:
        operations = (
            (
                "acquire",
                lambda env, session_id, token: acquire_lease(
                    env.home, session_id, now_ns=4_000_000_001, ttl_ms=1_000
                ),
            ),
            (
                "recover",
                lambda env, session_id, token: recover_expired_lease(
                    env.home, session_id, now_ns=4_000_000_001, ttl_ms=1_000
                ),
            ),
            (
                "renew",
                lambda env, session_id, token: renew_lease(
                    env.home, session_id, token, now_ns=3_000_000_000, ttl_ms=1_000
                ),
            ),
        )

        for name, operation in operations:
            with self.subTest(operation=name), IsolatedEnv() as env:
                created = create_session(
                    env.home,
                    summary=f"terminal lease {name}",
                    task_type="feature",
                    now_ns=1_000_000_000,
                )
                acquired = acquire_lease(
                    env.home,
                    created["session"]["id"],
                    now_ns=2_000_000_000,
                    ttl_ms=1_000,
                )
                terminal = self.succeed(env.home, acquired)
                session_id = terminal["id"]
                session_before = session_path(env.home, session_id).read_bytes()
                lease_before = lease_path(env.home, session_id).read_bytes()

                rejected = operation(env, session_id, acquired["lease"]["token"])

                self.assertEqual("error", rejected["status"])
                self.assertEqual("session_terminal", rejected["error"])
                persisted = load_session(env.home, session_id)["session"]
                self.assertEqual(terminal["version"], persisted["version"])
                self.assertEqual(terminal["leaseToken"], persisted["leaseToken"])
                self.assertEqual(terminal["leaseEpoch"], persisted["leaseEpoch"])
                self.assertEqual(session_before, session_path(env.home, session_id).read_bytes())
                self.assertEqual(lease_before, lease_path(env.home, session_id).read_bytes())

    def test_invalid_lease_structures_fail_closed(self) -> None:
        missing = object()
        corruptions = (
            ("hostname missing", "hostname", missing),
            ("pid missing", "pid", missing),
            ("pid zero", "pid", 0),
            ("pid string", "pid", "123"),
            ("epoch zero", "epoch", 0),
            ("epoch negative", "epoch", -1),
            ("ttl zero", "ttlMs", 0),
            ("ttl negative", "ttlMs", -5),
            ("acquired negative", "acquiredAtNs", -1),
            ("expires negative", "expiresAtNs", -1),
            ("expires before renewed", "expiresAtNs", 1_999_999_999),
            ("renewed before acquired", "renewedAtNs", 1_999_999_999),
        )

        for name, field, value in corruptions:
            with self.subTest(case=name), IsolatedEnv() as env:
                created = create_session(
                    env.home,
                    summary=f"invalid lease {name}",
                    task_type="feature",
                    now_ns=1_000_000_000,
                )
                session_id = created["session"]["id"]
                acquired = acquire_lease(env.home, session_id, now_ns=2_000_000_000, ttl_ms=1_000)
                path = lease_path(env.home, session_id)
                damaged = dict(acquired["lease"])
                if value is missing:
                    damaged.pop(field)
                else:
                    damaged[field] = value
                path.write_text(json.dumps(damaged), encoding="utf-8")
                lease_before = path.read_bytes()

                rejected = acquire_lease(env.home, session_id, now_ns=4_000_000_001, ttl_ms=1_000)

                self.assertEqual("error", rejected["status"])
                self.assertEqual("lease_invalid", rejected["error"])
                self.assertEqual(lease_before, path.read_bytes())

    def test_expired_lease_cannot_be_renewed(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], now_ns=2_000_000_000, ttl_ms=1)
        renewed = renew_lease(
            self.env.home,
            created["session"]["id"],
            acquired["lease"]["token"],
            now_ns=2_001_000_001,
            ttl_ms=1,
        )

        self.assertEqual("error", renewed["status"])
        self.assertEqual("lease_expired", renewed["error"])

    def test_expired_lease_cannot_save_without_takeover(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], now_ns=2_000_000_000, ttl_ms=1)
        candidate = dict(acquired["session"], status="active")

        accepted = save_session(
            self.env.home,
            candidate,
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=2_000_999_999,
        )
        self.assertEqual("ok", accepted["status"])

        rejected = save_session(
            self.env.home,
            dict(accepted["session"], status="waiting"),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=2_001_000_001,
        )
        self.assertEqual("error", rejected["status"])
        self.assertEqual("lease_expired", rejected["error"])

    def test_finalize_session_writes_terminal_state_and_releases_lease(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        active = save_session(
            self.env.home,
            dict(acquired["session"], status="active"),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=2_500_000_000,
        )

        finalized = finalize_session(
            self.env.home,
            active["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="failed",
            result={"status": "failed", "refs": ["artifact.json"]},
            error={"code": "gate_rejected"},
            now_ns=3_000_000_000,
        )

        self.assertEqual("ok", finalized["status"])
        self.assertEqual("failed", finalized["terminalStatus"])
        self.assertEqual(active["session"]["version"] + 1, finalized["session"]["version"])
        self.assertEqual(3_000_000_000, finalized["session"]["updatedAtNs"])
        self.assertIsNone(finalized["session"]["leaseToken"])
        self.assertFalse(lease_path(self.env.home, active["session"]["id"]).exists())
        self.assertEqual(finalized["session"], load_session(self.env.home, active["session"]["id"])["session"])

    def test_release_lease_is_idempotent_and_partial_state_is_repaired(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        acquired = self.acquire(session_id, ttl_ms=10_000)
        session_file = session_path(self.env.home, session_id)
        partial = json.loads(session_file.read_text(encoding="utf-8"))
        partial["leaseToken"] = None
        session_file.write_text(json.dumps(partial), encoding="utf-8")

        released = release_lease(
            self.env.home,
            session_id,
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
        )

        self.assertEqual("ok", released["status"])
        self.assertFalse(lease_path(self.env.home, session_id).exists())
        reacquired = acquire_lease(self.env.home, session_id, now_ns=3_000_000_000, ttl_ms=10_000)
        self.assertEqual("ok", reacquired["status"])
        self.assertEqual("ok", release_lease(
            self.env.home,
            session_id,
            lease_token=reacquired["lease"]["token"],
            lease_epoch=reacquired["lease"]["epoch"],
        )["status"])

    def test_finalize_session_recovers_residual_lease_after_unlink_failure(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        path = lease_path(self.env.home, created["session"]["id"])
        original_unlink = Path.unlink
        calls = 0

        def fail_once(target: Path, *args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected unlink failure")
            original_unlink(target, *args, **kwargs)

        with patch("pathlib.Path.unlink", autospec=True, side_effect=fail_once):
            first = finalize_session(
                self.env.home,
                created["session"]["id"],
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                terminal_status="failed",
                now_ns=3_000_000_000,
            )
            recovered = finalize_session(
                self.env.home,
                created["session"]["id"],
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                terminal_status="failed",
                now_ns=3_000_000_001,
            )

        self.assertEqual("session_finalize_failed", first["error"])
        self.assertEqual("failed", load_session(self.env.home, created["session"]["id"])["session"]["status"])
        self.assertTrue(recovered["recovered"])
        self.assertEqual("ok", recovered["status"])
        self.assertFalse(path.exists())

    def test_idempotent_finalize_retains_residual_lease_when_epoch_mismatches(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        path = lease_path(self.env.home, created["session"]["id"])

        with patch("pathlib.Path.unlink", autospec=True, side_effect=OSError("injected unlink failure")):
            first = finalize_session(
                self.env.home,
                created["session"]["id"],
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                terminal_status="failed",
                now_ns=3_000_000_000,
            )

        repeated = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"] + 1,
            terminal_status="failed",
            now_ns=3_000_000_001,
        )

        self.assertEqual("session_finalize_failed", first["error"])
        self.assertEqual("ok", repeated["status"])
        self.assertFalse(repeated["recovered"])
        self.assertTrue(repeated["leaseRetained"])
        self.assertTrue(path.exists())

    def test_finalize_session_reaches_terminal_from_created_via_legal_path(self) -> None:
        """finalize 必须能走 D11 的合法路径到达终态（含 created -> succeeded）。

        不要求调用方手动先置 active/completing：中间过渡由 finalize 内部完成。
        """
        for terminal in ("succeeded", "failed", "cancelled"):
            with self.subTest(terminal=terminal):
                with IsolatedEnv() as env:
                    created = create_session(
                        env.home, summary=f"fin-{terminal}", task_type="feature", now_ns=1_000_000_000
                    )
                    session_id = created["session"]["id"]
                    acquired = acquire_lease(env.home, session_id, now_ns=2_000_000_000, ttl_ms=60_000)

                    finalized = finalize_session(
                        env.home,
                        session_id,
                        lease_token=acquired["lease"]["token"],
                        lease_epoch=acquired["lease"]["epoch"],
                        terminal_status=terminal,
                        now_ns=2_000_000_001,
                    )

                    self.assertEqual("ok", finalized["status"], finalized)
                    self.assertEqual(terminal, finalized["terminalStatus"])
                    persisted = load_session(env.home, session_id)["session"]
                    self.assertEqual(terminal, persisted["status"])
                    self.assertFalse(lease_path(env.home, session_id).exists())

    def test_terminal_status_path_is_legal_and_minimal(self) -> None:
        from orchagent.session import STATUS_TRANSITIONS, terminal_status_path

        self.assertEqual(["active", "completing", "succeeded"], terminal_status_path("created", "succeeded"))
        self.assertEqual(["completing", "succeeded"], terminal_status_path("active", "succeeded"))
        self.assertEqual(["cancelled"], terminal_status_path("created", "cancelled"))
        # 终态无法再到别的终态
        self.assertIsNone(terminal_status_path("succeeded", "failed"))
        # 路径上的每一步都必须是状态机允许的合法边
        path = terminal_status_path("created", "succeeded")
        self.assertIsNotNone(path)
        cursor = "created"
        for step in path:
            self.assertIn(step, STATUS_TRANSITIONS[cursor])
            cursor = step

    def test_finalize_session_rejects_expired_lease_without_mutation(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], now_ns=2_000_000_000, ttl_ms=1)
        session_before = session_path(self.env.home, created["session"]["id"]).read_bytes()
        lease_before = lease_path(self.env.home, created["session"]["id"]).read_bytes()

        rejected = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="failed",
            now_ns=2_001_000_001,
        )

        self.assertEqual("lease_expired", rejected["error"])
        self.assertEqual(session_before, session_path(self.env.home, created["session"]["id"]).read_bytes())
        self.assertEqual(lease_before, lease_path(self.env.home, created["session"]["id"]).read_bytes())

    def test_terminal_session_finalize_is_idempotent_only_for_same_terminal(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        first = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="cancelled",
            now_ns=3_000_000_000,
        )
        self.assertEqual("ok", first["status"])

        repeated = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="cancelled",
            now_ns=3_000_000_001,
        )
        self.assertEqual("ok", repeated["status"])
        self.assertFalse(repeated["recovered"])

        rejected = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="failed",
            now_ns=3_000_000_002,
        )
        self.assertEqual("session_terminal", rejected["error"])

    def test_finalize_session_rejects_invalid_terminal_and_payloads(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)

        invalid_status = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="expired",
            now_ns=3_000_000_000,
        )
        invalid_result = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="failed",
            result={"status": "failed", "refs": [object()]},
            now_ns=3_000_000_000,
        )
        invalid_extra = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="failed",
            extra_fields={"pipelineRuns": object()},
            now_ns=3_000_000_000,
        )

        self.assertEqual("invalid_terminal_status", invalid_status["error"])
        self.assertEqual("session_invalid", invalid_result["error"])
        self.assertEqual("session_invalid", invalid_extra["error"])
        self.assertTrue(lease_path(self.env.home, created["session"]["id"]).exists())

    def test_finalize_session_atomically_merges_extra_fields(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        run = {"run-1": {"status": "succeeded", "attempts": {"emit": 1}}}

        finalized = finalize_session(
            self.env.home,
            created["session"]["id"],
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            terminal_status="succeeded",
            result={"status": "succeeded", "refs": []},
            extra_fields={"pipelineRuns": run, "currentStep": "emit"},
            now_ns=3_000_000_000,
        )

        self.assertEqual("ok", finalized["status"], finalized)
        persisted = load_session(self.env.home, created["session"]["id"])["session"]
        self.assertEqual("succeeded", persisted["status"])
        self.assertEqual(run, persisted["pipelineRuns"])
        self.assertFalse(lease_path(self.env.home, created["session"]["id"]).exists())

    def test_finalize_session_rejects_reserved_and_unknown_extra_fields_without_mutation(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        session_file = session_path(self.env.home, created["session"]["id"])
        lease_file = lease_path(self.env.home, created["session"]["id"])
        session_before = session_file.read_bytes()
        lease_before = lease_file.read_bytes()

        for extra_fields in ({"leaseEpoch": 999}, {"unexpected": True}):
            with self.subTest(extra_fields=extra_fields):
                rejected = finalize_session(
                    self.env.home,
                    created["session"]["id"],
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    terminal_status="failed",
                    extra_fields=extra_fields,
                    now_ns=3_000_000_000,
                )

                self.assertEqual("session_invalid", rejected["error"])
                self.assertEqual(session_before, session_file.read_bytes())
                self.assertEqual(lease_before, lease_file.read_bytes())

    def test_stale_holder_cannot_write_after_lease_takeover(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        holder_a = self.acquire(session_id, now_ns=2_000_000_000, ttl_ms=1)
        holder_b = acquire_lease(
            self.env.home,
            session_id,
            now_ns=2_001_000_001,
            ttl_ms=1,
        )

        self.assertEqual("ok", holder_b["status"])
        self.assertEqual(holder_a["lease"]["epoch"] + 1, holder_b["lease"]["epoch"])
        stale_candidate = dict(holder_a["session"])
        stale_candidate["status"] = "active"
        rejected = save_session(
            self.env.home,
            stale_candidate,
            lease_token=holder_a["lease"]["token"],
            lease_epoch=holder_a["lease"]["epoch"],
            now_ns=2_001_000_002,
        )
        self.assertEqual("stale_lease_holder", rejected["error"])

    def test_stale_token_is_rejected_even_with_freshly_loaded_session(self) -> None:
        """旧持有者重新读取 session 后，仍不能凭自己的旧 token 写入。

        这条路径专门覆盖 save_session 的 fenced token 参数校验：
        session dict 取自最新状态（leaseToken 已是新持有者的），
        但 lease_token 参数仍是旧持有者的 -> 必须被拒绝。
        """
        created = self.create()
        session_id = created["session"]["id"]
        holder_a = self.acquire(session_id, now_ns=2_000_000_000, ttl_ms=1)
        holder_b = acquire_lease(self.env.home, session_id, now_ns=2_001_000_001, ttl_ms=10)
        self.assertEqual("ok", holder_b["status"])

        fresh = load_session(self.env.home, session_id)["session"]
        self.assertEqual(holder_b["lease"]["epoch"], fresh["leaseEpoch"])

        # 旧 token + 最新 session dict -> 拒绝
        rejected = save_session(
            self.env.home,
            dict(fresh, status="active"),
            lease_token=holder_a["lease"]["token"],
            lease_epoch=holder_a["lease"]["epoch"],
            now_ns=2_001_000_002,
        )
        self.assertEqual("stale_lease_holder", rejected["error"])

        # 对照：用新持有者的 token + 最新 session dict -> 成功，证明上面不是被别的检查挡下
        accepted = save_session(
            self.env.home,
            dict(fresh, status="active"),
            lease_token=holder_b["lease"]["token"],
            lease_epoch=holder_b["lease"]["epoch"],
            now_ns=2_001_000_003,
        )
        self.assertEqual("ok", accepted["status"])
        self.assertEqual("active", accepted["session"]["status"])

    def test_recover_expired_lease_fences_old_holder(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        holder_a = self.acquire(session_id, now_ns=2_000_000_000, ttl_ms=1)
        recovered = recover_expired_lease(
            self.env.home,
            session_id,
            now_ns=2_001_000_001,
            ttl_ms=10,
        )

        self.assertEqual("ok", recovered["status"])
        self.assertNotEqual(holder_a["lease"]["token"], recovered["lease"]["token"])
        stale_candidate = dict(holder_a["session"])
        stale_candidate["status"] = "active"
        rejected = save_session(
            self.env.home,
            stale_candidate,
            lease_token=holder_a["lease"]["token"],
            lease_epoch=holder_a["lease"]["epoch"],
            now_ns=2_001_000_002,
        )
        self.assertEqual("stale_lease_holder", rejected["error"])

    def test_clock_rollback_does_not_expire_active_lease(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], now_ns=5_000_000_000, ttl_ms=1_000)
        original_expiry = acquired["lease"]["expiresAtNs"]
        renewed = renew_lease(
            self.env.home,
            created["session"]["id"],
            acquired["lease"]["token"],
            now_ns=4_000_000_000,
            ttl_ms=1_000,
        )

        self.assertEqual("ok", renewed["status"])
        self.assertEqual(5_000_000_000, renewed["lease"]["renewedAtNs"])
        self.assertGreaterEqual(renewed["lease"]["expiresAtNs"], original_expiry)

        takeover = acquire_lease(
            self.env.home,
            created["session"]["id"],
            now_ns=original_expiry - 1,
            ttl_ms=1_000,
        )
        self.assertEqual("error", takeover["status"])
        self.assertEqual("active_lease", takeover["error"])

    def test_partial_lease_double_write_fails_closed(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        real_atomic_write = session_module._atomic_write_json
        lease_written = False

        def fail_session_write(path: Path, data: dict) -> None:
            nonlocal lease_written
            if path == lease_path(self.env.home, session_id):
                lease_written = True
                real_atomic_write(path, data)
                return
            if lease_written and path == session_path(self.env.home, session_id):
                raise OSError("injected session write failure")
            real_atomic_write(path, data)

        with patch("orchagent.session._atomic_write_json", side_effect=fail_session_write):
            failed = acquire_lease(
                self.env.home,
                session_id,
                now_ns=2_000_000_000,
                ttl_ms=1_000,
            )

        self.assertEqual("error", failed["status"])
        self.assertEqual("lease_acquire_failed", failed["error"])
        self.assertTrue(lease_path(self.env.home, session_id).is_file())
        persisted = load_session(self.env.home, session_id)
        self.assertIsNone(persisted["session"]["leaseToken"])

        retry = acquire_lease(
            self.env.home,
            session_id,
            now_ns=4_000_000_001,
            ttl_ms=1_000,
        )
        self.assertEqual("error", retry["status"])
        self.assertEqual("lease_state_conflict", retry["error"])

    def test_partial_takeover_fences_old_holder_during_save(self) -> None:
        created = self.create()
        session_id = created["session"]["id"]
        holder_a = self.acquire(session_id, now_ns=2_000_000_000, ttl_ms=1)
        real_atomic_write = session_module._atomic_write_json
        new_lease_written = False

        def fail_takeover_session_write(path: Path, data: dict) -> None:
            nonlocal new_lease_written
            if path == lease_path(self.env.home, session_id):
                new_lease_written = True
                real_atomic_write(path, data)
                return
            if new_lease_written and path == session_path(self.env.home, session_id):
                raise OSError("injected takeover session write failure")
            real_atomic_write(path, data)

        with patch("orchagent.session._atomic_write_json", side_effect=fail_takeover_session_write):
            failed = acquire_lease(
                self.env.home,
                session_id,
                now_ns=2_001_000_001,
                ttl_ms=1_000,
            )

        self.assertEqual("lease_acquire_failed", failed["error"])
        stale_candidate = dict(holder_a["session"], status="active")
        rejected = save_session(
            self.env.home,
            stale_candidate,
            lease_token=holder_a["lease"]["token"],
            lease_epoch=holder_a["lease"]["epoch"],
            now_ns=2_001_000_002,
        )
        self.assertEqual("error", rejected["status"])
        self.assertEqual("lease_state_conflict", rejected["error"])

    def test_new_session_id_rejects_empty_slug_and_sanitizes_normal_summary(self) -> None:
        self.assertEqual("", new_session_id(""))
        self.assertEqual("", new_session_id("///***---"))

        session_id = new_session_id("Feature / Session Lock")
        self.assertTrue(session_id.startswith("feature-session-lock-"))
        self.assertNotIn("/", session_id)
        self.assertNotIn("\\", session_id)

    def test_invalid_names_and_ttls_return_structured_errors(self) -> None:
        cases = [
            acquire_lock(self.env.home, "../escape"),
            acquire_lease(self.env.home, "../escape", now_ns=1, ttl_ms=1),
            acquire_lease(self.env.home, "missing", now_ns=1, ttl_ms=-1),
            recover_expired_lease(self.env.home, "missing", now_ns=1, ttl_ms=0),
            renew_lease(self.env.home, "../escape", "token", now_ns=1, ttl_ms=1),
            renew_lease(self.env.home, "missing", "token", now_ns=1, ttl_ms=False),
        ]
        for result in cases:
            with self.subTest(result=result):
                self.assertEqual("error", result["status"])
                self.assertIn(result["error"], {"invalid_resource", "invalid_session_id", "invalid_ttl"})

    def test_pure_dot_session_ids_return_structured_errors(self) -> None:
        for session_id in (".", "..", "..."):
            with self.subTest(session_id=session_id):
                created = create_session(
                    self.env.home,
                    summary=session_id,
                    task_type="feature",
                    now_ns=1,
                )
                loaded = load_session(self.env.home, session_id)
                saved = save_session(self.env.home, {"id": session_id}, now_ns=1)
                acquired = acquire_lease(self.env.home, session_id, now_ns=1, ttl_ms=1)

                self.assertEqual("invalid_summary", created["error"])
                self.assertEqual("invalid_session_id", loaded["error"])
                self.assertEqual("invalid_session_id", saved["error"])
                self.assertEqual("invalid_session_id", acquired["error"])

    def test_save_requires_lease(self) -> None:
        created = self.create()
        candidate = dict(created["session"])
        candidate["status"] = "active"

        rejected = save_session(self.env.home, candidate, now_ns=2_000_000_000)

        self.assertEqual("error", rejected["status"])
        self.assertEqual("lease_required", rejected["error"])

    def test_save_rejects_stale_session_version(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        stale = dict(acquired["session"])
        stale["version"] -= 1
        stale["status"] = "active"

        rejected = save_session(
            self.env.home,
            stale,
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=3_000_000_000,
        )

        self.assertEqual("conflict", rejected["status"])
        self.assertEqual("session_version_conflict", rejected["error"])

    def test_save_rejects_missing_or_invalid_required_fields(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        base = dict(acquired["session"], status="active")
        invalid_values = {
            "schemaVersion": "1",
            "id": "...",
            "status": "unknown",
            "version": True,
            "leaseEpoch": True,
            "leaseToken": "",
            "createdAtNs": "now",
            "updatedAtNs": False,
            "createdBy": {"hostname": "", "pid": "pid"},
            "task": {"summary": "", "type": "feature"},
            "currentStep": 1,
            "result": None,
            "error": "failure",
        }

        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field, kind="missing"):
                candidate = dict(base)
                candidate.pop(field)
                rejected = save_session(
                    self.env.home,
                    candidate,
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )
                # id 缺失由更早的 id 校验拦下，错误码更精确
                expected = "invalid_session_id" if field == "id" else "session_invalid"
                self.assertEqual(expected, rejected["error"])

            with self.subTest(field=field, kind="invalid"):
                candidate = dict(base)
                candidate[field] = invalid_value
                rejected = save_session(
                    self.env.home,
                    candidate,
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )
                expected = "invalid_session_id" if field == "id" else "session_invalid"
                self.assertEqual(expected, rejected["error"])

    def test_save_rejects_invalid_result_fields(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        base = dict(acquired["session"], status="active")

        for invalid_result in (
            {"status": 1, "refs": []},
            {"status": "pending", "refs": "artifact.json"},
        ):
            with self.subTest(result=invalid_result):
                rejected = save_session(
                    self.env.home,
                    dict(base, result=invalid_result),
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )
                self.assertEqual("error", rejected["status"])
                self.assertEqual("session_invalid", rejected["error"])

    def test_unserializable_result_is_rejected_without_leaking_lock(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        candidate = dict(acquired["session"], status="active", result={"status": "pending", "refs": [object()]})

        rejected = save_session(
            self.env.home,
            candidate,
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=3_000_000_000,
        )

        self.assertEqual("error", rejected["status"])
        self.assertEqual("session_invalid", rejected["error"])
        self.assertEqual({}, session_module._LOCK_FDS)
        accepted = save_session(
            self.env.home,
            dict(acquired["session"], status="active"),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=2_999_999_999,
        )
        self.assertEqual("ok", accepted["status"])

    def test_non_oserror_inside_held_lock_releases_registration_and_lock(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        session_id = created["session"]["id"]
        resource = f"session.{session_id}"

        with patch("orchagent.session._atomic_write_json", side_effect=RuntimeError("injected write failure")):
            with self.assertRaisesRegex(RuntimeError, "injected write failure"):
                save_session(
                    self.env.home,
                    dict(acquired["session"], status="active"),
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )

        self.assertEqual({}, session_module._LOCK_FDS)
        self.assertEqual({}, session_module._LOCK_OWNERS)
        reacquired = acquire_lock(self.env.home, resource)
        self.assertEqual("ok", reacquired["status"])
        self.assertEqual("ok", release_lock(self.env.home, resource, reacquired["token"])["status"])

    def test_terminal_session_cannot_be_saved_even_without_status_change(self) -> None:
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=10_000)
        current = acquired["session"]
        for status_value in ("active", "completing", "succeeded"):
            saved = save_session(
                self.env.home,
                dict(current, status=status_value),
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                now_ns=3_000_000_000,
            )
            self.assertEqual("ok", saved["status"])
            current = saved["session"]

        rejected = save_session(
            self.env.home,
            dict(current, result={"status": "changed", "refs": []}),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=4_000_000_000,
        )
        self.assertEqual("error", rejected["status"])
        self.assertEqual("session_terminal", rejected["error"])

    def test_terminal_error_takes_precedence_over_other_diagnostics(self) -> None:
        """终态是最强不变量：lease 过期或版本陈旧时，仍应统一返回 session_terminal。"""
        created = self.create()
        acquired = self.acquire(created["session"]["id"], ttl_ms=1_000)
        current = acquired["session"]
        for status_value in ("active", "completing", "succeeded"):
            saved = save_session(
                self.env.home,
                dict(current, status=status_value),
                lease_token=acquired["lease"]["token"],
                lease_epoch=acquired["lease"]["epoch"],
                now_ns=3_000_000_000,
            )
            self.assertEqual("ok", saved["status"])
            current = saved["session"]

        # a) 终态 + lease 已过期
        expired = save_session(
            self.env.home,
            dict(current),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=acquired["lease"]["expiresAtNs"] + 10_000_000_000,
        )
        self.assertEqual("session_terminal", expired["error"])

        # b) 终态 + 陈旧 version
        stale_version = save_session(
            self.env.home,
            dict(current, version=999),
            lease_token=acquired["lease"]["token"],
            lease_epoch=acquired["lease"]["epoch"],
            now_ns=4_000_000_000,
        )
        self.assertEqual("session_terminal", stale_version["error"])

    def test_lease_schema_version_type_must_be_strict_int(self) -> None:
        """true / 1.0 在 Python 中 == 1，必须被判为非法而不是通过校验。"""
        for bad_value in (True, 1.0):
            with self.subTest(value=bad_value):
                with IsolatedEnv() as env:
                    created = create_session(env.home, summary="schema", task_type="feature", now_ns=1_000_000_000)
                    session_id = created["session"]["id"]
                    acquired = acquire_lease(env.home, session_id, now_ns=2_000_000_000, ttl_ms=60_000)
                    self.assertEqual("ok", acquired["status"])

                    path = lease_path(env.home, session_id)
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["schemaVersion"] = bad_value
                    path.write_text(json.dumps(payload), encoding="utf-8")

                    again = acquire_lease(env.home, session_id, now_ns=2_000_000_001, ttl_ms=60_000)
                    self.assertEqual("lease_invalid", again["error"])

    def test_session_schema_version_type_must_be_strict_int(self) -> None:
        """session 的 schemaVersion 同样必须严格 int。"""
        created = self.create()
        acquired = self.acquire(created["session"]["id"])
        for bad_value in (True, 1.0):
            with self.subTest(value=bad_value):
                rejected = save_session(
                    self.env.home,
                    dict(acquired["session"], schemaVersion=bad_value),
                    lease_token=acquired["lease"]["token"],
                    lease_epoch=acquired["lease"]["epoch"],
                    now_ns=3_000_000_000,
                )
                self.assertEqual("session_invalid", rejected["error"])


if __name__ == "__main__":
    unittest.main()
