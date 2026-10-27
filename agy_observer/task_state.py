"""Versioned client-maintained task memory and explicit attempt grants.

No model is called here. Memory is an attributed working record, never inferred
consensus. Mandatory context is sent whole or rejected before reserving a turn.
"""

from copy import deepcopy
import json

from .discussions import checked, units
from .runtime import utc

CONTEXT_LIMIT = 32000
MEMORY_LIMIT = 4500
ATTEMPT_LIMIT = 32
VERSION_LIMIT = 32
SECTIONS = {"facts", "disagreements", "positions", "next_steps"}


def limits(data):
    if "budget_limits" in data:
        if not isinstance(data["budget_limits"], dict):
            raise ValueError("Invalid participant budget limits")
        return dict(data["budget_limits"])
    if data.get("protocol") == "council-v1":
        return {p: data["max_turns_per_model"] for p in data["models"]}
    return {"reviewer": data["max_model_turns"]}


def version(data, key):
    return len(data.get(key + "_versions", []))


def successful(message):
    return message.get("state", "success") == "success"


def memory_spec(data, spec, through):
    if not isinstance(spec, dict) or set(spec) != SECTIONS:
        raise ValueError("memory needs facts, disagreements, positions and next_steps lists")
    result = {}
    roster = set(data["roles"])
    for section in sorted(SECTIONS):
        rows = spec[section]
        if not isinstance(rows, list) or len(rows) > 8:
            raise ValueError("Each memory section accepts at most 8 entries")
        result[section] = []
        for row in rows:
            fields = (
                {"participant", "view", "reason", "sources"}
                if section == "positions"
                else {"text", "sources"}
            )
            if not isinstance(row, dict) or set(row) != fields:
                raise ValueError("Invalid memory entry fields")
            sources = row["sources"]
            if (
                not isinstance(sources, list)
                or not 1 <= len(sources) <= 12
                or any(type(i) is not int or not 1 <= i <= through for i in sources)
                or len(set(sources)) != len(sources)
                or any(not successful(data["messages"][i - 1]) for i in sources)
            ):
                raise ValueError("Memory sources must reference existing successful messages")
            entry = {"sources": list(sources)}
            if section == "positions":
                if row["participant"] not in roster or not any(
                    data["messages"][i - 1]["author"] == row["participant"] for i in sources
                ):
                    raise ValueError("A position needs a source from that participant")
                entry.update(
                    participant=row["participant"],
                    view=checked(row["view"], 1000, "view"),
                    reason=checked(row["reason"], 1000, "reason"),
                )
            else:
                entry["text"] = checked(row["text"], 1500, "memory text")
            result[section].append(entry)
    if units(json.dumps(result, ensure_ascii=False)) > MEMORY_LIMIT:
        raise ValueError("Task memory exceeds 4500 UTF-16 units; narrow it explicitly")
    return result


def validate(data):
    if data.get("protocol") == "council-v1" and not isinstance(data.get("models"), dict):
        raise ValueError("Invalid council models")
    memories = data.get("memory_versions", [])
    if not isinstance(memories, list) or (
        not data.get("project_id") and len(memories) > VERSION_LIMIT
    ):
        raise ValueError("Invalid memory history")
    previous = 0
    for index, entry in enumerate(memories, 1):
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"version", "through_message_id", "source", "author", "reason", "memory", "time_utc"}
            or type(entry["version"]) is not int
            or entry["version"] != index
            or entry["source"] != "client"
            or entry["author"] != "gpt"
            or type(entry["through_message_id"]) is not int
            or not previous <= entry["through_message_id"] <= len(data["messages"])
        ):
            raise ValueError("Invalid memory version")
        previous = entry["through_message_id"]
        checked(entry["reason"], 1000, "memory reason")
        checked(entry["time_utc"], 80, "time_utc")
        memory_spec(data, entry["memory"], previous)
    if memories and data.get("phase") == "collect":
        raise ValueError("Task memory is unavailable before initial reveal")
    budgets = data.get("budget_versions", [])
    if not isinstance(budgets, list) or len(budgets) > VERSION_LIMIT:
        raise ValueError("Invalid budget history")
    current = limits(data)
    roster = set(data["models"]) if data.get("protocol") == "council-v1" else {"reviewer"}
    if set(current) != roster or any(
        type(v) is not int or not 1 <= v <= ATTEMPT_LIMIT for v in current.values()
    ):
        raise ValueError("Invalid participant budget limits")
    prior = None
    for index, entry in enumerate(budgets, 1):
        if (
            not isinstance(entry, dict)
            or set(entry)
            != {"version", "source", "reason", "before", "after", "additions", "time_utc"}
            or type(entry["version"]) is not int
            or entry["version"] != index
            or entry["source"] != "client"
        ):
            raise ValueError("Invalid budget version")
        before, after, additions = entry["before"], entry["after"], entry["additions"]
        if (
            not isinstance(before, dict)
            or not isinstance(after, dict)
            or set(before) != roster
            or set(after) != roster
            or not isinstance(additions, dict)
            or not additions
            or not set(additions) <= roster
            or any(type(n) is not int or not 1 <= n <= 8 for n in additions.values())
            or any(type(n) is not int or not 1 <= n <= ATTEMPT_LIMIT for n in before.values())
            or any(type(n) is not int or not 1 <= n <= ATTEMPT_LIMIT for n in after.values())
            or after != {p: before[p] + additions.get(p, 0) for p in roster}
            or (prior is not None and before != prior)
        ):
            raise ValueError("Invalid budget grant chain")
        if index == 1 and any(n > 8 for n in before.values()):
            raise ValueError("The initial budget must not exceed 8 attempts")
        checked(entry["reason"], 1000, "budget reason")
        checked(entry["time_utc"], 80, "time_utc")
        prior = after
    if (bool(budgets) != ("budget_limits" in data)) or (budgets and prior != current):
        raise ValueError("Budget limits must match their audit history")
    if data.get("max_model_turns") != sum(current.values()):
        raise ValueError("Total budget differs from participant limits")


class TaskState:
    def __init__(self, store):
        self.store = store

    def writable(self, data, key, expected):
        self.store.writable(data)
        if any(m.get("state") == "running" for m in data["messages"]):
            raise ValueError("A model call is pending; wait or recover before updating task state")
        if type(expected) is not int or expected != version(data, key):
            raise ValueError(f"{key} version changed; read before updating")
        if not data.get("project_id") and expected >= VERSION_LIMIT:
            raise ValueError(f"{key} history limit reached")

    def memory(self, identifier, spec, expected, reason):
        reason = checked(reason, 1000, "memory reason")
        with self.store.writing():
            data = self.store.read(identifier)
            self.writable(data, "memory", expected)
            if data.get("phase") == "collect":
                raise ValueError("Reveal independent initial judgments before maintaining memory")
            entry = dict(
                version=expected + 1,
                through_message_id=len(data["messages"]),
                source="client",
                author="gpt",
                reason=reason,
                memory=memory_spec(data, spec, len(data["messages"])),
                time_utc=utc(),
            )
            data.setdefault("memory_versions", []).append(entry)
            validate(data)
            data["updated_utc"] = utc()
            self.store.save(data)
            return deepcopy(entry)

    def extend(self, identifier, additions, expected, reason):
        reason = checked(reason, 1000, "budget reason")
        with self.store.writing():
            data = self.store.read(identifier)
            self.writable(data, "budget", expected)
            from .projects import continuous

            if continuous(data):
                raise ValueError(
                    "This project discussion is on demand; no additional round budget is needed"
                )
            before = limits(data)
            if (
                not isinstance(additions, dict)
                or not additions
                or not set(additions) <= set(before)
                or any(type(n) is not int or not 1 <= n <= 8 for n in additions.values())
            ):
                raise ValueError(
                    "Choose task remotes and add 1..8 attempts each; client GPT has no external budget"
                )
            after = {p: n + additions.get(p, 0) for p, n in before.items()}
            if any(n > ATTEMPT_LIMIT for n in after.values()):
                raise ValueError("A participant cannot exceed 32 total attempts")
            entry = dict(
                version=expected + 1,
                source="client",
                reason=reason,
                before=before,
                after=after,
                additions=dict(additions),
                time_utc=utc(),
            )
            data.setdefault("budget_versions", []).append(entry)
            data["budget_limits"] = after
            data["max_model_turns"] = sum(after.values())
            if data.get("protocol") == "council-v1":
                data["max_turns_per_model"] = max(after.values())
            validate(data)
            data["updated_utc"] = utc()
            self.store.save(data)
            return deepcopy(entry)


def attach(data, material, manifest, participant):
    """Pin unabridged task state; do not silently turn old opinions into facts."""
    memories = data.get("memory_versions", [])
    current = memories[-1] if memories else None
    material["task_memory"] = deepcopy(current)
    selected_questions = set(manifest.get("question_ids_included", []))
    material["open_questions"] = [
        deepcopy(q) for q in data.get("open_questions", []) if q["message_id"] in selected_questions
    ]
    manifest["memory_version"] = current["version"] if current else 0
    manifest["budget_version"] = version(data, "budget")
    manifest["memory_source"] = "client-maintained; not independent verification or consensus"
    pinned = set()
    plans = [m for m in data["messages"] if m["kind"] == "plan" and successful(m)]
    plan = plans[-1] if plans else None
    material["current_plan"] = deepcopy(plan) if plan else None
    if plan:
        # Context fields on a plan are unnecessary; a client plan is already a complete record.
        material["current_plan"].pop("context", None)
        pinned.add(plan["id"])
    own = next(
        (m for m in reversed(data["messages"]) if m["author"] == participant and successful(m)),
        None,
    )
    if own:
        material["own_last_contribution"] = {
            k: own.get(k)
            for k in (
                "id",
                "author",
                "content",
                "evidence",
                "change_reason",
                "question",
                "references",
            )
        }
        pinned.add(own["id"])
    manifest["pinned_ids"] = sorted(pinned)
    manifest["memory_source_ids"] = (
        sorted({i for rows in current["memory"].values() for row in rows for i in row["sources"]})
        if current
        else []
    )
    manifest["plan_version"] = data.get("plan_version", 0)
    manifest["full_plan_included"] = bool(plan)
    # Only texts actually sent as full messages are valid response references.
    if plan or own:
        full = {m["id"] for m in (plan, own) if m}
        manifest["included_ids"] = sorted(set(manifest["included_ids"]) | full)
        manifest["omitted_ids"] = [i for i in manifest["omitted_ids"] if i not in full]
        manifest["truncated_ids"] = [i for i in manifest.get("truncated_ids", []) if i not in full]
    material["memory_usage"] = (
        "任务记忆是客户端有来源的整理稿，不是全员共识或已独立核验。保持不同判断与理由，"
        "发现记忆过时请明确指出。当前方案和自己的上一条成功发言完整附带；其他历史可能省略。"
    )
    material["context"] = manifest
