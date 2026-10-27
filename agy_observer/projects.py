"""Persistent project records shared by explicitly linked discussions.

The provider sessions remain independent requests. Public source material and
each participant's own public views provide continuity across tasks/restarts.
"""

from copy import deepcopy
import json
import re

from .archive import atomic, bounded
from .discussions import checked, clipped, units
from .runtime import strict_json, utc

ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
SOURCE = re.compile(r"([A-Za-z0-9_-]{1,100})#([1-9][0-9]*)")
EMPTY = dict(facts=[], disagreements=[], positions=[], next_steps=[])


def continuous(data):
    return bool(data.get("project_id")) and data.get("project_continuous") is True


def compact_manifest(manifest):
    # A complete omission list need not grow once for every new public turn.
    for field in ("omitted_ids", "question_ids_omitted"):
        values = manifest.pop(field, [])
        ranges = []
        for value in sorted(set(values)):
            if ranges and ranges[-1][1] + 1 == value:
                ranges[-1][1] = value
            else:
                ranges.append([value, value])
        manifest[field + "_ranges"] = ranges
        manifest[field + "_count"] = len(values)


class Projects:
    def __init__(self, store):
        self.store = store
        self.root = store.root.parent / "projects"

    def folder(self, identifier):
        if not isinstance(identifier, str) or not ID.fullmatch(identifier):
            raise ValueError("Invalid project identifier")
        folder = self.root / identifier
        if self.root.is_symlink() or folder.is_symlink():
            raise ValueError("Project symlinks are not supported")
        return folder

    def members_folder(self, identifier):
        folder = self.folder(identifier) / "discussions"
        if folder.is_symlink():
            raise ValueError("Project membership symlinks are not supported")
        return folder

    def load(self, identifier, version=None):
        folder = self.folder(identifier)
        if version is None:
            paths = sorted(
                p for p in folder.glob("*.json") if re.fullmatch(r"[0-9]{20}\.json", p.name)
            )
            if not paths:
                raise ValueError(
                    "Project does not exist; create it explicitly with expected_version=0"
                )
            path = paths[-1]
        else:
            if type(version) is not int or version < 1:
                raise ValueError("Invalid project version")
            path = folder / f"{version:020d}.json"
        data = strict_json(bounded(path).decode("utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("protocol") != "project-v1"
            or data.get("project_id") != identifier
            or type(data.get("version")) is not int
            or data["version"] != int(path.stem)
            or data.get("status") not in {"active", "paused", "completed"}
            or data.get("source") != "client"
            or not isinstance(data.get("memory"), dict)
            or set(data["memory"]) != set(EMPTY)
        ):
            raise ValueError("Invalid project snapshot")
        checked(data.get("name"), 200, "project name")
        checked(data.get("goal"), 2000, "project goal")
        checked(data.get("reason"), 1000, "project reason")
        if units(json.dumps(data["memory"], ensure_ascii=False)) > 4500:
            raise ValueError("Project memory exceeds 4500 UTF-16 units")
        for section, rows in data["memory"].items():
            if not isinstance(rows, list) or len(rows) > 8:
                raise ValueError("Invalid stored project memory")
            for row in rows:
                fields = (
                    {"participant", "view", "reason", "sources"}
                    if section == "positions"
                    else {"text", "sources"}
                )
                if not isinstance(row, dict) or set(row) != fields:
                    raise ValueError("Invalid stored project memory entry")
                refs = row["sources"]
                if (
                    not isinstance(refs, list)
                    or not 1 <= len(refs) <= 12
                    or any(not isinstance(r, str) or not SOURCE.fullmatch(r) for r in refs)
                ):
                    raise ValueError("Invalid stored project sources")
                for key in ("view", "reason") if section == "positions" else ("text",):
                    checked(row[key], 1500, "stored project memory")
        plan = data.get("plan")
        if plan is not None:
            if not isinstance(plan, dict) or set(plan) != {"text", "sources"}:
                raise ValueError("Invalid project plan")
            checked(plan["text"], 4000, "project plan")
        return data

    def append(self, data, reason):
        result = deepcopy(data)
        result.update(
            version=data["version"] + 1,
            reason=checked(reason, 1000, "reason"),
            updated_utc=utc(),
            source="client",
        )
        folder = self.folder(data["project_id"])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{result["version"]:020d}.json'
        if path.exists():
            raise ValueError("Project version changed; read before updating")
        atomic(path, json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8"))
        return result

    def member(self, project_id, discussion_id):
        self.store.file(discussion_id)
        path = self.members_folder(project_id) / (discussion_id + ".json")
        if not path.exists():
            raise ValueError("Discussion is not linked to this project")
        data = strict_json(bounded(path).decode("utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("project_id") != project_id
            or data.get("discussion_id") != discussion_id
            or type(data.get("continuous")) is not bool
        ):
            raise ValueError("Invalid project membership")
        return data

    def validate_binding(self, data):
        if "project_id" not in data:
            if "project_continuous" in data:
                raise ValueError("Continuous calls require explicit project membership")
            return
        self.load(data["project_id"])
        member = self.member(data["project_id"], data["discussion_id"])
        if member["continuous"] is not data.get("project_continuous"):
            raise ValueError("Project call policy differs from its membership record")

    def ensure_active(self, data):
        if data.get("project_id") and self.load(data["project_id"])["status"] != "active":
            raise ValueError(
                "Project is paused or completed; resume it explicitly before new discussion writes/calls"
            )

    def discussions(self, identifier):
        result = []
        folder = self.members_folder(identifier)
        for path in sorted(folder.glob("*.json")):
            self.member(identifier, path.stem)
            data = self.store.read(path.stem)
            if data.get("project_id") == identifier:
                result.append(data)
        return result

    def source(self, identifier, reference):
        match = SOURCE.fullmatch(reference) if isinstance(reference, str) else None
        if not match:
            raise ValueError("Project sources use discussion_id#message_id")
        discussion_id, message_id = match[1], int(match[2])
        self.member(identifier, discussion_id)
        data = self.store.read(discussion_id)
        if data.get("project_id") != identifier or not 1 <= message_id <= len(data["messages"]):
            raise ValueError("Source is outside the project's linked discussions")
        message = data["messages"][message_id - 1]
        if message.get("state", "success") != "success":
            raise ValueError("Project memory sources must be successful messages")
        if data.get("phase") == "collect" and message["kind"] == "initial":
            raise ValueError("Reveal initial judgments before using them in project memory")
        return deepcopy(message)

    def sources(self, identifier, references):
        if (
            not isinstance(references, list)
            or not 1 <= len(references) <= 12
            or any(not isinstance(r, str) for r in references)
            or len(set(references)) != len(references)
        ):
            raise ValueError("Provide 1..12 distinct project source references")
        return [self.source(identifier, r) for r in references]

    def memory(self, identifier, value):
        if not isinstance(value, dict) or set(value) != set(EMPTY):
            raise ValueError("Project memory needs facts, disagreements, positions and next_steps")
        result = {}
        for section, rows in value.items():
            if not isinstance(rows, list) or len(rows) > 8:
                raise ValueError("Project memory accepts at most 8 rows per section")
            result[section] = []
            for row in rows:
                fields = (
                    {"participant", "view", "reason", "sources"}
                    if section == "positions"
                    else {"text", "sources"}
                )
                if not isinstance(row, dict) or set(row) != fields:
                    raise ValueError("Invalid project memory entry")
                originals = self.sources(identifier, row["sources"])
                entry = dict(sources=list(row["sources"]))
                if section == "positions":
                    participant = row["participant"]
                    if participant not in {
                        "gpt",
                        "gemini",
                        "claude",
                        "codex",
                        "reviewer",
                    } or not any(
                        m.get("participant", "gpt" if m["author"] == "lead" else m["author"])
                        == participant
                        for m in originals
                    ):
                        raise ValueError("A project position needs that participant's own source")
                    entry.update(
                        participant=participant,
                        view=checked(row["view"], 1000, "view"),
                        reason=checked(row["reason"], 1000, "reason"),
                    )
                else:
                    entry["text"] = checked(row["text"], 1500, "memory text")
                result[section].append(entry)
        if units(json.dumps(result, ensure_ascii=False)) > 4500:
            raise ValueError("Project memory exceeds 4500 UTF-16 units; narrow explicitly")
        return result

    def update(
        self,
        identifier,
        expected,
        reason,
        *,
        name=None,
        goal=None,
        memory=None,
        plan=None,
        plan_sources=None,
        status=None,
        capture_discussion_id=None,
    ):
        reason = checked(reason, 1000, "reason")
        if type(expected) is not int or expected < 0:
            raise ValueError("Invalid expected project version")
        with self.store.writing():
            folder = self.folder(identifier)
            if expected == 0:
                if any(folder.glob("[0-9]*.json")):
                    raise ValueError("Project version changed; read before updating")
                data = dict(
                    protocol="project-v1",
                    project_id=identifier,
                    version=0,
                    name=checked(name, 200, "project name"),
                    goal=checked(goal, 2000, "project goal"),
                    status="active",
                    memory=deepcopy(EMPTY),
                    plan=None,
                    source="client",
                    created_utc=utc(),
                )
            else:
                data = self.load(identifier)
                if data["version"] != expected:
                    raise ValueError("Project version changed; read before updating")
            if status is not None and status not in {"active", "paused", "completed"}:
                raise ValueError("Choose active, paused or completed project status")
            if capture_discussion_id is not None:
                if any(v is not None for v in (name, goal, memory, plan, plan_sources, status)):
                    raise ValueError(
                        "Capture synchronization separately from other project changes"
                    )
                task = self.store.read(capture_discussion_id)
                if task.get("project_id") != identifier:
                    raise ValueError("Capture must reference this project's linked discussion")
                if any(m.get("state") == "running" for m in task["messages"]):
                    raise ValueError("Wait for active calls before synchronizing")
                self.observe(task)
                return self.append(self.load(identifier), reason)
            if any(v is not None for v in (memory, plan, name, goal)) and expected:
                if any(
                    m.get("state") == "running"
                    for d in self.discussions(identifier)
                    for m in d["messages"]
                ):
                    raise ValueError(
                        "Wait for active model calls before replacing shared project context"
                    )
            if name is not None:
                data["name"] = checked(name, 200, "project name")
            if goal is not None:
                data["goal"] = checked(goal, 2000, "project goal")
            if memory is not None:
                data["memory"] = self.memory(identifier, memory)
            if plan is not None:
                if plan == "":
                    if plan_sources:
                        raise ValueError("A cleared plan cannot have sources")
                    data["plan"] = None
                else:
                    self.sources(identifier, plan_sources)
                    data["plan"] = dict(
                        text=checked(plan, 4000, "project plan"), sources=plan_sources
                    )
            elif plan_sources is not None:
                raise ValueError("plan_sources requires an explicit plan")
            if status == "completed" and any(
                m.get("state") == "running"
                for d in self.discussions(identifier)
                for m in d["messages"]
            ):
                raise ValueError("Wait for or cancel active calls before completing the project")
            if status is not None:
                data["status"] = status
            return self.append(data, reason)

    def link(self, identifier, discussion_id, expected, reason, on_demand=True):
        reason = checked(reason, 1000, "reason")
        if type(on_demand) is not bool:
            raise ValueError("on_demand must be boolean")
        with self.store.writing():
            project = self.load(identifier)
            if type(expected) is not int or project["version"] != expected:
                raise ValueError("Project version changed; read before linking")
            if project["status"] != "active":
                raise ValueError("Resume the project before linking a discussion")
            data = self.store.read(discussion_id)
            if data.get("project_id"):
                raise ValueError("Discussion is already linked; its project binding is immutable")
            if any(m.get("state") == "running" for m in data["messages"]):
                raise ValueError("Wait for active calls before linking")
            data.update(project_id=identifier, project_continuous=on_demand, updated_utc=utc())
            self.preflight(data)
            folder = self.members_folder(identifier)
            folder.mkdir(parents=True, exist_ok=True)
            member = dict(
                project_id=identifier,
                discussion_id=discussion_id,
                continuous=on_demand,
                linked_utc=utc(),
                observed_plan=0,
                observed_memory=0,
            )
            atomic(folder / (discussion_id + ".json"), json.dumps(member).encode("utf-8"))
            self.store.save(data)
            # save may capture an existing local plan/memory in another checkpoint.
            return self.append(self.load(identifier), reason)

    def observe(self, data):
        """Carry forward only NEW client-authored plan/memory versions, under the store lock."""
        identifier = data.get("project_id")
        if not identifier or data.get("phase") == "collect":
            return
        member = self.member(identifier, data["discussion_id"])
        project = self.load(identifier)
        changed = []
        plans = [m for m in data["messages"] if m["kind"] == "plan" and m.get("source") == "client"]
        if plans and plans[-1]["id"] > member.get("observed_plan", 0):
            m = plans[-1]
            project["plan"] = dict(
                text=m["content"], sources=[data["discussion_id"] + "#" + str(m["id"])]
            )
            member["observed_plan"] = m["id"]
            changed.append("共同方案")
        memories = data.get("memory_versions", [])
        if memories and memories[-1]["version"] > member.get("observed_memory", 0):
            memory = deepcopy(memories[-1]["memory"])
            for rows in memory.values():
                for row in rows:
                    row["sources"] = [data["discussion_id"] + "#" + str(i) for i in row["sources"]]
                    if row.get("participant") == "lead":
                        row["participant"] = "gpt"
            # Longer project-qualified source ids count toward the same prompt budget.
            project["memory"] = self.memory(identifier, memory)
            member["observed_memory"] = memories[-1]["version"]
            changed.append("任务记忆")
        if changed:
            self.append(
                project, "从关联讨论保存" + "、".join(changed) + "：" + data["discussion_id"]
            )
            atomic(
                self.members_folder(identifier) / (data["discussion_id"] + ".json"),
                json.dumps(member).encode("utf-8"),
            )

    def preflight(self, data):
        if not data.get("project_id") or not data.get("memory_versions"):
            return
        memory = deepcopy(data["memory_versions"][-1]["memory"])
        for rows in memory.values():
            for row in rows:
                row["sources"] = [data["discussion_id"] + "#" + str(i) for i in row["sources"]]
        if units(json.dumps(memory, ensure_ascii=False)) > 4500:
            raise ValueError(
                "Project-qualified memory sources exceed 4500 UTF-16 units; narrow explicitly before saving"
            )

    def views(self, identifier, discussions=None):
        result = {}
        for data in discussions if discussions is not None else self.discussions(identifier):
            for m in data["messages"]:
                if (
                    m.get("state", "success") != "success"
                    or m.get("source") != "model"
                    or data.get("phase") == "collect"
                ):
                    continue
                participant = m.get("participant", m["author"])
                stamp = m.get("finished_utc", m["time_utc"])
                if participant in result and result[participant]["time_utc"] > stamp:
                    continue
                result[participant] = {
                    k: m.get(k)
                    for k in (
                        "content",
                        "evidence",
                        "change_reason",
                        "question",
                        "channel",
                        "model_requested",
                        "run_id",
                    )
                }
                result[participant].update(
                    participant=participant,
                    source="model",
                    time_utc=stamp,
                    reference=data["discussion_id"] + "#" + str(m["id"]),
                )
        return result

    def read(
        self, identifier=None, *, version=None, query="", sources=None, limit=20, participant=None
    ):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        if identifier is None:
            if self.root.is_symlink():
                raise ValueError("Project symlinks are not served")
            return [self.load(p.name) for p in sorted(self.root.glob("*")) if p.is_dir()][:limit]
        project = self.load(identifier, version)
        discussions = self.discussions(identifier)
        result = dict(
            project=project,
            discussions=[
                dict(
                    discussion_id=d["discussion_id"],
                    goal=d["goal"],
                    closed=d["closed"],
                    model_attempts=d["model_attempts"],
                    on_demand=continuous(d),
                )
                for d in discussions
            ],
            participant_views=self.views(identifier, discussions),
            lifetime_attempts=sum(d["model_attempts"] for d in discussions),
            history=[
                dict(version=int(p.stem))
                for p in sorted(self.folder(identifier).glob("[0-9]*.json"), reverse=True)[:limit]
            ],
        )
        if participant is not None:
            if participant not in {"gpt", "gemini", "claude", "codex", "reviewer"}:
                raise ValueError("Invalid project participant")
            history = []
            for task in discussions:
                if task.get("phase") == "collect":
                    continue
                for message in task["messages"]:
                    author = message.get(
                        "participant", "gpt" if message["author"] == "lead" else message["author"]
                    )
                    if author != participant or message.get("state", "success") != "success":
                        continue
                    row = {
                        k: message.get(k)
                        for k in (
                            "source",
                            "content",
                            "evidence",
                            "change_reason",
                            "channel",
                            "model_requested",
                            "run_id",
                            "time_utc",
                        )
                    }
                    row["reference"] = task["discussion_id"] + "#" + str(message["id"])
                    history.append(row)
            result["participant_history"] = sorted(
                history, key=lambda m: (m["time_utc"], m["reference"]), reverse=True
            )[:limit]
        if sources is not None:
            result["sources"] = [
                dict(reference=r, message=m)
                for r, m in zip(sources, self.sources(identifier, sources))
            ]
        if query:
            query = checked(query, 200, "query").casefold()
            matches = []
            for d in reversed(discussions):
                for m in reversed(d["messages"]):
                    if d.get("phase") == "collect" and m["kind"] == "initial":
                        continue
                    if query in m["content"].casefold():
                        matches.append(
                            dict(
                                reference=d["discussion_id"] + "#" + str(m["id"]),
                                author=m["author"],
                                source=m.get("source"),
                                excerpt=clipped(m["content"], 800),
                            )
                        )
                        if len(matches) >= limit:
                            break
                if len(matches) >= limit:
                    break
            result["matches"] = matches
        return result

    def inject(self, data, material, manifest, participant, collecting=False):
        if not data.get("project_id"):
            return
        project = self.load(data["project_id"])
        self.ensure_active(data)
        memory = deepcopy(project["memory"])
        if collecting:
            memory["positions"] = []
            memory["disagreements"] = []
        material["persistent_project"] = dict(
            project_id=project["project_id"],
            version=project["version"],
            name=project["name"],
            goal=project["goal"],
            memory=memory,
            plan=deepcopy(project["plan"]),
        )
        discussions = self.discussions(project["project_id"])
        own = self.views(project["project_id"], discussions).get(participant)
        if own:
            material["project_own_last_view"] = own
            local = material.get("own_last_contribution")
            if local and own["reference"] == data["discussion_id"] + "#" + str(local["id"]):
                material.pop("own_last_contribution")
        local_plan = material.get("current_plan")
        if local_plan and project["plan"] and local_plan.get("content") == project["plan"]["text"]:
            # The full plan is present once, under the persistent project record.
            material["current_plan"] = {
                "id": local_plan["id"],
                "plan_version": local_plan.get("plan_version"),
                "full_text_in": "persistent_project.plan",
            }
        questions = []
        if not collecting:
            for task in discussions:
                if task.get("phase") == "collect":
                    continue
                questions.extend(
                    dict(
                        reference=task["discussion_id"] + "#" + str(q["message_id"]),
                        author=q["author"],
                        question=q["question"],
                    )
                    for q in task.get("open_questions", [])
                )
            material["project_open_questions"] = questions[:3]
            manifest["project_question_sources"] = [q["reference"] for q in questions[:3]]
            manifest["project_questions_omitted_count"] = max(0, len(questions) - 3)
        manifest.update(
            project_id=project["project_id"],
            project_version=project["version"],
            project_own_view_source=own["reference"] if own else None,
            project_initial_mode=(
                "shared-project-baseline; no peer positions" if collecting else None
            ),
        )
        compact_manifest(manifest)
        material["context"] = manifest
        material["project_continuity_rule"] = (
            "这是持续项目的资料与各自公开判断，不是全员共识或提供方私有记忆。"
            "接着当前进度推进；可以质疑旧前提。跨讨论来源用discussion_id#message_id注明，"
            "不要把它当作本场references的数字编号。不要求固定轮数，也不因次数达到某个值宣布项目结束。"
        )
