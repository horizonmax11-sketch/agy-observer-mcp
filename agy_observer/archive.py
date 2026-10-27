"""Content-addressed pages for project discussions; publish the head last.

Every page is bounded and verified. Older pages stay available; readers of an
old head cannot observe partial changes to a page already referenced by it.
"""

from copy import deepcopy
import hashlib
import json
import os
import re
import uuid

from .runtime import strict_json
from .storage import retry_permission

LIMIT = 2 * 1024 * 1024


def bounded(path):
    if path.is_symlink():
        raise ValueError("Archive symlinks are not served")
    with path.open("rb") as source:
        raw = source.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Archive page exceeds 2MiB")
    return raw


def atomic(path, raw):
    if path.is_symlink():
        raise ValueError("Snapshot symlinks are not supported")
    if len(raw) > LIMIT:
        raise ValueError("Snapshot exceeds 2MiB")
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as output:
            output.write(raw)
        retry_permission(lambda: os.replace(temp, path))
    finally:
        temp.unlink(missing_ok=True)


def directory(head):
    root = head.with_suffix(".pages")
    if root.is_symlink():
        raise ValueError("Archive directory symlinks are not supported")
    return root


def pack(data, head):
    result = deepcopy(data)
    root = directory(head)
    root.mkdir(exist_ok=True)
    if head.exists() and not (root / "before-project.json").exists():
        previous = bounded(head)
        # This is an exact local backup, never a new external model result.
        with (root / "before-project.json").open("xb") as output:
            output.write(previous)
    groups = {
        "messages": result.pop("messages"),
        "memory_versions": result.pop("memory_versions", []),
        "budget_versions": result.pop("budget_versions", []),
    }
    if "project" in result:
        groups["role_versions"] = result["project"].pop("role_versions")
    index = {}
    for name, rows in groups.items():
        pages, batch = [], []

        def write_page():
            if not batch:
                return
            raw = json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            path = root / (digest + ".json")
            if path.exists():
                if bounded(path) != raw:
                    raise ValueError("Archive page digest mismatch")
            else:
                atomic(path, raw)
            pages.append(dict(sha256=digest, count=len(batch)))

        for row in rows:
            candidate = json.dumps(batch + [row], ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            if len(batch) >= 50 or len(candidate) > LIMIT:
                write_page()
                batch = []
            batch.append(row)
        write_page()
        index[name] = pages
    # Pending questions are derived from the immutable messages, not duplicated
    # indefinitely in the head document.
    result.pop("open_questions", None)
    result["archive"] = dict(format="project-pages-v1", groups=index)
    return result


def unpack(data, head):
    if "archive" not in data:
        return data
    index = data.pop("archive")
    if (
        not data.get("project_id")
        or not isinstance(index, dict)
        or set(index) != {"format", "groups"}
        or index["format"] != "project-pages-v1"
        or not isinstance(index["groups"], dict)
        or set(index["groups"])
        not in (
            {"messages", "memory_versions", "budget_versions"},
            {"messages", "memory_versions", "budget_versions", "role_versions"},
        )
    ):
        raise ValueError("Invalid project archive index")
    root = directory(head)
    for name, pages in index["groups"].items():
        if not isinstance(pages, list):
            raise ValueError("Invalid archive pages")
        rows = []
        for page in pages:
            if (
                not isinstance(page, dict)
                or set(page) != {"sha256", "count"}
                or not isinstance(page["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", page["sha256"])
                or type(page["count"]) is not int
                or not 1 <= page["count"] <= 50
            ):
                raise ValueError("Invalid archive page descriptor")
            raw = bounded(root / (page["sha256"] + ".json"))
            if hashlib.sha256(raw).hexdigest() != page["sha256"]:
                raise ValueError("Archive page digest mismatch")
            items = strict_json(raw.decode("utf-8"))
            if not isinstance(items, list) or len(items) != page["count"]:
                raise ValueError("Archive page count mismatch")
            rows.extend(items)
        if name == "role_versions":
            if not isinstance(data.get("project"), dict):
                raise ValueError("Missing archived project roles")
            data["project"][name] = rows
        else:
            data[name] = rows
    return data


def page_view(data, offset=None, limit=100):
    """Bound public responses while retaining original, stable message ids."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("message_limit must be between 1 and 200")
    total = len(data["messages"])
    start = max(0, total - limit) if offset is None else offset
    if type(start) is not int or not 0 <= start <= total:
        raise ValueError("Invalid message offset")
    result = dict(data)
    plans = [m for m in data["messages"] if m["kind"] == "plan"]
    result["current_plan"] = deepcopy(plans[-1]) if plans else None
    for key in ("memory", "budget"):
        versions = data.get(key + "_versions", [])
        result[key + "_version"] = len(versions)
        if len(versions) > 20:
            result[key + "_versions"] = versions[-20:]
            result[key + "_history_omitted"] = len(versions) - 20
    result["messages"] = data["messages"][start : start + limit]
    result["message_page"] = dict(
        offset=start,
        limit=limit,
        total=total,
        previous_offset=max(0, start - limit) if start else None,
        next_offset=start + limit if start + limit < total else None,
    )
    return result
