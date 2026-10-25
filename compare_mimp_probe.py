"""Find which part of the MIMP probe's request turns a working NTLM login
into HTTP 401.

Run on the PROD server from the appassist folder:

    python compare_mimp_probe.py https://amp2view/applications/Amp2View/

Starts from a plain NTLM request (known to return 200) and adds one probe
difference per step. The first step whose status changes is the cause.
Credentials come from .env; the password is never printed.
"""
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)

from requests_ntlm import HttpNtlmAuth  # noqa: E402
from modules.mimp_report import probes  # noqa: E402


def auth():
    return HttpNtlmAuth(os.environ["URL_NTLM_USER"], os.environ["URL_NTLM_PASS"])


def status(label, url, **kw):
    s = probes._probe_session()
    try:
        r = s.get(url, timeout=(5, 30), verify=probes._VERIFY, **kw)
        loc = r.headers.get("Location", "")
        print("%-46s %s%s" % (label, r.status_code,
                              ("  -> " + loc) if loc else ""))
        r.close()
    except Exception as e:  # noqa: BLE001 - diagnostic, show anything
        print("%-46s ERROR %s" % (label, str(e)[:150]))
    finally:
        s.close()


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    url = sys.argv[1].strip()
    sep = "&" if "?" in url else "?"
    bust = "%s%s_hc=%d" % (url, sep, int(time.time() * 1000))
    h = probes.PROBE_HEADERS

    status("1 plain NTLM (like test.py)", url, auth=auth())
    status("2 + probe headers", url, auth=auth(), headers=h)
    status("3 + cache-bust ?_hc=", bust, auth=auth(), headers=h)
    status("4 + no redirects", bust, auth=auth(), headers=h,
           allow_redirects=False)
    status("5 + stream=True (= probe's NTLM request)", bust, auth=auth(),
           headers=h, allow_redirects=False, stream=True)
    status("6 no cache-bust, rest as probe", url, auth=auth(), headers=h,
           allow_redirects=False, stream=True)

    out, challenged = probes._attempt_url(bust, time.monotonic(), auth=None)
    print("%-46s %s | NTLM retry triggered: %s"
          % ("7 probe anonymous attempt", out.reason, challenged))
    out, _ = probes._attempt_url(bust, time.monotonic(), auth=auth())
    print("%-46s %s" % ("8 probe NTLM attempt", out.reason))
    print("%-46s %s" % ("9 full probe (_do_url_probe)",
                        probes._do_url_probe(url).reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
