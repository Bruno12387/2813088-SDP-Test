"""Deterministic git fixture used to verify the metric engine by hand.

History (12 non-merge commits c1..c12 with committer timestamps TS[0]..TS[11],
100000 s apart, all authored by Test <test@example.com> unless noted):

  c1   add    foo.txt(+3) deep/a/nested.txt(+2) sub/x.txt(+1)
  c2   modify foo.txt(+2,-1) deep/a/nested.txt(+2,-1)
  c3   rename foo.txt -> bar.txt (pure rename, no content change)
  c4   modify bar.txt(+1)
  c5   add    bin.dat (binary - never measured)
  c6   delete sub/x.txt(-1)
  c7   add    big.txt(+10)
  c8   rename big.txt -> big2.txt with one changed line in it (+1,-1)
  c9   rename bin.dat -> bin2.dat (binary - never measured)
  c10  add    .mailmap(+1)      maps Alias <alias@example.com> -> Canonical
  c11  add    mm.txt(+1)        authored by Alias <alias@example.com>
  c12  empty commit             (counts towards |H|, no changes)

Hand-computed expectations for the full history H = {c1..c12}:

  repository root: added 24, removed 4, growth 20, churn 28,
                   modifications 8, |H| 12
  authors: Test <test@example.com>       churn 27, modifications 7
           Canonical <canon@example.com> churn 1, modifications 1
           (the two identities are merged automatically by the mailmap)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

TEST_NAME = "Test"
TEST_EMAIL = "test@example.com"
ALIAS_NAME = "Alias"
ALIAS_EMAIL = "alias@example.com"
CANON_NAME = "Canonical"
CANON_EMAIL = "canon@example.com"

#: Committer timestamp of each fixture commit.
TS = [1580000000 + i * 100000 for i in range(12)]


def _git(repo: Path, *args: str, ts: int | None = None,
         author: tuple[str, str] | None = None) -> str:
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if ts is not None:
        stamp = f"{ts} +0000"
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    name, email = author or (TEST_NAME, TEST_EMAIL)
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = email
    env["GIT_COMMITTER_NAME"] = TEST_NAME
    env["GIT_COMMITTER_EMAIL"] = TEST_EMAIL
    proc = subprocess.run(["git", *args], cwd=repo, env=env,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return proc.stdout


def _write(repo: Path, relative: str, content) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _commit(repo: Path, index: int, message: str,
            author: tuple[str, str] | None = None,
            allow_empty: bool = False) -> None:
    args = ["commit", "-q", "--allow-empty" if allow_empty else "-m",
            *(["-m", message] if allow_empty else [message])]
    _git(repo, "add", "-A")
    _git(repo, *args, ts=TS[index], author=author)


def build_fixture_repo(path: Path) -> Path:
    """Create the fixture repository at ``path`` and return it."""
    repo = Path(path)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", TEST_NAME)
    _git(repo, "config", "user.email", TEST_EMAIL)
    _git(repo, "config", "commit.gpgsign", "false")

    _write(repo, "foo.txt", "a\nb\nc\n")
    _write(repo, "deep/a/nested.txt", "n1\nn2\n")
    _write(repo, "sub/x.txt", "x\n")
    _commit(repo, 0, "c1 add files")

    _write(repo, "foo.txt", "a\nB\nc\nd\n")
    _write(repo, "deep/a/nested.txt", "n1\nN2\nn3\n")
    _commit(repo, 1, "c2 modify")

    _git(repo, "mv", "foo.txt", "bar.txt")
    _commit(repo, 2, "c3 rename foo")

    _write(repo, "bar.txt", "a\nB\nc\nd\ne\n")
    _commit(repo, 3, "c4 change bar")

    _write(repo, "bin.dat", b"\x00\x01\x02\x03\x04")
    _commit(repo, 4, "c5 binary")

    _git(repo, "rm", "-q", "sub/x.txt")
    _commit(repo, 5, "c6 delete")

    _write(repo, "big.txt", "".join(f"{i}\n" for i in range(1, 11)))
    _commit(repo, 6, "c7 big")

    _git(repo, "mv", "big.txt", "big2.txt")
    _write(repo, "big2.txt",
           "1\n2\n3\nX\n5\n6\n7\n8\n9\n10\n")
    _commit(repo, 7, "c8 rename with change")

    _git(repo, "mv", "bin.dat", "bin2.dat")
    _commit(repo, 8, "c9 binary rename")

    _write(repo, ".mailmap",
           f"{CANON_NAME} <{CANON_EMAIL}> {ALIAS_NAME} <{ALIAS_EMAIL}>\n")
    _commit(repo, 9, "c10 mailmap")

    _write(repo, "mm.txt", "y\n")
    _commit(repo, 10, "c11 alias commit", author=(ALIAS_NAME, ALIAS_EMAIL))

    _commit(repo, 11, "c12 empty", allow_empty=True)

    return repo
