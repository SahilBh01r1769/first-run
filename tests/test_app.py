import json
import os
from pathlib import Path
import shutil
import socket
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from first_run.agent import Decision
from first_run.app import KeyCheckWorker, MainWindow


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_example_selection_clears_clone_destination(self):
        window = MainWindow()
        try:
            window.destination.setText("unused clone folder")
            window.choose_sample()
            self.assertTrue(window.sample_path.is_dir())
            self.assertEqual(window.source.text(), str(window.sample_path))
            self.assertEqual(window.destination.text(), "")
        finally:
            window.process_watch.stop()
            window.close()

    def test_recovery_example_selection_needs_no_node(self):
        window = MainWindow()
        try:
            window.recent.addItem("previous recovery run", "previous recovery run")
            window.recent.setCurrentIndex(window.recent.count() - 1)
            window.start.setText("Continue setup")
            window.choose_recovery_sample()
            self.assertEqual(window.source.text(), str(window.recovery_sample_path))
            self.assertEqual(window.destination.text(), "")
            self.assertEqual(window.recent.currentIndex(), 0)
            self.assertFalse(window.again.isEnabled())
            self.assertEqual(window.start.text(), "Set up and run")
        finally:
            window.process_watch.stop()
            window.close()

    def test_continue_button_reuses_completed_setup(self):
        window = MainWindow()
        try:
            window.start.setText("Continue setup")
            with patch.object(window, "open_project") as open_project:
                window.start.click()
            open_project.assert_called_once_with(reuse=True)
        finally:
            window.process_watch.stop()
            window.close()

    def test_recovery_key_is_masked_and_not_saved_in_window_history(self):
        window = MainWindow()
        try:
            self.assertEqual(window.api_key.echoMode(), QLineEdit.EchoMode.Password)
            window.api_key.setText("test-key")
            self.assertNotIn("test-key", window.output.toPlainText())
        finally:
            window.process_watch.stop()
            window.close()

    def test_key_check_reports_model_response_and_auth_fallback(self):
        worker = KeyCheckWorker("private-test-key")
        results = []
        worker.finished.connect(lambda success, message: results.append((success, message)))
        with patch("first_run.app.decide_failure", return_value=Decision(
            "blocked", "Sample blocker", source="AI",
        )) as decide:
            worker.run()
        self.assertEqual(results[-1][0], True)
        self.assertEqual(decide.call_args.kwargs["api_key"], "private-test-key")
        with patch("first_run.app.decide_failure", return_value=Decision(
            "blocked", "OpenAI request failed (HTTP 401)", source="fallback",
        )):
            worker.run()
        self.assertEqual(results[-1][0], False)
        self.assertIn("HTTP 401", results[-1][1])
        self.assertNotIn("private-test-key", str(results))

    def test_key_check_status_remains_visible_after_output_is_cleared(self):
        window = MainWindow()
        try:
            window.key_check_finished(True, "AI connection works.")
            window.output.clear()
            self.assertEqual(window.key_status.text(), "Connected")
            window.api_key.setText("another-key")
            self.assertEqual(window.key_status.text(), "Not checked")
        finally:
            window.process_watch.stop()
            window.close()

    def test_recovery_reason_is_visible_outside_raw_output(self):
        window = MainWindow()
        try:
            window.reporter.report("Observed: port 8000 is in use before launch.")
            window.reporter.report("Recovery (rules): trying port 8001.")
            self.assertIn("port 8000", window.observation.text())
            self.assertEqual(window.decision_source.text(), "rules")
            self.assertIn("8001", window.recovery.text())
        finally:
            window.process_watch.stop()
            window.close()

    def test_recovery_trace_keeps_inspection_and_second_decision_in_order(self):
        window = MainWindow()
        try:
            window.reporter.report("Observed: Flask could not import app.py")
            window.reporter.report("Recovery (AI): Check entry points")
            window.reporter.report("Observed: main.py has a module-level app assignment")
            window.reporter.report("Recovery (AI): Try main.py")
            trace = window.recovery_trace.toPlainText().splitlines()
            self.assertEqual(len(trace), 4)
            self.assertTrue(trace[0].startswith("Observed · Flask"))
            self.assertEqual(trace[1], "AI action · Check entry points")
            self.assertTrue(trace[2].startswith("Observed · main.py"))
            self.assertEqual(trace[3], "AI action · Try main.py")
            self.assertIn("main.py", window.observation.text())
        finally:
            window.process_watch.stop()
            window.close()

    def test_running_status_clears_when_process_exits(self):
        window = MainWindow()
        window.address = "http://127.0.0.1:3000/"
        window.state.setText("Running")
        window.open_button.setEnabled(True)
        process = Mock()
        process.poll.return_value = 1
        runner = SimpleNamespace(application=process, stop=Mock())
        window.process_runner = runner

        window.check_process()

        self.assertEqual(window.state.text(), "Blocked")
        self.assertIsNone(window.address)
        self.assertFalse(window.open_button.isEnabled())
        runner.stop.assert_called_once()
        window.process_watch.stop()
        window.close()

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "Node and npm required")
    def test_output_after_setup_worker_exits_reaches_window(self):
        port = None
        for candidate in (3000, 5173, 8080, 4173):
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", candidate))
                port = candidate
                break
            except OSError:
                continue
        if port is None:
            self.skipTest("All supported Node ports are occupied")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "late-output-test", "version": "1.0.0",
                "scripts": {"start": "node server.js"},
            }))
            (path / "server.js").write_text(
                "const fs = require('fs'); const http = require('http');\n"
                f"http.createServer((req, res) => res.end('ok')).listen({port}, '127.0.0.1');\n"
                "const timer = setInterval(() => { if (fs.existsSync('release-output')) {\n"
                "  console.log('late application output'); clearInterval(timer);\n"
                "}}, 50);\n"
            )
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                window = MainWindow()
                try:
                    window.source.setText(str(path))
                    window.start.click()

                    def wait_for(predicate, timeout=20):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            self.app.processEvents()
                            if predicate():
                                return True
                            time.sleep(.05)
                        return False

                    self.assertTrue(wait_for(lambda: window.state.text() == "Running" and window.thread is None),
                                    window.current.text())
                    (path / "release-output").touch()
                    self.assertTrue(wait_for(lambda: "late application output" in window.output.toPlainText()),
                                    window.output.toPlainText())
                finally:
                    window.stop_run()
                    window.process_watch.stop()
                    window.close()


if __name__ == "__main__":
    unittest.main()
