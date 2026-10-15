"""Offline acceptance tests. Never call GitHub or an LLM provider."""

import contextlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from audit.replay import list_threads
from common.db import replay_events
from common.schemas import PRAnalysis
from exercises import exercise_1_confidence as e1
from exercises import exercise_2_hitl as e2
from exercises import exercise_3_escalation as e3
from exercises import exercise_4_audit as e4


class ReviewWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Keep temporary databases inside the workspace on restricted Windows hosts.
        fd, name = tempfile.mkstemp(prefix="hitl-test-", suffix=".db", dir=Path.cwd())
        os.close(fd)
        self.db = name
        self.env = patch.dict(os.environ, {"HITL_DB_PATH": name, "GITHUB_USER": "test-reviewer"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def test_routing_boundaries(self):
        for module in (e1, e2, e3):
            module.build_graph()
            with patch.object(module.console, "quiet", True):
                for confidence, expected in ((0.579, "escalate"), (0.58, "human_approval"),
                                             (0.729, "human_approval"), (0.73, "auto_approve")):
                    a = PRAnalysis(summary="test", confidence=confidence, confidence_reasoning="test")
                    self.assertEqual(module.node_route({"analysis": a})["decision"], expected)

    def test_explicit_decision_and_nonempty_edit_required(self):
        for response in ({}, {"choice": "unexpected"}, {"choice": "edit"},
                         {"choice": "edit", "edited_body": "   "}, None):
            with self.subTest(response=response), self.assertRaises(ValueError):
                e4.validate_approval_response(response)

    def test_exercise3_edit_confirm_and_cancel(self):
        payload = {"kind": "approval_request", "summary": "test", "confidence": .65}
        with patch.object(e3.console, "quiet", True):
            for confirmed in (True, False):
                with patch.object(e3.console, "input", side_effect=["edit", "note", "edited review", "y" if confirmed else "n"]):
                    answer = e3.handle_interrupt(payload)
                    self.assertEqual(answer["choice"], "edit" if confirmed else "reject")
                    if confirmed:
                        self.assertEqual(answer["edited_body"], "edited review")

    async def run_case(self, confidence, choice=None, ui=False, failure=False):
        analysis = PRAnalysis(summary="Offline review", confidence=confidence,
                              confidence_reasoning="Offline fixture", escalation_questions=["Intent?"])

        class LLM:
            def with_structured_output(self, *_):
                return self

            async def ainvoke(self, *_):
                return analysis

        pr = SimpleNamespace(title="Fixture", diff="+ fixture", files_changed=["fixture.py"], head_sha="abc123456")
        cfg = {"configurable": {"thread_id": "offline-case"}}
        with patch.object(e4.console, "quiet", True), patch.object(e4, "fetch_pr", return_value=pr), \
             patch.object(e4, "get_llm", return_value=LLM()), patch.object(e4, "post_review_comment") as post:
            if failure:
                post.side_effect = RuntimeError("Simulated GitHub failure")
            async with AsyncSqliteSaver.from_conn_string(self.db) as cp:
                await cp.setup()
                graph = e4.build_graph(cp, confirm_after_synthesize=ui)
                result = await graph.ainvoke({"pr_url": "https://github.com/test/test/pull/1",
                                             "thread_id": "offline-case"}, cfg)
            if confidence < .73:
                self.assertEqual(post.call_count, 0)
                # Reopen the database/checkpointer to model a process restart.
                async with AsyncSqliteSaver.from_conn_string(self.db) as cp:
                    graph = e4.build_graph(cp, confirm_after_synthesize=ui)
                    snapshot = await graph.aget_state(cfg)
                    self.assertTrue(snapshot.tasks[0].interrupts)
                    if confidence < .58:
                        result = await graph.ainvoke(Command(resume={"Intent?": "No risks resolved"}), cfg)
                        if ui:
                            self.assertEqual(result["__interrupt__"][0].value["kind"], "synthesize_approval")
                            self.assertEqual(post.call_count, 0)
                    if choice:
                        result = await graph.ainvoke(Command(resume={"choice": choice, "edited_body": "CUSTOM REVIEW"}), cfg)
            self.assertNotIn("__interrupt__", result)
            self.assertEqual(post.call_count, 0 if choice == "reject" else 1)
            if choice == "edit":
                self.assertEqual(post.call_args.args[1], "CUSTOM REVIEW")
            events = await replay_events("offline-case")
            pending = [ev["action"] for ev in events if ev["decision"] == "pending"
                       and ev["action"] in {"human_approval", "escalate", "confirm_synthesize"}]
            self.assertEqual(len(pending), len(set(pending)))
            self.assertEqual(sum(ev["action"] == "fetch_pr" for ev in events), 1)
            if confidence < .58:
                self.assertIn("No risks resolved", next(ev["reason"] for ev in events
                              if ev["action"] == "escalate" and ev["decision"] == "escalate"))
                self.assertEqual(events[-1]["reviewer_id"], "test-reviewer")
            if failure:
                self.assertIn("failed", result["final_action"])
                self.assertEqual(events[-1]["decision"], "failed")
            return result

    async def test_approve_and_resume_from_disk(self):
        self.assertEqual((await self.run_case(.65, "approve"))["final_action"], "committed")

    async def test_reject_does_not_post(self):
        self.assertEqual((await self.run_case(.65, "reject"))["final_action"], "rejected")

    async def test_edit_posts_exact_text(self):
        await self.run_case(.65, "edit")

    async def test_auto_posts_without_interrupt(self):
        await self.run_case(.73)

    async def test_escalation_cli_records_reviewer(self):
        await self.run_case(.4)

    async def test_escalation_ui_reject(self):
        await self.run_case(.4, "reject", ui=True)

    async def test_escalation_ui_edit(self):
        await self.run_case(.4, "edit", ui=True)

    async def test_github_failure_is_not_rejection(self):
        await self.run_case(.65, "approve", failure=True)

    async def test_auto_failure(self):
        await self.run_case(.73, failure=True)

    async def test_missing_decision_never_posts(self):
        a = PRAnalysis(summary="test", confidence=.65, confidence_reasoning="test")
        with patch.object(e4.console, "quiet", True), patch.object(e4, "post_review_comment") as post:
            result = await e4.node_commit({"thread_id": "invalid", "pr_url": "test", "analysis": a})
            self.assertEqual(result["final_action"], "rejected")
            post.assert_not_called()

    async def test_retry_failed_synthesis_uses_saved_answers(self):
        a = PRAnalysis(summary="test", confidence=.4, confidence_reasoning="test", escalation_questions=["Intent?"])
        calls = 0

        class LLM:
            def with_structured_output(self, *_):
                return self

            async def ainvoke(self, *_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("Simulated credit limit")
                return a

        pr = SimpleNamespace(title="test", diff="+ test", files_changed=["a.py"], head_sha="abc1234")
        cfg = {"configurable": {"thread_id": "retry"}}
        with patch.object(e4.console, "quiet", True), patch.object(e4, "get_llm", return_value=LLM()), \
             patch.object(e4, "fetch_pr", return_value=pr) as fetch, patch.object(e4, "post_review_comment") as post:
            async with AsyncSqliteSaver.from_conn_string(self.db) as cp:
                await cp.setup()
                graph = e4.build_graph(cp, confirm_after_synthesize=True)
                await graph.ainvoke({"pr_url": "https://github.com/test/test/pull/1", "thread_id": "retry"}, cfg)
                with self.assertRaisesRegex(RuntimeError, "credit limit"):
                    await graph.ainvoke(Command(resume={"Intent?": "Saved answer"}), cfg)
            async with AsyncSqliteSaver.from_conn_string(self.db) as cp:
                graph = e4.build_graph(cp, confirm_after_synthesize=True)
                snapshot = await graph.aget_state(cfg)
                self.assertEqual(snapshot.values["escalation_answers"], {"Intent?": "Saved answer"})
                result = await graph.ainvoke(None, cfg)
                self.assertEqual(result["__interrupt__"][0].value["kind"], "synthesize_approval")
            fetch.assert_called_once()
            post.assert_not_called()
            events = await replay_events("retry")
            self.assertEqual(sum(ev["action"] == "escalate" and ev["decision"] == "escalate" for ev in events), 1)

    async def test_streamlit_restore_edit_validation_and_reject(self):
        # Streamlit's test runner creates its own temporary files.
        with patch.object(tempfile, "TemporaryDirectory", return_value=SimpleNamespace(name=str(Path.cwd()))):
            await self.check_streamlit_flow()

    async def check_streamlit_flow(self):
        from streamlit.testing.v1 import AppTest

        a = PRAnalysis(summary="UI fixture", confidence=.65, confidence_reasoning="test")
        cfg = {"configurable": {"thread_id": "ui-fixture"}}
        # Seed a pending approval without invoking GitHub or the LLM.
        async with AsyncSqliteSaver.from_conn_string(self.db) as cp:
            await cp.setup()
            graph = e4.build_graph(cp, confirm_after_synthesize=True)
            await graph.aupdate_state(cfg, {"pr_url": "https://github.com/test/test/pull/1",
                                           "thread_id": "ui-fixture", "analysis": a, "pr_diff": "+ fixture"},
                                      as_node="pre_human_approval")
            await graph.ainvoke(None, cfg)
        with patch.object(e4.console, "quiet", True), patch.object(e4, "post_review_comment") as post:
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=15)
            at.query_params["thread"] = "ui-fixture"
            at.run()
            self.assertFalse(at.exception)
            self.assertIn("Human Approval Requested", " ".join(h.value for h in at.markdown))
            next(b for b in at.button if b.label == "✏️ Edit Review").click().run()
            at.text_area[0].set_value("   ").run()
            next(b for b in at.button if "Confirm & Post" in b.label).click().run()
            self.assertTrue(any("must not be empty" in e.value for e in at.error))
            next(b for b in at.button if b.label == "❌ Cancel Edit").click().run()
            next(b for b in at.button if b.label == "🚫 Reject Review").click().run()
            self.assertFalse(at.exception)
            self.assertTrue(any("rejected" in w.value for w in at.warning),
                            {"warnings": [w.value for w in at.warning], "errors": [e.value for e in at.error],
                             "final": at.session_state["final"]})
            post.assert_not_called()

    async def test_replay_worst_risk_uses_severity_order(self):
        await self.run_case(.4)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            await list_threads()
        self.assertIn("high", output.getvalue())


if __name__ == "__main__":
    unittest.main()
