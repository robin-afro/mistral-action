"""Install and run Mistral Vibe CLI in headless/programmatic mode.

This module handles:
1. Installing Vibe via uv (if not already present)
2. Running Vibe with the assembled prompt in programmatic mode (--prompt)
3. Capturing and parsing output (text or JSON)
4. Returning structured results for the orchestrator
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

VIBE_PACKAGE = "mistral-vibe"


class Conclusion(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"


@dataclass
class VibeResult:
    conclusion: Conclusion
    output: str = ""
    output_json: list[dict] | None = None
    cost: float | None = None
    turns: int = 0
    error: str = ""


@dataclass
class VibeConfig:
    """Configuration for a Vibe run."""

    prompt: str
    api_key: str
    model: str = ""
    max_turns: int | None = None
    max_price: float | None = None
    output_format: str = "text"  # "text", "json", "streaming"
    auto_approve: bool = True
    extra_args: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800  # 30 minutes default
    workdir: str = ""
    enabled_tools: list[str] = field(default_factory=list)


def _find_vibe() -> str | None:
    """Find the vibe executable on PATH."""
    return shutil.which("vibe")


def install_vibe() -> str:
    """Install Vibe via uv and return the path to the executable.

    If Vibe is already installed and on PATH, returns the existing path.
    """
    existing = _find_vibe()
    if existing:
        logger.info("Vibe already installed at: %s", existing)
        return existing

    logger.info("Installing %s via uv...", VIBE_PACKAGE)

    # Check that uv is available
    uv_path = shutil.which("uv")
    if not uv_path:
        raise RuntimeError(
            "uv is not installed. Install it first: "
            "curl -LsSf https://astral.sh/uv/install.sh | sh"
        )

    # Install vibe as a uv tool (global CLI)
    for attempt in range(1, 4):
        logger.info("Installation attempt %d/3...", attempt)
        result = subprocess.run(
            [uv_path, "tool", "install", VIBE_PACKAGE],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("Vibe installed successfully")
            break
        logger.warning(
            "Install attempt %d failed: %s", attempt, result.stderr.strip()
        )
        if attempt == 3:
            raise RuntimeError(
                f"Failed to install {VIBE_PACKAGE} after 3 attempts: {result.stderr}"
            )
    else:
        # Should not reach here, but just in case
        raise RuntimeError(f"Failed to install {VIBE_PACKAGE}")

    # Find the installed executable
    vibe_path = _find_vibe()
    if not vibe_path:
        # uv tools go to ~/.local/bin by default; try adding it to PATH
        local_bin = Path.home() / ".local" / "bin"
        if (local_bin / "vibe").exists():
            vibe_path = str(local_bin / "vibe")
            os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"
            logger.info("Added %s to PATH", local_bin)
        else:
            raise RuntimeError(
                "Vibe was installed but the executable was not found on PATH. "
                f"Checked: {local_bin}"
            )

    logger.info("Vibe executable: %s", vibe_path)
    return vibe_path


def _build_command(vibe_path: str, config: VibeConfig, prompt: str) -> list[str]:
    """Build the vibe command line from config.

    ``prompt`` should be a *short* string (the bootstrap prompt) that fits
    within the OS argument-length limit.  The real payload lives in a file
    that the bootstrap prompt tells Vibe to read.
    """
    cmd = [vibe_path]

    cmd.extend(["--prompt", prompt])

    if config.auto_approve:
        cmd.extend(["--agent", "auto-approve"])

    if config.model:
        cmd.extend(["--model", config.model])

    if config.max_turns is not None:
        cmd.extend(["--max-turns", str(config.max_turns)])

    if config.max_price is not None:
        cmd.extend(["--max-price", str(config.max_price)])

    if config.output_format and config.output_format != "text":
        cmd.extend(["--output", config.output_format])

    if config.workdir:
        cmd.extend(["--workdir", config.workdir])

    for tool in config.enabled_tools:
        cmd.extend(["--enabled-tools", tool])

    # Extra args passed through
    cmd.extend(config.extra_args)

    return cmd


# Name of the prompt file written into the workspace so vibe can read_file it.
PROMPT_FILENAME = ".mistral-action-prompt.md"


def run_vibe(config: VibeConfig) -> VibeResult:
    """Run Vibe in programmatic/headless mode and return the result.

    Large prompts (>100 KB) can't be passed as CLI arguments because of the
    kernel's ARG_MAX limit.  Instead we:

    1. Write the full prompt to a file inside the workspace.
    2. Pass a short *bootstrap* prompt that tells Vibe to read that file.
    3. Vibe uses its built-in ``read_file`` tool, loads the instructions,
       and proceeds as normal.
    4. We clean up the file afterwards.
    """
    # Install if needed
    try:
        vibe_path = install_vibe()
    except RuntimeError as exc:
        return VibeResult(
            conclusion=Conclusion.FAILURE,
            error=f"Failed to install Vibe: {exc}",
        )

    # Resolve the workspace directory (where the repo is checked out)
    workdir = config.workdir or os.getcwd()
    prompt_file_path = os.path.join(workdir, PROMPT_FILENAME)

    try:
        # Write the full prompt into the workspace so vibe's read_file can see it
        Path(prompt_file_path).write_text(config.prompt, encoding="utf-8")
        logger.info(
            "Prompt written to %s (%d chars)", prompt_file_path, len(config.prompt),
        )

        # Build a tiny bootstrap prompt that fits comfortably in ARG_MAX
        bootstrap_prompt = (
            f"Your full task instructions are in the file `{PROMPT_FILENAME}` "
            f"in the current working directory. "
            f"Read that file NOW with read_file, then follow every instruction in it."
        )

        # Assemble the command
        cmd = _build_command(vibe_path, config, bootstrap_prompt)

        # Log (safe — the bootstrap prompt is short)
        logger.info("Running: %s", " ".join(cmd))

        # Environment
        env = os.environ.copy()
        env["MISTRAL_API_KEY"] = config.api_key
        env["CI"] = "true"
        env["TERM"] = "dumb"

        # Run vibe
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                env=env,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            logger.error("Vibe timed out after %d seconds", config.timeout_seconds)
            return VibeResult(
                conclusion=Conclusion.TIMEOUT,
                error=f"Vibe timed out after {config.timeout_seconds} seconds",
            )

        stdout = result.stdout
        stderr = result.stderr

        logger.info("Vibe exit code: %d", result.returncode)
        if stderr:
            logger.info("Vibe stderr:\n%s", stderr[:2000])

        # Parse output
        output_json = None
        if config.output_format == "json" and stdout.strip():
            try:
                output_json = json.loads(stdout)
            except json.JSONDecodeError:
                logger.warning("Failed to parse Vibe JSON output")

        if result.returncode == 0:
            return VibeResult(
                conclusion=Conclusion.SUCCESS,
                output=stdout,
                output_json=output_json if isinstance(output_json, list) else None,
            )
        else:
            return VibeResult(
                conclusion=Conclusion.FAILURE,
                output=stdout,
                output_json=output_json if isinstance(output_json, list) else None,
                error=stderr or f"Vibe exited with code {result.returncode}",
            )

    finally:
        # Clean up the prompt file so it doesn't get committed
        try:
            os.unlink(prompt_file_path)
        except OSError:
            pass
