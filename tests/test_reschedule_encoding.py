import io
import json
import unittest
from unittest import mock

from info_kierowca_notifier.booking import reschedule


class RescheduleEncodingTests(unittest.TestCase):
    def test_cp1251_output_is_reconfigured_to_utf8(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1251")
        try:
            with mock.patch.object(reschedule.sys, "stdout", stream), \
                    mock.patch.object(reschedule.sys, "stderr", io.StringIO()):
                reschedule.configure_utf8_output()
                print("Skipping the Zmie\u0144 termin auto-click-through.")
            self.assertEqual(stream.encoding, "utf-8")
            self.assertIn("Zmie\u0144", raw.getvalue().decode("utf-8"))
        finally:
            stream.close()

    def test_missing_streams_are_supported(self):
        with mock.patch.object(reschedule.sys, "stdout", None), \
                mock.patch.object(reschedule.sys, "stderr", None):
            reschedule.configure_utf8_output()

    def test_manual_open_browser_completes_with_cp1251_stdout(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1251")
        session_file = mock.Mock()
        session_file.exists.return_value = True
        session_file.read_text.return_value = json.dumps({
            "cookies": {name: "fake" for name in reschedule.cdp_client.COOKIE_NAMES}
        })
        patches = {
            "SESSION_FILE": session_file,
            "wait_for_debug_port": mock.Mock(),
            "set_cookies": mock.Mock(),
            "create_page_target": mock.Mock(return_value=mock.Mock()),
            "navigate_target": mock.Mock(),
            "bring_target_to_front": mock.Mock(),
        }
        try:
            with mock.patch.object(reschedule.sys, "stdout", stream), \
                    mock.patch.object(reschedule.sys, "stderr", io.StringIO()), \
                    mock.patch.object(reschedule.sys, "argv", ["helper", "--no-auto-click"]), \
                    mock.patch.object(reschedule, "find_chrome", return_value="chrome.exe"), \
                    mock.patch.object(reschedule, "ensure_private_profile_dir"), \
                    mock.patch.object(reschedule.subprocess, "Popen"), \
                    mock.patch.multiple(reschedule.cdp_client, **patches):
                reschedule.main()
            output = raw.getvalue().decode("utf-8")
            self.assertIn("Skipping the Zmie\u0144 termin", output)
            self.assertIn("Close the window whenever you're done", output)
            patches["navigate_target"].assert_called_once()
        finally:
            stream.close()


if __name__ == "__main__":
    unittest.main()
