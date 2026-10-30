"""Check the code blocks of the documentation site.

Every fenced code block under ``docs/site`` must be one of four things: synced from
a tested file under ``docs/examples``, syntax-checked, marked as output, or marked
as not tested with a reason. This module finds the blocks; `check` and `main` judge
them.

Line endings are not part of a comparison (a page with CRLF equals a source with LF);
every other character is. Only a line feed ends a line: form feeds and the Unicode
line and paragraph separators are ordinary characters. A leading byte-order mark is
ignored on reading and kept by ``--fix``. Reported, because the reader sees a code
block the check cannot judge: a fence inside a block quote and an HTML ``<pre>``
element. Not detected: a four-space indented code block (it cannot be told from a list
continuation without a full Markdown parser) and a hard link under ``docs/examples``
(git cannot store one).

An output block may name the region of an example it shows, ``<!-- output: example#region -->``.
The example runner (``scripts/run_docs_examples.sh``) writes what each region printed to
``$LAKANA_DOCS_CAPTURE/<example>/<region>.txt``; given that directory, this module compares the
block with the file after hiding the parts that differ on every run (`MASKS`), and ``--fix``
copies the changed lines from the file into the page.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import NamedTuple, NoReturn

import yaml

OUTPUT_LANGUAGES = {"", "text", "console"}
"""Languages an ``output`` marker may sit on: what a command printed is plain text."""

PARSED = {"python", "py", "toml", "yaml", "yml", "json"}
"""Languages whose blocks are parsed when they carry no marker."""

# A backtick fence's info string may not contain a backtick (else it is inline code);
# a tilde fence's may.
_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}(?=[^`]*$)|~{3,})(?P<info>.*)$")
_MARKER = re.compile(
    r"^[ \t]*<!--\s*(?P<kind>source|not tested|output)\s*(?::(?P<value>.*?))?\s*-->\s*$"
)
_COMMENT = re.compile(r"^[ \t]*<!--(?P<inner>.*?)-->\s*$")
_QUOTED_FENCE = re.compile(r"^[ \t]*>[> \t]*(?:`{3,}|~{3,})")
_PRE = re.compile(r"^[ \t]*<pre(?:[\s>]|$)", re.IGNORECASE)
_OUTPUT_REGION = re.compile(r"^[A-Za-z0-9_-]+#[A-Za-z0-9_-]+$")
_KINDS = ("source", "not tested", "output")
_FORMS = (
    "`<!-- source: docs/examples/<file> -->`, `<!-- not tested: <reason> -->` or `<!-- output -->`"
)
_LATER = "Blocks after this line on this page were not checked."


class BlockError(Exception):
    """A page whose code blocks cannot be read."""


@dataclass(frozen=True)
class Marker:
    """The comment on the line above a block.

    Parameters
    ----------
    kind
        ``"source"``, ``"not-tested"`` or ``"output"``.
    value
        The source path (with an optional ``#region``), the reason, the ``example#region`` an
        output block shows, or ``""``.
    """

    kind: str
    value: str


@dataclass(frozen=True)
class Block:
    """One fenced code block of a page.

    Parameters
    ----------
    path
        The page.
    line
        The 1-based line of the opening fence.
    lang
        The first word of the info string, lower-cased, or ``""``.
    body
        The block's content with the fence's indent removed, each line ending in a newline.
    marker
        The marker directly above the block, if any.
    indent
        The whitespace before the opening fence.
    fence
        The opening fence's characters.
    start
        The 0-based index of the first body line in the page's lines.
    end
        The 0-based index of the closing fence in the page's lines.
    """

    path: Path
    line: int
    lang: str
    body: str
    marker: Marker | None
    indent: str
    fence: str
    start: int
    end: int


class _Page(NamedTuple):
    r"""A page as read from disk.

    Parameters
    ----------
    bom
        Whether the file starts with a byte-order mark.
    lines
        The lines, split on line feeds only; a line ending in CRLF keeps its ``\\r``.
    final_newline
        Whether the file ends with a line feed.
    """

    bom: bool
    lines: list[str]
    final_newline: bool


def _read_page(path: Path) -> _Page:
    """Read a page, splitting it on line feeds only.

    Parameters
    ----------
    path
        The page.

    Returns
    -------
    _Page
        The page's content.

    Raises
    ------
    OSError
        If the file cannot be read.
    UnicodeDecodeError
        If the file is not UTF-8 text.
    """
    text = path.read_bytes().decode("utf-8")
    bom = text.startswith("\ufeff")
    text = text.removeprefix("\ufeff")
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return _Page(bom, lines, text.endswith("\n"))


def _stop(path: Path, number: int, message: str) -> NoReturn:
    """Fail a page whose later blocks cannot be read.

    Parameters
    ----------
    path
        The page.
    number
        The 1-based line the message is about.
    message
        What is wrong and what to do.

    Raises
    ------
    BlockError
        Always; the message says that later blocks were not checked.
    """
    raise BlockError(f"{path}:{number}: {message} {_LATER}")


def _distance(a: str, b: str) -> int:
    """Measure the edit distance between two words.

    Parameters
    ----------
    a
        One word.
    b
        The other.

    Returns
    -------
    int
        The number of single-character insertions, deletions and substitutions
        that turn `a` into `b`.
    """
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (x != y))
    return row[-1]


def _looks_like_marker(text: str) -> bool:
    """Tell a misspelled marker from an ordinary comment.

    A line looks like a marker when it is a whole HTML comment and the words before
    its first colon, lower-cased with spaces collapsed, are ``source`` or ``output``
    with one letter changed or missing, or within two edits of ``not tested``. A longer
    word such as ``sources`` or ``outputs`` is an ordinary comment.

    Parameters
    ----------
    text
        The line directly above a fence.

    Returns
    -------
    bool
        Whether the line is a near miss of a marker.
    """
    comment = _COMMENT.match(text)
    if comment is None:
        return False
    head = " ".join(comment["inner"].split(":", 1)[0].split()).lower()
    if _distance(head, "not tested") <= 2:
        return True
    return any(
        len(head) <= len(kind) and _distance(head, kind) <= 1 for kind in ("source", "output")
    )


def _marker(path: Path, number: int, text: str) -> Marker | None:
    """Read a marker line.

    Parameters
    ----------
    path
        The page, for the error message.
    number
        The 1-based line number of `text`.
    text
        The line directly above a fence.

    Returns
    -------
    Marker or None
        The marker, or ``None`` if the line is not one.

    Raises
    ------
    BlockError
        If the marker lacks the path or the reason it requires, or the line is a
        near miss of a marker (see `_looks_like_marker`).
    """
    match = _MARKER.match(text)
    if match is None:
        if _looks_like_marker(text):
            _stop(
                path,
                number,
                f"this comment above a code block looks like a marker but is not one; "
                f"write it exactly as {_FORMS} (lower case), or move it off the line "
                "above the fence.",
            )
        return None
    kind = match["kind"].replace(" ", "-")
    value = (match["value"] or "").strip()
    if kind == "output":
        if value and _OUTPUT_REGION.match(value) is None:
            _stop(
                path,
                number,
                f"`output: {value}` does not name a region; "
                "write `<!-- output: <example>#<region> -->`"
                " (the example's directory name under docs/examples, a `#`, and a `docs:start` name"
                " of its run.sh), or plain `<!-- output -->` for a block no example prints.",
            )
        return Marker("output", value)
    if not value or value.startswith("#"):
        need = "a path under docs/examples" if kind == "source" else "a reason"
        _stop(path, number, f"the `{match['kind']}` marker needs {need} after the colon.")
    return Marker(kind, value)


def _closing(lines: list[str], opening: int, fence: str) -> int | None:
    """Find the line that closes a fence.

    Parameters
    ----------
    lines
        The page's lines.
    opening
        The 0-based index of the opening fence.
    fence
        The opening fence's characters.

    Returns
    -------
    int or None
        The 0-based index of the closing fence, or ``None`` if the page ends first.
    """
    for number in range(opening + 1, len(lines)):
        stripped = lines[number].strip()
        if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
            return number
    return None


def find_blocks(path: Path) -> list[Block]:
    """Find the fenced code blocks of a Markdown page.

    A fence may be indented (a block inside a list item), may use backticks or
    tildes, and is closed only by a fence of the same character that is at least as
    long. Fences inside a block are part of its body.

    Parameters
    ----------
    path
        The page.

    Returns
    -------
    list of Block
        The blocks in page order.

    Raises
    ------
    BlockError
        If a fence is never closed, a marker is malformed, misspelled or doubled, or
        the page has a fence in a block quote or a ``<pre>`` element.
    OSError
        If the page cannot be read.
    UnicodeDecodeError
        If the page is not UTF-8 text.
    """
    lines = [line.removesuffix("\r") for line in _read_page(path).lines]
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            if _QUOTED_FENCE.match(lines[index]) or _PRE.match(lines[index]):
                _stop(
                    path,
                    index + 1,
                    "this code block is inside a quote or a `<pre>` element, which cannot be "
                    "checked; use a fenced code block at the start of a line, so it can be "
                    "checked.",
                )
            index += 1
            continue
        indent, fence = opening["indent"], opening["fence"]
        marker = _marker(path, index, lines[index - 1]) if index else None
        if marker is not None and index > 1 and _MARKER.match(lines[index - 2]):
            _stop(
                path,
                index,
                f"two markers sit above this code block (lines {index - 1} and {index}); "
                "keep the one that applies.",
            )
        above = index - 1
        while above >= 0 and not lines[above].strip():
            above -= 1
        if marker is None and above >= 0 and above < index - 1 and _MARKER.match(lines[above]):
            _stop(
                path,
                above + 1,
                "this marker is not directly above its code block; remove the blank "
                "line(s) between them, because a marker only counts on the line above the fence.",
            )
        closing = _closing(lines, index, fence)
        if closing is None:
            _stop(
                path,
                index + 1,
                f"this code block is never closed; add a closing line of "
                f"{fence[0] * len(fence)} (or longer) after it.",
            )
        body = "".join(
            (line[len(indent) :] if line.startswith(indent) else line.lstrip()) + "\n"
            for line in lines[index + 1 : closing]
        )
        info = opening["info"].strip().split()
        blocks.append(
            Block(
                path=path,
                line=index + 1,
                lang=info[0].lower() if info else "",
                body=body,
                marker=marker,
                indent=indent,
                fence=fence,
                start=index + 1,
                end=closing,
            )
        )
        index = closing + 1
    return blocks


def syntax_problem(lang: str, body: str) -> str | None:
    """Parse a block in one of the parsed languages.

    Parameters
    ----------
    lang
        The block's language; one of `PARSED`.
    body
        The block's content.

    Returns
    -------
    str or None
        The parser's message, or ``None`` if the content parses.

    Raises
    ------
    ValueError
        If `lang` is not a language this module can parse.
    """
    try:
        if lang in ("python", "py"):
            ast.parse(body)
        elif lang == "toml":
            tomllib.loads(body)
        elif lang in ("yaml", "yml"):
            list(yaml.safe_load_all(body))
        elif lang == "json":
            json.loads(body)
        else:
            raise ValueError(f"cannot parse {lang!r}")
    except (SyntaxError, tomllib.TOMLDecodeError, yaml.YAMLError, json.JSONDecodeError) as exc:
        return (str(exc).splitlines() or [type(exc).__name__])[0]
    return None


EXAMPLES = "docs/examples"
SITE = "docs/site"
_REGION = re.compile(r"docs:(?P<edge>start|end)\s+(?P<name>[A-Za-z0-9_-]+)\s*(?:-->|\*/)?\s*$")
_WAYS = (
    "Fix it one of these ways: put `<!-- source: docs/examples/<file> -->` above it and keep "
    "it equal to that tested file; write it in a language this check parses "
    "(python, toml, yaml, json); put `<!-- output -->` above it if it shows output; or put "
    "`<!-- not tested: <reason> -->` above it."
)


def read_source(root: Path, value: str) -> str:
    """Read the file or region a ``source`` marker names.

    A region is the lines between ``docs:start <name>`` and ``docs:end <name>``
    (in any comment syntax). Delimiter lines are never shown, whether the source is
    the whole file or a region. The result always ends in a newline, as a block's
    body does, a leading byte-order mark is ignored, and CRLF becomes a line feed;
    only a line feed ends a line.

    Parameters
    ----------
    root
        The repository root.
    value
        The marker's value: a path under ``docs/examples``, optionally ``#region``.

    Returns
    -------
    str
        The content a synced block must equal.

    Raises
    ------
    BlockError
        If the path is outside the examples, is or passes through a symbolic link,
        is a directory, does not exist, cannot be read, has a name that is too long,
        is not UTF-8 text, or has no such region or one whose delimiters are
        unbalanced.
    """
    name, separator, region = value.partition("#")
    parts = Path(name).parts
    if (
        Path(name).is_absolute()
        or ".." in parts
        or parts[:2] != ("docs", "examples")
        or len(parts) < 3
    ):
        raise BlockError(
            f"a source must be under {EXAMPLES}, not `{value}`; "
            f"write the path from the repository root, for example `{EXAMPLES}/<example>/<file>`"
        )
    if separator and not region:
        raise BlockError(f"`{value}` names no region after the `#`; remove the `#` or add the name")
    if any(len(part.encode()) > 255 for part in parts):
        raise BlockError(
            f"`{name}` has a file or directory name longer than 255 bytes, which no file "
            "system allows; check the path in the marker"
        )
    try:
        here = root
        for part in parts:
            here = here / part
            if here.is_symlink():
                raise BlockError(
                    f"`{name}` is or passes through a symbolic link; point at the real file"
                )
        if here.is_dir():
            raise BlockError(f"`{name}` is a directory; a source must name a file in it")
        if not here.is_file():
            raise BlockError(
                f"`{name}` does not exist; check the path, which starts at the repository root"
            )
        raw = here.read_bytes()
    except OSError as exc:
        raise BlockError(
            f"`{name}` cannot be read ({exc.strerror}); check the path and its permissions"
        ) from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BlockError(f"`{name}` is not UTF-8 text; a source must be a text file") from None
    lines = _split(text.removeprefix("\ufeff"))
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    # A page line loses a carriage return before its line feed; a source line must too,
    # or a source with one could never match its block.
    lines = [line[:-1].rstrip("\r") + "\n" for line in lines]
    if not region:
        return "".join(line for line in lines if _REGION.search(line.rstrip("\n")) is None)
    return _region(name, region, lines)


def _region(name: str, region: str, lines: list[str]) -> str:
    """Cut one named region out of a file's lines.

    Parameters
    ----------
    name
        The file's path, for the error messages.
    region
        The region's name.
    lines
        The file's lines, each ending in a newline.

    Returns
    -------
    str
        The lines between the region's delimiters.

    Raises
    ------
    BlockError
        If the region is missing, starts or ends twice, or ends before it starts or never.
    """
    kept: list[str] = []
    started = ended = False
    for line in lines:
        edge = _REGION.search(line.rstrip("\n"))
        if edge is None:
            if started and not ended:
                kept.append(line)
        elif edge["name"] != region:
            continue
        elif edge["edge"] == "start":
            if started:
                raise BlockError(
                    f"`{name}`: the region `{region}` starts twice; "
                    "give each region one start and one end"
                )
            started = True
        elif not started:
            raise BlockError(
                f"`{name}`: the region `{region}` ends before it starts; "
                f"put `docs:start {region}` first"
            )
        elif ended:
            raise BlockError(
                f"`{name}`: the region `{region}` ends twice; "
                "give each region one start and one end"
            )
        else:
            ended = True
    if not started:
        raise BlockError(
            f"`{name}` has no region `{region}`; add the lines `docs:start {region}` and "
            f"`docs:end {region}` in the file's comment syntax"
        )
    if not ended:
        raise BlockError(
            f"`{name}`: the region `{region}` never ends; add a line `docs:end {region}`"
        )
    return "".join(kept)


_HEX = r"[0-9a-f]"
MASKS: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (name, re.compile(pattern), replacement)
    for name, pattern, replacement in (
        (
            "uuid",
            rf"(?<![0-9A-Za-z]){_HEX}{{8}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{4}}-{_HEX}{{12}}(?![0-9A-Za-z])",
            "<uuid>",
        ),
        (
            "timestamp",
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            "<timestamp>",
        ),
        ("hash", rf"(?<![0-9A-Za-z]){_HEX}{{40,64}}(?![0-9A-Za-z])", "<hash>"),
        (
            "token",
            r"\b(lakana_(?:pat|ci|task)_)[A-Za-z0-9]{6,}_[A-Za-z0-9_-]{8,}",
            r"\1<key id>_<secret>",
        ),
        ("port", r"\b(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d{2,5}\b", r"\1:<port>"),
        (
            "path",
            r"(?:(?<=file://)|(?<![\w.:/-]))"
            r"/(?:tmp|var/tmp|var/folders|home|Users|root|run/user)/[^\s'\"`<>),;]*",
            "<path>",
        ),
        (
            "version",
            r"(?<![\w.])\d+\.\d+\.\d+(?:[.-]?(?:dev|post|a|b|rc)\d+)*"
            r"(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?(?![\w]|\.\d)",
            "<version>",
        ),
        (
            "duration",
            r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds?|min|minutes?)\b",
            "<duration>",
        ),
        ("size", r"(?<![\w.])\d+(?:\.\d+)?\s?(?:KiB|MiB|GiB|KB|MB|GB)\b", "<size>"),
    )
)
"""The parts of an output that differ on every run, hidden before a comparison.

Each rule is ``(name, pattern, replacement)``, applied in this order to the page and to the
captured text alike, so a page that shows a real identifier and a run that prints another still
match. A rule is one row of the table in ``docs/agent/testing.md``; keep the two equal.
"""


def mask(text: str) -> str:
    """Hide the parts of an output that differ on every run.

    Parameters
    ----------
    text
        Output as a page shows it or as an example printed it.

    Returns
    -------
    str
        `text` with every match of `MASKS` replaced by its placeholder.
    """
    for _, pattern, replacement in MASKS:
        text = pattern.sub(replacement, text)
    return text


def _lines(text: str) -> list[str]:
    """Split output into lines whose trailing space and final blank lines do not count.

    Parameters
    ----------
    text
        Output as a page shows it or as an example printed it.

    Returns
    -------
    list of str
        The lines without trailing white space; trailing empty lines are dropped.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _captured_text(captured: Path | None, value: str) -> str | None:
    """Read what the runner captured for ``example#region``.

    Parameters
    ----------
    captured
        The capture directory, or ``None`` for no run.
    value
        The marker's ``example#region``.

    Returns
    -------
    str or None
        The text, or ``None`` if there is no run or no file for the region.
    """
    example, _, region = value.partition("#")
    if captured is None:
        return None
    try:
        return (captured / example / f"{region}.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _merge(shown: str, printed: str) -> str:
    """Bring a block up to date with a run, keeping the page's own values.

    A line that equals the printed one after masking stays as the page has it, so a fixed
    block keeps the identifiers a page chose to show; every other line comes from the run.

    Parameters
    ----------
    shown
        The block's body.
    printed
        What the example printed.

    Returns
    -------
    str
        The new body, each line ending in a newline.
    """
    page, run = _lines(shown), _lines(printed)
    matcher = difflib.SequenceMatcher(None, [mask(x) for x in page], [mask(x) for x in run])
    out: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        out += page[i1:i2] if tag == "equal" else run[j1:j2]
    return "".join(line + "\n" for line in out)


def _output_problem(
    where: str, block: Block, value: str, printed: str, captured: Path
) -> str | None:
    """Compare an output block with the text its region printed.

    Parameters
    ----------
    where
        ``page:line`` for the message.
    block
        An output block that names a region.
    value
        The region it names, ``example#region``.
    printed
        What the example printed.
    captured
        The capture directory, for the message.

    Returns
    -------
    str or None
        The problem with a diff of the masked texts, or ``None`` if they are equal.
    """
    page = [mask(x) for x in _lines(block.body)]
    run = [mask(x) for x in _lines(printed)]
    if page == run:
        return None
    diff = "\n".join(
        difflib.unified_diff(page, run, "the page", f"the run ({value})", lineterm="", n=2)
    )
    return (
        f"{where}: this output block differs from what {value} printed "
        f"(parts that differ per run are masked):\n{diff}\nRun the examples with "
        f"LAKANA_DOCS_CAPTURE={captured} and `uv run python scripts/docs_blocks.py --fix "
        f"--captured {captured}` to copy the run into the page, or change the CLI back."
    )


def _site_problem(root: Path, site: Path) -> str | None:
    """Say why the site directory may not be read or written.

    Parameters
    ----------
    root
        The repository root.
    site
        The site directory.

    Returns
    -------
    str or None
        The problem, or ``None`` if `site` is a real directory inside `root`.
    """
    try:
        relative = site.relative_to(root)
    except ValueError:
        relative = None
    if relative is None or ".." in relative.parts or not relative.parts:
        return (
            f"{site} is not inside the repository root {root}; pass the repository root as --root"
        )
    here = root
    for part in relative.parts:
        here = here / part
        if here.is_symlink():
            return (
                f"{relative} is or passes through a symbolic link "
                f"({here.relative_to(root)}); the check follows none, so use a real directory"
            )
    if not site.is_dir():
        return (
            f"{relative} does not exist under {root}; "
            "run this from the repository root or pass it as --root"
        )
    return None


def _pages(root: Path, site: Path) -> tuple[list[Path], list[str]]:
    """List the Markdown pages of the site and the links the walk refused.

    Parameters
    ----------
    root
        The repository root.
    site
        The site directory.

    Returns
    -------
    tuple of (list of Path, list of str)
        The sorted real ``.md`` files, and a problem for each symbolic link to a
        page or directory, none of which is followed.
    """
    pages: list[Path] = []
    problems: list[str] = []
    for directory, dirnames, filenames in os.walk(site):
        dirnames.sort()
        for entry in dirnames:
            link = Path(directory) / entry
            if link.is_symlink():
                problems.append(
                    f"{link.relative_to(root)} is a symbolic link; "
                    "the check follows none, so move the pages into a real directory"
                )
        for entry in sorted(filenames):
            path = Path(directory) / entry
            if not entry.endswith(".md"):
                continue
            if path.is_symlink():
                problems.append(
                    f"{path.relative_to(root)} is a symbolic link; "
                    "the check follows none, so replace it with the real page"
                )
            else:
                pages.append(path)
    return pages, problems


def _split(text: str) -> list[str]:
    """Split text into lines that keep their line feed.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    list of str
        The lines; only a line feed ends one.
    """
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _first_difference(shown: str, source: str) -> str:
    """Describe where a block and its source part ways.

    Parameters
    ----------
    shown
        The block's content.
    source
        The source's content; it must differ from `shown`.

    Returns
    -------
    str
        The first differing line of each, for the message.
    """
    pairs = zip_longest(_split(shown), _split(source), fillvalue="<nothing>")
    number, left, right = next((n, a, b) for n, (a, b) in enumerate(pairs, 1) if a != b)
    return (
        f"first difference at line {number} of the block: "
        f"the page has {left!r}, the file has {right!r}"
    )


def _tied_problem(
    root: Path, where: str, block: Block, captured: Path | None, tally: dict[str, int]
) -> str | None:
    """Judge an output block against the region it names.

    Parameters
    ----------
    root
        The repository root.
    where
        ``page:line`` for the message.
    block
        An output block.
    captured
        The capture directory, or ``None`` for no run.
    tally
        The counts of `check`; one of ``unchecked``, ``skipped`` or ``compared`` rises.

    Returns
    -------
    str or None
        The problem, or ``None`` if the block is not tied, is skipped or matches.
    """
    value = block.marker.value if block.marker else ""
    if not value:
        tally["unchecked"] += 1
        return None
    example, _, region = value.partition("#")
    try:
        read_source(root, f"{EXAMPLES}/{example}/run.sh#{region}")
    except BlockError as exc:
        return f"{where}: {exc}"
    if captured is None:
        tally["skipped"] += 1
        return None
    printed = _captured_text(captured, value)
    if printed is None:
        return (
            f"{where}: {captured} holds no capture of {value}. The example did not run that "
            f"region (it stops at its first failing command; see the runner's output), or the "
            f"capture is from before the region was added; run "
            f"`LAKANA_DOCS_CAPTURE={captured} uv run bash scripts/run_docs_examples.sh {example}`."
        )
    tally["compared"] += 1
    return _output_problem(where, block, value, printed, captured)


def check(
    root: Path,
    site: Path,
    counts: dict[str, int] | None = None,
    captured: Path | None = None,
) -> tuple[list[str], int]:
    """Judge every code block of the site.

    Parameters
    ----------
    root
        The repository root.
    site
        The site directory.
    counts
        If given, set to the number of blocks marked as output (``output``), of those not
        tied to an example region (``unchecked``), compared with a run (``compared``) and
        tied but not compared for want of a run (``skipped``).
    captured
        The directory the example runner captured the regions into, or ``None`` for no run.

    Returns
    -------
    tuple of (list of str, int)
        The problems, and the number of blocks marked as not tested.

    Notes
    -----
    A synced block must equal its source exactly: a trailing space or a missing
    final newline is a difference. Line endings are the one exception; a page
    with CRLF equals a source with LF.
    """
    tally = dict.fromkeys(("output", "unchecked", "compared", "skipped"), 0)
    if counts is not None:
        counts.update(tally)
    if (refusal := _site_problem(root, site)) is not None:
        return [refusal], 0
    pages, problems = _pages(root, site)
    not_tested = 0
    for page in pages:
        shown = page.relative_to(root)
        try:
            blocks = find_blocks(page)
        except BlockError as exc:
            problems.append(str(exc).replace(str(page), str(shown)))
            continue
        except UnicodeDecodeError:
            problems.append(f"{shown}: the page is not UTF-8 text; save it as UTF-8")
            continue
        except OSError as exc:
            problems.append(f"{shown}: cannot read it ({exc.strerror}); check its permissions")
            continue
        for block in blocks:
            where = f"{shown}:{block.line}"
            marker = block.marker
            if marker is None:
                if block.lang not in PARSED:
                    problems.append(
                        f"{where}: this `{block.lang or 'plain'}` block is not checked. {_WAYS}"
                    )
                elif (message := syntax_problem(block.lang, block.body)) is not None:
                    problems.append(f"{where}: this {block.lang} block does not parse: {message}")
            elif marker.kind == "not-tested":
                not_tested += 1
            elif marker.kind == "output":
                if block.lang in OUTPUT_LANGUAGES:
                    tally["output"] += 1
                    problem = _tied_problem(root, where, block, captured, tally)
                    if problem is not None:
                        problems.append(problem)
                else:
                    problems.append(
                        f"{where}: the `output` marker is for what a command printed, in a "
                        f"`text` or `console` block (or one with no language), not for a "
                        f"`{block.lang}` block. Change the block's language to `text` if it "
                        "is output; otherwise sync it from a tested file, or mark it "
                        "`<!-- not tested: <reason> -->`."
                    )
            else:
                try:
                    source = read_source(root, marker.value)
                except BlockError as exc:
                    problems.append(f"{where}: {exc}")
                    continue
                if block.body != source:
                    problems.append(
                        f"{where}: this block differs from {marker.value} "
                        f"({_first_difference(block.body, source)}). Run "
                        "`uv run python scripts/docs_blocks.py --fix` to copy the file "
                        "into the page, "
                        "or change the file and run its example."
                    )
    if counts is not None:
        counts.update(tally)
    return problems, not_tested


def _wanted(root: Path, block: Block, captured: Path | None) -> str | None:
    """Say what a block should hold according to its marker.

    Parameters
    ----------
    root
        The repository root.
    block
        A block of a page.
    captured
        The capture directory, or ``None`` for no run.

    Returns
    -------
    str or None
        The new body of a synced block, or of an output block that names a region with a
        capture; ``None`` if the block is none of these or its source cannot be read.
    """
    marker = block.marker
    if marker is None:
        return None
    if marker.kind == "output" and marker.value:
        printed = _captured_text(captured, marker.value)
        return None if printed is None else _merge(block.body, printed)
    if marker.kind != "source":
        return None
    try:
        return read_source(root, marker.value)
    except BlockError:
        return None


def fix(
    root: Path, site: Path, failures: list[str] | None = None, captured: Path | None = None
) -> list[Path]:
    """Rewrite every synced block from its source, and every named output block from a run.

    Only real pages inside a real `site` directory are written; a page that cannot
    be read, a source that cannot be read and a link are skipped for `check` to
    report. The page's own line endings and byte-order mark are kept.

    Parameters
    ----------
    root
        The repository root.
    site
        The site directory; nothing outside it is written.
    failures
        If given, a problem line is appended for each page that could not be written.
    captured
        The directory the example runner captured the regions into; without it no output
        block is touched. Only the lines that differ after masking are replaced.

    Returns
    -------
    list of Path
        The pages that changed.
    """
    if _site_problem(root, site) is not None:
        return []
    changed: list[Path] = []
    for page in _pages(root, site)[0]:
        try:
            blocks = find_blocks(page)
            content = _read_page(page)
        except (BlockError, UnicodeDecodeError, OSError):
            continue
        lines = list(content.lines)
        for block in reversed(blocks):
            source = _wanted(root, block, captured)
            if source is None:
                continue
            ending = "\r" if lines[block.start - 1].endswith("\r") else ""
            lines[block.start : block.end] = [
                (block.indent + line if line else line) + ending for line in source.split("\n")[:-1]
            ]
        if lines == content.lines:
            continue
        text = ("\ufeff" if content.bom else "") + "\n".join(lines)
        text += "\n" if content.final_newline else ""
        try:
            with page.open("w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            if failures is not None:
                failures.append(
                    f"{page.relative_to(root)}: cannot write it ({exc.strerror}); make it "
                    "writable and run --fix again"
                )
            continue
        changed.append(page)
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    """Check, and with ``--fix`` first repair, the site's code blocks.

    Parameters
    ----------
    argv
        The command line without the program name; ``sys.argv[1:]`` if ``None``.

    Returns
    -------
    int
        ``1`` if a problem remains, else ``0``.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true", help="copy each source file into its block first"
    )
    parser.add_argument(
        "--captured",
        type=Path,
        default=os.environ.get("LAKANA_DOCS_CAPTURE") or None,
        help="the directory scripts/run_docs_examples.sh captured the regions into "
        "(default: $LAKANA_DOCS_CAPTURE); without it output blocks are not compared with a run",
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1], help="the repository root"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    site = root / SITE
    problems: list[str] = []
    captured: Path | None = args.captured
    if captured is not None and not captured.is_dir():
        problems.append(
            f"{captured} is not a directory; run `LAKANA_DOCS_CAPTURE={captured} uv run bash "
            "scripts/run_docs_examples.sh` first, or leave --captured out"
        )
        captured = None
    if args.fix:
        for page in fix(root, site, problems, captured):
            print(f"updated {page.relative_to(root)}")
    counts: dict[str, int] = {}
    found, not_tested = check(root, site, counts, captured)
    problems += found
    for problem in problems:
        print(problem)
    blocks = "block" if not_tested == 1 else "blocks"
    count = "problem" if len(problems) == 1 else "problems"
    outputs = counts["output"]
    kind = "block" if outputs == 1 else "blocks"
    print(
        f"{not_tested} {blocks} marked as not tested, {outputs} {kind} marked as output; "
        f"{len(problems)} {count}."
    )
    if counts["compared"]:
        compared = "output block" if counts["compared"] == 1 else "output blocks"
        print(
            f"{counts['compared']} {compared} compared with a run, {counts['skipped']} skipped, "
            f"{counts['unchecked']} not tied to a region."
        )
    elif counts["skipped"]:
        print(
            f"{counts['skipped']} output blocks not compared: no captured run. Run "
            "`LAKANA_DOCS_CAPTURE=DIR uv run bash scripts/run_docs_examples.sh`, then this "
            "script with --captured DIR (or the same variable)."
        )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
