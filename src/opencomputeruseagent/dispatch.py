"""What one tool call does: check, ask, refuse, forward to the helper, frame the answer."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import APP_NAME, PATCH_REVISION, UPSTREAM_VERSION, __version__, consent, framing, guard, procs, schema, tools
from .desktop_lock import DesktopLock
from .helper import CALL_TIMEOUT, Cancelled, Helper, HelperError, run_cli
from .payload import (INSTALL_ROOT, UNSUPPORTED, Payload, PayloadError, describe_platform, is_serve_daemon,
                      missing_session_variables, target as detect_target)

NEEDS_OBSERVATION = ("click", "perform_secondary_action", "scroll", "drag", "type_text", "press_key", "set_value")
KEYBOARD = ("type_text", "press_key")
STALE_REFUSAL = ("Run get_app_state for {app} in this chat first. An action needs a fresh look at the app, "
                 "and a failed or uncertain action counts as a change.")


DETECT = object()


class Observation:
    def __init__(self, facts, title):
        self.facts, self.title = facts, title


class Dispatcher:
    def __init__(self, ask=None, environ=None, root=INSTALL_ROOT, lock=None, log=None, target=DETECT,
                 helper_factory=Helper, identity=None, clock=time.monotonic):
        self.environ = os.environ if environ is None else environ
        self.root = root
        self.log = log or (lambda message: None)
        self.target = detect_target() if target is DETECT else target
        self.payload = Payload(self.target, root) if self.target else None
        self.helper_factory = helper_factory
        self.helper = None
        self.helper_owner = None
        self.helper_lock = threading.Lock()
        self.observed = {}
        self.desktop = lock or DesktopLock()
        helper_paths = [str(self.payload.directory.parent)] if self.payload else []
        self.identity = identity or guard.CachedIdentity(helper_paths)
        self.grants = consent.Grants(ask or (lambda params, cancel: {"action": "cancel"}),
                                     self.desktop.acquire, self.desktop.release, self._owner_ended, clock=clock)
        self._stop = threading.Event()

    # -- lifetime ---------------------------------------------------------
    def startup(self):
        """Clean up what an earlier install or a killed wrapper left behind."""
        if not self.payload:
            return
        try:
            stale = self.payload.stale_version_dirs()
            if stale and self.target.startswith("darwin-"):
                end_app_agents([d for d in stale])
            self.payload.remove_stale()
        except OSError as error:
            self.log(f"cleanup failed: {error}")
        threading.Thread(target=self._reaper, daemon=True).start()

    def _reaper(self):
        while not self._stop.wait(30):
            self.grants.expire()

    def shutdown(self):
        self._stop.set()
        self.grants.end_all()
        self._stop_helper()
        if self.payload and self.target.startswith("darwin-"):
            # The app agent is not our child: end it unless another wrapper of this install uses it.
            if not other_proxies_running(self.payload):
                end_app_agents([self.payload.directory])

    def _owner_ended(self, session):
        self.observed.pop(session, None)
        self._stop_helper()

    def _stop_helper(self):
        helper, self.helper, self.helper_owner = self.helper, None, None
        if helper is not None:
            helper.stop()

    def _helper_for(self, session):
        """The helper for this chat. A chat taking over the desktop gets a fresh helper, so
        no app state crosses from one chat to another."""
        if self.helper is not None and (self.helper_owner != session or not self.helper.alive):
            self._stop_helper()
            self.observed = {}
        if self.helper is None:
            # A new helper knows no app state, so nothing observed before it counts.
            self.observed = {}
            self.helper = self.helper_factory(self.payload, environ=self.environ, log=self.log).start()
            self.helper_owner = session
        return self.helper

    # -- one call ---------------------------------------------------------
    def call(self, name, arguments, session, cancel=None):
        try:
            return self._call(name, arguments if arguments is not None else {}, session, cancel)
        except consent.Refusal as refusal:
            return framing.error_result(str(refusal))
        except (PayloadError, HelperError) as error:
            return framing.error_result(str(error))
        except Cancelled:
            return framing.error_result("The call was cancelled. " + "Its outcome is uncertain: run get_app_state "
                                        "before trying again.")

    def _call(self, name, arguments, session, cancel):
        kind = tools.kind_of(name)
        if kind is None:
            return framing.error_result(f"Unknown tool: {name}")
        if not isinstance(arguments, dict):
            return framing.error_result("arguments must be an object")
        if name == "setup_status":
            return framing.text_result(json.dumps(self.setup_status(), indent=2))
        if name == "open_permission_setup":
            return self.open_permission_setup()
        contract = tools.load_contract()
        problems = schema.errors(arguments, contract[name])
        if problems:
            return framing.error_result("Invalid arguments: " + "; ".join(problems))
        if not self.target:
            return framing.error_result(UNSUPPORTED.format(detail=describe_platform()))
        if is_serve_daemon(self.environ):
            return framing.error_result(consent.SERVE_REFUSAL)
        missing = missing_session_variables(self.target, self.environ)
        if missing:
            return framing.error_result("Open Computer Use Agent needs a signed-in desktop session, and "
                                        f"{', '.join(missing)} is not set. Run setup_status for details.")
        refusal = self._refuse_arguments(name, arguments)
        if refusal:
            return framing.error_result(refusal)
        app = arguments.get("app")
        if isinstance(app, str) and guard.query_names_protected(app):
            return framing.error_result(guard.SECURITY_PROMPT_REFUSAL if guard.is_security_prompt(
                guard.Facts(app, app, 0)) else guard.OWN_REFUSAL)
        needed = consent.ACT if kind == tools.ACT else consent.LOOK
        self.grants.ensure(session, needed, cancel)
        with self.helper_lock:
            if self.grants.level(session) < needed:
                raise consent.Refusal(consent.LOOK_ONLY_REFUSAL if needed == consent.ACT else consent.DECLINED_REFUSAL)
            helper = self._helper_for(session)
            if name in NEEDS_OBSERVATION:
                refusal = self._refuse_target(name, app, session)
                if refusal:
                    return framing.error_result(refusal)
            if name in KEYBOARD and guard.secure_input_enabled():
                return framing.error_result(guard.SECURE_INPUT_REFUSAL)
            if name == "screen_capture" and not arguments.get("list_only"):
                refusal = self._refuse_capture(helper, arguments, cancel)
                if refusal:
                    return framing.error_result(refusal)
            try:
                result = helper.call_tool(name, arguments, timeout=CALL_TIMEOUT, cancel=cancel)
            except (HelperError, Cancelled):
                self._forget(session, app)
                raise
            return self._after(name, arguments, session, result)

    def _refuse_arguments(self, name, arguments):
        if name == "type_text":
            return guard.secret_refusal(arguments.get("text", ""))
        if name == "set_value":
            return guard.secret_refusal(arguments.get("value", ""))
        if name == "press_key" and guard.is_clipboard_chord(arguments.get("key", "")):
            return guard.CLIPBOARD_REFUSAL
        if name == "screen_capture" and not arguments.get("window_title") and not arguments.get("list_only"):
            return guard.DISPLAY_CAPTURE_REFUSAL
        if name == "screen_capture" and "display" in arguments and not arguments.get("list_only"):
            return guard.DISPLAY_CAPTURE_REFUSAL
        return None

    def _refuse_target(self, name, app, session):
        observation = self.observed.get(session, {}).get(app)
        if observation is None:
            return STALE_REFUSAL.format(app=json.dumps(app))
        refusal = self._refuse_facts(observation.facts, observation.title)
        if refusal:
            return refusal
        if name in guard.KEYBOARD_LIKE and guard.is_terminal(observation.facts):
            return guard.TERMINAL_REFUSAL
        if guard.is_privacy_settings(observation.facts, observation.title):
            return guard.PRIVACY_SETTINGS_REFUSAL
        return None

    def _refuse_facts(self, facts, title=None):
        if self.identity.get().owns(facts):
            return guard.OWN_REFUSAL
        if guard.is_security_prompt(facts):
            return guard.SECURITY_PROMPT_REFUSAL
        return None

    def _refuse_capture(self, helper, arguments, cancel):
        probe = helper.call_tool("screen_capture", {"window_title": arguments["window_title"], "list_only": True},
                                 timeout=CALL_TIMEOUT, cancel=cancel)
        if probe.get("isError"):
            return framing.text_of(probe) or "The window could not be found."
        try:
            target = json.loads(framing.text_of(probe)).get("target") or {}
        except ValueError:
            return "The window could not be identified, so it was not captured."
        facts = guard.Facts(str(target.get("app") or ""), None, int(target.get("pid") or 0))
        return self._refuse_facts(facts, target.get("title"))

    def _forget(self, session, app):
        self.observed.get(session, {}).pop(app, None)

    def _after(self, name, arguments, session, result):
        app = arguments.get("app")
        text = framing.text_of(result)
        if name == "list_apps":
            return framing.frame_result(self._filter_list(result))
        if name == "screen_capture":
            if arguments.get("list_only"):
                return framing.frame_result(self._filter_topology(result))
            return framing.frame_result(result, None)
        facts, title = guard.facts_from_state(text)
        if facts is not None:
            refusal = self._refuse_facts(facts, title)
            if refusal:
                self._forget(session, app)
                return framing.error_result(refusal)
        if result.get("isError") or facts is None:
            # A failed or unreadable action is a change of unknown extent: look again first.
            if name != "get_app_state" or result.get("isError"):
                self._forget(session, app)
            return framing.frame_result(result, facts.name if facts else app)
        self.observed.setdefault(session, {})[app] = Observation(facts, title)
        return framing.frame_result(result, facts.name)

    def _filter_list(self, result):
        identity = self.identity.get()
        content = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                kept = []
                for line in str(block.get("text", "")).splitlines():
                    facts = guard.facts_from_list_line(line)
                    if facts is not None and (identity.owns(facts) or guard.is_security_prompt(facts)):
                        continue
                    kept.append(line)
                content.append({"type": "text", "text": "\n".join(kept)})
        return {"content": content, "isError": bool(result.get("isError"))}

    def _filter_topology(self, result):
        identity = self.identity.get()
        text = framing.text_of(result)
        try:
            data = json.loads(text)
        except ValueError:
            return result
        def allowed(window):
            facts = guard.Facts(str(window.get("app") or ""), None, int(window.get("pid") or 0))
            return not (identity.owns(facts) or guard.is_security_prompt(facts))
        for key in ("windows",):
            if isinstance(data.get(key), list):
                data[key] = [w for w in data[key] if isinstance(w, dict) and allowed(w)]
        if isinstance(data.get("target"), dict) and "pid" in data["target"] and not allowed(data["target"]):
            data.pop("target")
        return {"content": [{"type": "text", "text": json.dumps(data)}], "isError": bool(result.get("isError"))}

    # -- setup tools ------------------------------------------------------
    def setup_status(self):
        report = {
            "extension": {"name": "Open Computer Use Agent", "version": __version__,
                          "engine": f"Open Computer Use {UPSTREAM_VERSION}", "patch_revision": PATCH_REVISION},
            "computer": describe_platform(),
            "target": self.target,
            "supported": bool(self.target),
        }
        if not self.target:
            report["message"] = UNSUPPORTED.format(detail=describe_platform())
            return report
        if is_serve_daemon(self.environ):
            report["supported"] = False
            report["message"] = consent.SERVE_REFUSAL
            return report
        try:
            self.payload.verify()
            report["helper"] = {"verified": True, "path": str(self.payload.executable)}
        except PayloadError as error:
            report["supported"] = False
            report["helper"] = {"verified": False}
            report["message"] = str(error)
            return report
        if self.target.startswith("linux-"):
            report["linux"] = linux_checks(self.target, self.environ)
            if report["linux"]["missing_session_variables"] or report["linux"]["missing_packages"]:
                report["supported"] = False
        try:
            code, out, err = run_cli(self.payload, self.environ, ["doctor", "--json"], timeout=30)
            try:
                doctor = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
            except ValueError:
                doctor = {"raw": out.strip()[:500]}
            if err.strip():
                doctor["stderr"] = err.strip()[:500]
            doctor["exit_code"] = code
        except (PayloadError, OSError, subprocess.SubprocessError) as error:
            doctor = {"error": str(error)}
        report["doctor"] = doctor
        if self.target.startswith("darwin-"):
            report["macos"] = {
                "permissions_belong_to": APP_NAME,
                "accessibility": doctor.get("accessibility"),
                "screen_recording": doctor.get("screen_recording"),
                "note": f"Grant Accessibility and Screen Recording to \"{APP_NAME}\", not to Biorouter. "
                        "open_permission_setup opens its permission window.",
            }
        state = doctor.get("state")
        report["ready"] = state == "ready" and report["supported"]
        report["message"] = doctor.get("message") or report.get("message") or ""
        return report

    def open_permission_setup(self):
        if not self.target or not self.target.startswith("darwin-"):
            return framing.error_result("open_permission_setup is for macOS. On Windows and Linux the helper "
                                        "needs no permission grant; run setup_status instead.")
        try:
            code, out, err = run_cli(self.payload, self.environ, ["doctor"], timeout=20)
        except subprocess.TimeoutExpired:
            return framing.text_result(f"The {APP_NAME} permission window is open. Grant Accessibility and "
                                       f"Screen Recording to \"{APP_NAME}\" there, then run setup_status.")
        except (PayloadError, OSError) as error:
            return framing.error_result(str(error))
        message = (out.strip() or err.strip())[:1500]
        return framing.text_result(f"Opened the {APP_NAME} permission window. Grant Accessibility and Screen "
                                   f"Recording to \"{APP_NAME}\" (not to Biorouter), then run setup_status.\n\n"
                                   + message)


LINUX_PROBE = (
    "import gi\n"
    "missing = []\n"
    "for name, version in (('Atspi', '2.0'), ('Gtk', '3.0'), ('Gdk', '3.0')):\n"
    "    try:\n"
    "        gi.require_version(name, version)\n"
    "        __import__('gi.repository', fromlist=[name])\n"
    "        getattr(__import__('gi.repository', fromlist=[name]), name)\n"
    "    except Exception:\n"
    "        missing.append(name)\n"
    "print(','.join(missing))\n"
)


def linux_checks(target_name, environ):
    from .payload import helper_environment

    result = {"session_type": environ.get("XDG_SESSION_TYPE", ""),
              "missing_session_variables": missing_session_variables(target_name, environ),
              "missing_packages": []}
    if not os.path.exists("/usr/bin/python3"):
        result["missing_packages"] = ["python3"]
    else:
        env = helper_environment(target_name, environ, "/tmp", "probe")
        try:
            completed = subprocess.run(["/usr/bin/python3", "-I", "-c", LINUX_PROBE], capture_output=True,
                                       text=True, timeout=20, env=env)
            if completed.returncode != 0:
                result["missing_packages"] = ["PyGObject (python3-gi or python3-gobject)"]
            else:
                names = [n for n in completed.stdout.strip().split(",") if n]
                labels = {"Atspi": "AT-SPI typelib (gir1.2-atspi-2.0 or at-spi2-core)",
                          "Gtk": "GTK 3 typelib (gir1.2-gtk-3.0 or gtk3)", "Gdk": "GDK 3 typelib (gir1.2-gtk-3.0 or gtk3)"}
                result["missing_packages"] = [labels[n] for n in names if n in labels]
        except (OSError, subprocess.SubprocessError) as error:
            result["missing_packages"] = [f"could not run /usr/bin/python3: {error}"]
    if result["session_type"].lower() == "wayland":
        result["note"] = ("This is a Wayland session: pointer input and capture are not supported. Use an X11 "
                          "session for clicks by position, drags and window capture.")
    if result["missing_packages"]:
        result["install"] = {
            "apt": "sudo apt install python3 python3-gi gir1.2-atspi-2.0 gir1.2-gtk-3.0 at-spi2-core",
            "dnf": "sudo dnf install python3 python3-gobject at-spi2-core gtk3",
            "zypper": "sudo zypper install python3 python3-gobject typelib-1_0-Atspi-2_0 typelib-1_0-Gtk-3_0 at-spi2-core",
            "pacman": "sudo pacman -S python python-gobject at-spi2-core gtk3",
        }
    return result


def _agent_paths(directories):
    from . import APP_NAME as name

    return [os.path.realpath(os.path.join(str(d), f"{name}.app", "Contents", "MacOS", "ocu")) for d in directories]


def _real_exe(row):
    """A process's executable as a real path. LaunchServices reports the agent by its resolved
    path (`/private/var/...` for a folder reached as `/var/...`), so both sides are resolved."""
    if os.path.basename(row.exe) != "ocu":
        return row.exe
    try:
        return os.path.realpath(row.exe)
    except (OSError, ValueError):
        return row.exe


def end_app_agents(directories, rows=None):
    """End the macOS app agents started from these payload folders.

    The agent is launched through LaunchServices, so its parent is launchd, not this wrapper.
    Only a process whose executable is exactly one of these paths and whose parent is launchd is
    ended: the same exact path rule Biorouter's own retired helper cleanup uses."""
    if sys.platform != "darwin":
        return []
    paths = set(_agent_paths(directories))
    ended = []
    for row in rows if rows is not None else procs.table():
        if row.ppid == 1 and _real_exe(row) in paths:
            try:
                os.kill(row.pid, signal.SIGTERM)
                ended.append(row.pid)
            except (ProcessLookupError, PermissionError):
                pass
    return ended


def other_proxies_running(payload, rows=None):
    path = os.path.realpath(str(payload.executable))
    return any(row.ppid != 1 and _real_exe(row) == path for row in (rows if rows is not None else procs.table()))
