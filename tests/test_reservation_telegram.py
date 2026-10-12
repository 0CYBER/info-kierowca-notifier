import contextlib
import io
import json
import unittest
import logging
from unittest import mock
from info_kierowca_notifier.booking import reservation
from info_kierowca_notifier import ntfy_transport, notifier
from test_telegram_transport import _enabled_config, _Response, URLOPEN, SUCCESS, CHAT


class ReservationTelegramTests(unittest.TestCase):
    def test_result_is_mirrored_without_ntfy_configuration(self):
        disabled = ntfy_transport.NotificationOutcome(ntfy_transport.NOT_CONFIGURED, 'disabled')
        with mock.patch.object(reservation, 'read_config', return_value=_enabled_config(ntfy_topic='')), \
                mock.patch.object(reservation.ntfy_transport, 'push_ntfy', return_value=disabled), \
                mock.patch(URLOPEN, return_value=_Response()) as send:
            self.assertIs(reservation.push_ntfy('', 'Result', 'Body'), disabled)
        send.assert_called_once()

    def test_telegram_failure_preserves_ntfy_result(self):
        failed = ntfy_transport.NotificationOutcome(ntfy_transport.NETWORK_ERROR, 'down')
        with mock.patch.object(reservation, 'read_config', return_value=_enabled_config()), \
                mock.patch.object(reservation.ntfy_transport, 'push_ntfy', return_value=SUCCESS), \
                mock.patch.object(reservation.telegram_transport, 'push_from_config', return_value=failed):
            self.assertIs(reservation.push_ntfy('topic', 'Result', 'Body'), SUCCESS)

    def test_create_outcomes_are_mirrored(self):
        target = {'word': 'PORD Gdansk', 'organization_id': 43,
                  'exam_type': 'Theoretical', 'datetime': '2026-11-01T10:00:00'}
        cases = [
            ({'status': 400, 'ok': False, 'message': 'slot unavailable'}, {'status': 'step5_reached'}, 'rejected'),
            ({'status': 200, 'ok': True}, {'status': 'step5_reached'}, 'payment/review required'),
            ({'status': 200, 'ok': True}, {'status': 'unknown'}, 'verify browser'),
            (None, {'status': 'portal_message', 'message': 'unavailable'}, 'not confirmed'),
            (None, {'status': 'unknown'}, 'attempted'),
        ]
        for api, dom, expected in cases:
            with self.subTest(expected=expected), contextlib.ExitStack() as stack:
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                for name in ('wait_and_expand_date', 'wait_and_select_slot', 'wait_and_click_enabled',
                             'wait_for_page_text', 'wait_and_click_radio_selector', 'summary_matches_target',
                             'target_beats_current_slot', 'record_confirm_cooldown', 'reset_confirm_marker',
                             'click_confirm_once'):
                    stack.enter_context(mock.patch.object(reservation, name, return_value=True))
                observer = stack.enter_context(mock.patch.object(reservation, 'ReservationCreateObserver'))
                observer.return_value.start.return_value.wait.return_value = api
                stack.enter_context(mock.patch.object(reservation, 'observe_post_create', return_value=dom))
                stack.enter_context(mock.patch.object(reservation, 'read_config', return_value=_enabled_config()))
                stack.enter_context(mock.patch.object(reservation.ntfy_transport, 'push_ntfy', return_value=SUCCESS))
                telegram = stack.enter_context(mock.patch.object(reservation.telegram_transport, 'push_from_config', return_value=SUCCESS))
                reservation.try_select_target_slot('localhost', 9555, json.dumps(target), confirm=True)
                telegram.assert_called_once()
                self.assertIn(expected, telegram.call_args.args[1])
                self.assertEqual(telegram.call_args.kwargs['priority'], 'urgent')

    def test_utf8_output_supports_windows_legacy_locale(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding='cp1251')
        try:
            with mock.patch.object(reservation.sys, 'stdout', stream), \
                    mock.patch.object(reservation.sys, 'stderr', io.StringIO()):
                reservation.configure_utf8_output()
                print('Zmie\u0144 termin')
            self.assertEqual(stream.encoding, 'utf-8')
            self.assertIn('Zmie\u0144', raw.getvalue().decode('utf-8'))
        finally:
            stream.close()

    def test_auto_select_never_creates_a_reservation(self):
        target = {'word': 'PORD Gdansk', 'organization_id': 43,
                  'exam_type': 'Theoretical', 'datetime': '2026-11-01T10:00:00'}
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            for name in ('wait_and_expand_date', 'wait_and_select_slot', 'wait_and_click_enabled',
                         'wait_for_page_text', 'wait_and_click_radio_selector', 'summary_matches_target'):
                stack.enter_context(mock.patch.object(reservation, name, return_value=True))
            click = stack.enter_context(mock.patch.object(reservation, 'click_confirm_once'))
            observer = stack.enter_context(mock.patch.object(reservation, 'ReservationCreateObserver'))
            reservation.try_select_target_slot('localhost', 9555, json.dumps(target), confirm=False)
            click.assert_not_called()
            observer.assert_not_called()


class AutoReservationSlotAlertTests(unittest.TestCase):
    def _check_slot_alert(self, launch_result):
        import tempfile
        from pathlib import Path
        from info_kierowca_notifier import client
        from info_kierowca_notifier.booking import launch as booking_launch

        results = [{
            "wordId": 26,
            "wordName": "Test WORD",
            "examCollectionForDay": [
                {"examType": "Theoretical", "theoryDateTime": "2026-08-20T10:00:00", "placeTheoryAmount": 2},
            ],
        }]

        def request(_url, _session, method="GET", json_body=None):
            return (204, b"", {}) if method == "GET" else (200, json.dumps(results).encode(), {})

        class FixedDateTime(__import__("datetime").datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 9, 12, 0, 0, tzinfo=tz)

        config = _enabled_config(
            organization_ids=[26], category=5, profile_number="123", exam_types=["Theoretical"],
            current_slot_date="2026-09-14", search_mode="multi", phone_alerts=True,
            auto_open_browser=True, auto_select_slot=True, auto_confirm_reschedule=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_file, session_file = root / "config.json", root / "session.json"
            config_file.write_text(json.dumps(config))
            session_file.write_text(json.dumps({"cookies": {}}))
            with mock.patch.object(notifier, "CONFIG_FILE", config_file), \
                    mock.patch.object(notifier, "SESSION_FILE", session_file), \
                    mock.patch.object(notifier, "PAUSE_FILE", root / "pause"), \
                    mock.patch.object(notifier, "STATUS_FILE", root / "status.json"), \
                    mock.patch.object(client, "do_request", side_effect=request), \
                    mock.patch.object(notifier, "datetime", FixedDateTime), \
                    mock.patch.object(notifier, "build_search_organization_ids", return_value=[26] * 5), \
                    mock.patch.object(booking_launch, "trigger_open_browser", return_value=launch_result) as browser, \
                    mock.patch.object(notifier, "push_ntfy") as ntfy, \
                    mock.patch.object(notifier, "push_telegram") as telegram:
                state = {"paused": False}
                events = []
                telegram.side_effect = lambda *a, **kw: events.append('telegram')
                browser.side_effect = lambda *a, **kw: events.append('browser') or launch_result
                notifier.run_check(logging.getLogger("test"), state)
                notifier.run_check(logging.getLogger("test"), state)
                self.assertEqual(events, ['telegram', 'browser'])
                self.assertTrue(browser.call_args.args[1]['auto_confirm_reschedule'])

        ntfy.assert_called_once()
        telegram.assert_called_once()
        _logger, sent_config, title, body = telegram.call_args.args
        self.assertEqual(sent_config["telegram_chat_id"], CHAT)
        self.assertEqual(title, "Slot within range!")
        self.assertIn(notifier.TELEGRAM_BOOKING_URL, body)
        self.assertEqual(telegram.call_args.kwargs["priority"], "urgent")

    def test_slot_alert_precedes_launch(self):
        self._check_slot_alert('launched')

    def test_slot_alert_when_browser_is_already_open(self):
        self._check_slot_alert('already_running')

    def test_slot_alert_when_launch_fails(self):
        self._check_slot_alert('launch_failed')
