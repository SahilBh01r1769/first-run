"""Run local setup steps and keep the launched web process alive."""

import ast
from dataclasses import dataclass
import os
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse
from urllib.request import urlopen

from first_run.inspect import ProjectInfo
from first_run.agent import decide_failure, observe_failure
from first_run.history import installed_setup, save_install, save_project, saved_launch


STARTUP_TIMEOUT = 30


@dataclass(frozen=True)
class Outcome:
    state: str
    detail: str
    address: str | None = None


@dataclass
class RecoveryState:
    tried_routes: set[int]
    inspections_used: set[str]
    decisions_remaining: int = 6


class SetupRunner:
    def __init__(self, info: ProjectInfo, report, cancelled: threading.Event, api_key: str | None = None):
        self.info = info
        self.report = report
        self.cancelled = cancelled
        self.api_key = api_key
        self.active: subprocess.Popen | None = None
        self.application: subprocess.Popen | None = None
        self.app_reader: threading.Thread | None = None
        self.output: list[str] = []
        self.verified_launch: tuple[str, ...] | None = None

    def _command(self, args: tuple[str, ...], timeout: int = 600) -> tuple[int, str]:
        self.report("$ " + " ".join(args))
        process = subprocess.Popen(
            args, cwd=self.info.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=(os.name != "nt"),
            env=self._base_env(),
        )
        self.active = process
        lines: list[str] = []

        def drain():
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if len(lines) > 200:
                    lines.pop(0)
                self.report(line.rstrip())

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if self.cancelled.is_set() or time.monotonic() > deadline:
                self._stop(process)
                reader.join(timeout=2)
                process.stdout.close()
                self.active = None
                return -1, "Cancelled or timed out.\n" + "".join(lines[-30:])
            time.sleep(0.1)
        reader.join(timeout=2)
        process.stdout.close()
        self.active = None
        return process.returncode, "".join(lines[-30:])

    @staticmethod
    def _stop(process: subprocess.Popen):
        if process.poll() is not None:
            return
        # Child processes inherit the process group on POSIX; stop the group on cancellation.
        if os.name != "nt":
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def stop(self):
        self.cancelled.set()
        for process in (self.active, self.application):
            if process:
                self._stop(process)
        if self.app_reader:
            self.app_reader.join(timeout=2)
        if self.application and self.application.stdout:
            self.application.stdout.close()

    def _python(self) -> Path:
        return self.info.path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def _environment_values(self) -> tuple[str, ...]:
        example = self.info.path / ".env.example"
        local = self.info.path / ".env"
        if not example.exists():
            return ()
        if not local.exists():
            if self.cancelled.is_set():
                return ()
            content = example.read_text(errors="replace")
            if self.cancelled.is_set():
                return ()
            local.write_text(content)
            self.report("Created .env from .env.example")
        values = {}
        for line in local.read_text(errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"\'')
        child_env = self._base_env()
        return tuple(key for key in self.info.needs_env if not values.get(key) and not child_env.get(key))

    @staticmethod
    def _base_env() -> dict[str, str]:
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        env.pop("FIRST_RUN_MODEL", None)
        return env

    def _app_env(self) -> dict[str, str]:
        env = self._base_env()
        local = self.info.path / ".env"
        if local.is_file():
            for line in local.read_text(errors="replace").splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key and key not in env:
                    env[key] = value.strip().strip('"\'')
        return env

    def run(self, reuse: bool = False) -> Outcome:
        if self.cancelled.is_set():
            return Outcome("Blocked", "Run cancelled before setup.")
        if not self.info.launch:
            if self.info.framework == "Multiple components":
                return Outcome("Needs input", self.info.observations[0])
            return Outcome("Blocked", "No supported launch route was found. See Output for the detected project facts; this version starts common root Python web entries or npm dev/start/serve scripts.")
        if self.info.runtime_issue:
            return Outcome("Needs input", self.info.runtime_issue)
        if self.info.kind == "node" and (not shutil.which("node") or not shutil.which("npm")):
            return Outcome("Needs input", "Node and npm must be installed on the machine. System runtime installation needs your approval outside First Run.")
        missing = self._environment_values()
        if missing:
            return Outcome("Needs input", f"Fill {', '.join(missing)} in {self.info.path / '.env'}, then continue setup.")
        if self.cancelled.is_set():
            return Outcome("Blocked", "Run cancelled before setup.")

        if self.info.kind == "python":
            python = self._python()
            created = not python.exists()
            if not python.exists():
                if self.cancelled.is_set():
                    return Outcome("Blocked", "Run cancelled before setup.")
                code, output = self._command((sys.executable, "-m", "venv", ".venv"), 120)
                if code:
                    return Outcome("Blocked", "Could not create a virtual environment. " + output[-1200:])
            install = (str(python), "-m", *self.info.install)
            launch = ((str(python), *self.info.launch[1:]) if self.info.framework == "django"
                      else (str(python), "-m", *self.info.launch))
        else:
            install = self.info.install
            launch = self.info.launch
        if not install or not launch:
            return Outcome("Blocked", "No supported package manager or launch route was found.")
        routes = self._launch_routes(launch)
        previous = saved_launch(self.info.path) if reuse and not (self.info.kind == "python" and created) else None
        if previous and previous not in routes and self.info.framework in ("fastapi", "flask", "django"):
            saved_port = self._port_for(previous)
            if saved_port and 1024 <= saved_port <= 65535 and any(
                    self._with_port(route, saved_port) == previous for route in routes):
                routes.append(previous)
        ready = installed_setup(self.info.path, self.info.kind)
        if previous not in routes or not ready:
            previous = None
        if previous or (reuse and ready):
            self.report("Using completed setup; dependency installation skipped.")
        else:
            if self.cancelled.is_set():
                return Outcome("Blocked", "Run cancelled before dependency installation.")
            code, output = self._command(install)
            if code:
                if self.cancelled.is_set():
                    return Outcome("Blocked", "Run cancelled during dependency installation.")
                observed = observe_failure(output, "install")
                self.report(f"Observed: {observed.category}: {observed.detail}")
                facts = (f"Detected {self.info.framework} ({self.info.kind})",
                         f"Install route: {' '.join(self.info.install)}", *self.info.observations)
                decision = decide_failure(output, [], set(), facts,
                                          phase="install", api_key=self.api_key)
                self.report(f"Recovery ({decision.source}): {decision.action.replace('_', ' ').capitalize()} — {decision.reason}")
                if decision.action == "needs_input":
                    return Outcome("Needs input", decision.reason)
                if decision.action != "retry_install":
                    return Outcome("Blocked", decision.reason)
                code, output = self._command(install)
                if code:
                    return Outcome("Blocked", "Dependency installation failed again. " + output[-1200:])
            save_install(self.info.path)
        if self.cancelled.is_set():
            return Outcome("Blocked", "Run cancelled.")

        route_index = routes.index(previous) if previous else 0
        recovery = RecoveryState(set(), set())
        migrations_checked = False
        # Each route is tried once; the final decision may still explain a blocker.
        for attempt_no in range(min(len(routes), 5)):
            recovery.tried_routes.add(route_index)
            result = self._launch_verify(routes[route_index])
            if result.state == "Running":
                save_project(self.info.path, self.verified_launch or routes[route_index])
                return result
            if self.cancelled.is_set():
                return result
            failure_output = "\n".join(self.output).lower()
            if (not migrations_checked and self.info.framework == "django"
                    and "unapplied migration" in failure_output and "no such table" in failure_output):
                migrations_checked = True
                recovered = self._recover_django_migrations(routes[route_index])
                if recovered is not None:
                    if recovered.state == "Running":
                        save_project(self.info.path, self.verified_launch or routes[route_index])
                    return recovered
            project_facts = (f"Detected {self.info.framework} ({self.info.kind})",
                             f"Failed route: {' '.join(routes[route_index])}",
                             f"Routes tried: {len(recovery.tried_routes)} of {len(routes)}", *self.info.observations)
            observed = observe_failure(result.detail)
            self.report(f"Observed: {observed.category}: {observed.detail}")
            recent_output = "\n".join(self.output[-100:])
            failure_details = result.detail
            inspection_results: list[str] = []
            while True:
                if recovery.decisions_remaining == 0:
                    return Outcome("Blocked", "Recovery decision limit reached.")
                can_inspect_output = ("inspect_output" not in recovery.inspections_used
                                      and len(recent_output) > len(result.detail) + 400)
                can_inspect_entry_points = ("inspect_entry_points" not in recovery.inspections_used
                                            and self.info.framework in ("flask", "fastapi")
                                            and any(i not in recovery.tried_routes for i in range(len(routes))))
                decision = decide_failure(
                    failure_details, routes, recovery.tried_routes.copy(), (*project_facts, *inspection_results),
                    api_key=self.api_key, can_inspect_output=can_inspect_output,
                    can_inspect_entries=can_inspect_entry_points,
                    inspections_used=tuple(sorted(recovery.inspections_used)),
                    decisions_remaining=recovery.decisions_remaining,
                )
                recovery.decisions_remaining -= 1
                self.report(f"Recovery ({decision.source}): {decision.action.replace('_', ' ').capitalize()} — {decision.reason}")
                if decision.action not in ("inspect_output", "inspect_entry_points"):
                    break
                if decision.action == "inspect_output" and can_inspect_output:
                    recovery.inspections_used.add("inspect_output")
                    failure_details = result.detail + "\nAdditional captured process output:\n" + recent_output[-5000:]
                    self.report("Observed: inspected more of the captured process output.")
                elif decision.action == "inspect_entry_points" and can_inspect_entry_points:
                    recovery.inspections_used.add("inspect_entry_points")
                    observation = self._entry_point_hints()
                    inspection_results.append(observation)
                    self.report("Observed: " + observation)
                else:
                    return Outcome("Blocked", "Recovery requested an unavailable or repeated inspection.")
            if decision.action == "needs_input":
                return Outcome("Needs input", decision.reason)
            if decision.action != "retry_launch":
                return Outcome("Blocked", decision.reason)
            # Validate again at the execution boundary, including mocked or future deciders.
            if decision.candidate in recovery.tried_routes or not 0 <= decision.candidate < len(routes):
                return Outcome("Blocked", "Recovery selected an invalid or previously tried route.")
            if attempt_no == 4:
                return Outcome("Blocked", "Recovery attempt limit reached; no further route was started.")
            route_index = decision.candidate
            self.report("Trying another detected entry point after the failed launch.")
        return Outcome("Blocked", "Recovery attempt limit reached; no further route was started.")

    def _entry_point_hints(self) -> str:
        """Inspect only conventional Python entry files; never import or run source."""
        hints = []
        for name in ("app.py", "main.py"):
            path = self.info.path / name
            if not path.is_file() or not path.resolve().is_relative_to(self.info.path.resolve()):
                continue
            if path.stat().st_size > 65536:
                hints.append(f"{name}: too large for static inspection")
                continue
            try:
                tree = ast.parse(path.read_text(errors="replace"))
            except (OSError, SyntaxError, UnicodeError):
                hints.append(f"{name}: could not parse")
                continue
            assigned = any(
                (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "app"
                                                       for target in node.targets))
                or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                    and node.target.id == "app") for node in tree.body
            )
            factory = any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.name == "create_app" for node in tree.body)
            label = "module-level app assignment" if assigned else "create_app factory" if factory else "no obvious app object"
            hints.append(f"{name}: {label}")
        return "Static entry point hints (not proof of a working app): " + "; ".join(hints)

    def _recover_django_migrations(self, launch: tuple[str, ...]) -> Outcome | None:
        """Migrate only a new SQLite file inside the selected project folder."""
        probe = (
            "import json; from django.conf import settings; "
            "db=settings.DATABASES['default']; "
            "print('FIRST_RUN_DB '+json.dumps([db['ENGINE'], str(db['NAME'])]))"
        )
        code, output = self._command((launch[0], "manage.py", "shell", "-c", probe), 30)
        match = re.search(r"^FIRST_RUN_DB (\[.*\])$", output, re.MULTILINE)
        if code or not match:
            return None
        try:
            engine, name = json.loads(match.group(1))
            database = Path(name).resolve()
            fresh = (engine == "django.db.backends.sqlite3" and name != ":memory:"
                     and database.is_relative_to(self.info.path)
                     and (not database.exists() or database.stat().st_size == 0))
        except (ValueError, OSError, TypeError):
            fresh = False
        if not fresh:
            return Outcome("Needs input", "Django reports unapplied migrations, but its database is not a fresh "
                           "project-local SQLite file. Review and apply the project's migrations yourself, "
                           "then click Continue setup.")
        self.report("Recovery (rules): applying Django migrations to a fresh local SQLite database.")
        code, output = self._command((launch[0], "manage.py", "migrate", "--noinput"), 120)
        if code:
            return Outcome("Blocked", "Django migrations failed. " + output[-1200:])
        if self.cancelled.is_set():
            return Outcome("Blocked", "Run cancelled after migrations.")
        return self._launch_verify(launch)

    def _launch_routes(self, launch: tuple[str, ...]) -> list[tuple[str, ...]]:
        routes = [launch]
        if self.info.framework == "fastapi":
            for entry in ("main.py", "app.py"):
                if (self.info.path / entry).is_file():
                    route = (launch[0], "-m", "uvicorn", f"{entry[:-3]}:app", "--host", "127.0.0.1")
                    if route not in routes:
                        routes.append(route)
        elif self.info.framework == "flask":
            for entry in ("app.py", "main.py"):
                if (self.info.path / entry).is_file():
                    route = (launch[0], "-m", "flask", "--app", entry, "run", "--host", "127.0.0.1")
                    if route not in routes:
                        routes.append(route)
        elif self.info.kind == "node" and (self.info.path / "package.json").is_file():
            import json
            scripts = json.loads((self.info.path / "package.json").read_text()).get("scripts", {})
            for name in ("start", "dev", "serve"):
                route = ("npm", "run", name)
                if name in scripts and route not in routes:
                    routes.append(route)
        return routes

    @staticmethod
    def _port_in_use(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    def _port_for(self, launch: tuple[str, ...]) -> int | None:
        defaults = {"fastapi": 8000, "django": 8000, "flask": 5000}
        port = defaults.get(self.info.framework)
        if port is None:
            return None
        if self.info.framework == "django":
            match = re.fullmatch(r"127\.0\.0\.1:(\d+)", launch[-1])
            return int(match[1]) if match else port
        if "--port" in launch:
            try:
                return int(launch[launch.index("--port") + 1])
            except (IndexError, ValueError):
                return port
        return port

    def _with_port(self, launch: tuple[str, ...], port: int) -> tuple[str, ...]:
        if self.info.framework == "django":
            return (*launch[:-1], f"127.0.0.1:{port}")
        if "--port" in launch:
            index = launch.index("--port") + 1
            return (*launch[:index], str(port), *launch[index + 1:])
        return (*launch, "--port", str(port))

    def _launch_verify(self, launch: tuple[str, ...]) -> Outcome:
        self.output = []
        self.verified_launch = None
        port = self._port_for(launch)
        if port and self._port_in_use(port):
            alternative = next((candidate for candidate in range(port + 1, port + 10)
                                if not self._port_in_use(candidate)), None)
            if alternative is None:
                return Outcome("Blocked", f"Port {port} is in use; no nearby free port was found.")
            self.report(f"Observed: port {port} is in use before launch.")
            self.report(f"Recovery (rules): port {port} is in use; trying port {alternative}.")
            launch = self._with_port(launch, alternative)
        candidates = self._ports(launch)
        ports = {urlparse(url).port for url in candidates}
        occupied = {
            port for port in ports
            if self._port_in_use(port)
        }
        self.report("$ " + " ".join(launch))
        self.application = subprocess.Popen(
            launch, cwd=self.info.path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=(os.name != "nt"), env=self._app_env(),
        )
        app = self.application

        def collect():
            assert app.stdout is not None
            for line in app.stdout:
                self.output.append(line.rstrip())
                if len(self.output) > 100:
                    self.output.pop(0)
                self.report(line.rstrip())

        self.app_reader = threading.Thread(target=collect, daemon=True)
        self.app_reader.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT
        rejected: dict[str, int] = {}
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                self.stop()
                return Outcome("Blocked", "Run cancelled.")
            if app.poll() is not None:
                if self.app_reader:
                    self.app_reader.join(timeout=2)
                if app.stdout:
                    app.stdout.close()
                return Outcome("Blocked", "Application exited during startup. " + "\n".join(self.output[-20:])[-1600:])
            found = re.findall(r"https?://(?:localhost|127\.0\.0\.1):\d+", "\n".join(self.output))
            # A printed URL alone does not establish which process owns that port.
            # V1 verifies only the ports checked before launching the application.
            for url in dict.fromkeys([*found, *candidates]):
                port = urlparse(url).port
                if port not in ports or port in occupied:
                    continue
                try:
                    with urlopen(url, timeout=0.7) as response:
                        status = response.status
                    if 200 <= status < 400 and app.poll() is None:
                        self.verified_launch = launch
                        return Outcome("Running", f"HTTP {status} from {url}", url)
                    rejected[url] = status
                except HTTPError as exc:
                    rejected[url] = exc.code
                except (OSError, URLError):
                    pass
            time.sleep(0.5)
        self._stop(app)
        if self.app_reader:
            self.app_reader.join(timeout=2)
        if app.stdout:
            app.stdout.close()
        if rejected:
            responses = ", ".join(f"HTTP {status} from {url}" for url, status in rejected.items())
            return Outcome("Blocked", "Application responded, but no checked route verified usable: " + responses
                           + ". See Output for startup details.")
        return Outcome("Blocked", f"Application did not respond over HTTP within {STARTUP_TIMEOUT} seconds. "
                       + "\n".join(self.output[-15:])[-1200:])

    @staticmethod
    def _responds(url: str) -> bool:
        try:
            with urlopen(url, timeout=0.3):
                return True
        except HTTPError:
            return True
        except (OSError, URLError):
            return False

    def _ports(self, launch: tuple[str, ...] | None = None) -> list[str]:
        ports = {"django": (8000,), "fastapi": (8000,), "flask": (5000,),
                 "streamlit": (8501,), "Node web": (3000, 5173, 8080, 4173)}
        selected = self._port_for(launch) if launch else None
        numbers = (selected,) if selected else ports.get(self.info.framework, ())
        roots = [f"http://127.0.0.1:{port}/" for port in numbers]
        if self.info.framework == "fastapi":
            return [url for root in roots for url in (root, root + "docs", root + "openapi.json")]
        if self.info.framework == "streamlit":
            return [url for root in roots for url in (root, root + "_stcore/health")]
        return roots
