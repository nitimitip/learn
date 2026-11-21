"""Check two ways of making the recording browser sign in as the TYPED account
(DOMAIN\\user) instead of the Windows account this PC is logged in with.

  1  typed account sent by the browser, silent sign-in switched off
  2  the browser runs as the typed account ("runas /netonly"): this script
     starts a copy of itself that way, and that copy drives Edge normally

Run from the tools folder:
    python diag_netonly.py https://qpidprod --domain VOLT --user Anup.Garain
Asks for the password (never saved). Paste everything it prints. ASCII only.
"""
import argparse
import ctypes
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

NO_SILENT_SIGNIN = "qa-no-silent-signin.invalid"  # matches no host: silent sign-in off


def look(url, allowlist, credentials=None, persistent=False):
    """Open url in Edge; return what the page shows."""
    args = [f"--auth-server-allowlist={allowlist}"] if allowlist else []
    seen = {}
    profile = tempfile.mkdtemp(prefix="qa_netonly_diag_") if persistent else None
    try:
        with sync_playwright() as p:
            options = {"ignore_https_errors": True}
            if credentials:
                options["http_credentials"] = credentials
            if persistent:
                context = p.chromium.launch_persistent_context(profile, channel="msedge", headless=False,
                                                               args=args, **options)
                browser = None
            else:
                browser = p.chromium.launch(channel="msedge", headless=False, args=args)
                context = browser.new_context(**options)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                seen["url"] = page.url
                seen["body"] = " ".join(page.inner_text("body", timeout=3000).split())[:140]
            except Exception as exc:
                seen["error"] = str(exc).splitlines()[0][:160]
            context.close()
            if browser:
                browser.close()
    except Exception as exc:
        seen["error"] = "could not start: " + str(exc).splitlines()[0][:160]
    finally:
        if profile:
            shutil.rmtree(profile, ignore_errors=True)
    return seen


def show(label, seen):
    print(f"\n== {label}", flush=True)
    for key in ("url", "body", "error", "who"):
        if key in seen:
            print(f"   {key:5}: {seen[key]}", flush=True)


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


def run_netonly(domain, user, password, command_line, cwd, timeout_s):
    """Run command_line with network sign-ins as DOMAIN\\user (runas /netonly).
    Returns the exit code, or None on timeout."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    si, pi = STARTUPINFO(), PROCESS_INFORMATION()
    si.cb = ctypes.sizeof(si)
    LOGON_NETCREDENTIALS_ONLY, CREATE_NO_WINDOW = 2, 0x08000000
    ok = advapi32.CreateProcessWithLogonW(
        user, domain or None, password, LOGON_NETCREDENTIALS_ONLY, None,
        ctypes.create_unicode_buffer(command_line), CREATE_NO_WINDOW, None, cwd,
        ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.CloseHandle(pi.hThread)
    try:
        if kernel32.WaitForSingleObject(pi.hProcess, int(timeout_s * 1000)) != 0:
            kernel32.TerminateProcess(pi.hProcess, 1)
            return None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(pi.hProcess)


def child(url, out_file):
    """Runs under runas /netonly: plain Edge, no password given to the browser."""
    host = urlsplit(url).hostname or ""
    result = {"private": look(url, host), "normal profile": look(url, host, persistent=True)}
    with open(out_file, "w", encoding="utf-8") as handle:
        json.dump(result, handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--domain", default="")
    parser.add_argument("--user", default="")
    parser.add_argument("--child-out", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child_out:
        child(args.url, args.child_out)
        return
    if not args.user:
        parser.error("--user is required")
    password = getpass.getpass(f"Password of {args.domain}\\{args.user}: ")
    name = f"{args.domain}\\{args.user}" if args.domain else args.user
    creds = {"username": name, "password": password}
    print(f"Python {sys.version.split()[0]}  logged-in Windows user: {os.environ.get('USERDOMAIN')}\\"
          f"{os.environ.get('USERNAME')}")

    show("1a typed account, silent sign-in OFF, private window",
         look(args.url, NO_SILENT_SIGNIN, creds))
    show("1b typed account, silent sign-in OFF, normal profile",
         look(args.url, NO_SILENT_SIGNIN, creds, persistent=True))

    out_dir = tempfile.mkdtemp(prefix="qa_netonly_out_")
    out_file = os.path.join(out_dir, "result.json")
    command = subprocess.list2cmdline([sys.executable, os.path.abspath(__file__), args.url,
                                       "--child-out", out_file])
    print("\n(2: starting a copy of this script as runas /netonly ... up to 2 minutes)", flush=True)
    started = time.time()
    try:
        code = run_netonly(args.domain, args.user, password, command, os.getcwd(), 120)
    except OSError as exc:
        code = f"could not start: {exc}"
    try:
        with open(out_file, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, ValueError):
        result = {}
    print(f"   copy finished: exit {code} after {time.time() - started:.0f}s", flush=True)
    show("2a browser runs AS typed account (netonly), private window",
         result.get("private", {"error": "no result written"}))
    show("2b browser runs AS typed account (netonly), normal profile",
         result.get("normal profile", {"error": "no result written"}))
    shutil.rmtree(out_dir, ignore_errors=True)
    print("\nDone. Copy everything above this line.")


if __name__ == "__main__":
    main()
