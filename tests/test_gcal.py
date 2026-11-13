import unittest
from unittest.mock import Mock, patch

from gmail_mcp import gcal, server

EVENTS = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_LIST = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"


def all_day(event_id="e1", first="2027-03-01", after_last="2027-03-04", **extra):
    return {"id": event_id, "summary": "Conference", "start": {"date": first}, "end": {"date": after_last}, **extra}


class ServiceForTests(unittest.TestCase):
    def resolve(self, provider="google", scopes=()):
        return patch.object(gcal.auth, "resolve_provider",
                            return_value=("a@example.com", provider, {"scopes": list(scopes)}))

    def test_microsoft_accounts_are_refused_before_credentials(self):
        with self.resolve("microsoft"), patch.object(gcal.auth, "google_credentials") as creds:
            with self.assertRaisesRegex(ValueError, "only available for Google"):
                gcal.service_for(None, gcal.READ_EVENTS)
        creds.assert_not_called()

    def test_a_missing_grant_asks_for_a_reconnect_without_refreshing(self):
        with self.resolve(scopes=["https://mail.google.com/"]), \
             patch.object(gcal.auth, "google_credentials") as creds:
            with self.assertRaisesRegex(PermissionError, "add_account"):
                gcal.service_for(None, gcal.READ_EVENTS)
        creds.assert_not_called()

    def test_every_need_must_be_granted(self):
        with self.resolve(scopes=[EVENTS]), patch.object(gcal.auth, "google_credentials") as creds:
            with self.assertRaises(PermissionError):
                gcal.service_for(None, gcal.WRITE_EVENTS, gcal.READ_CALENDARS)
        creds.assert_not_called()

    def test_granted_accounts_get_a_calendar_service(self):
        with self.resolve(scopes=[EVENTS, CALENDAR_LIST]), \
             patch.object(gcal.auth, "google_credentials", return_value="creds"), \
             patch.object(gcal, "build", return_value="svc") as build:
            result = gcal.service_for(None, gcal.WRITE_EVENTS, gcal.READ_CALENDARS)
        self.assertEqual(result, ("a@example.com", "svc"))
        build.assert_called_once_with("calendar", "v3", credentials="creds", cache_discovery=False)

    def test_connecting_an_account_requests_every_calendar_need(self):
        requested = set(gcal.auth.GOOGLE_SCOPES)
        for need in (gcal.READ_EVENTS, gcal.WRITE_EVENTS, gcal.READ_CALENDARS):
            self.assertTrue(need & requested)


class EventTimesTests(unittest.TestCase):
    def test_an_all_day_end_is_the_last_day(self):
        self.assertEqual(gcal.event_times("2027-03-01", "2027-03-03", None),
                         ({"date": "2027-03-01"}, {"date": "2027-03-04"}))

    def test_a_one_day_event_starts_and_ends_on_the_same_date(self):
        self.assertEqual(gcal.event_times("2027-03-05", "2027-03-05", None)[1], {"date": "2027-03-06"})

    def test_times_with_offsets_need_no_zone(self):
        self.assertEqual(
            gcal.event_times("2027-03-08T15:00:00+09:00", "2027-03-08T17:00:00+09:00", None),
            ({"dateTime": "2027-03-08T15:00:00+09:00"}, {"dateTime": "2027-03-08T17:00:00+09:00"}))

    def test_times_without_offsets_carry_the_zone(self):
        start, _ = gcal.event_times("2027-03-08T15:00", "2027-03-08T17:00", "Asia/Tokyo")
        self.assertEqual(start, {"dateTime": "2027-03-08T15:00:00", "timeZone": "Asia/Tokyo"})

    def test_a_patch_clears_the_other_form(self):
        start, _ = gcal.event_times("2027-03-05", "2027-03-05", None, clear_other=True)
        self.assertEqual(start, {"date": "2027-03-05", "dateTime": None, "timeZone": None})
        start, _ = gcal.event_times("2027-03-08T15:00:00+09:00", "2027-03-08T16:00:00+09:00", None,
                                    clear_other=True)
        self.assertEqual(start, {"dateTime": "2027-03-08T15:00:00+09:00", "date": None})

    def test_invalid_spans_are_refused(self):
        cases = [
            (("2027-03-03", "2027-03-01", None), "last day"),
            (("2027-03-08", "2027-03-08T17:00:00+09:00", None), "both be dates"),
            (("2027-03-08T15:00", "2027-03-08T17:00:00+09:00", "UTC"), "offset, or neither"),
            (("2027-03-08T15:00", "2027-03-08T17:00", None), "need a time zone"),
            (("2027-03-08T17:00:00+09:00", "2027-03-08T15:00:00+09:00", None), "after start"),
            (("2027-03-08T15:00", "2027-03-08T17:00", "Mars/Olympus"), "Unknown time zone"),
            (("tomorrow", "2027-03-08", None), "must be a date"),
        ]
        for args, message in cases:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                gcal.event_times(*args)

    def test_checking_times_allows_a_zone_to_be_looked_up_later(self):
        self.assertIsNone(gcal.check_times("2027-03-08T15:00", "2027-03-08T17:00", None))
        with self.assertRaisesRegex(ValueError, "both be dates"):
            gcal.check_times("2027-03-08", "2027-03-08T17:00", None)

    def test_which_values_need_a_zone(self):
        self.assertFalse(gcal.bound_needs_zone(None))
        self.assertTrue(gcal.bound_needs_zone("2027-03-08"))
        self.assertTrue(gcal.bound_needs_zone("2027-03-08T15:00"))
        self.assertFalse(gcal.bound_needs_zone("2027-03-08T15:00:00Z"))
        self.assertFalse(gcal.times_need_zone("2027-03-08", "2027-03-09"))
        self.assertTrue(gcal.times_need_zone("2027-03-08T15:00", "2027-03-08T16:00"))
        self.assertFalse(gcal.times_need_zone("2027-03-08T15:00+09:00", "2027-03-08T16:00+09:00"))


class BoundTests(unittest.TestCase):
    def test_dates_cover_whole_days_in_the_zone(self):
        self.assertEqual(gcal.bound("2027-03-08", "Asia/Tokyo"), "2027-03-08T00:00:00+09:00")
        self.assertEqual(gcal.bound("2027-03-08", "Asia/Tokyo", end=True), "2027-03-09T00:00:00+09:00")

    def test_times_keep_their_offset_or_take_the_zone(self):
        self.assertEqual(gcal.bound("2027-03-08T15:00:00+09:00", None), "2027-03-08T15:00:00+09:00")
        self.assertEqual(gcal.bound("2027-03-08T15:00", "UTC"), "2027-03-08T15:00:00+00:00")

    def test_a_date_without_a_zone_is_refused(self):
        with self.assertRaisesRegex(ValueError, "time zone"):
            gcal.bound("2027-03-08", None)


class SimplifyTests(unittest.TestCase):
    def test_all_day_events_end_on_their_last_day_and_drop_empty_fields(self):
        event = all_day(status="confirmed", eventType="default", location="", htmlLink="https://example.com/e1",
                        attendees=[{"email": "b@example.com", "responseStatus": "accepted"}])
        self.assertEqual(gcal.simplify(event), {
            "id": "e1", "summary": "Conference", "all_day": True, "start": "2027-03-01", "end": "2027-03-03",
            "status": "confirmed", "attendees": [{"email": "b@example.com", "response": "accepted"}],
            "html_link": "https://example.com/e1",
        })

    def test_timed_events_keep_their_times_and_zone(self):
        event = {"id": "e2", "summary": "Workshop", "eventType": "birthday",
                 "start": {"dateTime": "2027-03-08T15:00:00+09:00", "timeZone": "Asia/Tokyo"},
                 "end": {"dateTime": "2027-03-08T17:00:00+09:00"}}
        result = gcal.simplify(event)
        self.assertFalse(result["all_day"])
        self.assertEqual((result["start"], result["end"]), ("2027-03-08T15:00:00+09:00", "2027-03-08T17:00:00+09:00"))
        self.assertEqual((result["time_zone"], result["event_type"]), ("Asia/Tokyo", "birthday"))

    def test_lists_cut_long_descriptions_and_leave_out_links(self):
        result = gcal.simplify(all_day(description="abcdef", htmlLink="https://example.com/e1"),
                               max_description=3, link=False)
        self.assertEqual((result["description"], result["description_truncated"]), ("abc", True))
        self.assertNotIn("html_link", result)


class ListTests(unittest.TestCase):
    def test_short_event_pages_are_filled_and_continuation_returned(self):
        svc = Mock()
        svc.events.return_value.list.return_value.execute.side_effect = [
            {"items": [all_day("1")], "nextPageToken": "two", "timeZone": "Asia/Tokyo"},
            {"items": [all_day("2")], "nextPageToken": "three", "timeZone": "Asia/Tokyo"},
        ]
        result = gcal.list_events(svc, "primary", 2, timeMin="2027-02-01T00:00:00+09:00")
        self.assertEqual([e["id"] for e in result["events"]], ["1", "2"])
        self.assertEqual((result["next_page_token"], result["time_zone"]), ("three", "Asia/Tokyo"))
        calls = svc.events.return_value.list.call_args_list
        self.assertEqual([c.kwargs["maxResults"] for c in calls], [2, 1])
        self.assertEqual(calls[1].kwargs["pageToken"], "two")
        first = calls[0].kwargs
        self.assertEqual((first["singleEvents"], first["orderBy"]), (True, "startTime"))
        self.assertEqual(first["timeMin"], "2027-02-01T00:00:00+09:00")
        self.assertIn("nextPageToken", first["fields"])

    def test_event_loops_and_invalid_limits_fail(self):
        svc = Mock()
        svc.events.return_value.list.return_value.execute.return_value = {"nextPageToken": "same"}
        with self.assertRaisesRegex(RuntimeError, "pagination loop"):
            gcal.list_events(svc, "primary", 2)
        for value in (0, -1, True, "2"):
            with self.assertRaises(ValueError):
                gcal.list_events(svc, "primary", value)

    def test_calendars_follow_every_page(self):
        svc = Mock()
        svc.calendarList.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "p", "summary": "Main", "primary": True, "accessRole": "owner",
                        "timeZone": "Asia/Tokyo"}], "nextPageToken": "next"},
            {"items": [{"id": "h", "summary": "Holidays", "summaryOverride": "Public holidays",
                        "accessRole": "reader"}]},
        ]
        result = gcal.list_calendars(svc)
        self.assertEqual([(c["id"], c["name"], c["primary"]) for c in result],
                         [("p", "Main", True), ("h", "Public holidays", False)])

    def test_calendar_list_loops_fail(self):
        svc = Mock()
        svc.calendarList.return_value.list.return_value.execute.return_value = {"nextPageToken": "same"}
        with self.assertRaisesRegex(RuntimeError, "pagination loop"):
            gcal.list_calendars(svc)


class CalendarToolTests(unittest.TestCase):
    def service(self, returned=None):
        svc = Mock()
        for method in ("insert", "patch", "get"):
            getattr(svc.events.return_value, method).return_value.execute.return_value = returned or all_day()
        return patch.object(server.gcal, "service_for", return_value=("a@example.com", svc)), svc

    def test_creating_an_all_day_event_sends_googles_end_and_returns_the_last_day(self):
        service, svc = self.service()
        with service as service_for:
            result = server.calendar_create_event("Conference", "2027-03-01", "2027-03-03", location="Main hall")
        service_for.assert_called_once_with(None, gcal.WRITE_EVENTS)
        kwargs = svc.events.return_value.insert.call_args.kwargs
        self.assertEqual(kwargs["body"], {"summary": "Conference", "start": {"date": "2027-03-01"},
                                          "end": {"date": "2027-03-04"}, "location": "Main hall"})
        self.assertEqual((kwargs["calendarId"], kwargs["sendUpdates"]), ("primary", "none"))
        self.assertEqual(result["created"]["end"], "2027-03-03")

    def test_local_times_use_the_calendars_zone(self):
        service, svc = self.service()
        with service as service_for, \
             patch.object(server.gcal, "calendar_time_zone", return_value="Asia/Tokyo") as zone:
            server.calendar_create_event("Workshop", "2027-03-08T15:00", "2027-03-08T17:00")
        service_for.assert_called_once_with(None, gcal.WRITE_EVENTS, gcal.READ_CALENDARS)
        zone.assert_called_once_with(svc, "primary")
        body = svc.events.return_value.insert.call_args.kwargs["body"]
        self.assertEqual(body["start"], {"dateTime": "2027-03-08T15:00:00", "timeZone": "Asia/Tokyo"})

    def test_guests_and_invitations_are_passed_on(self):
        service, svc = self.service()
        with service:
            server.calendar_create_event("Workshop", "2027-03-12", "2027-03-12", attendees=["b@example.com"],
                                         send_updates="all")
        kwargs = svc.events.return_value.insert.call_args.kwargs
        self.assertEqual(kwargs["body"]["attendees"], [{"email": "b@example.com"}])
        self.assertEqual(kwargs["sendUpdates"], "all")

    def test_bad_requests_fail_before_credentials(self):
        service, _ = self.service()
        bad_calls = [
            lambda: server.calendar_create_event("x", "2027-03-08", "2027-03-08T17:00"),
            lambda: server.calendar_create_event("x", "2027-03-08", "2027-03-08", send_updates="everyone"),
            lambda: server.calendar_create_event("x", "2027-03-08", "2027-03-08", attendees=["not an email"]),
            lambda: server.calendar_update_event("e1", start="2027-03-08"),
            lambda: server.calendar_update_event("e1"),
            lambda: server.calendar_list_events(time_zone="Mars/Olympus"),
            lambda: server.calendar_list_events(time_min="soon"),
        ]
        with service as service_for:
            for call in bad_calls:
                with self.assertRaises(ValueError):
                    call()
        service_for.assert_not_called()

    def test_updates_patch_only_what_was_given(self):
        service, svc = self.service()
        with service as service_for:
            server.calendar_update_event("e1", summary="New name", location="")
        service_for.assert_called_once_with(None, gcal.WRITE_EVENTS)
        kwargs = svc.events.return_value.patch.call_args.kwargs
        self.assertEqual(kwargs["body"], {"summary": "New name", "location": ""})
        self.assertEqual((kwargs["eventId"], kwargs["sendUpdates"]), ("e1", "none"))

    def test_moving_to_all_day_clears_the_old_time(self):
        service, svc = self.service()
        with service:
            server.calendar_update_event("e1", start="2027-03-05", end="2027-03-05")
        body = svc.events.return_value.patch.call_args.kwargs["body"]
        self.assertEqual(body["end"], {"date": "2027-03-06", "dateTime": None, "timeZone": None})

    def test_deleting_returns_what_was_deleted(self):
        service, svc = self.service()
        with service:
            result = server.calendar_delete_event("e1")
        svc.events.return_value.delete.assert_called_once_with(calendarId="primary", eventId="e1", sendUpdates="none")
        self.assertEqual((result["deleted"]["summary"], result["deleted"]["end"]), ("Conference", "2027-03-03"))

    def test_listing_defaults_to_now(self):
        service, svc = self.service()
        with service as service_for, patch.object(server.gcal, "now", return_value="NOW"), \
             patch.object(server.gcal, "list_events", return_value={"events": []}) as list_events:
            server.calendar_list_events()
        service_for.assert_called_once_with(None, gcal.READ_EVENTS)
        list_events.assert_called_once_with(svc, "primary", 25, None, timeMin="NOW")

    def test_listing_whole_days_uses_the_calendars_zone(self):
        service, svc = self.service()
        with service as service_for, \
             patch.object(server.gcal, "calendar_time_zone", return_value="Asia/Tokyo"), \
             patch.object(server.gcal, "list_events", return_value={"events": []}) as list_events:
            server.calendar_list_events(time_min="2027-03-08", time_max="2027-03-08", query="lunch")
        service_for.assert_called_once_with(None, gcal.READ_EVENTS, gcal.READ_CALENDARS)
        params = list_events.call_args.kwargs
        self.assertEqual(params, {"timeMin": "2027-03-08T00:00:00+09:00",
                                  "timeMax": "2027-03-09T00:00:00+09:00", "q": "lunch"})

    def test_getting_an_event_returns_its_link(self):
        service, _ = self.service(all_day(htmlLink="https://example.com/e1"))
        with service:
            result = server.calendar_get_event("e1")
        self.assertEqual(result["event"]["html_link"], "https://example.com/e1")


if __name__ == "__main__":
    unittest.main()
