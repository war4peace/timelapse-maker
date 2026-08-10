"""Tests for timelapse_update.py: the release query and the update command.

The release query moved here out of test_web.py when timelapse_web.py stopped
carrying its own copy. Nothing in this file touches the network: every test
that would reach GitHub patches fetch_json or fetch_text, and one of them
exists specifically to prove that.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (puts scripts/ on sys.path)

import timelapse_update as upd


class TestParseVersion(unittest.TestCase):

    def test_accepts_both_spellings_of_a_tag(self):
        self.assertEqual(upd.parse_version("v0.0.9"), (0, 0, 9))
        self.assertEqual(upd.parse_version("0.0.9"), (0, 0, 9))

    def test_junk_is_none_rather_than_a_guess(self):
        for bad in ("", None, "latest", "v", "main", "0.1"):
            self.assertIsNone(upd.parse_version(bad), bad)

    def test_ordering_is_numeric_not_lexical(self):
        # The whole point: "0.0.10" sorts BELOW "0.0.9" as a string, and the
        # next release after 0.0.9 is exactly that.
        self.assertGreater(upd.parse_version("v0.0.10"),
                           upd.parse_version("v0.0.9"))
        self.assertGreater(upd.parse_version("v0.1.0"),
                           upd.parse_version("v0.0.99"))


class TestChangelogSection(unittest.TestCase):

    SAMPLE = "\n".join([
        "# Changelog", "", "Preamble that belongs to nobody.", "",
        "## [Unreleased]", "- not this one", "",
        "## [0.1.0] - 2026-08-09", "### Fixed", "- the thing", "- and another",
        "", "## [0.0.9] - 2026-08-07", "- older, must not appear", ""])

    def test_extracts_only_the_named_release(self):
        got = upd.changelog_section(self.SAMPLE, "0.1.0")
        self.assertIn("the thing", got)
        self.assertIn("and another", got)
        self.assertNotIn("older", got)
        self.assertNotIn("not this one", got)
        self.assertNotIn("Preamble", got)

    def test_an_absent_version_yields_nothing(self):
        self.assertEqual(upd.changelog_section(self.SAMPLE, "9.9.9"), "")

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(upd.changelog_section("", "0.1.0"), "")


class TestLatestRelease(unittest.TestCase):
    """This repo has nine tags with no GitHub Release behind them, so
    /releases/latest 404d until 0.1.0. An implementation that knew only about
    Releases would report "up to date" forever, on its own project."""

    def test_a_published_release_is_preferred(self):
        with mock.patch.object(upd, "fetch_json", return_value={
                "tag_name": "v1.2.3", "html_url": "u", "body": "notes here"}):
            ver, tag, url, notes = upd.latest_release()
        self.assertEqual((ver, tag, notes), ((1, 2, 3), "v1.2.3", "notes here"))

    def test_falls_back_to_tags_on_404(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", None, None)
        calls = []

        def fake(url, timeout=10):
            calls.append(url)
            if url.endswith("/releases/latest"):
                raise err
            return [{"name": "v0.0.9"}, {"name": "v0.0.10"}, {"name": "v0.0.8"}]

        with mock.patch.object(upd, "fetch_json", fake):
            ver, tag, url, notes = upd.latest_release()
        # Highest, not first: the API's ordering is its own business.
        self.assertEqual((ver, tag), ((0, 0, 10), "v0.0.10"))
        self.assertEqual(notes, "")
        self.assertTrue(any("/tags" in c for c in calls))

    def test_an_http_error_that_is_not_404_propagates(self):
        err = urllib.error.HTTPError("u", 403, "rate limited", None, None)
        with mock.patch.object(upd, "fetch_json", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                upd.latest_release()

    def test_unparseable_tags_are_skipped(self):
        err = urllib.error.HTTPError("u", 404, "Not Found", None, None)

        def fake(url, timeout=10):
            if url.endswith("/releases/latest"):
                raise err
            return [{"name": "nightly"}, {"name": "v0.2.0"}, {"name": "x"}]

        with mock.patch.object(upd, "fetch_json", fake):
            ver, tag, _, _ = upd.latest_release()
        self.assertEqual(tag, "v0.2.0")


class TestFriendlyError(unittest.TestCase):
    """The first operator to hit a failure was shown "URLError: <urlopen error
    [Errno -3] Temporary failure in name resolution>", which says nothing
    about whose fault it is. It is almost never this program's."""

    def test_dns_says_it_is_the_resolver(self):
        got = upd.friendly_error(
            urllib.error.URLError(
                OSError(-3, "Temporary failure in name resolution")))
        self.assertIn("DNS lookup failed", got)
        self.assertIn("your resolver", got)

    def test_rate_limiting_is_named(self):
        got = upd.friendly_error(urllib.error.HTTPError(
            "u", 403, "rate limit exceeded", None, None))
        self.assertIn("rate limiting", got)

    def test_a_timeout_is_named(self):
        self.assertIn("timed out", upd.friendly_error(OSError("timed out")))

    def test_the_raw_text_is_kept_for_searching(self):
        got = upd.friendly_error(urllib.error.URLError("no route to host"))
        self.assertIn("no route to host", got)

    def test_an_unknown_failure_still_reads_as_a_sentence(self):
        got = upd.friendly_error(ValueError("something odd"))
        self.assertIn("update check failed", got.lower())
        self.assertIn("something odd", got)


class TestClipNotes(unittest.TestCase):
    """v0.1.0's release body was 4020 characters against a 4000 cap, so the
    panel cut it three characters into a sentence with nothing to say that it
    had. Cutting on a line boundary and reporting it is the fix."""

    def test_short_notes_pass_through_untouched(self):
        text = "line one\nline two"
        self.assertEqual(upd.clip_notes(text, 100), (text, False))

    def test_a_body_exactly_at_the_limit_is_not_clipped(self):
        # Off-by-one guard: == limit fits, so nothing was lost.
        self.assertEqual(upd.clip_notes("x" * 40, 40), ("x" * 40, False))

    def test_the_cut_lands_on_a_line_boundary(self):
        text = "\n".join(f"- item number {n}" for n in range(40))
        got, clipped = upd.clip_notes(text, 200)
        self.assertTrue(clipped)
        self.assertLessEqual(len(got), 200)
        # Whole lines only: every surviving line is one of the originals.
        for line in got.splitlines():
            self.assertIn(line, text.splitlines())

    def test_one_long_paragraph_falls_back_to_a_word_boundary(self):
        text = " ".join(["word"] * 200)
        got, clipped = upd.clip_notes(text, 100)
        self.assertTrue(clipped)
        self.assertFalse(got.endswith("wor"))       # never mid-word
        self.assertTrue(got.endswith("word"))

    def test_text_with_no_break_at_all_is_still_bounded(self):
        # A limit is a limit. Nothing to cut cleanly on is not a reason to
        # return 10 KB of prose to a page that asked for 100 characters.
        got, clipped = upd.clip_notes("x" * 500, 100)
        self.assertTrue(clipped)
        self.assertEqual(len(got), 100)

    def test_an_early_newline_does_not_trim_almost_everything(self):
        # "title\n" then a wall of text: cutting at that newline would throw
        # away everything the reader came for. The half-limit floor stops it.
        got, _ = upd.clip_notes("title\n" + "y" * 500, 100)
        self.assertGreater(len(got), 50)

    def test_empty_and_none_are_not_errors(self):
        self.assertEqual(upd.clip_notes("", 10), ("", False))
        self.assertEqual(upd.clip_notes(None, 10), ("", False))


class TestFetchInstaller(unittest.TestCase):
    """This downloads a script and runs it as root, so what came back gets
    checked before it is trusted: a 404 page, a captive portal or a proxy's
    error page all arrive as a successful HTTP response."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def fetch(self, text):
        with mock.patch.object(upd, "fetch_text", return_value=text):
            with redirect_stdout(io.StringIO()):
                return upd.fetch_installer("v9.9.9", self.tmp)

    def test_it_downloads_the_installer_for_the_tag_not_for_main(self):
        # The installer unpacks a tarball of the ref it is given; an installer
        # from main can expect files a older tag does not contain.
        with mock.patch.object(upd, "fetch_text",
                               return_value="# timelapse-maker installer\n") as f:
            with redirect_stdout(io.StringIO()):
                upd.fetch_installer("v0.1.0", self.tmp)
        self.assertIn("/v0.1.0/install.sh", f.call_args[0][0])

    def test_a_page_that_is_not_the_installer_is_refused(self):
        with self.assertRaises(ValueError):
            self.fetch("<!doctype html><title>404: Not Found</title>")

    def test_the_installer_is_written_with_unix_line_endings(self):
        # CRLF makes bash read \r as part of the command, and the failure
        # reads as a corrupt download rather than as a line-ending problem.
        path = self.fetch("#!/usr/bin/env bash\n# timelapse-maker installer\n")
        self.assertNotIn(b"\r\n", Path(path).read_bytes())

    def test_it_lands_alone_so_the_installer_still_downloads_a_tree(self):
        # install.sh's obtain_source() treats its own directory as a checkout
        # if scripts/ sits beside it, and would then install whatever tree
        # that is instead of the ref it was asked for.
        path = self.fetch("# timelapse-maker installer\n")
        self.assertEqual(os.listdir(os.path.dirname(path)), ["install.sh"])


class UpdateCLICase(unittest.TestCase):

    def run_main(self, argv, **patches):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.multiple(upd, **patches):
            with redirect_stdout(out), redirect_stderr(err):
                rc = upd.main(argv)
        return rc, out.getvalue() + err.getvalue()

    @staticmethod
    def release(tag="v9.9.9", body="- something new"):
        """A stand-in for latest_release(), which is the network boundary."""
        return mock.MagicMock(return_value=(
            upd.parse_version(tag), tag, f"https://example/{tag}", body))


class TestUpdateCheck(UpdateCLICase):

    def test_up_to_date_says_so_and_exits_zero(self):
        rc, text = self.run_main(
            ["--check", "--current", "9.9.9"],
            latest_release=self.release("v9.9.9", ""))
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date", text)

    def test_an_available_update_reports_status_ten(self):
        # Distinct from 0 so a cron job can notify without a human reading it.
        rc, text = self.run_main(
            ["--check", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value="- something new"))
        self.assertEqual(rc, 10)
        self.assertIn("v9.9.9", text)
        self.assertIn("something new", text)

    def test_check_never_installs(self):
        run = mock.MagicMock()
        rc, _ = self.run_main(
            ["--check", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value=""),
            run_installer=run, fetch_installer=mock.MagicMock())
        self.assertEqual(rc, 10)
        run.assert_not_called()

    def test_check_needs_no_root(self):
        # The point of splitting the privilege check out of the top of main():
        # asking the question should not need sudo, only acting on it.
        with mock.patch.object(os, "geteuid", lambda: 1000, create=True):
            rc, text = self.run_main(
                ["--check", "--current", "0.1.1"],
                latest_release=self.release("v9.9.9"),
                release_notes=mock.MagicMock(return_value=""))
        self.assertEqual(rc, 10)
        self.assertNotIn("needs root", text)

    def test_a_failed_query_is_reported_in_words_and_changes_nothing(self):
        run = mock.MagicMock()
        rc, text = self.run_main(
            ["--check"],
            latest_release=mock.MagicMock(side_effect=urllib.error.URLError(
                OSError(-3, "Temporary failure in name resolution"))),
            run_installer=run)
        self.assertEqual(rc, 1)
        self.assertIn("DNS lookup failed", text)
        self.assertIn("Nothing was changed", text)
        run.assert_not_called()


class TestUpdateInstall(UpdateCLICase):

    def setUp(self):
        # main() refuses to install as a normal user, and the suite must not
        # depend on who is running it.
        p = mock.patch.object(os, "geteuid", lambda: 0, create=True)
        p.start()
        self.addCleanup(p.stop)

    def test_it_installs_the_tag_it_reported(self):
        fetch = mock.MagicMock(return_value="/tmp/x/install.sh")
        run = mock.MagicMock(return_value=0)
        rc, text = self.run_main(
            ["--yes", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value=""),
            fetch_installer=fetch, run_installer=run, check_syntax=lambda p: True)
        self.assertEqual(rc, 0)
        self.assertEqual(fetch.call_args[0][0], "v9.9.9")
        self.assertEqual(run.call_args[0][1], "v9.9.9")
        self.assertIn("Updated to v9.9.9", text)

    def test_an_explicit_ref_skips_the_version_question_entirely(self):
        # The operator has said which one they want, so asking GitHub which is
        # newest and then refusing to go backwards would be arguing with them.
        latest = mock.MagicMock()
        fetch = mock.MagicMock(return_value="/tmp/x/install.sh")
        rc, _ = self.run_main(
            ["--yes", "--ref", "v0.0.9", "--current", "0.1.1"],
            latest_release=latest, fetch_installer=fetch,
            run_installer=mock.MagicMock(return_value=0),
            check_syntax=lambda p: True)
        self.assertEqual(rc, 0)
        latest.assert_not_called()
        self.assertEqual(fetch.call_args[0][0], "v0.0.9")

    def test_being_current_does_not_install_without_force(self):
        run = mock.MagicMock()
        rc, text = self.run_main(
            ["--yes", "--current", "9.9.9"],
            latest_release=self.release("v9.9.9", ""), run_installer=run)
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertIn("Already up to date", text)

    def test_force_reinstalls_the_current_version(self):
        run = mock.MagicMock(return_value=0)
        rc, text = self.run_main(
            ["--yes", "--force", "--current", "9.9.9"],
            latest_release=self.release("v9.9.9", ""),
            fetch_installer=mock.MagicMock(return_value="/tmp/x/install.sh"),
            run_installer=run, check_syntax=lambda p: True)
        self.assertEqual(rc, 0)
        self.assertIn("reinstalling anyway", text)
        run.assert_called_once()

    def test_installing_as_a_normal_user_is_refused_before_downloading(self):
        fetch = mock.MagicMock()
        with mock.patch.object(os, "geteuid", lambda: 1000, create=True):
            rc, text = self.run_main(
                ["--yes", "--current", "0.1.1"],
                latest_release=self.release("v9.9.9"),
                release_notes=mock.MagicMock(return_value=""),
                fetch_installer=fetch, run_installer=mock.MagicMock())
        self.assertEqual(rc, 1)
        self.assertIn("sudo timelapse update", text)
        fetch.assert_not_called()

    def test_a_declined_confirmation_changes_nothing(self):
        run = mock.MagicMock()
        rc, text = self.run_main(
            ["--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value=""),
            ask_yes=lambda q, d=True: False,
            fetch_installer=mock.MagicMock(), run_installer=run)
        self.assertEqual(rc, 0)
        run.assert_not_called()
        self.assertIn("Nothing was changed", text)

    def test_a_syntactically_broken_download_is_never_run(self):
        run = mock.MagicMock()
        rc, _ = self.run_main(
            ["--yes", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value=""),
            fetch_installer=mock.MagicMock(return_value="/tmp/x/install.sh"),
            check_syntax=lambda p: False, run_installer=run)
        self.assertEqual(rc, 1)
        run.assert_not_called()

    def test_a_failing_installer_is_reported_as_a_failure(self):
        rc, text = self.run_main(
            ["--yes", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value=""),
            fetch_installer=mock.MagicMock(return_value="/tmp/x/install.sh"),
            check_syntax=lambda p: True,
            run_installer=mock.MagicMock(return_value=3))
        self.assertEqual(rc, 1)
        self.assertIn("half-upgraded", text)

    def test_yes_is_passed_through_to_the_installer(self):
        # Otherwise 'timelapse update --yes' asks nothing and then hands over
        # to a script that asks five questions.
        with mock.patch.object(upd.subprocess, "call",
                               return_value=0) as call:
            with redirect_stdout(io.StringIO()):
                upd.run_installer("/tmp/x/install.sh", "v9.9.9", True)
        self.assertIn("--unattended", call.call_args[0][0])

    def test_long_release_notes_are_clipped_with_a_pointer_to_the_rest(self):
        rc, text = self.run_main(
            ["--check", "--current", "0.1.1"],
            latest_release=self.release("v9.9.9"),
            release_notes=mock.MagicMock(return_value="- a line\n" * 2000))
        self.assertEqual(rc, 10)
        self.assertIn("notes shortened", text)
        self.assertIn(upd.RELEASES_URL, text)


class TestNoAccidentalNetwork(unittest.TestCase):

    def test_importing_the_module_makes_no_request(self):
        # An import that phoned home would make the whole suite depend on
        # GitHub being reachable, and this module is imported by the web
        # server at startup.
        with mock.patch.object(upd.urllib.request, "urlopen") as urlopen:
            import importlib
            importlib.reload(upd)
        urlopen.assert_not_called()

    def test_the_user_agent_is_set_because_github_rejects_requests_without_one(self):
        # Same trap as Discord behind Cloudflare, different vendor.
        self.assertIn("timelapse-maker", upd.UPDATE_UA)


if __name__ == "__main__":
    unittest.main()
