"""
Git Tools — Version Control Operations
========================================

Git operations via subprocess. Works fully offline with no platform
dependencies. Finds the repository root by walking up from the current
working directory or using ``AITHER_ROOT``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _get_repo_root() -> Path:
    """Find the repository root by walking up from cwd or AITHER_ROOT."""
    root_env = os.environ.get("AITHER_ROOT")
    if root_env:
        return Path(root_env)

    current = Path.cwd().resolve()
    while current != current.parent:
        if (current / ".git").exists():
            return current
        current = current.parent

    # Fallback to cwd
    return Path.cwd()


REPO_ROOT = _get_repo_root()


def _run_git(*args: str, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    """Execute a git command and return (returncode, stdout, stderr).

    Args:
        *args: Git subcommand and arguments.
        cwd: Working directory (default: REPO_ROOT).

    Returns:
        Tuple of (return code, stdout text, stderr text).
    """
    cmd = ["git"] + list(args)
    work_dir = cwd or REPO_ROOT

    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        return (1, "", f"git {args[0]} timed out after 30s")
    except FileNotFoundError:
        return (1, "", "git is not installed or not on PATH")
    except Exception as e:
        return (1, "", str(e))


# ---------------------------------------------------------------------------
# Status & Information
# ---------------------------------------------------------------------------

def git_status() -> str:
    """Get the current git repository status.

    Shows the current branch, modified files, staged changes, and untracked
    files. Use this to understand the repository state before making changes.

    Returns:
        JSON string with branch, staged, modified, untracked files,
        and ahead/behind counts.
    """
    result = {
        "branch": "",
        "is_clean": True,
        "staged": [],
        "modified": [],
        "untracked": [],
        "ahead": 0,
        "behind": 0,
    }

    # Current branch
    code, stdout, _ = _run_git("branch", "--show-current")
    if code == 0:
        result["branch"] = stdout.strip()

    # Porcelain status
    code, stdout, stderr = _run_git("status", "--porcelain", "-b")
    if code != 0:
        return json.dumps({"error": stderr or "git status failed"})

    for line in stdout.strip().split("\n"):
        if not line:
            continue

        if line.startswith("##"):
            if "[" in line:
                tracking = line.split("[")[1].rstrip("]")
                match = re.search(r"ahead (\d+)", tracking)
                if match:
                    result["ahead"] = int(match.group(1))
                match = re.search(r"behind (\d+)", tracking)
                if match:
                    result["behind"] = int(match.group(1))
        else:
            status_code = line[:2]
            file_path = line[3:]

            if status_code[0] in "MADRC":
                result["staged"].append(file_path)
            if status_code[1] in "MD":
                result["modified"].append(file_path)
            if status_code == "??":
                result["untracked"].append(file_path)

    result["is_clean"] = not (
        result["staged"] or result["modified"] or result["untracked"]
    )

    # Cap output to avoid overwhelming MCP pipes
    MAX_FILES = 50
    for key in ("staged", "modified", "untracked"):
        total = len(result[key])
        if total > MAX_FILES:
            result[key] = result[key][:MAX_FILES]
            result[f"{key}_truncated"] = total

    return json.dumps(result, indent=2)


def git_log(count: int = 10) -> str:
    """Show recent commit history.

    Args:
        count: Number of commits to show (default: 10, max: 100).

    Returns:
        JSON string with list of commits (hash, message, author, date).
    """
    count = min(max(count, 1), 100)
    code, stdout, stderr = _run_git(
        "log", f"-{count}", "--format=%H|%s|%an|%ar"
    )

    if code != 0:
        return json.dumps({"error": stderr or "git log failed"})

    commits = []
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) >= 4:
            commits.append({
                "hash": parts[0][:8],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })

    return json.dumps({"commits": commits, "count": len(commits)}, indent=2)


def git_diff(staged: bool = False, stat_only: bool = True) -> str:
    """Show differences in the working directory.

    Args:
        staged: If True, show staged changes (--cached). Default: unstaged.
        stat_only: If True, show only file-level stats (default: True).

    Returns:
        Diff output string or statistics summary.
    """
    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat_only:
        args.append("--stat")

    code, stdout, stderr = _run_git(*args)

    if code != 0:
        return json.dumps({"error": stderr or "git diff failed"})

    return stdout if stdout.strip() else "No differences found."


# ---------------------------------------------------------------------------
# Branch Management
# ---------------------------------------------------------------------------

def git_branch_list() -> str:
    """List all local git branches.

    Returns:
        JSON string with current branch name and list of all branches.
    """
    code, stdout, _ = _run_git("branch", "--format=%(refname:short)")
    branches = [b.strip() for b in stdout.strip().split("\n") if b.strip()]

    code2, current, _ = _run_git("branch", "--show-current")
    current_branch = current.strip()

    return json.dumps({
        "current": current_branch,
        "branches": branches,
        "count": len(branches),
    }, indent=2)


def git_create_branch(
    branch_name: str,
    branch_type: str = "feature",
    checkout: bool = True,
) -> str:
    """Create a new branch with a type prefix.

    Creates branches following the convention: type/name
    (e.g., feature/add-auth, fix/null-check).

    Args:
        branch_name: Name for the branch (without prefix).
        branch_type: Prefix type -- feature, patch, fix, hotfix, chore, docs.
        checkout: If True, switch to the new branch (default: True).

    Returns:
        JSON string with success status and branch name.
    """
    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "-", branch_name.lower())
    clean_name = re.sub(r"-+", "-", clean_name).strip("-")

    valid_types = {"feature", "patch", "fix", "hotfix", "chore", "docs"}
    if branch_type not in valid_types:
        branch_type = "feature"

    full_name = f"{branch_type}/{clean_name}"

    if checkout:
        code, stdout, stderr = _run_git("checkout", "-b", full_name)
    else:
        code, stdout, stderr = _run_git("branch", full_name)

    if code == 0:
        msg = f"Created branch '{full_name}'"
        if checkout:
            msg += " and switched to it"
        return json.dumps({"success": True, "branch": full_name, "message": msg})

    return json.dumps({"success": False, "branch": full_name, "error": stderr})


# ---------------------------------------------------------------------------
# Staging & Committing
# ---------------------------------------------------------------------------

def git_add(
    paths: Optional[str] = None,
    all_changes: bool = False,
) -> str:
    """Stage files for commit.

    Args:
        paths: Comma-separated file paths to stage (e.g., "file1.py,file2.py").
        all_changes: If True, stage all changes (git add -A). Overrides paths.

    Returns:
        JSON string with success status and staged file list.
    """
    if all_changes:
        args = ["add", "-A"]
    elif paths:
        path_list = [p.strip() for p in paths.split(",")]
        args = ["add"] + path_list
    else:
        return json.dumps({"error": "Specify paths or set all_changes=True"})

    code, stdout, stderr = _run_git(*args)
    if code != 0:
        return json.dumps({"success": False, "error": stderr})

    # List what is staged now
    code2, staged_out, _ = _run_git("diff", "--cached", "--name-only")
    staged_files = [f for f in staged_out.strip().split("\n") if f]

    return json.dumps({
        "success": True,
        "staged_files": staged_files,
        "count": len(staged_files),
    }, indent=2)


def git_commit(message: str, description: Optional[str] = None) -> str:
    """Create a commit with staged changes.

    Args:
        message: Short commit message summary line.
        description: Extended description (optional, becomes a second paragraph).

    Returns:
        JSON string with success status and commit hash.
    """
    full_message = message
    if description:
        full_message = f"{message}\n\n{description}"

    code, stdout, stderr = _run_git("commit", "-m", full_message)

    if code != 0:
        if "nothing to commit" in stderr.lower() or "nothing to commit" in stdout.lower():
            return json.dumps({
                "success": False,
                "message": "Nothing to commit -- working directory clean",
            })
        return json.dumps({"success": False, "error": stderr})

    # Get commit hash
    code2, hash_out, _ = _run_git("rev-parse", "HEAD")
    commit_hash = hash_out.strip()[:8] if code2 == 0 else ""

    return json.dumps({
        "success": True,
        "commit_hash": commit_hash,
        "message": message,
    })


def git_branch(branch_name: str = "") -> str:
    """Show current branch or switch to a different branch.

    Args:
        branch_name: Branch to switch to. If empty, shows current branch.

    Returns:
        JSON string with branch information or switch result.
    """
    if not branch_name:
        code, stdout, _ = _run_git("branch", "--show-current")
        return json.dumps({
            "current": stdout.strip() if code == 0 else "unknown",
        })

    code, stdout, stderr = _run_git("checkout", branch_name)
    return json.dumps({
        "success": code == 0,
        "branch": branch_name,
        "message": f"Switched to '{branch_name}'" if code == 0 else stderr,
    })


__all__ = [
    "git_status",
    "git_log",
    "git_diff",
    "git_branch_list",
    "git_create_branch",
    "git_add",
    "git_commit",
    "git_branch",
]
