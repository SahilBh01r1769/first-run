import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from io import BytesIO, StringIO
from urllib.error import HTTPError

from first_run.agent import Decision, decide_failure, failure_summary, observe_failure
from first_run.history import recent, saved_launch
from first_run.inspect import inspect_project
from first_run.runner import Outcome, SetupRunner


class RunnerTests(unittest.TestCase):
    def test_failure_categories_do_not_guess_at_unknown_output(self):
        self.assertEqual(observe_failure("npm ERR! ETIMEDOUT", "install").category,
                         "installation network error")
        self.assertEqual(observe_failure("NameError: missing_name").category, "source error")
        self.assertEqual(observe_failure("something unusual happened").category, "unclear failure")

    def test_flask_missing_app_is_named_as_entry_point_issue(self):
        error = "Error: Failed to find Flask application or factory in module 'app'. Use 'app:name' to specify one."
        observed = observe_failure(error)
        self.assertEqual(observed.category, "entry point issue")
        self.assertIn("Failed to find Flask application", observed.detail)

    def test_http_404_is_not_a_verified_running_app(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "not-found-test", "version": "1.0.0", "scripts": {"start": "node server.js"},
            }))
            (path / "server.js").write_text(
                "require('http').createServer((q,r)=>{r.statusCode=404;r.end('Not Found')})"
                ".listen(3000,'127.0.0.1')"
            )
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch("first_run.runner.STARTUP_TIMEOUT", 3):
                    runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
                    try:
                        result = runner.run()
                    finally:
                        runner.stop()
                self.assertEqual(result.state, "Blocked")
                self.assertIn("HTTP 404", result.detail)
                self.assertIsNone(saved_launch(path))

    def test_existing_local_server_is_not_claimed_as_new_app(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"unrelated")

            def log_message(self, *_):
                pass

        server = None
        for port in (3000, 8080, 4173):
            try:
                server = HTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        if server is None:
            self.skipTest("All test ports are occupied")
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with TemporaryDirectory() as directory:
                path = Path(directory)
                (path / "package.json").write_text(json.dumps({
                    "name": "occupied-port-test", "version": "1.0.0",
                    "scripts": {"start": "node server.js"},
                }))
                (path / "server.js").write_text(
                    f"console.log('http://localhost:{port}'); setInterval(()=>{{}}, 1000)"
                )
                started = threading.Event()
                runner = SetupRunner(
                    inspect_project(path),
                    lambda line: started.set() if line == "$ npm run start" else None,
                    threading.Event(),
                )
                result = []
                worker = threading.Thread(target=lambda: result.append(runner.run()))
                worker.start()
                self.assertTrue(started.wait(10))
                time.sleep(1)
                self.assertFalse(result, "First Run claimed the unrelated server")
                runner.stop()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(result[0].state, "Blocked")
        finally:
            server.shutdown()
            server.server_close()

    def test_occupied_flask_port_uses_free_alternative_and_verifies_it(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "flask_hello"
        runner = SetupRunner(inspect_project(example), lambda _: None, threading.Event())
        app = Mock()
        app.poll.return_value = None
        app.stdout = StringIO()
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        with patch.object(runner, "_port_in_use", side_effect=lambda port: port == 5000):
            with patch("first_run.runner.subprocess.Popen", return_value=app) as start:
                with patch("first_run.runner.urlopen", return_value=response) as probe:
                    outcome = runner._launch_verify(("python", "-m", "flask", "--app", "app.py", "run",
                                                      "--host", "127.0.0.1"))
        self.assertEqual(outcome.state, "Running")
        self.assertIn("--port", start.call_args.args[0])
        self.assertIn("5001", start.call_args.args[0])
        self.assertEqual(probe.call_args.args[0], "http://127.0.0.1:5001/")
        self.assertEqual(runner.verified_launch, start.call_args.args[0])

    def test_fastapi_verification_checks_docs_after_missing_root(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "requirements.txt").write_text("fastapi\nuvicorn\n")
            (path / "main.py").write_text("app = None\n")
            runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
            app = Mock()
            app.poll.return_value = None
            app.stdout = StringIO()
            response = MagicMock(status=200)
            response.__enter__.return_value = response
            def probe(url, **_):
                if url.endswith("/"):
                    raise HTTPError(url, 404, "Not Found", {}, None)
                return response
            with patch.object(runner, "_port_in_use", return_value=False):
                with patch("first_run.runner.subprocess.Popen", return_value=app):
                    with patch("first_run.runner.urlopen", side_effect=probe):
                        outcome = runner._launch_verify(("python", "-m", "uvicorn", "main:app"))
            self.assertEqual(outcome.state, "Running")
            self.assertEqual(outcome.address, "http://127.0.0.1:8000/docs")

    def test_missing_env_value_needs_input_before_install(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
            (path / ".env.example").write_text("SERVICE_API_KEY=\n")
            outcome = SetupRunner(inspect_project(path), lambda _: None, threading.Event()).run()
            self.assertEqual(outcome.state, "Needs input")
            self.assertIn("SERVICE_API_KEY", outcome.detail)
            self.assertIn(str(path / ".env"), outcome.detail)
            self.assertFalse((path / "node_modules").exists())

    def test_cancelled_before_setup_does_not_create_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "requirements.txt").write_text("fastapi\nuvicorn\n")
            (path / "main.py").write_text("app = None\n")
            (path / ".env.example").write_text("SERVICE_KEY=\n")
            cancelled = threading.Event()
            cancelled.set()
            outcome = SetupRunner(inspect_project(path), lambda _: None, cancelled).run()
            self.assertIn("cancelled", outcome.detail)
            self.assertFalse((path / ".env").exists())
            self.assertFalse((path / ".venv").exists())

    @unittest.skipIf(os.name == "nt", "Uses a POSIX test executable")
    def test_cancel_during_install_stops_before_launch(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
            npm = path / "npm"
            npm.write_text("#!/bin/sh\nif [ \"$1\" = '--version' ]; then echo 1; exit 0; fi\nexec sleep 60\n")
            npm.chmod(0o755)
            with patch.dict(os.environ, {"PATH": str(path) + os.pathsep + os.environ["PATH"]}):
                started = threading.Event()
                cancelled = threading.Event()
                runner = SetupRunner(inspect_project(path), lambda line: started.set() if line == "$ npm install" else None, cancelled)
                result = []
                thread = threading.Thread(target=lambda: result.append(runner.run()))
                thread.start()
                self.assertTrue(started.wait(3))
                runner.stop()
                thread.join(4)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(runner.application)
                self.assertEqual(result[0].state, "Blocked")

    def test_recovery_rejects_repeated_route_without_model_call(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            decision = decide_failure("Failed", [("npm", "run", "start")], {0}, ())
        self.assertEqual(decision.action, "blocked")

    def test_model_can_diagnose_failure_without_another_launch_route(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "needs_input", "reason": "Configure the external service", "candidate": -1,
        })}]}]}
        with patch("first_run.agent.urlopen") as request:
            request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
            decision = decide_failure("Service refused connection", [("npm", "run", "start")], {0},
                                      ("node: available",), api_key="test-key")
        self.assertEqual(decision.action, "needs_input")
        self.assertEqual(decision.source, "AI")
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(json.loads(sent["input"])["available_launch_candidates"], [])

    def test_model_request_redacts_credentials_from_failure_and_facts(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "blocked", "reason": "Service unavailable", "candidate": -1,
        })}]}]}
        with patch("first_run.agent.urlopen") as request:
            request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
            decision = decide_failure(
                "TOKEN=abc123 Authorization: Bearer token123\n"
                "postgres://alice:password123@localhost/db failed",
                [("npm", "run", "start")], {0},
                ("API_KEY=key123", "mysql://bob:pw456@localhost/db", "SECRET: secret123"),
                api_key="provider-test-key",
            )
        self.assertEqual(decision.source, "AI")
        sent = json.loads(request.call_args.args[0].data)
        evidence = sent["input"]
        for secret in ("abc123", "token123", "password123", "key123", "pw456", "secret123"):
            self.assertNotIn(secret, evidence)
        self.assertIn("[redacted]", evidence)
        self.assertIn("postgres://[redacted]@localhost/db", evidence)

    def test_rejected_api_key_is_visible_and_uses_rules(self):
        with patch("first_run.agent.urlopen", side_effect=HTTPError(
            "https://api.openai.com/v1/responses", 401, "Unauthorized", {}, None,
        )):
            decision = decide_failure("Application exited", [("npm", "run", "start")], {0}, (),
                                      api_key="invalid-key")
        self.assertEqual(decision.action, "blocked")
        self.assertEqual(decision.source, "fallback")
        self.assertIn("HTTP 401", decision.reason)
        self.assertNotIn("invalid-key", decision.reason)

    def test_model_can_choose_allowed_transient_install_retry(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "retry_install", "reason": "Temporary registry timeout", "candidate": -1,
        })}]}]}
        with patch("first_run.agent.urlopen") as request:
            request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
            decision = decide_failure("npm ERR! ETIMEDOUT", [], set(), (),
                                      phase="install", api_key="test-key")
        self.assertEqual(decision.action, "retry_install")
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(json.loads(sent["input"])["phase"], "install")

    def test_model_can_request_only_available_captured_output(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "inspect_output", "reason": "Read earlier startup lines", "candidate": -1,
        })}]}]}
        with patch("first_run.agent.urlopen") as request:
            request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
            allowed = decide_failure("short failure", [], {0}, (), api_key="test-key",
                                     can_inspect_output=True)
            denied = decide_failure("short failure", [], {0}, (), api_key="test-key")
        self.assertEqual(allowed.action, "inspect_output")
        self.assertEqual(denied.action, "blocked")
        self.assertEqual(denied.source, "fallback")

    def test_transient_install_failure_retries_once_without_model(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "network-retry", "version": "1.0.0", "scripts": {"start": "node server.js"},
            }))
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
                    with patch.object(runner, "_command", side_effect=[(1, "npm ERR! ETIMEDOUT"),
                                                                       (0, "installed")]) as command:
                        with patch.object(runner, "_launch_verify", return_value=Outcome("Running", "HTTP 200")):
                            self.assertEqual(runner.run().state, "Running")
                    self.assertEqual(command.call_count, 2)

    def test_model_install_recovery_runs_only_the_detected_install_command(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "model-install", "version": "1.0.0", "scripts": {"start": "node server.js"},
            }))
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "action": "retry_install", "reason": "Retry registry timeout", "candidate": -1,
            })}]}]}
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch("first_run.agent.urlopen") as request:
                    request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
                    runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event(),
                                         api_key="test-key")
                    with patch.object(runner, "_command", side_effect=[(1, "npm ERR! ETIMEDOUT"),
                                                                       (0, "installed")]) as command:
                        with patch.object(runner, "_launch_verify", return_value=Outcome("Running", "HTTP 200")):
                            self.assertEqual(runner.run().state, "Running")
                    self.assertEqual(command.call_args_list[0].args[0], command.call_args_list[1].args[0])
                    self.assertEqual(command.call_count, 2)
                    self.assertTrue(request.called)

    def test_model_cannot_retry_nontransient_install_error(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "retry_install", "reason": "try again", "candidate": -1,
        })}]}]}
        with patch("first_run.agent.urlopen") as request:
            request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
            decision = decide_failure("No matching distribution found", [], set(), (),
                                      phase="install", api_key="test-key")
        self.assertEqual(decision.action, "blocked")

    def test_failure_summary_skips_traceback_closing_lines(self):
        node = "Application exited.\nFailed to connect to MongoDB: Error: querySrv ECONNREFUSED\n    at QueryReqWrap.onresolve\n  code: 'ECONNREFUSED'\n}"
        python = "Traceback (most recent call last):\n  File /tmp/main.py, line 12\nNameError: name 'SingletonMeta' is not defined"
        self.assertIn("MongoDB", failure_summary(node))
        self.assertIn("NameError", failure_summary(python))

    def test_mongodb_connection_failure_requests_intervention(self):
        failure = ("Failed to connect to MongoDB: Error: querySrv ECONNREFUSED "
                   "_mongodb._tcp.cluster0.cojoign.mongodb.net")
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            decision = decide_failure(failure, [("npm", "run", "start")], {0}, ())
        self.assertEqual(decision.action, "needs_input")
        self.assertIn("database URL", decision.reason)
        self.assertEqual(decision.candidate, -1)

    def test_model_cannot_select_unlisted_command(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
            "action": "retry_launch", "reason": "try it", "candidate": 99,
        })}]}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            with patch("first_run.agent.urlopen") as request:
                request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
                decision = decide_failure("Failed", [("npm", "run", "dev"), ("npm", "run", "start")], {0}, ())
        self.assertEqual(decision.action, "retry_launch")
        self.assertEqual(decision.candidate, 1)

    def test_model_can_choose_input_or_blocker_without_starting_another_route(self):
        routes = [("npm", "run", "dev"), ("npm", "run", "start")]
        for action in ("needs_input", "blocked"):
            with self.subTest(action=action):
                reason = "SERVICE_API_KEY is required" if action == "needs_input" else "MongoDB service is unavailable"
                response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                    "action": action, "reason": reason, "candidate": -1,
                })}]}]}
                with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                    with patch("first_run.agent.urlopen") as request:
                        request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
                        decision = decide_failure("MongoDB connection failed", routes, {0}, ())
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.candidate, -1)

    def test_malformed_model_response_falls_back_to_known_route(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            with patch("first_run.agent.urlopen") as request:
                request.return_value.__enter__.return_value = BytesIO(b'{"output": null}')
                decision = decide_failure("Launch failed", [("npm", "run", "dev"), ("npm", "run", "start")], {0}, ())
        self.assertEqual(decision.action, "retry_launch")
        self.assertEqual(decision.candidate, 1)
        self.assertIn("TypeError", decision.reason)

    def test_provider_key_is_not_passed_to_project_commands(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
            runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event(),
                                 api_key="private-test-value")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "private-test-value"}):
                code, output = runner._command((sys.executable, "-c", "import os; print(os.getenv('OPENAI_API_KEY', 'absent'))"))
                self.assertEqual(code, 0)
                self.assertEqual(output.strip(), "absent")
                self.assertNotIn("OPENAI_API_KEY", runner._app_env())

    def test_provider_key_does_not_satisfy_project_env_requirement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({"scripts": {"start": "node server.js"}}))
            (path / ".env.example").write_text("OPENAI_API_KEY=\n")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "private-test-value"}):
                result = SetupRunner(inspect_project(path), lambda _: None, threading.Event()).run()
            self.assertEqual(result.state, "Needs input")
            self.assertIn("OPENAI_API_KEY", result.detail)

    def test_saved_node_route_skips_unchanged_install_but_not_changed_manifest(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({"name": "rerun-test", "version": "1.0.0", "scripts": {"start": "node server.js"}}))
            (path / "server.js").write_text("require('http').createServer((q,r)=>r.end('ready')).listen(3000,'127.0.0.1')")
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                logs = []
                first = SetupRunner(inspect_project(path), logs.append, threading.Event())
                try:
                    self.assertEqual(first.run().state, "Running")
                finally:
                    first.stop()
                self.assertEqual(saved_launch(path), ("npm", "run", "start"))
                second = SetupRunner(inspect_project(path), logs.append, threading.Event())
                try:
                    self.assertEqual(second.run(reuse=True).state, "Running")
                finally:
                    second.stop()
                self.assertTrue(any("installation skipped" in line for line in logs))
                (path / "package.json").write_text(json.dumps({
                    "name": "rerun-test", "version": "1.0.0",
                    "scripts": {"start": "node server.js", "test": "node --version"},
                }))
                self.assertIsNone(saved_launch(path))
                logs.clear()
                third = SetupRunner(inspect_project(path), logs.append, threading.Event())
                try:
                    self.assertEqual(third.run(reuse=True).state, "Running")
                finally:
                    third.stop()
                self.assertTrue(any(line in ("$ npm install", "$ npm ci") for line in logs))
                self.assertFalse(any("installation skipped" in line for line in logs))

    def test_failed_dev_script_retries_detected_start_without_provider_key(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "route-test", "version": "1.0.0",
                "scripts": {"dev": "node broken.js", "start": "node server.js"},
            }))
            (path / "broken.js").write_text("process.exit(1)")
            (path / "server.js").write_text("require('http').createServer((q,r)=>r.end('ready')).listen(3000,'127.0.0.1')")
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    logs = []
                    runner = SetupRunner(inspect_project(path), logs.append, threading.Event())
                    try:
                        result = runner.run()
                        self.assertEqual(result.state, "Running")
                        self.assertEqual(saved_launch(path), ("npm", "run", "start"))
                        self.assertTrue(any("Trying another detected entry point" in line for line in logs))
                    finally:
                        runner.stop()

    def test_recovery_can_observe_two_failures_before_third_route_succeeds(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "three-routes", "version": "1.0.0", "scripts": {
                    "dev": "node broken.js", "start": "node broken.js",
                    "serve": "node server.js",
                },
            }))
            (path / "broken.js").write_text("console.error('route failed'); process.exit(1)")
            (path / "server.js").write_text(
                "require('http').createServer((q,r)=>r.end('ready')).listen(3000,'127.0.0.1')"
            )
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    logs = []
                    runner = SetupRunner(inspect_project(path), logs.append, threading.Event())
                    try:
                        result = runner.run()
                        self.assertEqual(result.state, "Running", result.detail)
                        self.assertEqual(saved_launch(path), ("npm", "run", "serve"))
                        self.assertEqual(sum(line.startswith("Recovery (") for line in logs), 2)
                    finally:
                        runner.stop()

    def test_execution_rejects_a_repeated_model_route(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({
                "scripts": {"dev": "node broken.js", "start": "node server.js"},
            }))
            runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
            with patch.object(runner, "_command", return_value=(0, "installed")):
                with patch.object(runner, "_launch_verify", return_value=Outcome("Blocked", "failed")) as launch:
                    with patch("first_run.runner.decide_failure", return_value=Decision(
                        "retry_launch", "repeat the first route", 0, "AI",
                    )):
                        result = runner.run()
            self.assertEqual(result.state, "Blocked")
            self.assertIn("previously tried", result.detail)
            self.assertEqual(launch.call_count, 1)

    def test_agent_inspects_captured_output_once_then_retries_known_route(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({
                "scripts": {"dev": "node broken.js", "start": "node server.js"},
            }))
            runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
            def launch(_):
                if not hasattr(launch, "seen"):
                    launch.seen = True
                    runner.output = ["earlier diagnostic " * 200, "last failure"]
                    return Outcome("Blocked", "last failure")
                return Outcome("Running", "HTTP 200", "http://127.0.0.1:3000/")
            with patch("first_run.history.history_path", return_value=path / "history.json"):
                with patch.object(runner, "_command", return_value=(0, "installed")):
                    with patch.object(runner, "_launch_verify", side_effect=launch) as started:
                        with patch("first_run.runner.decide_failure", side_effect=[
                            Decision("inspect_output", "Need earlier lines", source="AI"),
                            Decision("retry_launch", "Use start script", 1, "AI"),
                        ]) as decide:
                            result = runner.run()
            self.assertEqual(result.state, "Running")
            self.assertEqual(started.call_count, 2)
            self.assertTrue(decide.call_args_list[0].kwargs["can_inspect_output"])
            self.assertFalse(decide.call_args_list[1].kwargs["can_inspect_output"])
            self.assertIn("earlier diagnostic", decide.call_args_list[1].args[0])

    def test_model_inspects_entry_points_then_retries_verified_route(self):
        project = Path(__file__).resolve().parents[1] / "examples" / "flask_recovery"
        replies = [
            {"action": "inspect_entry_points", "reason": "Check which file defines app", "candidate": -1},
            {"action": "retry_launch", "reason": "main.py defines app", "candidate": 1},
        ]
        responses = [BytesIO(json.dumps({"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(reply)},
        ]}]}).encode()) for reply in replies]
        logs = []
        runner = SetupRunner(inspect_project(project), logs.append, threading.Event(), api_key="test-key")
        with TemporaryDirectory() as directory:
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.object(runner, "_command", return_value=(0, "installed")):
                    with patch.object(runner, "_launch_verify", side_effect=[
                        Outcome("Blocked", "Error: Failed to find Flask application in app.py"),
                        Outcome("Running", "HTTP 200", "http://127.0.0.1:5000/"),
                    ]) as launched:
                        with patch("first_run.agent.urlopen") as request:
                            request.return_value.__enter__.side_effect = responses
                            result = runner.run()
        self.assertEqual(result.state, "Running")
        self.assertEqual(launched.call_count, 2)
        self.assertIn("app.py", launched.call_args_list[0].args[0])
        self.assertIn("main.py", launched.call_args_list[1].args[0])
        first = json.loads(json.loads(request.call_args_list[0].args[0].data)["input"])
        second = json.loads(json.loads(request.call_args_list[1].args[0].data)["input"])
        self.assertIn("inspect_entry_points", first["available_inspections"])
        self.assertNotIn("inspect_entry_points", second["available_inspections"])
        self.assertIn("inspect_entry_points", second["inspections_used"])
        self.assertTrue(any("main.py: module-level app assignment" in item for item in second["observations"]))
        self.assertTrue(any("Observed: Static entry point hints" in line for line in logs))
        self.assertTrue(any("Recovery (AI): Inspect entry points —" in line for line in logs))
        self.assertTrue(any("Recovery (AI): Retry launch —" in line for line in logs))

    def test_repeated_entry_inspection_is_rejected_at_execution(self):
        project = Path(__file__).resolve().parents[1] / "examples" / "flask_recovery"
        runner = SetupRunner(inspect_project(project), lambda _: None, threading.Event())
        with TemporaryDirectory() as directory:
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.object(runner, "_command", return_value=(0, "installed")):
                    with patch.object(runner, "_launch_verify", return_value=Outcome("Blocked", "failed")) as launched:
                        with patch("first_run.runner.decide_failure", return_value=Decision(
                            "inspect_entry_points", "look again", source="AI",
                        )) as decide:
                            result = runner.run()
        self.assertEqual(result.state, "Blocked")
        self.assertIn("repeated inspection", result.detail)
        self.assertEqual(decide.call_count, 2)
        self.assertEqual(launched.call_count, 1)

    def test_model_selects_known_start_route_after_real_launch_failure(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "model-route-test", "version": "1.0.0",
                "scripts": {"dev": "node broken.js", "start": "node server.js"},
            }))
            (path / "broken.js").write_text("console.error('dev failed'); process.exit(1)")
            (path / "server.js").write_text("require('http').createServer((q,r)=>r.end('ready')).listen(3000,'127.0.0.1')")
            response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({
                "action": "retry_launch", "reason": "Use the detected start script", "candidate": 1,
            })}]}]}
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
                    with patch("first_run.agent.urlopen") as request:
                        request.return_value.__enter__.return_value = BytesIO(json.dumps(response).encode())
                        runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
                        try:
                            self.assertEqual(runner.run().state, "Running")
                            self.assertEqual(saved_launch(path), ("npm", "run", "start"))
                            sent = json.loads(request.call_args.args[0].data)
                            self.assertEqual(json.loads(sent["input"])["available_launch_candidates"][0]["index"], 1)
                        finally:
                            runner.stop()

    def test_source_bug_is_blocked_without_source_changes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "package.json").write_text(json.dumps({
                "name": "source-bug-test", "version": "1.0.0", "scripts": {"start": "node broken.js"},
            }))
            source = "throw new Error('source bug needs a code fix')\n"
            (path / "broken.js").write_text(source)
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                outcome = SetupRunner(inspect_project(path), lambda _: None, threading.Event()).run()
            self.assertEqual(outcome.state, "Blocked")
            self.assertIn("source bug needs a code fix", outcome.detail)
            self.assertEqual((path / "broken.js").read_text(), source)

    def test_continue_after_database_intervention_skips_completed_install(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project"
            path.mkdir()
            (path / "package.json").write_text(json.dumps({
                "name": "service-test", "version": "1.0.0", "scripts": {"start": "node server.js"},
            }))
            (path / "server.js").write_text(
                "if (!require('fs').existsSync('database-ready')) {\n"
                "  console.error('Failed to connect to MongoDB: Error: connect ECONNREFUSED 127.0.0.1:27017');\n"
                "  process.exit(1);\n"
                "}\n"
                "require('http').createServer((q,r)=>r.end('ready')).listen(3000,'127.0.0.1');\n"
            )
            with patch("first_run.history.history_path", return_value=Path(directory) / "history.json"):
                with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                    first = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
                    self.assertEqual(first.run().state, "Needs input")
                    (path / "database-ready").touch()
                    logs = []
                    second = SetupRunner(inspect_project(path), logs.append, threading.Event())
                    try:
                        self.assertEqual(second.run(reuse=True).state, "Running")
                        self.assertTrue(any("installation skipped" in line for line in logs))
                        self.assertFalse(any(line.startswith("$ npm install") for line in logs))
                    finally:
                        second.stop()

    def test_django_recovery_only_migrates_fresh_local_sqlite(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "requirements.txt").write_text("Django==5.1.15\n")
            (path / "manage.py").write_text("# project entry\n")
            database = path / "db.sqlite3"
            runner = SetupRunner(inspect_project(path), lambda _: None, threading.Event())
            probe = "FIRST_RUN_DB " + json.dumps(["django.db.backends.sqlite3", str(database)])
            launch = (sys.executable, "manage.py", "runserver")
            with patch.object(runner, "_command", side_effect=[(0, probe), (0, "migrated")]) as command:
                with patch.object(runner, "_launch_verify", return_value=Outcome("Running", "HTTP 200")):
                    self.assertEqual(runner._recover_django_migrations(launch).state, "Running")
            self.assertEqual(command.call_args_list[1].args[0][1:], ("manage.py", "migrate", "--noinput"))

            database.write_bytes(b"existing database")
            with patch.object(runner, "_command", return_value=(0, probe)) as command:
                result = runner._recover_django_migrations(launch)
            self.assertEqual(result.state, "Needs input")
            self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
