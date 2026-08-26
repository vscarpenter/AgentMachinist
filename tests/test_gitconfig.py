"""Tests for the restrictive git-config reader used by the custody guard.

The parser never shells out to git: the whole point is to classify a possibly
hostile config file *before* any Git subprocess gives that file a chance to
execute. Anything it cannot parse with confidence returns None so callers fall
back to a strict byte comparison.
"""

import pytest

from machinist.gitconfig import (
    changed_sensitive_keys,
    parse_git_config,
    sensitive_key_digests,
)


def test_parses_plain_sections_and_subsections():
    text = """
[core]
\tfilemode = true
[remote "origin"]
\turl = https://github.com/example/repo.git
"""

    assert parse_git_config(text) == [
        ("core.filemode", "true"),
        ("remote.origin.url", "https://github.com/example/repo.git"),
    ]


def test_lowercases_section_and_key_but_preserves_subsection_case():
    text = '[Remote "MyFork"]\n\tURL = https://example.test/x.git\n'

    assert parse_git_config(text) == [
        ("remote.MyFork.url", "https://example.test/x.git")
    ]


def test_valueless_key_is_boolean_true():
    assert parse_git_config("[core]\n\tbare\n") == [("core.bare", "true")]


def test_strips_comments_outside_quoted_values():
    text = (
        "[core]\n\tpager = less # trailing comment\n"
        "; leading comment\n"
        '[user]\n\tname = "A ; kept"\n'
    )

    assert parse_git_config(text) == [
        ("core.pager", "less"),
        ("user.name", '"A ; kept"'),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "[core]\n\tpager = one \\\n\ttwo\n",  # line continuation
        "[core.subsection]\n\tkey = value\n",  # deprecated dotted subsection
        "key = orphan\n",  # key outside any section
        "[unterminated\n",  # malformed header
        '[remote "unterminated]\n\turl = x\n',  # unterminated subsection quote
    ],
)
def test_returns_none_for_anything_it_cannot_parse_confidently(text):
    assert parse_git_config(text) is None


def test_returns_none_for_undecodable_bytes():
    assert parse_git_config("[core]\n\tname = \udcff\n") is None


BEFORE = (
    '[core]\n\tfilemode = true\n[remote "origin"]\n\turl = https://example.test/a.git\n'
)


def _changed(after: str, before: str = BEFORE) -> tuple[str, ...]:
    old, new = sensitive_key_digests(before), sensitive_key_digests(after)
    assert old is not None and new is not None
    return changed_sensitive_keys(old, new)


@pytest.mark.parametrize(
    "added",
    [
        "[diff]\n\ttool = vimdiff\n",
        "[user]\n\tname = Someone Else\n\temail = other@example.test\n",
        '[remote "upstream"]\n\turl = https://example.test/b.git\n\tfetch = +refs/heads/*:refs/remotes/upstream/*\n',
        '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
        "[gc]\n\tauto = 0\n",
        "[push]\n\tdefault = simple\n",
        '[remote "origin"]\n\tgh-resolved = base\n',
    ],
)
def test_inert_edits_report_no_sensitive_change(added):
    assert _changed(BEFORE + added) == ()


@pytest.mark.parametrize(
    ("added", "expected_key"),
    [
        ("[core]\n\tfsmonitor = /tmp/evil\n", "core.fsmonitor"),
        ("[core]\n\thooksPath = /tmp/hooks\n", "core.hookspath"),
        ("[core]\n\tpager = /tmp/evil\n", "core.pager"),
        ("[core]\n\tsshCommand = /tmp/evil\n", "core.sshcommand"),
        ('[filter "lfs"]\n\tclean = /tmp/evil\n', "filter.lfs.clean"),
        ('[diff "x"]\n\tcommand = /tmp/evil\n', "diff.x.command"),
        ("[alias]\n\tst = !/tmp/evil\n", "alias.st"),
        ("[credential]\n\thelper = /tmp/evil\n", "credential.helper"),
        (
            '[url "https://evil.test/"]\n\tinsteadOf = https://github.com/\n',
            "url.https://evil.test/.insteadof",
        ),
        ("[include]\n\tpath = /tmp/evil\n", "include.path"),
        ("[core]\n\tworktree = /tmp/elsewhere\n", "core.worktree"),
        ('[remote "origin"]\n\tuploadpack = /tmp/evil\n', "remote.origin.uploadpack"),
    ],
)
def test_execution_and_network_keys_report_a_sensitive_change(added, expected_key):
    assert expected_key in _changed(BEFORE + added)


def test_modifying_an_existing_sensitive_value_is_reported():
    before = BEFORE + "[core]\n\tpager = less\n"
    after = BEFORE + "[core]\n\tpager = /tmp/evil\n"

    assert _changed(after, before) == ("core.pager",)


def test_removing_a_sensitive_key_is_reported():
    before = BEFORE + "[core]\n\tfsmonitor = /usr/bin/watchman\n"

    assert _changed(BEFORE, before) == ("core.fsmonitor",)


def test_unparsable_config_yields_no_digest_map():
    """Fail closed: callers must treat None as 'cannot be judged benign'."""
    assert sensitive_key_digests("[core.subsection]\n\tkey = value\n") is None


def test_digest_map_hashes_values_and_keeps_key_names_readable():
    digests = sensitive_key_digests(BEFORE + "[core]\n\tpager = /tmp/secret-path\n")

    assert digests is not None
    assert "core.pager" in digests
    assert "secret-path" not in str(digests)


def test_reported_keys_are_sorted_and_deduplicated():
    after = BEFORE + "[core]\n\tpager = /tmp/a\n\tfsmonitor = /tmp/b\n"

    assert _changed(after) == ("core.fsmonitor", "core.pager")


def test_adding_a_second_remote_is_inert_but_repointing_origin_is_not():
    upstream = '[remote "upstream"]\n\turl = https://example.test/b.git\n'
    repointed = BEFORE.replace("example.test/a.git", "evil.test/a.git")

    assert _changed(BEFORE + upstream) == ()
    assert _changed(repointed) == ("remote.origin.url",)
