#!/usr/bin/env python3
"""Apply a validated Manus patch on an issue branch and open a review pull request."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import PurePosixPath


def run(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() if capture else ""
        raise RuntimeError(f"Command failed: {args[0]}" + (f" ({detail[:500]})" if detail else ""))
    return result.stdout.strip() if capture else ""


def validate_diff_paths(patch: str) -> None:
    paths: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise RuntimeError("Patch contains an unsupported or ambiguous path.")
            paths.add(parts[2][2:])
            paths.add(parts[3][2:])
    if not paths:
        raise RuntimeError("Patch contains no Git file changes.")
    blocked = {".gitmodules", "id_rsa", "id_ed25519", "credentials.json"}
    for name in paths:
        path = PurePosixPath(name)
        base = path.name.lower()
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise RuntimeError("Patch contains an unsafe path.")
        if path.parts[0] in {".git", ".github"}:
            raise RuntimeError("Generated code may not modify Git metadata or workflow files.")
        if base in blocked or base.startswith(".env") or "secret" in base:
            raise RuntimeError("Generated code may not modify secret or credential files.")
        if base.endswith((".pem", ".key", ".p12", ".pfx")):
            raise RuntimeError("Generated code may not add private-key or certificate files.")
        if name.lower() in {"scripts/manus_issue_code.py", "scripts/create_issue_pr.py"}:
            raise RuntimeError("Generated code may not modify this workflow's safety helpers.")
    if "new file mode 120000" in patch or "old mode 120000" in patch:
        raise RuntimeError("Generated code may not create or modify symlinks.")


def main() -> None:
    issue_number = os.environ["ISSUE_NUMBER"].strip()
    base_branch = os.environ["BASE_BRANCH"].strip()
    repo = os.environ["GITHUB_REPOSITORY"].strip()
    patch_file = os.environ["PATCH_FILE"]
    result_file = os.environ["RESULT_FILE"]
    issue_file = os.environ["ISSUE_JSON"]
    if not re.fullmatch(r"[1-9][0-9]{0,8}", issue_number):
        raise RuntimeError("Invalid issue number.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", base_branch):
        raise RuntimeError("Invalid base branch name.")

    with open(patch_file, encoding="utf-8") as f:
        patch = f.read()
    validate_diff_paths(patch)
    with open(result_file, encoding="utf-8") as f:
        result = json.load(f)
    with open(issue_file, encoding="utf-8") as f:
        issue = json.load(f)

    branch = f"manus/issue-{issue_number}"
    existing_pr = run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--head", branch, "--json", "url", "--jq", ".[0].url // \"\""],
        capture=True,
    )
    if existing_pr:
        print(f"An open proposal already exists: {existing_pr}")
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"pr_url={existing_pr}\n")
        return

    run(["git", "fetch", "origin", base_branch])
    remote_branch = run(["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"], capture=True)
    if remote_branch:
        raise RuntimeError(
            f"Remote branch {branch} already exists but has no open PR. Delete or rename that stale branch before retrying; no force-push was attempted."
        )
    run(["git", "switch", "--create", branch, f"origin/{base_branch}"])
    run(["git", "apply", "--check", patch_file])
    run(["git", "apply", patch_file])
    run(["git", "diff", "--check"])
    run(["git", "add", "--all"])
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if staged.returncode == 0:
        print("Patch produced no repository changes; no PR was opened.")
        return
    if staged.returncode != 1:
        raise RuntimeError("Could not inspect staged changes.")

    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run(["git", "commit", "-m", f"Propose code for issue #{issue_number}"])
    run(["git", "push", "origin", f"HEAD:refs/heads/{branch}"])

    issue_title = str(issue.get("title", "")).replace("\r", " ").replace("\n", " ").strip()
    if len(issue_title) > 100:
        issue_title = issue_title[:97] + "..."
    title = f"Proposal for issue #{issue_number}: {issue_title}"[:250]
    summary = str(result.get("summary", "")).strip()[:4000]
    test_notes = result.get("test_notes") or []
    if isinstance(test_notes, list):
        test_section = "\n".join(f"- {str(item)[:500]}" for item in test_notes[:20]) or "- No test notes provided."
    else:
        test_section = str(test_notes)[:2000]
    issue_url = issue.get("html_url", f"https://github.com/{repo}/issues/{issue_number}")
    body = (
        f"Proposed implementation for issue #{issue_number}.\n\n"
        f"Related issue: {issue_url}\n\n"
        f"## Manus summary\n{summary or 'No summary provided.'}\n\n"
        f"## Suggested validation\n{test_section}\n\n"
        "This is an AI-generated proposal. Review the diff and run the project's checks before merging. "
        "The workflow does not push generated code directly to the default branch."
    )
    pr_url = run(
        [
            "gh", "pr", "create", "--repo", repo, "--base", base_branch,
            "--head", branch, "--title", title, "--body", body,
        ],
        capture=True,
    )
    print(f"Created review PR: {pr_url}")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"pr_url={pr_url}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1)
