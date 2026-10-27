"""Task continuity through bounded, attributed context; all calls are offline."""

import json
import tempfile
import unittest

from agy_observer.council import Council, public
from agy_observer.discussions import Discussions
from agy_observer.task_state import TaskState, limits


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Discussions(self.temp.name)
        self.council = Council(self.store)
        self.state = TaskState(self.store)
        self.id = self.council.create("synthetic task", "original", {"gemini": "g", "claude": "c"})[
            "discussion_id"
        ]

    def reveal(self):
        self.council.note(self.id, "client first idea", "initial")
        for p, mid, _, _ in self.council.reserve(self.id, ["gemini", "claude"]):
            self.council.finish(
                self.id,
                mid,
                "success",
                json.dumps({"message": p + " prior view", "question": p + " open question?"}),
            )
        self.council.reveal(self.id)

    def spec(self):
        return dict(
            facts=[dict(text="supplied observation, not externally verified", sources=[1])],
            disagreements=[dict(text="two possible approaches remain", sources=[2, 3])],
            positions=[
                dict(
                    participant="claude",
                    view="prefer small change",
                    reason="limits migration cost",
                    sources=[3],
                )
            ],
            next_steps=[dict(text="verify the disputed assumption", sources=[2])],
        )

    def test_full_memory_plan_and_own_view_survive_eviction_and_restart(self):
        self.reveal()
        plan = "complete plan: " + "路" * 2800 + " IMPORTANT PLAN END"
        self.council.note(self.id, plan, "plan", [1, 2, 3], expected_plan_version=0)
        first = self.state.memory(self.id, self.spec(), 0, "preserve reasons and disagreements")
        for i in range(9):
            self.council.note(self.id, "recent unrelated " + str(i))
        restarted = Council(Discussions(self.temp.name))
        data = restarted.read(self.id)
        prompt, manifest = restarted.context(data, "claude", "", None)
        self.assertIn(plan, prompt)
        self.assertIn("claude prior view", prompt)
        self.assertIn("limits migration cost", prompt)
        self.assertIn("claude open question?", prompt)
        self.assertEqual(manifest["memory_version"], 1)
        self.assertTrue(manifest["full_plan_included"])
        self.assertIn(4, manifest["included_ids"])
        self.assertNotIn(4, manifest["truncated_ids"])
        self.assertEqual(data["memory_versions"][0], first)
        self.assertEqual(data["plan_version"], 1)

    def test_initial_isolation_and_no_early_memory(self):
        self.council.note(self.id, "SECRET initial", "initial")
        with self.assertRaisesRegex(ValueError, "Reveal"):
            self.state.memory(
                self.id,
                dict(facts=[], disagreements=[], positions=[], next_steps=[]),
                0,
                "too early",
            )
        self.state.extend(self.id, {"claude": 2}, 0, "explicit budget")
        prompt, manifest = self.council.context(self.council.read(self.id), "claude", "", None)
        self.assertNotIn("SECRET", prompt)
        self.assertNotIn("task_memory", prompt)
        self.assertEqual(manifest["included_ids"], [])

    def test_memory_compare_and_swap_and_sources(self):
        self.reveal()
        self.state.memory(self.id, self.spec(), 0, "first snapshot")
        before = self.store.file(self.id).read_bytes()
        with self.assertRaisesRegex(ValueError, "version"):
            self.state.memory(self.id, self.spec(), 0, "stale")
        self.assertEqual(self.store.file(self.id).read_bytes(), before)
        for source in [0, 999, True]:
            spec = self.spec()
            spec["facts"][0]["sources"] = [source]
            with self.assertRaisesRegex(ValueError, "sources"):
                self.state.memory(self.id, spec, 1, "bad reference")
        spec = self.spec()
        spec["positions"][0]["sources"] = [2]
        with self.assertRaisesRegex(ValueError, "participant"):
            self.state.memory(self.id, spec, 1, "misattributed position")
        self.state.memory(self.id, self.spec(), 1, "updated snapshot")
        self.assertEqual(len(self.council.read(self.id)["memory_versions"]), 2)

    def test_failed_output_not_a_memory_source(self):
        self.reveal()
        _, mid, _, _ = self.council.reserve(self.id, ["claude"])[0]
        self.council.finish(self.id, mid, "failed", error="synthetic")
        spec = self.spec()
        spec["facts"][0]["sources"] = [mid]
        with self.assertRaisesRegex(ValueError, "successful"):
            self.state.memory(self.id, spec, 0, "failed run cannot prove a fact")

    def test_explicit_uneven_grant_preserves_attempts_and_resumes_exhausted_model(self):
        self.reveal()
        _, mid, _, _ = self.council.reserve(self.id, ["claude"])[0]
        self.council.finish(self.id, mid, "cancelled")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.council.reserve(self.id, ["claude"])
        entry = self.state.extend(self.id, {"claude": 3}, 0, "verify unresolved alternatives")
        data = self.council.read(self.id)
        self.assertEqual(limits(data), {"gemini": 2, "claude": 5})
        self.assertEqual(data["attempts"], {"gemini": 1, "claude": 2})
        self.assertEqual(data["model_attempts"], 3)
        self.assertEqual(data["max_model_turns"], 7)
        self.assertEqual(entry["before"]["claude"], 2)
        self.assertFalse(public(data)["budget_exhausted"])
        self.council.reserve(self.id, ["claude"])

    def test_grants_can_cross_old_cap_but_not_total_cap_or_stale_version(self):
        for i in range(3):
            self.state.extend(self.id, {"claude": 8}, i, "explicit additional budget")
        data = self.council.read(self.id)
        self.assertEqual(limits(data)["claude"], 26)
        with self.assertRaisesRegex(ValueError, "32"):
            self.state.extend(self.id, {"claude": 8}, 3, "too many")
        with self.assertRaisesRegex(ValueError, "version"):
            self.state.extend(self.id, {"claude": 1}, 2, "stale")
        for additions in [{"gpt": 1}, {"claude": -1}, {"claude": True}, {"claude": 9}, {}]:
            with self.assertRaises(ValueError):
                self.state.extend(self.id, additions, 3, "invalid addition")
        self.assertEqual(self.council.read(self.id)["model_attempts"], 0)

    def test_pending_and_archived_tasks_reject_updates(self):
        self.reveal()
        _, mid, _, _ = self.council.reserve(self.id, ["gemini"])[0]
        for update in [
            lambda: self.state.memory(self.id, self.spec(), 0, "pending"),
            lambda: self.state.extend(self.id, {"claude": 1}, 0, "pending"),
        ]:
            with self.assertRaisesRegex(ValueError, "pending"):
                update()
        self.council.finish(self.id, mid, "success", '{"message":"done"}')
        self.council.note(self.id, "archive", "summary")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.state.extend(self.id, {"claude": 1}, 0, "cannot reopen")

    def test_peer_memory_and_budget_share_same_mechanism(self):
        id = self.store.create("peer task", "client", "peer", 1)["discussion_id"]
        self.store.note(id, "observation", "proposal")
        mid, _ = self.store.reserve(id)
        self.store.finish(
            id, mid, "success", '{"message":"peer opinion","question":null,"answers_to":[]}'
        )
        self.state.extend(id, {"reviewer": 2}, 0, "continue review")
        spec = dict(
            facts=[dict(text="observation", sources=[1])],
            disagreements=[],
            positions=[
                dict(participant="reviewer", view="peer opinion", reason="test this", sources=[2])
            ],
            next_steps=[],
        )
        self.state.memory(id, spec, 0, "preserve peer view")
        data = self.store.read(id)
        prompt, manifest = self.store.context(data, "continue", None)
        self.assertIn("peer opinion", prompt)
        self.assertEqual(manifest["memory_version"], 1)
        self.assertEqual(data["model_attempts"], 1)
        self.assertEqual(data["max_model_turns"], 3)

    def test_legacy_documents_read_without_rewrite(self):
        before = self.store.file(self.id).read_bytes()
        data = self.council.read(self.id)
        self.assertNotIn("memory_versions", data)
        self.assertNotIn("budget_limits", data)
        self.assertEqual(self.store.file(self.id).read_bytes(), before)

    def test_budget_audit_tampering_is_rejected(self):
        self.state.extend(self.id, {"claude": 1}, 0, "grant")
        with self.store.writing():
            data = self.council.read(self.id)
            data["budget_limits"]["gemini"] += 1
            self.store.save(data)
        with self.assertRaisesRegex(ValueError, "audit"):
            self.council.read(self.id)

    def test_memory_capacity_does_not_silently_clip(self):
        self.reveal()
        spec = self.spec()
        spec["facts"] = [dict(text="字" * 1400, sources=[1]) for _ in range(4)]
        before = self.store.file(self.id).read_bytes()
        with self.assertRaisesRegex(ValueError, "4500"):
            self.state.memory(self.id, spec, 0, "oversized")
        self.assertEqual(self.store.file(self.id).read_bytes(), before)

    def test_longer_task_and_prompt_overflow_do_not_reset_or_spend_attempts(self):
        self.reveal()
        for i in range(42):
            self.council.note(self.id, "synthetic continuation " + str(i))
        self.assertGreater(len(self.council.read(self.id)["messages"]), 40)
        # A legacy/imported document can contain larger original material. Keep
        # it readable, but reject an oversized request before reserving a call.
        with self.store.writing():
            data = self.council.read(self.id)
            data["facts"] = "长" * 32000
            self.store.save(data)
        before = self.store.file(self.id).read_bytes()
        with self.assertRaisesRegex(ValueError, "32000"):
            self.council.reserve(self.id, ["claude"])
        self.assertEqual(self.store.file(self.id).read_bytes(), before)
