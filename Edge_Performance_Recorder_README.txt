EDGE APPLICATION PERFORMANCE RECORDER - PYTHON 3.9
================================================

REQUIREMENTS
- Windows with Microsoft Edge already installed.
- Python 3.9 already installed.
- Organisational permission to run Python and launch Edge with local DevTools debugging.
- NO pip install, Selenium, WebDriver, browser extensions, or application changes.

HOW TO RUN (PowerShell or Command Prompt)
1. Save edge_performance_recorder.py to a folder (e.g. C:\PerfTest).
2. Open PowerShell in that folder.
3. Run one of the commands below. Replace example addresses with internal URLs.

   py -3.9 edge_performance_recorder.py --app1 "https://app1.example.local/" --app2 "https://app2.example.local/"

   Or if 'py' isn't available:
   python edge_performance_recorder.py --app1 "https://app1.example.local/" --app2 "https://app2.example.local/"

4. A separate Edge test window opens. Sign in if prompted.
5. Interact with application 1 (open enquiries, click Search, Details, etc.).
6. In that SAME TAB, navigate to application 2; repeat the same actions.
7. Press Ctrl+C in the PowerShell/CMD window to stop and export.

OUTPUT (a newly created 'edge_perf_results_YYYYMMDD_HHMMSS' folder)
- actions.csv: every detected click/navigation and associated elapsed timings.
- requests.csv: each tracked network request, HTTP status, URL without query,
  duration, approximate waiting/TTFB portion, received bytes, errors.
- comparison.csv: paired median durations for identical action labels on app 1
  and app 2. For meaningful comparisons, perform each action about 5 times.

IMPORTANT MEASUREMENT NOTES
- 'click_to_last_network_ms' measures from click to the completion of the last
  request associated with the action. This is not exact visual-rendering time.
- Recorder confirms 1.2 seconds of network quiet before finalizing an action.
  That quiet confirmation time is NOT included in the reported duration.
- Clicks without a corresponding request show 'NO_NETWORK_OBSERVED' and a
  blank measurement; client-only UI rendering cannot be measured this way.
- Delayed requests starting after the quiet period may not be associated with
  the click. Background polling and long-running requests may affect results.
- Only the Edge tab originally opened by the script is tracked.
- Requests inside service workers / other tabs may not be fully captured.
- Full page DOMContentLoaded and Load events are also reported when observed.
- URLs in CSV have query strings and #fragments removed, but URL paths,
  button labels, and other metadata can still reveal confidential information.
  Keep the reports on approved internal storage and check them before sharing.

TROUBLESHOOTING
- No click records? The application may use nonstandard custom controls.
  Edit RECORD_ALL_CLICKS = False to True near the top of the script.
- A console error mentions remote debugging? Your enterprise Edge policy may
  block it. Ask your IT/security team; do not bypass that policy.
- It opens a separate browser profile deliberately (current versions of Edge
  may not allow remote debugging of a normal/default profile). Sign in there.
- Empty comparison.csv? Make sure you've run the same clickable action names
  on both apps and both actions were classified NETWORK_QUIET.
- Test under similar network/PC conditions and record cold/warm load separately.
- This is a prototype validated with synthetic DevTools events; it has not been
  exercised against your Windows workstation and private web applications.

SECURITY
- Edge debugging listens only on local loopback, on an ephemeral port.
- A separate temporary browser profile is used and deletion is attempted on exit.
- Close the test browser when done. Any lingering temporary profile should be
  removed after the browser fully closes.
- Remote debugging access is powerful: run only with organisational approval.
