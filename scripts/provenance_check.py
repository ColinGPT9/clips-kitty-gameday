#!/usr/bin/env python3
"""Check that this repository holds no code from Clips Kitty (clips-studio) as
it was before the event.

It compares this repository with a reference git repository at one commit:
by default clips-studio at 6245e02 (28 Oct 2026), the commit the event branch
started from. The reference is read with git plumbing only (`git ls-tree`
lists its files, `git cat-file --batch` reads them), so nothing is checked
out and nothing in it changes.

Both sides are normalized the same way. Each line is trimmed and its runs of
spaces collapsed. Blank lines, comment-only lines and lines that are only
brackets or punctuation are dropped. Then every run of five such lines (a
window) found in both is flagged, unless all five are imports or trivial
lines, so common boilerplate does not trip it. A trivial line has two words
or fewer (three in YAML and TOML, where every line starts with a key), or is
a stock line: a bare HTML head tag, Python's __main__ guard, "use strict".

Code (.py .js .mjs .cjs .ts .tsx .jsx .html .css .sql .toml .yaml .yml) fails
the check. Markdown is compared separately, Markdown with Markdown, and only
warned about.

    python scripts/provenance_check.py
    python scripts/provenance_check.py --reference ../clips-studio --json
    python scripts/provenance_check.py --root ../another-checkout --exclude "docs/*"

The default reference is $PROVENANCE_REFERENCE, else ~/YouTube-Clips-Automation
(a local clone of clips-studio). --list-blobs prints the reference blobs the
check reads, so CI can fetch them in one go into a partial clone first.

Exit status: 0 no shared code, 1 shared code found, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REV = "6245e02332ba07e0c9b12d47a95ee81502f4122d"
WINDOW = 5
MAX_BYTES = 4 * 1024 * 1024  # anything bigger is data or generated, not code someone wrote

CODE_EXTS = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".sql",
                       ".toml", ".yaml", ".yml"})
DOC_EXTS = frozenset({".md"})
CONFIG_EXTS = frozenset({".toml", ".yaml", ".yml"})
# Never part of the repository, even when present in the folder.
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
                       ".mypy_cache", ".wrangler", "data", "build", "dist"})

_C_FAMILY = (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")
LINE_COMMENTS: dict[str, tuple[str, ...]] = {
    **{ext: ("#",) for ext in (".py", ".toml", ".yaml", ".yml")},
    **{ext: ("//",) for ext in _C_FAMILY},
    ".sql": ("--",),
    ".html": ("//",),  # inside <script>
}
BLOCK_COMMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    **{ext: (("/*", "*/"),) for ext in (*_C_FAMILY, ".css", ".sql")},
    ".html": (("<!--", "-->"), ("/*", "*/")),
    ".md": (("<!--", "-->"),),
}

HAS_TEXT = re.compile(r"[^\W_]")  # a letter or a digit
PY_IMPORT = re.compile(r"^(?:import\s+[\w.]+|from\s+[\w.]+\s+import\b)")
JS_IMPORT = re.compile(r"^(?:import\b|export\s+(?:\*|\{[^}]*\})\s+from\b"
                       r"|(?:const|let|var)\s+[\w${}\s,:]+=\s*(?:require|await\s+import)\()")
CSS_IMPORT = re.compile(r"^@(?:import|use|forward|charset)\b")
STOCK_LINE = re.compile(  # in nearly every project: a bare HTML head tag, the __main__ guard, "use strict"
    r"""^(?:<!doctype\b[^>]*>|</?(?:html|head|body)\b[^>]*>|<(?:meta|link|base)\b[^>]*>"""
    r"""|<script\b[^>]*>\s*</script>|<title>[^<]*</title>|</[a-z][\w-]*>"""
    r"""|if __name__ == (["'])__main__\1:|(["'])use strict\2;?)$""",
    re.IGNORECASE,
)


class CheckError(Exception):
    """The check could not run: a bad reference, a missing commit, no git."""


@dataclass
class Source:
    """One file, normalized: the kept lines, their line numbers, and whether
    each is plain (an import or a trivial line)."""

    path: str
    numbers: list[int] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    plain: list[bool] = field(default_factory=list)


@dataclass
class Match:
    path: str
    start: int
    end: int
    windows: int
    reference: dict
    text: str

    def as_dict(self) -> dict:
        return {"path": self.path, "start": self.start, "end": self.end, "windows": self.windows,
                "reference": self.reference, "text": self.text}


# ---- normalizing ------------------------------------------------------------------------


def words(line: str) -> int:
    """Space-separated pieces with a letter or digit in them."""
    return sum(1 for piece in line.split(" ") if HAS_TEXT.search(piece))


def is_trivial(line: str, ext: str = "") -> bool:
    """Two words or fewer (three in YAML and TOML, where every line starts
    with a key), or a stock line such as a bare HTML head tag."""
    return words(line) <= (3 if ext in CONFIG_EXTS else 2) or bool(STOCK_LINE.match(line))


def _import(line: str, ext: str) -> str | None:
    """None when `line` is not an import. Else "" or, for an import that goes
    on over the next lines, the bracket that ends it."""
    if ext == ".py" and PY_IMPORT.match(line):
        return ")" if line.endswith("(") else ""
    if ext in _C_FAMILY and JS_IMPORT.match(line):
        return "}" if line.endswith("{") else ""
    if ext in (".css", ".html") and CSS_IMPORT.match(line):
        return ""
    return None


def normalize(text: str, ext: str) -> Source:
    """`text` as the check compares it. `ext` picks the comment syntax."""
    out = Source(path="")
    line_comments = LINE_COMMENTS.get(ext, ())
    blocks = BLOCK_COMMENTS.get(ext, ())
    closing = ""  # the end of the block comment we are in
    import_until = ""  # the bracket that ends a multi-line import we are in
    for number, raw in enumerate(text.split("\n"), start=1):
        line = " ".join(raw.split())
        if closing:
            if closing in line:
                closing = ""
            continue
        if not line:
            continue
        opened = next(((o, c) for o, c in blocks if line.startswith(o)), None)
        if opened is not None:
            o, c = opened
            if c not in line[len(o):]:
                closing = c
            continue
        if line_comments and line.startswith(line_comments):
            continue
        if import_until:
            plain = True
            if import_until in line:
                import_until = ""
        else:
            ending = _import(line, ext)
            plain = ending is not None or is_trivial(line, ext)
            import_until = ending or ""
        if not HAS_TEXT.search(line):
            continue  # brackets and punctuation only
        out.numbers.append(number)
        out.lines.append(line)
        out.plain.append(plain)
    return out


def decode(data: bytes) -> str | None:
    """Text, or None for a file that is binary or too big to be code."""
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace")  # -sig: a leading byte order mark is dropped


def ext_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""


def skipped(path: str) -> bool:
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.split("/")[:-1])


# ---- reading both sides -----------------------------------------------------------------


def _git(where: Path, *args: str, stdin: bytes | None = None) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(["git", "-C", str(where), *args], input=stdin, capture_output=True, env=env)
    except OSError as exc:
        raise CheckError(f"git didn't run: {exc}") from exc
    if proc.returncode != 0:
        why = proc.stderr.decode("utf-8", errors="replace").strip()[:400]
        raise CheckError(f"git {args[0]} failed in {where}: {why}")
    return proc.stdout


def reference_entries(reference: Path, rev: str) -> list[tuple[str, str]]:
    """(path, blob id) for each code and Markdown file at `rev`, from ls-tree."""
    out = []
    for entry in _git(reference, "ls-tree", "-r", "-z", "--full-tree", rev).split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        path = raw_path.decode("utf-8", errors="replace")
        if kind == "blob" and mode in ("100644", "100755") and ext_of(path) in CODE_EXTS | DOC_EXTS:
            out.append((path, oid))
    return out


def read_blobs(reference: Path, oids: list[str]) -> dict[str, bytes]:
    """The contents of each blob, from one `git cat-file --batch`."""
    unique = sorted(set(oids))
    if not unique:
        return {}
    data = _git(reference, "cat-file", "--batch", stdin=("\n".join(unique) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(data):
        end = data.index(b"\n", pos)
        header = data[pos:end].decode().split()
        if len(header) != 3:
            raise CheckError(f"git cat-file couldn't read {header[0] if header else '?'} in {reference}")
        size = int(header[2])
        blobs[header[0]] = data[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


def repo_files(root: Path) -> list[str]:
    """This repository's files, relative and with forward slashes. In a git
    checkout: what git would commit (tracked, or new and not ignored)."""
    if (root / ".git").exists():
        listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        paths = [p for p in listed.decode("utf-8", errors="replace").split("\0") if p]
    else:
        paths = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
            rel = Path(folder).relative_to(root).as_posix()
            paths += [f if rel == "." else f"{rel}/{f}" for f in sorted(files)]
    return sorted(p for p in paths if not skipped(p) and (root / p).is_file())


def _excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


# ---- comparing --------------------------------------------------------------------------


def _windows(source: Source):
    for i in range(len(source.lines) - WINDOW + 1):
        if not all(source.plain[i:i + WINDOW]):
            yield i, hash(tuple(source.lines[i:i + WINDOW]))


def index_of(sources: list[Source]) -> dict[int, tuple[int, int]]:
    """Each window's hash -> (source number, first line index) of its first appearance."""
    index: dict[int, tuple[int, int]] = {}
    for n, source in enumerate(sources):
        for i, key in _windows(source):
            index.setdefault(key, (n, i))
    return index


def shared(source: Source, refs: list[Source], index: dict[int, tuple[int, int]]) -> list[Match]:
    """The windows in `source` that a reference file also has, merged into
    blocks where they overlap or touch."""
    hits: list[tuple[int, int, int]] = []  # (window start here, reference source, window start there)
    for i, key in _windows(source):
        found = index.get(key)
        if found is not None and refs[found[0]].lines[found[1]:found[1] + WINDOW] == source.lines[i:i + WINDOW]:
            hits.append((i, *found))
    matches: list[Match] = []
    run: list[tuple[int, int, int]] = []
    for hit in hits + [(-WINDOW - 1, 0, 0)]:
        if run and not run[-1][0] < hit[0] <= run[-1][0] + WINDOW:
            first, last = run[0], run[-1]
            ref = refs[first[1]]
            ref_end = ref.numbers[first[2] + WINDOW - 1]
            if last[1] == first[1]:
                ref_end = max(ref_end, refs[last[1]].numbers[last[2] + WINDOW - 1])
            matches.append(Match(
                path=source.path, start=source.numbers[first[0]], end=source.numbers[last[0] + WINDOW - 1],
                windows=len(run), reference={"path": ref.path, "start": ref.numbers[first[2]], "end": ref_end},
                text=source.lines[first[0]][:120],
            ))
            run = []
        run.append(hit)
    return matches


@dataclass
class Report:
    root: str
    reference: str
    rev: str
    reference_files: int = 0
    reference_docs: int = 0
    files_checked: int = 0
    docs_checked: int = 0
    matches: list[Match] = field(default_factory=list)
    doc_matches: list[Match] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": not self.matches, "root": self.root, "reference": self.reference, "rev": self.rev,
                "window": WINDOW, "reference_files": self.reference_files, "reference_docs": self.reference_docs,
                "files_checked": self.files_checked, "docs_checked": self.docs_checked,
                "matches": [m.as_dict() for m in self.matches],
                "doc_matches": [m.as_dict() for m in self.doc_matches]}


def check(root: Path, reference: Path, rev: str, excludes: list[str] | None = None) -> Report:
    excludes = list(excludes or [])
    report = Report(root=str(root), reference=str(reference), rev=rev)
    entries = reference_entries(reference, rev)
    blobs = read_blobs(reference, [oid for _, oid in entries])
    ref_code: list[Source] = []
    ref_docs: list[Source] = []
    for path, oid in entries:
        text = decode(blobs[oid])
        if text is None:
            continue
        source = normalize(text, ext_of(path))
        source.path = path
        (ref_docs if ext_of(path) in DOC_EXTS else ref_code).append(source)
    if not ref_code:
        raise CheckError(f"{reference} has no code at {rev} to compare with. Is it the right repository?")
    report.reference_files, report.reference_docs = len(ref_code), len(ref_docs)
    code_index, doc_index = index_of(ref_code), index_of(ref_docs)

    for path in repo_files(root):
        ext = ext_of(path)
        if ext not in CODE_EXTS | DOC_EXTS or _excluded(path, excludes):
            continue
        text = decode((root / path).read_bytes())
        if text is None:
            continue
        source = normalize(text, ext)
        source.path = path
        if ext in DOC_EXTS:
            report.docs_checked += 1
            report.doc_matches += shared(source, ref_docs, doc_index)
        else:
            report.files_checked += 1
            report.matches += shared(source, ref_code, code_index)
    return report


# ---- the command ------------------------------------------------------------------------


def _default_reference() -> Path:
    return Path(os.environ.get("PROVENANCE_REFERENCE") or Path.home() / "YouTube-Clips-Automation")


def _print_report(report: Report) -> None:
    print("Provenance check")
    print(f"  this repository: {report.root} ({report.files_checked} code files, {report.docs_checked} Markdown)")
    print(f"  reference:       {report.reference} at {report.rev[:12]} "
          f"({report.reference_files} code files, {report.reference_docs} Markdown)")

    def show(matches: list[Match]) -> None:
        for m in matches:
            ref = m.reference
            print(f"  {m.path}:{m.start}-{m.end} = {ref['path']}:{ref['start']}-{ref['end']} "
                  f"({m.windows} window{'s' if m.windows != 1 else ''})")
            print(f"      {m.text}")

    if report.matches:
        print(f"\nCode shared with the reference ({WINDOW} or more lines in a row):")
        show(report.matches)
    if report.doc_matches:
        print("\nMarkdown shared with the reference (a warning, not a failure):")
        show(report.doc_matches)
    files = len({m.path for m in report.matches})
    print()
    if report.matches:
        print(f"FAIL: {len(report.matches)} shared block{'s' if len(report.matches) != 1 else ''} of code "
              f"in {files} file{'s' if files != 1 else ''}.")
    else:
        print("OK: no code shared with the reference.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that no code here comes from the reference repository "
                                                 "(clips-studio before the event).")
    parser.add_argument("--reference", type=Path, default=None,
                        help="the reference git repository (default: $PROVENANCE_REFERENCE, "
                             "else ~/YouTube-Clips-Automation)")
    parser.add_argument("--rev", default=DEFAULT_REV, help=f"the reference commit (default: {DEFAULT_REV[:12]})")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="the repository to check (default: the one this script is in)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip paths here matching GLOB, e.g. 'docs/*' (repeatable)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--list-blobs", action="store_true",
                        help="print the reference blob ids the check reads, one per line, and stop")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    reference = (args.reference or _default_reference()).expanduser()
    try:
        if not reference.is_dir():
            raise CheckError(f"No reference repository at {reference}. Pass --reference.")
        if args.list_blobs:
            oids = sorted({oid for _, oid in reference_entries(reference, args.rev)})
            sys.stdout.flush()
            sys.stdout.buffer.write("".join(f"{oid}\n" for oid in oids).encode())  # "\n" on Windows too, for git
            sys.stdout.buffer.flush()
            return 0
        report = check(args.root.resolve(), reference, args.rev, args.exclude)
    except CheckError as exc:
        print(f"provenance_check: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        _print_report(report)
    return 1 if report.matches else 0


if __name__ == "__main__":
    sys.exit(main())
