#!/usr/bin/env python3
"""Flag the words and constructs this project does not use.

The banned list is not a matter of taste. Words like "leverage", "robust" or
"seamless" say nothing, and a reader who meets three of them in a paragraph stops
trusting the paragraph. Same for the em dash used as an aside: it lets a writer
bolt a second thought onto a sentence instead of writing two sentences.

The list lives in one file so prose and code are held to the same standard, and so
this can run in CI instead of depending on someone reading carefully.

    python tools/stylecheck.py README.md scriba/*.py
    python tools/stylecheck.py --list          # what is banned and why
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDLIST = HERE / "banned-words.txt"

# Put this on a line that quotes a banned term deliberately.
ALLOW_PRAGMA = "stylecheck: allow"

# Punctuation this project does not use.
#
# The em dash is the one that matters. Prefer a comma, a colon, parentheses, or a
# full stop. Two short sentences beat one sentence with a clause bolted on.
PUNCT_RULES = [
    (re.compile(r"—"), "em dash: use a comma, a colon, parentheses, or two sentences"),
    # Not a CLI flag (--force), and not the SRT/VTT cue arrow (-->), which is required
    # by those formats and is not punctuation anybody chose.
    (re.compile(r"(?<!-)--(?![->])(?!\s*[a-z-]+\b)"), "double hyphen used as a dash"),
    (re.compile(r"\s–\s"), "en dash as punctuation: use a comma or a colon"),
    (re.compile(r"\bhowever\b", re.I), '"however": use "still", or reword'),  # stylecheck: allow
    (re.compile(r"\bbut not\b", re.I), 'forced contrast "X but not Y": make two statements'),  # stylecheck: allow
    (re.compile(r"\bbut must\b", re.I), 'forced contrast "X but must Y": make two statements'),  # stylecheck: allow
    (re.compile(r"\bco-presence\b", re.I), 'write "copresence"'),
    (re.compile(r"\bBy [a-z]+ing\b.{0,60}\byou can\b"), '"By …ing, you can": say it directly'),
]

def prose_lines(path: Path) -> list[tuple[int, str]]:
    """The writing inside a source file: comments, docstrings, and shown strings.

    Identifiers stay out of it. SwiftUI colour names and Foundation argument labels
    are API, and flagging them would bury the real findings under noise. What gets
    checked is what a person wrote as text.
    """
    text = path.read_text(errors="replace")
    suffix = path.suffix.lower()
    out: list[tuple[int, str]] = []

    if suffix == ".py":
        import io
        import tokenize as tk
        try:
            for tok in tk.generate_tokens(io.StringIO(text).readline):
                if tok.type == tk.COMMENT:
                    out.append((tok.start[0], tok.string.lstrip("# ")))
                elif tok.type == tk.STRING:
                    raw = tok.string
                    # Triple-quoted strings are docstrings; single-quoted ones are
                    # usually either shown to a person or used as data. Both are text.
                    body = raw.strip("rbfu").strip("\"'")
                    # A path template is not writing. This rule was learned the hard
                    # way: the em dash ban once fired on a filename, the filename got
                    # renamed, and every job exported under the old name was left with
                    # two output files where one was stale.
                    looks_like_path = bool(
                        re.search(r"\.(md|json|srt|vtt|txt|wav|npz|npy|py|swift)\b", body)
                        or body.startswith(("/", "~", "./"))
                    )
                    if len(body) > 25 and " " in body and not looks_like_path:
                        for i, ln in enumerate(body.splitlines()):
                            out.append((tok.start[0] + i, ln))
        except (tk.TokenError, IndentationError, SyntaxError):
            pass
        return out

    if suffix in {".swift", ".sh", ".zsh"}:
        marker = "//" if suffix == ".swift" else "#"
        for n, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(marker) or stripped.startswith("///"):
                out.append((n, stripped.lstrip("/# ")))
                continue
            # Text shown to a person: a quoted run long enough to be a sentence.
            for m in re.finditer(r'"([^"\\]{25,})"', line):
                out.append((n, m.group(1)))
        return out

    return [(n, l) for n, l in enumerate(text.splitlines(), 1)]


def load_terms() -> list[str]:
    if not WORDLIST.exists():
        sys.exit(f"missing word list: {WORDLIST}")
    terms = []
    for raw in WORDLIST.read_text().splitlines():
        t = raw.strip().strip('"').lower()
        if t and not t.startswith("#"):
            terms.append(t)
    return terms


def build_pattern(terms: list[str]) -> re.Pattern[str]:
    # Longest first so "delve into" reports before "delve".  # stylecheck: allow
    parts = [re.escape(t).replace(r"\ ", r"\s+") for t in sorted(terms, key=len, reverse=True)]
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I)


def check(path: Path, pattern: re.Pattern[str], *, prose_only: bool) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    source = path.read_text(errors="replace").splitlines()
    if prose_only:
        lines = prose_lines(path)
    else:
        lines = list(enumerate(source, 1))
    for n, line in lines:
        # A line that quotes a banned term on purpose says so. The pragma is looked up
        # on the source line, not on the extracted text: for a string literal those are
        # different, and the comment carrying the pragma lives on the source line.
        # This file is the main customer, since a checker that names what it rejects
        # will always match itself.
        raw_line = source[n - 1] if 0 < n <= len(source) else ""
        if ALLOW_PRAGMA in raw_line:
            continue
        # An f-string placeholder holds an identifier, not writing. `{key}` names a
        # parameter; flagging it would push someone to rename working code to satisfy
        # a rule about prose.
        prose = re.sub(r"\{[^{}]*\}", " ", line)
        for rx, why in PUNCT_RULES:
            if rx.search(prose):
                hits.append((n, line.strip()[:90], why))
        for m in pattern.finditer(prose):
            hits.append((n, line.strip()[:90], f'banned term: "{m.group(0)}"'))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--list", action="store_true", help="print the banned terms and exit")
    ap.add_argument("--code", action="store_true",
                    help="check only comments, docstrings and shown strings (for source files)")
    args = ap.parse_args()

    terms = load_terms()
    if args.list:
        print(f"{len(terms)} banned terms in {WORDLIST}")
        print(f"{len(PUNCT_RULES)} punctuation and construct rules")
        return 0

    pattern = build_pattern(terms)
    total = 0
    for p in args.paths:
        if not p.is_file():
            continue
        hits = check(p, pattern, prose_only=args.code)
        if hits:
            print(f"\n{p}")
            for n, line, why in hits:
                print(f"  {n}: {why}")
                print(f"      {line}")
        total += len(hits)

    print(f"\n{total} findings across {len(args.paths)} files")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
