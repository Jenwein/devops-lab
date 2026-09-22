"""Documentation stays honest: links resolve, no workstation traces, commands match the code."""

from __future__ import annotations

import functools
import pathlib
import re
import subprocess
import sys
import unittest

REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
SELF = pathlib.Path(__file__).resolve()
DOCUMENTS = [
    REPOSITORY / "README.md",
    REPOSITORY / "CONTRIBUTING.md",
    REPOSITORY / "SECURITY.md",
    *sorted((REPOSITORY / "docs").glob("*.md")),
    *sorted((REPOSITORY / "examples").rglob("*.md")),
]
CODE_TREES = [REPOSITORY / "scripts", REPOSITORY / "config", REPOSITORY / "examples", REPOSITORY / "tests",
              REPOSITORY / ".github"]
CODE_FILES = [REPOSITORY / "compose.yaml", REPOSITORY / "versions.env"]
FORBIDDEN = (
    re.compile(r"jenwein"),
    re.compile(r"rengongwei"),
    re.compile(r"fysics", re.IGNORECASE),
    re.compile(r"\bPERD\b"),
    re.compile(r"172\.16\."),
    re.compile(r"\bwsl\b", re.IGNORECASE),
    re.compile(r"C:\\"),
    # CJK punctuation and kana, extension A, unified ideographs, compatibility
    # ideographs and the fullwidth forms: the whole range a CJK keyboard emits.
    re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"),
)
LINK = re.compile(r"\]\(([^)\s]+)\)")
# `scripts/lab <sub> …` anywhere in a command line, so a cron entry or a line
# prefixed with `cd … &&` or `VAR=value` is checked like a bare invocation.
LAB_COMMAND = re.compile(r"(?:^|\s)scripts/lab\s+(\S+)((?:\s+\S+)*)")
HELP_FLAG = re.compile(r"(?<!\S)(--[a-z][a-z0-9-]*)")
FENCE = re.compile(r"```(?:bash|sh|console)[^\S\n]*\n(.*?)```", re.DOTALL)
SCRIPT_FOR = {
    "setup": REPOSITORY / "scripts" / "bootstrap.py",
    "status": REPOSITORY / "scripts" / "status.py",
    "backup": REPOSITORY / "scripts" / "backup.py",
    "restore": REPOSITORY / "scripts" / "restore.py",
    "add-team": REPOSITORY / "scripts" / "teams.py",
    "enable-gitlab-auth": REPOSITORY / "scripts" / "auth.py",
}
PASSTHROUGH = {"up": {"--build"}, "down": {"--remove-orphans", "--volumes"}}
QUICKSTART = REPOSITORY / "examples" / "quickstart" / "quickstart.py"


def text_files(paths):
    for path in paths:
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for candidate in candidates:
            if "__pycache__" in candidate.parts or candidate == SELF:
                continue
            try:
                yield candidate, candidate.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


@functools.lru_cache(maxsize=None)
def help_text(script: pathlib.Path) -> str:
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=True)
    return result.stdout


@functools.lru_cache(maxsize=None)
def help_flags(script: pathlib.Path) -> frozenset[str]:
    """The flags a script actually declares, so `--roo` cannot pass as a prefix of `--root`."""
    return frozenset(HELP_FLAG.findall(help_text(script)))


def flags_in(command: str) -> set[str]:
    return {word.split("=", 1)[0] for word in command.split() if word.startswith("--")}


def usage_subcommands() -> set[str]:
    for line in (REPOSITORY / "scripts" / "lab").read_text().splitlines():
        match = re.search(r"usage: scripts/lab ([a-z|-]+) \[options\]", line)
        if match:
            return set(match.group(1).split("|"))
    raise AssertionError("scripts/lab has no usage line")


def command_lines(text: str):
    """Yield joined command lines from shell fences, continuation lines folded, comments dropped."""
    for block in FENCE.findall(text):
        buffer = ""
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            buffer += line[:-1].rstrip() + " " if line.endswith("\\") else line
            if not line.endswith("\\"):
                yield buffer
                buffer = ""
        if buffer:
            yield buffer


class LinkTests(unittest.TestCase):
    def test_every_relative_link_resolves(self) -> None:
        for path in DOCUMENTS:
            for target in LINK.findall(path.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("mailto:"):
                    continue
                target_path = (path.parent / target.split("#", 1)[0]).resolve() if target.split("#", 1)[0] else path
                self.assertTrue(target_path.exists(), f"{path.relative_to(REPOSITORY)} -> {target}")


class TraceTests(unittest.TestCase):
    def test_documents_and_code_carry_no_workstation_traces(self) -> None:
        for path, text in text_files([*DOCUMENTS, *CODE_TREES, *CODE_FILES]):
            with self.subTest(path=str(path.relative_to(REPOSITORY))):
                for pattern in FORBIDDEN:
                    match = pattern.search(text)
                    self.assertIsNone(match, f"{path.relative_to(REPOSITORY)} contains {pattern.pattern!r}")


class CommandTests(unittest.TestCase):
    def test_lab_subcommands_and_flags_in_docs_exist(self) -> None:
        subcommands = usage_subcommands()
        for path in DOCUMENTS:
            for line in command_lines(path.read_text(encoding="utf-8")):
                where = f"{path.relative_to(REPOSITORY)}: {line}"
                invocation = LAB_COMMAND.search(line)
                if invocation:
                    subcommand, arguments = invocation.group(1), invocation.group(2)
                    self.assertIn(subcommand, subcommands, where)
                    allowed = (PASSTHROUGH[subcommand] if subcommand in PASSTHROUGH
                               else help_flags(SCRIPT_FOR[subcommand]))
                    for flag in flags_in(arguments):
                        self.assertIn(flag, allowed, where)
                elif line.startswith("examples/quickstart/run.sh"):
                    for flag in flags_in(line.partition(" ")[2]):
                        self.assertIn(flag, help_flags(QUICKSTART), where)


if __name__ == "__main__":
    unittest.main()
