"""Sequential run worker: one run at a time, one test case at a time.

Runs wait in the database with status Queued. A single daemon thread claims the
oldest, so the portal must be served by one process (app.py does this).
"""
import base64
import json
import logging
import random
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from cryptography.fernet import InvalidToken
from playwright.sync_api import sync_playwright

import config
from engine import browser_factory, maintenance, report_generator
from engine.keywords import (
    BLOCK_KEYWORDS, CALL_BLOCK, CALL_BLOCK_IF, CALL_BLOCK_IF_VISIBLE, RAW_INPUT_KEYWORDS,
    REPEAT_BLOCK, REPEAT_BLOCK_UNTIL, SOFT_KEYWORDS, TOKEN_RE, KeywordResult, StepEnv, date_token,
    parse_block_call, parse_call_block, run_keyword,
)
from engine.performance import PerformanceRecorder, view_url
from models import (
    DIAGNOSTICS_BULLET, DIAGNOSTICS_HEADING, RUN_ABORTED, RUN_ACTIVE, RUN_COMPLETED, RUN_QUEUED, RUN_RUNNING, STEP_FAIL, STEP_PASS,
    TC_BLOCKED, TC_FAIL, TC_PASS, TC_PENDING, TC_RUNNING, TestCase, TestRun, TestStepResult,
    db, get_system_config, utcnow,
)

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MASK = "********"
MIN_SECRET_LENGTH = 4
MAX_BLOCK_DEPTH = 10
DEFAULT_DIALOG_PLAN = ("accept", None)

BUILTIN_TOKENS = {
    "RANDOM_INT": lambda: str(random.randint(100000, 999999)),
    "UUID": lambda: str(uuid.uuid4()),
    "TODAY_DATE": lambda: date_token("TODAY_DATE"),
    "TODAY_DMY": lambda: date_token("TODAY_DMY"),
    "TODAY_MDY": lambda: date_token("TODAY_MDY"),
    "TIMESTAMP": lambda: datetime.now().strftime("%Y%m%d%H%M%S"),
}
TEARDOWN_PREFIX = "[Teardown] "


class UnresolvedVariable(Exception):
    def __init__(self, name):
        super().__init__(f"Variable {{{name}}} has no value")
        self.name = name


class Blocked(Exception):
    """The test case cannot start; it is recorded as Skipped/Blocked."""


class RuntimeContext:
    """Shared memory for one run. Stored variables chain between test cases.
    Application credentials are available as tokens but never persisted."""

    def __init__(self, username="", password="", secrets=None):
        self.variables = {}
        self._credentials = {}
        self._password = password
        # The application's named secret values, used in steps as {NAME}.
        self.secrets = dict(secrets or {})
        # Text hidden in everything written to results: the stored values, and anything a
        # step typed into a password field.
        self._hidden = set()
        if username:
            self._credentials["AUTH_USERNAME"] = username
        if password:
            self._credentials["AUTH_PASSWORD"] = password
        for value in (password, *self.secrets.values()):
            self.hide(value)

    def hide(self, value):
        """Mask this text in results from now on. Very short text is left alone: hiding
        every "1" would wreck the results and protect nothing."""
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._hidden.add(value)

    def stored_secrets(self):
        """The values a test must not be able to reveal: the service password and the
        application's secret values."""
        return [value for value in (self._password, *self.secrets.values())
                if value and len(value) >= MIN_SECRET_LENGTH]

    def known_names(self):
        return set(self.variables) | set(BUILTIN_TOKENS) | set(self._credentials) | set(self.secrets)

    def _value_of(self, match):
        name = match.group(1)
        if name in BUILTIN_TOKENS:
            return BUILTIN_TOKENS[name]()
        dated = date_token(name)  # {TODAY_DATE+7} and the like
        if dated is not None:
            return dated
        if name in self._credentials:
            return self._credentials[name]
        if name in self.secrets:
            return self.secrets[name]
        if name in self.variables:
            return self.variables[name]
        raise UnresolvedVariable(name)

    def resolve(self, text):
        return TOKEN_RE.sub(self._value_of, text) if text else text

    def mask(self, text):
        """Hide passwords and secret values in anything written to results."""
        if text:
            for value in sorted(self._hidden, key=len, reverse=True):  # longest first
                text = text.replace(value, _MASK)
        return text

    def public_variables(self):
        return {name: self.mask(value) for name, value in self.variables.items()}


class SecretMisuse(Exception):
    """A step would have shown, moved or stored a password or secret value."""


# The only keywords that may be given a stored password or secret value.
SECRET_TYPING_KEYWORDS = {"FILL", "FILL_IF_VISIBLE", "TYPE_SEQUENTIALLY"}
_IS_PASSWORD_FIELD = "el => el.tagName === 'INPUT' && (el.getAttribute('type') || '').toLowerCase() === 'password'"
# Marks every field showing a secret, so a screenshot can cover it. A page can turn a
# password field into plain text ("show password"), so the value is checked, not the type.
_TAG_SECRET_FIELDS = """secrets => {
  let found = 0;
  document.querySelectorAll('input, textarea').forEach((el) => {
    const shown = (el.getAttribute('type') || '').toLowerCase() !== 'password'
      && secrets.some((secret) => String(el.value || '').includes(secret));
    if (shown) { el.setAttribute('data-qa-secret', ''); found += 1; }
    else { el.removeAttribute('data-qa-secret'); }
  });
  return found;
}"""


class SecretGuard:
    """Keeps a test from revealing the application's stored password or secret values.
    They may only be typed into a password field, on the application's own host; they
    cannot be put in an address, a locator or any other step, read back from a password
    field, stored in a variable, or photographed. A test author still decides what the
    application does with a password once it is typed, so these accounts should have no
    more rights than testing needs."""

    def __init__(self, context, base_url):
        self.context = context
        self.hosts = {(urlsplit(base_url).hostname or "").lower()} | set(config.AUTH_FILL_HOSTS)
        self.hosts.discard("")

    def holds(self, text):
        return bool(text) and any(secret in text for secret in self.context.stored_secrets())

    def check_step(self, keyword, locator_value, input_data):
        """Before a step runs, with its {TOKEN}s already replaced."""
        if self.holds(locator_value):
            raise SecretMisuse("A password or secret value cannot be used in a locator.")
        if self.holds(input_data) and keyword not in SECRET_TYPING_KEYWORDS and keyword != CALL_BLOCK:
            raise SecretMisuse(
                f"A password or secret value cannot be used in {keyword}. It can only be typed "
                "into a password field with FILL, FILL_IF_VISIBLE or TYPE_SEQUENTIALLY."
            )

    def before_typing(self, env, target, data):
        """Called by the typing keywords with the element they are about to fill."""
        is_password = bool(target.evaluate(_IS_PASSWORD_FIELD, timeout=env.timeout_ms))
        if not self.holds(data):
            if is_password:
                # A password typed as plain text in the step: at least keep it out of
                # results and reports. A secret value keeps it out of the step as well.
                self.context.hide(data)
            return
        if not is_password:
            raise SecretMisuse(
                "A password or secret value can only be typed into a password field, and this "
                "field is not one. Typed anywhere else it would be readable on the page."
            )
        host = (urlsplit(env.page.url).hostname or "").lower()
        if host not in self.hosts:
            raise SecretMisuse(
                f"A password or secret value can only be typed on the application's own host "
                f"({', '.join(sorted(self.hosts)) or 'none set'}), but this page is on '{host}'. "
                "An Admin can allow a separate sign-in host with QA_AUTH_FILL_HOSTS."
            )

    def before_reading(self, target):
        if self.context.stored_secrets() and target.evaluate(_IS_PASSWORD_FIELD):
            raise SecretMisuse("The value of a password field cannot be stored in a variable.")

    def check_stored(self, value):
        if self.holds(value):
            raise SecretMisuse("This step would store a password or secret value in a variable.")

    def screenshot_masks(self, page):
        """Locators of the fields that show a secret, for the screenshot to cover."""
        secrets = self.context.stored_secrets()
        masks = []
        if not secrets:
            return masks
        for frame in page.frames:
            try:
                if frame.evaluate(_TAG_SECRET_FIELDS, secrets):
                    masks.append(frame.locator("[data-qa-secret]"))
            except Exception:  # a frame that is navigating or gone has nothing to show
                continue
        return masks


@dataclass
class PlannedStep:
    """A step after reusable blocks are expanded in place."""
    keyword: str
    locator_type: Optional[str]
    locator_value: Optional[str]
    input_data: Optional[str]
    store_as: Optional[str]
    description: str
    # CALL_BLOCK_IF_VISIBLE, CALL_BLOCK_IF and REPEAT_BLOCK_UNTIL: how many steps after it
    # are left out when it says so.
    guarded_steps: int = 0
    loop: Optional[tuple] = None  # REPEAT_BLOCK_UNTIL check: (iterations before it, maximum).
    sets: tuple = ()  # CALL_BLOCK with arguments: the variables it sets for the block.


def _block_steps(step, test_case, stack):
    """The expanded steps of the block a step calls, and how many times to run them."""
    try:
        name, count = parse_block_call(step.action_keyword, step.input_data)
    except ValueError as exc:
        raise Blocked(f"Step {step.step_order}: {exc}") from None
    block = TestCase.query.filter_by(
        app_id=test_case.app_id, name=name, is_reusable_block=True
    ).first()
    if not block:
        raise Blocked(
            f"Step {step.step_order} calls reusable block '{name}', "
            "which does not exist in this application."
        )
    return expand_steps(block, stack + (test_case.id,)), count


def expand_steps(test_case, stack=()):
    """Flatten a test case, replacing block keywords with the block's own steps. A
    conditional block stays in the list, led by a step that can skip it at run time. A
    REPEAT_BLOCK_UNTIL loop becomes a check, then the block and a check again for each
    iteration up to the maximum; the first check that is met skips the rest."""
    if test_case.id in stack:
        raise Blocked(f"Reusable block '{test_case.name}' calls itself.")
    if len(stack) >= MAX_BLOCK_DEPTH:
        raise Blocked(f"Reusable blocks are nested more than {MAX_BLOCK_DEPTH} levels deep.")
    planned = []
    prefix = f"[{test_case.name}] " if stack else ""
    for step in test_case.steps:
        keyword = step.action_keyword
        if keyword not in BLOCK_KEYWORDS:
            planned.append(PlannedStep(
                keyword, step.locator_type, step.locator_value, step.input_data,
                step.store_variable_name, prefix + (step.description or ""),
            ))
            continue
        inner, count = _block_steps(step, test_case, stack)
        label = prefix + (step.description or "")
        span = len(inner) + 1  # One iteration of a loop: the block and the check after it.
        added = span * count + 1 if keyword == REPEAT_BLOCK_UNTIL else len(inner) * count
        # Checked before copying: nested repeats multiply, so they could fill the memory.
        if len(planned) + added > config.MAX_EXPANDED_STEPS:
            raise Blocked(
                f"Reusable blocks expand this test case to more than {config.MAX_EXPANDED_STEPS} "
                "steps. Lower the REPEAT_BLOCK counts."
            )
        if keyword in (CALL_BLOCK_IF_VISIBLE, CALL_BLOCK_IF):
            planned.append(PlannedStep(
                keyword, step.locator_type, step.locator_value,
                step.input_data if keyword == CALL_BLOCK_IF else None, None, label,
                guarded_steps=len(inner),
            ))
        if keyword == CALL_BLOCK:
            # Already read once by _block_steps, so this cannot fail here.
            arguments = parse_call_block(step.input_data)[1]
            if arguments:
                # A step of its own, so the results show what the block was given.
                planned.append(PlannedStep(
                    keyword, None, None, step.input_data, None, label,
                    sets=tuple(key for key, _value in arguments),
                ))
        if keyword == REPEAT_BLOCK:
            for turn in range(1, count + 1):
                planned.extend(
                    replace(item, description=f"[{turn}/{count}] {item.description}") for item in inner
                )
        elif keyword == REPEAT_BLOCK_UNTIL:
            for done in range(count + 1):
                planned.append(PlannedStep(
                    keyword, step.locator_type, step.locator_value, step.input_data, None,
                    f"[check {done}/{count}] {label}".rstrip(),
                    guarded_steps=(count - done) * span, loop=(done, count),
                ))
                if done < count:
                    planned.extend(
                        replace(item, description=f"[{done + 1}/{count}] {item.description}") for item in inner
                    )
        else:
            planned.extend(inner)
    return planned


def _download_matches(pattern, name):
    """Part of the name, or the whole name with * as a wildcard. Blank matches any."""
    if "*" in pattern:
        return re.fullmatch(".*".join(map(re.escape, pattern.split("*"))), name) is not None
    return pattern in name


def _check_case_variables(definitions, context, available):
    """The test case's own variables, set before its first step. A value may use tokens,
    variables of earlier test cases and the variables listed above it."""
    for item in definitions:
        name = item["name"]
        for token in TOKEN_RE.findall(item["value"]):
            if token in available:
                continue
            try:
                if date_token(token) is not None:
                    continue
            except ValueError as exc:
                raise Blocked(f"Variable {name}: {exc}") from None
            raise Blocked(
                f"Variable {name} uses {{{token}}}, which no earlier test case or variable "
                "above it has populated."
            )
        if name in context.secrets:
            raise Blocked(
                f"Variable {name} would overwrite {{{name}}}, which is a secret value of this "
                "application. Use another variable name."
            )
        available.add(name)


def set_case_variables(definitions, context, run):
    """Give the test case's variables their values. Tokens are filled in once here, so
    {RANDOM_INT} in a value is the same number in every step that uses the variable,
    and in the test cases that run after this one."""
    for item in definitions:
        name = item["name"]
        if item.get("keep") and name in context.variables:
            continue  # an earlier test case of this run already set it
        value = context.resolve(item["value"])
        if any(secret in value for secret in context.stored_secrets()):
            raise Blocked(
                f"Variable {name} would hold a password or secret value. Use the token in "
                "the step that types it instead."
            )
        context.variables[name] = value
    if definitions:
        run.runtime_variables_json = json.dumps(context.public_variables())


def check_variables(steps, context, definitions=()):
    """Fail fast: every {TOKEN} must already exist, be one of the test case's own
    variables (definitions) or be stored by an earlier step."""
    available = context.known_names()
    _check_case_variables(definitions, context, available)
    for number, step in enumerate(steps, start=1):
        for field in (step.locator_value, step.input_data):
            for name in TOKEN_RE.findall(field or ""):
                if name in available:
                    continue
                try:
                    if date_token(name) is not None:  # {TODAY_DATE+7} and the like
                        continue
                except ValueError as exc:
                    raise Blocked(f"Step {number} ({step.keyword}): {exc}") from None
                raise Blocked(
                    f"Step {number} ({step.keyword}) uses {{{name}}}, which no earlier "
                    "step or test case has populated."
                )
        for name in (step.store_as, *step.sets):
            if name and name in context.secrets:
                raise Blocked(
                    f"Step {number} ({step.keyword}) would overwrite {{{name}}}, which is a secret "
                    "value of this application. Use another variable name."
                )
        if step.store_as:
            available.add(step.store_as)
        available.update(step.sets)


def clean_error(exc):
    return _ANSI_RE.sub("", str(exc)).strip() or type(exc).__name__


class BrowserSession:
    """Browser plus current context. Passing test cases share the context, so a
    login carries forward; a failure resets it so the next case starts clean."""

    def __init__(self, launched, headless, timeout_ms, credentials, recorder):
        self.browser = launched.browser
        self.marker = getattr(launched, "marker", "")
        # JS errors, console errors and failed HTTP calls seen during the current step,
        # newest last, added to the step's error when it fails.
        self.recent_diagnostics = []
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.credentials = credentials
        self.recorder = recorder
        self.context = None
        self.page = None
        # Windows the steps have worked in, oldest first. SWITCH_TO_WINDOW numbers them.
        self.windows = []
        # The same windows, most recently used last: where CLOSE_WINDOW goes back to.
        self.recent = []
        self.visited = set()
        self.active_frame = None
        self._frame_document = None
        self.last_dialog_message = ""
        self.next_dialog_plan = DEFAULT_DIALOG_PLAN
        self.downloads = []
        self._download_dir = None

    def _work_in(self, page):
        self.page = page
        self.leave_frame()  # A frame belongs to the window it was entered in.
        if page in self.recent:
            self.recent.remove(page)
        self.recent.append(page)
        # Otherwise a visible run shows a different tab from the one being tested.
        page.bring_to_front()

    def ensure_page(self):
        if self.page is None or self.page.is_closed():
            # A pop-up can close itself, such as a picker: carry on where it came from.
            self.windows = [page for page in self.windows if not page.is_closed()]
            self.recent = [page for page in self.recent if not page.is_closed()]
            if self.recent:
                self._work_in(self.recent[-1])
                return self.page
            if self.context is None:
                self.context = browser_factory.new_context(
                    self.browser, self.headless, self.timeout_ms, self.credentials
                )
                # Set on the context so that pop-ups are covered too.
                self.context.on("dialog", self._on_dialog)
                self.context.on("download", self._on_download)
                # weberror is the context-wide form of each page's pageerror event.
                self.context.on("weberror", self._on_web_error)
                self.context.on("console", self._on_console)
                self.context.on("response", self._on_response)
                self.context.on("requestfailed", self._on_request_failed)
                self.recorder.attach(self.context)
            self.page = self.context.new_page()
            self.recorder.mark_main(self.page)
            self.windows.append(self.page)
            self.recent.append(self.page)
            self.visited.add(self.page)
        return self.page

    # ---- Failure diagnostics. Listeners must never raise into Playwright. ----

    def _note(self, text):
        lines = str(text or "").strip().splitlines()
        if not lines:
            return
        self.recent_diagnostics.append(lines[0][:300])
        del self.recent_diagnostics[:-config.MAX_STEP_DIAGNOSTICS]

    def _on_web_error(self, web_error):
        try:
            error = web_error.error
            self._note(f"JS Error: {getattr(error, 'message', None) or error}")
        except Exception:
            pass

    def _on_console(self, message):
        try:
            # The browser also logs each failed HTTP call; _on_response says it better.
            if message.type == "error" and not message.text.startswith("Failed to load resource"):
                self._note(f"Console Error: {message.text}")
        except Exception:
            pass

    def _on_response(self, response):
        try:
            if response.status >= 400:
                # view_url: the query or fragment can hold a sign-in token.
                self._note(f"HTTP {response.status} on {response.request.method} {view_url(response.url)}")
        except Exception:
            pass

    def _on_request_failed(self, request):
        try:
            reason = request.failure or ""
            if "ERR_ABORTED" not in reason:  # a page left before it loaded: not a fault
                self._note(f"Request failed: {request.method} {view_url(request.url)} ({reason})")
        except Exception:
            pass

    def clear_diagnostics(self):
        self.recent_diagnostics = []

    def take_diagnostics(self):
        found, self.recent_diagnostics = self.recent_diagnostics, []
        return found

    # ---- Dialogs, downloads, windows and frames ----

    def _on_dialog(self, dialog):
        """Accept by default: legacy .NET pages guard postbacks with confirm(), and
        Playwright's own default would dismiss it and silently cancel the action."""
        self.last_dialog_message = dialog.message
        action, text = self.next_dialog_plan
        self.next_dialog_plan = DEFAULT_DIALOG_PLAN  # A plan answers one dialog only.
        try:
            if action == "dismiss":
                dialog.dismiss()
            elif text is None:
                dialog.accept()
            else:
                dialog.accept(text)
        except Exception as exc:  # The page closed first; the next step reports that.
            log.warning("Answering a browser dialog failed: %s", exc)

    def prepare_dialog(self, plan):
        self.next_dialog_plan = plan
        # Otherwise VERIFY_DIALOG_TEXT could pass on the message of an earlier dialog.
        self.last_dialog_message = ""

    def _on_download(self, download):
        """Each event runs in its own greenlet, so the wait in save_as holds up neither
        the running step nor other events. Saved under a generated name, because the
        suggested one comes from the application's server."""
        entry = {"name": download.suggested_filename, "size": None, "error": None, "verified": False}
        self.downloads.append(entry)
        try:
            if self._download_dir is None:
                config.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
                self._download_dir = Path(tempfile.mkdtemp(prefix="run_", dir=config.DOWNLOADS_DIR))
            path = self._download_dir / uuid.uuid4().hex
            download.save_as(path)
            entry["size"] = path.stat().st_size
        except Exception as exc:
            entry["error"] = clean_error(exc)

    def wait_for_download(self, pattern, timeout_ms):
        """Takes the oldest matching download no earlier step has checked, so a second
        VERIFY_DOWNLOAD needs a second file. Returns (file name, size in bytes)."""
        wanted = f" matching '{pattern}'" if pattern else ""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            page = self.ensure_page()
            unfinished = []
            for entry in self.downloads:
                if entry["verified"] or not _download_matches(pattern, entry["name"]):
                    continue
                if entry["error"]:
                    entry["verified"] = True
                    raise AssertionError(f"Download of {entry['name']} failed: {entry['error']}")
                if entry["size"] is None:
                    unfinished.append(entry["name"])
                    continue
                entry["verified"] = True
                if entry["size"] <= 0:
                    raise AssertionError(f"Downloaded file {entry['name']} is empty (0 bytes).")
                return entry["name"], entry["size"]
            if time.monotonic() >= deadline:
                if unfinished:
                    raise AssertionError(f"Download of {unfinished[0]} did not finish within {timeout_ms} ms.")
                others = [entry["name"] for entry in self.downloads if not entry["verified"]]
                seen = f" Files not checked yet: {', '.join(others)}." if others else ""
                raise AssertionError(f"No download{wanted} within {timeout_ms} ms.{seen}")
            page.wait_for_timeout(100)

    def enter_frame(self, frame):
        self.active_frame = frame
        self._frame_document = self.page.evaluate("performance.timeOrigin")

    def leave_frame(self):
        self.active_frame = None
        self._frame_document = None

    def current_frame(self):
        """The frame to search in, or None once the page has loaded a new document. The
        document's start time tells these apart exactly. A navigation event would not:
        an Angular or React route change sends one too, and keeps the frame on screen."""
        if self.active_frame is None:
            return None
        try:
            unchanged = self.page.evaluate("performance.timeOrigin") == self._frame_document
        except Exception:  # The old document was destroyed while asking.
            unchanged = False
        if not unchanged:
            self.leave_frame()
        return self.active_frame

    def open_windows(self):
        """Every window open now, so that CLICK_AND_SWITCH_WINDOW can tell which one its
        click opened."""
        self.ensure_page()
        self._await_known_pages()
        return set(self.context.pages)

    def _await_known_pages(self, timeout_ms=2000):
        """A tab an earlier step opened reaches context.pages only once Playwright has
        set it up, which can be after the next step starts. Ask the browser how many tabs
        this context really has, and wait until Playwright knows them all, so that such a
        tab is not later taken for the one a click opened."""
        try:
            info = self.context.new_cdp_session(self.page)
            try:
                context_id = info.send("Target.getTargetInfo")["targetInfo"]["browserContextId"]
            finally:
                info.detach()
            browser = self.browser.new_browser_cdp_session()
            try:
                targets = browser.send("Target.getTargets")["targetInfos"]
            finally:
                browser.detach()
        except Exception as exc:  # not essential: without it the race is only more likely
            log.debug("Could not count the browser's tabs: %s", exc)
            return
        expected = sum(
            1 for target in targets
            if target.get("type") == "page" and target.get("browserContextId") == context_id
        )
        deadline = time.monotonic() + timeout_ms / 1000
        while len(self.context.pages) < expected and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)

    def _find_new_window(self, match, ignore):
        """Newest open window the steps have not been in, optionally by address or title."""
        for page in reversed(self.context.pages):
            if page in self.visited or page in ignore or page.is_closed():
                continue
            if not match or match in page.url:
                return page
            try:
                if match in page.title():
                    return page
            except Exception:  # It is navigating; the caller looks again.
                continue
        return None

    def switch_to_new_window(self, match, timeout_ms, ignore=frozenset()):
        """ignore: windows that do not count as new, such as those open before a click."""
        wanted = f" with '{match}' in its address or title" if match else ""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            current = self.ensure_page()
            # The click that opens a window can return before the window exists.
            page = self._find_new_window(match, ignore)
            if page is not None:
                break
            if time.monotonic() >= deadline:
                earlier = " or was open before the click" if ignore else ""
                raise AssertionError(
                    f"No new window{wanted} opened within {timeout_ms} ms. "
                    f"A window the test has already switched to{earlier} does not count."
                )
            current.wait_for_timeout(100)
        self.windows.append(page)
        self.visited.add(page)
        self._work_in(page)
        page.wait_for_load_state("load")
        return page

    def _find_window(self, match):
        """An open window by address or title, oldest first. The current window is the
        last choice, so that text matching it too still moves somewhere."""
        current = self.page
        for page in sorted(self.context.pages, key=lambda candidate: candidate is current):
            if page.is_closed():
                continue
            if match in page.url:
                return page
            try:
                if match in page.title():
                    return page
            except Exception:  # It is navigating; the caller looks again.
                continue
        return None

    def switch_to_window(self, wanted, timeout_ms):
        """Move to any open window and leave the others open: 'main' or a number counts
        the windows the steps have worked in, and anything else is part of an address
        or title. Only text waits, because a number cannot refer to a window that the
        steps have not been in yet."""
        current = self.ensure_page()
        self.windows = [page for page in self.windows if not page.is_closed()]
        if wanted.lower() == "main" or wanted.isdecimal():
            index = 0 if wanted.lower() == "main" else int(wanted)
            if index >= len(self.windows):
                raise AssertionError(
                    f"There is no window {index}. {len(self.windows)} window(s) are open, numbered "
                    "from 0 in the order the test first moved to them."
                )
            page = self.windows[index]
        else:
            deadline = time.monotonic() + timeout_ms / 1000
            while (page := self._find_window(wanted)) is None:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"No open window with '{wanted}' in its address or title within {timeout_ms} ms."
                    )
                current.wait_for_timeout(100)
            if page not in self.windows:
                self.windows.append(page)
                self.visited.add(page)
        self._work_in(page)
        page.wait_for_load_state("load")
        return page

    def close_window(self):
        current = self.ensure_page()
        others = [page for page in self.recent if page is not current and not page.is_closed()]
        if not others:
            raise AssertionError(
                "There is no earlier window to return to, so this window was left open. "
                "Use SWITCH_TO_NEW_WINDOW before closing."
            )
        current.close()
        self.windows = [page for page in self.windows if page is not current and not page.is_closed()]
        self.recent = others
        self._work_in(others[-1])
        return self.page

    def settle(self):
        if self.context is not None:
            self.recorder.settle(self.context)

    def force_kill(self, reason):
        """Kill this browser's process tree. Called from a timer thread, so it only
        uses the operating system, never Playwright, which is not thread-safe."""
        if not self.marker:
            log.warning("Cannot force-kill the browser (%s): its process is not known", reason)
            return
        log.warning("Force-killing the browser process tree: %s", reason)
        pids = browser_factory.kill_marked_browser(self.marker)
        log.warning("Killed browser process(es) %s", ", ".join(map(str, pids)) or "none found")

    def _bounded(self, action, what):
        """Run a close on this thread (Playwright objects belong to it), with a timer
        that kills the browser if the close hangs. A killed browser ends the close."""
        limit = config.BROWSER_CLOSE_TIMEOUT_SEC
        timer = threading.Timer(
            limit, self.force_kill, args=(f"closing the {what} took longer than {limit} seconds",)
        )
        timer.daemon = True
        timer.start()
        try:
            action()
        except Exception as exc:
            log.warning("Closing the %s failed: %s", what, exc)
        finally:
            timer.cancel()

    def reset(self):
        if self.context is not None:
            self._bounded(self.context.close, "browser context")
        self.context = None
        self.page = None
        self.windows = []
        self.recent = []
        self.visited = set()
        self.leave_frame()
        self.last_dialog_message = ""
        self.next_dialog_plan = DEFAULT_DIALOG_PLAN
        self.downloads = []
        self.recent_diagnostics = []
        if self._download_dir is not None:
            shutil.rmtree(self._download_dir, ignore_errors=True)
            self._download_dir = None

    def close(self):
        self.reset()
        self._bounded(self.browser.close, "browser")


def _capture_screenshot(session, guard=None):
    try:
        options = {"full_page": True, "timeout": config.SCREENSHOT_TIMEOUT_MS}
        masks = guard.screenshot_masks(session.page) if guard is not None else []
        if masks:
            options["mask"] = masks
        data = session.page.screenshot(**options)
        return base64.b64encode(data).decode("ascii")
    except Exception as exc:
        log.warning("Failure screenshot not captured: %s", exc)
        return None


def _abort_requested(run_id):
    requested = db.session.execute(
        db.select(TestRun.abort_requested).where(TestRun.id == run_id)
    ).scalar()
    db.session.commit()
    return bool(requested)


ABORT_USER = "user"
ABORT_WATCHDOG = "watchdog"


class RunGuard:
    """Called before every step: true once the run must stop, because a user asked or
    because it has run longer than MAX_RUN_DURATION_SEC. Once true it stays true, and
    reason says which. A limit of 0 or less turns the watchdog off."""

    def __init__(self, run_id, limit_sec=None):
        self.run_id = run_id
        self.limit_sec = config.MAX_RUN_DURATION_SEC if limit_sec is None else limit_sec
        self.started = time.monotonic()
        self.reason = None

    def expired(self):
        return self.limit_sec > 0 and time.monotonic() - self.started > self.limit_sec

    def __call__(self):
        if self.reason is None:
            if _abort_requested(self.run_id):
                self.reason = ABORT_USER
            elif self.expired():
                self.reason = ABORT_WATCHDOG
        return self.reason is not None

    def stopped_before(self, number, phase=""):
        if self.reason == ABORT_WATCHDOG:
            return f"Run stopped by the watchdog before {phase}step {number}."
        return f"Run aborted before {phase}step {number}."

    def watchdog_note(self):
        minutes = f"{self.limit_sec / 60:g}"
        return (
            f"Run exceeded maximum allowed duration of {minutes} minutes and was "
            "terminated by the watchdog."
        )


def _start_hard_stop(guard, holder):
    """A step stuck inside a hung browser never reaches the next guard check, so kill
    the browser a grace period after the limit. The stuck step then fails at once."""
    if guard.limit_sec <= 0:
        return None

    def hard_stop():
        session = holder.get("session")
        if session is not None:
            session.force_kill(f"run {guard.run_id} is past its maximum duration and a step is stuck")

    timer = threading.Timer(guard.limit_sec + config.WATCHDOG_KILL_GRACE_SEC, hard_stop)
    timer.daemon = True
    timer.start()
    return timer


def _refresh_counts(run):
    results = run.case_results
    run.passed_tests = sum(r.status == TC_PASS for r in results)
    run.failed_tests = sum(r.status == TC_FAIL for r in results)
    run.skipped_tests = sum(r.status == TC_BLOCKED for r in results)


def _block(result, reason):
    result.status = TC_BLOCKED
    result.error_message = reason
    result.current_step = None
    result.duration_sec = 0.0


def _credentials_for(application, run):
    """Returns (runtime username, runtime password, Playwright http_credentials)."""
    if application.auth_type == "None":
        return "", "", None
    username = application.auth_username or ""
    try:
        password = application.get_password()
    except InvalidToken:
        run.add_note(
            "The stored application password could not be decrypted (credentials key "
            "changed). Re-enter it on the application; continuing without it."
        )
        password = ""
    http_credentials = None
    if application.auth_type == "OnPrem_AD" and username and password:
        domain = (application.ad_domain or "").strip()
        http_credentials = {
            "username": f"{domain}\\{username}" if domain else username,
            "password": password,
        }
    return username, password, http_credentials


def _secret_values(application, run):
    """{name: value} of the application's secret values. One that cannot be decrypted
    is left out, so a step using it is reported as having no value."""
    values = {}
    for secret in application.secrets:
        try:
            values[secret.name] = secret.get_value()
        except InvalidToken:
            run.add_note(
                f"The secret value {secret.name} could not be decrypted (credentials key "
                "changed). Re-enter it on the application."
            )
    return values


def _teardown_steps(case):
    """The expanded steps of the test case's teardown block, labelled [Teardown]."""
    block = case.teardown_block
    if block is None:
        return []
    steps = expand_steps(block, (case.id,))
    return [replace(item, description=TEARDOWN_PREFIX + item.description) for item in steps]


def _set_block_arguments(step, context, run):
    """CALL_BLOCK | KEY=VALUE: read before tokens are replaced, so a value cannot add a
    separator, then each value's own tokens are replaced."""
    shown = []
    for key, value in parse_call_block(step.input_data)[1]:
        context.variables[key] = context.resolve(value)
        shown.append(f"{key}={context.variables[key]}")
    run.runtime_variables_json = json.dumps(context.public_variables())
    return KeywordResult("set " + ", ".join(shown))


@dataclass
class _Phase:
    """One list of steps run in order: the test case's own, or its teardown."""
    steps: list
    first_number: int
    total: int
    label: str = ""  # "teardown " in messages about the teardown
    stopped: str = ""  # "", "failed" (a step that is not soft failed) or "aborted"
    stopped_at: int = 0


def _run_phase(phase, result, session, context, env, guard, failures, earlier=()):
    """Run the steps of a phase. Failures are added to failures, and error_message is
    earlier messages plus these. Stops at the first failure that is not soft, or before
    a step once the guard says the run must stop."""
    skip_through = 0
    for offset, step in enumerate(phase.steps):
        number = phase.first_number + offset
        if number <= skip_through:
            continue
        if guard():
            phase.stopped, phase.stopped_at = "aborted", number
            return phase

        label = step.description or step.keyword
        result.current_step = f"Step {number} of {phase.total}: {context.mask(label)}"[:300]
        db.session.commit()
        record = TestStepResult(
            tc_result_id=result.id, step_order=number, action_keyword=step.keyword,
            description=step.description or None, status=STEP_PASS,
        )
        session.clear_diagnostics()
        try:
            # The last step may have switched windows, or closed the one it was in.
            env.page = session.ensure_page()
            env.active_frame = session.current_frame()
            session.recorder.mark_step(result.id, number, step.keyword)
            env.loop = step.loop
            if step.keyword == CALL_BLOCK:  # only planned when it sets arguments
                outcome = _set_block_arguments(step, context, result.run)
            else:
                raw = step.keyword in RAW_INPUT_KEYWORDS  # These replace their own tokens.
                locator_value = context.resolve(step.locator_value)
                input_data = context.resolve(step.input_data)
                if env.secrets is not None:
                    env.secrets.check_step(step.keyword, locator_value, input_data)
                outcome = run_keyword(
                    env, step.keyword, step.locator_type, locator_value,
                    step.input_data if raw else input_data,
                )
                if env.secrets is not None and step.store_as:
                    env.secrets.check_stored(outcome.stored_value)
            if step.store_as:
                context.variables[step.store_as] = outcome.stored_value
                result.run.runtime_variables_json = json.dumps(context.public_variables())
            record.actual_value = context.mask(outcome.actual_value)
            if outcome.screenshot:
                record.screenshot_base64 = base64.b64encode(outcome.screenshot).decode("ascii")
            if outcome.skip_guarded:
                skip_through = number + step.guarded_steps
                if step.guarded_steps:
                    record.actual_value += f", {step.guarded_steps} step(s) not run"
        except Exception as exc:
            message = context.mask(clean_error(exc))
            record.status = STEP_FAIL
            record.error_details = message
            diagnostics = session.take_diagnostics()
            if diagnostics:
                record.error_details += context.mask(
                    DIAGNOSTICS_HEADING + DIAGNOSTICS_BULLET.join(diagnostics)
                )
            record.screenshot_base64 = _capture_screenshot(session, env.secrets)
            failures.append(f"{phase.label}step".capitalize() + f" {number} ({step.keyword}) failed: {message}")
            result.error_message = "\n".join([*earlier, *failures])
            if step.keyword not in SOFT_KEYWORDS:
                db.session.add(record)
                db.session.commit()
                phase.stopped, phase.stopped_at = "failed", number
                return phase
        db.session.add(record)
        db.session.commit()
    return phase


def _run_teardown(teardown, first_number, total, result, session, context, env, guard, failures):
    """Run the teardown block after the test case's own steps, pass or fail. Its
    failures are added to the test case's message after the main ones, and never
    change whether the test case passed. Returns True if a teardown step failed."""
    notes = []
    phase = _run_phase(
        _Phase(teardown, first_number, total, label="teardown "), result, session, context, env,
        guard, notes, earlier=failures,
    )
    if phase.stopped == "aborted":
        notes.append(guard.stopped_before(phase.stopped_at, "teardown "))
    if notes:
        result.error_message = "\n".join([*failures, *notes])
    return any(note.startswith("Teardown step") for note in notes)


def _run_case(result, session, context, outcomes, guard, application):
    """Execute one test case. Returns True if the browser context must be reset."""
    case = result.test_case
    if case is None:
        _block(result, "This test case was deleted before it could run.")
        return False

    dependency = case.depends_on
    if dependency is not None:
        outcome = outcomes.get(dependency.id)
        if outcome is None:
            _block(result, f"Depends on '{dependency.name}', which did not run earlier in this queue.")
            return False
        if outcome != TC_PASS:
            _block(result, f"Depends on '{dependency.name}', which ended as {outcome}.")
            return False

    try:
        steps = expand_steps(case)
        if not steps:
            raise Blocked("This test case has no steps.")
        try:
            teardown = _teardown_steps(case)
        except Blocked as exc:
            raise Blocked(f"Teardown block '{case.teardown_block.name}': {exc}") from None
        # One check for both, so the teardown may use what the steps store.
        definitions = case.variables
        check_variables(steps + teardown, context, definitions)
        set_case_variables(definitions, context, result.run)
    except Blocked as exc:
        _block(result, str(exc))
        return False

    total = len(steps) + len(teardown)
    result.status = TC_RUNNING
    result.total_steps = total
    db.session.commit()
    started = time.monotonic()

    try:
        env = StepEnv(
            session.ensure_page(), application.base_url, session.timeout_ms, guard, session,
            resolve=context.resolve, secrets=SecretGuard(context, application.base_url),
        )
    except Exception as exc:
        result.status = TC_FAIL
        result.error_message = f"Could not open a browser page: {clean_error(exc)}"
        result.duration_sec = time.monotonic() - started
        return True

    failures = []
    main = _run_phase(_Phase(steps, 1, total), result, session, context, env, guard, failures)
    if main.stopped == "aborted":
        # An aborted run skips the teardown too: the user, or the watchdog, wants it over.
        result.duration_sec = time.monotonic() - started
        result.current_step = None
        if failures:  # What a soft check already found still stands.
            result.status = TC_FAIL
            result.error_message = "\n".join([*failures, guard.stopped_before(main.stopped_at)])
            return True
        _block(result, guard.stopped_before(main.stopped_at))
        return False

    teardown_failed = False
    if teardown:
        teardown_failed = _run_teardown(
            teardown, len(steps) + 1, total, result, session, context, env, guard, failures,
        )
    result.status = TC_FAIL if failures else TC_PASS
    result.current_step = None
    result.duration_sec = time.monotonic() - started
    # A soft failure resets the session like any other failure, and so does a failed
    # teardown: the next test case must not start logged in, or in a half-deleted state.
    return bool(failures) or teardown_failed


def prune_finished_runs(app_id=None):
    """Each application keeps its newest KEEP_RUNS_PER_APP finished runs. Runs still
    queued or running are left alone and do not count."""
    query = TestRun.query.filter(TestRun.status.notin_(RUN_ACTIVE))
    if app_id is not None:
        query = query.filter(TestRun.app_id == app_id)
    limit = max(config.KEEP_RUNS_PER_APP, 1)
    kept, removed = {}, []
    try:
        for run in query.order_by(TestRun.id.desc()).all():
            kept[run.app_id] = kept.get(run.app_id, 0) + 1
            if kept[run.app_id] > limit:
                removed.append(run.id)
                db.session.delete(run)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Removing older runs failed")
        return
    for run_id in removed:
        report_generator.delete_report(run_id)


def _finish(run, status):
    for result in run.case_results:
        if result.status in (TC_PENDING, TC_RUNNING):
            _block(result, "Run ended before this test case finished.")
    _refresh_counts(run)
    run.status = status
    run.completed_at = utcnow()
    db.session.commit()
    prune_finished_runs(run.app_id)


def _save_performance(run, recorder):
    """Losing the timings must not lose the test results."""
    try:
        recorder.save(run)
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("Saving performance results for run %s failed", run.id)


def execute_run(run_id):
    """Run a claimed TestRun to completion. Needs an active app context."""
    run = db.session.get(TestRun, run_id)
    application = run.application
    system = get_system_config()
    timeout_ms = system.default_timeout_ms
    custom_paths = browser_factory.custom_paths_from(system)
    username, password, http_credentials = _credentials_for(application, run)
    context = RuntimeContext(username, password, _secret_values(application, run))
    recorder = PerformanceRecorder(context.mask)
    final_status = RUN_COMPLETED
    guard = RunGuard(run_id)
    holder = {}  # the current browser session, for the hard-stop timer

    def launch(playwright):
        # With the application's own AD account, Edge must not sign in silently as
        # the account the portal runs as, or http_credentials is never asked for.
        launched = browser_factory.launch_browser(
            playwright, run.requested_browser, custom_paths, run.headless,
            silent_signin=http_credentials is None,
        )
        run.browser_used = launched.browser_used
        if launched.note and launched.note not in run.notes:
            run.add_note(launched.note)
        db.session.commit()
        holder["session"] = BrowserSession(launched, run.headless, timeout_ms, http_credentials, recorder)
        return holder["session"]

    hard_stop = _start_hard_stop(guard, holder)
    try:
        with sync_playwright() as playwright:
            session = launch(playwright)
            outcomes = {}
            for result in list(run.case_results):
                if guard():
                    break
                if not session.browser.is_connected():
                    log.warning("Browser disconnected during run %s; relaunching", run_id)
                    session.reset()
                    session = launch(playwright)
                case_id = result.test_case_id
                if _run_case(result, session, context, outcomes, guard, application):
                    session.settle()
                    session.reset()
                if case_id is not None:
                    outcomes[case_id] = result.status
                # Again, because a value typed into a password field is hidden from then on.
                run.runtime_variables_json = json.dumps(context.public_variables())
                _refresh_counts(run)
                db.session.commit()
            if guard.reason == ABORT_WATCHDOG:
                final_status = RUN_ABORTED
                log.warning("Run %s: %s", run_id, guard.watchdog_note())
                run.add_note(guard.watchdog_note())
            elif _abort_requested(run_id):
                final_status = RUN_ABORTED
                run.add_note("Run aborted by user.")
            session.settle()
            session.close()
    except browser_factory.BrowserLaunchError as exc:
        db.session.rollback()
        final_status = RUN_ABORTED
        run.add_note(str(exc))
    except Exception as exc:
        db.session.rollback()
        log.exception("Run %s stopped unexpectedly", run_id)
        final_status = RUN_ABORTED
        run.add_note(f"Run stopped unexpectedly: {clean_error(exc)}")
        if guard.expired():
            run.add_note(guard.watchdog_note())
    finally:
        if hard_stop is not None:
            hard_stop.cancel()
    _save_performance(run, recorder)
    _finish(run, final_status)


def claim_next_run():
    """Atomically move the oldest Queued run to Running. Returns its id or None."""
    run_id = db.session.execute(
        db.select(TestRun.id).where(TestRun.status == RUN_QUEUED).order_by(TestRun.id).limit(1)
    ).scalar()
    if run_id is None:
        db.session.commit()
        return None
    claimed = db.session.execute(
        db.update(TestRun)
        .where(TestRun.id == run_id, TestRun.status == RUN_QUEUED)
        .values(status=RUN_RUNNING, started_at=utcnow())
    ).rowcount
    db.session.commit()
    return run_id if claimed else None


def recover_interrupted_runs():
    """On startup nothing is executing, so any active run was cut off by a restart,
    and any file left in the downloads folder belongs to one."""
    shutil.rmtree(config.DOWNLOADS_DIR, ignore_errors=True)
    for run in TestRun.query.filter(TestRun.status.in_(RUN_ACTIVE)).all():
        run.add_note("Run was interrupted because the portal restarted.")
        _finish(run, RUN_ABORTED)


def _worker_loop(flask_app, stop_event):
    while not stop_event.is_set():
        try:
            with flask_app.app_context():
                run_id = claim_next_run()
                if run_id is not None:
                    execute_run(run_id)
            if run_id is None:
                maintenance.run_idle_tasks()  # checkpoint and daily backup, between runs only
                stop_event.wait(1.0)
        except Exception:
            log.exception("Run worker error; continuing")
            stop_event.wait(2.0)


def start_worker(flask_app):
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_worker_loop, args=(flask_app, stop_event), name="qa-run-worker", daemon=True
    )
    thread.start()
    return thread, stop_event
