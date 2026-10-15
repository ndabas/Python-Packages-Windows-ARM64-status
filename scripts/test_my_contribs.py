"""Offline tests for the post-merge comment checks."""

import contextlib
import io
import unittest
from unittest.mock import patch

import github_cli
import my_contribs


def item(repo: str, number: int, created_at: str, merged_at: str | None = None) -> dict:
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "title": f"Add win_arm64 | {number}",
        "created_at": created_at,
        "pull_request": {"merged_at": merged_at},
    }


class PostMergeCommentTests(unittest.TestCase):
    def test_only_others_comments_after_merge_are_kept(self) -> None:
        pr = item("o/r", 7, "2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z")
        responses = {
            "repos/o/r/issues/7/comments": [
                {"user": {"login": "alice"}, "created_at": "2026-09-09T00:00:00Z",
                 "body": "before", "html_url": "u1"},
                {"user": {"login": "NDabas"}, "created_at": "2026-09-11T00:00:00Z",
                 "body": "mine", "html_url": "u2"},
                {"user": {"login": "bob"}, "created_at": "2026-09-12T00:00:00Z",
                 "body": "This broke\n\nthe build", "html_url": "u3"},
            ],
            "repos/o/r/pulls/7/comments": [
                {"user": {"login": "carol"}, "created_at": "2026-09-11T00:00:00Z",
                 "body": "nit", "html_url": "u4", "path": "setup.py"},
            ],
            "repos/o/r/pulls/7/reviews": [
                {"user": {"login": "carol"}, "submitted_at": "2026-09-11T00:00:00Z",
                 "body": "", "html_url": "u5"},
                {"user": {"login": "dave"}, "submitted_at": None, "body": "pending"},
            ],
        }
        with patch.object(my_contribs, "gh_list", side_effect=responses.__getitem__), \
                contextlib.redirect_stdout(io.StringIO()):
            comments = my_contribs.post_merge_comments(pr, "ndabas", 1, 1)
        self.assertEqual(
            [(c.author, c.kind) for c in comments],
            [("carol", "review comment on `setup.py`"), ("bob", "comment")],
        )

        report = my_contribs.render_comments_report("ndabas", [(pr, comments)])
        self.assertIn("## [o/r#7](https://github.com/o/r/pull/7): Add win_arm64 \\| 7", report)
        self.assertIn("### @bob, 2026-09-12 00:00 UTC ([comment](u3))", report)
        self.assertIn("> This broke\n>\n> the build", report)

    def test_comments_report_lists_latest_activity_first(self) -> None:
        def comment(timestamp: str) -> my_contribs.Comment:
            return my_contribs.Comment("x", my_contribs.parse_time(timestamp), "u", "b", "comment")

        merged = "2026-09-01T00:00:00Z"
        commented = [
            (item("o/a", 1, merged, merged), [comment("2026-09-05T00:00:00Z")]),
            (item("o/b", 2, merged, merged),
             [comment("2026-09-02T00:00:00Z"), comment("2026-10-01T00:00:00Z")]),
            (item("o/c", 3, merged, merged), [comment("2026-09-20T00:00:00Z")]),
        ]
        ordered = my_contribs.latest_activity_first(commented)
        self.assertEqual([my_contribs.pr_label(pr) for pr, _ in ordered],
                         ["o/b#2", "o/c#3", "o/a#1"])
        report = my_contribs.render_comments_report("ndabas", commented)
        positions = [report.index(f"## [o/{name}#") for name in "bca"]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("latest comment 2026-10-01 00:00 UTC", report)

    def test_gh_list_flattens_pages(self) -> None:
        with patch.object(github_cli, "gh", return_value=[[{"a": 1}], [{"a": 2}]]) as gh:
            self.assertEqual(github_cli.gh_list("repos/o/r/issues/1/comments"),
                             [{"a": 1}, {"a": 2}])
        gh.assert_called_once_with(
            "api", "--paginate", "--slurp", "repos/o/r/issues/1/comments"
        )


if __name__ == "__main__":
    unittest.main()
