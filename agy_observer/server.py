"""STDIO MCP entry point. Application logs must never be printed to stdout."""

from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__
from .config import Settings
from .service import Service


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
    async def agy_run(prompt: str) -> dict:
        """Send one authorized prompt to the configured CLI model; may incur cost and allow CLI tools. No retries."""
        return await service.run(prompt)

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
        lead_brief: str = "负责方案、实现、测试和最终取舍；逐条记录对审阅意见的决定。",
        reviewer_brief: str = "审阅证据、查找缺陷、质疑假设，逐点回应反馈并说明未验证项。",
        max_model_turns: int = 4,
    ) -> dict:
        """Create a local collaboration task with explicit lead/reviewer duties and 1..8 attempt budget. No model call."""
        return service.discussions.create(goal, lead_brief, reviewer_brief, max_model_turns)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_discussion_read(discussion_id: str | None = None, limit: int = 20) -> dict | list[dict]:
        """List task summaries when discussion_id is omitted; otherwise read roles, attributed messages, decisions, context manifests and linked runs. No model call."""
        return (
            service.discussions.read(discussion_id)
            if discussion_id
            else service.discussions.list(limit)
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
        """Submit a lead proposal/feedback/decision/summary. Decisions require reply_to and accepted/rejected/deferred; summary closes the task. Client authorship is not authenticated. No model call."""
        return service.discussions.note(discussion_id, content, kind, reply_to, resolution)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        )
    )
    async def agy_discussion_turn(
        discussion_id: str, instruction: str, reply_to: int | None = None
    ) -> dict:
        """Request ONE authorized reviewer call using bounded shared context. Failure/cancellation consumes an attempt; no automatic turns/retries. May incur cost and CLI side effects."""
        return await service.discussion_turn(discussion_id, instruction, reply_to)

    @app.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        )
    )
    def agy_discussion_recover(discussion_id: str) -> dict:
        """Reconcile an interrupted review with its terminal run; refuses active/nonterminal runs. Never retries or refunds attempts."""
        return service.discussion_recover(discussion_id)

    @app.tool(
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    )
    def agy_info() -> dict:
        """Report non-secret configuration, version, and execution gate; does not inspect CLI authentication."""
        return dict(
            version=__version__,
            execution_enabled=service.settings.enabled,
            model_requested=service.settings.model,
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
