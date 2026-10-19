#!/usr/bin/env python3
"""Microsoft Edge click/network performance recorder (Python 3.9, stdlib only).

Requires: Windows, Microsoft Edge, permission to launch Edge with local CDP.
No Selenium, Playwright, WebDriver, pip install, or application-code changes.

Use:
    py -3.9 edge_performance_recorder.py --app1 "https://app1.example.local/" --app2 "https://app2.example.local/"

Navigate and click in the browser tab that opens. Press Ctrl+C in the terminal
when finished. CSV reports are written to ./edge_perf_results_<timestamp>/.
"""

import argparse
import base64
import csv
import datetime as dt
import hashlib
import json
import os
import select
import shutil
import statistics
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


# A click is complete when its tracked network requests finish and there have
# been no new tracked requests for QUIET_MS. This is NOT exact visual readiness.
QUIET_MS = 1200
NO_REQUEST_WAIT_MS = 2000
MAX_ACTION_MS = 120000
MAX_WS_FRAME_BYTES = 16 * 1024 * 1024

# Change to True if your application uses custom clickable divs rather than
# normal HTML buttons, links, or accessible role=button controls.
RECORD_ALL_CLICKS = False

INJECT_JS = r"""
(function () {
  if (window.__edgePerfRecorderInstalled) return;
  window.__edgePerfRecorderInstalled = true;
  var allClicks = __ALL_CLICKS__;
  document.addEventListener('click', function(ev) {
    try {
      var target = ev.target;
      if (!target || !target.closest) return;
      var hit = target.closest('button, a, input[type="submit"], input[type="button"], input[type="image"], [role="button"], [role="tab"], [onclick], [tabindex]');
      if (!hit && !allClicks) return;
      var el = hit || target;
      var raw = el.getAttribute('aria-label') || el.innerText || el.value ||
                el.title || el.id || el.getAttribute('name') || el.tagName;
      var label = String(raw || 'Unnamed control').replace(/\s+/g, ' ').trim().slice(0, 100);
      var url = location.origin + location.pathname;
      window.__edgePerfNotify(JSON.stringify({ label: label, url: url, timeMs: Date.now() }));
    } catch (ex) { /* Don't interfere with application behavior. */ }
  }, true);
})();
""".replace('__ALL_CLICKS__', 'true' if RECORD_ALL_CLICKS else 'false')


def clean_url(raw):
    """Never record query strings or URL fragments (potentially sensitive)."""
    try:
        parts = urlsplit(raw or '')
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except ValueError:
        return '[invalid URL]'


def find_edge():
    paths = [
        shutil.which('msedge'),
        os.path.join(os.environ.get('PROGRAMFILES(X86)', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    ]
    for path in paths:
        if path and os.path.isfile(path):
            return path
    raise RuntimeError('Microsoft Edge (msedge.exe) was not found on this computer.')


def wait_for_debugging_port(profile, process, seconds=25):
    port_file = Path(profile) / 'DevToolsActivePort'
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_file.exists():
            lines = port_file.read_text(encoding='utf-8', errors='replace').splitlines()
            if lines and lines[0].isdigit():
                return int(lines[0])
        if process.poll() is not None:
            raise RuntimeError('Edge exited before it opened the debugging endpoint.')
        time.sleep(0.2)
    raise RuntimeError('Edge did not enable local debugging. IT browser policy may block it.')


class WSClient:
    """Minimal RFC 6455 WebSocket client for localhost CDP, stdlib only."""

    def __init__(self, ws_url):
        parsed = urlsplit(ws_url)
        if parsed.scheme != 'ws' or parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError('Refusing to connect to a non-local debugging WebSocket.')
        host = parsed.hostname
        port = parsed.port or 80
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(10)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (
            'GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n'
            'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
            'Sec-WebSocket-Key: %s\r\n\r\n'
        ) % (path, host, port, key)
        self.sock.sendall(request.encode('ascii'))
        response = bytearray()
        while b'\r\n\r\n' not in response:
            block = self.sock.recv(4096)
            if not block:
                raise ConnectionError('Browser closed during WebSocket handshake.')
            response.extend(block)
            if len(response) > 65536:
                raise RuntimeError('Oversized WebSocket handshake.')
        hdrs, leftovers = bytes(response).split(b'\r\n\r\n', 1)
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        if not hdrs.startswith(b'HTTP/1.1 101') or ('sec-websocket-accept: ' + expected).lower().encode() not in hdrs.lower():
            raise RuntimeError('CDP WebSocket handshake was rejected: %r' % hdrs[:250])
        self.buffer = bytearray(leftovers)
        self.sock.settimeout(15)

    def _exact(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buffer)))
            if not chunk:
                raise ConnectionError('Edge browser connection closed.')
            self.buffer.extend(chunk)
        out = bytes(self.buffer[:n])
        del self.buffer[:n]
        return out

    def send_frame(self, opcode, payload):
        # Browser clients MUST mask WebSocket frames.
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            length_part = bytes((0x80 | n,))
        elif n < 65536:
            length_part = bytes((0x80 | 126,)) + struct.pack('!H', n)
        else:
            length_part = bytes((0x80 | 127,)) + struct.pack('!Q', n)
        payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode,)) + length_part + mask + payload)

    def send_json(self, obj):
        self.send_frame(1, json.dumps(obj, separators=(',', ':')).encode('utf-8'))

    def recv_json(self, wait_seconds=0.2):
        if not self.buffer:
            readable, _, _ = select.select([self.sock], [], [], wait_seconds)
            if not readable:
                return None
        message = bytearray()
        started = False
        while True:
            b1, b2 = self._exact(2)
            opcode, fin = b1 & 0x0f, bool(b1 & 0x80)
            size = b2 & 0x7f
            if size == 126:
                size = struct.unpack('!H', self._exact(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self._exact(8))[0]
            if size > MAX_WS_FRAME_BYTES:
                raise RuntimeError('WebSocket message exceeds safety limit.')
            mask = self._exact(4) if b2 & 0x80 else None
            payload = self._exact(size)
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode == 8:
                raise ConnectionError('Edge closed the debugging WebSocket.')
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                started = True
            elif opcode != 0 or not started:
                continue
            message.extend(payload)
            if len(message) > MAX_WS_FRAME_BYTES:
                raise RuntimeError('WebSocket message exceeds safety limit.')
            if fin:
                return json.loads(message.decode('utf-8'))

    def close(self):
        try:
            self.send_frame(8, b'')
        except (OSError, ConnectionError):
            pass
        self.sock.close()


class Recorder:
    def __init__(self, ws, app1='', app2=''):
        self.ws = ws
        self.app_urls = {'Application 1': clean_url(app1), 'Application 2': clean_url(app2)}
        self.seq = 0
        self.cmd_seq = 0
        self.commands = {}
        self.active = None
        self.completed = []
        self.reqs = {}
        self.main_frame = None
        self.cdp_offset_ms = None

    def app_name(self, url):
        # Longest match wins when applications are on the same domain.
        matches = [(len(base), name) for name, base in self.app_urls.items()
                   if base and clean_url(url).startswith(base)]
        if matches:
            return max(matches)[1]
        return urlsplit(url).hostname or 'Unknown'

    def cmd(self, name, params=None):
        self.cmd_seq += 1
        self.commands[self.cmd_seq] = name
        self.ws.send_json({'id': self.cmd_seq, 'method': name, 'params': params or {}})

    def start(self):
        self.cmd('Runtime.enable')
        self.cmd('Page.enable')
        self.cmd('Network.enable', {'maxTotalBufferSize': 10000000})
        self.cmd('Runtime.addBinding', {'name': '__edgePerfNotify'})
        self.cmd('Page.addScriptToEvaluateOnNewDocument', {'source': INJECT_JS})
        self.cmd('Runtime.evaluate', {'expression': INJECT_JS})
        self.cmd('Page.getFrameTree')

    def new_action(self, label, page, start_ms, kind='click'):
        if self.active:
            self.finish_action('INTERRUPTED_BY_NEXT_ACTION')
        self.seq += 1
        action = dict(id=self.seq, kind=kind, app=self.app_name(page), page=clean_url(page),
                      label=label[:110], started_ms=start_ms, requests=[], pending=set(),
                      last_activity_ms=start_ms, last_end_ms=None, dom_ms=None, load_ms=None)
        self.active = action
        print('[%03d] %-18s %-35s %s' %
              (action['id'], action['app'][:18], action['label'][:35], action['page'][:90]), flush=True)
        return action

    def on_event(self, message):
        if 'id' in message:
            method = self.commands.pop(message['id'], 'unknown')
            if 'error' in message:
                print('DevTools error for %s: %s' % (method, message['error']), file=sys.stderr)
                if method in ('Runtime.addBinding', 'Page.addScriptToEvaluateOnNewDocument', 'Network.enable'):
                    raise RuntimeError('Required Edge DevTools feature not available: ' + method)
            if method == 'Page.getFrameTree':
                self.main_frame = message.get('result', {}).get('frameTree', {}).get('frame', {}).get('id')
            return
        method = message.get('method')
        p = message.get('params', {})
        if method == 'Page.frameNavigated':
            frame = p.get('frame', {})
            if not frame.get('parentId'):
                self.main_frame = frame.get('id', self.main_frame)
        elif method == 'Runtime.bindingCalled':
            if p.get('name') == '__edgePerfNotify':
                try:
                    body = json.loads(p['payload'])
                    self.new_action(body.get('label') or 'Unnamed click',
                                    body.get('url') or '', float(body.get('timeMs', time.time() * 1000)))
                except (ValueError, KeyError, TypeError) as ex:
                    print('Could not parse a click event: %s' % ex, file=sys.stderr)
        elif method == 'Network.requestWillBeSent':
            self.request_start(p)
        elif method == 'Network.responseReceived':
            req = self.reqs.get(p.get('requestId'))
            if req:
                response = p.get('response') or {}
                req['status'] = response.get('status', '')
                timings = response.get('timing') or {}
                if 'receiveHeadersEnd' in timings and 'sendEnd' in timings:
                    req['wait_ttfb_ms'] = max(0, round(timings['receiveHeadersEnd'] - timings['sendEnd'], 1))
        elif method in ('Network.loadingFinished', 'Network.loadingFailed'):
            self.request_done(p, failed=(method == 'Network.loadingFailed'))
        elif method in ('Page.domContentEventFired', 'Page.loadEventFired'):
            a = self.active
            if a and self.cdp_offset_ms is not None and 'timestamp' in p:
                event_ms = float(p['timestamp']) * 1000 + self.cdp_offset_ms
                delta = round(event_ms - a['started_ms'])
                if delta >= 0:
                    a['dom_ms' if method == 'Page.domContentEventFired' else 'load_ms'] = delta

    def request_start(self, p):
        rid = p.get('requestId')
        if not rid:
            return
        ts = float(p.get('timestamp', 0))
        wall = float(p.get('wallTime', time.time())) * 1000
        if ts:
            self.cdp_offset_ms = wall - ts * 1000
        request = p.get('request') or {}
        url = clean_url(request.get('url', ''))
        if not url.startswith(('http://', 'https://')):
            return
        kind = p.get('type', '')
        if (kind == 'Document' and not self.active and
                (not self.main_frame or p.get('frameId') == self.main_frame)):
            self.new_action('Direct page navigation / refresh', url, wall, 'navigation')
        action = self.active
        if not action or wall < action['started_ms'] - 150:
            return
        # The new request belongs to the active click/navigation.
        req = dict(id=rid, action=action, method=request.get('method', ''), url=url,
                   type=kind, started_ts=ts, started_ms=wall, status='',
                   duration_ms=None, wait_ttfb_ms=None, bytes_received=0, error='')
        self.reqs[rid] = req
        action['requests'].append(req)
        action['pending'].add(rid)
        action['last_activity_ms'] = max(action['last_activity_ms'], wall)

    def request_done(self, p, failed=False):
        rid = p.get('requestId')
        req = self.reqs.pop(rid, None)
        if not req:
            return
        end_ts = float(p.get('timestamp', req['started_ts']))
        elapsed = max(0, (end_ts - req['started_ts']) * 1000)
        req['duration_ms'] = round(elapsed, 1)
        req['bytes_received'] = p.get('encodedDataLength', 0) if not failed else 0
        if failed:
            req['error'] = p.get('errorText') or 'Network failed'
        action = req['action']
        action['pending'].discard(rid)
        end_wall = req['started_ms'] + elapsed
        action['last_end_ms'] = max(action['last_end_ms'] or 0, end_wall)
        action['last_activity_ms'] = max(action['last_activity_ms'], end_wall)

    def tick(self):
        action = self.active
        if not action:
            return
        now = time.time() * 1000
        if now - action['started_ms'] > MAX_ACTION_MS:
            self.finish_action('TIMEOUT')
        elif action['pending']:
            return
        elif action['requests'] and now - action['last_activity_ms'] >= QUIET_MS:
            self.finish_action('NETWORK_QUIET')
        elif not action['requests'] and now - action['started_ms'] >= NO_REQUEST_WAIT_MS:
            self.finish_action('NO_NETWORK_OBSERVED')

    def finish_action(self, reason):
        action = self.active
        if not action:
            return
        self.active = None
        # The duration here is from click to LAST network completion, not the
        # 1.2-second confirmation window used before declaring network quiet.
        completed_ms = None
        if action['last_end_ms'] is not None:
            completed_ms = round(max(0, action['last_end_ms'] - action['started_ms']))
        ended_requests = [r for r in action['requests'] if r['duration_ms'] is not None]
        slow = max(ended_requests, key=lambda r: r['duration_ms']) if ended_requests else None
        result = {
            'action_id': action['id'],
            'started_local': dt.datetime.fromtimestamp(action['started_ms'] / 1000).astimezone().isoformat(timespec='seconds'),
            'application': action['app'], 'page_url_no_query': action['page'],
            'action': action['label'], 'action_type': action['kind'], 'outcome': reason,
            'click_to_last_network_ms': completed_ms if reason == 'NETWORK_QUIET' else '',
            'DOMContentLoaded_after_click_ms': action['dom_ms'] if action['dom_ms'] is not None else '',
            'load_event_after_click_ms': action['load_ms'] if action['load_ms'] is not None else '',
            'request_count': len(action['requests']),
            'completed_request_count': len(ended_requests),
            'slowest_request_ms': slow['duration_ms'] if slow else '',
            'slowest_request_url_no_query': slow['url'] if slow else '',
            'total_received_kb': round(sum(float(r['bytes_received'] or 0) for r in ended_requests) / 1024, 1),
            'failed_requests': sum(bool(r['error']) or (isinstance(r['status'], (int, float)) and r['status'] >= 400)
                                   for r in action['requests']),
        }
        self.completed.append((result, action['requests']))
        shown = str(result['click_to_last_network_ms']) + ' ms' if result['click_to_last_network_ms'] != '' else 'not measured'
        print('      -> %-27s network: %-15s requests: %d, slowest: %s ms' %
              (reason, shown, len(action['requests']), result['slowest_request_ms']), flush=True)

    def export(self, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        actions_path = output_dir / 'actions.csv'
        requests_path = output_dir / 'requests.csv'
        sample_action = {
            'action_id': '', 'started_local': '', 'application': '', 'page_url_no_query': '',
            'action': '', 'action_type': '', 'outcome': '', 'click_to_last_network_ms': '',
            'DOMContentLoaded_after_click_ms': '', 'load_event_after_click_ms': '',
            'request_count': '', 'completed_request_count': '', 'slowest_request_ms': '',
            'slowest_request_url_no_query': '', 'total_received_kb': '', 'failed_requests': '',
        }
        with actions_path.open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(sample_action))
            writer.writeheader()
            for result, _ in self.completed:
                writer.writerow(result)
        with requests_path.open('w', newline='', encoding='utf-8-sig') as f:
            fields = ['action_id', 'application', 'action', 'request_method', 'request_type',
                      'request_url_no_query', 'http_status', 'duration_ms',
                      'waiting_ttfb_ms', 'received_kb', 'error']
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for result, requests in self.completed:
                for req in requests:
                    writer.writerow({
                        'action_id': result['action_id'], 'application': result['application'],
                        'action': result['action'], 'request_method': req['method'],
                        'request_type': req['type'], 'request_url_no_query': req['url'],
                        'http_status': req['status'], 'duration_ms': req['duration_ms'],
                        'waiting_ttfb_ms': req['wait_ttfb_ms'],
                        'received_kb': round(float(req['bytes_received'] or 0) / 1024, 1),
                        'error': req['error'],
                    })
        comparison_path = output_dir / 'comparison.csv'
        groups = {}
        for row, _ in self.completed:
            if row['outcome'] != 'NETWORK_QUIET' or row['click_to_last_network_ms'] == '':
                continue
            key = row['action'].strip().lower()
            groups.setdefault(key, {}).setdefault(row['application'], []).append(
                int(row['click_to_last_network_ms']))
        with comparison_path.open('w', newline='', encoding='utf-8-sig') as f:
            fields = ['action', 'app1_samples', 'app1_median_ms', 'app2_samples',
                      'app2_median_ms', 'app2_minus_app1_ms', 'slower_application']
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for label, apps in sorted(groups.items()):
                vals1 = apps.get('Application 1', [])
                vals2 = apps.get('Application 2', [])
                med1 = round(statistics.median(vals1), 1) if vals1 else ''
                med2 = round(statistics.median(vals2), 1) if vals2 else ''
                delta = round(med2 - med1, 1) if vals1 and vals2 else ''
                writer.writerow({
                    'action': label, 'app1_samples': len(vals1), 'app1_median_ms': med1,
                    'app2_samples': len(vals2), 'app2_median_ms': med2,
                    'app2_minus_app1_ms': delta,
                    'slower_application': ('Application 2' if delta > 0 else
                                           'Application 1' if delta < 0 else 'Tied')
                                          if delta != '' else '',
                })
        return actions_path, requests_path, comparison_path


def main():
    parser = argparse.ArgumentParser(description='Record user clicks and Edge browser network timings (Python 3.9, no dependencies).')
    parser.add_argument('--app1', default='', help='First on-premise application URL, for labelling and launch')
    parser.add_argument('--app2', default='', help='Second on-premise application URL, for labelling')
    args = parser.parse_args()
    if sys.platform != 'win32':
        print('This recorder launches Microsoft Edge on Windows. Running OS: %s' % sys.platform, file=sys.stderr)
        return 2
    ws = None
    process = None
    recorder = None
    profile = tempfile.mkdtemp(prefix='edge_perf_profile_')
    output = Path.cwd() / ('edge_perf_results_' + dt.datetime.now().strftime('%Y%m%d_%H%M%S'))
    print('Opening a separate Edge browser profile for this test.')
    print('Please use the SAME newly opened tab for both applications.')
    try:
        edge = find_edge()
        process = subprocess.Popen([
            edge, '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
            '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check',
            '--new-window', 'about:blank'
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port = wait_for_debugging_port(profile, process)
        with urllib.request.urlopen('http://127.0.0.1:%d/json/list' % port, timeout=5) as res:
            targets = json.load(res)
        pages = [t for t in targets if t.get('type') == 'page' and t.get('webSocketDebuggerUrl')]
        if not pages:
            raise RuntimeError('No debuggable Edge page found.')
        pages.sort(key=lambda p: p.get('url') != 'about:blank')
        ws = WSClient(pages[0]['webSocketDebuggerUrl'])
        recorder = Recorder(ws, args.app1, args.app2)
        recorder.start()
        print('Recording. Use the newly opened Edge tab to log in and click through your application.')
        print('To switch applications, navigate to the second URL IN THE SAME TAB.')
        print('Press Ctrl+C in this console to stop recording and save CSV files.\n')
        if args.app1:
            # Delaying page navigation until after instrumentation is attached.
            recorder.cmd('Page.navigate', {'url': args.app1})
        while True:
            event = ws.recv_json(0.2)
            if event is not None:
                recorder.on_event(event)
            recorder.tick()
    except KeyboardInterrupt:
        print('\nStopped by user.')
    except (RuntimeError, OSError, ConnectionError, ValueError) as ex:
        print('\nERROR: %s' % ex, file=sys.stderr)
    finally:
        if recorder:
            if recorder.active:
                recorder.finish_action('STOPPED_BEFORE_COMPLETION')
            paths = recorder.export(output)
            print('\nSaved %d actions. Reports:\n  %s\n  %s\n  %s' % (len(recorder.completed), *paths))
        if ws:
            ws.close()
        if process:
            # Terminate only the dedicated Edge process we started, not existing normal Edge sessions.
            try:
                process.terminate()
                process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            shutil.rmtree(profile)
        except OSError:
            print('Temporary Edge profile could not be deleted; close test Edge and delete: %s' % profile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
