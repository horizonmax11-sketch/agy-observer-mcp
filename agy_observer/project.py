"""Project understanding and append-only, stage-specific role assignments.

The MCP client supplies material it has read. This module never reads arbitrary
project files or claims that a role assignment has been endorsed by peers.
"""

from copy import deepcopy
import json

from .discussions import checked, units
from .runtime import utc

PEERS = {"gpt", "gemini", "claude"}
BRIEF_FIELDS = {"stage", "materials", "success_criteria", "constraints", "unknowns"}
ROLE_FIELDS = {"role", "focus", "output", "reason"}


def brief(value):
    if not isinstance(value, dict) or set(value) != BRIEF_FIELDS:
        raise ValueError("project must contain stage, materials, success_criteria, constraints, unknowns")
    result = {key: checked(value[key], 2000, key) for key in sorted(BRIEF_FIELDS)}
    if units(json.dumps(result, ensure_ascii=False)) > 3000:
        raise ValueError("Project brief exceeds 3000 UTF-16 units; narrow the supplied material")
    return result


def assignments(value):
    if not isinstance(value, dict) or set(value) != PEERS:
        raise ValueError("Assignments must contain gpt, gemini and claude")
    result = {}
    for peer in sorted(PEERS):
        spec = value[peer]
        if not isinstance(spec, dict) or set(spec) != ROLE_FIELDS:
            raise ValueError("Each assignment needs role, focus, output and reason")
        result[peer] = {
            key: checked(spec[key], {"role": 80, "focus": 200, "output": 200, "reason": 300}[key], key)
            for key in sorted(ROLE_FIELDS)
        }
    if len({spec["role"].casefold() for spec in result.values()}) != 3:
        raise ValueError("Choose three distinct project roles")
    return result


def prompts(specs):
    return {
        peer: (
            f"本轮角色：{spec['role']}。优先关注：{spec['focus']}。有新发现时带来：{spec['output']}。"
            "可跨角色补充、质疑共同前提；给出具体依据或验证办法，没有补充可以pass。"
        )
        for peer, spec in specs.items()
    }


def version(data):
    return len(data.get("project", {}).get("role_versions", []))


def current_brief(data):
    project = data["project"]
    return project["role_versions"][-1]["brief"] if version(data) else project["brief"]


def stage(data):
    if data["phase"] == "collect":
        return "understand"
    return "work" if version(data) else "assign"


def validate(data):
    project = data["project"]
    if not isinstance(project, dict) or set(project) != {"brief", "role_versions"}:
        raise ValueError("Invalid project workflow")
    brief(project["brief"])
    versions = project["role_versions"]
    if not isinstance(versions, list) or len(versions) > 8:
        raise ValueError("Invalid role history")
    previous_id = 0
    for index, entry in enumerate(versions, 1):
        fields = {
            "version", "stage", "brief", "assignments", "rationale", "references", "message_id", "time_utc"
        }
        if not isinstance(entry, dict) or set(entry) not in (fields, fields | {"prompts"}):
            raise ValueError("Invalid role version")
        if type(entry["version"]) is not int or entry["version"] != index:
            raise ValueError("Invalid role version number")
        checked(entry["stage"], 300, "stage")
        checked(entry["rationale"], 1000, "rationale")
        checked(entry["time_utc"], 80, "time_utc")
        checked_brief = brief(entry["brief"])
        assignments(entry["assignments"])
        if "prompts" in entry:
            if not isinstance(entry["prompts"], dict) or set(entry["prompts"]) != PEERS:
                raise ValueError("Invalid role prompt snapshot")
            for value in entry["prompts"].values():
                checked(value, 600, "role prompt")
        identifier = entry["message_id"]
        if type(identifier) is not int or not previous_id < identifier <= len(data["messages"]):
            raise ValueError("Invalid role history message")
        message = data["messages"][identifier - 1]
        if (
            message.get("kind") != "roles"
            or message.get("author") != "gpt"
            or message.get("state") != "success"
            or message.get("role_version") != index
            or message.get("references") != entry["references"]
            or entry["stage"] != checked_brief["stage"]
            or not entry["references"]
        ):
            raise ValueError("Role history does not match its recorded contribution")
        previous_id = identifier
    if versions:
        last = versions[-1]
        saved = last["prompts"] if "prompts" in last else prompts(last["assignments"])
        if data["roles"] != saved:
            raise ValueError("Active role prompts do not match history")
    for message in data["messages"]:
        n = message.get("role_version")
        if type(n) is not int or not 0 <= n <= len(versions):
            raise ValueError("Invalid message role version")
        if n and versions[n - 1]["message_id"] > message["id"]:
            raise ValueError("Message references a future role assignment")
    if data["phase"] == "collect" and versions:
        raise ValueError("Roles cannot precede the independent project understanding")


class ProjectWorkflow:
    def __init__(self, council):
        self.council = council

    def assign(self, identifier, roles, stage_name, rationale, references, expected_version, updates=None):
        specs = assignments(roles)
        stage_name = checked(stage_name, 300, "stage")
        rationale = checked(rationale, 1000, "rationale")
        with self.council.store.writing():
            data = self.council.read(identifier)
            self.council.writable(data)
            if "project" not in data or data["phase"] != "discuss":
                raise ValueError("Reveal all three project understandings before assigning roles")
            old_version = version(data)
            if type(expected_version) is not int or expected_version != old_version:
                raise ValueError("Role version changed; read before revising")
            if old_version >= 8:
                raise ValueError("Role revision limit reached")
            # Reuse the council's earlier-message validation without circular imports.
            from .council import refs

            references = refs(data, references)
            if not references or any(data["messages"][i - 1]["state"] != "success" for i in references):
                raise ValueError("Role assignments need successful source contributions")
            if not old_version:
                authors = {
                    data["messages"][i - 1]["author"] for i in references
                    if data["messages"][i - 1]["kind"] == "initial"
                }
                if authors != PEERS:
                    raise ValueError("First assignment must draw on all three initial understandings")
            revised = deepcopy(current_brief(data))
            if updates is not None:
                if not isinstance(updates, dict) or not set(updates) <= BRIEF_FIELDS:
                    raise ValueError("Unknown project brief update field")
                revised.update(updates)
            revised["stage"] = stage_name
            revised = brief(revised)
            n = old_version + 1
            message_id = len(data["messages"]) + 1
            now = utc()
            entry = dict(
                version=n, stage=stage_name, brief=revised, assignments=specs,
                rationale=rationale, references=references, message_id=message_id, time_utc=now,
                prompts=prompts(specs),
            )
            data["project"]["role_versions"].append(entry)
            data["roles"] = deepcopy(entry["prompts"])
            content = f"阶段：{stage_name}\n" + "\n".join(
                f"{name}：{specs[p]['role']}；重点：{specs[p]['focus']}；产出：{specs[p]['output']}"
                for p, name in (("gpt", "GPT"), ("gemini", "Gemini"), ("claude", "Claude"))
            ) + "\n选择及调整原因：" + rationale
            data["messages"].append(dict(
                id=message_id, author="gpt", source="client", kind="roles", state="success",
                content=content, references=references, answers_to=[], role_version=n, time_utc=now,
            ))
            data["updated_utc"] = now
            self.council.store.save(data)
            from .council import public

            return public(data)
