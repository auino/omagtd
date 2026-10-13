"""Tests for the GTD engine.

Run them with:

    ./tests/run            # or: python3 -m unittest discover -s tests

Everything runs against a throwaway database in a temp directory, so nothing
here touches the real one.
"""

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

from gtd import core, dates, db as db_module, importer, quick, review  # noqa: E402
from gtd.core import GtdError  # noqa: E402
from gtd.db import Store  # noqa: E402


class TempStore(unittest.TestCase):
    """Gives every test its own database, with a frozen "today"."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="gtd-test-")
        self._previous = os.environ.get("GTD_HOME")
        os.environ["GTD_HOME"] = self.home
        self.store = Store()
        self.addCleanup(self._close)

    def _close(self):
        self.store.close()
        if self._previous is None:
            os.environ.pop("GTD_HOME", None)
        else:
            os.environ["GTD_HOME"] = self._previous
        shutil.rmtree(self.home, ignore_errors=True)

    def capture(self, text, **kwargs):
        return core.capture(self.store, text, **kwargs)


# ----------------------------------------------------------------- dates


class Dates(unittest.TestCase):
    def test_absolute_and_relative(self):
        today = dt.date.today()
        self.assertEqual(dates.parse_when("today"), dates.parse_when(today.isoformat()))
        self.assertEqual(dates.parse_when("tomorrow").date(),
                         today + dt.timedelta(days=1))
        self.assertEqual(dates.parse_when("+3d").date(), today + dt.timedelta(days=3))
        self.assertEqual(dates.parse_when("in 2 weeks").date(),
                         today + dt.timedelta(days=14))
        self.assertEqual(dates.parse_when("2026-12-25").date(), dt.date(2026, 12, 25))

    def test_named_days_roll_forward(self):
        today = dt.date.today()
        friday = dates.parse_when("friday")
        self.assertEqual(friday.weekday(), 4)
        self.assertGreaterEqual(friday.date(), today)

    def test_rejects_nonsense(self):
        for text in ("", "next", "someday", "0x", "not a date"):
            self.assertIsNone(dates.parse_when(text), text)

    def test_yearless_dates_choose_the_next_occurrence(self):
        today = dt.date.today()
        parsed = dates.parse_when("5 oct")
        self.assertEqual((parsed.month, parsed.day), (10, 5))
        self.assertGreaterEqual(parsed.date(), today)
        self.assertLess(parsed.date(), today.replace(year=today.year + 1)
                        + dt.timedelta(days=1))
        self.assertEqual(parsed.hour, dates.default_hm()[0])

    def test_bare_dates_land_on_the_configured_default_time(self):
        self.assertEqual(dates.parse_when("tomorrow").strftime("%H:%M"), "18:00")
        self.assertEqual(dates.to_stamp(dt.date(2026, 10, 5)), "2026-10-05 18:00")

        dates.configure(due_time="07:30")
        try:
            self.assertEqual(dates.parse_when("tomorrow").strftime("%H:%M"), "07:30")
            self.assertEqual(dates.to_stamp(dt.date(2026, 10, 5)), "2026-10-05 07:30")
        finally:
            dates.configure(due_time="18:00")

    def test_twelve_hour_clock_round_trips_and_formats(self):
        parsed = dates.parse_when("2026-10-05 6:00 PM")
        self.assertEqual(parsed.strftime("%Y-%m-%d %H:%M"), "2026-10-05 18:00")
        self.assertEqual(dates.parse_when("2026-10-05 6:00PM").hour, 18)

        reference = dt.datetime(2026, 10, 5, 8, 0)
        self.assertEqual(
            dates.human("2026-10-05 20:00", reference), "today 20:00")
        dates.configure(time_format="12h")
        try:
            self.assertEqual(dates.format_time(parsed), "6:00 PM")
            self.assertEqual(
                dates.human("2026-10-05 20:00", reference), "today 8:00 PM")
        finally:
            dates.configure(time_format="24h")

    def test_add_unit_and_anchors(self):
        base = dt.datetime(2026, 1, 31, 9, 0)
        self.assertEqual(dates.add_unit(base, 1, "month").month, 2)
        self.assertEqual(dates.add_unit(base, 1, "year").year, 2027)
        self.assertEqual(dates.add_unit(base, 2, "week").day, 14)

    def test_durations(self):
        self.assertEqual(dates.parse_duration("every week"), (1, "week"))
        self.assertEqual(dates.parse_duration("every 2 weeks"), (2, "week"))
        self.assertEqual(dates.parse_duration("every other month"), (2, "month"))
        self.assertIsNone(dates.parse_duration("every fortnight"))

    def test_effort_minutes(self):
        self.assertEqual(dates.parse_minutes("30m"), 30)
        self.assertEqual(dates.parse_minutes("1h"), 60)
        self.assertEqual(dates.parse_minutes("1h30m"), 90)
        self.assertEqual(dates.parse_minutes("1.5 hours"), 90)
        self.assertEqual(dates.parse_minutes("90 minutes"), 90)
        self.assertIsNone(dates.parse_minutes("every week"))
        self.assertIsNone(dates.parse_minutes(""))
        self.assertEqual(dates.human_minutes(90), "1h30m")
        self.assertEqual(dates.human_minutes(120), "2h")
        self.assertEqual(dates.human_minutes(45), "45m")


# ------------------------------------------------------------ quick entry


class QuickEntry(unittest.TestCase):
    KNOWN = {"garden fence": "project", "home admin": "area"}

    def parse(self, text):
        return quick.parse(text, self.KNOWN)

    def test_flag_prefix_and_suffix(self):
        # A run of marks is one flag wherever it sits, so the count of marks
        # never becomes a level to rank tasks by.
        self.assertEqual(self.parse("!! Ring the plumber")["priority"], 1)
        self.assertEqual(self.parse("Ring the plumber !!")["priority"], 1)
        self.assertEqual(self.parse("!!! Ring the plumber")["priority"], 1)
        self.assertEqual(self.parse("Ring the plumber !!!")["title"],
                         "Ring the plumber")

    def test_contexts(self):
        parsed = self.parse("Buy oat milk #errands #Home")
        self.assertEqual(parsed["tags"], ["errands", "Home"])
        self.assertEqual(parsed["title"], "Buy oat milk")

    def test_subcontext_path_is_one_context(self):
        parsed = self.parse("Buy nails #home>kitchen #home")
        self.assertEqual(parsed["tags"], ["home>kitchen", "home"])
        self.assertEqual(parsed["title"], "Buy nails")

    def test_container_with_spaces(self):
        parsed = self.parse("Repair the latch /Garden fence/ #home")
        self.assertEqual(parsed["project"], "Garden fence")
        self.assertEqual(parsed["title"], "Repair the latch")

    def test_unknown_container_stays_in_title(self):
        parsed = self.parse("Repair the latch /Nonexistent/")
        self.assertIsNone(parsed["project"])
        self.assertEqual(parsed["title"], "Repair the latch /Nonexistent/")

    def test_dates_do_not_swallow_the_sentence(self):
        parsed = self.parse("Pay Ana <next friday >2026-11-01 ~every 2 months =Ana")
        self.assertEqual(parsed["defer"], dates.parse_when("next friday"))
        self.assertEqual(parsed["due"], dates.parse_when("2026-11-01"))
        self.assertEqual(parsed["repeat"], (2, "month"))
        self.assertEqual(parsed["waiting_for"], "Ana")
        self.assertEqual(parsed["title"], "Pay Ana")

    def test_waiting_for_keeps_tag_separate(self):
        parsed = self.parse("Chase Ana =Ana #email")
        self.assertEqual(parsed["waiting_for"], "Ana")
        self.assertEqual(parsed["tags"], ["email"])
        self.assertEqual(parsed["title"], "Chase Ana")

    def test_effort_estimate(self):
        parsed = self.parse("Draft the proposal *1h30m #work")
        self.assertEqual(parsed["duration"], 90)
        self.assertEqual(parsed["title"], "Draft the proposal")
        self.assertEqual(parsed["tags"], ["work"])
        self.assertIsNone(self.parse("Draft the proposal *nonsense")["duration"])


    def test_quoted_contexts_and_notes(self):
        parsed = quick.parse('Draft the "annual review" memo #work\nremember the numbers')
        self.assertEqual(parsed["title"], 'Draft the "annual review" memo')
        self.assertEqual(parsed["notes"], "remember the numbers")

    def test_unknown_date_is_kept_as_text(self):
        parsed = self.parse("Ship it >whenever")
        self.assertIsNone(parsed["due"])
        self.assertIn("whenever", parsed["title"])


# ------------------------------------------------------------ the five steps


class Capture(TempStore):
    def test_lands_in_the_inbox_by_default(self):
        item = self.capture("Think about the thing")
        self.assertEqual(item["status"], "inbox")
        self.assertEqual(item["title"], "Think about the thing")

    def test_needs_a_title(self):
        with self.assertRaises(GtdError):
            self.capture("   ")

    def test_flags_and_contexts_are_applied(self):
        item = self.capture("!! Buy milk #errands")
        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["tags"], ["errands"])

    def test_known_project_bypasses_the_inbox(self):
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        item = self.capture("Repair the latch /Garden fence/")
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["parent_title"], "Garden fence")

    def test_duration_round_trips(self):
        item = self.capture("Write the report *45m")
        self.assertEqual(item["duration"], 45)
        item = core.organize(self.store, item["id"], duration="2h")
        self.assertEqual(item["duration"], 120)
        item = core.organize(self.store, item["id"], clear_duration=True)
        self.assertIsNone(item["duration"])

    def test_defer_after_the_deadline_is_refused(self):
        with self.assertRaises(GtdError):
            self.capture("Too late to even start", defer="2026-05-20",
                         due="2026-05-10")


class Clarify(TempStore):
    def setUp(self):
        super().setUp()
        self.item = self.capture("Ring the plumber")

    def test_actionable_needs_a_home_or_stays_in_the_inbox(self):
        with self.assertRaises(GtdError):
            core.clarify(self.store, self.item["id"], "actionable")
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        item = core.clarify(self.store, self.item["id"], "actionable",
                            project="Garden fence", defer="today")
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["parent_title"], "Garden fence")

    def test_do_now_completes_immediately(self):
        self.assertEqual(core.clarify(self.store, self.item["id"], "do-now")["status"],
                         "done")

    def test_waiting_needs_someone(self):
        with self.assertRaises(GtdError):
            core.clarify(self.store, self.item["id"], "wait")
        self.assertEqual(
            core.clarify(self.store, self.item["id"], "wait", waiting_for="Ana")["status"],
            "waiting")

    def test_deferred_after_a_deadline_is_refused(self):
        with self.assertRaises(GtdError):
            core.clarify(self.store, self.item["id"], "defer",
                         project="Garden fence", defer="2026-05-20",
                         due="2026-05-10")

    def test_delete_goes_to_the_trash(self):
        item = core.clarify(self.store, self.item["id"], "delete")
        self.assertEqual(item["status"], "trashed")
        self.assertEqual(len(core.list_view(self.store, "trash")), 1)

    def test_reference_is_kept_not_done(self):
        item = core.clarify(self.store, self.item["id"], "reference",
                            notes="look this up later")
        self.assertEqual(item["status"], "reference")
        self.assertEqual(core.stats(self.store)["reference"], 1)


class Organize(TempStore):
    def setUp(self):
        super().setUp()
        self.area = core.ensure_container(self.store, "Home admin", core.AREA)
        self.project = core.ensure_container(self.store, "Garden fence", core.PROJECT,
                                             area="Home admin")
        self.action = self.capture("Repair the latch")

    def test_project_belongs_to_its_area(self):
        self.assertEqual(core.get(self.store, self.project["id"])["parent_title"],
                         "Home admin")

    def test_project_path_knows_its_ancestors(self):
        item = core.get(self.store, self.action["id"])
        self.assertEqual(item["parent_path"], "")
        item = core.organize(self.store, self.action["id"], project="Garden fence")
        self.assertEqual(item["parent_path"], "Home admin>Garden fence")
        # A container reports its own ancestry too.
        self.assertEqual(core.get(self.store, self.project["id"])["parent_path"],
                         "Home admin")

    def test_project_path_rooted_at_an_area_resolves_in_place(self):
        moved = core.organize(self.store, self.action["id"],
                              project="Home admin>Garden fence")
        self.assertEqual(moved["parent_title"], "Garden fence")
        self.assertEqual(moved["parent_path"], "Home admin>Garden fence")
        projects = [p["title"] for p in core.list_view(self.store, "projects")]
        self.assertNotIn("Home admin", projects,
                         "an area must not be recreated as a project")

    def test_deep_project_path_builds_and_reuses_the_chain(self):
        first = core.organize(self.store, self.action["id"], project="A>B>Cardiology")
        self.assertEqual(first["parent_path"], "A>B>Cardiology")
        names = sorted(p["title"] for p in core.list_view(self.store, "projects"))
        self.assertEqual(names, ["A", "B", "Cardiology"])
        second = self.capture("Do the echo", project="A>B>Cardiology")
        self.assertEqual(second["parent_path"], "A>B>Cardiology")
        self.assertEqual(len(core.list_view(self.store, "projects")), 3,
                         "filing again must not duplicate the chain")

    def test_organize_moves_an_item(self):
        core.organize(self.store, self.action["id"], project="Garden fence",
                      add=["home"], priority=1, due="friday")
        item = core.get(self.store, self.action["id"])
        self.assertEqual(item["parent_title"], "Garden fence")
        self.assertEqual(item["tags"], ["home"])
        self.assertEqual(item["priority"], 1)
        self.assertEqual(item["due_at"], dates.to_stamp(dates.parse_when("friday")))

    def test_defer_after_the_deadline_is_refused_even_when_only_one_moves(self):
        item = core.organize(self.store, self.action["id"],
                             defer="2026-05-11", due="2026-05-20")
        # Pulling the deadline in before the defer date is refused…
        with self.assertRaises(GtdError):
            core.organize(self.store, item["id"], due="2026-05-09")
        # …and so is pushing the defer date out past the deadline.
        with self.assertRaises(GtdError):
            core.organize(self.store, item["id"], defer="2026-05-22")
        # The window closing on the day it opens is still fine.
        item = core.organize(self.store, item["id"], due="2026-05-11")
        self.assertEqual(item["due_at"][:10], "2026-05-11")

    def test_untag_and_clearing_dates(self):
        self.capture("Thing", tags=["home", "work"])
        other = core.find(self.store, "Thing")
        core.organize(self.store, other["id"], defer="tomorrow", due="friday")
        core.organize(self.store, other["id"], drop_tags=["work"],
                      clear_defer=True, clear_due=True)
        item = core.get(self.store, other["id"])
        self.assertEqual(item["tags"], ["home"])
        self.assertIsNone(item["defer_at"])
        self.assertIsNone(item["due_at"])

    def test_review_cadence_is_validated(self):
        with self.assertRaises(GtdError):
            core.organize(self.store, self.project["id"], review="fortnightly")
        self.assertEqual(
            core.organize(self.store, self.project["id"], review="weekly")["review"],
            "weekly")

    def test_project_names_are_matched_without_regard_to_case(self):
        again = core.ensure_container(self.store, "GARDEN FENCE", core.PROJECT)
        self.assertEqual(again["id"], self.project["id"])
        self.assertEqual(again["title"], "Garden fence")
        rows = [r for r in core.list_view(self.store, "projects")
                if r["title"] == "Garden fence"]
        self.assertEqual(len(rows), 1)

    def test_the_first_spelling_of_a_project_is_the_one_kept(self):
        fresh = core.ensure_container(self.store, "Kitchen Repaint", core.PROJECT)
        for spelling in ("kitchen repaint", "KITCHEN REPAINT", "KiTcHeN rEpAiNt"):
            self.assertEqual(
                core.ensure_container(self.store, spelling, core.PROJECT)["title"],
                "Kitchen Repaint")
        self.assertEqual(core.get(self.store, fresh["id"])["title"], "Kitchen Repaint")

    def test_contexts_are_folded_but_project_names_are_not(self):
        core.organize(self.store, self.action["id"], project="garden FENCE",
                      add=["Home>Kitchen"])
        item = core.get(self.store, self.action["id"])
        self.assertEqual(item["parent_title"], "Garden fence")
        self.assertEqual(item["tags"], ["home>kitchen"])

    def test_a_task_has_exactly_one_project(self):
        core.organize(self.store, self.action["id"], project="Garden fence")
        other = core.ensure_container(self.store, "Kitchen repaint", core.PROJECT)
        core.organize(self.store, self.action["id"], project=other["id"])
        item = core.get(self.store, self.action["id"])
        self.assertEqual(item["parent_id"], other["id"])
        self.assertEqual(item["parent_title"], "Kitchen repaint")
        self.assertEqual([r["parent_id"] for r in self.store.query(
            "SELECT DISTINCT parent_id FROM items WHERE id = ?", (item["id"],))],
            [other["id"]])

    def test_adding_a_context_alone_is_a_change(self):
        # Only item_tags is touched, so the "nothing to change" guard has to
        # know that counts.
        item = core.organize(self.store, self.action["id"], add=["Errands"])
        self.assertEqual(item["tags"], ["errands"])
        item = core.organize(self.store, self.action["id"], drop_tags=["Errands"])
        self.assertEqual(item["tags"], [])
        with self.assertRaises(GtdError):
            core.organize(self.store, self.action["id"])


class Engage(TempStore):
    def setUp(self):
        super().setUp()
        self.item = self.capture("Email Ana about the invoice")

    def test_done_removes_the_project_it_emptied(self):
        project = core.ensure_container(self.store, "Q4 audit", core.PROJECT)
        child = self.capture("Collect the numbers", project="Q4 audit")
        self.assertEqual(core.get(self.store, project["id"])["completed"], 0)
        core.engage(self.store, child["id"], "done")
        # The project existed to hold that one action, so it goes with it. The
        # action itself is archived, not deleted.
        self.assertIsNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                         (project["id"],)))
        self.assertEqual(core.get(self.store, child["id"])["status"], "done")

    def test_a_project_still_working_is_left_alone(self):
        core.ensure_container(self.store, "Q4 audit", core.PROJECT)
        first = self.capture("Collect the numbers", project="Q4 audit")
        self.capture("Draft the summary", project="Q4 audit")
        core.engage(self.store, first["id"], "done")
        self.assertEqual(core.list_view(self.store, "projects")[0]["title"],
                         "Q4 audit")

    def test_repeat_spawns_the_next_instance_once(self):
        item = self.capture("Water the plants ~every 2 days")
        done = core.engage(self.store, item["id"], "done")
        spawned = done["next"]
        self.assertTrue(spawned)
        self.assertEqual(core.engage(self.store, item["id"], "done")["status"], "done")
        again = core.list_view(self.store, "inbox") + core.list_view(self.store, "focus")
        matching = [i for i in again if i["title"] == "Water the plants"
                    and i["status"] != "done"]
        self.assertEqual(len(matching), 1, "completing twice must not spawn twice")

    def test_flag_levels(self):
        self.assertEqual(core.engage(self.store, self.item["id"], "flag")["priority"], 1)
        self.assertEqual(core.engage(self.store, self.item["id"], "unflag")["priority"], 0)
        # A level above the one flag is refused, not quietly lowered: a caller
        # still thinking in three ranks deserves to be told which world it is in.
        with self.assertRaises(GtdError):
            core.engage(self.store, self.item["id"], "flag", priority=3)

    def test_waiting_then_resuming(self):
        core.engage(self.store, self.item["id"], "wait", waiting_for="Ana")
        self.assertEqual(core.get(self.store, self.item["id"])["waiting_for"], "Ana")
        resumed = core.engage(self.store, self.item["id"], "resume")
        self.assertEqual(resumed["status"], "active")
        self.assertIsNone(resumed["waiting_for"])

    def test_waiting_can_be_a_boolean_without_a_name(self):
        waited = core.engage(self.store, self.item["id"], "wait")
        self.assertEqual(waited["status"], "waiting")
        self.assertIsNone(waited["waiting_for"])
        resumed = core.engage(self.store, self.item["id"], "resume")
        self.assertEqual(resumed["status"], "active")

    def test_defer_and_drop(self):
        deferred = core.engage(self.store, self.item["id"], "defer", when="+2w")
        self.assertEqual(deferred["defer_at"], dates.to_stamp(dates.parse_when("+2w")))
        dropped = core.engage(self.store, self.item["id"], "drop")
        self.assertEqual(dropped["status"], "dropped")
        self.assertTrue(dropped["dropped_at"])

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(GtdError):
            core.engage(self.store, self.item["id"], "obliterate")


class Postpone(TempStore):
    def setUp(self):
        super().setUp()

    def test_rolls_due_one_day_later_and_keeps_the_clock(self):
        item = self.capture("Email Ana about the invoice", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"])
        self.assertEqual(rolled["due_at"], "2026-05-11 15:00")

    def test_defaults_to_one_day(self):
        item = self.capture("Email Ana about the invoice", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"])
        self.assertEqual(rolled["due_at"], "2026-05-11 15:00")

    def test_explicit_day_count(self):
        item = self.capture("Email Ana about the invoice", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"], days=3)
        self.assertEqual(rolled["due_at"], "2026-05-13 15:00")

    def test_defer_on_the_old_deadline_fires_with_it(self):
        item = self.capture("Email Ana about the invoice",
                            defer="2026-05-10 09:00", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"])
        self.assertEqual(rolled["due_at"], "2026-05-11 15:00")
        self.assertEqual(rolled["defer_at"][:10], "2026-05-11")

    def test_defer_before_the_deadline_stays_put(self):
        item = self.capture("Email Ana about the invoice",
                            defer="2026-05-08 09:00", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"])
        self.assertEqual(rolled["due_at"], "2026-05-11 15:00")
        self.assertEqual(rolled["defer_at"], "2026-05-08 09:00")

    def test_no_deadline_is_a_noop(self):
        item = self.capture("Email Ana about the invoice")
        self.assertIsNone(item["due_at"])
        rolled = core.postpone(self.store, item["id"])
        self.assertIsNone(rolled["due_at"])

    def test_zero_days_changes_nothing(self):
        item = self.capture("Email Ana about the invoice", due="2026-05-10 15:00")
        rolled = core.postpone(self.store, item["id"], days=0)
        self.assertEqual(rolled["due_at"], "2026-05-10 15:00")

    def test_logs_the_change(self):
        item = self.capture("Email Ana about the invoice", due="2026-05-10 15:00")
        core.postpone(self.store, item["id"])
        log = self.store.query("SELECT kind FROM log WHERE item_id = ? ORDER BY id",
                               (item["id"],))
        self.assertEqual([row[0] for row in log][-1], "postpone")


class Link(TempStore):
    def test_item_url_is_the_link(self):
        item = self.capture("Read the manual", url="https://example.com/docs")
        self.assertEqual(core.link_for(self.store, item["id"]),
                         "https://example.com/docs")

    def test_url_wins_over_a_url_attachment(self):
        item = self.capture("Read the manual", url="https://example.com/docs")
        core.add_attachment(self.store, item["id"], "https://example.com/extra")
        self.assertEqual(core.link_for(self.store, item["id"]),
                         "https://example.com/docs")

    def test_a_url_attachment_is_the_link_when_there_is_no_url_field(self):
        item = self.capture("Check the dashboard")
        core.add_attachment(self.store, item["id"], "https://example.com/status")
        self.assertEqual(core.link_for(self.store, item["id"]),
                         "https://example.com/status")

    def test_a_file_attachment_is_not_a_link(self):
        item = self.capture("Review the report")
        path = os.path.join(self.home, "report.pdf")
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4")
        core.add_attachment(self.store, item["id"], path)
        self.assertIsNone(core.link_for(self.store, item["id"]))

    def test_no_url_and_no_attachment_is_no_link(self):
        item = self.capture("Just a thought")
        self.assertIsNone(core.link_for(self.store, item["id"]))


class DatabaseNaming(TempStore):
    def test_default_database_is_named_omagtd(self):
        # The store opens `omagtd.sqlite3`, not the old `gtd.sqlite3`, so the
        # database carries the app's name on disk.
        self.assertEqual(self.store.path.name, "omagtd.sqlite3")

    def test_old_gtd_database_is_carried_forward_once(self):
        # A store named `gtd.sqlite3` from before the rename is moved to
        # `omagtd.sqlite3` on first use, so an existing user does not lose
        # data. Use a fresh home with no `omagtd.sqlite3` yet, since the
        # migration never overwrites a store that already has the new name.
        self.store.close()
        fresh_home = tempfile.mkdtemp(prefix="gtd-rename-")
        os.environ["GTD_HOME"] = fresh_home
        from gtd.db import data_home
        (data_home() / "gtd.sqlite3").write_text("", encoding="utf-8")
        try:
            fresh = Store()
            self.assertEqual(fresh.path.name, "omagtd.sqlite3")
            self.assertFalse((data_home() / "gtd.sqlite3").exists())
            fresh.close()
        finally:
            shutil.rmtree(fresh_home, ignore_errors=True)


class Reflect(TempStore):
    def test_daily_and_weekly_checklists(self):
        self.capture("Something")
        daily = review.daily(self.store)
        weekly = review.weekly(self.store)
        self.assertTrue(any(step["count"] for step in daily["steps"]))
        self.assertTrue(weekly["steps"])
        self.assertEqual(weekly["due"]["scopes"], [])

    def test_marking_records_the_cadence(self):
        core.ensure_container(self.store, "Q4 audit", core.PROJECT, review="weekly")
        due = review.due_reviews(self.store)
        self.assertEqual([scope["title"] for scope in due["scopes"]], ["Q4 audit"])
        self.assertEqual(due["scopes"][0]["unreviewed"], 1)

        review.mark(self.store, "weekly")
        self.assertEqual(review.due_reviews(self.store)["scopes"][0]["unreviewed"], 0)

    def test_marking_one_container_touches_its_actions(self):
        project = core.ensure_container(self.store, "Garden fence", core.PROJECT,
                                        review="weekly")
        child = self.capture("Repair the latch", project="Garden fence")
        review.mark(self.store, "weekly", scope=project["id"])
        self.assertIsNotNone(core.get(self.store, project["id"])["reviewed_at"])
        self.assertIsNotNone(core.get(self.store, child["id"])["reviewed_at"])

    def test_history_after_marking(self):
        review.mark(self.store, "weekly")
        review.mark(self.store, "daily")
        history = review.history(self.store)
        self.assertIsNotNone(history["last_weekly"])
        self.assertIsNotNone(history["last_daily"])
        self.assertEqual(history["weeklies"], 1)
        self.assertEqual(history["dailies"], 1)


class Views(TempStore):
    def setUp(self):
        super().setUp()
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        self.ready = self.capture("Ready now", project="Garden fence")
        self.scheduled = self.capture("Someday task")
        core.clarify(self.store, self.scheduled["id"], "someday")
        self.waiting = self.capture("Waiting on the bank")
        core.clarify(self.store, self.waiting["id"], "wait", waiting_for="Bank")
        self.flagged = self.capture("Flagged thing", priority=1)
        self.late = self.capture("Late thing", due="-1d")
        self.ref = self.capture("Reference thing")
        core.clarify(self.store, self.ref["id"], "reference")

    def titles(self, view):
        return [item["title"] for item in core.list_view(self.store, view)]

    def test_every_view_finds_its_own_rows(self):
        self.assertIn("Ready now", self.titles("focus"))
        self.assertIn("Someday task", self.titles("someday"))
        self.assertIn("Waiting on the bank", self.titles("waiting"))
        self.assertIn("Flagged thing", self.titles("flagged"))
        self.assertIn("Late thing", self.titles("overdue"))
        self.assertIn("Reference thing", self.titles("reference"))

    def test_waiting_is_not_also_available(self):
        self.assertNotIn("Waiting on the bank", self.titles("focus"))
        self.assertNotIn("Someday task", self.titles("focus"))

    def test_filtering_by_project_and_context(self):
        item = self.capture("Tagged thing", project="Garden fence", tags=["home"])
        self.assertEqual(
            [i["title"] for i in core.list_view(self.store, "focus", project="Garden fence")],
            ["Ready now", "Tagged thing"])
        self.assertEqual([i["name"] for i in core.list_view(self.store, "tags")],
                         ["home"])

    def test_unknown_view_lists_the_options(self):
        with self.assertRaises(GtdError) as caught:
            core.list_view(self.store, "nonsense")
        self.assertIn("inbox", str(caught.exception))

    def test_stats_counts_what_the_bar_shows(self):
        stats = core.stats(self.store)
        self.assertEqual(stats["waiting"], 1)
        self.assertEqual(stats["someday"], 1)
        self.assertEqual(stats["flagged"], 1)
        self.assertEqual(stats["reference"], 1)
        self.assertEqual(stats["overdue"], 1)

    def test_bar_snapshot_is_small_and_json_ready(self):
        snapshot = core.bar_snapshot(self.store)
        json.dumps(snapshot)
        self.assertIn("inbox", snapshot)
        self.assertIn("today", snapshot)


class Identity(TempStore):
    def test_ids_are_unique_short_and_resolvable(self):
        first = self.capture("One")
        second = self.capture("Two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertGreaterEqual(len(first["id"]), 6)
        self.assertEqual(core.find(self.store, first["id"])["title"], "One")
        self.assertEqual(core.find(self.store, first["id"][:4])["title"], "One")

    def test_last_and_negative_offsets(self):
        self.capture("One")
        second = self.capture("Two")
        third = self.capture("Three")
        self.assertEqual(core.find(self.store, "last")["id"], third["id"])
        self.assertEqual(core.find(self.store, "-2")["id"], second["id"])

    def test_ambiguous_prefix_is_refused_not_guessed(self):
        # two ids that share a prefix would need forging; check the message path
        self.capture("One")
        with self.assertRaises(GtdError) as caught:
            core.find(self.store, "")
        self.assertIn("need an item id", str(caught.exception))

    def test_names_resolve_and_containers_win(self):
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        self.capture("Garden fence")
        resolved = core.find(self.store, "Garden fence")
        self.assertEqual(resolved["kind"], "project")

    def test_unknown_reference_is_an_error(self):
        with self.assertRaises(GtdError):
            core.find(self.store, "nothing-called-this")


class Trash(TempStore):
    def test_trash_restore_and_purge(self):
        item = self.capture("Regrettable")
        core.trash(self.store, item["id"])
        self.assertEqual(len(core.list_view(self.store, "inbox")), 0)
        self.assertEqual(len(core.list_view(self.store, "trash")), 1)

        restored = core.restore(self.store, item["id"])
        self.assertEqual(restored["status"], "inbox")

        core.trash(self.store, item["id"])
        core.purge(self.store, all_items=True)
        self.assertEqual(core.list_view(self.store, "trash"), [])

    def test_restore_returns_an_item_from_a_project_to_the_inbox(self):
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        item = self.capture("Repair the latch", project="Garden fence")
        self.capture("Fit the new hinges", project="Garden fence")
        core.trash(self.store, item["id"])
        restored = core.restore(self.store, item["id"])
        self.assertEqual(restored["status"], "active")
        self.assertEqual(restored["parent_title"], "Garden fence")

    def test_trashing_the_last_action_takes_the_project_with_it(self):
        core.ensure_container(self.store, "Garden fence", core.PROJECT)
        item = self.capture("Repair the latch", project="Garden fence")
        core.trash(self.store, item["id"])
        self.assertIsNone(self.store.one(
            "SELECT id FROM items WHERE title = 'Garden fence'"))
        # The action keeps its 30 days, and comes back unfiled rather than
        # pointing at a project that is gone.
        self.assertEqual(core.get(self.store, item["id"])["status"], "trashed")
        restored = core.restore(self.store, item["id"])
        self.assertIsNone(restored["parent_id"])


# ---------------------------------------------------------------- importing


class OmniFocusXML(TempStore):
    def setUp(self):
        super().setUp()
        core.ensure_container(self.store, "Work", core.AREA)
        core.ensure_container(self.store, "Q4 audit", core.PROJECT, area="Work")
        self.live = self.capture("Reconcile the ledger", project="Q4 audit",
                                 tags=["work"], priority=1, defer="tomorrow", due="friday")
        self.finished = self.capture("Sign the contract", project="Q4 audit")
        core.engage(self.store, self.finished["id"], "done")
        self.floating = self.capture("Email Ana")

    def snapshot(self):
        rows = self.store.query(
            "SELECT kind, status, title, COALESCE(defer_at,''), COALESCE(due_at,''), "
            "COALESCE(repeat,'') FROM items ORDER BY kind, title")
        return [tuple(row) for row in rows]

    def test_ofxml_round_trip_is_lossless(self):
        before = self.snapshot()
        text = importer.export_text(self.store, "ofxml")
        target = Store(path=os.path.join(self.home, "copy.sqlite3"))
        self.addCleanup(target.close)
        importer.import_file(target, self.write("copy.ofxml", text))
        self.assertEqual(self.snapshot_of(target), before)

    def test_json_round_trip_keeps_ids(self):
        text = importer.export_text(self.store, "json")
        target = Store(path=os.path.join(self.home, "json.sqlite3"))
        self.addCleanup(target.close)
        importer.import_file(target, self.write("copy.json", text))
        payload = json.loads(text)
        self.assertEqual(sorted(row["id"] for row in self.rows(target)),
                         sorted(row["id"] for row in payload["items"]))

    def test_csv_round_trip_is_lossless(self):
        before = self.snapshot()
        text = importer.export_text(self.store, "csv")
        target = Store(path=os.path.join(self.home, "csv.sqlite3"))
        self.addCleanup(target.close)
        importer.import_file(target, self.write("copy.csv", text))
        self.assertEqual(self.snapshot_of(target), before)

    def test_dry_run_changes_nothing(self):
        text = importer.export_text(self.store, "json")
        path = self.write("dry.json", text)
        target = Store(path=os.path.join(self.home, "dryrun.sqlite3"))
        self.addCleanup(target.close)
        result = importer.import_file(target, path, dry_run=True)
        self.assertGreater(result["actions"], 0)
        self.assertEqual(target.scalar("SELECT COUNT(*) FROM items"), 0)

    def test_reads_a_hand_written_omnifocus_backup(self):
        plist = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Archives</key><array><dict>
<key>ArchiveName</key><string>OmniFocus</string>
<key>Contents</key><array>
  <dict>
    <key>Title</key><string>Home admin</string>
    <key>Type</key><string>Area</string>
  </dict>
  <dict>
    <key>Title</key><string>Roof</string>
    <key>Type</key><string>Project</string>
    <key>Parents</key><array><dict>
      <key>Title</key><string>Home admin</string>
      <key>Type</key><string>Area</string>
    </dict></array>
  </dict>
  <dict>
    <key>Title</key><string>Call the roofer</string>
    <key>Type</key><string>Action</string>
    <key>Parents</key><array><dict>
      <key>Title</key><string>Roof</string>
      <key>Type</key><string>Project</string>
    </dict></array>
    <key>Tags</key><array><string>phone</string></array>
    <key>Flagged</key><true/>
    <key>DueDate</key><date>2026-11-01T09:00:00Z</date>
  </dict>
  <dict>
    <key>Title</key><string>Old idea</string>
    <key>Type</key><string>Action</string>
    <key>State</key><string>Completed</string>
    <key>CompletedDate</key><date>2026-09-30T12:00:00Z</date>
  </dict>
</array></dict></array></dict></plist>
"""
        target = Store(path=os.path.join(self.home, "of.sqlite3"))
        self.addCleanup(target.close)
        importer.import_file(target, self.write("backup.ofxml", plist))

        self.assertEqual(target.scalar(
            "SELECT parent_title FROM item_titles WHERE title = 'Roof'"), "Home admin")
        action = target.one(
            "SELECT * FROM item_titles WHERE title = 'Call the roofer'")
        self.assertEqual(action["parent_title"], "Roof")
        self.assertEqual(action["priority"], 1)
        self.assertEqual(action["due_at"], "2026-11-01 09:00")
        self.assertEqual([t["name"] for t in target.query("SELECT name FROM tags")], ["phone"])
        self.assertEqual(target.one(
            "SELECT status FROM items WHERE title = 'Old idea'")["status"], "done")

    def test_reads_an_ofocusmx_export(self):
        mx = """Title: Work
Type: Area

Title: Q4 audit
Type: Project
ParentFolder: Work
ReviewInterval: 2

Title: Reconcile the ledger
Type: Action
Container: Q4 audit
Context: work
Flagged: true
DeferredDate: 2026-10-05 09:00:00 +0000
DueDate: 2026-10-09 17:00:00 +0000

Title: Sign the contract
Type: Action
Container: Q4 audit
State: Completed
CompletedDate: 2026-09-30 12:00:00 +0000

Title: Email Ana
Type: Action
LocationInFolder: Inbox
Context: email

Title: Read the manual
Type: Action
LocationInFolder: Someday
Repetition: every 2 months
"""
        target = Store(path=os.path.join(self.home, "mx.sqlite3"))
        self.addCleanup(target.close)
        importer.import_file(target, self.write("export.ofocusmx", mx))

        self.assertEqual(target.scalar(
            "SELECT COUNT(*) FROM items WHERE kind = 'area'"), 1)
        project = target.one("SELECT * FROM item_titles WHERE title = 'Q4 audit'")
        self.assertEqual(project["parent_title"], "Work")
        self.assertEqual(project["review"], "weekly")

        action = target.one("SELECT * FROM item_titles WHERE title = 'Reconcile the ledger'")
        self.assertEqual(action["parent_title"], "Q4 audit")
        self.assertEqual(action["defer_at"], "2026-10-05 09:00")
        self.assertEqual(action["due_at"], "2026-10-09 17:00")
        # OmniFocus's "flagged and deferred" never was a third level here;
        # the flag is on, however loudly the file asked for attention.
        self.assertEqual(action["priority"], 1)

        self.assertEqual(target.one(
            "SELECT status FROM items WHERE title = 'Sign the contract'")["status"], "done")
        self.assertEqual(target.one(
            "SELECT status FROM items WHERE title = 'Email Ana'")["status"], "inbox")
        someday = target.one("SELECT * FROM items WHERE title = 'Read the manual'")
        self.assertEqual(someday["status"], "someday")
        self.assertIn('"interval": 2', someday["repeat"])

    def test_junk_is_skipped_not_fatal(self):
        target = Store(path=os.path.join(self.home, "junk.sqlite3"))
        self.addCleanup(target.close)
        result = importer.import_csv(target, "title,kind\nBuy milk,action\n,action\n")
        self.assertEqual(result["actions"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_reimporting_the_same_export_does_not_duplicate_containers(self):
        text = importer.export_text(self.store, "json")
        path = self.write("again.json", text)
        importer.import_file(self.store, path)
        before = self.snapshot()
        importer.import_file(self.store, path)
        self.assertEqual(self.snapshot(), before)

    # helpers -------------------------------------------------------------

    def write(self, name, text):
        path = os.path.join(self.home, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def rows(self, store):
        return [dict(row) for row in store.query(
            "SELECT id, kind, title FROM items ORDER BY kind, title")]

    def snapshot_of(self, store):
        rows = store.query(
            "SELECT kind, status, title, COALESCE(defer_at,''), COALESCE(due_at,''), "
            "COALESCE(repeat,'') FROM items ORDER BY kind, title")
        return [tuple(row) for row in rows]


class Database(TempStore):
    def test_profiles_live_side_by_side(self):
        other = Store(profile="work")
        self.addCleanup(other.close)
        self.capture("Personal thing")
        core.capture(other, "Work thing")
        self.assertEqual(core.stats(self.store)["inbox"], 1)
        self.assertEqual(core.stats(other)["inbox"], 1)

    def test_backup_and_restore(self):
        self.capture("Something")
        target = os.path.join(self.home, "backup.sqlite3")
        destination = sqlite3.connect(target)
        try:
            self.store.db.backup(destination)
        finally:
            destination.close()
        restored = Store(path=os.path.join(self.home, "restored.sqlite3"))
        self.addCleanup(restored.close)
        restored.db.close()
        shutil.copyfile(target, restored.path)
        reopened = Store(path=restored.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.scalar("SELECT COUNT(*) FROM items"), 1)

    def test_schema_is_applied_once_and_again(self):
        count = self.store.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'items'")
        self.store.ensure()
        self.assertEqual(self.store.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'items'"),
            count)

    def test_log_records_the_why(self):
        item = self.capture("Something")
        core.engage(self.store, item["id"], "done")
        kinds = [row["kind"] for row in self.store.query(
            "SELECT kind FROM log WHERE item_id = ?", (item["id"],))]
        self.assertIn("capture", kinds)
        self.assertIn("done", kinds)

    def test_existing_contexts_are_folded_to_lowercase(self):
        # A database written before contexts were lowercased: mixed-case names,
        # subcontexts included, and a task still pointing at them by id. Work on
        # a copy, so reopening it cannot reuse this connection's transaction.
        self.capture("Buy nails #Errands")
        self.capture("Fix the tap #Home>Kitchen")
        legacy = os.path.join(self.home, "legacy.sqlite3")
        destination = sqlite3.connect(legacy)
        try:
            self.store.db.backup(destination)
        finally:
            destination.close()
        old = sqlite3.connect(legacy)
        try:
            for name, new_name in (("errands", "Errands"), ("home", "HOME"),
                                   ("home>kitchen", "Home>Kitchen")):
                old.execute("UPDATE tags SET name = ? WHERE name = ?", (new_name, name))
            old.execute("UPDATE meta SET value = '8' WHERE key = 'schema'")
            old.commit()
        finally:
            old.close()

        reopened = Store(path=legacy)
        self.addCleanup(reopened.close)
        names = sorted(r["name"] for r in reopened.query("SELECT name FROM tags"))
        self.assertEqual(names, ["errands", "home", "home>kitchen"])
        self.assertEqual([r["title"] for r in reopened.query("SELECT title FROM items")],
                         ["Buy nails", "Fix the tap"])
        # The folded context is found by the name that is now on disk.
        self.assertEqual(len(core.list_view(reopened, "focus", tag="errands")), 1)
        self.assertEqual(core.complete_name(reopened, "context", "Err"), "errands")

    def test_folding_contexts_leaves_no_duplicates_behind(self):
        self.capture("Buy nails #Errands")
        self.capture("Order parts #ERRANDS")
        rows = self.store.query("SELECT name FROM tags")
        self.assertEqual([r["name"] for r in rows], ["errands"])
        self.assertEqual(self.store.scalar("SELECT COUNT(*) FROM item_tags"), 2)

    def test_contexts_are_folded_even_when_the_version_is_already_current(self):
        # A database that recorded the current schema version while the fold
        # still had a case-blind filter would otherwise keep mixed-case names
        # forever, because no version change ever comes to trigger it again.
        self.capture("Buy nails #Errands")
        self.store.run("UPDATE tags SET name = 'Errands' WHERE name = 'errands'")
        self.store.ensure()
        self.assertEqual([r["name"] for r in self.store.query("SELECT name FROM tags")],
                         ["errands"])
        self.assertEqual(self.store.meta("schema"), str(db_module.SCHEMA_VERSION))

    def test_flags_above_one_are_folded_on_open(self):
        # A database written when there were three levels still holds rows at
        # 2 and 3. The flag is on or off now, so they come down to on.
        item = self.capture("Rank things")
        self.store.run(
            "UPDATE items SET priority = 3, updated_at = '2020-01-01 00:00' "
            "WHERE id = ?", (item["id"],))
        self.store.ensure()
        folded = self.store.one("SELECT priority, updated_at FROM items WHERE id = ?",
                                (item["id"],))
        self.assertEqual(folded["priority"], 1)
        # Lowering an old flag is not a touch, so the trash grace period and
        # the recency reads must not see it as one.
        self.assertEqual(folded["updated_at"], "2020-01-01 00:00")
        with self.assertRaises(GtdError):
            core.capture(self.store, "Loud thing", priority=3)

    def test_the_context_index_is_upgraded_to_the_covering_one(self):
        # A v11 store carried idx_item_tags_tag(tag_id); the covering
        # (tag_id, item_id) index answers the context joins without a table
        # fetch, and reopening leaves only the covering one behind.
        self.capture("Buy nails #errands")
        legacy = os.path.join(self.home, "legacy.sqlite3")
        destination = sqlite3.connect(legacy)
        try:
            self.store.db.backup(destination)
        finally:
            destination.close()
        old = sqlite3.connect(legacy)
        try:
            old.execute("DROP INDEX IF EXISTS idx_item_tags_tag_item")
            old.execute("CREATE INDEX idx_item_tags_tag ON item_tags(tag_id)")
            old.execute("UPDATE meta SET value = '11' WHERE key = 'schema'")
            old.commit()
        finally:
            old.close()

        reopened = Store(path=legacy)
        self.addCleanup(reopened.close)
        indexes = {r["name"] for r in reopened.query(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("idx_item_tags_tag_item", indexes)
        self.assertNotIn("idx_item_tags_tag", indexes)
        self.assertEqual(reopened.meta("schema"), str(db_module.SCHEMA_VERSION))


# ------------------------------------------------------------------ archive


class Archived(TempStore):
    def test_done_is_invisible_to_the_workflow_views(self):
        item = self.capture("Water the plants", project="Q4 audit")
        core.complete(self.store, item["id"])
        for view in ("today", "focus", "inbox", "next_actions", "overdue",
                     "waiting", "someday", "tasks"):
            titles = [r["title"] for r in core.list_view(self.store, view)]
            self.assertNotIn("Water the plants", titles, view)

    def test_search_is_the_only_way_back_to_it(self):
        item = self.capture("Water the plants")
        core.complete(self.store, item["id"])
        self.assertEqual([r["title"] for r in core.search(self.store, "plants")],
                         ["Water the plants"])

    def test_search_puts_archived_items_after_everything_open(self):
        self.capture("Renovate", due="2026-01-01")
        archived = self.capture("Water the plants")
        core.complete(self.store, archived["id"])
        self.capture("Later but open")
        order = [r["title"] for r in core.search(self.store, "e", limit=50)
                 if r["title"] in ("Renovate", "Water the plants",
                                   "Later but open")]
        self.assertEqual(order[-1], "Water the plants")

    def test_archived_items_carry_their_completion_date(self):
        item = self.capture("Water the plants")
        core.complete(self.store, item["id"])
        found = core.search(self.store, "plants")[0]
        self.assertEqual(found["status"], "done")
        self.assertTrue(found["completed_at"])

    def test_most_recently_archived_comes_first_among_them(self):
        first = self.capture("Plant the seeds")
        second = self.capture("Plant the beans")
        core.complete(self.store, first["id"])
        self.store.run("UPDATE items SET completed_at = '2026-01-01 09:00' "
                       "WHERE id = ?", (first["id"],))
        core.complete(self.store, second["id"])
        order = [r["title"] for r in core.search(self.store, "Plant the")]
        self.assertEqual(order, ["Plant the beans", "Plant the seeds"])


# ---------------------------------------------------------- trash retention


class TrashRetention(TempStore):
    def _age(self, item_id, days):
        stamp = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        self.store.run("UPDATE items SET updated_at = ? WHERE id = ?", (stamp, item_id))

    def test_thirty_days_is_the_line(self):
        self.assertEqual(core.TRASH_DAYS, 30)

    def test_fresh_trash_is_left_alone(self):
        item = self.capture("Oops")
        core.trash(self.store, item["id"])
        self.assertEqual(core.sweep_trash(self.store), [])
        self.assertEqual(core.get(self.store, item["id"])["status"], "trashed")

    def test_thirty_one_days_is_gone(self):
        item = self.capture("Oops")
        core.trash(self.store, item["id"])
        self._age(item["id"], 31)
        self.assertEqual(core.sweep_trash(self.store), [item["id"]])
        self.assertIsNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                         (item["id"],)))

    def test_thirty_days_to_the_day_is_kept(self):
        item = self.capture("Oops")
        core.trash(self.store, item["id"])
        self._age(item["id"], 29)
        self.assertEqual(core.sweep_trash(self.store), [])

    def test_only_the_trash_is_swept(self):
        keep = self.capture("Still going")
        gone = self.capture("Oops")
        core.trash(self.store, gone["id"])
        self._age(gone["id"], 40)
        core.sweep_trash(self.store)
        self.assertIsNotNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                            (keep["id"],)))


# ------------------------------------------------------------ date completion


class DateCompletion(unittest.TestCase):
    def setUp(self):
        dates.configure(due_time="18:00")

    def test_tod_completes_today_at_the_configured_time(self):
        self.assertEqual(dates.complete_when("tod"), "today 18:00")

    def test_the_configured_time_is_the_one_used(self):
        dates.configure(due_time="09:30")
        try:
            self.assertEqual(dates.complete_when("tod"), "today 09:30")
        finally:
            dates.configure(due_time="18:00")

    def test_ambiguous_prefix_is_left_alone(self):
        # today, tonight and tomorrow all start with "to".
        self.assertIsNone(dates.complete_when("to"))
        self.assertIsNone(dates.complete_when("t"))
        self.assertIsNone(dates.complete_when(""))

    def test_a_weekday_and_a_month_complete(self):
        self.assertEqual(dates.complete_when("fri"), "friday 18:00")
        self.assertEqual(dates.complete_when("oct"), "october 18:00")

    def test_tonight_keeps_its_own_time(self):
        self.assertEqual(dates.complete_when("tonight"), "tonight")

    def test_only_the_last_word_is_completed(self):
        self.assertEqual(dates.complete_when("next fri"), "next friday 18:00")
        self.assertIsNone(dates.complete_when("next"))

    def test_a_finished_date_is_left_alone(self):
        self.assertIsNone(dates.complete_when("friday 18:00"))
        self.assertIsNone(dates.complete_when("2026-10-05"))
        self.assertIsNone(dates.complete_when("+2d"))
        self.assertIsNone(dates.complete_when("9:30"))

    def test_nonsense_is_left_alone(self):
        self.assertIsNone(dates.complete_when("sometime"))
        self.assertIsNone(dates.complete_when(None))


# ------------------------------------------------------------ completion


class Completion(TempStore):
    def setUp(self):
        super().setUp()
        self.fence = core.ensure_container(self.store, "Garden fence")
        self.shed = core.ensure_container(self.store, "Garden shed")
        self.diy = core.ensure_container(self.store, "DIY shelving")
        core.ensure_container(self.store, "Home admin", core.AREA)
        core.ensure_tag(self.store, "errands")
        core.ensure_tag(self.store, "hardware")
        core.ensure_tag(self.store, "holiday")
        self.capture("Buy nails", waiting_for="Ana")
        self.capture("Email Anders", waiting_for="Anders")

    def test_unique_prefix_completes(self):
        self.assertEqual(core.complete_name(self.store, "project", "Garden f"),
                         "Garden fence")
        self.assertEqual(core.complete_name(self.store, "project", "Garden sh"),
                         "Garden shed")
        self.assertEqual(core.complete_name(self.store, "project", "home adm"),
                         "Home admin")
        self.assertEqual(core.complete_name(self.store, "context", "err"),
                         "errands")

    def test_match_ignores_case(self):
        self.assertEqual(core.complete_name(self.store, "project", "diY"),
                         "DIY shelving")

    def test_exact_name_is_left_alone(self):
        # Otherwise "Gar" would drag "Garden fence" in behind a real project
        # called "Garage door", and an exact name is never wrong.
        self.assertIsNone(core.complete_name(self.store, "project", "Garden fence"))
        self.assertIsNone(core.complete_name(self.store, "context", "errands"))

    def test_ambiguous_prefix_is_left_alone(self):
        # Two gardens, so "Garde" is a coin flip and "G" is worse.
        self.assertIsNone(core.complete_name(self.store, "project", "Garde"))
        self.assertIsNone(core.complete_name(self.store, "project", "G"))
        self.assertIsNone(core.complete_name(self.store, "project", ""))
        self.assertIsNone(core.complete_name(self.store, "context", "h"))

    def test_no_match_is_left_alone(self):
        self.assertIsNone(core.complete_name(self.store, "project", "Zzz"))
        self.assertIsNone(core.complete_name(self.store, "waiting", "Z"))

    def test_waiting_comes_from_whoever_you_already_wait_on(self):
        self.assertEqual(core.complete_name(self.store, "waiting", "And"),
                         "Anders")
        # "An" is Ana and Anders both: a guess would be the wrong person.
        self.assertIsNone(core.complete_name(self.store, "waiting", "An"))

    def test_trashed_projects_are_not_offered(self):
        core.organize(self.store, self.diy["id"], inbox=True)
        core.organize(self.store, self.diy["id"], onhold=True)
        core.trash(self.store, self.diy["id"])
        self.assertIsNone(core.complete_name(self.store, "project", "DIY"))

    def test_unknown_kind_raises(self):
        with self.assertRaises(GtdError):
            core.complete_name(self.store, "place", "Ga")


# ------------------------------------------------------------- contexts


class Contexts(TempStore):
    def test_subcontext_creates_its_parent(self):
        tag = core.ensure_tag(self.store, "home>kitchen")
        self.assertEqual(tag["name"], "home>kitchen")
        parent = core.ensure_tag(self.store, "home")
        self.assertEqual(tag["parent_id"], parent["id"])
        names = [t["name"] for t in core.list_view(self.store, "tags")]
        self.assertEqual(names, ["home", "home>kitchen"])

    def test_capture_uses_a_subcontext(self):
        item = self.capture("Buy labels #home>kitchen")
        self.assertEqual(item["tags"], ["home>kitchen"])
        self.assertEqual(core.tag_depth("home>kitchen"), 1)
        rows = core.list_view(self.store, "tags")
        self.assertEqual([r["name"] for r in rows], ["home", "home>kitchen"])
        self.assertEqual(rows[1]["parent_id"], rows[0]["id"])

    def test_filtering_by_parent_includes_subcontexts(self):
        self.capture("Buy labels #home>kitchen")
        self.capture("Fix the tap #home>plumbing")
        self.capture("Watch a film #fun")
        titles = [r["title"] for r in core.list_view(self.store, "focus", tag="home")]
        self.assertEqual(sorted(titles), ["Buy labels", "Fix the tap"])
        only = core.list_view(self.store, "focus", tag="home>kitchen")
        self.assertEqual([r["title"] for r in only], ["Buy labels"])

    def test_empty_segment_is_rejected(self):
        with self.assertRaises(GtdError):
            core.ensure_tag(self.store, "home>>kitchen")

    def test_deleting_a_parent_removes_its_subcontexts(self):
        core.ensure_tag(self.store, "home>kitchen")
        core.ensure_tag(self.store, "home>plumbing")
        home = core.ensure_tag(self.store, "home")
        self.store.run("DELETE FROM tags WHERE id = ?", (home["id"],))
        self.assertEqual(core.list_view(self.store, "tags"), [])

    def test_tasks_view_lists_every_open_action_under_a_context(self):
        self.capture("Buy labels #home>kitchen")
        self.capture("Read a book #reading")
        finished = self.capture("Already finished #reading")
        core.engage(self.store, finished["id"], "done")
        titles = [r["title"] for r in core.list_view(self.store, "tasks", tag="home")]
        self.assertEqual(titles, ["Buy labels"])
        both = core.list_view(self.store, "tasks", tag="reading")
        self.assertEqual([r["title"] for r in both], ["Read a book"])
        self.assertEqual(len(core.list_view(self.store, "tasks")), 2)

    def test_contexts_are_stored_in_lowercase(self):
        tag = core.ensure_tag(self.store, "Errands")
        self.assertEqual(tag["name"], "errands")
        self.assertEqual([r["name"] for r in core.list_view(self.store, "tags")],
                         ["errands"])

    def test_contexts_differing_only_in_case_are_one_context(self):
        first = core.ensure_tag(self.store, "Errands")
        second = core.ensure_tag(self.store, "errands")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(core.list_view(self.store, "tags")), 1)

    def test_a_nested_context_is_lowercased_segment_by_segment(self):
        tag = core.ensure_tag(self.store, "Home>Kitchen")
        self.assertEqual(tag["name"], "home>kitchen")
        self.assertEqual([r["name"] for r in core.list_view(self.store, "tags")],
                         ["home", "home>kitchen"])

    def test_typed_case_does_not_split_a_context_list(self):
        item = self.capture("Buy labels #Errands #home>kitchen #Home>Hall")
        # Sorted, and home>kitchen/home>hall are separate subcontexts of one
        # parent — typing them in a different case does not merge them.
        self.assertEqual(item["tags"], ["errands", "home>hall", "home>kitchen"])
        self.assertEqual(sorted(r["name"] for r in core.list_view(self.store, "tags")),
                         ["errands", "home", "home>hall", "home>kitchen"])

    def test_one_tag_argument_can_hold_several_contexts(self):
        item = self.capture("Buy labels", tags=["Errands Hardware"])
        self.assertEqual(item["tags"], ["errands", "hardware"])

    def test_a_comma_separated_list_is_several_contexts(self):
        item = self.capture("Buy labels", tags=["#Errands, Hardware"])
        self.assertEqual(item["tags"], ["errands", "hardware"])

    def test_a_task_holds_many_contexts_at_once(self):
        item = self.capture("Buy labels", tags=["errands", "hardware", "home"])
        self.assertEqual(item["tags"], ["errands", "hardware", "home"])
        core.add_tags(self.store, item["id"], ["Garden"])
        self.assertEqual(core.tags_of(self.store, item["id"]),
                         ["errands", "garden", "hardware", "home"])
        # organize(tags=...) is the one that replaces the whole list.
        core.organize(self.store, item["id"], tags=["Home", "errands"])
        self.assertEqual(core.tags_of(self.store, item["id"]),
                         ["errands", "home"])

    def test_removing_contexts_splits_the_list_too(self):
        item = self.capture("Buy labels", tags=["errands", "hardware", "home"])
        core.remove_tags(self.store, item["id"], ["Errands hardware"])
        self.assertEqual(core.tags_of(self.store, item["id"]), ["home"])

    def test_split_names_keeps_subcontext_paths_whole(self):
        self.assertEqual(core.split_names(["#Home>Kitchen", "Errands Hardware"]),
                         ["Home>Kitchen", "Errands", "Hardware"])
        self.assertEqual(core.split_names([]), [])
        self.assertEqual(core.split_names(None), [])


# ------------------------------------------------------------- calendar


class Calendar(TempStore):
    def test_buckets_due_and_scheduled(self):
        self.capture("Due then", due="2026-05-12")
        self.capture("Start then", defer="2026-05-11")

        data = core.calendar(self.store, start="2026-05-10", end="2026-05-14")
        cells = {c["date"]: c for c in data["days"]}
        self.assertEqual(len(data["days"]), 5)
        self.assertEqual([i["title"] for i in cells["2026-05-12"]["due"]], ["Due then"])
        self.assertEqual([i["title"] for i in cells["2026-05-11"]["scheduled"]],
                         ["Start then"])
        self.assertEqual(data["totals"]["due"], 1)
        self.assertEqual(data["totals"]["scheduled"], 1)

    def test_only_todays_completions_are_shown(self):
        # Completed work is archived: the month grid should not go on listing
        # it, but today should still report what got finished.
        finished = self.capture("Finished today")
        core.engage(self.store, finished["id"], "done")
        older = self.capture("Finished in May")
        core.engage(self.store, older["id"], "done", when="2026-05-13 10:00")

        today = dt.date.today()
        start = (today - dt.timedelta(days=2)).isoformat()
        end = (today + dt.timedelta(days=2)).isoformat()
        data = core.calendar(self.store, start=start, end=end)
        cells = {c["date"]: c for c in data["days"]}

        self.assertEqual([i["title"] for i in cells[today.isoformat()]["completed"]],
                         ["Finished today"])
        self.assertEqual(data["totals"]["completed"], 1)
        self.assertEqual(
            core.calendar(self.store, start="2026-05-10",
                          end="2026-05-14")["totals"]["completed"], 0)
        # A day whose only entry was a completion is not a busy day.
        may = {c["date"]: c for c in
               core.calendar(self.store, start="2026-05-10", end="2026-05-14")["days"]}
        self.assertEqual(may["2026-05-13"]["count"], 0)

    def test_reversed_range_is_normalized(self):
        data = core.calendar(self.store, start="2026-05-14", end="2026-05-10")
        self.assertEqual(data["start"], "2026-05-10")
        self.assertEqual(data["end"], "2026-05-14")
        self.assertEqual(len(data["days"]), 5)

    def test_deferred_task_spans_defer_to_deadline(self):
        self.capture("Across the week", defer="2026-05-11", due="2026-05-15")
        data = core.calendar(self.store, start="2026-05-10", end="2026-05-16")
        cells = {c["date"]: c for c in data["days"]}
        for date in ("2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14"):
            self.assertEqual([i["title"] for i in cells[date]["scheduled"]],
                             ["Across the week"], date)
            self.assertEqual(cells[date]["due"], [])
        # The deadline day owns the task under `due`, not twice under scheduled.
        self.assertEqual([i["title"] for i in cells["2026-05-15"]["due"]],
                         ["Across the week"])
        self.assertEqual(cells["2026-05-15"]["scheduled"], [])
        self.assertEqual(cells["2026-05-10"]["scheduled"], [])
        self.assertEqual(data["totals"]["scheduled"], 4)
        self.assertEqual(data["totals"]["due"], 1)

    def test_deferred_task_without_a_deadline_sits_on_its_defer_day(self):
        self.capture("Whenever it comes round", defer="2026-05-11")
        data = core.calendar(self.store, start="2026-05-10", end="2026-05-14")
        cells = {c["date"]: c for c in data["days"]}
        self.assertEqual([i["title"] for i in cells["2026-05-11"]["scheduled"]],
                         ["Whenever it comes round"])
        self.assertEqual(cells["2026-05-12"]["scheduled"], [])

    def test_agenda_only_returns_busy_days(self):
        self.capture("Something", due="2026-05-12")
        cells = core.agenda(self.store, days=1, start="2026-05-12")
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0]["date"], "2026-05-12")
        self.assertEqual(core.agenda(self.store, days=1, start="2026-05-20"), [])


# --------------------------------------------------------- notifications


class Notifications(TempStore):
    def test_deadline_fires_once_and_again_when_moved(self):
        item = self.capture("Post the form >2020-01-01")
        pending = core.due_notifications(self.store)
        self.assertEqual([i["id"] for i in pending], [item["id"]])
        core.mark_notified(self.store, [item["id"]])
        self.assertEqual(core.due_notifications(self.store), [])
        core.organize(self.store, item["id"], due="2020-02-01")
        self.assertEqual(len(core.due_notifications(self.store)), 1)

    def test_future_deadline_does_not_fire(self):
        self.capture("Later >2999-01-01")
        self.assertEqual(core.due_notifications(self.store), [])

    def test_the_announcement_is_named_for_the_plugin(self):
        import argparse
        import subprocess
        from unittest import mock

        from gtd import cli

        self.capture("Post the form >2020-01-01")
        seen = []
        args = argparse.Namespace(json=False, quiet=True, no_send=False,
                                  dry_run=False)

        def fake_run(command, **kwargs):
            seen.append(list(command))
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(cli.shutil, "which",
                               lambda name: "/usr/bin/" + name), \
             mock.patch.object(cli.subprocess, "run", fake_run):
            result = cli.cmd_notify(args, cli.Out(args), self.store)

        self.assertEqual(result["sent"], 1)
        command = seen[0]
        # The headline is what a desktop shows; the app name is what it files
        # it under. Both have to say OmaGTD rather than the bare protocol name.
        self.assertIn("OmaGTD", command)
        self.assertTrue(command[-2].startswith("OmaGTD"), command[-2])
        self.assertEqual(command[command.index("--app-name") + 1], "OmaGTD")


# ------------------------------------------------------------- the tree


class Tree(TempStore):
    def test_actions_hang_off_their_project(self):
        core.ensure_container(self.store, "Home", "area")
        project = core.ensure_container(self.store, "Renovate")["id"]
        core.organize(self.store, project, area="Home")
        action = self.capture("Order tiles")["id"]
        core.organize(self.store, action, project="Renovate")

        data = core.tree(self.store)
        areas = {a["title"]: a for a in data["areas"]}
        self.assertIn("Home", areas)
        titles = [p["title"] for p in areas["Home"]["projects"]]
        self.assertEqual(titles, ["Renovate"])
        under = areas["Home"]["projects"][0]["actions"]
        self.assertEqual([a["title"] for a in under], ["Order tiles"])
        self.assertEqual(under[0]["id"], action)
        self.assertEqual([a["title"] for a in data["loose"]], [])

    def test_actions_without_a_project_are_listed_loose(self):
        self.capture("Just capture it #home")
        self.assertEqual([a["title"] for a in core.tree(self.store)["loose"]],
                         ["Just capture it"])

    def test_inbox_actions_are_not_listed_loose(self):
        # The project view is about assigned work; a bare capture belongs to
        # the inbox and does not also appear as an unattached action.
        self.capture("Just capture it")
        self.assertEqual(core.tree(self.store)["loose"], [])

    def test_project_with_no_area_gets_an_unnamed_group(self):
        core.ensure_container(self.store, "Side quest")
        data = core.tree(self.store)
        self.assertEqual([p["title"] for a in data["areas"] for p in a["projects"]],
                         ["Side quest"])

    def test_editing_into_a_project_works_without_the_onhold_flag(self):
        # Regression: the CLI's --onhold defaults to False, and testing that
        # flag against None made its default swallow --project and --area.
        core.ensure_container(self.store, "Renovate")
        item = self.capture("Order tiles")

        core.organize(self.store, item["id"], project="Renovate")

        row = core.get(self.store, item["id"])
        self.assertEqual(row["parent_title"], "Renovate")
        self.assertEqual(row["status"], "active")

    def test_organize_still_pauses_on_demand(self):
        core.ensure_container(self.store, "Renovate")
        item = self.capture("Order tiles")
        core.organize(self.store, item["id"], onhold=True)
        self.assertEqual(core.get(self.store, item["id"])["status"], "onhold")
        core.organize(self.store, item["id"], onhold=False, project="Renovate")
        self.assertEqual(core.get(self.store, item["id"])["parent_title"], "Renovate")

    def test_projects_can_nest_inside_projects(self):
        core.ensure_container(self.store, "Home", "area")
        parent = core.ensure_container(self.store, "Move house", area="Home")
        child = core.ensure_container(self.store, "Kitchen", parent="Move house")
        action = self.capture("Buy labels")["id"]
        core.organize(self.store, action, project="Kitchen")

        data = core.tree(self.store)
        home = next(a for a in data["areas"] if a["title"] == "Home")
        self.assertEqual([p["title"] for p in home["projects"]], ["Move house"])
        nested = home["projects"][0]["projects"]
        self.assertEqual([p["title"] for p in nested], ["Kitchen"])
        self.assertEqual(nested[0]["id"], child["id"])
        self.assertEqual([a["title"] for a in nested[0]["actions"]], ["Buy labels"])
        self.assertEqual(core.get(self.store, parent["id"])["parent_title"], "Home")

    def test_angle_path_creates_nested_projects(self):
        core.ensure_container(self.store, "Home", "area")
        leaf = core.ensure_container(self.store, "Move house>Kitchen>Labels",
                                     area="Home")
        self.assertEqual(leaf["title"], "Labels")
        parent = core.get(self.store, leaf["parent_id"])
        self.assertEqual(parent["title"], "Kitchen")
        grandparent = core.get(self.store, parent["parent_id"])
        self.assertEqual(grandparent["title"], "Move house")
        self.assertEqual(grandparent["parent_title"], "Home")

        # Re-running the path is idempotent, not a second tree.
        again = core.ensure_container(self.store, "Move house>Kitchen>Labels")
        self.assertEqual(again["id"], leaf["id"])

        data = core.tree(self.store)
        home = next(a for a in data["areas"] if a["title"] == "Home")
        self.assertEqual([p["title"] for p in home["projects"]], ["Move house"])
        kitchen = home["projects"][0]["projects"][0]
        self.assertEqual(kitchen["title"], "Kitchen")
        self.assertEqual([p["title"] for p in kitchen["projects"]], ["Labels"])

    def test_a_project_cannot_move_under_its_own_child(self):
        core.ensure_container(self.store, "Move house")
        core.ensure_container(self.store, "Kitchen", parent="Move house")
        with self.assertRaises(core.GtdError):
            core.ensure_container(self.store, "Move house", parent="Kitchen")
        parent = core.ensure_container(self.store, "Move house")
        with self.assertRaises(core.GtdError):
            core.organize(self.store, parent["id"], project="Kitchen")

    def test_nested_projects_survive_an_export_import(self):
        core.ensure_container(self.store, "Home", "area")
        core.ensure_container(self.store, "Move house", area="Home")
        core.ensure_container(self.store, "Kitchen", parent="Move house")
        action = self.capture("Buy labels")["id"]
        core.organize(self.store, action, project="Kitchen")
        payload = json.loads(importer.export_text(self.store, fmt="json"))

        fresh = Store(path=os.path.join(self.home, "nested.sqlite3"))
        self.addCleanup(fresh.close)
        importer.import_json(fresh, payload)

        home = next(a for a in core.tree(fresh)["areas"] if a["title"] == "Home")
        self.assertEqual([p["title"] for p in home["projects"]], ["Move house"])
        kitchen = home["projects"][0]["projects"][0]
        self.assertEqual(kitchen["title"], "Kitchen")
        self.assertEqual([a["title"] for a in kitchen["actions"]], ["Buy labels"])


# ------------------------------------------------------------- the pill


class PillCounts(TempStore):
    def test_due_today_and_overdue_are_two_buckets(self):
        today = core.now()[:10]
        self.capture("Later today", due=today + " 23:00")
        self.capture("Tomorrow", due=dates.parse_when("tomorrow").strftime("%Y-%m-%d 09:00"))

        numbers = core.bar_snapshot(self.store)
        self.assertEqual(numbers["due_today"], 1)
        self.assertEqual(numbers["overdue"], 0)

    def test_an_item_past_its_deadline_leaves_today_and_becomes_overdue(self):
        today = core.now()[:10]
        item = self.capture("Missed it", due=today + " 23:00")
        self.assertEqual(core.bar_snapshot(self.store)["due_today"], 1)

        # Yesterday's deadline, moved by hand so the test does not depend on
        # the wall clock.
        self.store.run("UPDATE items SET due_at = ? WHERE id = ?",
                       (dates.parse_when("yesterday").strftime("%Y-%m-%d 09:00"), item["id"]))

        numbers = core.bar_snapshot(self.store)
        self.assertEqual(numbers["due_today"], 0)
        self.assertEqual(numbers["overdue"], 1)

    def test_due_today_late_is_the_expired_slice_of_today(self):
        day = core.now()[:10]
        self.capture("Still to come", due=day + " 23:59")
        self.capture("Already gone", due=day + " 00:01")

        numbers = core.bar_snapshot(self.store)
        self.assertEqual(numbers["due_today"], 2)
        self.assertEqual(numbers["due_today_late"], 1)
        self.assertEqual(numbers["due_time"], "18:00")
        self.assertEqual(numbers["time_format"], "24h")


class Settings(TempStore):
    def test_settings_round_trip_and_validate(self):
        self.assertEqual(core.settings(self.store),
                         {"due_time": "18:00", "time_format": "24h",
                          "auto_remove_empty_projects": "7",
                          "auto_remove_empty_contexts": "7",
                          "done_retention_days": "30",
                          "calendar_show_completed": "off",
                          "theme": "omarchy"})

        self.assertEqual(core.set_setting(self.store, "due_time", "7:30"), "07:30")
        self.assertEqual(core.set_setting(self.store, "time_format", "12H"), "12h")
        self.assertEqual(core.settings(self.store)["due_time"], "07:30")

        # The empty-container settings are day counts, not on/off switches: a
        # number of days to keep, with 0 meaning keep forever.
        self.assertEqual(core.set_setting(self.store, "auto_remove_empty_projects", "10"), "10")
        self.assertEqual(core.set_setting(self.store, "auto_remove_empty_contexts", "0"), "0")
        self.assertEqual(core.settings(self.store)["auto_remove_empty_projects"], "10")

        self.assertEqual(core.set_setting(self.store, "done_retention_days", "90"), "90")
        self.assertEqual(core.set_setting(self.store, "done_retention_days", "0"), "0")
        self.assertEqual(core.settings(self.store)["done_retention_days"], "0")

        # The fullscreen app's palette is a name from a fixed set; the default
        # follows the desktop theme.
        self.assertEqual(core.set_setting(self.store, "theme", "OF-Light"), "of-light")
        self.assertEqual(core.settings(self.store)["theme"], "of-light")
        self.assertEqual(core.set_setting(self.store, "theme", "of-dark"), "of-dark")
        self.assertEqual(core.set_setting(self.store, "theme", "omarchy"), "omarchy")

        # The calendar knob is an on/off switch, off by default.
        self.assertEqual(core.set_setting(self.store, "calendar_show_completed", "TRUE"), "on")
        self.assertEqual(core.settings(self.store)["calendar_show_completed"], "on")
        self.assertEqual(core.set_setting(self.store, "calendar_show_completed", "off"), "off")

        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "due_time", "25:00")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "time_format", "military")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "auto_remove_empty_projects", "maybe")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "auto_remove_empty_projects", "on")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "done_retention_days", "weeks")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "calendar_show_completed", "sometimes")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "theme", "solarized")
        with self.assertRaises(core.GtdError):
            core.set_setting(self.store, "nonsense", "x")

    @staticmethod
    def _stamp(hours_ago):
        return (dt.datetime.now() - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M")

    def _age(self, table, ident, hours=25):
        self.store.run(f"UPDATE {table} SET updated_at = ? WHERE id = ?",
                       (self._stamp(hours), ident))

    def test_auto_remove_is_off_and_touches_nothing(self):
         # With the default 7-day window, an item aged only 25 hours is far
         # inside it, so nothing is taken and both lists keep everything.
        project = core.ensure_container(self.store, "Q4 audit", core.PROJECT)
        core.ensure_tag(self.store, "errands")
        self._age("items", project["id"])
        self._age("tags", core.ensure_tag(self.store, "errands")["id"])

          # The default 7-day window has not closed yet, so nothing is swept and
          # both containers are still in the database.
        self.assertEqual(core.sweep_empty(self.store),
                         {"projects": [], "contexts": []})
        self.assertIsNotNone(self.store.one(
             "SELECT id FROM items WHERE id = ?", (project["id"],)))
        self.assertIsNotNone(self.store.one(
             "SELECT id FROM tags WHERE id = ?",
              (core.ensure_tag(self.store, "errands")["id"],)))

    def test_auto_remove_projects_takes_only_the_aged_and_childless(self):
        old = core.ensure_container(self.store, "Old plan", core.PROJECT)
        fresh = core.ensure_container(self.store, "Fresh plan", core.PROJECT)
        busy = core.ensure_container(self.store, "Busy plan", core.PROJECT)
        self.capture("Dig", project="Busy plan")
        self._age("items", old["id"])
        self._age("items", busy["id"])

          # A one-day window: the 25-hour-old empty project crosses it, the busy
          # one has a task, the fresh one has not aged past the window. The
          # projects view only shows projects that still hold open work, so the
          # empty "Fresh plan" is not named there even though it survives.
        core.set_setting(self.store, "auto_remove_empty_projects", "1")
        self.assertEqual(core.sweep_empty(self.store)["projects"], ["Old plan"])
        self.assertEqual(
            sorted(p["title"] for p in core.list_view(self.store, "projects")),
              ["Busy plan"])

    def test_auto_remove_contexts_keeps_used_ones_and_live_parents(self):
        core.ensure_tag(self.store, "errands")           # never used
        core.ensure_tag(self.store, "home>kitchen")
        self.capture("Buy labels #home>kitchen")         # used, so it stays
        core.ensure_tag(self.store, "reading")           # never used, still fresh
        for name in ("errands", "home", "home>kitchen"):
            self._age("tags", core.ensure_tag(self.store, name)["id"])

        core.set_setting(self.store, "auto_remove_empty_contexts", "1")
           # Only "errands" is both empty and past the window; "home" holds a
           # used subcontext and "reading" is still fresh, so both survive.
        removed = core.sweep_empty(self.store)
        self.assertEqual(removed["contexts"], ["errands"])
        for name in ("home", "home>kitchen", "reading"):
            self.assertIsNotNone(self.store.one(
                  "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)))
        self.assertIsNone(self.store.one(
              "SELECT id FROM tags WHERE name = 'errands' COLLATE NOCASE"))

    def test_auto_remove_also_clears_the_lists_it_would_empty(self):
        project = core.ensure_container(self.store, "Old plan", core.PROJECT)
        core.ensure_tag(self.store, "errands")
        self._age("items", project["id"])
        self._age("tags", core.ensure_tag(self.store, "errands")["id"])

        def projects_in(payload):
            return [p for a in payload["areas"] for p in a["projects"]]

           # Off by default: nothing is swept, so both containers are still in
           # the database.
        self.assertEqual(core.sweep_empty(self.store),
                         {"projects": [], "contexts": []})
        self.assertIsNotNone(self.store.one(
              "SELECT id FROM items WHERE id = ?", (project["id"],)))
        self.assertIsNotNone(self.store.one(
              "SELECT id FROM tags WHERE name = 'errands' COLLATE NOCASE"))

        core.set_setting(self.store, "auto_remove_empty_projects", "1")
        core.set_setting(self.store, "auto_remove_empty_contexts", "1")
            # A one-day window has now closed for the aged, empty containers, so
            # the sweep takes them; the database no longer holds either.
        removed = core.sweep_empty(self.store)
        self.assertEqual(removed["projects"], ["Old plan"])
        self.assertEqual(removed["contexts"], ["errands"])
        self.assertIsNone(self.store.one(
              "SELECT id FROM items WHERE id = ?", (project["id"],)))
        self.assertIsNone(self.store.one(
              "SELECT id FROM tags WHERE name = 'errands' COLLATE NOCASE"))

    def test_sweep_done_keeps_tasks_only_the_set_number_of_days(self):
        old = self.capture("An old finish")
        core.engage(self.store, old["id"], "done")
        self.store.run(
             "UPDATE items SET completed_at = '2020-01-01 09:00' "
             "WHERE id = ?", (old["id"],))

        fresh = self.capture("A recent finish")
        core.engage(self.store, fresh["id"], "done")

          # The default keeps thirty days: the old one is gone, the recent
          # one is still within the window.
        self.assertEqual(core.sweep_done(self.store), [old["id"]])
        self.assertEqual(
            len(core.list_view(self.store, "done")), 1)

    def test_sweep_done_keeps_everything_forever_when_zero(self):
        task = self.capture("Keep me")
        core.engage(self.store, task["id"], "done")
        self.store.run(
             "UPDATE items SET completed_at = '2020-01-01 09:00' "
             "WHERE id = ?", (task["id"],))

        core.set_setting(self.store, "done_retention_days", "0")
        self.assertEqual(core.sweep_done(self.store), [])
        self.assertEqual(len(core.list_view(self.store, "done")), 1)

    def test_stats_look_back_thirty_days(self):
        fresh = self.capture("A fresh task")
        core.engage(self.store, fresh["id"], "done")

        old = self.capture("An old task")
        core.engage(self.store, old["id"], "done")
        self.store.run(
            "UPDATE items SET created_at = '2020-01-01 09:00', "
            "completed_at = '2020-01-02 09:00' WHERE id = ?", (old["id"],))

        stats = core.stats(self.store)
        self.assertEqual(stats["created_30"], 1)   # only the fresh one arrived
        self.assertEqual(stats["closed_30"], 1)    # only the fresh one closed


# ----------------------------------------------------------- attachments


class Attachments(TempStore):
    """One file per task, kept beside the database and let go with the task."""

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.home, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def managed(self, uri: str) -> str:
        return os.path.join(self.home, uri)

    def test_a_copy_is_recorded_relative_to_the_store(self):
        source = self.write("spec.txt", "the spec")
        item = self.capture("Read the spec", attach=[source])
        stored = core.attachments_of(self.store, item["id"])[0]["uri"]
        self.assertTrue(stored.startswith("attachments/"), stored)
        path = core.attachment_path(self.store, stored)
        self.assertEqual(path, core.attachments_dir(self.store)
                         / os.path.basename(stored))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "the spec")
        # The original is left where it was: the copy is what travels.
        self.assertTrue(os.path.exists(source))

    def test_a_url_is_held_as_it_came_and_is_never_copied(self):
        item = self.capture("Check the invoice",
                            attach=["https://example.com/invoice.pdf"])
        held = core.attachments_of(self.store, item["id"])
        self.assertEqual([a["uri"] for a in held],
                         ["https://example.com/invoice.pdf"])
        self.assertIsNone(core.attachment_path(self.store, held[0]["uri"]))

    def test_a_capture_keeps_the_last_file_it_was_given(self):
        first = self.write("one.txt", "1")
        second = self.write("two.txt", "2")
        item = self.capture("Something", attach=[first, second])
        held = core.attachments_of(self.store, item["id"])
        self.assertEqual([a["name"] for a in held], ["two.txt"])
        self.assertTrue(os.path.exists(self.managed(held[0]["uri"])))

    def test_the_second_attachment_replaces_the_first(self):
        first = self.write("first.txt", "one")
        second = self.write("second.txt", "two")
        item = self.capture("Something", attach=[first])
        first_uri = core.attachments_of(self.store, item["id"])[0]["uri"]
        core.add_attachment(self.store, item["id"], second)

        held = core.attachments_of(self.store, item["id"])
        self.assertEqual([a["name"] for a in held], ["second.txt"])
        # The copy the first one left behind has no row pointing at it now.
        self.assertFalse(os.path.exists(self.managed(first_uri)))
        self.assertTrue(os.path.exists(core.attachment_path(
            self.store, held[0]["uri"])))

    def test_a_copy_shared_by_two_tasks_outlives_one_of_them(self):
        source = self.write("photo.png", "the same bytes")
        one = self.capture("One", attach=[source])
        two = self.capture("Two", attach=[source])
        uri = core.attachments_of(self.store, one["id"])[0]["uri"]
        self.assertEqual(core.attachments_of(self.store, two["id"])[0]["uri"], uri)

        core.remove_attachment(self.store, one["id"], uri)
        self.assertEqual(core.attachments_of(self.store, one["id"]), [])
        self.assertTrue(os.path.exists(self.managed(uri)))

        core.remove_attachment(self.store, two["id"], uri)
        self.assertFalse(os.path.exists(self.managed(uri)))

    def test_the_trash_keeps_the_file_and_purging_takes_it(self):
        source = self.write("note.txt", "keep me")
        item = self.capture("Something", attach=[source])
        path = self.managed(core.attachments_of(self.store, item["id"])[0]["uri"])

        core.trash(self.store, item["id"])
        self.assertTrue(os.path.exists(path), "restoring still needs the file")
        core.restore(self.store, item["id"])
        self.assertTrue(os.path.exists(path))

        core.trash(self.store, item["id"])
        core.purge(self.store, item["id"])
        self.assertFalse(os.path.exists(path))

    def test_purging_a_project_takes_the_files_of_its_actions(self):
        source = self.write("plan.txt", "the plan")
        core.ensure_container(self.store, "Renovate", core.PROJECT)
        action = self.capture("Order tiles", project="Renovate", attach=[source])
        project = core.lookup_container(self.store, "Renovate", core.PROJECT)
        path = self.managed(core.attachments_of(self.store, action["id"])[0]["uri"])

        core.trash(self.store, project["id"])
        self.assertTrue(os.path.exists(path))
        core.purge(self.store, project["id"])
        # The action went with its project through the cascade, so its file
        # must not be left on disk with no row to say whose it was.
        self.assertFalse(os.path.exists(path))


# ------------------------------------------------------- the command line


class CommandLine(TempStore):
    def run_cli_result(self, *argv):
        import subprocess
        cli = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "gtd")
        env = dict(os.environ)
        env["GTD_DB"] = str(self.store.path)
        return subprocess.run(
            [sys.executable, cli, *argv, "--json"],
            capture_output=True, text=True, env=env)

    def run_cli(self, *argv):
        proc = self.run_cli_result(*argv)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_capture_and_filter_by_context(self):
        data = self.run_cli("capture", "Buy nails #errands")
        item_id = data["item"]["id"]
        self.assertEqual(data["item"]["tags"], ["errands"])

        listed = self.run_cli("list", "focus", "--tag", "errands")
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["id"], item_id)

    def test_edit_retitle(self):
        item_id = self.run_cli("capture", "Fix gate")["item"]["id"]
        self.run_cli("edit", item_id, "--title", "Fix the garden gate")
        shown = self.run_cli("show", item_id)
        self.assertEqual(shown["item"]["title"], "Fix the garden gate")

    def test_the_trash_empties_itself(self):
        item_id = self.run_cli("capture", "Oops")["item"]["id"]
        self.run_cli("trash", item_id)
        stamp = (dt.datetime.now() - dt.timedelta(days=31)).strftime("%Y-%m-%d %H:%M")
        self.store.run("UPDATE items SET updated_at = ? WHERE id = ?", (stamp, item_id))
        # Nothing was asked to be purged: the next ordinary command is enough.
        self.run_cli("bar")
        self.assertIsNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                         (item_id,)))

    def test_open_link_prints_the_link_without_launching(self):
        item_id = self.run_cli(
            "capture", "Read the manual",
            "--link", "https://example.com/docs")["item"]["id"]
        opened = self.run_cli("open", item_id, "--print")
        self.assertEqual(opened["link"], "https://example.com/docs")

    def test_open_fails_on_an_item_with_no_link(self):
        item_id = self.run_cli("capture", "No link here")["item"]["id"]
        proc = self.run_cli_result("open", item_id)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("carries no link", json.loads(proc.stdout)["error"])

    def test_settings_command_reports_and_sets_the_new_knobs(self):
        shown = self.run_cli("settings")
        self.assertEqual(shown["auto_remove_empty_projects"], "7")
        self.assertEqual(shown["auto_remove_empty_contexts"], "7")

         # A day count: 0 keeps the container forever.
        turned = self.run_cli("settings", "set", "auto_remove_empty_projects", "0")
        self.assertEqual(turned["auto_remove_empty_projects"], "0")

    def test_the_next_command_removes_an_empty_project_once_allowed(self):
        self.run_cli("project", "add", "Loose end")
          # It is empty, so the project list (which shows only open work) does
          # not name it; the id is read straight from the database.
        project_id = self.store.one(
              "SELECT id FROM items WHERE kind = 'project' AND title = 'Loose end'")["id"]
        stamp = (dt.datetime.now() - dt.timedelta(hours=25)).strftime("%Y-%m-%d %H:%M")
        self.store.run("UPDATE items SET updated_at = ? WHERE id = ?",
                        (stamp, project_id))

          # Still off: the next ordinary command leaves it where it is.
        self.run_cli("bar")
        self.assertIsNotNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                             (project_id,)))

          # Turning the window to one day, which the 25-hour-old project has
          # crossed, is enough — no purge command to remember.
        self.run_cli("settings", "set", "auto_remove_empty_projects", "1")
        self.run_cli("bar")
        self.assertIsNone(self.store.one("SELECT id FROM items WHERE id = ?",
                                          (project_id,)))

    def test_complete_command(self):
        self.run_cli("capture", "Buy nails", "--project", "Garden fence")
        self.run_cli("capture", "Email the plumber", "--tag", "errands")

        done = self.run_cli("complete", "project", "Garden f")
        self.assertEqual(done["completion"], "Garden fence")

        done = self.run_cli("complete", "context", "err")
        self.assertEqual(done["completion"], "errands")

        done = self.run_cli("complete", "project", "Zzz")
        self.assertIsNone(done["completion"])

    def test_calendar_command(self):
        self.run_cli("capture", "Due on the twelfth", "--due", "2026-05-12")
        data = self.run_cli("calendar", "--start", "2026-05-10", "--days", "5")
        self.assertEqual(data["start"], "2026-05-10")
        self.assertEqual(data["end"], "2026-05-14")
        self.assertEqual(data["totals"]["due"], 1)

    def test_tree_command(self):
        self.run_cli("area", "add", "Home")
        project = self.run_cli("project", "add", "Renovate", "--area", "Home")
        self.run_cli("capture", "Order tiles")
        self.run_cli("edit", "Order tiles", "--project", project["item"]["id"])

        data = self.run_cli("tree")
        areas = {a["title"]: a for a in data["areas"]}
        self.assertIn("Home", areas)
        self.assertEqual([p["title"] for p in areas["Home"]["projects"]], ["Renovate"])
        self.assertEqual(
            [a["title"] for a in areas["Home"]["projects"][0]["actions"]],
            ["Order tiles"])
        self.assertEqual(data["loose"], [])

    def test_export_zip_holds_json_and_a_database(self):
        import zipfile
        self.run_cli("capture", "Something worth keeping")

        target = os.path.join(self.home, "backup.zip")
        data = self.run_cli("export", "--format", "zip", "-o", target)
        self.assertTrue(data["ok"])
        self.assertEqual(data["output"], target)

        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            self.assertIn("gtd.json", names)
            self.assertIn(self.store.path.name, names)
            payload = json.loads(archive.read("gtd.json"))
        self.assertEqual(payload["items"][0]["title"], "Something worth keeping")

        with zipfile.ZipFile(target) as archive:
            archive.extract(self.store.path.name, self.home)
        restored = Store(os.path.join(self.home, self.store.path.name))
        self.addCleanup(restored.close)
        self.assertEqual(restored.scalar("SELECT title FROM items"),
                         "Something worth keeping")

    def test_import_zip_round_trip(self):
        self.run_cli("capture", "Recovered from a zip #errand")

        target = os.path.join(self.home, "roundtrip.zip")
        self.run_cli("export", "--format", "zip", "-o", target)

        fresh = Store(path=os.path.join(self.home, "fromzip.sqlite3"))
        self.addCleanup(fresh.close)
        result = importer.import_file(fresh, target)

        self.assertEqual(result["format"], "zip")
        self.assertEqual(result["actions"], 1)
        self.assertEqual(fresh.scalar("SELECT title FROM items"),
                         "Recovered from a zip")
        self.assertEqual(fresh.scalar("SELECT name FROM tags"), "errand")

    def test_link_and_attachments_round_trip(self):
        created = self.run_cli(
            "capture", "Pay the invoice", "--link", "https://bank.example/42",
            "--attach", "/tmp/invoice.pdf", "--attach", "receipt.png")
        item_id = created["item"]["id"]
        detail = self.run_cli("show", item_id)["item"]
        self.assertEqual(detail["url"], "https://bank.example/42")
        # One attachment per item: the later --attach is what the item holds.
        self.assertEqual([a["name"] for a in detail["attachments"]],
                         ["receipt.png"])

        self.run_cli("unattach", item_id, "receipt.png")
        self.run_cli("edit", item_id, "--unlink")
        detail = self.run_cli("show", item_id)["item"]
        self.assertIsNone(detail["url"])
        # The slot is empty again: there was only ever the one attachment.
        self.assertEqual([a["name"] for a in detail["attachments"]], [])

        self.run_cli("edit", item_id, "--link", "https://new.example")
        self.run_cli("attach", item_id, "extra.txt")
        detail = self.run_cli("show", item_id)["item"]
        self.assertEqual(detail["url"], "https://new.example")
        self.assertEqual([a["name"] for a in detail["attachments"]],
                         ["extra.txt"])

    def test_search_spans_titles_notes_projects_contexts_and_attachments(self):
        self.run_cli("area", "add", "Home")
        project = self.run_cli("project", "add", "Renovate", "--area", "Home")
        self.run_cli("capture", "Order tiles", "--project", project["item"]["id"],
                     "--tag", "errands")
        self.run_cli("capture", "Buy paint", "--notes", "the blue eggshell")
        self.run_cli("capture", "Pay the invoice", "--attach", "budget.pdf")

        def titles(query):
            return sorted(i["title"] for i in self.run_cli("search", query)["items"])

        self.assertEqual(titles("Renovate"), ["Order tiles", "Renovate"])
        self.assertEqual(titles("errands"), ["Order tiles"])
        self.assertEqual(titles("eggshell"), ["Buy paint"])
        self.assertEqual(titles("invoice"), ["Pay the invoice"])
        self.assertEqual(titles("budget.pdf"), ["Pay the invoice"])
        self.assertEqual(self.run_cli("search", "nothing-here")["count"], 0)

    def test_attachments_are_saved_and_travel_in_a_zip(self):
        import zipfile

        source = os.path.join(self.home, "spec.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("the spec")
        created = self.run_cli("capture", "Read the spec", "--attach", source)
        detail = self.run_cli("show", created["item"]["id"])["item"]
        stored = detail["attachments"][0]["uri"]
        # The row names the file's place beside the database, never the
        # absolute path of the machine it was attached on.
        self.assertTrue(stored.startswith("attachments/"), stored)
        self.assertTrue(os.path.exists(os.path.join(self.home, stored)))

        target = os.path.join(self.home, "with-files.zip")
        self.run_cli("export", "--format", "zip", "-o", target)
        with zipfile.ZipFile(target) as archive:
            bundled = [n for n in archive.namelist() if n.startswith("attachments/")]
        self.assertEqual(len(bundled), 1)

        fresh_home = os.path.join(self.home, "restore")
        os.makedirs(fresh_home)
        previous = os.environ.get("GTD_HOME")
        os.environ["GTD_HOME"] = fresh_home
        try:
            fresh = Store()
            self.addCleanup(fresh.close)
            result = importer.import_file(fresh, target)
            self.assertEqual(result["attachments"], 1)
            row = fresh.one("SELECT uri FROM attachments")
            self.assertTrue(row["uri"].startswith("attachments/"), row["uri"])
            with open(os.path.join(fresh_home, row["uri"]),
                      encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "the spec")
        finally:
            if previous is None:
                os.environ.pop("GTD_HOME", None)
            else:
                os.environ["GTD_HOME"] = previous


    def test_open_without_link_returns_reason(self):
        item_id = self.run_cli("capture", "Nope")["item"]["id"]
        proc = self.run_cli_result("open", item_id, "--json")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertIn("no link", data["error"].lower())
        self.assertEqual(data.get("reason"), "no-link")

    def test_list_pages_with_offset(self):
        ids = [self.run_cli("capture", f"Task {n}")["item"]["id"]
               for n in range(5)]
        # Inbox sorts by creation time; pin it so the pages line up.
        for n, item_id in enumerate(ids):
            self.store.run("UPDATE items SET created_at = ? WHERE id = ?",
                           (f"2024-01-0{n + 1} 09:00", item_id))

        first = self.run_cli("list", "inbox", "--limit", "2")
        self.assertEqual(first["count"], 2)
        self.assertEqual(first["offset"], 0)
        second = self.run_cli("list", "inbox", "--limit", "2", "--offset", "2")
        third = self.run_cli("list", "inbox", "--limit", "2", "--offset", "4")
        self.assertEqual(second["offset"], 2)
        # The three pages partition the view, in order, with no overlap.
        seen = [row["id"] for page in (first, second, third)
                for row in page["items"]]
        self.assertEqual(seen, ids)
        self.assertEqual(third["count"], 1)

    def test_search_pages_with_offset(self):
        self.run_cli("capture", "Widget 0")
        self.run_cli("capture", "Widget 1")
        self.run_cli("capture", "Widget 2")

        first = self.run_cli("search", "Widget", "--limit", "2")
        second = self.run_cli("search", "Widget", "--limit", "2", "--offset", "2")
        self.assertEqual(first["count"], 2)
        self.assertEqual(second["count"], 1)
        titles = [row["title"] for row in first["items"] + second["items"]]
        self.assertEqual(titles, ["Widget 0", "Widget 1", "Widget 2"])

    def test_context_list_pages_with_offset(self):
        for name in ("alpha", "beta", "gamma"):
            self.run_cli("capture", f"Task #{name}")

        first = self.run_cli("tag", "list", "--limit", "2")
        second = self.run_cli("tag", "list", "--limit", "2", "--offset", "2")
        self.assertEqual([tag["name"] for tag in first["tags"]], ["alpha", "beta"])
        self.assertEqual([tag["name"] for tag in second["tags"]], ["gamma"])

    def test_state_is_one_panel_snapshot(self):
        self.run_cli("area", "add", "Home")
        self.run_cli("project", "add", "Move", "--area", "Home")
        self.run_cli("capture", "Buy boxes", "--project", "Move")
        loose = self.run_cli("capture", "Loose thing #errand")["item"]["id"]
        # An action with no project stops being "inbox" once it is active,
        # which is what puts it in the unnamed bucket.
        self.store.run("UPDATE items SET status = 'active' WHERE id = ?", (loose,))
        self.run_cli("capture", "Inbox thing")

        data = self.run_cli("state")
        self.assertIn("settings", data)
        self.assertIn("stats", data)
        self.assertIn("db", data)
        self.assertEqual([a["title"] for a in data["areas"] if a["title"]], ["Home"])
        home = next(a for a in data["areas"] if a["title"] == "Home")
        move = next(p for p in home["projects"] if p["title"] == "Move")
        self.assertEqual(move["action_count"], 1)
        self.assertEqual(data["loose_count"], 1)
        self.assertEqual([c["name"] for c in data["contexts"]], ["errand"])

        paged = self.run_cli("state", "--view", "inbox", "--limit", "1")
        self.assertEqual(paged["view"]["key"], "inbox")
        self.assertEqual(paged["view"]["limit"], 1)
        self.assertEqual(paged["view"]["count"], 1)

    def test_subtree_returns_one_branch(self):
        self.run_cli("area", "add", "Home")
        self.run_cli("project", "add", "Move", "--area", "Home")
        self.run_cli("capture", "Box", "--project", "Move")

        home = next(a for a in self.run_cli("state")["areas"]
                    if a["title"] == "Home")
        move = next(p for p in home["projects"] if p["title"] == "Move")
        node = self.run_cli("subtree", move["id"])["container"]
        self.assertEqual(node["title"], "Move")
        self.assertEqual([a["title"] for a in node["actions"]], ["Box"])




class Undo(TempStore):
    def test_undo_edit_reverts_in_one_step(self):
        from lib.gtd import core
        core.undo_begin(self.store)
        item = core.capture(self.store, "Draft the report")
        core.undo_commit(self.store, "capture")
        self.assertEqual(item["title"], "Draft the report")
        core.undo_begin(self.store)
        core.organize(self.store, item["id"], title="Draft the final report")
        core.undo_commit(self.store, "edit")
        self.assertEqual(core.get(self.store, item["id"])["title"],
                         "Draft the final report")
        result = core.undo(self.store)
        self.assertIn("reverted", result)
        self.assertEqual(result["reverted"], "edit")
        self.assertEqual(core.get(self.store, item["id"])["title"],
                         "Draft the report")

    def test_undo_flag_then_flag_back(self):
        from lib.gtd import core
        core.undo_begin(self.store)
        item = core.capture(self.store, "Pay bill")
        core.undo_commit(self.store, "capture")
        core.undo_begin(self.store)
        core.engage(self.store, item["id"], "flag")
        core.undo_commit(self.store, "flag")
        self.assertEqual(core.get(self.store, item["id"])["priority"], 1)
        core.undo_begin(self.store)
        core.engage(self.store, item["id"], "unflag")
        core.undo_commit(self.store, "unflag")
        self.assertEqual(core.get(self.store, item["id"])["priority"], 0)
        core.undo(self.store, item["id"])
        self.assertEqual(core.get(self.store, item["id"])["priority"], 1)
        core.undo(self.store, item["id"])
        self.assertEqual(core.get(self.store, item["id"])["priority"], 0)


class Containers(TempStore):
    """The lazy left column: counts up front, actions on demand."""

    def setUp(self):
        super().setUp()
        core.ensure_container(self.store, "Home", kind="area")
        core.ensure_container(self.store, "Move", kind="project")
        core.organize(self.store, "Move", area="Home")
        self.capture("Box A", project="Move")
        self.capture("Box B", project="Move")
        core.ensure_container(self.store, "Sub", kind="project")
        core.organize(self.store, "Sub", project="Move")
        self.capture("Sub task", project="Sub")
        self.capture("Late one", project="Move", due="2020-01-01 09:00")

    def move(self):
        home = next(a for a in core.containers(self.store)["areas"]
                    if a["title"] == "Home")
        return home, next(p for p in home["projects"] if p["title"] == "Move")

    def test_containers_roll_counts_up_but_carry_no_actions(self):
        home, move = self.move()
        self.assertEqual(home["projects"][0]["title"], "Move")
        self.assertEqual(move["action_count"], 3)   # direct only
        self.assertEqual(move["total"], 4)          # the subproject counts
        self.assertEqual(move["late"], 1)
        self.assertNotIn("actions", move)

    def test_subtree_carries_actions_for_just_the_branch(self):
        _, move = self.move()
        node = core.subtree(self.store, move["id"])
        self.assertEqual(sorted(a["title"] for a in node["actions"]),
                         ["Box A", "Box B", "Late one"])
        self.assertEqual([p["title"] for p in node["projects"]], ["Sub"])
        self.assertEqual([a["title"] for a in node["projects"][0]["actions"]],
                         ["Sub task"])
        self.assertEqual(node["total"], 4)
        self.assertEqual(node["late"], 1)

    def test_subtree_of_an_area_lists_its_projects(self):
        home, _ = self.move()
        node = core.subtree(self.store, home["id"])
        self.assertEqual([p["title"] for p in node["projects"]], ["Move"])
        self.assertEqual(node["total"], 4)

    def test_subtree_rejects_something_that_is_not_a_container(self):
        item = self.capture("Just a task")
        with self.assertRaises(GtdError):
            core.subtree(self.store, item["id"])

