from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import console as console_module
from . import mods as mods_module
from . import logs as logs_module
from . import server as server_module
from .config import (
    DEFAULT_CONFIG_FILENAME,
    ConfigError,
    MscConfig,
    USER_CONFIG_PATH,
    load_config,
    load_user_config,
    save_user_config,
)
from .mods import ManifestError

app = typer.Typer(help="Minecraft Server CLI (msc)")
console_app = typer.Typer(help="Send commands to the Minecraft server console")
logs_app = typer.Typer(help="Inspect Minecraft server logs")
server_app = typer.Typer(help="Control server lifecycle via docker compose")
quick_app = typer.Typer(help="Handy shortcuts for common server commands")
mods_app = typer.Typer(help="Manage server mods and manifest entries")
user_config_app = typer.Typer(help="Manage user-level defaults")

app.add_typer(console_app, name="console")
app.add_typer(logs_app, name="logs")
app.add_typer(server_app, name="server")
app.add_typer(quick_app, name="quick")
app.add_typer(mods_app, name="mods")
app.add_typer(user_config_app, name="config")

_rich_console = Console()


def _fail(message: str, code: int = 1) -> None:
    typer.secho(message, err=True, fg="red")
    raise typer.Exit(code=code)


def _load_or_exit(root: Path | None = None) -> MscConfig:
    try:
        return load_config(root=root)
    except ConfigError as exc:
        _fail(str(exc), code=2)


def _get_config(ctx: typer.Context) -> MscConfig:
    if ctx.obj is None:
        ctx.obj = {}
    cfg = ctx.obj.get("config")
    if cfg is None:
        cfg = _load_or_exit()
        ctx.obj["config"] = cfg
    return cfg


def _load_user_config_or_exit():
    try:
        return load_user_config()
    except ConfigError as exc:
        _fail(str(exc), code=2)


@user_config_app.command("show")
def user_config_show():
    """Display the user-level defaults stored under ~/.config."""
    cfg = _load_user_config_or_exit()
    table = Table(title="User config", box=box.MINIMAL_DOUBLE_HEAD)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Server root", str(cfg.server_root) if cfg.server_root else "(not set)")
    table.add_row("File", str(USER_CONFIG_PATH))
    _rich_console.print(table)


@user_config_app.command("set-root")
def user_config_set_root(
    path: Path = typer.Argument(..., help="Path to your Minecraft server root (contains .msc.json)"),
):
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        _fail(f"{resolved} does not exist.")
    if not resolved.is_dir():
        _fail(f"{resolved} is not a directory.")
    config_file = resolved / DEFAULT_CONFIG_FILENAME
    if not config_file.exists():
        typer.secho(
            f"Warning: {config_file} does not exist yet. Commands may fail until it's created.",
            fg="yellow",
        )
    cfg = _load_user_config_or_exit()
    cfg.server_root = resolved
    save_user_config(cfg)
    typer.secho(f"Default server root set to {resolved}", fg="green")
    typer.secho(f"Saved to {USER_CONFIG_PATH}", fg="cyan")


@user_config_app.command("clear-root")
def user_config_clear_root():
    cfg = _load_user_config_or_exit()
    cfg.server_root = None
    save_user_config(cfg)
    typer.secho("Cleared stored server root.", fg="yellow")


@app.callback()
def main(
    ctx: typer.Context,
    root: Path = typer.Option(None, "--root", help="Optional explicit server root directory"),
):
    ctx.obj = ctx.obj or {}
    if root is not None:
        ctx.obj["config"] = _load_or_exit(root=root)


def _send_rcon_command(ctx: typer.Context, command: str) -> str:
    cfg = _get_config(ctx)
    try:
        response = console_module.send_command(cfg, command)
    except RuntimeError as exc:
        _fail(str(exc))
    return response.strip()


def _print_rcon_response(output: str) -> None:
    if not output:
        typer.secho("(no response)", fg="bright_black")
    else:
        typer.echo(output)


def _ensure_server_stopped(cfg: MscConfig, *, force: bool, action: str) -> None:
    if force:
        typer.secho("Force flag supplied; skipping running-server check.", fg="yellow")
        return
    status = server_module.get_status(cfg)
    if status.running:
        _fail(
            f"Cannot {action} while the server is running. Stop it first or pass --force.",
            code=3,
        )


class GameMode(str, Enum):
    survival = "survival"
    creative = "creative"
    adventure = "adventure"
    spectator = "spectator"


class WeatherType(str, Enum):
    clear = "clear"
    rain = "rain"
    thunder = "thunder"


class Difficulty(str, Enum):
    peaceful = "peaceful"
    easy = "easy"
    normal = "normal"
    hard = "hard"


@app.command("status")
def status_command(ctx: typer.Context):
    """Show the current server status."""
    cfg = _get_config(ctx)
    status = server_module.get_status(cfg)

    table = Table(title="Minecraft Server Status", box=box.MINIMAL_DOUBLE_HEAD)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Name", Text(cfg.name, style="bold cyan"))
    table.add_row("Type", Text(f"{cfg.server_type} {cfg.minecraft_version}", style="magenta"))
    table.add_row("Docker service", Text(cfg.docker_service, style="cyan"))
    table.add_row("Data dir", Text(str(cfg.data_dir), style="dim"))
    table.add_row("Log file", Text(str(cfg.log_file), style="dim"))
    running_value = Text("Yes", style="bold green") if status.running else Text("No", style="bold red")
    table.add_row("Running", running_value)
    if status.running:
        table.add_row("Container ID", Text(status.container_id or "?", style="cyan"))
        table.add_row("Uptime", Text(status.uptime or "?", style="green"))
    _rich_console.print(table)


@console_app.command("run")
def console_run(ctx: typer.Context, command: str = typer.Argument(..., help="Command to send")):
    """Send a one-off command over RCON."""
    output = _send_rcon_command(ctx, command)
    _print_rcon_response(output)


@console_app.command("attach")
def console_attach(ctx: typer.Context):
    """Attach to the live Minecraft server console via docker."""
    cfg = _get_config(ctx)
    typer.secho("Attaching to container console (detach with CTRL-P CTRL-Q)...", fg="cyan")
    try:
        server_module.attach_console(cfg)
    except RuntimeError as exc:
        _fail(str(exc))


@quick_app.command("say")
def quick_say(ctx: typer.Context, message: str = typer.Argument(..., help="Message to broadcast")):
    """Broadcast a chat message."""
    _print_rcon_response(_send_rcon_command(ctx, f"say {message}"))


@quick_app.command("kick")
def quick_kick(
    ctx: typer.Context,
    player: str = typer.Argument(..., help="Player to kick"),
    reason: str = typer.Option(None, "--reason", "-r", help="Optional reason"),
):
    command = f"kick {player}"
    if reason:
        command += f" {reason}"
    _print_rcon_response(_send_rcon_command(ctx, command))


@quick_app.command("op")
def quick_op(ctx: typer.Context, player: str = typer.Argument(..., help="Player to op")):
    _print_rcon_response(_send_rcon_command(ctx, f"op {player}"))


@quick_app.command("deop")
def quick_deop(ctx: typer.Context, player: str = typer.Argument(..., help="Player to deop")):
    _print_rcon_response(_send_rcon_command(ctx, f"deop {player}"))


@quick_app.command("gamemode")
def quick_gamemode(
    ctx: typer.Context,
    mode: GameMode = typer.Argument(..., case_sensitive=False),
    target: str = typer.Argument("@s", help="Target selector or player (default: @s)"),
):
    _print_rcon_response(_send_rcon_command(ctx, f"gamemode {mode.value} {target}"))


@quick_app.command("difficulty")
def quick_difficulty(ctx: typer.Context, difficulty: Difficulty = typer.Argument(..., case_sensitive=False)):
    _print_rcon_response(_send_rcon_command(ctx, f"difficulty {difficulty.value}"))


@quick_app.command("weather")
def quick_weather(
    ctx: typer.Context,
    weather: WeatherType = typer.Argument(..., case_sensitive=False),
    duration: int = typer.Option(None, "--duration", "-d", help="Optional duration in seconds"),
):
    command = f"weather {weather.value}"
    if duration is not None:
        command += f" {duration}"
    _print_rcon_response(_send_rcon_command(ctx, command))


whitelist_app = typer.Typer(help="Whitelist shortcuts")
quick_app.add_typer(whitelist_app, name="whitelist")


@whitelist_app.command("add")
def whitelist_add(ctx: typer.Context, player: str = typer.Argument(..., help="Player to whitelist")):
    _print_rcon_response(_send_rcon_command(ctx, f"whitelist add {player}"))


@whitelist_app.command("remove")
def whitelist_remove(ctx: typer.Context, player: str = typer.Argument(..., help="Player to remove")):
    _print_rcon_response(_send_rcon_command(ctx, f"whitelist remove {player}"))


@whitelist_app.command("list")
def whitelist_list(ctx: typer.Context):
    _print_rcon_response(_send_rcon_command(ctx, "whitelist list"))


save_app = typer.Typer(help="World save controls")
quick_app.add_typer(save_app, name="save")


@save_app.command("all")
def save_all(ctx: typer.Context, flush: bool = typer.Option(False, "--flush", help="Use save-all flush")):
    command = "save-all flush" if flush else "save-all"
    _print_rcon_response(_send_rcon_command(ctx, command))


@save_app.command("on")
def save_on(ctx: typer.Context):
    _print_rcon_response(_send_rcon_command(ctx, "save-on"))


@save_app.command("off")
def save_off(ctx: typer.Context):
    _print_rcon_response(_send_rcon_command(ctx, "save-off"))


time_app = typer.Typer(help="Time controls")
quick_app.add_typer(time_app, name="time")


@time_app.command("set")
def time_set(ctx: typer.Context, value: str = typer.Argument(..., help="Value such as day/night or ticks")):
    _print_rcon_response(_send_rcon_command(ctx, f"time set {value}"))


@time_app.command("add")
def time_add(ctx: typer.Context, ticks: int = typer.Argument(..., help="Ticks to add")):
    _print_rcon_response(_send_rcon_command(ctx, f"time add {ticks}"))


@logs_app.command("tail")
def logs_tail(
    ctx: typer.Context,
    lines: int = typer.Option(50, "--lines", "-n", help="Number of lines to display"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow log output"),
):
    """Tail the latest server log."""
    cfg = _get_config(ctx)
    try:
        logs_module.tail_logs(cfg, lines=lines, follow=follow)
    except RuntimeError as exc:
        _fail(str(exc))


@server_app.command("start")
def server_start(ctx: typer.Context):
    """Start the Minecraft server container."""
    cfg = _get_config(ctx)
    try:
        server_module.start_server(cfg)
    except RuntimeError as exc:
        _fail(str(exc))
    typer.secho("Server started", fg="green")


@server_app.command("stop")
def server_stop(ctx: typer.Context):
    """Stop the Minecraft server container."""
    cfg = _get_config(ctx)
    try:
        server_module.stop_server(cfg)
    except RuntimeError as exc:
        _fail(str(exc))
    typer.secho("Server stopped", fg="yellow")


@server_app.command("restart")
def server_restart(ctx: typer.Context):
    """Restart the Minecraft server container."""
    cfg = _get_config(ctx)
    try:
        server_module.restart_server(cfg)
    except RuntimeError as exc:
        _fail(str(exc))
    typer.secho("Server restarted", fg="green")


@mods_app.command("init")
def mods_init(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing manifest"),
    adopt_existing: bool = typer.Option(False, "--adopt-existing", help="Add current files to manifest"),
):
    """Create a mods manifest in the server data directory."""
    cfg = _get_config(ctx)
    try:
        manifest, adopted = mods_module.init_manifest(cfg, force=force, adopt_existing=adopt_existing)
    except ManifestError as exc:
        _fail(str(exc))
    typer.secho(
        f"Initialized manifest for loader={manifest.loader or 'unknown'} at {mods_module.manifest_path(cfg)}",
        fg="green",
    )
    if adopted:
        typer.secho(f"Adopted {adopted} existing mod(s).", fg="cyan")


@mods_app.command("status")
def mods_status(ctx: typer.Context):
    """Show manifest summary and filesystem drift."""
    cfg = _get_config(ctx)
    try:
        manifest = mods_module.load_manifest(cfg)
        inv = mods_module.inventory(cfg, manifest)
    except ManifestError as exc:
        _fail(str(exc))

    summary = inv.summary
    table = Table(title="Mods summary", box=box.SIMPLE_HEAVY)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Tracked mods", str(summary["total"]))
    table.add_row("Healthy", str(summary["ok"]))
    table.add_row("Missing", str(summary["missing"]))
    table.add_row("Misplaced", str(summary["moved"]))
    table.add_row("Hash mismatch", str(summary["hash_mismatch"]))
    table.add_row("Extras", str(summary["extras"]))
    _rich_console.print(table)

    if inv.extras:
        extra_table = Table(title="Untracked files", box=box.MINIMAL)
        extra_table.add_column("Filename")
        extra_table.add_column("Location")
        for extra in inv.extras:
            extra_table.add_row(extra.filename, extra.location)
        _rich_console.print(extra_table)


@mods_app.command("list")
def mods_list(
    ctx: typer.Context,
    include_extras: bool = typer.Option(True, "--show-extras/--no-show-extras", help="Toggle untracked files display"),
):
    """List mods tracked in the manifest."""
    cfg = _get_config(ctx)
    try:
        manifest = mods_module.load_manifest(cfg)
        inv = mods_module.inventory(cfg, manifest)
    except ManifestError as exc:
        _fail(str(exc))

    table = Table(title="Tracked mods", box=box.MINIMAL_DOUBLE_HEAD)
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Filename")
    table.add_column("Enabled")
    table.add_column("Status")
    table.add_column("Hash")
    for status in inv.entries:
        hash_cell = "-"
        if status.hash_ok is True:
            hash_cell = "ok"
        elif status.hash_ok is False:
            hash_cell = "mismatch"
        enabled = "yes" if status.entry.enabled else "no"
        status_style = {
            "ok": "green",
            "missing": "red",
            "moved": "yellow",
            "hash-mismatch": "magenta",
        }.get(status.status, "white")
        table.add_row(
            status.entry.id,
            status.entry.name or "-",
            status.entry.filename,
            enabled,
            Text(status.status, style=status_style),
            hash_cell,
        )
    _rich_console.print(table)

    if include_extras and inv.extras:
        extra_table = Table(title="Untracked files", box=box.MINIMAL)
        extra_table.add_column("Filename")
        extra_table.add_column("Location")
        for extra in inv.extras:
            extra_table.add_row(extra.filename, extra.location)
        _rich_console.print(extra_table)


@mods_app.command("add")
def mods_add(
    ctx: typer.Context,
    source: str = typer.Argument(..., help="Local path, URL, or remote identifier (e.g. modrinth:lithium)"),
    mod_id: str = typer.Option(None, "--id", help="Explicit manifest ID"),
    name: str = typer.Option(None, "--name", help="Human readable name"),
    disable: bool = typer.Option(False, "--disable", help="Add but mark disabled"),
    manifest_only: bool = typer.Option(False, "--manifest-only", help="Record entry without downloading"),
    filename: str = typer.Option(None, "--filename", help="Override destination filename"),
    source_type: str = typer.Option(None, "--source-type", help="Force source type detection"),
    force: bool = typer.Option(False, "--force", help="Allow adding while server is running"),
    loader_hint: str = typer.Option(None, "--loader", help="Loader override when resolving Modrinth/CurseForge sources"),
    mc_version_hint: str = typer.Option(None, "--mc-version", help="Minecraft version override for remote sources"),
    version_hint: str = typer.Option(None, "--version", help="Specific remote version identifier (e.g., Modrinth version ID)"),
    project_id: str = typer.Option(None, "--project-id", help="Explicit project ID for Modrinth/CurseForge"),
):
    """Add a new mod to the manifest and copy/download its file."""
    cfg = _get_config(ctx)
    _ensure_server_stopped(cfg, force=force, action="add mods")

    try:
        manifest = mods_module.load_manifest(cfg)
        entry = mods_module.add_mod(
            cfg,
            source=source,
            manifest=manifest,
            mod_id=mod_id,
            name=name,
            enabled=not disable,
            manifest_only=manifest_only,
            filename_override=filename,
            source_type=source_type,
            loader_hint=loader_hint,
            mc_version_hint=mc_version_hint,
            version_hint=version_hint,
            project_id=project_id,
        )
    except ManifestError as exc:
        _fail(str(exc))

    state = "disabled" if disable else "enabled"
    typer.secho(f"Added mod {entry.id} ({state})", fg="green")


@mods_app.command("enable")
def mods_enable(
    ctx: typer.Context,
    mod_id: str = typer.Argument(..., help="Manifest mod ID"),
    force: bool = typer.Option(False, "--force", help="Allow while server running"),
    no_move: bool = typer.Option(False, "--no-move", help="Only toggle manifest flag"),
):
    """Enable a mod (moves file back into mods directory)."""
    cfg = _get_config(ctx)
    _ensure_server_stopped(cfg, force=force, action="enable mods")
    try:
        manifest = mods_module.load_manifest(cfg)
        entry = mods_module.set_enabled(cfg, manifest=manifest, mod_id=mod_id, enabled=True, move_files=not no_move)
    except ManifestError as exc:
        _fail(str(exc))
    typer.secho(f"Enabled mod {entry.id}", fg="green")


@mods_app.command("disable")
def mods_disable(
    ctx: typer.Context,
    mod_id: str = typer.Argument(..., help="Manifest mod ID"),
    force: bool = typer.Option(False, "--force", help="Allow while server running"),
    no_move: bool = typer.Option(False, "--no-move", help="Only toggle manifest flag"),
):
    """Disable a mod (moves file into mods-disabled)."""
    cfg = _get_config(ctx)
    _ensure_server_stopped(cfg, force=force, action="disable mods")
    try:
        manifest = mods_module.load_manifest(cfg)
        entry = mods_module.set_enabled(cfg, manifest=manifest, mod_id=mod_id, enabled=False, move_files=not no_move)
    except ManifestError as exc:
        _fail(str(exc))
    typer.secho(f"Disabled mod {entry.id}", fg="yellow")
