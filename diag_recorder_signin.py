"""Run the RECORDER's own browser start-up (record_test_case.record) against a
site with Windows sign-in, and print what the page really shows.
diag_windows_signin.py proved plain Playwright signs in; this finds what in
the recorder is different.

Run from the tools folder, next to record_test_case.py:
    python diag_recorder_signin.py https://qpidprod --domain VOLT --user <user name>
Asks for the password (never saved). Edge opens 4 times, ~15 s each.
Paste everything it prints. ASCII only.
"""
import argparse
import getpass
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_test_case as rtc  # noqa: E402

WAIT_SECONDS = 10


def run(url, label, credentials, allowlist, ignore_https):
    out_dir = Path(tempfile.mkdtemp(prefix="qa_rec_diag_"))
    recorder = rtc.Recorder(url, "diag", out_path=out_dir / "diag.xlsx", verbose=False)
    seen = {}

    def on_ready(page):
        log = []
        page.context.on("response", lambda r: log.append(f"{r.status} {r.url[:70]}")
                        if r.request.is_navigation_request() else None)
        page.wait_for_timeout(WAIT_SECONDS * 1000)
        try:
            seen["url"] = page.url
            seen["title"] = page.title()
            seen["body"] = " ".join(page.inner_text("body", timeout=3000).split())[:160]
        except Exception as exc:
            seen["error"] = str(exc).splitlines()[0][:160]
        seen["later navigations"] = log[:8]
        challenge = recorder.auth_challenge
        if challenge:
            seen["recorder sign-in check"] = (f"401 {challenge['scheme']} for "
                                              f"{time.time() - challenge['since']:.0f}s -> would STOP the recording")
        else:
            seen["recorder sign-in check"] = "no sign-in problem seen"

    started = time.time()
    try:
        rtc.record(recorder, "msedge", ignore_https_errors=ignore_https, windows_auth=allowlist,
                   on_ready=on_ready, http_credentials=credentials)
    except BaseException as exc:  # SystemExit when no browser starts
        seen["record() raised"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    print(f"\n== {label}  ({time.time() - started:.0f}s)", flush=True)
    for key in ("url", "title", "body", "error", "later navigations", "recorder sign-in check", "record() raised"):
        if key in seen:
            print(f"   {key:24}: {seen[key]}", flush=True)
    shutil.rmtree(out_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--domain", default="")
    parser.add_argument("--user", default="")
    args = parser.parse_args()
    host = urlsplit(args.url).hostname or ""
    password = getpass.getpass(f"Password of {args.domain}\\{args.user}: ") if args.user else ""
    name = f"{args.domain}\\{args.user}" if args.domain else args.user
    creds = {"username": name, "password": password} if args.user else None

    print(f"Python {sys.version.split()[0]}  recorder file {rtc.__file__}")
    print(f"recorder modified {time.ctime(os.path.getmtime(rtc.__file__))}")
    print("has sign-in check:", hasattr(rtc, "auth_blocked_lines"),
          "| has normal-profile mode:", "profile_dir" in rtc.launch.__code__.co_varnames)
    if creds:
        run(args.url, "A typed account (section 3), allowlist, strict HTTPS", creds, host, False)
        run(args.url, "B typed account (section 3), allowlist, accept any HTTPS", creds, host, True)
    run(args.url, "C section 3 empty (my Windows login), allowlist, strict HTTPS", None, host, False)
    run(args.url, "D section 3 empty (my Windows login), accept any HTTPS", None, host, True)
    print("\nDone. Copy everything above this line.")


if __name__ == "__main__":
    main()
