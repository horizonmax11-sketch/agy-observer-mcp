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
