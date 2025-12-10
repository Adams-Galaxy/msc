from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess
from typing import Optional

from .config import MscConfig


@dataclass(slots=True)
class ServerStatus:
    running: bool
    container_id: Optional[str] = None
    uptime: Optional[str] = None


class ComposeError(RuntimeError):
    pass


def _compose_base_cmd() -> list[str]:
    return ["docker", "compose"]


def _compose_run(cfg: MscConfig, args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = _compose_base_cmd() + args + [cfg.docker_service]
    return subprocess.run(
        cmd,
        cwd=cfg.server_root,
        text=True,
        capture_output=True,
        check=False,
    )


def _raise_on_error(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise ComposeError(f"Failed to {action}: {stderr}")


def get_status(cfg: MscConfig) -> ServerStatus:
    result = subprocess.run(
        _compose_base_cmd() + ["ps", "-q", cfg.docker_service],
        cwd=cfg.server_root,
        text=True,
        capture_output=True,
        check=False,
    )
    container_id = result.stdout.strip()
    if not container_id:
        return ServerStatus(running=False)

    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id],
        text=True,
        capture_output=True,
        check=False,
    )
    uptime = None
    if inspect.returncode == 0:
        started_at = inspect.stdout.strip()
        if started_at:
            uptime = _calculate_uptime(started_at)

    return ServerStatus(running=True, container_id=container_id, uptime=uptime)


def _calculate_uptime(started_at: str) -> Optional[str]:
    try:
        clean_value = started_at.rstrip("Z")
        if "." in clean_value:
            clean_value = clean_value.split(".")[0]
        started = datetime.fromisoformat(clean_value).replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - started
        return str(delta).split(".")[0]
    except ValueError:
        return None


def start_server(cfg: MscConfig) -> None:
    result = _compose_run(cfg, ["up", "-d"])
    _raise_on_error(result, "start server")


def stop_server(cfg: MscConfig) -> None:
    result = _compose_run(cfg, ["stop"])
    _raise_on_error(result, "stop server")


def restart_server(cfg: MscConfig) -> None:
    stop_server(cfg)
    start_server(cfg)


def attach_console(cfg: MscConfig) -> None:
    status = get_status(cfg)
    if not status.running or not status.container_id:
        raise ComposeError("Server is not running; start it before attaching.")

    cmd = ["docker", "attach", status.container_id]
    # Use inherited stdin/stdout for an interactive session
    result = subprocess.run(cmd, cwd=cfg.server_root, check=False)
    if result.returncode not in (0, 130):  # 130 == interrupted
        raise ComposeError("Failed to attach to server console")
