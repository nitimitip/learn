"""Find out which way the recording browser can sign in to a site that shows the
Windows Security "Sign in to access this site" pop-up.

Run on the machine where the pop-up appears:
    python tools\\diag_windows_signin.py https://qpidprod --domain VOLT --user <user name>
It asks for the password (never saved). Each attempt opens Edge for a few
seconds. Paste the printed report back. ASCII only.
"""
import argparse
import getpass
import os
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def offered_schemes(url):
    """What the server asks for, read without any credentials."""
    ctx = ssl._create_unverified_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
    try:
        with opener.open(url, timeout=20) as resp:
            return f"HTTP {resp.status} without credentials (no sign-in asked)"
    except urllib.error.HTTPError as exc:
        values = exc.headers.get_all("WWW-Authenticate") or []
        return f"HTTP {exc.code}; WWW-Authenticate: {values}"
    except Exception as exc:
        return f"could not reach: {exc}"


def attempt(playwright, url, label, persistent, credentials, allowlist):
    args = []
    if allowlist:
        args = [f"--auth-server-allowlist={allowlist}", f"--auth-negotiate-delegate-allowlist={allowlist}"]
    profile = None
    statuses = []
    try:
        if persistent:
            profile = tempfile.mkdtemp(prefix="qa_signin_diag_")
            options = {"headless": False, "channel": "msedge", "args": args, "ignore_https_errors": True}
            if credentials:
                options["http_credentials"] = credentials
            context = playwright.chromium.launch_persistent_context(profile, **options)
            browser = None
        else:
            browser = playwright.chromium.launch(headless=False, channel="msedge", args=args)
            options = {"ignore_https_errors": True}
            if credentials:
                options["http_credentials"] = credentials
            context = browser.new_context(**options)
        context.on("response", lambda r: statuses.append(r.status) if r.request.is_navigation_request() else None)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            final = resp.status if resp else None
            page.wait_for_timeout(1500)
            title = page.title()
            result = f"final HTTP {final}, title '{title[:50]}'"
        except Exception as exc:
            result = "error: " + str(exc).splitlines()[0][:120]
        context.close()
        if browser:
            browser.close()
    except Exception as exc:
        result = "could not start: " + str(exc).splitlines()[0][:120]
    finally:
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
    verdict = "OK " if result.startswith("final HTTP 2") or result.startswith("final HTTP 3") else "NO "
    print(f"  [{verdict}] {label:55} {result}; navigation statuses {statuses[:8]}", flush=True)


def start_edge_netonly(domain, user, password, port, profile, allowlist=""):
    """Start Edge whose NETWORK sign-ins use DOMAIN\\user (like runas /netonly).
    The password is only checked when a site asks, not here. Returns the
    process handle (Windows only)."""
    import ctypes
    from ctypes import wintypes

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    edge = shutil.which("msedge") or next(
        (p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.isfile(p)), None)
    if not edge:
        raise RuntimeError("msedge.exe not found")
    command = (f'"{edge}" --remote-debugging-port={port} --user-data-dir="{profile}" --no-first-run '
               f'--no-default-browser-check ')
    if allowlist:
        command += f"--auth-server-allowlist={allowlist} --auth-negotiate-delegate-allowlist={allowlist} "
    command += "about:blank"
    si, pi = STARTUPINFO(), PROCESS_INFORMATION()
    si.cb = ctypes.sizeof(si)
    LOGON_NETCREDENTIALS_ONLY = 2
    ok = ctypes.windll.advapi32.CreateProcessWithLogonW(
        user, domain or None, password, LOGON_NETCREDENTIALS_ONLY, None,
        ctypes.create_unicode_buffer(command), 0, None, None, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise ctypes.WinError()
    ctypes.windll.kernel32.CloseHandle(pi.hThread)
    return pi.hProcess


def attempt_netonly(playwright, url, label, domain, user, password, allowlist):
    """Edge started as runas /netonly, then attached with connect_over_cdp."""
    import socket
    import time
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    profile = tempfile.mkdtemp(prefix="qa_signin_diag_")
    statuses = []
    try:
        start_edge_netonly(domain, user, password, port, profile, allowlist)
        browser = None
        for _ in range(60):
            try:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                break
            except Exception:
                time.sleep(0.5)
        if browser is None:
            raise RuntimeError("Edge did not answer on the debugging port")
        context = browser.contexts[0]
        context.on("response", lambda r: statuses.append(r.status) if r.request.is_navigation_request() else None)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            result = f"final HTTP {resp.status if resp else None}, title '{page.title()[:50]}'"
        except Exception as exc:
            result = "error: " + str(exc).splitlines()[0][:120]
        try:
            browser.new_browser_cdp_session().send("Browser.close")
        except Exception:
            pass
    except Exception as exc:
        result = "could not start: " + str(exc).splitlines()[0][:120]
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    verdict = "OK " if result.startswith(("final HTTP 2", "final HTTP 3")) else "NO "
    print(f"  [{verdict}] {label:55} {result}; navigation statuses {statuses[:8]}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--domain", default="")
    parser.add_argument("--user", default="")
    args = parser.parse_args()
    host = urlsplit(args.url).hostname or ""
    password = getpass.getpass(f"Password of {args.domain}\\{args.user}: ") if args.user else ""

    print(f"Python {sys.version.split()[0]}  host={host}")
    print("Server:", offered_schemes(args.url))
    print("Attempts:")
    forms = []
    if args.user:
        if args.domain:
            forms.append((f"{args.domain}\\{args.user}", "DOMAIN\\user"))
            forms.append((f"{args.user}@{args.domain}", "user@DOMAIN"))
        forms.append((args.user, "user only"))
    with sync_playwright() as p:
        attempt(p, args.url, "1 normal profile, my Windows login (no password)", True, None, host)
        attempt(p, args.url, "2 normal profile, my Windows login, allow all hosts", True, None, "*")
        for n, (name, label) in enumerate(forms, start=3):
            creds = {"username": name, "password": password}
            attempt(p, args.url, f"{n}a private context, {label}", False, creds, "")
            attempt(p, args.url, f"{n}b private context, {label}, allowlist", False, creds, host)
            attempt(p, args.url, f"{n}c normal profile, {label}, allowlist", True, creds, host)
        if args.user:
            n = 3 + len(forms)
            attempt_netonly(p, args.url, f"{n}a Edge as runas /netonly {args.domain}\\{args.user}",
                            args.domain, args.user, password, "")
            attempt_netonly(p, args.url, f"{n}b Edge as runas /netonly, allowlist",
                            args.domain, args.user, password, host)
    print("Done. Copy everything above this line.")


if __name__ == "__main__":
    main()
