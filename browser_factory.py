"""Finds and launches locally installed Microsoft Edge or Google Chrome.

Playwright's bundled browsers are never used: the server has no internet access
to download them.
"""
import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)

# Added to every browser the portal starts, with a value unique to that launch, so a
# hung browser's process can be found by its command line and killed. Chromium ignores
# switches it does not know, and does not pass this one to its child processes.
MARKER_SWITCH = "--qa-portal-session"
_MARKER_RE = re.compile(r"^[0-9a-f]{32}$")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOOKUP_TIMEOUT_SEC = 30

BROWSER_LABELS = {"msedge": "Microsoft Edge", "chrome": "Google Chrome"}
_STANDARD_PATHS = {"msedge": config.DEFAULT_EDGE_PATHS, "chrome": config.DEFAULT_CHROME_PATHS}
_COMMANDS = {
    "msedge": ["msedge", "microsoft-edge", "microsoft-edge-stable"],
    "chrome": ["chrome", "google-chrome", "google-chrome-stable"],
}


class BrowserLaunchError(RuntimeError):
    """Neither Edge nor Chrome could be started."""


@dataclass
class LaunchResult:
    browser: object
    browser_used: str
    note: str
    marker: str = ""  # MARKER_SWITCH value of this browser, for kill_marked_browser


def new_marker():
    return uuid.uuid4().hex


# Matches no host. Used as the silent sign-in allowlist when the application has
# its own AD account: otherwise Edge signs in to intranet sites as the Windows
# account the portal runs as (it never asks, so http_credentials is never used)
# and the application refuses that account. Seen on prod 2026-11-21: QPID showed
# UnauthorizedAccess.aspx for the server account, Welcome for the typed one.
NO_SILENT_SIGNIN = "qa-no-silent-signin.invalid"


def launch_args(marker=None, silent_signin=True):
    """silent_signin=False: the run signs in with the application's own account."""
    marked = [f"{MARKER_SWITCH}={marker}"] if marker else []
    allowlist = config.AUTH_SERVER_ALLOWLIST if silent_signin else NO_SILENT_SIGNIN
    return marked + [
        "--start-maximized",
        "--ignore-certificate-errors",
        "--disable-gpu",
        "--no-sandbox",
        f"--auth-server-allowlist={allowlist}",
        f"--auth-negotiate-delegate-whitelist={config.AUTH_DELEGATE_ALLOWLIST}",
        # Current Chromium name for the switch above; the old name is kept for
        # older Edge/Chrome builds still found on managed desktops.
        f"--auth-negotiate-delegate-allowlist={config.AUTH_DELEGATE_ALLOWLIST}",
    ]


def candidate_paths(browser, custom_path=""):
    """Admin-configured path first, then the standard install locations."""
    paths = [custom_path] if custom_path else []
    paths += _STANDARD_PATHS[browser]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data and browser == "chrome":
        # Chrome installed per-user without admin rights.
        paths.append(os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"))
    return paths


def find_executable(browser, custom_path=""):
    for path in candidate_paths(browser, custom_path):
        if os.path.isfile(path):
            return path
    for command in _COMMANDS[browser]:
        found = shutil.which(command)
        if found:
            return found
    return None


def custom_paths_from(system_config):
    return {
        "msedge": system_config.edge_executable_path or "",
        "chrome": system_config.chrome_executable_path or "",
    }


def detect_browsers(system_config):
    """Status for the Admin 'Detect Installed Browsers' indicator."""
    custom = custom_paths_from(system_config)
    status = {}
    for browser, label in BROWSER_LABELS.items():
        path = find_executable(browser, custom[browser])
        status[browser] = {
            "label": label,
            "detected": bool(path),
            "path": path or "",
            "custom_path": custom[browser],
            # A saved path that vanished must not read as healthy just because
            # a standard location still works.
            "custom_path_valid": (not custom[browser]) or os.path.isfile(custom[browser]),
        }
    return status


def _launch_one(playwright, browser, custom_path, headless, silent_signin=True):
    """Launch via executable path when known, else by channel. Raises on failure. The
    browser object carries its marker, so that a launcher wrapping this one keeps it."""
    marker = new_marker()
    options = {"headless": headless, "args": launch_args(marker, silent_signin)}
    path = find_executable(browser, custom_path)
    launched = None
    if path:
        try:
            launched = playwright.chromium.launch(executable_path=path, **options)
        except Exception as exc:
            log.warning("Launch from %s failed, trying channel: %s", path, exc)
    if launched is None:
        launched = playwright.chromium.launch(channel=browser, **options)
    launched._qa_marker = marker
    return launched


def launch_browser(playwright, requested, custom_paths, headless, launcher=None, silent_signin=True):
    """Start the requested browser, falling back to the other so a run never
    crashes merely because one browser is missing from this machine.
    silent_signin=False when the application has its own AD account (see NO_SILENT_SIGNIN)."""
    if launcher is None:
        launcher = functools.partial(_launch_one, silent_signin=silent_signin)
    other = "msedge" if requested == "chrome" else "chrome"
    try:
        browser = launcher(playwright, requested, custom_paths.get(requested, ""), headless)
        return LaunchResult(browser, requested, "", getattr(browser, "_qa_marker", ""))
    except Exception as first_error:
        log.warning("%s unavailable: %s", requested, first_error)
        try:
            browser = launcher(playwright, other, custom_paths.get(other, ""), headless)
        except Exception as second_error:
            raise BrowserLaunchError(
                f"Could not start {BROWSER_LABELS[requested]} ({_first_line(first_error)}) "
                f"or {BROWSER_LABELS[other]} ({_first_line(second_error)}). "
                "Check the browser paths on the Admin page."
            ) from second_error
    return LaunchResult(
        browser, other,
        f"{BROWSER_LABELS[requested]} was requested but is not available on this server; "
        f"the run used {BROWSER_LABELS[other]} instead.",
        getattr(browser, "_qa_marker", ""),
    )


# --------------------------------------------------------------------------
# Killing a hung browser. Standard library only: no psutil on the server.
# --------------------------------------------------------------------------

def _windows_marked_pids(needle):
    # The needle is a fixed switch and 32 hex characters (checked by the caller), so it
    # cannot break out of the quotes. This PowerShell's own command line holds the needle
    # too, so it leaves itself out.
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and "
        f"$_.CommandLine.Contains('{needle}') }} | ForEach-Object {{ $_.ProcessId }}"
    )
    output = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=_LOOKUP_TIMEOUT_SEC, creationflags=_NO_WINDOW,
    ).stdout
    return [int(line) for line in output.split() if line.isdigit()]


def _posix_processes():
    """{pid: (parent pid, command line)} from /proc, or from ps where there is no /proc."""
    found = {}
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as handle:
                    command = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    # The name in brackets can hold spaces; the parent pid follows it.
                    parent = int(handle.read().rsplit(b")", 1)[1].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            found[int(entry)] = (parent, command)
        return found
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=_LOOKUP_TIMEOUT_SEC
    ).stdout
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            found[int(parts[0])] = (int(parts[1]), parts[2])
    return found


def marked_pids(marker):
    """Process ids whose command line carries this launch's marker. Usually one: the
    browser's main process."""
    if not _MARKER_RE.match(marker or ""):
        return []
    needle = f"{MARKER_SWITCH}={marker}"
    try:
        if os.name == "nt":
            return _windows_marked_pids(needle)
        return [pid for pid, (_parent, command) in _posix_processes().items() if needle in command]
    except Exception as exc:
        log.warning("Could not list browser processes: %s", exc)
        return []


def _kill_tree(pid):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
            timeout=_LOOKUP_TIMEOUT_SEC, creationflags=_NO_WINDOW,
        )
        return
    processes = _posix_processes()
    family, added = {pid}, True
    while added:  # children, grandchildren ...
        added = False
        for child, (parent, _command) in processes.items():
            if parent in family and child not in family:
                family.add(child)
                added = True
    for member in family:
        try:
            os.kill(member, signal.SIGKILL)
        except OSError:
            pass


def kill_marked_browser(marker):
    """Kill the process tree of the browser launched with this marker (renderers, GPU
    process and the rest). Returns the main process ids that were found. Safe to call
    from any thread: it never touches Playwright."""
    pids = marked_pids(marker)
    for pid in pids:
        try:
            _kill_tree(pid)
        except Exception as exc:
            log.warning("Could not kill browser process %s: %s", pid, exc)
    return pids


def _first_line(error):
    lines = str(error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


def new_context(browser, headless, timeout_ms, http_credentials=None):
    """Create a browser context. With no explicit credentials in AD mode the
    browser passes the Windows session ticket (SSO) on its own."""
    options = {"ignore_https_errors": True, "accept_downloads": True}
    if headless:
        options["viewport"] = {"width": 1920, "height": 1080}
    else:
        # Required for --start-maximized to take effect.
        options["no_viewport"] = True
    if http_credentials:
        options["http_credentials"] = http_credentials
    context = browser.new_context(**options)
    context.set_default_timeout(timeout_ms)
    context.set_default_navigation_timeout(max(timeout_ms, config.MIN_NAVIGATION_TIMEOUT_MS))
    return context
