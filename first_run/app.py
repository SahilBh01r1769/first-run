"""Small task-oriented desktop shell for a First Run session."""

import os
import sys
import threading
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from first_run.source import prepare_source
from first_run.inspect import ProjectInfo, inspect_project
from first_run.runner import Outcome, SetupRunner
from first_run.history import recent
from first_run.agent import decide_failure


class SourceWorker(QObject):
    finished = Signal(object, object)
    failed = Signal(str)
    inspected = Signal(object)

    def __init__(self, source: str, destination: str, report, reuse: bool = False,
                 api_key: str | None = None):
        super().__init__()
        self.source = source
        self.destination = destination
        self.report = report
        self.reuse = reuse
        self.api_key = api_key
        self.cancelled = threading.Event()
        self.runner = None

    def run(self):
        try:
            info = inspect_project(prepare_source(self.source, self.destination, self.cancelled))
            self.inspected.emit(info)
            self.runner = SetupRunner(info, self.report, self.cancelled, api_key=self.api_key)
            self.finished.emit(self.runner.run(reuse=self.reuse), self.runner)
        except (OSError, ValueError, RuntimeError) as exc:
            self.failed.emit(str(exc))


class RunReporter(QObject):
    output = Signal(str)
    step = Signal(str)
    observed = Signal(str)
    decision = Signal(str, str)

    def report(self, line: str):
        if line.startswith("$ "):
            self.step.emit("Running " + line[2:])
        elif line.startswith("Observed: "):
            self.observed.emit(line.removeprefix("Observed: "))
        elif line.startswith("Recovery (") and "): " in line:
            source, reason = line.removeprefix("Recovery (").split("): ", 1)
            self.decision.emit(source, reason)
        self.output.emit(line)


class KeyCheckWorker(QObject):
    finished = Signal(bool, str)

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    def run(self):
        try:
            decision = decide_failure(
                "Connection check: a sample setup has no launch route.", [], set(),
                ("No project files or logs are included in this check.",), api_key=self.key,
            )
        except Exception as exc:
            self.finished.emit(False, f"AI connection check failed ({type(exc).__name__}).")
            return
        if decision.source == "AI":
            self.finished.emit(True, "AI connection works. Recovery decisions can use this key.")
        else:
            self.finished.emit(False, "AI connection failed: " + decision.reason)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("First Run")
        self.resize(760, 560)
        self.thread = None
        self.worker = None
        self.key_thread = None
        self.key_worker = None
        self.process_runner = None
        self.address = None
        self.project_path = None
        self.reporter = RunReporter(self)
        self.process_watch = QTimer(self)
        self.process_watch.setInterval(500)
        self.process_watch.timeout.connect(self.check_process)
        self.process_watch.start()

        self.source = QLineEdit()
        self.source.setPlaceholderText("Local project folder or public HTTPS Git URL")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_source)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source)
        source_row.addWidget(browse)
        self.sample_path = Path(__file__).resolve().parent.parent / "examples" / "flask_hello"
        self.recovery_sample_path = Path(__file__).resolve().parent.parent / "examples" / "flask_recovery"
        if self.sample_path.is_dir():
            sample_button = QPushButton("Sample app")
            sample_button.clicked.connect(self.choose_sample)
            source_row.addWidget(sample_button)
        if self.recovery_sample_path.is_dir():
            recovery_button = QPushButton("Recovery sample")
            recovery_button.clicked.connect(self.choose_recovery_sample)
            source_row.addWidget(recovery_button)

        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Folder to create when cloning")
        destination_button = QPushButton("Choose…")
        destination_button.clicked.connect(self.choose_destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination)
        destination_row.addWidget(destination_button)

        form = QFormLayout()
        form.addRow("Project", source_row)
        form.addRow("Clone to", destination_row)
        self.recent = QComboBox()
        self.recent.addItem("Previously configured projects", "")
        for item in recent():
            self.recent.addItem(item["path"], item["path"])
        self.recent.currentIndexChanged.connect(self.choose_recent)
        form.addRow("Recent", self.recent)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Optional; used only to diagnose a failed setup")
        self.key_status = QLabel("Not checked")
        self.api_key.textChanged.connect(lambda _: self.key_status.setText("Not checked"))
        self.check_key_button = QPushButton("Test key")
        self.check_key_button.clicked.connect(self.test_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key)
        key_row.addWidget(self.check_key_button)
        form.addRow("OpenAI key", key_row)
        form.addRow("AI connection", self.key_status)

        self.start = QPushButton("Set up and run")
        self.start.clicked.connect(lambda: self.open_project(reuse=self.start.text() == "Continue setup"))
        self.again = QPushButton("Start again")
        self.again.setEnabled(False)
        self.again.clicked.connect(lambda: self.open_project(reuse=True))
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        self.open_button = QPushButton("Open app")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(lambda: webbrowser.open(self.address) if self.address else None)
        self.folder_button = QPushButton("Open project folder")
        self.folder_button.setEnabled(False)
        self.folder_button.clicked.connect(self.open_folder)
        actions = QHBoxLayout()
        actions.addWidget(self.start)
        actions.addWidget(self.again)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.folder_button)
        self.state = QLabel("Ready")
        self.state.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.current = QLabel("Choose a local folder or repository URL.")
        self.plan = QLabel("No plan yet")
        self.plan.setWordWrap(True)
        self.observation = QLabel("No failure observed.")
        self.observation.setWordWrap(True)
        self.recovery = QLabel("No recovery decision yet.")
        self.recovery.setWordWrap(True)
        self.decision_source = QLabel("—")
        self.recovery_steps: list[str] = []
        self.recovery_trace = QTextEdit()
        self.recovery_trace.setReadOnly(True)
        self.recovery_trace.setMaximumHeight(95)
        self.recovery_trace.setPlaceholderText("Recovery steps appear here when setup needs them.")
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.reporter.step.connect(self.current.setText)
        self.reporter.observed.connect(self.show_observation)
        self.reporter.decision.connect(self.show_decision)
        self.reporter.output.connect(self.output.append)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(QLabel("Current run"))
        layout.addWidget(self.state)
        layout.addWidget(self.current)
        layout.addWidget(QLabel("Setup plan"))
        layout.addWidget(self.plan)
        recovery = QFormLayout()
        recovery.addRow("Observed", self.observation)
        recovery.addRow("Decision", self.recovery)
        recovery.addRow("Source", self.decision_source)
        layout.addLayout(recovery)
        layout.addWidget(QLabel("Recovery steps"))
        layout.addWidget(self.recovery_trace)
        layout.addWidget(QLabel("Output"))
        layout.addWidget(self.output, 1)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)

    def _record_recovery(self, label: str, detail: str):
        self.recovery_steps.append(f"{label} · {detail[:180]}")
        self.recovery_trace.setPlainText("\n".join(self.recovery_steps[-6:]))
        scrollbar = self.recovery_trace.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def show_observation(self, detail: str):
        self.observation.setText(detail)
        self._record_recovery("Observed", detail)

    def show_decision(self, source: str, reason: str):
        self.decision_source.setText(source)
        self.recovery.setText(reason)
        if source == "AI":
            self.key_status.setText("Connected")
        self._record_recovery(f"{source.upper() if source == 'AI' else source.capitalize()} action", reason)

    def browse_source(self):
        path = QFileDialog.getExistingDirectory(self, "Select project folder")
        if path:
            self.source.setText(path)

    def choose_sample(self):
        self.source.setText(str(self.sample_path))
        self.destination.clear()
        self.recent.setCurrentIndex(0)
        self.start.setText("Set up and run")

    def choose_recovery_sample(self):
        self.source.setText(str(self.recovery_sample_path))
        self.destination.clear()
        self.recent.setCurrentIndex(0)
        self.start.setText("Set up and run")

    def choose_recent(self):
        path = self.recent.currentData()
        self.again.setEnabled(bool(path))
        if path:
            self.source.setText(path)
            self.destination.clear()

    def choose_destination(self):
        parent = QFileDialog.getExistingDirectory(self, "Select parent folder")
        if parent:
            name = Path(self.source.text().rstrip("/")).name.removesuffix(".git") or "project"
            self.destination.setText(str(Path(parent) / name))

    def open_folder(self):
        if self.project_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project_path)))

    def test_key(self):
        key = self.api_key.text().strip() or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            self.output.append("Enter an OpenAI API key to test the connection.")
            return
        self.check_key_button.setEnabled(False)
        self.api_key.setEnabled(False)
        self.output.append("Checking AI connection; no project files or logs are sent…")
        self.key_thread = QThread(self)
        self.key_worker = KeyCheckWorker(key)
        self.key_worker.moveToThread(self.key_thread)
        self.key_thread.started.connect(self.key_worker.run)
        self.key_worker.finished.connect(self.key_check_finished)
        self.key_worker.finished.connect(self.key_thread.quit)
        self.key_thread.finished.connect(self.key_worker.deleteLater)
        self.key_thread.finished.connect(self.key_thread.deleteLater)
        self.key_thread.finished.connect(self.key_thread_finished)
        self.key_thread.start()

    def key_check_finished(self, success: bool, message: str):
        self.output.append(message)
        self.key_status.setText("Connected" if success else "Check failed; see Output")

    def key_thread_finished(self):
        self.check_key_button.setEnabled(True)
        self.api_key.setEnabled(True)
        self.key_thread = None
        self.key_worker = None

    def open_project(self, reuse: bool = False):
        if self.process_runner:
            self.process_runner.stop()
            self.process_runner = None
        self.start.setEnabled(False)
        self.again.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.open_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.address = None
        self.project_path = None
        self.start.setText("Set up and run")
        self.state.setText("Opening")
        self.current.setText("Resolving project source…")
        self.plan.setText("Inspecting project…")
        self.observation.setText("No failure observed.")
        self.recovery.setText("No recovery decision yet.")
        self.decision_source.setText("—")
        self.recovery_steps.clear()
        self.recovery_trace.clear()
        self.output.clear()
        self.thread = QThread(self)
        self.worker = SourceWorker(self.source.text(), self.destination.text(), self.reporter.report, reuse,
                                   api_key=self.api_key.text().strip() or None)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.inspected.connect(self.source_ready)
        self.worker.finished.connect(self.run_finished)
        self.worker.failed.connect(self.source_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.thread_finished)
        self.thread.start()

    def thread_finished(self):
        self.start.setEnabled(True)
        self.again.setEnabled(bool(self.recent.currentData()))
        self.stop_button.setEnabled(self.address is not None)
        self.thread = None
        self.worker = None

    def stop_run(self):
        was_running = self.address is not None
        if self.thread and self.thread.isRunning() and self.worker:
            self.state.setText("Stopping")
            self.worker.cancelled.set()
            if self.worker.runner:
                self.worker.runner.stop()
        elif self.process_runner:
            self.process_runner.stop()
        self.stop_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.address = None
        if was_running:
            self.state.setText("Stopped")
            self.current.setText("Application stopped. Use Start again to relaunch it.")

    def check_process(self):
        if not self.address or not self.process_runner or not self.process_runner.application:
            return
        code = self.process_runner.application.poll()
        if code is None:
            return
        self.process_runner.stop()
        self.address = None
        self.state.setText("Blocked")
        self.current.setText(f"Application exited after responding (exit code {code}). See output for details.")
        self.open_button.setEnabled(False)
        self.stop_button.setEnabled(False)

    def closeEvent(self, event):
        if self.key_thread and self.key_thread.isRunning():
            self.output.append("Please wait for the AI key check to finish before closing.")
            event.ignore()
            return
        self.stop_run()
        if self.thread and self.thread.isRunning():
            if not self.thread.wait(4000):
                event.ignore()
                return
        super().closeEvent(event)

    def source_ready(self, info: ProjectInfo):
        self.project_path = info.path
        self.folder_button.setEnabled(True)
        self.state.setText("Setting up" if info.launch else "Blocked")
        self.current.setText(f"{info.framework} · {info.path}")
        if info.launch:
            steps = ["Check .env values"] if info.needs_env else []
            if info.kind == "python":
                steps.append("Create or reuse .venv")
            steps.extend(["Install dependencies", "Launch application", "Verify HTTP response"])
            self.plan.setText(" → ".join(steps))
            self.output.append(f"Install: {' '.join(info.install)}\nLaunch: {' '.join(info.launch)}")
        else:
            self.plan.setText("No safe setup route determined.")
        self.output.append("\n".join(info.observations))

    def run_finished(self, outcome: Outcome, runner: SetupRunner):
        self.process_runner = runner
        if self.recovery_steps:
            self._record_recovery(outcome.state, outcome.detail)
        stopped = runner.cancelled.is_set() and outcome.state != "Running"
        self.state.setText("Stopped" if stopped else outcome.state)
        self.current.setText(outcome.detail)
        if outcome.state == "Needs input" and self.observation.text() == "No failure observed.":
            self.observation.setText(outcome.detail)
            self.recovery.setText("Continue after addressing the requested input.")
            self.decision_source.setText("rules")
        self.start.setText("Continue setup" if outcome.state == "Needs input" else "Set up and run")
        self.current.setWordWrap(True)
        self.address = outcome.address
        if outcome.address and not any(self.recent.itemData(i) == str(runner.info.path) for i in range(self.recent.count())):
            self.recent.addItem(str(runner.info.path), str(runner.info.path))
        if outcome.address:
            self.recent.setCurrentIndex(self.recent.findData(str(runner.info.path)))
        self.open_button.setEnabled(bool(outcome.address))
        self.stop_button.setEnabled(bool(outcome.address))

    def source_failed(self, message: str):
        self.state.setText("Stopped" if self.worker and self.worker.cancelled.is_set() else "Blocked")
        self.current.setText(message)
        self.output.append(message)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
