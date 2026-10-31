"""
Command Tools — Shell Command Execution
=========================================

Execute shell commands with timeout protection and output capture.
Works fully offline with no platform dependencies.

Security: Commands are executed as an argument list (no shell=True)
to prevent injection. If shell interpretation is needed, the caller
must explicitly wrap the command.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from typing import Optional


# Maximum output size to return (prevent memory issues with large outputs)
_MAX_OUTPUT_BYTES = 100_000  # 100 KB


def run_command(
    command: str,
    cwd: str = "",
    timeout: int = 60,
    shell: bool = False,
) -> str:
    """Execute a shell command and return its output.

    Runs a command as a subprocess with timeout protection. Output is
    captured and returned as a JSON string. By default, the command is
    split into an argument list (no shell interpretation) for safety.

    Args:
        command: The command to execute (e.g., "python --version").
        cwd: Working directory for the command (default: current directory).
        timeout: Maximum execution time in seconds (default: 60, max: 300).
        shell: If True, run through the system shell. Use with caution.

    Returns:
        JSON string with stdout, stderr, return code, and execution status.
    """
    if not command.strip():
        return json.dumps({"error": "Empty command"})

    # Cap timeout
    timeout = min(max(timeout, 1), 300)

    # Resolve working directory
    work_dir = cwd if cwd else None
    if work_dir and not os.path.isdir(work_dir):
        return json.dumps({"error": f"Working directory not found: {work_dir}"})

    try:
        if shell:
            args = command
        else:
            # Split command into argument list for safety
            try:
                args = shlex.split(command)
            except ValueError as e:
                return json.dumps({"error": f"Failed to parse command: {e}"})

        result = subprocess.run(
            args,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
        )

        stdout = result.stdout
        stderr = result.stderr

        # Truncate large outputs
        stdout_truncated = False
        stderr_truncated = False

        if len(stdout.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            stdout = stdout[:_MAX_OUTPUT_BYTES] + "\n... (output truncated)"
            stdout_truncated = True

        if len(stderr.encode("utf-8", errors="replace")) > _MAX_OUTPUT_BYTES:
            stderr = stderr[:_MAX_OUTPUT_BYTES] + "\n... (output truncated)"
            stderr_truncated = True

        response = {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

        if stdout_truncated or stderr_truncated:
            response["truncated"] = True

        return json.dumps(response)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "error": f"Command timed out after {timeout}s",
            "command": command,
        })
    except FileNotFoundError:
        # Command executable not found
        cmd_name = command.split()[0] if command.split() else command
        return json.dumps({
            "success": False,
            "error": f"Command not found: {cmd_name}",
        })
    except PermissionError:
        return json.dumps({
            "success": False,
            "error": f"Permission denied executing: {command}",
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e) or type(e).__name__,
        })


__all__ = [
    "run_command",
]
