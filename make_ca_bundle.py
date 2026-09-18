"""make_ca_bundle.py - build the PEM file that CORPORATE_CA_BUNDLE points at.

Connects to a monitored HTTPS endpoint, collects the certificate chain above
the leaf (intermediates + root), writes them as PEM and - unless told not to -
appends certifi's public root store.

That last step is the whole point. requests treats verify=<path> as a
REPLACEMENT trust store, not an addition: a bundle holding only the internal
root makes every public HTTPS monitor in this install start failing. The
combined bundle keeps both working.

Servers usually send their intermediates but not the root, so the root is
looked up in the Windows certificate stores. Run this ON the monitoring host
(SV301077) - it is domain-joined, so the corporate CA is already installed
there by GPO, and it is where the bundle has to end up anyway.

Targets Python 3.9+ so it runs on the production Anaconda as well as on a
developer machine.

Usage:
    python make_ca_bundle.py --url https://host.volt.local:4443/path
                             --out E:\\ApplicationDashboard\\certs\\corporate-ca.pem

Then set the printed path in .env and restart BOTH the IIS app pool and the
health-monitor Windows service - modules/application_health_check/utils/
common.py resolves CORPORATE_CA_BUNDLE once at import time, so a running
process never picks up the change.
"""

import argparse
import base64
import datetime
import os
import socket
import ssl
import sys
import warnings
from urllib.parse import urlparse

try:
    from cryptography import x509
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
except ImportError:
    sys.exit("This script needs the 'cryptography' package: pip install cryptography")


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------

def load(der):
    """Parse a DER blob into an x509 certificate object."""
    return x509.load_der_x509_certificate(der, default_backend())


def name(x509_name):
    """Human-readable RDN string, falling back to the raw form."""
    try:
        return x509_name.rfc4514_string()
    except Exception:
        return str(x509_name)


def expiry(cert):
    """not_valid_after, tolerating the cryptography 42+ rename."""
    try:
        return cert.not_valid_after_utc.replace(tzinfo=None)
    except AttributeError:
        return cert.not_valid_after


def is_self_signed(cert):
    return cert.subject == cert.issuer


def _der_of(entry):
    """DER bytes from a get_unverified_chain() element that is not raw bytes.

    The DER constant lives on the private _ssl module, not on ssl itself.
    """
    import _ssl
    return entry.public_bytes(_ssl.ENCODING_DER)


def to_pem(der):
    """Render a DER certificate as an annotated PEM block."""
    cert = load(der)
    body = base64.b64encode(der).decode('ascii')
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return (
        "# Subject: %s\n"
        "# Issuer : %s\n"
        "# Expires: %s\n"
        "-----BEGIN CERTIFICATE-----\n%s\n-----END CERTIFICATE-----\n"
        % (name(cert.subject), name(cert.issuer),
           expiry(cert).strftime('%Y-%m-%d'), "\n".join(lines))
    )


# ---------------------------------------------------------------------------
# Step 1 - what the server presents
# ---------------------------------------------------------------------------

def fetch_from_server(host, port, timeout=15):
    """Return (leaf_der, [extra_der, ...]) straight off the TLS handshake.

    Verification is deliberately off: the whole reason we are here is that the
    chain does not validate yet. Nothing fetched is trusted blindly - it is
    matched against the Windows stores below, and the requests check at the
    end is what actually proves the bundle.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as sock:
            leaf = sock.getpeercert(binary_form=True)
            extras = []
            # get_unverified_chain() is Python 3.10+; on 3.9 we rely purely on
            # the Windows stores to supply the intermediates. Its element type
            # is not stable across builds - some return raw DER bytes, others
            # a Certificate object - so handle both rather than trusting one.
            getter = getattr(sock, 'get_unverified_chain', None)
            if getter is not None:
                try:
                    for entry in (getter() or []):
                        extras.append(entry if isinstance(entry, bytes) else _der_of(entry))
                except Exception as exc:
                    print("  ? could not read the served chain (%s) - using the "
                          "Windows stores only." % exc)
                    extras = []
    # The served chain usually repeats the leaf as its first element.
    return leaf, [der for der in extras if der and der != leaf]


# ---------------------------------------------------------------------------
# Step 2 - what the machine already trusts
# ---------------------------------------------------------------------------

def windows_ca_pool():
    """DER certificates from the Windows ROOT and CA stores, keyed by subject.

    ssl.enum_certificates is Windows-only. On any other platform this returns
    an empty pool and the chain is built from what the server sent.
    """
    pool = {}
    if not hasattr(ssl, 'enum_certificates'):
        return pool
    # A machine store routinely holds certificates that trip cryptography's
    # RFC-5280 deprecation warnings (zero or negative serials, most often on
    # old internal CAs). We are only reading subjects here, so keep the
    # output legible instead of printing a warning per certificate.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        for store in ('ROOT', 'CA'):
            try:
                entries = ssl.enum_certificates(store)
            except Exception:
                continue
            for der, encoding, _trust in entries:
                if encoding != 'x509_asn':
                    continue
                try:
                    pool.setdefault(name(load(der).subject), der)
                except Exception:
                    continue
    return pool


def build_chain(leaf_der, extra_ders, pool, max_depth=12):
    """Walk from the leaf up to a self-signed root, collecting the CAs.

    Issuer/subject name matching, not signature verification - good enough to
    assemble a candidate bundle, and the requests check at the end is what
    decides whether it is right.
    """
    candidates = dict(pool)
    for der in extra_ders:
        try:
            candidates.setdefault(name(load(der).subject), der)
        except Exception:
            continue

    chain = []
    seen = set()
    current = load(leaf_der)
    for _ in range(max_depth):
        if is_self_signed(current):
            break
        issuer_der = candidates.get(name(current.issuer))
        if issuer_der is None:
            print("  ! issuer not found locally: %s" % name(current.issuer))
            print("    Install the corporate CA on this machine, or run this")
            print("    script on the monitoring host where it is already trusted.")
            break
        issuer = load(issuer_der)
        fingerprint = issuer.fingerprint(hashes.SHA256())
        if fingerprint in seen:
            break
        seen.add(fingerprint)
        chain.append(issuer_der)
        current = issuer
    return chain


# ---------------------------------------------------------------------------
# Step 3 - prove it before anyone edits .env
# ---------------------------------------------------------------------------

def verify(url, bundle_path, public_probe):
    """Re-run the real request path against the new bundle.

    Only an SSLError means the bundle is wrong. A timeout, refused connection
    or proxy error says nothing about trust - a locked-down monitoring host
    routinely cannot reach the public probe at all - so those are reported
    but do not fail the run.
    """
    try:
        import requests
    except ImportError:
        print('  ?    requests is not installed here - skipping verification.')
        return True

    def probe(label, target, fatal_note):
        try:
            response = requests.get(target, verify=bundle_path, timeout=20,
                                    allow_redirects=False)
            print("  OK   %s - HTTP %s" % (label, response.status_code))
            return True
        except requests.exceptions.SSLError as exc:
            print("  FAIL %s: %s" % (label, exc))
            print("       %s" % fatal_note)
            return False
        except Exception as exc:
            print("  ?    %s could not be reached (%s)." % (label, exc))
            print('       Not a trust failure - the bundle was not disproved.')
            return True

    ok = probe('target trusted', url,
               'The bundle does not cover this endpoint.')

    if public_probe and urlparse(public_probe).hostname == urlparse(url).hostname:
        print('  -    public probe skipped: same host as the target.')
    elif public_probe:
        ok = probe('public HTTPS still trusted', public_probe,
                   'This bundle breaks public HTTPS monitors. Re-run '
                   'without --no-public-roots.') and ok
    return ok


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a CORPORATE_CA_BUNDLE PEM from a monitored HTTPS endpoint.')
    parser.add_argument('--url', required=True,
                        help='A monitored HTTPS endpoint issued by the corporate CA.')
    parser.add_argument('--out', default='corporate-ca.pem',
                        help='Destination PEM (default: ./corporate-ca.pem).')
    parser.add_argument('--no-public-roots', action='store_true',
                        help='Do not append certifi. Only safe when EVERY monitored '
                             'HTTPS target is issued internally.')
    parser.add_argument('--include-leaf', action='store_true',
                        help="Also write the server's own certificate. Set automatically "
                             'when the endpoint is self-signed with no CA above it.')
    parser.add_argument('--skip-verify', action='store_true',
                        help='Write the bundle without testing it.')
    parser.add_argument('--public-probe', default='https://www.google.com',
                        help='Public HTTPS URL used to confirm the bundle has not '
                             'broken externally-issued monitors. Point it at any '
                             'public site this host can reach, or pass an empty '
                             'string to skip that check.')
    args = parser.parse_args(argv)

    parsed = urlparse(args.url)
    if parsed.scheme != 'https' or not parsed.hostname:
        sys.exit('--url must be an https:// URL with a hostname.')
    port = parsed.port or 443

    print("Connecting to %s:%s ..." % (parsed.hostname, port))
    leaf_der, extra_ders = fetch_from_server(parsed.hostname, port)
    leaf = load(leaf_der)
    print("  leaf: %s" % name(leaf.subject))
    print("  server also sent %d intermediate(s)" % len(extra_ders))

    pool = windows_ca_pool()
    print("  windows trust stores: %d CA certificate(s)" % len(pool))
    if not pool and os.name == 'nt':
        print("  ! Could not read the Windows stores - the root may be missing.")

    chain = build_chain(leaf_der, extra_ders, pool)
    include_leaf = args.include_leaf
    if not chain:
        print('  ! No CA found above the leaf - treating the endpoint as self-signed.')
        include_leaf = True

    wanted = ([leaf_der] if include_leaf else []) + chain
    if not wanted:
        sys.exit('Nothing to export.')

    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    parts = [
        '# Corporate CA bundle for CORPORATE_CA_BUNDLE\n'
        '# Generated %s from %s\n'
        '# Regenerate with make_ca_bundle.py before the roots below expire.\n\n'
        % (stamp, args.url)
    ]
    for der in wanted:
        cert = load(der)
        print("  + %s  (expires %s)" % (name(cert.subject), expiry(cert).strftime('%Y-%m-%d')))
        parts.append(to_pem(der) + "\n")

    if args.no_public_roots:
        print('  ! Public roots omitted - every monitored HTTPS target must now be')
        print('    issued by the CAs above, or its check will fail.')
    else:
        try:
            import certifi
        except ImportError:
            sys.exit('certifi is not installed. pip install certifi, or pass --no-public-roots.')
        print("  + public roots from %s" % certifi.where())
        parts.append('# ---- certifi public roots ----\n')
        with open(certifi.where(), 'r', encoding='ascii', errors='replace') as handle:
            parts.append(handle.read())

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # PEM is ASCII by definition, and a BOM breaks OpenSSL-style parsers.
    with open(args.out, 'w', encoding='ascii', errors='replace', newline='\n') as handle:
        handle.write(''.join(parts))

    resolved = os.path.abspath(args.out)
    print("\nWrote %s" % resolved)

    if not args.skip_verify:
        print('Verifying ...')
        if not verify(args.url, resolved, args.public_probe):
            print('\nVerification failed - do NOT point CORPORATE_CA_BUNDLE at this file yet.')
            return 1

    print('\nSet this in .env, then restart BOTH the IIS app pool and the')
    print('health-monitor Windows service:')
    print("  CORPORATE_CA_BUNDLE=%s" % resolved)
    return 0


if __name__ == '__main__':
    sys.exit(main())
