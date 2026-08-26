"""A restrictive, subprocess-free reader for Git configuration files.

The Git-custody guard compares repository metadata before and after an
untrusted phase. Under ``workspace.strategy: worktree`` the watched config
file is the *parent* repository's, which the developer edits for their own
reasons, so a byte comparison reports benign edits as custody violations
(issue #16).

Narrowing that comparison means understanding which keys changed, and that
has to happen without running ``git config``: the guard runs before the first
Git subprocess precisely so a planted ``core.fsmonitor`` or clean filter never
gets a chance to execute. So this module parses the file itself, and refuses
to guess. Any construct it cannot read with confidence yields ``None`` and the
caller falls back to the strict byte comparison.
"""

from __future__ import annotations

import hashlib
import re

_SECTION = re.compile(r'^\[([A-Za-z0-9-]+)(?:\s+"((?:[^"\\]|\\[\\"])*)")?\]$')
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?$")

# Sections whose every key can name a program, rewrite a URL, or pull in
# another config file.
_SENSITIVE_SECTIONS = frozenset(
    {
        "alias",
        "browser",
        "credential",
        "extensions",
        "filter",
        "guitool",
        "http",
        "include",
        "includeif",
        "init",
        "instaweb",
        "man",
        "protocol",
        "receivepack",
        "safe",
        "sendemail",
        "ssh",
        "trace2",
        "uploadpack",
        "url",
    }
)

# Key names that name a program, a filesystem location Git will trust, or a
# transport, in whatever section they appear.
_SENSITIVE_LEAVES = frozenset(
    {
        "alternaterefscommand",
        "askpass",
        "attributesfile",
        "bare",
        "clean",
        "cmd",
        "command",
        "driver",
        "editor",
        "excludesfile",
        "external",
        "fsmonitor",
        "gitproxy",
        "helper",
        "hookspath",
        "pager",
        "path",
        "process",
        "program",
        "proxy",
        "pushinsteadof",
        "pushurl",
        "receivepack",
        "reference",
        "repositoryformatversion",
        "smudge",
        "sshcommand",
        "templatedir",
        "textconv",
        "uploadpack",
        "worktree",
    }
)


# Adding a second remote is routine; repointing the controller's origin is an
# attack. Other remotes' URLs stay inert because the controller never fetches
# or pushes by remote name.
_SENSITIVE_KEYS = frozenset({"remote.origin.url"})


def parse_git_config(text: str) -> list[tuple[str, str]] | None:
    """Return ``[(dotted_key, raw_value)]``, or ``None`` if unsure.

    Keys and section names are lowercased the way Git compares them;
    subsection names keep their case. Values are returned as written, with
    surrounding whitespace and any trailing comment removed, because the
    caller compares them rather than interpreting them.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None

    entries: list[tuple[str, str]] = []
    prefix: str | None = None
    for raw_line in text.splitlines():
        if raw_line.rstrip().endswith("\\"):
            return None  # line continuation
        line = _strip_comment(raw_line)
        if line is None:
            return None  # unbalanced quotes
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            header = _SECTION.match(line)
            if header is None:
                return None
            section, subsection = header.group(1), header.group(2)
            if "." in section:
                return None  # deprecated [section.subsection] form
            prefix = section.lower()
            if subsection is not None:
                prefix = f"{prefix}.{_unescape_subsection(subsection)}"
            continue
        if prefix is None:
            return None  # key outside any section
        pair = _KEY.match(line)
        if pair is None:
            return None
        name, value = pair.group(1), pair.group(2)
        entries.append(
            (f"{prefix}.{name.lower()}", "true" if value is None else value.strip())
        )
    return entries


def is_sensitive_key(key: str) -> bool:
    """Report whether a dotted key can execute code or redirect the network."""
    if key in _SENSITIVE_KEYS:
        return True
    section, _, remainder = key.partition(".")
    leaf = remainder.rsplit(".", 1)[-1]
    return section in _SENSITIVE_SECTIONS or leaf in _SENSITIVE_LEAVES


def sensitive_key_digests(text: str) -> dict[str, str] | None:
    """Map each sensitive key to a digest of its values, or ``None`` if unsure.

    Only key *names* survive in the clear. Config values routinely hold
    credentials, and this map is persisted in Task Run records, so values are
    hashed rather than stored. Git allows a key more than once, so values are
    sorted before hashing: the guard cares that the set changed, not about
    file order.
    """
    entries = parse_git_config(text)
    if entries is None:
        return None
    grouped: dict[str, list[str]] = {}
    for key, value in entries:
        if is_sensitive_key(key):
            grouped.setdefault(key, []).append(value)
    return {
        key: hashlib.sha256("\0".join(sorted(values)).encode()).hexdigest()
        for key, values in grouped.items()
    }


def changed_sensitive_keys(
    before: dict[str, str], after: dict[str, str]
) -> tuple[str, ...]:
    """Return the sensitive keys that were added, removed, or altered."""
    return tuple(
        sorted(
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        )
    )


def _strip_comment(line: str) -> str | None:
    """Drop a ``#``/``;`` comment that starts outside a quoted value."""
    quoted = False
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\" and quoted:
            index += 2
            continue
        if character == '"':
            quoted = not quoted
        elif character in "#;" and not quoted:
            return line[:index]
        index += 1
    return None if quoted else line


def _unescape_subsection(subsection: str) -> str:
    return subsection.replace('\\"', '"').replace("\\\\", "\\")
