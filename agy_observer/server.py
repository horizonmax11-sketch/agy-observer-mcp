"""STDIO MCP entry point. Application logs must never be printed to stdout."""

from contextlib import asynccontextmanager
import os
import shutil
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .config import Settings
from .service import Service
from .council import public


def create_server(settings=None):
    service = Service(settings or Settings.from_env())

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield {}
        finally:
            await service.close()

    app = FastMCP("agy-observer-mcp", lifespan=lifespan)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        )
    )
    async def agy_run(
        prompt: str,
        participant: str = "gemini",
        channel: str | None = None,
        model: str | None = None,
    ) -> dict:
        """Send one authorized request. participant: gemini/claude/codex (never client gpt); channel: antigravity/claude_code/codex_cli as supported. Default Gemini via Antigravity stays compatible. A channel override needs its explicit model; Claude requires Opus. Missing CLI/login fails without fallback/retry. May incur cost."""
        return await service.run(prompt, participant=participant, channel=channel, model=model)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_history(limit: int = 20, include_tests: bool = False) -> list[dict]:
        """List local history summaries and task previews. Use agy_status for full prompts and replies."""
        return service.history.list(limit, include_tests)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_status(run_id: str) -> dict:
        """Read one run's current or terminal status from the configured artifact directory."""
        return service.history.status(run_id)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    async def agy_cancel(run_id: str) -> dict:
        """Request cancellation of an active run owned by this server instance."""
        return service.cancel(run_id)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_discussion_create(
        goal: str,
        lead_brief: str = "共同提出方案、追问、反驳与补证；负责可执行修改与测试，明确修正自己的判断。",
        reviewer_brief: str = "共同提出方案、追问、反驳与补证；发挥独立推理优势，明确修正自己的判断。",
        max_model_turns: int = 4,
    ) -> dict:
        """Create a local peer collaboration task with complementary duties and 1..8 attempt budget. No model call."""
        return service.discussions.create(goal, lead_brief, reviewer_brief, max_model_turns)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_discussion_read(discussion_id: str | None = None, limit: int = 20) -> dict | list[dict]:
        """List task summaries when discussion_id is omitted; otherwise read roles, attributed messages, decisions, memory_versions, budget_versions, context manifests and linked runs. Memory/budget expected version is the history length (0 initially). No model call."""
        return (
            public(service.discussions.read(discussion_id))
            if discussion_id
            else service.discussions.list(limit)
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_discussion_memory(
        discussion_id: str,
        memory: dict,
        expected_memory_version: int,
        reason: str,
    ) -> dict:
        """Replace the client-maintained working memory with a versioned snapshot, not inferred consensus. No model call. Read first; expected version is 0 initially. memory has facts/disagreements/next_steps lists of {text,sources:[message ids]}, and positions list of {participant,view,reason,sources}. Each source must be a successful message; each position needs that participant's own source. At most 8 rows/section and 4500 UTF-16 units total. Keep disagreements and reasons, never invent verification. Full snapshot and current plan enter later discussion prompts; initial judgments stay isolated. Pending/closed tasks reject edits."""
        from .task_state import TaskState

        return TaskState(service.discussions).memory(
            discussion_id, memory, expected_memory_version, reason
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_discussion_extend(
        discussion_id: str,
        additions: dict[str, int],
        expected_budget_version: int,
        reason: str,
    ) -> dict:
        """Explicitly add 1..8 attempts to selected task remotes, at most 32 total per remote. Council keys are selected gemini/claude/codex; peer tasks use reviewer. Read first; expected version is 0 initially. Records before/after limits and reason; preserves spent attempts, roles, routes, memory and plan versions. Does not call/retry models, reopen archived tasks or start a loop. Failure/cancellation still consumes an attempt. Pending calls block changes; initial budget defaults stay unchanged."""
        from .task_state import TaskState

        return TaskState(service.discussions).extend(
            discussion_id, additions, expected_budget_version, reason
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_discussion_note(
        discussion_id: str,
        content: str,
        kind: str = "feedback",
        reply_to: int | None = None,
        resolution: str | None = None,
    ) -> dict:
        """Submit a client proposal/question/answer/challenge/revision/feedback/decision/summary. An answer must reference the peer question. Decisions require reply_to and accepted/rejected/deferred; summary closes the task. Client authorship is not authenticated. No model call."""
        return service.discussions.note(discussion_id, content, kind, reply_to, resolution)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        )
    )
    async def agy_discussion_turn(
        discussion_id: str,
        instruction: str = "",
        reply_to: int | None = None,
        participant: str = "gemini",
        channel: str | None = None,
        model: str | None = None,
    ) -> dict:
        """Yield ONE authorized peer turn using bounded shared context. Instruction is optional; the peer may initiate questions or proposals, including the first turn. Failure/cancellation consumes an attempt; no automatic turns/retries. May incur cost and CLI side effects."""
        return await service.discussion_turn(
            discussion_id, instruction, reply_to, participant, channel, model
        )

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    def agy_discussion_recover(discussion_id: str) -> dict:
        """Reconcile interrupted messages or failed delivery with linked terminal runs. A failed message is restored only from a verified matching successful run, retaining recovery audit. Never retries models or refunds attempts."""
        return service.discussion_recover(discussion_id)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_council_create(
        goal: str,
        facts: str = "",
        max_turns_per_model: int = 2,
        roles: dict[str, str] | None = None,
        project: dict[str, str] | None = None,
        participants: list[str] | None = None,
        routes: dict[str, dict[str, str]] | None = None,
    ) -> dict:
        """Create a task with 2..4 selected participants including client gpt; defaults remain gpt/gemini/claude. Optional codex is an independent Codex CLI participant. routes maps each remote participant to {channel, model}; Claude requires Opus. Snapshots prevent implicit route/model changes. Each remote starts with 1..8 attempts (default 2, including initial judgment); agy_discussion_extend can explicitly add more without a model call. Prefer project for new projects: client first READS relevant files/docs, supplies stage/materials (source + observed contents)/success_criteria/constraints/unknowns, total <=3000 UTF-16 units. Collect all selected project understandings, reveal, then select roles with agy_council_assign_roles. Do not also preassign roles. Without project, legacy roles map remains supported. No model call or arbitrary project file reading."""
        return service.create_council(
            goal,
            facts,
            max_turns_per_model,
            roles=roles,
            project=project,
            participants=participants,
            routes=routes,
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_council_assign_roles(
        discussion_id: str,
        assignments: dict[str, dict[str, str]],
        stage: str,
        rationale: str,
        references: list[int],
        expected_role_version: int,
        project_updates: dict[str, str] | None = None,
    ) -> dict:
        """After project understanding/reveal, choose complementary roles for this task's selected participants (gpt is client, codex is external), then assign them. Each spec needs role (<=80), focus/output (<=200 each), reason (<=300 UTF-16 units). Initial sources must include all selected initial understandings. Explain choice/tradeoffs in rationale, preserve unresolved differences. No peer endorsement is inferred. Revision needs current expected_role_version, sources and rationale; may update project brief fields. Appends history and generates role prompts, never rewrites old contributions or budgets. Client applies GPT prompt; no new GPT call, model call or automatic turn."""
        from .project import ProjectWorkflow

        return ProjectWorkflow(service.council).assign(
            discussion_id,
            assignments,
            stage,
            rationale,
            references,
            expected_role_version,
            project_updates,
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    )
    def agy_council_note(
        discussion_id: str,
        content: str,
        kind: str = "observation",
        references: list[int] | None = None,
        evidence: str = "",
        change_reason: str = "",
        question: str | None = None,
        answers_to: list[int] | None = None,
        initial_context: str = "history_visible",
        expected_plan_version: int | None = None,
    ) -> dict:
        """Submit GPT/client public contribution. During collect use initial with honest context declaration (not authenticated). Later revision needs reason+references; support needs grounds+references; plan needs sources+expected version; summary archives without consensus. No model call."""
        return service.council.note(
            discussion_id,
            content,
            kind,
            references,
            evidence,
            change_reason,
            question,
            answers_to,
            initial_context,
            expected_plan_version,
        )

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
    )
    async def agy_council_round(
        discussion_id: str,
        participants: list[str] | None = None,
        focus: str = "",
        reply_to: int | None = None,
    ) -> dict:
        """Explicitly call up to two selected remote participants via their stored channels (default first two task remotes). No call is made for client gpt. Codex CLI may be selected independently. Separate attempt budgets; no retries/loop. Collect uses only original material, disallows leading focus, and keeps replies sealed. Later batch shares a pre-call snapshot with full current plan, current task memory and each participant's own last successful contribution. Recent history/questions remain bounded; consult context manifests. May incur cost; CLI tools are not forcibly disabled."""
        return await service.council_round(discussion_id, participants, focus, reply_to)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    def agy_council_reveal(discussion_id: str) -> dict:
        """Publish all three successful initial judgments together and enable shared discussion. No model call. Sealing is workflow isolation, not a local access-control boundary."""
        return service.council.reveal(discussion_id)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_info() -> dict:
        """Report non-secret configuration, version, and execution gate; does not inspect CLI authentication."""
        participants = {}
        for peer in ("gemini", "claude", "codex"):
            try:
                endpoint = service.settings.endpoint(peer)
                executable = shutil.which(service.settings.channel_executable(endpoint.channel))
                participants[peer] = dict(
                    **endpoint.public(),
                    cli_available=bool(executable)
                    and not (os.name == "posix" and executable.lower().endswith(".exe")),
                )
            except ValueError as error:
                participants[peer] = dict(configuration_error=str(error))
        return dict(
            participants=participants,
            channels={
                channel: dict(
                    cli_available=bool(shutil.which(service.settings.channel_executable(channel))),
                    authentication_checked=False,
                )
                for channel in ("antigravity", "claude_code", "codex_cli")
            },
            client_source="gpt",
            authentication_checked=False,
            version=__version__,
            execution_enabled=service.settings.enabled,
            model_requested=service.settings.model,
            council_models={
                "gemini": participants["gemini"].get("model", service.settings.model),
                "claude": participants["claude"].get("model", service.settings.claude_model),
            },
            max_concurrent_council_models=2,
            monitor_url=service.monitor.url,
            cli_timeout=service.settings.cli_timeout,
            outer_timeout=service.settings.outer_timeout,
            output_limit_per_stream=service.settings.output_limit,
        )

    return app


def main():
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
