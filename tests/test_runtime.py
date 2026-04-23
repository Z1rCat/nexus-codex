from __future__ import annotations

import sys
import tempfile
import textwrap
import threading
import time
import unittest
import json
import zipfile
from pathlib import Path

from nexus_codex.idea_import import import_idea_bundle
from nexus_codex.runtime import Runtime
from nexus_codex.scaffold import write_rl_research_starter


def _write_config(path: Path, repo: Path, *, lease_ttl_seconds: int = 1800) -> Path:
    fake_codex = Path(__file__).with_name("fake_codex.py")
    python_executable = Path(sys.executable).as_posix()
    config = textwrap.dedent(
        f"""
        [app]
        timezone = "UTC"
        state_dir = ".nexus-state"
        lease_ttl_seconds = {lease_ttl_seconds}
        codex_command = ["{python_executable}", "{fake_codex.as_posix()}"]

        [[jobs]]
        id = "analysis-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        prompt = "Summarize this repo."

        [[jobs]]
        id = "plan-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "plan_then_execute"
        skip_git_repo_check = true
        verifier = ["python -c \\"print('ok')\\""]
        prompt = "Investigate and be ready to fix the issue."

        [[jobs]]
        id = "repair-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "execute"
        skip_git_repo_check = true
        verifier = ["python -c \\"from pathlib import Path; raise SystemExit(0 if Path('.nexus-pass').exists() else 1)\\""]
        verifier_max_attempts = 1
        prompt = "Make the verifier pass."

        [[jobs]]
        id = "persistent-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        persistent_session = true
        wake_prompt = "Wake up and continue the long-running analysis."
        prompt = "Start the long-running analysis."

        [[jobs]]
        id = "inbox-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        inbox_dir = "{(repo / 'inbox').as_posix()}"
        inbox_glob = "*.md"
        prompt = "Analyze the inbox task and summarize the right action."

        [[jobs]]
        id = "slow-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        prompt = "Simulate a long-running run. sleep=2.5"

        [[jobs]]
        id = "multi-agent-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "execute"
        skip_git_repo_check = true
        agent_roles = ["planner", "executor", "reviewer"]
        reviewer_max_rounds = 1
        prompt = "Coordinate a small repository update through the agent pipeline."

        [[jobs]]
        id = "parallel-agent-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "execute"
        skip_git_repo_check = true
        agent_roles = ["planner", "executor", "reviewer"]
        parallel_executor_count = 3
        reviewer_max_rounds = 1
        prompt = "Coordinate a repository update through parallel executor branches."

        [[jobs]]
        id = "goal-loop-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        persistent_session = true
        goal_loop = true
        prompt = "goal-mode-done-after-two Drive the long-running goal."

        [[jobs]]
        id = "goal-idle-job"
        enabled = true
        schedule = "*/5 * * * *"
        repo = "{repo.as_posix()}"
        mode = "analysis"
        skip_git_repo_check = true
        goal_loop = true
        goal_max_idle_wakes = 2
        prompt = "goal-mode-idle Monitor until useful work appears."
        """
    ).strip()
    path.write_text(config, encoding="utf-8")
    return path


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.repo = self.tmp_path / "repo"
        self.repo.mkdir()
        self.config = _write_config(self.tmp_path / "jobs.toml", self.repo)
        self.runtime = Runtime(self.config)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_run_analysis_job(self) -> None:
        run_id = self.runtime.execute_job("analysis-job")
        row = self.runtime.show_run(run_id)
        task = self.runtime.show_task(int(row["task_id"]))
        memories = self.runtime.memories(job_id="analysis-job")

        self.assertEqual(row["status"], "completed")
        self.assertIsNotNone(row["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["last_run_id"], run_id)
        self.assertGreaterEqual(len(memories), 1)
        self.assertEqual(memories[0]["kind"], "run_outcome")
        self.assertTrue(Path(str(row["final_message_path"])).exists())

    def test_follow_up_run_receives_memory_context(self) -> None:
        first_run_id = self.runtime.execute_job("analysis-job")
        self.assertEqual(self.runtime.show_run(first_run_id)["status"], "completed")

        second_run_id = self.runtime.execute_job("analysis-job", trigger="schedule")
        second_row = self.runtime.show_run(second_run_id)
        final_message = Path(str(second_row["final_message_path"])).read_text(encoding="utf-8")
        retrieved = self.runtime.search_memories("analysis-job", "summarize repo", limit=3)

        self.assertEqual(second_row["status"], "completed")
        self.assertIn("with memory", final_message)
        self.assertGreaterEqual(len(retrieved), 1)

    def test_memory_upsert_merges_duplicates_and_counts_occurrences(self) -> None:
        first_id = self.runtime.store.create_memory(
            job_id="analysis-job",
            kind="note",
            summary="Repository ownership note",
            detail="Initial short note.",
            tags=["owner"],
            cluster_key="repo-owner",
        )
        second_id = self.runtime.store.create_memory(
            job_id="analysis-job",
            kind="note",
            summary="Repository ownership note",
            detail="A longer and more useful note about repository ownership.",
            tags=["triage"],
            cluster_key="repo-owner",
        )
        memories = [
            memory
            for memory in self.runtime.memories(job_id="analysis-job")
            if memory["kind"] == "note"
        ]

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["occurrence_count"], 2)
        self.assertEqual(memories[0]["cluster_key"], "repo-owner")
        self.assertIn("longer and more useful", str(memories[0]["detail"]))
        self.assertIn("owner", str(memories[0]["tags_json"]))
        self.assertIn("triage", str(memories[0]["tags_json"]))

    def test_scheduled_dispatch_dedupes_until_task_finishes(self) -> None:
        first_task_id = self.runtime.dispatch_job("analysis-job", trigger="schedule")
        second_task_id = self.runtime.dispatch_job("analysis-job", trigger="schedule")
        first_task = self.runtime.show_task(first_task_id)

        self.assertEqual(first_task_id, second_task_id)
        self.assertEqual(first_task["status"], "ready")
        self.assertEqual(first_task["dedupe_key"], "schedule:analysis-job")

        run_id = self.runtime.run_task(first_task_id)
        self.assertEqual(self.runtime.show_run(run_id)["status"], "completed")

        third_task_id = self.runtime.dispatch_job("analysis-job", trigger="schedule")
        self.assertNotEqual(third_task_id, first_task_id)

    def test_plan_then_approve(self) -> None:
        plan_run_id = self.runtime.execute_job("plan-job")
        plan_row = self.runtime.show_run(plan_run_id)
        task = self.runtime.show_task(int(plan_row["task_id"]))
        self.assertEqual(plan_row["status"], "awaiting_approval")
        self.assertTrue(plan_row["codex_thread_id"])
        self.assertEqual(task["status"], "awaiting_approval")

        execute_run_id = self.runtime.approve_run(plan_run_id)
        execute_row = self.runtime.show_run(execute_run_id)
        updated_task = self.runtime.show_task(int(plan_row["task_id"]))
        self.assertEqual(execute_row["status"], "completed")
        self.assertEqual(execute_row["codex_thread_id"], plan_row["codex_thread_id"])
        self.assertEqual(execute_row["task_id"], plan_row["task_id"])
        self.assertEqual(updated_task["status"], "completed")

    def test_resume_run_uses_same_thread(self) -> None:
        first_run_id = self.runtime.execute_job("analysis-job")
        first_row = self.runtime.show_run(first_run_id)
        resumed_run_id = self.runtime.resume_run(first_run_id, "Continue with a follow-up task.")
        resumed_row = self.runtime.show_run(resumed_run_id)

        self.assertEqual(resumed_row["status"], "completed")
        self.assertEqual(resumed_row["codex_thread_id"], first_row["codex_thread_id"])

    def test_verifier_retry_loop_recovers_with_same_thread(self) -> None:
        run_id = self.runtime.execute_job("repair-job")
        row = self.runtime.show_run(run_id)

        self.assertEqual(row["status"], "completed")
        attempts_path = Path(str(row["artifact_dir"])) / "attempts.jsonl"
        attempts = [
            json.loads(line)
            for line in attempts_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["phase"], "verification_failed")
        self.assertEqual(attempts[1]["phase"], "verified")
        self.assertEqual(attempts[0]["session_id"], attempts[1]["session_id"])

    def test_persistent_session_job_reuses_same_thread(self) -> None:
        first_run_id = self.runtime.execute_job("persistent-job")
        first_row = self.runtime.show_run(first_run_id)
        first_state = self.runtime.job_status("persistent-job")
        second_run_id = self.runtime.execute_job("persistent-job", trigger="schedule")
        second_row = self.runtime.show_run(second_run_id)

        self.assertEqual(first_row["status"], "completed")
        self.assertEqual(second_row["status"], "completed")
        self.assertEqual(second_row["parent_run_id"], first_run_id)
        self.assertEqual(first_row["codex_thread_id"], second_row["codex_thread_id"])
        self.assertIsNotNone(first_state["session"])
        self.assertEqual(
            first_state["session"]["codex_thread_id"],
            first_row["codex_thread_id"],
        )

    def test_inbox_task_is_processed_and_archived(self) -> None:
        inbox_dir = self.repo / "inbox"
        inbox_dir.mkdir()
        task_file = inbox_dir / "task-001.md"
        task_file.write_text("Investigate the nightly failure.\n", encoding="utf-8")

        next_item = self.runtime.next_inbox_item("inbox-job")
        self.assertEqual(next_item, task_file)

        run_id = self.runtime.execute_inbox_task("inbox-job", task_file)
        row = self.runtime.show_run(run_id)
        task = self.runtime.show_task(int(row["task_id"]))

        self.assertEqual(row["status"], "completed")
        self.assertEqual(task["kind"], "inbox_item")
        self.assertEqual(task["source_ref"], str(task_file.resolve()))
        self.assertFalse(task_file.exists())
        archived = self.tmp_path / ".nexus-state" / "inbox" / "inbox-job" / "processed" / "task-001.md"
        self.assertTrue(archived.exists())

    def test_dispatch_inbox_prioritizes_urgent_tasks(self) -> None:
        inbox_dir = self.repo / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "task-001.md").write_text("Routine documentation follow-up.\n", encoding="utf-8")
        (inbox_dir / "task-002.md").write_text(
            "Critical security blocker in production.\n",
            encoding="utf-8",
        )

        task_ids = self.runtime.dispatch_inbox("inbox-job")
        queued = [self.runtime.show_task(task_id) for task_id in task_ids]
        next_task = self.runtime.next_ready_task("inbox-job")

        self.assertEqual(len(task_ids), 2)
        self.assertEqual(sorted(task["status"] for task in queued), ["ready", "ready"])
        self.assertIsNotNone(next_task)
        self.assertEqual(next_task["priority"], 5)

        run_id = self.runtime.run_next_task("inbox-job")
        run_row = self.runtime.show_run(int(run_id))
        executed_task = self.runtime.show_task(int(run_row["task_id"]))
        self.assertEqual(executed_task["priority"], 5)

    def test_daily_wake_budget_blocks_scheduling(self) -> None:
        config_text = Path(self.config).read_text(encoding="utf-8")
        config_text += textwrap.dedent(
            f"""

            [[jobs]]
            id = "budget-job"
            enabled = true
            schedule = "*/5 * * * *"
            repo = "{self.repo.as_posix()}"
            mode = "analysis"
            skip_git_repo_check = true
            persistent_session = true
            max_daily_wakes = 1
            prompt = "Stay awake."
            """
        )
        Path(self.config).write_text(config_text, encoding="utf-8")
        runtime = Runtime(self.config)

        first_run_id = runtime.execute_job("budget-job")
        first_row = runtime.show_run(first_run_id)
        self.assertEqual(first_row["status"], "completed")

        allowed, reason = runtime.can_schedule_job("budget-job")
        self.assertFalse(allowed)
        self.assertEqual(reason, "daily wake budget exhausted")

    def test_consecutive_failures_auto_pause_job(self) -> None:
        config_text = Path(self.config).read_text(encoding="utf-8")
        config_text += textwrap.dedent(
            f"""

            [[jobs]]
            id = "failing-job"
            enabled = true
            schedule = "*/5 * * * *"
            repo = "{self.repo.as_posix()}"
            mode = "execute"
            skip_git_repo_check = true
            max_consecutive_failures = 1
            verifier = ["python -c \\"raise SystemExit(1)\\""]
            prompt = "This should fail verification."
            """
        )
        Path(self.config).write_text(config_text, encoding="utf-8")
        runtime = Runtime(self.config)

        run_id = runtime.execute_job("failing-job")
        row = runtime.show_run(run_id)
        self.assertEqual(row["status"], "verification_failed")

        state = runtime.job_status("failing-job")
        self.assertTrue(state["paused"])
        self.assertIn("auto-paused", str(state["pause_reason"]))
        memories = runtime.memories(job_id="failing-job")
        self.assertTrue(any(memory["kind"] == "failure_pattern" for memory in memories))

    def test_repeated_failures_cluster_into_one_memory_pattern(self) -> None:
        config_text = Path(self.config).read_text(encoding="utf-8")
        config_text += textwrap.dedent(
            f"""

            [[jobs]]
            id = "repeat-fail-job"
            enabled = true
            schedule = "*/5 * * * *"
            repo = "{self.repo.as_posix()}"
            mode = "execute"
            skip_git_repo_check = true
            max_consecutive_failures = 0
            verifier = ["python -c \\"raise SystemExit(1)\\""]
            prompt = "Repeat the same failure for clustering."
            """
        )
        Path(self.config).write_text(config_text, encoding="utf-8")
        runtime = Runtime(self.config)

        first_run_id = runtime.execute_job("repeat-fail-job")
        second_run_id = runtime.execute_job("repeat-fail-job")
        self.assertEqual(runtime.show_run(first_run_id)["status"], "verification_failed")
        self.assertEqual(runtime.show_run(second_run_id)["status"], "verification_failed")

        failures = [
            memory
            for memory in runtime.memories(job_id="repeat-fail-job")
            if memory["kind"] == "failure_pattern"
        ]

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["occurrence_count"], 2)
        self.assertIn("python -c", str(failures[0]["cluster_key"]))

    def test_supervised_run_renews_lease_until_completion(self) -> None:
        config = _write_config(self.tmp_path / "slow-jobs.toml", self.repo, lease_ttl_seconds=1)
        runtime = Runtime(config)
        result: dict[str, int] = {}

        worker = threading.Thread(
            target=lambda: result.setdefault("run_id", runtime.execute_job("slow-job")),
            daemon=True,
        )
        worker.start()
        time.sleep(1.6)

        leased = runtime.store.acquire_lease("job:slow-job", "other-owner", 1)
        self.assertFalse(leased)

        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        row = runtime.show_run(result["run_id"])
        self.assertEqual(row["status"], "completed")

        released = runtime.store.acquire_lease("job:slow-job", "other-owner", 1)
        self.assertTrue(released)
        runtime.store.release_lease("job:slow-job", "other-owner")

    def test_multi_agent_pipeline_completes_after_reviewer_feedback(self) -> None:
        run_id = self.runtime.execute_job("multi-agent-job")
        row = self.runtime.show_run(run_id)
        task = self.runtime.show_task(int(row["task_id"]))
        summary = Path(str(row["final_message_path"])).read_text(encoding="utf-8")
        tasks = self.runtime.tasks(limit=20, job_id="multi-agent-job")
        agent_kinds = {entry["kind"] for entry in tasks}

        self.assertEqual(row["status"], "completed")
        self.assertEqual(task["status"], "completed")
        self.assertTrue((self.repo / ".review-pass").exists())
        self.assertIn("role=planner", summary)
        self.assertIn("role=reviewer", summary)
        self.assertIn("agent_planner", agent_kinds)
        self.assertIn("agent_executor", agent_kinds)
        self.assertIn("agent_reviewer", agent_kinds)

    def test_dashboard_and_sessions_reflect_operational_state(self) -> None:
        inbox_dir = self.repo / "inbox"
        inbox_dir.mkdir()
        (inbox_dir / "task-001.md").write_text("Routine documentation follow-up.\n", encoding="utf-8")
        (inbox_dir / "task-002.md").write_text("Critical production failure.\n", encoding="utf-8")

        plan_run_id = self.runtime.execute_job("plan-job")
        persistent_run_id = self.runtime.execute_job("persistent-job")
        self.runtime.pause_job("analysis-job", "manual operator pause")

        dashboard = {row["job_id"]: row for row in self.runtime.dashboard()}
        sessions = {row["job_id"]: row for row in self.runtime.sessions()}
        persistent_row = self.runtime.show_run(persistent_run_id)

        self.assertEqual(self.runtime.show_run(plan_run_id)["status"], "awaiting_approval")
        self.assertTrue(dashboard["analysis-job"]["paused"])
        self.assertEqual(
            dashboard["analysis-job"]["schedule_block_reason"],
            "manual operator pause",
        )
        self.assertEqual(dashboard["plan-job"]["awaiting_approval_tasks"], 1)
        self.assertEqual(dashboard["plan-job"]["last_run_status"], "awaiting_approval")
        self.assertEqual(dashboard["inbox-job"]["inbox_backlog"], 2)
        self.assertEqual(
            dashboard["persistent-job"]["session_thread_id"],
            persistent_row["codex_thread_id"],
        )
        self.assertIn("persistent-job", sessions)
        self.assertEqual(
            sessions["persistent-job"]["codex_thread_id"],
            persistent_row["codex_thread_id"],
        )

    def test_operator_note_creates_memory_and_dispatchable_task(self) -> None:
        result = self.runtime.add_operator_note(
            "analysis-job",
            "Check flaky tests before merging.",
            dispatch=True,
        )
        task_id = int(result["task_id"])
        memory_id = int(result["memory_id"])
        task = self.runtime.show_task(task_id)
        memory = self.runtime.show_memory(memory_id)

        self.assertEqual(task["kind"], "operator_note")
        self.assertEqual(task["status"], "ready")
        self.assertEqual(memory["kind"], "operator_note")
        self.assertTrue(Path(str(result["note_path"])).exists())

        run_id = self.runtime.run_task(task_id)
        row = self.runtime.show_run(run_id)
        final_message = Path(str(row["final_message_path"])).read_text(encoding="utf-8")

        self.assertEqual(row["status"], "completed")
        self.assertIn("with memory", final_message)

    def test_parallel_multi_agent_selects_and_merges_best_branch(self) -> None:
        run_id = self.runtime.execute_job("parallel-agent-job")
        row = self.runtime.show_run(run_id)
        task = self.runtime.show_task(int(row["task_id"]))
        summary = Path(str(row["final_message_path"])).read_text(encoding="utf-8")
        tasks = self.runtime.tasks(limit=40, job_id="parallel-agent-job")
        executor_tasks = [entry for entry in tasks if entry["kind"] == "agent_executor"]

        self.assertEqual(row["status"], "completed")
        self.assertEqual(task["status"], "completed")
        self.assertTrue((self.repo / "candidate-branch-robust.txt").exists())
        self.assertFalse((self.repo / "candidate-branch-minimal.txt").exists())
        self.assertGreaterEqual(len(executor_tasks), 4)
        self.assertIn("role=executor[branch-robust]", summary)
        self.assertIn("role=reviewer[branch-select]", summary)
        self.assertIn("Selected branch: branch-robust", summary)

    def test_goal_loop_marks_goal_completed_and_stops_future_schedule(self) -> None:
        first_run_id = self.runtime.execute_job("goal-loop-job", trigger="schedule")
        first_goal = self.runtime.job_status("goal-loop-job")["goal"]
        self.assertEqual(self.runtime.show_run(first_run_id)["status"], "completed")
        self.assertEqual(first_goal["last_outcome"], "continue")

        second_run_id = self.runtime.execute_job("goal-loop-job", trigger="schedule")
        second_goal = self.runtime.job_status("goal-loop-job")["goal"]
        allowed, reason = self.runtime.can_schedule_job("goal-loop-job")

        self.assertEqual(self.runtime.show_run(second_run_id)["status"], "completed")
        self.assertEqual(second_goal["status"], "completed")
        self.assertEqual(second_goal["wake_count"], 2)
        self.assertFalse(allowed)
        self.assertEqual(reason, "goal completed")

    def test_goal_loop_stops_after_repeated_idle_wakes(self) -> None:
        first_run_id = self.runtime.execute_job("goal-idle-job", trigger="schedule")
        first_goal = self.runtime.job_status("goal-idle-job")["goal"]
        self.assertEqual(self.runtime.show_run(first_run_id)["status"], "completed")
        self.assertEqual(first_goal["status"], "idle")

        second_run_id = self.runtime.execute_job("goal-idle-job", trigger="schedule")
        second_goal = self.runtime.job_status("goal-idle-job")["goal"]
        allowed, reason = self.runtime.can_schedule_job("goal-idle-job")

        self.assertEqual(self.runtime.show_run(second_run_id)["status"], "completed")
        self.assertEqual(second_goal["status"], "stopped")
        self.assertFalse(allowed)
        self.assertEqual(reason, "goal stopped")


if __name__ == "__main__":
    unittest.main()


class ScaffoldTests(unittest.TestCase):
    def test_write_rl_research_starter_creates_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "rl-starter"
            written = write_rl_research_starter(root)
            jobs_path = root / "jobs.rl.toml"
            smoke_script = root / "experiments" / "smoke_train.py"
            smoke_test = root / "tests" / "test_smoke.py"
            idea_path = root / "ideas" / "idea.md"

            self.assertTrue(jobs_path.exists())
            self.assertTrue(smoke_script.exists())
            self.assertTrue(smoke_test.exists())
            self.assertTrue(idea_path.exists())
            self.assertIn(jobs_path, written)
            self.assertIn(root.resolve().as_posix(), jobs_path.read_text(encoding="utf-8"))

    def test_write_rl_research_starter_rejects_non_empty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "occupied"
            root.mkdir()
            (root / "keep.txt").write_text("x\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                write_rl_research_starter(root)

    def test_import_idea_bundle_creates_structured_files_from_docx_and_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "rl-starter"
            write_rl_research_starter(root)
            docx_path = Path(tmpdir) / "idea.docx"
            pdf_path = Path(tmpdir) / "idea.pdf"
            self._write_fake_docx(
                docx_path,
                [
                    "RECAP RL Prototype Plan",
                    "Use a minimal environment and validate a short CPU smoke run first.",
                ],
            )
            pdf_path.write_bytes(b"%PDF-1.4\n%fake\n")

            imported = import_idea_bundle(root, docx_path=docx_path, pdf_path=pdf_path)
            source_text = imported.source_text_path.read_text(encoding="utf-8")
            idea_markdown = imported.idea_markdown_path.read_text(encoding="utf-8")
            raw_markdown = imported.raw_markdown_path.read_text(encoding="utf-8")

            self.assertTrue((root / "ideas" / "idea.source.docx").exists())
            self.assertTrue((root / "ideas" / "idea.source.pdf").exists())
            self.assertIn("RECAP RL Prototype Plan", source_text)
            self.assertIn("## Source Highlights", idea_markdown)
            self.assertIn("RECAP RL Prototype Plan", idea_markdown)
            self.assertIn("Imported Idea Raw Notes", raw_markdown)

    def test_import_idea_bundle_requires_at_least_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "rl-starter"
            write_rl_research_starter(root)
            with self.assertRaises(ValueError):
                import_idea_bundle(root)

    def _write_fake_docx(self, path: Path, paragraphs: list[str]) -> None:
        document_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
            "<w:body>"
            + "".join(f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs)
            + "</w:body></w:document>"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
