"""Persistent collaboration across tasks, restarts, pauses and paged archives."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from agy_observer.archive import LIMIT, page_view
from agy_observer.council import Council, public
from agy_observer.discussions import Discussions
from agy_observer.projects import Projects
from agy_observer.task_state import TaskState


class PersistentProjectsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Discussions(self.temp.name)
        self.council = Council(self.store)
        self.projects = Projects(self.store)
        self.projects.update(
            "synthetic",
            0,
            "create lasting project",
            name="Synthetic project",
            goal="Complete verified work",
        )
        self.id = self.council.create(
            "first topic", "supplied facts", {"gemini": "g", "claude": "c"}
        )["discussion_id"]

    def reveal(self, identifier=None):
        identifier = identifier or self.id
        self.council.note(identifier, "client observation", "initial")
        for p, mid, _, _ in self.council.reserve(identifier, ["gemini", "claude"]):
            self.council.finish(
                identifier,
                mid,
                "success",
                json.dumps({"message": p.upper() + "_VIEW", "question": p + " pending question?"}),
            )
        return self.council.reveal(identifier)

    def link(self, identifier=None, on_demand=True):
        return self.projects.link(
            "synthetic",
            identifier or self.id,
            self.projects.load("synthetic")["version"],
            "explicit project linkage",
            on_demand,
        )

    def test_project_calls_continue_past_32_without_resetting_usage(self):
        self.reveal()
        self.link()
        for i in range(34):
            _, mid, _, _ = self.council.reserve(self.id, ["claude"])[0]
            self.council.finish(
                self.id, mid, "success", json.dumps({"message": "progress " + str(i)})
            )
        data = self.council.read(self.id)
        self.assertEqual(data["attempts"]["claude"], 35)
        self.assertEqual(data["max_turns_per_model"], 2)
        self.assertFalse(public(data)["budget_exhausted"])
        self.assertEqual(self.projects.read("synthetic")["lifetime_attempts"], 36)
        with self.assertRaisesRegex(ValueError, "on demand"):
            TaskState(self.store).extend(self.id, {"claude": 1}, 0, "no new grant needed")

    def test_cross_task_memory_plan_own_view_and_restart(self):
        self.reveal()
        self.council.note(
            self.id,
            "FULL PLAN " + "项" * 1000 + " PLAN END",
            "plan",
            [1, 2, 3],
            expected_plan_version=0,
        )
        self.link()
        original = self.projects.load("synthetic")
        memory = dict(
            facts=[dict(text="observed fact", sources=[self.id + "#1"])],
            disagreements=[
                dict(text="keep alternatives", sources=[self.id + "#2", self.id + "#3"])
            ],
            positions=[
                dict(
                    participant="claude",
                    view="smaller migration",
                    reason="lower cost",
                    sources=[self.id + "#3"],
                )
            ],
            next_steps=[],
        )
        self.projects.update("synthetic", original["version"], "retain disagreement", memory=memory)
        other = self.council.create(
            "second topic", "fresh material", {"gemini": "g", "claude": "c"}
        )["discussion_id"]
        self.link(other)
        restarted = Council(Discussions(self.temp.name))
        prompt, manifest = restarted.context(restarted.read(other), "gemini", "", None)
        self.assertIn("observed fact", prompt)
        self.assertIn("PLAN END", prompt)
        self.assertIn("GEMINI_VIEW", prompt)
        self.assertNotIn("CLAUDE_VIEW", prompt)
        self.assertNotIn("smaller migration", prompt)
        self.assertEqual(manifest["project_id"], "synthetic")
        self.assertEqual(manifest["project_own_view_source"], self.id + "#2")
        self.assertEqual(self.projects.load("synthetic")["memory"], memory)

    def test_local_memory_and_plan_automatically_checkpoint_without_relabeling(self):
        self.reveal()
        self.link()
        self.council.note(
            self.id, "current shared plan", "plan", [1, 2, 3], expected_plan_version=0
        )
        self.assertEqual(self.projects.load("synthetic")["plan"]["sources"], [self.id + "#4"])
        spec = dict(
            facts=[dict(text="provided fact", sources=[1])],
            disagreements=[],
            positions=[],
            next_steps=[],
        )
        TaskState(self.store).memory(self.id, spec, 0, "keep fact")
        checkpoint = self.projects.load("synthetic")
        self.assertEqual(checkpoint["memory"]["facts"][0]["sources"], [self.id + "#1"])
        self.assertEqual(checkpoint["source"], "client")
        before = checkpoint["version"]
        self.council.note(self.id, "ordinary comment")
        self.assertEqual(self.projects.load("synthetic")["version"], before)

    def test_pause_resume_and_completion_are_explicit_not_round_based(self):
        self.reveal()
        self.link()
        v = self.projects.load("synthetic")["version"]
        paused = self.projects.update("synthetic", v, "wait for measurements", status="paused")
        with self.assertRaisesRegex(ValueError, "paused"):
            self.council.reserve(self.id, ["claude"])
        self.assertTrue(self.projects.read("synthetic")["participant_views"])
        resumed = self.projects.update(
            "synthetic", paused["version"], "measurements available", status="active"
        )
        item = self.council.reserve(self.id, ["claude"])[0]
        self.projects.update("synthetic", resumed["version"], "pause new requests", status="paused")
        self.council.finish(self.id, item[1], "success", '{"message":"existing request finished"}')
        v = self.projects.load("synthetic")["version"]
        completed = self.projects.update(
            "synthetic", v, "owner marks work complete", status="completed"
        )
        with self.assertRaisesRegex(ValueError, "completed"):
            self.council.reserve(self.id, ["gemini"])
        self.projects.update(
            "synthetic", completed["version"], "explicit new work", status="active"
        )
        self.assertFalse(self.council.read(self.id)["closed"])

    def test_summary_archives_topic_without_completing_project(self):
        self.reveal()
        self.link()
        self.council.note(self.id, "topic done, project continues", "summary")
        self.assertEqual(self.projects.load("synthetic")["status"], "active")
        self.assertTrue(self.council.read(self.id)["closed"])
        other = self.store.create("next task", "client", "peer")["discussion_id"]
        self.link(other)
        self.assertEqual(len(self.projects.read("synthetic")["discussions"]), 2)

    def test_sources_cannot_cross_project_or_publish_sealed_initials(self):
        self.council.note(self.id, "SEALED", "initial")
        self.link()
        with self.assertRaisesRegex(ValueError, "Reveal"):
            self.projects.source("synthetic", self.id + "#1")
        self.projects.update("other", 0, "other project", name="Other", goal="Different project")
        other = self.store.create("other topic", "lead", "peer")["discussion_id"]
        self.projects.link("other", other, 1, "bind other")
        self.store.note(other, "other data", "proposal")
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.projects.source("synthetic", other + "#1")

    def test_project_positions_need_own_successful_source(self):
        self.reveal()
        self.link()
        memory = dict(
            facts=[],
            disagreements=[],
            next_steps=[],
            positions=[
                dict(participant="claude", view="claim", reason="why", sources=[self.id + "#2"])
            ],
        )
        v = self.projects.load("synthetic")["version"]
        with self.assertRaisesRegex(ValueError, "own source"):
            self.projects.update("synthetic", v, "misattribution", memory=memory)
        self.assertEqual(self.projects.load("synthetic")["version"], v)

    def test_old_checkpoint_and_exact_source_retrieval(self):
        self.reveal()
        self.link()
        record = self.projects.read("synthetic", sources=[self.id + "#3"], query="CLAUDE_VIEW")
        self.assertEqual(record["sources"][0]["message"]["source"], "model")
        self.assertEqual(record["matches"][0]["reference"], self.id + "#3")
        old = self.projects.read("synthetic", version=1)
        self.assertEqual(old["project"]["version"], 1)
        self.assertEqual(old["project"]["memory"]["facts"], [])

    def test_checkpoints_do_not_expire_after_32_changes(self):
        for i in range(35):
            v = self.projects.load("synthetic")["version"]
            self.projects.update("synthetic", v, "checkpoint " + str(i), status="active")
        self.assertEqual(self.projects.load("synthetic")["version"], 36)
        with self.assertRaisesRegex(ValueError, "version"):
            self.projects.update("synthetic", 35, "stale", status="paused")

    def test_paged_history_exceeds_old_file_and_message_caps_with_exact_backup(self):
        before = self.store.file(self.id).read_bytes()
        self.link()
        backup = self.store.file(self.id).with_suffix(".pages") / "before-project.json"
        self.assertEqual(backup.read_bytes(), before)
        with self.store.writing():
            data = self.council.read(self.id)
            data["phase"] = "discuss"
            data["messages"] = [
                dict(
                    id=i,
                    author="gpt",
                    participant="gpt",
                    source="client",
                    channel="client",
                    kind="observation",
                    state="success",
                    content="离线" * 1900,
                    time_utc="2026-01-01T00:00:00Z",
                    references=[],
                    answers_to=[],
                )
                for i in range(1, 241)
            ]
            self.store.save(data)
        restored = self.council.read(self.id)
        self.assertEqual(len(restored["messages"]), 240)
        self.assertGreater(len(json.dumps(restored, ensure_ascii=False).encode()), LIMIT)
        self.assertLess(self.store.file(self.id).stat().st_size, LIMIT)
        page = page_view(public(restored), 0, 50)
        self.assertEqual(page["messages"][-1]["id"], 50)
        self.assertEqual(page["message_page"]["next_offset"], 50)
        latest = page_view(public(restored))
        self.assertEqual(latest["messages"][0]["id"], 141)
        _, manifest = self.council.context(restored, "claude", "", None)
        self.assertNotIn("omitted_ids", manifest)
        self.assertEqual(manifest["omitted_ids_ranges"], [[1, 234]])

    def test_corrupted_page_is_rejected(self):
        self.reveal()
        self.link()
        head = json.loads(self.store.file(self.id).read_text())
        digest = head["archive"]["groups"]["messages"][0]["sha256"]
        path = self.store.file(self.id).with_suffix(".pages") / (digest + ".json")
        path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "digest"):
            self.council.read(self.id)

    def test_project_binding_cannot_be_forged_or_switched(self):
        self.link()
        self.projects.update("other", 0, "other", name="Other", goal="other")
        with self.assertRaisesRegex(ValueError, "already linked"):
            self.projects.link("other", self.id, 1, "cannot rebind")
        path = self.store.file(self.id)
        head = json.loads(path.read_text())
        head["project_continuous"] = False
        path.write_text(json.dumps(head))
        with self.assertRaisesRegex(ValueError, "policy"):
            self.council.read(self.id)

    def test_opt_in_budgeted_binding_keeps_previous_limit(self):
        self.reveal()
        self.link(on_demand=False)
        item = self.council.reserve(self.id, ["claude"])[0]
        self.council.finish(self.id, item[1], "cancelled")
        with self.assertRaisesRegex(ValueError, "budget"):
            self.council.reserve(self.id, ["claude"])

    def test_link_validation_happens_before_any_mutation(self):
        before = self.store.file(self.id).read_bytes()
        with self.assertRaisesRegex(ValueError, "reason"):
            self.projects.link("synthetic", self.id, 1, "")
        self.assertEqual(self.store.file(self.id).read_bytes(), before)
        self.assertFalse(self.projects.members_folder("synthetic").exists())

    def test_task_memory_versions_continue_and_public_read_remains_bounded(self):
        self.reveal()
        self.link()
        memory = dict(
            facts=[dict(text="retained observation", sources=[1])],
            disagreements=[],
            positions=[],
            next_steps=[],
        )
        state = TaskState(self.store)
        for i in range(34):
            state.memory(self.id, memory, i, "explicit memory update " + str(i))
        data = self.council.read(self.id)
        self.assertEqual(len(data["memory_versions"]), 34)
        viewed = page_view(public(data))
        self.assertEqual(viewed["memory_version"], 34)
        self.assertEqual(len(viewed["memory_versions"]), 20)
        self.assertEqual(viewed["memory_history_omitted"], 14)
        self.assertGreater(self.projects.load("synthetic")["version"], 32)

    def test_project_role_assignments_continue_across_many_stages(self):
        from agy_observer.project import ProjectWorkflow
        from tests.test_project import PROFILE, ROLES

        self.id = self.council.create(
            "staged work", "facts", {"gemini": "g", "claude": "c"}, project=PROFILE
        )["discussion_id"]
        self.reveal()
        self.link()
        workflow = ProjectWorkflow(self.council)
        for i in range(10):
            workflow.assign(
                self.id, ROLES, "stage " + str(i), "new stage needs explicit roles", [1, 2, 3], i
            )
        self.assertEqual(len(self.council.read(self.id)["project"]["role_versions"]), 10)
