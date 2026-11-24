"""Publish a finished digest to a git branch so another machine can deliver it.

Why a git branch: the scheduled run happens in a cloud sandbox that is thrown
away afterwards, and an iMessage can only be sent by the Messages app on a Mac.
The repository is the one place both sides can already reach, with credentials
they already hold.  The run commits the finished digest to
``claude/digest-outbox`` — one small JSON file, one commit a day — and
``relay_imessage.py`` on the Mac picks it up from there.

The commit is built with git plumbing, so the working tree and the checked-out
branch are never touched.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import List, Optional

from backend.digest import render

OUTBOX_BRANCH = "claude/digest-outbox"
OUTBOX_DIR = "outbox"
OUTBOX_NAME = "latest.json"
SCHEMA = 1

# Used for the outbox commits only; no repository or global git config is changed.
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Barcelona digest",
    "GIT_AUTHOR_EMAIL": "digest-bot@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "Barcelona digest",
    "GIT_COMMITTER_EMAIL": "digest-bot@users.noreply.github.com",
}
_CREDENTIALS_IN_URL = re.compile(r"://[^/@\s]+@")


class OutboxError(Exception):
    """The digest could not be published.  The message is safe to show."""


def build_record(payload: dict, message: str) -> dict:
    """What the relay needs: when the digest was made, and the text to deliver."""
    return {
        "schema": SCHEMA,
        "city": payload["city"]["key"],
        "date": payload["today"]["date"],
        "generated_at": payload["generated_at"],
        "messages": render.plain_messages(message),   # plain text, for iMessage
        "whatsapp": message,                          # as rendered, with WhatsApp markup
    }


def _git(repo: str, *args: str, stdin: Optional[str] = None, check: bool = True) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", repo] + list(args), input=stdin, capture_output=True, text=True, timeout=90,
            env=dict(os.environ, **_IDENTITY, GIT_TERMINAL_PROMPT="0"),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OutboxError("git %s failed: %s" % (args[0], type(exc).__name__))
    if check and done.returncode != 0:
        # git's own error can quote the remote URL; never let embedded credentials through.
        detail = _CREDENTIALS_IN_URL.sub("://", (done.stderr or done.stdout).strip())[-300:]
        raise OutboxError("git %s failed: %s" % (args[0], detail or "exit %d" % done.returncode))
    return done.stdout.strip()


def _remote_tip(repo: str, remote: str, branch: str) -> Optional[str]:
    """The branch's current commit on the remote, fetched locally; None if it does not exist yet."""
    listing = _git(repo, "ls-remote", "--heads", remote, branch)
    if not listing:
        return None
    _git(repo, "fetch", "--quiet", remote, branch)
    return listing.split()[0]


def publish(record: dict, remote: str = "origin", branch: str = OUTBOX_BRANCH, repo: str = ".") -> str:
    """Commit ``record`` as the branch's latest digest and push it.  Returns the new commit id."""
    body = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=body)
    folder = _git(repo, "mktree", stdin="100644 blob %s\t%s\n" % (blob, OUTBOX_NAME))
    tree = _git(repo, "mktree", stdin="040000 tree %s\t%s\n" % (folder, OUTBOX_DIR))
    parent = _remote_tip(repo, remote, branch)
    commit_args: List[str] = ["commit-tree", tree, "-m", "digest %s (%s)" % (record.get("date"), record.get("city"))]
    if parent:
        commit_args += ["-p", parent]
    commit = _git(repo, *commit_args)
    _git(repo, "push", "--quiet", remote, "%s:refs/heads/%s" % (commit, branch))
    return commit
