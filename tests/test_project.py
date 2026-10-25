"""Project roles follow shared understanding and retain every stage assignment."""

import json
import tempfile
import unittest
from unittest.mock import patch

from agy_observer.council import Council
from agy_observer.discussions import Discussions
from agy_observer.project import ProjectWorkflow


PROFILE = dict(stage="discovery", materials="README and observed behavior supplied by client", success_criteria="a usable workflow", constraints="no extra model retries", unknowns="best roles for this project")
ROLES = {
    p: dict(role=role, focus="check " + role, output="specific finding and verification", reason="addresses " + role)
    for p, role in (("gpt", "workflow design"), ("gemini", "user journey"), ("claude", "failure testing"))
}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.council = Council(Discussions(self.temp.name))
        self.workflow = ProjectWorkflow(self.council)
        self.identifier = self.council.create("project goal", "raw facts", {"gemini": "g", "claude": "c"}, 3, project=PROFILE)["discussion_id"]

    def initial(self):
        self.council.note(self.identifier, "GPT understanding", "initial")
        batch = self.council.reserve(self.identifier, ["gemini", "claude"])
        for _, mid, _, _ in batch:
            self.council.finish(self.identifier, mid, "success", json.dumps(dict(message="project gap", kind="observation")))
        self.council.reveal(self.identifier)
        return batch

    def assign(self, **kwargs):
        return self.workflow.assign(self.identifier, ROLES, kwargs.pop("stage_name", "implementation"), "why these three roles", kwargs.pop("references", [1, 2, 3]), kwargs.pop("expected_version", 0), **kwargs)

    def test_project_understanding_has_same_material_and_no_role_bias(self):
        batch = self.initial()
        self.assertEqual(batch[0][2], batch[1][2])
        self.assertIn(PROFILE["materials"], batch[0][2])
        self.assertNotIn("workflow design", batch[0][2])
        self.assertIn("项目", batch[0][2])
        data = self.council.read(self.identifier)
        self.assertEqual(data["project"]["role_versions"], [])
        with self.assertRaisesRegex(ValueError, "assign"):
            self.council.note(self.identifier, "premature plan", "plan", [1, 2, 3], expected_plan_version=0)

    def test_first_assignment_needs_three_sources_and_real_choices(self):
        with self.assertRaisesRegex(ValueError, "Reveal"):
            self.assign()
        self.initial()
        with self.assertRaisesRegex(ValueError, "three"):
            self.assign(references=[1, 2])
        bad = {p: {**spec, "role": "same role"} for p, spec in ROLES.items()}
        with self.assertRaisesRegex(ValueError, "distinct"):
            self.workflow.assign(self.identifier, bad, "work", "reason", [1, 2, 3], 0)
        data = self.assign()
        self.assertEqual(data["workflow_stage"], "work")
        self.assertEqual(data["role_version"], 1)
        self.assertIn("user journey", data["roles"]["gemini"])
        self.assertEqual(data["messages"][-1]["kind"], "roles")

    def test_stage_revision_preserves_old_roles_material_and_prompt_snapshot(self):
        self.initial()
        first = self.assign()
        reserved = self.council.reserve(self.identifier, ["gemini"])[0]
        with self.assertRaisesRegex(ValueError, "pending"):
            self.assign(expected_version=1)
        self.council.finish(self.identifier, reserved[1], "success", json.dumps(dict(message="new failure", kind="challenge")))
        changed = {p: {**spec, "role": "release " + spec["role"]} for p, spec in ROLES.items()}
        with self.assertRaisesRegex(ValueError, "version"):
            self.workflow.assign(self.identifier, changed, "release", "new phase", [5], 0)
        second = self.workflow.assign(self.identifier, changed, "release", "new failure evidence", [5], 1, {"unknowns": "release behavior"})
        self.assertEqual(second["project"]["role_versions"][0], first["project"]["role_versions"][0])
        self.assertEqual(second["project"]["brief"], PROFILE)
        self.assertEqual(second["attempts"], {"gemini": 2, "claude": 1})
        next_call = self.council.reserve(self.identifier, ["claude"])[0]
        self.assertEqual(reserved[2].count('"role_version": 1'), 1)
        self.assertIn("release failure testing", next_call[2])
        self.assertIn('"role_version": 2', next_call[2])
        self.assertEqual(second["messages"][4]["role_version"], 1)

    def test_invalid_briefs_and_mixed_preassigned_roles_are_rejected(self):
        for profile in ({}, {**PROFILE, "extra": "unknown"}, {**PROFILE, "materials": "x" * 2500}):
            with self.assertRaises(ValueError):
                self.council.create("bad", "", {"gemini": "g", "claude": "c"}, project=profile)
        with self.assertRaisesRegex(ValueError, "roles"):
            self.council.create("mixed", "", {"gemini": "g", "claude": "c"}, project=PROFILE, roles={p: "role" for p in ROLES})

    def test_framing_can_continue_without_roles_and_closed_task_cannot_reassign(self):
        self.initial()
        item = self.council.reserve(self.identifier, ["claude"])[0]
        material = json.loads(item[2].rsplit("\n", 1)[-1])
        self.assertNotIn("role", material)
        self.council.finish(self.identifier, item[1], "success", json.dumps(dict(message="clarification", kind="question")))
        self.council.note(self.identifier, "stop before assignment", "summary")
        with self.assertRaisesRegex(ValueError, "closed"):
            self.assign()

    def test_corrupted_role_history_cannot_change_active_prompts(self):
        self.initial()
        self.assign()
        data = self.council.read(self.identifier)
        data["roles"]["claude"] = "silently changed"
        self.council.store.save(data)
        with self.assertRaisesRegex(ValueError, "history"):
            self.council.read(self.identifier)

    def test_saved_prompts_do_not_depend_on_future_prompt_generator(self):
        self.initial()
        data = self.assign()
        self.assertEqual(data["roles"], data["project"]["role_versions"][0]["prompts"])
        with patch("agy_observer.project.prompts", side_effect=RuntimeError("a future generator")):
            restored = self.council.read(self.identifier)
        self.assertEqual(restored["roles"], data["roles"])
