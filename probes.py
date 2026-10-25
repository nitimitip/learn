"""
modules/mimp_report/probes.py
Three independent, side-effect-free probes used by the MIMP scheduler.

Each probe takes a MIMPApplication ORM row and returns a ProbeOutcome:

    ok      : 'good' | 'warning' | 'down' | 'skipped'
    reason  : short, ticket-pasteable human-readable string
    elapsed_ms : how long the probe actually took

Design rules
    * Probes do NOT touch the database - they take a populated row in,
      they return a verdict out. The scheduler is responsible for
      writing MIMPApplicationStatus rows.
    * Every external call is wrapped - no probe is ever allowed to
      raise to the caller. A bug or a missing driver = ('down', '<reason>').
    * Timeouts are explicit on every external call. We never want a
      single hung app to delay the whole sweep.
    * Credentials are decrypted at the LAST possible moment and never
      logged.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
import urllib3

# Optional: NTLM auth for AD-protected URLs. Same single-service-account model
# as the application_health_check module - credentials come from the env vars
# URL_NTLM_USER / URL_NTLM_PASS (typically supplied via the project .env).
# Install with `pip install requests-ntlm` to enable.
try:
    from requests_ntlm import HttpNtlmAuth
    _HAS_NTLM = True
except ImportError:
    _HAS_NTLM = False

from modules.mimp_report.crypto import decrypt_secret
from modules.mimp_report.models import MIMPApplication

logger = logging.getLogger(__name__)


# TLS / proxy policy for the URL probe
#
# This module is imported by routes.py, so it is loaded inside every IIS web
# worker. It therefore must NOT mutate process-wide state: the previous
# version set os.environ["NO_PROXY"]="*" and called urllib3.disable_warnings()
# at import time, which silently changed proxy behaviour and warning filters
# for every OTHER module in the process (health checks, ServiceNow, file
# monitor). Both are now scoped to the probe's own requests.Session.
#
# Verification follows the same three env vars the application_health_check
# module uses, so one setting covers both monitors:
#   CORPORATE_CA_BUNDLE=/path  -> verify against that PEM bundle
#   DISABLE_SSL_VERIFY=1       -> no verification (the historic behaviour)
#   neither                    -> no verification, preserved as the default
#                                 because MIMP targets are internal apps with
#                                 self-signed certificates. Set
#                                 CORPORATE_CA_BUNDLE to turn verification on.
def _resolve_verify():
    bundle = (os.environ.get("CORPORATE_CA_BUNDLE") or "").strip()
    if bundle and os.environ.get("DISABLE_SSL_VERIFY", "").strip() != "1":
        return bundle
    return False


_VERIFY = _resolve_verify()

if _VERIFY is False:
    # Scoped to this module's own warning category filter rather than the
    # global one urllib3.disable_warnings() installs process-wide.
    import warnings
    warnings.filterwarnings(
        "ignore", category=urllib3.exceptions.InsecureRequestWarning)


def _probe_session() -> requests.Session:
    """A Session that ignores the machine's proxy configuration.

    trust_env=False is the per-session equivalent of the NO_PROXY="*" the
    module used to export globally: monitored applications are on the internal
    network and must be reached directly, but nothing else in the process
    should have its proxy settings rewritten.
    """
    session = requests.Session()
    session.trust_env = False
    return session

# Tunables - edit here, no logic changes needed

URL_CONNECT_TIMEOUT  = 5      # TCP connect (seconds)
URL_READ_TIMEOUT     = 15     # first response byte (seconds)
URL_WARN_MS          = 3000   # response slower than this => 'warning'
URL_MAX_BODY_BYTES   = 64 * 1024
URL_RETRY_DELAY_SEC  = 3      # wait between attempt 1 and attempt 2

SSH_TIMEOUT          = 10     # paramiko connect + auth
SSH_COMMAND_TIMEOUT  = 10     # exec_command read timeout
WINRM_TIMEOUT        = 15     # pywinrm connect+exec
DB_TIMEOUT           = 10     # database connect (Oracle/MSSQL)

# python-oracledb thick-mode state
# python-oracledb defaults to THIN mode, which cannot authenticate Oracle
# accounts whose password is stored with the legacy 10g verifier - the connect
# fails with "DPY-3015: password verify type 0x939 is not supported by
# python-oracledb in thin mode". Thick mode hands authentication to the Oracle
# Client libraries, which DO accept the old verifier, so we initialise it once
# before the first Oracle probe. No password or database change is required.
# The host already ships the Oracle Client for the cx_Oracle-based modules; set
# ORACLE_CLIENT_LIB_DIR to the Instant Client path only if it is not on PATH.
_oracle_thick_lock = threading.Lock()
_oracle_thick_ready: Optional[bool] = None

PROBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}

# Substrings that, if found in a 200-OK body, mean the app is NOT really up.
# Lowercased - compared against body.lower().
FAILURE_MARKERS: Tuple[str, ...] = (
    "service unavailable",
    "we'll be right back",
    "we will be right back",
    "site is temporarily unavailable",
    "under maintenance",
    "scheduled maintenance",
    "temporarily down",
    "temporarily unavailable",
    "site offline",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "internal server error",
    "application error",
    "yellow screen of death",
    "runtime error",
    "server error in",
    "asp.net error",
    "whitelabel error page",
    "java.lang.",
    "javax.servlet",
    "at org.apache.",
    "at org.springframework.",
    "traceback (most recent call last)",
    "could not connect to database",
    "sqlexception",
    "connection refused",
)


# Service-name validation
#
# server_service_name is interpolated into a remote shell command (SSH) and a
# remote PowerShell command (WinRM) on the MONITORED production server, so it
# must never carry shell/PS metacharacters. Same philosophy as
# application_health_check.utils.common._sanitise_process_name: reject loudly
# instead of silently stripping (stripping would silently check a different
# service than the one configured).
#
# Allowed: letters, digits, spaces, dots, hyphens, underscores, and '@'
# (systemd template instances like 'tomcat@app1'). Must start with a letter,
# digit, or underscore so a name can never be mistaken for a command option.

_SERVICE_NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9 ._@-]*$')


def sanitise_service_name(name: str) -> str:
    """Validate a service name destined for a remote command. Returns the
    stripped name, or raises ValueError with a user-presentable reason."""
    name = (name or '').strip()
    if not name:
        raise ValueError('Service name is empty.')
    if not _SERVICE_NAME_RE.match(name):
        raise ValueError(
            f"Service name '{name}' contains unsupported characters. "
            f"Allowed: letters, digits, spaces, dots, hyphens, underscores "
            f"and '@'; it must start with a letter, digit, or underscore."
        )
    return name


# Outcome type

@dataclass
class ProbeOutcome:
    status: str        # 'good' | 'warning' | 'down' | 'skipped'
    reason: str        # short human-readable line
    elapsed_ms: float  # how long the probe took

    @property
    def passed(self) -> bool:
        return self.status in ("good", "warning", "skipped")


# 1. URL PROBE

def probe_url(app: MIMPApplication) -> ProbeOutcome:
    """
    HTTP GET against `app.url`. Retries once on failure.
    Final verdict = best of the two attempts (good > warning > down).
    """
    if not app.url:
        return ProbeOutcome("skipped", "No URL configured", 0.0)

    first = _do_url_probe(app.url)
    if first.status == "good":
        return first

    # Retry once after a short delay to debounce transient blips.
    time.sleep(URL_RETRY_DELAY_SEC)
    second = _do_url_probe(app.url)

    # Best-of-two - but report on the FINAL attempt's note for transparency.
    rank = {"good": 3, "warning": 2, "down": 1, "skipped": 0}
    if rank[second.status] >= rank[first.status]:
        return second
    return first


def _ntlm_credentials():
    """Return (user, pass) from the env if both are set, else None.

    Shared single-service-account model with application_health_check: the
    same URL_NTLM_USER / URL_NTLM_PASS variables (loaded from .env) are reused
    here so AD-protected MIMP URLs authenticate the same way."""
    user = os.environ.get("URL_NTLM_USER")
    pwd  = os.environ.get("URL_NTLM_PASS")
    if user and pwd:
        return user, pwd
    return None


def _server_offers_ntlm(response) -> bool:
    """Whether a 401's WWW-Authenticate header advertises Windows auth.

    NEGOTIATE counts too: IIS Windows Authentication often offers only
    "Negotiate", and requests-ntlm answers that challenge with NTLM inside
    it. requests merges repeated WWW-Authenticate headers into one
    comma-separated value, so a substring test covers both."""
    header = (response.headers.get("WWW-Authenticate") or "").upper()
    return "NTLM" in header or "NEGOTIATE" in header


def _ntlm_allowed_hosts() -> set:
    """The hostnames permitted to receive the NTLM service account.

    MIMP_NTLM_ALLOWED_HOSTS is read first so this module can be scoped
    separately if it ever needs to be, but it normally falls through to
    HEALTH_NTLM_ALLOWED_HOSTS - the list application_health_check already
    uses. One list then governs both monitors, which share the same
    URL_NTLM_USER / URL_NTLM_PASS service account: a host approved for AD
    authentication is approved for it once, not once per module.
    """
    raw = (os.environ.get("MIMP_NTLM_ALLOWED_HOSTS")
           or os.environ.get("HEALTH_NTLM_ALLOWED_HOSTS")
           or "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def _ntlm_target_approved(url: str) -> bool:
    """Whether the service account's credentials may be sent to this URL.

    An NTLM handshake puts the monitoring service account's Net-NTLM material
    on the wire, so anything able to intercept or impersonate the target can
    harvest it. Previously ANY host that answered a 401 with an NTLM challenge
    was handed the credentials.

    The gate is now the same allowlist application_health_check applies, and
    it fails CLOSED: an empty or unset list approves nothing. That is exactly
    how the health-check module already behaves, so an AD-protected target
    must be named explicitly before either monitor will authenticate to it.

    Note the deliberate difference from application_health_check, which also
    demands verified HTTPS: MIMP targets are internal applications with
    self-signed certificates, so requiring that here would refuse every real
    target. The allowlist is the control; set CORPORATE_CA_BUNDLE to add
    certificate verification on top.
    """
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    if not hostname:
        return False
    return hostname in _ntlm_allowed_hosts()


def _do_url_probe(url: str) -> ProbeOutcome:
    """One HTTP attempt. Never raises.

    Anonymous first; if the server answers 401 with an NTLM challenge and
    URL_NTLM_USER / URL_NTLM_PASS are configured (and requests-ntlm is
    installed), the request is re-issued once with NTLM auth before the
    response is classified."""
    started = time.monotonic()

    # Cache-bust query param defeats CDN/proxy caching.
    sep = "&" if "?" in url else "?"
    bust_url = f"{url}{sep}_hc={int(time.time() * 1000)}"

    # Stage 1: anonymous.
    outcome, ntlm_challenged = _attempt_url(bust_url, started, auth=None)

    # Stage 2: AD/NTLM retry on a 401 NTLM challenge.
    # Every 401 below names its cause in the report: a bare "HTTP 401 client
    # error" cannot tell "credentials never sent" from "credentials rejected".
    if ntlm_challenged:
        creds = _ntlm_credentials()
        host = urlparse(url).hostname
        if not creds:
            return ProbeOutcome(
                "down",
                "HTTP 401 - AD authentication required but URL_NTLM_USER / "
                "URL_NTLM_PASS are not set in .env",
                outcome.elapsed_ms,
            )
        if not _ntlm_target_approved(url):
            # Target is not on the allowlist - the anonymous 401 verdict
            # stands rather than handing it the service account.
            logger.warning(
                "Not sending NTLM credentials to '%s': add it to "
                "HEALTH_NTLM_ALLOWED_HOSTS (shared with the health-check "
                "module) if this application genuinely needs AD "
                "authentication.", host,
            )
            return ProbeOutcome(
                "down",
                f"HTTP 401 - AD credentials not sent: '{host}' is not in "
                "HEALTH_NTLM_ALLOWED_HOSTS (or MIMP_NTLM_ALLOWED_HOSTS) in .env",
                outcome.elapsed_ms,
            )
        if _HAS_NTLM:
            outcome, _ = _attempt_url(bust_url, started, auth=HttpNtlmAuth(*creds))
            if outcome.reason.startswith("HTTP 401"):
                return ProbeOutcome(
                    "down",
                    "HTTP 401 - AD credentials rejected: check URL_NTLM_USER is "
                    "DOMAIN\\user, the password, account lockout, and that the "
                    "account has access to this application",
                    outcome.elapsed_ms,
                )
        else:
            elapsed = (time.monotonic() - started) * 1000
            return ProbeOutcome(
                "down",
                "HTTP 401 - URL requires NTLM but the requests-ntlm library is "
                "not installed on the host.",
                elapsed,
            )

    return outcome


def _attempt_url(bust_url: str, started: float, auth) -> Tuple[ProbeOutcome, bool]:
    """One HTTP attempt with optional auth. Never raises.

    Returns (outcome, ntlm_challenged) where ntlm_challenged is True only on an
    anonymous (auth is None) 401 that advertised NTLM - the caller uses it to
    decide whether to retry with credentials."""
    session = None
    try:
        session = _probe_session()
        with session.get(
            bust_url,
            timeout=(URL_CONNECT_TIMEOUT, URL_READ_TIMEOUT),
            headers=PROBE_HEADERS,
            verify=_VERIFY,
            stream=True,
            # Credentialed attempts never follow a redirect: a target that can
            # be made to 302 elsewhere would otherwise replay the service
            # account's NTLM handshake against the redirect destination.
            allow_redirects=(auth is None),
            auth=auth,
        ) as resp:
            # Signal an NTLM retry to the caller (only worth it on the
            # anonymous attempt - a 401 while already authenticated is final).
            if auth is None and resp.status_code == 401 and _server_offers_ntlm(resp):
                elapsed = (time.monotonic() - started) * 1000
                return ProbeOutcome("down", "HTTP 401 client error", elapsed), True

            # Read at most MAX_BODY_BYTES - never wait for huge pages.
            chunks = []
            total = 0
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= URL_MAX_BODY_BYTES:
                        break
            except requests.exceptions.ChunkedEncodingError:
                elapsed = (time.monotonic() - started) * 1000
                return ProbeOutcome(
                    "down",
                    "Connection closed mid-stream (chunked encoding error)",
                    elapsed,
                ), False

            elapsed = (time.monotonic() - started) * 1000
            code = resp.status_code
            body = b"".join(chunks).decode("utf-8", errors="replace")
            body_lower = body.lower()

            # Status code first
            if 500 <= code < 600:
                return ProbeOutcome("down", f"HTTP {code} server error", elapsed), False
            if 400 <= code < 500:
                return ProbeOutcome("down", f"HTTP {code} client error", elapsed), False
            if 300 <= code < 400:
                return ProbeOutcome(
                    "warning",
                    f"HTTP {code} redirect to {resp.url}",
                    elapsed,
                ), False
            if not (200 <= code < 300):
                return ProbeOutcome("down", f"HTTP {code} unexpected", elapsed), False

            # 2xx but body says otherwise?
            for marker in FAILURE_MARKERS:
                if marker in body_lower:
                    return ProbeOutcome(
                        "down",
                        f"HTTP {code} but body contains failure marker: '{marker}'",
                        elapsed,
                    ), False

            # 2xx with empty body?
            if total < 50:
                return ProbeOutcome(
                    "down",
                    f"HTTP {code} but response body almost empty ({total} bytes)",
                    elapsed,
                ), False

            # Slow but OK?
            if elapsed > URL_WARN_MS:
                return ProbeOutcome(
                    "warning",
                    f"HTTP {code} OK but slow ({elapsed:.0f} ms)",
                    elapsed,
                ), False

            return ProbeOutcome(
                "good",
                f"HTTP {code} OK ({elapsed:.0f} ms, {total} bytes)",
                elapsed,
            ), False

    except requests.exceptions.ConnectTimeout:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome(
            "down",
            f"Connection timeout - server did not accept TCP within {URL_CONNECT_TIMEOUT}s",
            elapsed,
        ), False

    except requests.exceptions.ReadTimeout:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome(
            "down",
            f"Read timeout - server connected but sent no data within {URL_READ_TIMEOUT}s",
            elapsed,
        ), False

    except requests.exceptions.SSLError as e:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome("down", f"SSL/TLS error: {str(e)[:120]}", elapsed), False

    except requests.exceptions.ConnectionError as e:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome("down", f"Connection failed: {str(e)[:120]}", elapsed), False

    except requests.exceptions.RequestException as e:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome("down", f"Request error: {str(e)[:120]}", elapsed), False

    except Exception as e:  # pragma: no cover - defensive catch-all
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome("down", f"Unhandled error: {str(e)[:120]}", elapsed), False

    finally:
        # Release the socket back to the pool. Without this a sweep over many
        # applications leaves one connection per target open on the monitoring
        # host until the garbage collector gets to it.
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


# 2. SERVER PROBE

def probe_server(app: MIMPApplication) -> ProbeOutcome:
    """
    Login to the configured server (Linux/AIX via SSH, Windows via WinRM).

    If `server_service_name` is set, also verify that service is running.
    If blank, the verdict is based purely on whether login succeeded.
    """
    if not app.server_type:
        return ProbeOutcome("skipped", "No server check configured", 0.0)

    started = time.monotonic()
    try:
        password = decrypt_secret(app.server_password)
    except RuntimeError as e:
        return ProbeOutcome(
            "down",
            f"Server credential decrypt failed: {e}",
            (time.monotonic() - started) * 1000,
        )

    if not app.server_hostname or not app.server_username or not password:
        return ProbeOutcome(
            "down",
            "Server probe configured but hostname/username/password incomplete",
            (time.monotonic() - started) * 1000,
        )

    # Refuse an unsafe service name BEFORE opening any connection - it is
    # about to be embedded in a remote shell / PowerShell command.
    if app.server_service_name and app.server_service_name.strip():
        try:
            sanitise_service_name(app.server_service_name)
        except ValueError as e:
            return ProbeOutcome(
                "down",
                f"Service check refused: {e}",
                (time.monotonic() - started) * 1000,
            )

    stype = (app.server_type or "").lower()

    if stype in ("linux", "aix"):
        return _probe_unix_ssh(app, password, started)
    if stype == "windows":
        return _probe_windows_winrm(app, password, started)

    return ProbeOutcome(
        "down",
        f"Unknown server_type '{app.server_type}' (expected linux|aix|windows)",
        (time.monotonic() - started) * 1000,
    )


def _configure_ssh_trust(client, paramiko) -> None:
    """Decide how far to trust the host key before sending the password.

    The probe authenticates with a password, so an unverified host key means
    the monitoring account's password goes to whatever answered on that
    address. AutoAddPolicy - the previous behaviour - trusts every host
    unconditionally, which makes a man-in-the-middle a credential harvest.

    Known hosts are loaded first, so any host already recorded in the service
    account's known_hosts (or in MIMP_SSH_KNOWN_HOSTS) is now genuinely
    verified. Unknown hosts are still auto-added by default, because failing
    them closed would stop every existing Linux/AIX probe on a host that has
    never had a known_hosts file. Set MIMP_SSH_STRICT_HOST_KEY=1 once the
    monitored hosts are seeded to refuse unknown keys outright.
    """
    try:
        client.load_system_host_keys()
    except Exception as exc:
        logger.debug("Could not load system SSH host keys: %s", exc)
    known_hosts = os.environ.get("MIMP_SSH_KNOWN_HOSTS")
    if known_hosts:
        try:
            client.load_host_keys(known_hosts)
        except Exception as exc:
            logger.warning("Could not load MIMP_SSH_KNOWN_HOSTS (%s): %s",
                           known_hosts, exc)
    if os.environ.get("MIMP_SSH_STRICT_HOST_KEY", "").strip() == "1":
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())


def _close_ssh(client) -> None:
    """Close a probe's SSH client, never raising.

    Every exit from the connect step has to come through here: the
    `finally: client.close()` below covers the COMMAND block only, so a
    connect or auth failure used to return with the socket and paramiko's
    transport thread still alive. A host whose credentials are wrong is
    probed again every cycle, so that leak grows for as long as the service
    runs.
    """
    try:
        client.close()
    except Exception:
        logger.debug("Closing the SSH client failed", exc_info=True)


def _probe_unix_ssh(app: MIMPApplication, password: str, started: float) -> ProbeOutcome:
    try:
        import paramiko
    except ImportError:
        return ProbeOutcome(
            "down",
            "paramiko library not installed - pip install paramiko",
            (time.monotonic() - started) * 1000,
        )

    port = app.server_port or 22
    client = paramiko.SSHClient()
    _configure_ssh_trust(client, paramiko)

    try:
        client.connect(
            hostname=app.server_hostname,
            port=port,
            username=app.server_username,
            password=password,
            timeout=SSH_TIMEOUT,
            banner_timeout=SSH_TIMEOUT,
            auth_timeout=SSH_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
    except paramiko.AuthenticationException:
        _close_ssh(client)
        return ProbeOutcome(
            "down",
            f"SSH auth failed for {app.server_username}@{app.server_hostname}:{port}",
            (time.monotonic() - started) * 1000,
        )
    except (paramiko.SSHException, socket.timeout, OSError) as e:
        _close_ssh(client)
        return ProbeOutcome(
            "down",
            f"SSH connect failed to {app.server_hostname}:{port} - {str(e)[:120]}",
            (time.monotonic() - started) * 1000,
        )
    except Exception as e:  # pragma: no cover
        _close_ssh(client)
        return ProbeOutcome(
            "down",
            f"SSH unexpected error: {str(e)[:120]}",
            (time.monotonic() - started) * 1000,
        )

    try:
        # If no service requested, login alone counts as a pass.
        if not app.server_service_name:
            elapsed = (time.monotonic() - started) * 1000
            return ProbeOutcome(
                "good",
                f"SSH login OK to {app.server_hostname} (no service check)",
                elapsed,
            )

        # Re-validated at point of use (defence in depth - probe_server already
        # checked). The name is embedded in a remote shell command, so it must
        # contain no metacharacters; single-quoting is safe because the
        # sanitiser guarantees no quotes survive.
        svc = sanitise_service_name(app.server_service_name)
        # Try systemctl first (modern), then fall back to `service`.
        cmd = (
            f"if command -v systemctl >/dev/null 2>&1; then "
            f"  systemctl is-active '{svc}'; "
            f"else "
            f"  service '{svc}' status >/dev/null 2>&1 && echo active || echo inactive; "
            f"fi"
        )
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=SSH_COMMAND_TIMEOUT)
        out = stdout.read().decode("utf-8", errors="replace").strip().lower()
        err = stderr.read().decode("utf-8", errors="replace").strip()
        elapsed = (time.monotonic() - started) * 1000

        if out == "active":
            return ProbeOutcome(
                "good",
                f"SSH OK, service '{svc}' active on {app.server_hostname}",
                elapsed,
            )
        return ProbeOutcome(
            "down",
            f"SSH OK but service '{svc}' is '{out or 'unknown'}'"
            f"{' / stderr: ' + err[:80] if err else ''}",
            elapsed,
        )

    except socket.timeout:
        return ProbeOutcome(
            "down",
            f"SSH command timeout while checking service on {app.server_hostname}",
            (time.monotonic() - started) * 1000,
        )
    except Exception as e:  # pragma: no cover
        return ProbeOutcome(
            "down",
            f"SSH command error: {str(e)[:120]}",
            (time.monotonic() - started) * 1000,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def _probe_windows_winrm(app: MIMPApplication, password: str, started: float) -> ProbeOutcome:
    try:
        import winrm
    except ImportError:
        return ProbeOutcome(
            "down",
            "pywinrm library not installed - pip install pywinrm",
            (time.monotonic() - started) * 1000,
        )

    port = app.server_port or 5985
    scheme = "https" if port in (5986,) else "http"
    endpoint = f"{scheme}://{app.server_hostname}:{port}/wsman"

    # An HTTPS endpoint whose certificate is not checked gives no more
    # assurance than plain HTTP while looking like it does, and the NTLM
    # handshake below carries the monitoring account's credentials. Validate
    # by default; MIMP_WINRM_CERT_VALIDATION=ignore restores the old
    # behaviour for a host with a self-signed certificate that cannot yet be
    # added to the trust store.
    cert_validation = os.environ.get(
        "MIMP_WINRM_CERT_VALIDATION", "validate").strip().lower()
    if cert_validation not in ("validate", "ignore"):
        cert_validation = "validate"

    try:
        session = winrm.Session(
            endpoint,
            auth=(app.server_username, password),
            transport="ntlm",
            server_cert_validation=cert_validation,
            read_timeout_sec=WINRM_TIMEOUT,
            operation_timeout_sec=WINRM_TIMEOUT - 1,
        )
    except Exception as e:
        return ProbeOutcome(
            "down",
            f"WinRM session init failed: {str(e)[:120]}",
            (time.monotonic() - started) * 1000,
        )

    try:
        if not app.server_service_name:
            # Light-weight login probe - just whoami.
            result = session.run_ps("whoami")
            elapsed = (time.monotonic() - started) * 1000
            if result.status_code == 0:
                return ProbeOutcome(
                    "good",
                    f"WinRM login OK to {app.server_hostname} (no service check)",
                    elapsed,
                )
            return ProbeOutcome(
                "down",
                f"WinRM login failed: {result.std_err.decode('utf-8', 'replace')[:120]}",
                elapsed,
            )

        # Re-validated at point of use (defence in depth - probe_server already
        # checked). The sanitiser guarantees no quotes, so the single-quoted
        # PowerShell literal below cannot be broken out of.
        svc = sanitise_service_name(app.server_service_name)
        # Single PS line - return just the status string.
        ps = (
            f"$s = Get-Service -Name '{svc}' -ErrorAction SilentlyContinue; "
            f"if ($s) {{ $s.Status }} else {{ 'NotFound' }}"
        )
        result = session.run_ps(ps)
        elapsed = (time.monotonic() - started) * 1000

        if result.status_code != 0:
            err = result.std_err.decode("utf-8", "replace").strip()
            return ProbeOutcome(
                "down",
                f"WinRM exec failed checking '{svc}': {err[:120]}",
                elapsed,
            )

        status_text = result.std_out.decode("utf-8", "replace").strip()
        if status_text.lower() == "running":
            return ProbeOutcome(
                "good",
                f"WinRM OK, service '{svc}' Running on {app.server_hostname}",
                elapsed,
            )
        if status_text == "NotFound":
            return ProbeOutcome(
                "down",
                f"WinRM OK but service '{svc}' not found on {app.server_hostname}",
                elapsed,
            )
        return ProbeOutcome(
            "down",
            f"WinRM OK but service '{svc}' is {status_text} on {app.server_hostname}",
            elapsed,
        )

    except Exception as e:
        return ProbeOutcome(
            "down",
            f"WinRM error against {app.server_hostname}: {str(e)[:120]}",
            (time.monotonic() - started) * 1000,
        )


# 3. DATABASE PROBE

def probe_database(app: MIMPApplication) -> ProbeOutcome:
    """
    Connect to the configured database and run a trivial SELECT to prove
    the connection is real and the credentials still work.

    Oracle  : SELECT 1 FROM DUAL
    MSSQL   : SELECT 1
    """
    if not app.db_type:
        return ProbeOutcome("skipped", "No database check configured", 0.0)

    started = time.monotonic()
    try:
        password = decrypt_secret(app.db_password)
    except RuntimeError as e:
        return ProbeOutcome(
            "down",
            f"DB credential decrypt failed: {e}",
            (time.monotonic() - started) * 1000,
        )

    if not app.db_host or not app.db_username or not password:
        return ProbeOutcome(
            "down",
            "DB probe configured but host/username/password incomplete",
            (time.monotonic() - started) * 1000,
        )

    dtype = (app.db_type or "").lower()
    if dtype == "oracle":
        return _probe_oracle(app, password, started)
    if dtype == "mssql":
        return _probe_mssql(app, password, started)

    return ProbeOutcome(
        "down",
        f"Unknown db_type '{app.db_type}' (expected oracle|mssql)",
        (time.monotonic() - started) * 1000,
    )


def _ensure_oracle_thick_mode(oracledb) -> bool:
    """Enable python-oracledb THICK mode once per process. Idempotent + thread-safe.

    Returns True if thick mode is active (or already was), False if the Oracle
    Client libraries could not be loaded. Thick mode is what lets us connect to
    accounts that still use the legacy 10g password verifier (thin mode rejects
    them with DPY-3015). Set ORACLE_CLIENT_LIB_DIR to the Instant Client
    directory if the libraries are not already on the system path.
    """
    global _oracle_thick_ready
    if _oracle_thick_ready is not None:
        return _oracle_thick_ready
    with _oracle_thick_lock:
        if _oracle_thick_ready is not None:
            return _oracle_thick_ready
        try:
            lib_dir = (os.environ.get("ORACLE_CLIENT_LIB_DIR")
                       or os.environ.get("MIMP_ORACLE_CLIENT_LIB_DIR"))
            if lib_dir:
                oracledb.init_oracle_client(lib_dir=lib_dir)
            else:
                oracledb.init_oracle_client()
            _oracle_thick_ready = True
            logger.info("python-oracledb thick mode initialised for MIMP Oracle probes.")
        except Exception as e:
            # Re-initialising thick mode with identical args is harmless -
            # treat any "already initialised" message as success.
            if "already" in str(e).lower():
                _oracle_thick_ready = True
            else:
                _oracle_thick_ready = False
                logger.warning(
                    "Oracle thick mode unavailable (%s). Falling back to thin mode; "
                    "accounts with legacy 10g password verifiers will fail with "
                    "DPY-3015. Install the Oracle Client or set ORACLE_CLIENT_LIB_DIR.",
                    str(e)[:160],
                )
        return _oracle_thick_ready


def _probe_oracle(app: MIMPApplication, password: str, started: float) -> ProbeOutcome:
    try:
        import oracledb
    except ImportError:
        return ProbeOutcome(
            "down",
            "oracledb library not installed - pip install oracledb",
            (time.monotonic() - started) * 1000,
        )

    # Legacy 10g password verifiers (DPY-3015) require thick mode. This must run
    # before the first connect; it is a no-op on subsequent probes.
    _ensure_oracle_thick_mode(oracledb)

    port = app.db_port or 1521
    dsn = oracledb.makedsn(app.db_host, port, service_name=app.db_name) \
        if app.db_name else f"{app.db_host}:{port}"

    conn = None
    try:
        # Thin mode (no Oracle client install needed).
        conn = oracledb.connect(
            user=app.db_username,
            password=password,
            dsn=dsn,
            tcp_connect_timeout=DB_TIMEOUT,
        )
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM DUAL")
        row = cur.fetchone()
        cur.close()
        elapsed = (time.monotonic() - started) * 1000

        if row and row[0] == 1:
            return ProbeOutcome(
                "good",
                f"Oracle OK at {app.db_host}:{port}/{app.db_name or ''} ({elapsed:.0f} ms)",
                elapsed,
            )
        return ProbeOutcome(
            "down",
            f"Oracle connected but SELECT 1 returned unexpected: {row!r}",
            elapsed,
        )

    except Exception as e:
        # oracledb has a wide error hierarchy - we catch broadly because
        # this probe must NEVER raise to the scheduler.
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome(
            "down",
            f"Oracle error at {app.db_host}:{port} - {str(e)[:140]}",
            elapsed,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _odbc_value(value) -> str:
    """Brace-quote a value for an ODBC connection string."""
    return '{' + str(value).replace('}', '}}') + '}'


def _probe_mssql(app: MIMPApplication, password: str, started: float) -> ProbeOutcome:
    try:
        import pyodbc
    except ImportError:
        return ProbeOutcome(
            "down",
            "pyodbc library not installed - pip install pyodbc",
            (time.monotonic() - started) * 1000,
        )

    port = app.db_port or 1433
    # Driver name as registered on the server. ODBC 17/18 are the common ones.
    driver = os.environ.get("MIMP_MSSQL_ODBC_DRIVER", "ODBC Driver 17 for SQL Server")

    # Every caller-supplied value is brace-quoted. Without this a password or
    # database name containing ';' or '}' terminates its keyword early and the
    # remainder is parsed as further ODBC attributes - which could switch off
    # encryption or redirect the connection. Same escaping as
    # application_health_check.utils.database_check._odbc_value.
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={_odbc_value(str(app.db_host) + ',' + str(port))};"
        f"DATABASE={_odbc_value(app.db_name or 'master')};"
        f"UID={_odbc_value(app.db_username)};"
        f"PWD={_odbc_value(password)};"
        f"Encrypt=yes;TrustServerCertificate=yes;"
        f"Connection Timeout={DB_TIMEOUT};"
    )

    conn = None
    try:
        conn = pyodbc.connect(conn_str, timeout=DB_TIMEOUT)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        cur.close()
        elapsed = (time.monotonic() - started) * 1000

        if row and row[0] == 1:
            return ProbeOutcome(
                "good",
                f"MSSQL OK at {app.db_host}:{port}/{app.db_name or 'master'} "
                f"({elapsed:.0f} ms)",
                elapsed,
            )
        return ProbeOutcome(
            "down",
            f"MSSQL connected but SELECT 1 returned unexpected: {row!r}",
            elapsed,
        )

    except Exception as e:
        elapsed = (time.monotonic() - started) * 1000
        return ProbeOutcome(
            "down",
            f"MSSQL error at {app.db_host}:{port} - {str(e)[:140]}",
            elapsed,
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# Aggregator - combines the three probes into a single verdict

def run_all_probes(app: MIMPApplication) -> Tuple[str, str]:
    """
    Run URL + Server + DB probes (skipping any not configured) and return
    a single (status, note) tuple ready to write into MIMPApplicationStatus.

    Verdict rules:
        * Any configured probe reports 'down'    -> overall 'down'
        * No 'down', but any 'warning'           -> overall 'warning'
        * All configured probes pass cleanly     -> overall 'good'

    The note packs all three layer results into one line so on-call ops
    can see at a glance which layer failed:
        "URL good (412 ms) | Server good (svc Running) | DB good (62 ms)"
    """
    url_r    = probe_url(app)
    server_r = probe_server(app)
    db_r     = probe_database(app)

    parts = [
        f"URL {url_r.status}: {url_r.reason}",
        f"Server {server_r.status}: {server_r.reason}",
        f"DB {db_r.status}: {db_r.reason}",
    ]
    note = " | ".join(parts)

    statuses = [r.status for r in (url_r, server_r, db_r) if r.status != "skipped"]
    if not statuses:
        # Shouldn't happen - URL is always configured - but be safe.
        return "down", "No probes were configured for this application"

    if "down" in statuses:
        return "down", note
    if "warning" in statuses:
        return "warning", note
    return "good", note