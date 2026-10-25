"""Explicit runtime configuration; credential contents are never loaded by this package."""

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    executable: str = "agy"
    model: str = "gemini-3.8-flash-low"
    claude_model: str = "claude-sonnet-4-6"
    state_dir: Path = Path("state")
    workspace: Path | None = None
    enabled: bool = False
    open_browser: bool = True
    port: int = 0
    cli_timeout: int = 30
    outer_timeout: int = 40
    output_limit: int = 2 * 1024 * 1024

    def __post_init__(self):
        for model in (self.model, self.claude_model):
            if (
                not isinstance(model, str)
                or not model
                or model.startswith("-")
                or len(model) > 160
                or any(ord(c) < 32 for c in model)
            ):
                raise ValueError("Invalid model label")
        if self.model == self.claude_model:
            raise ValueError("Gemini and Claude must have distinct configured model identifiers")
        if not 0 <= self.port <= 65535:
            raise ValueError("Invalid monitor port")
        if (
            type(self.cli_timeout) is not int
            or type(self.outer_timeout) is not int
            or not 1 <= self.cli_timeout <= 180
            or not self.cli_timeout <= self.outer_timeout <= 210
        ):
            raise ValueError("Timeouts must remain within the 180s CLI / 210s outer ceiling")
        if not 1024 <= self.output_limit <= 2 * 1024 * 1024:
            raise ValueError("Output limit must be between 1KiB and 2MiB per stream")

    @classmethod
    def from_env(cls):
        state_value = os.environ.get("AGY_OBSERVER_STATE_DIR")
        if state_value:
            state_dir = Path(state_value)
        elif os.name == "nt":
            local = os.environ.get("LOCALAPPDATA")
            state_dir = (Path(local) if local else Path.home()) / "agy-observer"
        else:
            xdg = os.environ.get("XDG_STATE_HOME")
            state_dir = (Path(xdg) if xdg else Path.home() / ".local/state") / "agy-observer"
        workspace = os.environ.get("AGY_OBSERVER_WORKSPACE")
        cli_timeout = int(os.environ.get("AGY_OBSERVER_CLI_TIMEOUT", "30"))
        return cls(
            executable=os.environ.get("AGY_OBSERVER_CLI", "agy"),
            model=os.environ.get("AGY_OBSERVER_MODEL", "gemini-3.8-flash-low"),
            claude_model=os.environ.get("AGY_OBSERVER_CLAUDE_MODEL", "claude-sonnet-4-6"),
            state_dir=state_dir.expanduser().resolve(),
            workspace=Path(workspace).expanduser().resolve() if workspace else None,
            enabled=os.environ.get("AGY_OBSERVER_ENABLED") == "1",
            open_browser=os.environ.get("AGY_OBSERVER_OPEN_BROWSER", "1") != "0",
            port=int(os.environ.get("AGY_OBSERVER_PORT", "0")),
            cli_timeout=cli_timeout,
            outer_timeout=int(
                os.environ.get("AGY_OBSERVER_OUTER_TIMEOUT", str(max(40, cli_timeout + 10)))
            ),
        )

    def cli_environment(self):
        keep = {
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "DBUS_SESSION_BUS_ADDRESS",
            "XDG_RUNTIME_DIR",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        # These paths point to the user's existing CLI session; they are never scanned/copied.
        for suffix, target in [
            ("HOME", "HOME"),
            ("XDG_CONFIG_HOME", "XDG_CONFIG_HOME"),
            ("XDG_CACHE_HOME", "XDG_CACHE_HOME"),
            ("XDG_DATA_HOME", "XDG_DATA_HOME"),
            ("XDG_STATE_HOME", "XDG_STATE_HOME"),
        ]:
            value = os.environ.get("AGY_OBSERVER_CLI_" + suffix)
            if value:
                env[target] = value
        return env
