import io
import json
import logging
import ssl
import urllib.error
import unittest
from unittest import mock

import info_kierowca_notifier.app as app
from info_kierowca_notifier.auth import session as auto_refresh_session
from info_kierowca_notifier import notifier
from info_kierowca_notifier import ntfy_transport
from info_kierowca_notifier import telegram_transport
from info_kierowca_notifier.booking import reschedule as open_logged_in_browser
from info_kierowca_notifier import tls_transport

URLOPEN = "info_kierowca_notifier.telegram_transport.tls_transport.urlopen"
TOKEN = "123456789:AAE" + "x" * 32
CHAT = "987654321"
SUCCESS = ntfy_transport.NotificationOutcome(ntfy_transport.SUCCESS, "accepted", 200)


class _Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = json.dumps({"ok": True, "result": {}} if body is None else body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _http_error(code, description):
    body = io.BytesIO(json.dumps({"ok": False, "error_code": code, "description": description}).encode())
    return urllib.error.HTTPError(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", code, "error", {}, body
    )


def _enabled_config(**overrides):
    config = {
        "telegram_enabled": True,
        "telegram_bot_token": TOKEN,
        "telegram_chat_id": CHAT,
        "ntfy_topic": "topic",
    }
    config.update(overrides)
    return config


class TelegramTransportTests(unittest.TestCase):
    def test_success_posts_send_message_to_the_bot_api(self):
        with mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body", priority="urgent")

        self.assertTrue(outcome.ok)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["chat_id"], CHAT)
        self.assertIn("Title", payload["text"])
        self.assertIn("Body", payload["text"])
        self.assertNotIn("disable_notification", payload)

    def test_low_priority_messages_are_silent(self):
        with mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body", priority="low")
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertTrue(payload["disable_notification"])

    def test_long_messages_are_truncated_to_telegrams_limit(self):
        with mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            telegram_transport.push_telegram(TOKEN, CHAT, "Title", "x" * 10000)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertLessEqual(len(payload["text"]), telegram_transport.MAX_MESSAGE_LENGTH)

    def test_missing_or_malformed_settings_never_reach_the_network(self):
        cases = [
            ("", CHAT), (TOKEN, ""), ("not-a-token", CHAT), (TOKEN + "/../x", CHAT),
            (TOKEN, "12 34"), (TOKEN, "@a"), (TOKEN, "chat?x=1"),
        ]
        for token, chat_id in cases:
            with mock.patch(URLOPEN) as urlopen:
                outcome = telegram_transport.push_telegram(token, chat_id, "Title", "Body")
            urlopen.assert_not_called()
            self.assertEqual(outcome.kind, ntfy_transport.NOT_CONFIGURED, (token, chat_id))

    def test_accepted_chat_id_forms(self):
        for chat_id in ("123456789", "-1001234567890", "@my_channel"):
            self.assertTrue(telegram_transport.valid_chat_id(chat_id), chat_id)

    def test_chat_not_found_explains_the_start_step(self):
        with mock.patch(URLOPEN, side_effect=_http_error(400, "Bad Request: chat not found")):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertEqual(outcome.kind, ntfy_transport.HTTP_ERROR)
        self.assertEqual(outcome.http_status, 400)
        self.assertIn("/start", outcome.detail)
        self.assertNotIn(TOKEN, outcome.detail)

    def test_rejected_token_is_reported_without_echoing_it(self):
        with mock.patch(URLOPEN, side_effect=_http_error(401, "Unauthorized")):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertEqual(outcome.http_status, 401)
        self.assertIn("token", outcome.detail)
        self.assertNotIn(TOKEN, outcome.detail)

    def test_blocked_bot_is_reported(self):
        with mock.patch(URLOPEN, side_effect=_http_error(403, "Forbidden: bot was blocked by the user")):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertFalse(outcome.ok)
        self.assertIn("Start", outcome.detail)

    def test_ok_false_body_is_not_a_success(self):
        body = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        with mock.patch(URLOPEN, return_value=_Response(200, body)):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertFalse(outcome.ok)

    def test_network_errors_never_expose_the_token(self):
        error = urllib.error.URLError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage")
        with mock.patch(URLOPEN, side_effect=error):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertEqual(outcome.kind, ntfy_transport.NETWORK_ERROR)
        self.assertNotIn(TOKEN, outcome.detail)
        self.assertNotIn("api.telegram.org", outcome.detail)

    def test_tls_failures_are_classified(self):
        with mock.patch(URLOPEN, side_effect=urllib.error.URLError(ssl.SSLCertVerificationError("bad cert"))):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertEqual(outcome.kind, ntfy_transport.TLS_ERROR)
        with mock.patch(URLOPEN, side_effect=tls_transport.TLSConfigurationError("bad bundle")):
            outcome = telegram_transport.push_telegram(TOKEN, CHAT, "Title", "Body")
        self.assertEqual(outcome.kind, ntfy_transport.TLS_CONFIGURATION_ERROR)

    def test_push_from_config_requires_the_toggle(self):
        with mock.patch(URLOPEN) as urlopen:
            outcome = telegram_transport.push_from_config(
                _enabled_config(telegram_enabled=False), "Title", "Body"
            )
        urlopen.assert_not_called()
        self.assertEqual(outcome.kind, ntfy_transport.NOT_CONFIGURED)

        with mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            self.assertTrue(telegram_transport.push_from_config(_enabled_config(), "Title", "Body").ok)
        urlopen.assert_called_once()

    def test_find_chat_ids_lists_recent_chats_newest_first(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 111, "type": "private", "first_name": "Ann"}}},
            {"update_id": 2, "message": {"chat": {"id": -100222, "type": "group", "title": "Family"}}},
            {"update_id": 3, "message": {"chat": {"id": 111, "type": "private", "first_name": "Ann"}}},
            {"update_id": 4, "edited_message": "garbage"},
        ]
        with mock.patch(URLOPEN, return_value=_Response(200, {"ok": True, "result": updates})) as urlopen:
            outcome, chats = telegram_transport.find_chat_ids(TOKEN)
        self.assertTrue(outcome.ok)
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith("/getUpdates"))
        self.assertEqual([c["id"] for c in chats], ["111", "-100222"])
        self.assertEqual(chats[1]["name"], "Family")

    def test_find_chat_ids_reports_a_webhook_conflict(self):
        with mock.patch(URLOPEN, side_effect=_http_error(409, "Conflict: can't use getUpdates method while webhook is active")):
            outcome, chats = telegram_transport.find_chat_ids(TOKEN)
        self.assertEqual(chats, [])
        self.assertIn("webhook", outcome.detail)


class TelegramConfigTests(unittest.TestCase):
    def payload(self, **overrides):
        payload = {
            "profile_number": "123", "ntfy_topic": "topic", "organization_ids": [1],
            "exam_types": ["Theoretical"], "category": 5,
            "current_slot_date": "2026-09-14",
        }
        payload.update(overrides)
        return payload

    def test_telegram_is_off_by_default(self):
        config = app.build_config(self.payload())
        self.assertFalse(config["telegram_enabled"])
        self.assertEqual(config["telegram_bot_token"], "")
        self.assertEqual(config["telegram_chat_id"], "")

    def test_enabled_telegram_settings_are_saved(self):
        config = app.build_config(self.payload(
            telegram_enabled=True, telegram_bot_token=f"  {TOKEN} ", telegram_chat_id=-1001234,
        ))
        self.assertTrue(config["telegram_enabled"])
        self.assertEqual(config["telegram_bot_token"], TOKEN)
        self.assertEqual(config["telegram_chat_id"], "-1001234")
        self.assertEqual(telegram_transport.config_channel(config), (TOKEN, "-1001234"))

    def test_enabled_telegram_requires_token_and_chat(self):
        with self.assertRaises(ValueError):
            app.build_config(self.payload(telegram_enabled=True, telegram_chat_id=CHAT))
        with self.assertRaises(ValueError):
            app.build_config(self.payload(telegram_enabled=True, telegram_bot_token=TOKEN))

    def test_malformed_values_are_rejected_even_when_disabled(self):
        with self.assertRaises(ValueError):
            app.build_config(self.payload(telegram_bot_token="123:abc/../../x"))
        with self.assertRaises(ValueError):
            app.build_config(self.payload(telegram_chat_id="<script>"))

    def test_disabled_settings_are_kept_for_later(self):
        config = app.build_config(self.payload(
            telegram_enabled=False, telegram_bot_token=TOKEN, telegram_chat_id=CHAT,
        ))
        self.assertFalse(config["telegram_enabled"])
        self.assertEqual(config["telegram_bot_token"], TOKEN)
        self.assertIsNone(telegram_transport.config_channel(config))

    def test_settings_page_prefills_telegram_fields(self):
        page = app.render_wizard(_enabled_config()).decode()
        self.assertIn('id="telegram-enabled"', page)
        self.assertIn("EXISTING_CONFIG.telegram_bot_token", page)
        self.assertIn("telegram_enabled: switchOn('telegram-enabled')", page)


class TelegramFanOutTests(unittest.TestCase):
    def test_notifier_wrapper_is_silent_when_not_configured(self):
        logger = mock.Mock()
        with mock.patch(URLOPEN) as urlopen:
            outcome = notifier.push_telegram(logger, {"ntfy_topic": "topic"}, "Title", "Body")
        urlopen.assert_not_called()
        logger.info.assert_not_called()
        self.assertEqual(outcome.kind, ntfy_transport.NOT_CONFIGURED)

    def test_notifier_wrapper_logs_failures_without_the_token(self):
        with self.assertLogs("telegram-test", level=logging.INFO) as logs, \
                mock.patch(URLOPEN, side_effect=_http_error(401, "Unauthorized")):
            notifier.push_telegram(logging.getLogger("telegram-test"), _enabled_config(), "Title", "Body")
        self.assertIn("telegram_failed", "\n".join(logs.output))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def _session_config(self, **overrides):
        config_file = mock.Mock()
        config_file.read_text.return_value = json.dumps(_enabled_config(**overrides))
        return mock.patch("info_kierowca_notifier.auth.session.CONFIG_FILE", config_file)

    def test_relogin_alert_is_mirrored_to_telegram(self):
        with self._session_config(), \
                mock.patch("info_kierowca_notifier.auth.session.ntfy_transport.push_ntfy", return_value=SUCCESS) as ntfy, \
                mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            self.assertIs(auto_refresh_session.push_ntfy("Title", "Body"), SUCCESS)
        ntfy.assert_called_once()
        urlopen.assert_called_once()

    def test_relogin_alert_respects_the_relogin_toggle(self):
        with self._session_config(phone_alerts_relogin=False), \
                mock.patch("info_kierowca_notifier.auth.session.ntfy_transport.push_ntfy") as ntfy, \
                mock.patch(URLOPEN) as urlopen:
            outcome = auto_refresh_session.push_ntfy("Title", "Body")
        ntfy.assert_not_called()
        urlopen.assert_not_called()
        self.assertEqual(outcome.kind, ntfy_transport.NOT_CONFIGURED)

    def test_reschedule_alert_is_mirrored_to_telegram(self):
        with mock.patch.object(open_logged_in_browser, "read_config", return_value=_enabled_config()), \
                mock.patch("info_kierowca_notifier.booking.reschedule.ntfy_transport.push_ntfy", return_value=SUCCESS), \
                mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            self.assertIs(open_logged_in_browser.push_ntfy("topic", "Title", "Body"), SUCCESS)
        urlopen.assert_called_once()

    def test_reschedule_telegram_failure_does_not_change_the_ntfy_outcome(self):
        with mock.patch.object(open_logged_in_browser, "read_config", return_value=_enabled_config()), \
                mock.patch("info_kierowca_notifier.booking.reschedule.ntfy_transport.push_ntfy", return_value=SUCCESS), \
                mock.patch(URLOPEN, side_effect=urllib.error.URLError("down")):
            self.assertIs(open_logged_in_browser.push_ntfy("topic", "Title", "Body"), SUCCESS)


class TelegramApiTests(unittest.TestCase):
    def _handler(self, payload, saved=None):
        handler = object.__new__(app.AppHandler)
        handler._read_json_body = mock.Mock(return_value=payload)
        handler._send_json = mock.Mock()
        handler._load_config_or_empty = mock.Mock(return_value=saved or {})
        return handler

    def test_test_message_reports_success_only_after_delivery(self):
        handler = self._handler({"token": TOKEN, "chat_id": CHAT})
        with mock.patch(URLOPEN, return_value=_Response()):
            handler._handle_test_telegram()
        handler._send_json.assert_called_once_with(200, {"ok": True})

    def test_test_message_falls_back_to_the_saved_token(self):
        handler = self._handler({"token": "", "chat_id": CHAT}, saved=_enabled_config())
        with mock.patch(URLOPEN, return_value=_Response()) as urlopen:
            handler._handle_test_telegram()
        self.assertIn(TOKEN, urlopen.call_args.args[0].full_url)
        handler._send_json.assert_called_once_with(200, {"ok": True})

    def test_test_message_refuses_malformed_input(self):
        for payload in ({"token": "../x", "chat_id": CHAT}, {"token": TOKEN, "chat_id": "a b"},
                        {"token": TOKEN, "chat_id": ""}):
            handler = self._handler(payload)
            with mock.patch(URLOPEN) as urlopen:
                handler._handle_test_telegram()
            urlopen.assert_not_called()
            self.assertEqual(handler._send_json.call_args.args[0], 400, payload)

    def test_test_message_propagates_an_actionable_failure(self):
        handler = self._handler({"token": TOKEN, "chat_id": CHAT})
        with mock.patch(URLOPEN, side_effect=_http_error(400, "Bad Request: chat not found")):
            handler._handle_test_telegram()
        code, body = handler._send_json.call_args.args
        self.assertEqual(code, 502)
        self.assertFalse(body["ok"])
        self.assertIn("/start", body["error"])
        self.assertNotIn(TOKEN, json.dumps(body))

    def test_chat_id_lookup_returns_found_chats(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 111, "type": "private", "first_name": "Ann"}}}]
        handler = self._handler({"token": TOKEN})
        with mock.patch(URLOPEN, return_value=_Response(200, {"ok": True, "result": updates})):
            handler._handle_telegram_chat_id()
        handler._send_json.assert_called_once_with(
            200, {"ok": True, "chats": [{"id": "111", "type": "private", "name": "Ann"}]}
        )

    def test_chat_id_lookup_without_messages_explains_what_to_do(self):
        handler = self._handler({"token": TOKEN})
        with mock.patch(URLOPEN, return_value=_Response(200, {"ok": True, "result": []})):
            handler._handle_telegram_chat_id()
        code, body = handler._send_json.call_args.args
        self.assertEqual(code, 404)
        self.assertIn("Start", body["error"])


if __name__ == "__main__":
    unittest.main()


class SlotAlertTelegramTests(unittest.TestCase):
    def test_slot_alert_is_mirrored_to_telegram_with_a_link(self):
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
            auto_open_browser=False,
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
                    mock.patch.object(booking_launch, "trigger_open_browser"), \
                    mock.patch.object(notifier, "push_ntfy") as ntfy, \
                    mock.patch.object(notifier, "push_telegram") as telegram:
                notifier.run_check(logging.getLogger("test"), {"paused": False})

        ntfy.assert_called_once()
        telegram.assert_called_once()
        _logger, sent_config, title, body = telegram.call_args.args
        self.assertEqual(sent_config["telegram_chat_id"], CHAT)
        self.assertEqual(title, "Slot within range!")
        self.assertIn(notifier.TELEGRAM_BOOKING_URL, body)
        self.assertEqual(telegram.call_args.kwargs["priority"], "urgent")
