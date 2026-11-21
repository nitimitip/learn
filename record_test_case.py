"""Test case recorder for the QA Automation Portal.

Opens Edge (or Chrome) on the application URL, watches what you do, and writes a
ready-to-import Excel test case in the portal's import format (sheet "Test Steps",
the same 12 columns as the portal's import template).

    python tools/record_test_case.py            (or double-click "Record Test Case.bat")

opens a start window: sign in to the portal, choose the application, name the test
case and press Start recording. When you press Finish the test case is sent straight
to the portal; the Excel file is still saved as a fallback. The command line works
as before:

    python tools/record_test_case.py --url https://host/app --name "Create order"

While recording:
    * Use the application normally: clicks, typing, dropdowns, check boxes, file
      uploads, drag and drop, Enter/Escape keys, pop-up windows, browser dialogs,
      print and downloads are recorded.
    * Toolbar at the bottom right: Add check (then click the thing to check), Undo
      (remove the last step), Pause / Resume (nothing is recorded while paused, for
      example while you sign in), More (address check, wait, scroll, block,
      screenshot) and Finish.
    * Alt+Right-Click an element opens the keyword menu: checks and soft checks,
      save text / a regex match / a count to a variable, hover, wait until visible,
      click if visible, scroll into view, element screenshot, run a block if visible.
    * Shift+Alt+Click an element to add a quick check instead of clicking it: text ->
      VERIFY_TEXT_CONTAINS, field -> VERIFY_VALUE, no text -> VERIFY_ELEMENT_VISIBLE.
    * Shift+Alt+S adds a CAPTURE_SCREENSHOT step.
    * --block, --depends-on and --teardown fill the test case's own columns.
    * Press Finish (or close the browser window, or Ctrl+C here) to end. The workbook
      is saved after every step as well, so nothing is lost if the browser crashes.

Then in the portal: Applications > your application > Test cases > Import, choose
the .xlsx. Set the application's Base URL to the address you recorded against:
same-site addresses are written as relative paths so the test case also runs on
other environments.

Passwords are never written to the file: a password field becomes {AUTH_PASSWORD}
and the user name typed just before it becomes {AUTH_USERNAME} (use
--keep-typed-username to keep what you typed). Runs with Python 3.9+; needs only
playwright and openpyxl (both in the portal requirements).
"""
import argparse
import configparser
import getpass
import http.cookiejar
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# Packaged as one .exe the code is unpacked to a temporary folder, so the folder that
# matters is the one the .exe sits in.
TOOL_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PORTAL_DIR = TOOL_DIR.parent
# Inside the portal the recordings folder is beside it; on a tester's laptop there is no
# portal folder, so the workbooks go to the tester's own folder.
IN_PORTAL = (PORTAL_DIR / "engine" / "keywords.py").is_file()
DEFAULT_OUT_DIR = PORTAL_DIR / "recordings" if IN_PORTAL else Path.home() / "QA Recordings"


# --------------------------------------------------------------------------
# Output location: read from tools_config.ini (next to this script) so the
# folder and file name can be changed without editing code.
# --------------------------------------------------------------------------
CONFIG_FILE = TOOL_DIR / 'tools_config.ini'


def read_output_config(section, config_file=None):
    """Return (output_folder, file_name, config_path) for one tool's section.

    Missing file, section or keys give empty strings, so the tool falls back to
    its built-in defaults. A --config file that does not exist is an error.
    """
    path = Path(config_file).expanduser() if config_file else CONFIG_FILE
    if not path.is_file():
        if config_file:
            raise SystemExit('Config file not found: %s' % path)
        return '', '', path
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(str(path), encoding='utf-8')
    except configparser.Error as ex:
        print('WARNING: could not read %s (%s); using default output settings.' % (path, ex), file=sys.stderr)
        return '', '', path
    if not parser.has_section(section):
        return '', '', path
    return (parser.get(section, 'output_folder', fallback='').strip(),
            parser.get(section, 'file_name', fallback='').strip(), path)


def build_output_path(section, default_folder, default_name, placeholders=None, config_file=None,
                      folder_override=''):
    """Folder + file name from the config, with _YYYYMMDD_HHMMSS.xlsx added now.

    folder_override (the --out option) wins over the config folder. A relative
    config folder is taken from the config file's own folder.
    """
    folder_text, name_text, cfg_path = read_output_config(section, config_file)
    if folder_override:
        folder = Path(folder_override).expanduser()
    elif folder_text:
        folder = Path(os.path.expandvars(folder_text)).expanduser()
        if not folder.is_absolute():
            folder = cfg_path.parent / folder
    else:
        folder = Path(default_folder)
    name = name_text or default_name
    for key, value in (placeholders or {}).items():
        name = name.replace('{' + key + '}', value or '')
    if name.lower().endswith('.xlsx'):
        name = name[:-5]
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', name).strip('._')[:80] or re.sub(
        r'[^A-Za-z0-9._-]+', '_', default_name).strip('._') or 'output'
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return folder.resolve() / ('%s_%s.xlsx' % (name, stamp))

IMPORT_COLUMNS = [
    "test_case_name", "test_case_description", "is_reusable_block", "depends_on", "step_order",
    "action_keyword", "locator_type", "locator_value", "input_data", "store_variable_name",
    "step_description", "teardown_block",
]
LOCATOR_TYPES = [
    "id", "id_ends_with", "name", "test_id", "label", "placeholder", "role", "text", "title",
    "row_action", "css", "xpath",
]
# Steps that do something to the page, as opposed to checks and window moves.
ACTION_KEYWORDS = {
    "CLICK", "CLICK_AND_SWITCH_WINDOW", "DOUBLE_CLICK", "FILL", "TYPE_SEQUENTIALLY",
    "SELECT_DROPDOWN", "CHECK", "UNCHECK", "PRESS_KEY", "UPLOAD_FILE", "HOVER", "CLICK_IF_VISIBLE",
    "DRAG_AND_DROP",
}
# What the toolbar and the Alt+Right-Click menu may add.
MANUAL_KEYWORDS = {
    "VERIFY_TEXT_CONTAINS", "VERIFY_VALUE", "VERIFY_ELEMENT_VISIBLE", "VERIFY_ELEMENT_NOT_VISIBLE",
    "VERIFY_ELEMENT_ENABLED", "VERIFY_ELEMENT_DISABLED", "VERIFY_ELEMENT_COUNT", "SOFT_VERIFY_TEXT_CONTAINS",
    "SOFT_VERIFY_VALUE", "SOFT_VERIFY_ELEMENT_VISIBLE", "STORE_TEXT", "STORE_INPUT_VALUE", "STORE_REGEX",
    "STORE_ELEMENT_COUNT", "HOVER", "WAIT_FOR_ELEMENT", "CLICK_IF_VISIBLE", "SCROLL_TO", "CAPTURE_SCREENSHOT",
    "CALL_BLOCK_IF_VISIBLE", "VERIFY_URL", "WAIT_SECONDS", "CALL_BLOCK",
}
# Used for the Keywords sheet when the portal's engine cannot be imported.
FALLBACK_KEYWORDS = [
    "NAVIGATE", "WAIT_SECONDS", "WAIT_FOR_ELEMENT", "SCROLL_TO", "SWITCH_TO_NEW_WINDOW", "CLOSE_WINDOW",
    "SWITCH_TO_WINDOW", "SWITCH_TO_FRAME", "SWITCH_TO_MAIN_FRAME", "CLICK", "CLICK_AND_SWITCH_WINDOW",
    "DOUBLE_CLICK", "HOVER", "DRAG_AND_DROP", "CLICK_IF_VISIBLE", "FILL_IF_VISIBLE", "PREPARE_DIALOG", "FILL",
    "TYPE_SEQUENTIALLY", "SELECT_DROPDOWN", "CHECK", "UNCHECK", "PRESS_KEY", "UPLOAD_FILE",
    "VERIFY_TEXT_CONTAINS", "VERIFY_ELEMENT_VISIBLE", "VERIFY_ELEMENT_NOT_VISIBLE", "VERIFY_TEXT_NOT_PRESENT",
    "VERIFY_ELEMENT_ENABLED", "VERIFY_ELEMENT_DISABLED", "VERIFY_ELEMENT_COUNT", "CAPTURE_SCREENSHOT",
    "VERIFY_VALUE", "VERIFY_URL", "VERIFY_DIALOG_TEXT", "VERIFY_DOWNLOAD", "VERIFY_PRINT_CALLED",
    "SOFT_VERIFY_TEXT_CONTAINS", "SOFT_VERIFY_ELEMENT_VISIBLE", "SOFT_VERIFY_VALUE", "SOFT_VERIFY_URL",
    "STORE_TEXT", "STORE_INPUT_VALUE", "STORE_REGEX", "STORE_DIALOG_TEXT", "STORE_ELEMENT_COUNT", "CALL_BLOCK",
    "CALL_BLOCK_IF_VISIBLE", "CALL_BLOCK_IF", "REPEAT_BLOCK", "REPEAT_BLOCK_UNTIL",
]
CAUSED_NAV_SECONDS = 5.0   # A page change this soon after an action was caused by it.
CHAIN_NAV_SECONDS = 5.0    # Redirects closer together than this are one page change.
NEW_WINDOW_SECONDS = 4.0   # A window opening this soon after a click was opened by it.
SELF_CLOSE_SECONDS = 2.0   # A window closing this soon after an action closed itself.
MAX_TEXT = 100
CONTROLS_HELP = """
Controls while recording (toolbar at the bottom right of the page):
  Use the application normally          -> clicks, typing, dropdowns, ticks, uploads, drag and
                                           drop, Enter/Escape, pop-ups, dialogs and downloads
  Add check, then click an element      -> the same keyword menu as Alt + Right-Click
  Undo                                  -> remove the last recorded step
  Pause / Resume                        -> nothing is recorded while paused (e.g. signing in)
  More                                  -> Verify URL, Wait, Scroll, Call block, Screenshot
  Finish                                -> save, send to the portal, close the browser
  Alt + Right-Click an element          -> keyword menu: checks, soft checks, save text / regex /
                                           count to a variable, hover, wait, click if visible,
                                           scroll, element screenshot, run a block if visible
  Shift + Alt + Click an element        -> quick check of its text (or value) without clicking it
  Shift + Alt + S                       -> page screenshot step
  Close the browser window (or Ctrl+C)  -> also finishes; the .xlsx is saved after every step
"""
_INDEX_SUFFIX_RE = re.compile(r"(\s+)@(first|last|\d+)$")
_USERNAME_HINT_RE = re.compile(r"user|login|e-?mail|account|logon|signin|sign in", re.I)

# --------------------------------------------------------------------------
# Browser side: listens to the user's actions and works out a locator for the
# element, using the same matching rules as the portal's runner (Playwright:
# role, label, text, placeholder and title match part of the text, ignoring case).
# --------------------------------------------------------------------------
RECORDER_JS = r"""
(() => {
  if (window.__qaRecInstalled) return;
  window.__qaRecInstalled = true;
  const IS_TOP = window === window.top;
  // The iframes this document sits in, outermost first; null when a parent is on another
  // site and cannot be read from here (Python then works it out after the event).
  const frameChain = () => {
    const parts = [];
    let w = window;
    try {
      while (w !== w.top) {
        const fe = w.frameElement;
        if (!fe) return null;
        parts.unshift(w.parent.__qaFrameSelector ? w.parent.__qaFrameSelector(fe) : 'iframe');
        w = w.parent;
      }
    } catch (e) { return null; }
    return parts;
  };
  // The recorder answers every event with {count, paused, message}; the toolbar on the
  // top page shows it, also for an event sent from inside an iframe.
  const showState = (s) => {
    try { if (window.top.__qaApplyState) { window.top.__qaApplyState(s); return; } } catch (e) {}
    try { applyState(s); } catch (e) {}
  };
  const call = (ev) => {
    try {
      ev.frames = frameChain();
      const answer = window.__qaRecord ? window.__qaRecord(ev) : null;
      if (answer && answer.then) answer.then(showState, () => {});
    } catch (e) {}
  };
  const send = (ev) => {
    try { status((ev.keyword || ev.kind) + (ev.desc ? ': ' + ev.desc : '')); } catch (e) {}
    call(ev);
  };
  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const lc = (s) => norm(s).toLowerCase();
  const short = (s, n) => {
    s = norm(s); n = n || 60;
    if (s.length <= n) return s;
    const cut = s.slice(0, n); const sp = cut.lastIndexOf(' ');
    return sp > 20 ? cut.slice(0, sp) : cut;
  };
  const attrEsc = (v) => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');

  // ---- visibility -------------------------------------------------------
  function isVisible(el) {
    if (!el || !el.getClientRects || !el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  }
  function hiddenFromRoles(el) {
    return !isVisible(el) || !!(el.closest && el.closest('[aria-hidden="true"]'));
  }

  // ---- dynamic ids (same rules as the portal's Locator Tool) ------------
  const DYN = [
    /^(mat|cdk|ng|mdc|ui-id|gwt-uid|ext-gen|ember|mui|rc|downshift|headlessui|react-select|j_idt|__bvid__)[\w:-]*?\d+[\w-]*$/i,
    /:r[0-9a-z]*:/i,
    /^\d+$/,
    /[0-9a-f]{8}-[0-9a-f]{4}-/i,
  ];
  function dynamicId(id) {
    if (DYN.some((re) => re.test(id))) return true;
    const m = /(?:^|[-_:.])([0-9a-f]{5,})$/i.exec(id);
    return !!m && /\d/.test(m[1]);
  }
  function idTail(id) {
    if (id.indexOf('$') < 0 && !/^(?:ctl\d+[_$]|.*(?:ContentPlaceHolder|MainContent)\w*[_$])/i.test(id)) return null;
    const m = /[^_$]+(?:[_$]\d+)?$/.exec(id);
    return m && m[0] !== id ? m[0] : null;
  }

  // ---- roles and accessible names ---------------------------------------
  const NAME_FROM_CONTENT = new Set(['button', 'cell', 'checkbox', 'columnheader', 'gridcell',
    'heading', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'row',
    'rowheader', 'switch', 'tab', 'tooltip', 'treeitem']);
  const ROLE_TARGETS = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'option', 'switch', 'treeitem', 'heading', 'textbox',
    'combobox', 'searchbox', 'spinbutton', 'slider', 'listbox', 'gridcell', 'cell',
    'columnheader', 'alert', 'status', 'dialog']);
  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) { const r = explicit.trim().split(/\s+/)[0]; if (r && r !== 'presentation' && r !== 'none') return r; }
    const t = el.tagName.toLowerCase();
    if (t === 'button') return 'button';
    if ((t === 'a' || t === 'area') && el.hasAttribute('href')) return 'link';
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
      if (ty === 'range') return 'slider';
      if (ty === 'number') return 'spinbutton';
      if (['text', 'email', 'tel', 'url', ''].includes(ty)) return el.hasAttribute('list') ? 'combobox' : 'textbox';
      return null;
    }
    if (t === 'textarea') return 'textbox';
    if (t === 'select') return (el.multiple || el.size > 1) ? 'listbox' : 'combobox';
    if (/^h[1-6]$/.test(t)) return 'heading';
    if (t === 'option') return 'option';
    if (t === 'td') return 'cell';
    if (t === 'th') return 'columnheader';
    if (t === 'dialog') return 'dialog';
    return null;
  }
  function byIds(ids) {
    return norm(String(ids).split(/\s+/).map((id) => {
      const n = document.getElementById(id); return n ? n.textContent : '';
    }).join(' '));
  }
  function textNoControls(label) {
    let out = '';
    label.childNodes.forEach((n) => {
      if (n.nodeType === 3) out += n.textContent;
      else if (n.nodeType === 1 && !['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(n.tagName)) out += ' ' + n.textContent;
    });
    return norm(out);
  }
  function labelTexts(el) {
    const out = [];
    if (el.labels) for (const l of el.labels) { const t = textNoControls(l); if (t) out.push(t); }
    const lb = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb); if (t) out.push(t); }
    const al = norm(el.getAttribute && el.getAttribute('aria-label'));
    if (al) out.push(al);
    return out;
  }
  function accName(el) {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = byIds(lb); if (t) return t; }
    const al = norm(el.getAttribute('aria-label'));
    if (al) return al;
    const t = el.tagName.toLowerCase();
    if (t === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (['button', 'submit', 'reset'].includes(ty)) return norm(el.value) || (ty === 'submit' ? 'Submit' : ty === 'reset' ? 'Reset' : '');
      if (ty === 'image') return norm(el.alt) || norm(el.title);
    }
    if (t === 'input' || t === 'select' || t === 'textarea') {
      const ls = el.labels ? Array.from(el.labels).map(textNoControls).filter(Boolean) : [];
      if (ls.length) return ls.join(' ');
      return norm(el.title) || norm(el.getAttribute('placeholder'));
    }
    if (t === 'img') return norm(el.alt) || norm(el.title);
    if (NAME_FROM_CONTENT.has(roleOf(el))) {
      let s = norm(el.innerText != null ? el.innerText : el.textContent);
      if (!s) s = Array.from(el.querySelectorAll('img[alt]')).map((i) => norm(i.alt)).join(' ');
      if (s) return s;
    }
    return norm(el.title);
  }

  // ---- matching, as the runner's locators would -------------------------
  const all = () => Array.from(document.querySelectorAll('*')).filter((e) => e.id !== '__qa_rec_host');
  function qsa(sel) { try { return Array.from(document.querySelectorAll(sel)); } catch (e) { return []; } }
  function matchRole(role, name) {
    const q = lc(name);
    return all().filter((e) => roleOf(e) === role && !hiddenFromRoles(e) && (!q || lc(accName(e)).includes(q)));
  }
  function matchLabel(text) {
    const q = lc(text);
    return qsa('input,select,textarea,button,meter,output,progress,[aria-label],[aria-labelledby]')
      .filter((e) => labelTexts(e).some((t) => lc(t).includes(q)));
  }
  function matchAttr(attr, text) {
    const q = lc(text);
    return qsa('[' + attr + ']').filter((e) => lc(e.getAttribute(attr)).includes(q));
  }
  function ownText(e) {
    const t = e.tagName;
    if (t === 'SCRIPT' || t === 'STYLE' || t === 'NOSCRIPT' || t === 'TEMPLATE' || t === 'HEAD') return '';
    if (t === 'INPUT' && ['button', 'submit', 'reset'].includes((e.type || '').toLowerCase())) return e.value;
    return e.textContent;
  }
  function matchText(text) {
    const q = lc(text);
    const hits = (document.body ? Array.from(document.body.querySelectorAll('*')) : [])
      .filter((e) => e.id !== '__qa_rec_host' && lc(ownText(e)).includes(q));
    const set = new Set(hits);
    // The smallest element containing the text, as get_by_text picks.
    return hits.filter((e) => !Array.from(e.children).some((c) => set.has(c)));
  }

  function cssPath(el) {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node !== document.documentElement) {
      if (node.id && !dynamicId(node.id) && qsa('#' + CSS.escape(node.id)).length === 1) {
        parts.unshift('#' + CSS.escape(node.id)); break;
      }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(' > ');
  }

  // The best locator for el. purpose: 'field', 'click' or 'check'.
  function locate(el, purpose) {
    const cands = [];
    const add = (type, value, matches, allowInside) => {
      value = norm(value);
      if (!value || value.length > 300) return;
      let idx = matches.indexOf(el);
      if (idx < 0 && allowInside) idx = matches.findIndex((m) => el.contains(m));
      if (idx < 0) return;
      cands.push({ type: type, value: value, count: matches.length, index: idx + 1 });
    };
    const tag = el.tagName.toLowerCase();
    const role = roleOf(el);
    const name = accName(el);
    const testId = el.getAttribute('data-testid');
    const id = el.id || '';
    const tryTestId = () => { if (testId) add('test_id', testId, qsa('[data-testid="' + attrEsc(testId) + '"]')); };
    const tryId = () => {
      if (!id) return;
      if (!dynamicId(id)) add('id', id, qsa('[id="' + attrEsc(id) + '"]'));
      const tail = idTail(id);
      if (tail && !dynamicId(tail)) add('id_ends_with', tail, qsa('[id$="' + attrEsc(tail) + '"]'));
    };
    const tryName = () => {
      const n = el.getAttribute('name');
      if (n && ['input', 'select', 'textarea', 'button'].includes(tag)) add('name', n, qsa('[name="' + attrEsc(n) + '"]'));
    };
    const tryLabel = () => { for (const t of labelTexts(el)) add('label', short(t), matchLabel(short(t))); };
    const tryPlaceholder = () => { const p = el.getAttribute('placeholder'); if (p) add('placeholder', p, matchAttr('placeholder', p)); };
    const tryTitle = () => { const t = el.getAttribute('title'); if (t) add('title', short(t), matchAttr('title', short(t))); };
    const tryRole = () => {
      if (!role || !ROLE_TARGETS.has(role)) return;
      if (name) add('role', role + '|' + short(name), matchRole(role, short(name)));
    };
    const tryText = () => {
      if (['input', 'select', 'textarea'].includes(tag)) return;
      const t = short(el.innerText != null ? el.innerText : el.textContent);
      if (t) add('text', t, matchText(t), true);
    };
    const tryRow = () => {
      const row = el.closest('tr, [role="row"]');
      if (!row || row === el) return;
      const action = short(name || el.innerText || el.textContent, 40);
      if (!action || action.indexOf('|') >= 0) return;
      const rows = qsa('tr, [role="row"]');
      const inRow = (r) => Array.from(r.querySelectorAll('button, a[href], [role="button"], [role="link"]'))
        .filter((b) => lc(accName(b)).includes(lc(action)));
      if (inRow(row).length !== 1 && el.matches('button, a[href], [role="button"], [role="link"]')) return;
      const cells = Array.from(row.querySelectorAll('td, th, [role="cell"], [role="gridcell"]'));
      for (const c of cells) {
        if (c.contains(el)) continue;
        const ct = short(c.innerText || c.textContent, 60);
        if (!ct || lc(ct) === lc(action)) continue;
        const same = rows.filter((r) => lc(r.innerText || r.textContent).includes(lc(ct)));
        if (same.length === 1) {
          cands.push({ type: 'row_action', value: ct + ' | ' + action, count: 1, index: 1 });
          return;
        }
      }
    };
    let order;
    if (purpose === 'field') order = [tryTestId, tryLabel, tryPlaceholder, tryId, tryName, tryRole, tryTitle];
    else if (purpose === 'check') order = [tryTestId, tryId, tryRole, tryLabel, tryText, tryTitle, tryName];
    else order = [tryTestId, tryRole, tryId, tryRow, tryLabel, tryTitle, tryText, tryName, tryPlaceholder];
    for (const f of order) { try { f(); } catch (e) {} }
    const unique = cands.find((c) => c.count === 1);
    if (unique) return unique;
    const some = cands.find((c) => c.count <= 10);
    if (some) return Object.assign({}, some, { warn: 'Matches ' + some.count + ' elements; picked number ' + some.index + ' (@' + some.index + ').' });
    return { type: 'css', value: cssPath(el), count: 1, index: 1, warn: 'No stable locator found; uses the element position (css path).' };
  }
  function kindWord(el) {
    const r = roleOf(el);
    if (r) return r;
    return el.tagName.toLowerCase();
  }
  function fieldName(el) {
    return short(labelTexts(el)[0] || el.getAttribute('placeholder') || el.getAttribute('name') || el.id || kindWord(el), 50);
  }

  // ---- what counts as the element the user meant -------------------------
  const CLICKABLE = 'button, a[href], input, select, textarea, label, summary, option, [role="button"], ' +
    '[role="link"], [role="tab"], [role="menuitem"], [role="menuitemcheckbox"], [role="menuitemradio"], ' +
    '[role="option"], [role="checkbox"], [role="radio"], [role="switch"], [role="treeitem"], ' +
    '[onclick], [ng-click], [data-testid]';
  const TEXT_TYPES = ['', 'text', 'email', 'password', 'search', 'tel', 'url', 'number', 'date',
    'datetime-local', 'month', 'time', 'week'];
  const realTarget = (e) => { const p = e.composedPath ? e.composedPath() : []; return (p[0] && p[0].nodeType === 1) ? p[0] : e.target; };
  const isTextField = (el) => !!el && ((el.tagName === 'INPUT' && TEXT_TYPES.includes((el.getAttribute('type') || '').toLowerCase())) || el.tagName === 'TEXTAREA');
  const isToggle = (el) => !!el && el.tagName === 'INPUT' && ['checkbox', 'radio'].includes((el.type || '').toLowerCase());
  function clickTarget(raw) {
    let el = raw;
    if (el.closest('svg')) el = el.closest('svg').parentElement || el;
    return el.closest(CLICKABLE) || el;
  }
  function flash(el) {
    try {
      const old = el.style.outline;
      el.style.outline = '3px solid #c8a24a';
      setTimeout(() => { el.style.outline = old; }, 700);
    } catch (e) {}
  }
  function fieldValueEvent(el) {
    const auto = el.getAttribute('role') === 'combobox' || el.hasAttribute('list') ||
      ['list', 'both'].includes((el.getAttribute('aria-autocomplete') || '').toLowerCase());
    return {
      kind: 'fill', loc: locate(el, 'field'), value: el.value,
      secret: (el.type || '').toLowerCase() === 'password', typed: auto,
      desc: 'Enter ' + fieldName(el),
    };
  }
  function flushField(el) {
    if (isTextField(el) && el.value !== (el.__qaLast == null ? el.defaultValue : el.__qaLast)) {
      el.__qaLast = el.value;
      send(fieldValueEvent(el));
    }
  }
  const isCheckGesture = (e) => e.shiftKey && e.altKey;

  // ---- recorder toolbar and the Alt+Right-Click keyword menu --------------
  // Both live in a shadow root, so the page's styles and the locator counting above
  // never see them, and nothing done in them is recorded as a page action. Input is
  // asked for in the panel itself, never with prompt(), so no dialog step can appear.
  let hudHost = null, hudRoot = null, panelEl = null, statusEl = null, swallow = false;
  let paused = false, pickMode = false, stepCount = 0;
  function applyState(s) {
    if (!s || typeof s !== 'object') return;
    if (typeof s.paused === 'boolean') paused = s.paused;
    if (typeof s.count === 'number') stepCount = s.count;
    if (!hudRoot) return;
    const count = hudRoot.querySelector('.count');
    const dot = hudRoot.querySelector('.dot');
    const pause = hudRoot.querySelector('[data-a="pause"]');
    if (count) count.textContent = stepCount + (stepCount === 1 ? ' step' : ' steps');
    if (dot) { dot.textContent = paused ? 'PAUSED' : 'REC'; dot.className = paused ? 'dot off' : 'dot'; }
    if (pause) pause.textContent = paused ? 'Resume' : 'Pause';
    if (s.message) status(s.message);
  }
  window.__qaApplyState = applyState;
  // Add check: the next click picks an element instead of using it. Set in every frame
  // this page can reach, so an element inside an iframe can be picked too.
  function setPick(on) {
    pickMode = on;
    try { document.documentElement.style.cursor = on ? 'crosshair' : ''; } catch (e) {}
    for (let i = 0; i < window.frames.length; i++) {
      try { if (window.frames[i].__qaSetPick) window.frames[i].__qaSetPick(on); } catch (e) {}
    }
  }
  window.__qaSetPick = setPick;
  function setPickEverywhere(on) {
    try { if (window.top.__qaSetPick) { window.top.__qaSetPick(on); return; } } catch (e) {}
    setPick(on);
  }
  const fromHud = (e) => {
    if (!hudHost) return false;
    const p = e.composedPath ? e.composedPath() : [];
    return p.indexOf(hudHost) >= 0;
  };
  const HUD_CSS = ':host{all:initial}' +
    '.bar{position:fixed;right:12px;bottom:12px;z-index:2147483647;display:flex;gap:6px;align-items:center;' +
    'font:12px "Segoe UI",Arial,sans-serif;background:#2b2722;color:#f3e6c8;padding:6px 8px;border-radius:8px;' +
    'border:1px solid #c8a24a;box-shadow:0 4px 14px rgba(0,0,0,.3)}' +
    '.dot{color:#e06c5a;font-weight:700}.dot.off{color:#d9b25a}.count{font-weight:600;white-space:nowrap}' +
    '.fin{background:#c8a24a;color:#2b2722;border-color:#c8a24a;font-weight:600}' +
    '.status{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#d9ccb0}' +
    'button{font:inherit;background:#3a342d;color:#f3e6c8;border:1px solid #6b5a3a;border-radius:5px;padding:3px 8px;cursor:pointer}' +
    'button:hover{border-color:#c8a24a}' +
    '.panel{position:fixed;z-index:2147483647;width:300px;max-height:75vh;overflow:auto;background:#fbf8f2;color:#2b2722;' +
    'border:1px solid #c8a24a;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.3);font:12px "Segoe UI",Arial,sans-serif}' +
    '.panel[hidden]{display:none}' +
    '.title{padding:7px 10px;background:#2b2722;color:#f3e6c8;font-weight:600;word-break:break-all}' +
    '.item{display:block;width:100%;text-align:left;border:0;border-bottom:1px solid #eee6d6;border-radius:0;' +
    'background:#fbf8f2;color:#2b2722;padding:6px 10px}' +
    '.item:hover{background:#f1e7d0}' +
    '.form{padding:8px 10px;display:grid;gap:8px}' +
    '.form label{display:grid;gap:3px;font-weight:600}' +
    '.form input{font:inherit;padding:4px 6px;border:1px solid #bfb39a;border-radius:4px;background:#fff;color:#2b2722}' +
    '.hint{color:#6b604e;font-weight:400}.err{color:#a3392b}' +
    '.row{display:flex;gap:6px;justify-content:flex-end}' +
    '.ok{background:#2b2722;color:#f3e6c8;border-color:#c8a24a}.cancel{background:#fff;color:#2b2722}';
  const BAR_HTML = '<div class="bar"><span class="dot">REC</span><span class="count">0 steps</span>' +
    '<span class="status">Use the application. Add check verifies something on the page.</span>' +
    '<button data-a="check" title="Then click the thing to check">Add check</button>' +
    '<button data-a="undo" title="Remove the last recorded step">Undo</button>' +
    '<button data-a="pause" title="Stop recording for a while, for example to sign in">Pause</button>' +
    '<button data-a="more" title="Check the address, wait, scroll, run a block, screenshot">More</button>' +
    '<button data-a="finish" class="fin" title="Save the test case and close the browser">Finish</button></div>';
  function ensureHud() {
    if (hudHost && hudHost.isConnected) return true;
    if (!document.documentElement) return false;
    hudHost = document.createElement('div');
    hudHost.id = '__qa_rec_host';
    hudRoot = hudHost.attachShadow({ mode: 'open' });
    hudRoot.innerHTML = '<style>' + HUD_CSS + '</style>' + (IS_TOP ? BAR_HTML : '') + '<div class="panel" hidden></div>';
    panelEl = hudRoot.querySelector('.panel');
    statusEl = hudRoot.querySelector('.status');
    // Keys typed in the panel must not reach the page's own shortcuts.
    ['keydown', 'keyup', 'keypress', 'input', 'change', 'click', 'dblclick', 'mousedown', 'mouseup',
      'pointerdown', 'pointerup', 'contextmenu', 'focusin', 'focusout'].forEach((t) => {
      hudRoot.addEventListener(t, (ev) => ev.stopPropagation());
    });
    hudRoot.querySelectorAll('[data-a]').forEach((b) => {
      b.addEventListener('click', () => toolbarAction(b.getAttribute('data-a')));
    });
    document.documentElement.appendChild(hudHost);
    // A new page starts with no count: ask the recorder, which also says if it is paused.
    if (IS_TOP) { applyState({}); call({ kind: 'hello' }); }
    return true;
  }
  function status(text) { if (statusEl) statusEl.textContent = String(text).slice(0, 80); }
  const panelOpen = () => !!(panelEl && !panelEl.hidden);
  function closePanel() { if (panelEl) { panelEl.hidden = true; panelEl.innerHTML = ''; } }
  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (tag === 'button') n.type = 'button';
    return n;
  }
  function placePanel(at) {
    panelEl.hidden = false;
    const w = 300, h = Math.min(panelEl.scrollHeight || 360, window.innerHeight * 0.75);
    let top = at.above ? at.y - h : at.y;
    top = Math.max(4, Math.min(top, window.innerHeight - h - 8));
    panelEl.style.left = Math.max(4, Math.min(at.x, window.innerWidth - w - 8)) + 'px';
    panelEl.style.top = top + 'px';
  }
  function showMenu(at, title, items) {
    if (!ensureHud()) return;
    panelEl.innerHTML = '';
    panelEl.appendChild(node('div', 'title', title));
    items.forEach((it) => {
      const b = node('button', 'item', it.label);
      b.addEventListener('click', () => it.run());
      panelEl.appendChild(b);
    });
    placePanel(at);
  }
  // fields: [{label, hint, value}]. onOk(values) returns an error text, or nothing when done.
  function showForm(at, title, fields, onOk) {
    if (!ensureHud()) return;
    panelEl.innerHTML = '';
    panelEl.appendChild(node('div', 'title', title));
    const form = node('div', 'form');
    const inputs = fields.map((f) => {
      const label = node('label', '', f.label);
      if (f.hint) label.appendChild(node('span', 'hint', f.hint));
      const input = document.createElement('input');
      input.value = f.value == null ? '' : String(f.value);
      label.appendChild(input);
      form.appendChild(label);
      return input;
    });
    const err = node('div', 'err');
    const row = node('div', 'row');
    const cancel = node('button', 'cancel', 'Cancel');
    const ok = node('button', 'ok', 'Add step');
    const submit = () => {
      const problem = onOk(inputs.map((i) => i.value));
      if (problem) { err.textContent = problem; return; }
      closePanel();
    };
    ok.addEventListener('click', submit);
    cancel.addEventListener('click', closePanel);
    inputs.forEach((i) => i.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') submit();
      if (ev.key === 'Escape') closePanel();
    }));
    row.appendChild(cancel); row.appendChild(ok);
    form.appendChild(err); form.appendChild(row);
    panelEl.appendChild(form);
    placePanel(at);
    if (inputs[0]) { inputs[0].focus(); inputs[0].select(); }
  }
  function varName(text) {
    let v = String(text || '').toUpperCase().replace(/[^A-Z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 30);
    if (!v) v = 'VALUE';
    if (/^[0-9]/.test(v)) v = 'V_' + v;
    return v;
  }
  function manual(keyword, loc, input, store, desc, extra) {
    send(Object.assign({ kind: 'manual', keyword: keyword, loc: loc, input: input || '', store: store || '', desc: desc }, extra || {}));
  }
  const UNSTABLE_CLASS = /^(ng-|mat-|mdc-|cdk-|css-|sc-|jss|is-|has-|Mui|active$|disabled$|selected$|hover$|focus$|open$|show$|hidden$)|\d{3,}|[0-9a-f]{6,}$/;
  // A css locator for every element like this one (rows, list items, cards), for counts.
  function groupLocator(el) {
    const item = el.closest('tr, li, [role="row"], [role="listitem"], [role="option"]') || el;
    const classes = Array.from(item.classList || []).filter((c) => /^[A-Za-z][\w-]*$/.test(c) && !UNSTABLE_CLASS.test(c)).slice(0, 2);
    let sel = item.tagName.toLowerCase() + classes.map((c) => '.' + CSS.escape(c)).join('');
    let anc = item.parentElement;
    while (anc && anc !== document.documentElement) {
      if (anc.id && !dynamicId(anc.id) && qsa('#' + CSS.escape(anc.id)).length === 1) { sel = '#' + CSS.escape(anc.id) + ' ' + sel; break; }
      anc = anc.parentElement;
    }
    return { loc: { type: 'css', value: sel, count: 1, index: 1 }, count: qsa(sel).length };
  }
  function openElementMenu(raw, x, y) {
    const isField = isTextField(raw) || raw.tagName === 'SELECT';
    const el = raw;
    const target = clickTarget(raw);
    const loc = locate(el, isField ? 'field' : 'check');
    const tloc = locate(target, 'click');
    const name = isField ? fieldName(el) : short(accName(target) || el.innerText || el.textContent || kindWord(el), 30);
    const text = short(isField ? el.value : (el.innerText != null ? el.innerText : el.textContent), 80);
    const group = groupLocator(el);
    const at = { x: x, y: y };
    const form = (title, fields, ok) => () => showForm(at, title, fields, ok);
    const now = (fn) => () => { fn(); closePanel(); };
    const items = [];
    if (isField) {
      items.push({ label: 'Verify value', run: form('Verify value', [{ label: 'Expected value', value: el.value }], (v) => {
        if (!v[0]) return 'Enter the expected value';
        manual('VERIFY_VALUE', loc, v[0], '', 'Check ' + name + ' value');
      }) });
    } else {
      items.push({ label: 'Verify text contains' + (text ? ' "' + short(text, 24) + '"' : ''), run: form('Verify text contains', [{ label: 'Expected text', value: text }], (v) => {
        if (!v[0].trim()) return 'Enter the expected text';
        manual('VERIFY_TEXT_CONTAINS', loc, v[0].trim(), '', 'Check text "' + short(v[0], 40) + '"');
      }) });
    }
    items.push({ label: 'Verify visible', run: now(() => manual('VERIFY_ELEMENT_VISIBLE', loc, '', '', 'Check ' + name + ' is shown')) });
    items.push({ label: 'Verify not visible', run: now(() => manual('VERIFY_ELEMENT_NOT_VISIBLE', loc, '', '', 'Check ' + name + ' is not shown')) });
    items.push(target.disabled
      ? { label: 'Verify disabled', run: now(() => manual('VERIFY_ELEMENT_DISABLED', tloc, '', '', 'Check ' + name + ' is disabled')) }
      : { label: 'Verify enabled', run: now(() => manual('VERIFY_ELEMENT_ENABLED', tloc, '', '', 'Check ' + name + ' is enabled')) });
    items.push({ label: 'Verify count of ' + group.loc.value + ' (' + group.count + ' now)', run: form('Verify element count', [
      { label: 'Locator (css)', value: group.loc.value },
      { label: 'Condition', hint: ' e.g. 5, > 0, >= 1, != 0', value: '== ' + group.count }], (v) => {
        if (!v[0].trim() || !v[1].trim()) return 'Enter a locator and a condition';
        manual('VERIFY_ELEMENT_COUNT', { type: 'css', value: v[0].trim(), count: 1, index: 1 }, v[1].trim(), '', 'Check count of ' + v[0].trim() + ' is ' + v[1].trim(), { noPick: true });
      }) });
    if (isField) {
      items.push({ label: 'Soft verify value (test carries on)', run: form('Soft verify value', [{ label: 'Expected value', value: el.value }], (v) => {
        if (!v[0]) return 'Enter the expected value';
        manual('SOFT_VERIFY_VALUE', loc, v[0], '', 'Soft check ' + name + ' value');
      }) });
    } else if (text) {
      items.push({ label: 'Soft verify text (test carries on)', run: form('Soft verify text contains', [{ label: 'Expected text', value: text }], (v) => {
        if (!v[0].trim()) return 'Enter the expected text';
        manual('SOFT_VERIFY_TEXT_CONTAINS', loc, v[0].trim(), '', 'Soft check text "' + short(v[0], 40) + '"');
      }) });
    } else {
      items.push({ label: 'Soft verify visible (test carries on)', run: now(() => manual('SOFT_VERIFY_ELEMENT_VISIBLE', loc, '', '', 'Soft check ' + name + ' is shown')) });
    }
    items.push({ label: 'Save ' + (isField ? 'value' : 'text') + ' in a variable', run: form('Save in a variable', [{ label: 'Variable name', hint: ' used later as {NAME}', value: varName(name) }], (v) => {
      const n = varName(v[0]);
      manual(isField ? 'STORE_INPUT_VALUE' : 'STORE_TEXT', loc, '', n, 'Save ' + name + ' as {' + n + '}');
    }) });
    items.push({ label: 'Save a regex match in a variable', run: form('Save a regex match', [
      { label: 'Regular expression', hint: ' group 1 is kept', value: '(\\d+)' },
      { label: 'Variable name', value: varName(name) + '_ID' }], (v) => {
        if (!v[0]) return 'Enter a regular expression';
        try { new RegExp(v[0]); } catch (e) { return 'That is not a valid regular expression'; }
        const n = varName(v[1]);
        manual('STORE_REGEX', loc, v[0], n, 'Save the match of ' + v[0] + ' as {' + n + '}');
      }) });
    items.push({ label: 'Save count in a variable', run: form('Save element count', [
      { label: 'Locator (css)', value: group.loc.value }, { label: 'Variable name', value: 'ROW_COUNT' }], (v) => {
        if (!v[0].trim()) return 'Enter a locator';
        const n = varName(v[1]);
        manual('STORE_ELEMENT_COUNT', { type: 'css', value: v[0].trim(), count: 1, index: 1 }, '', n, 'Save count of ' + v[0].trim() + ' as {' + n + '}', { noPick: true });
      }) });
    items.push({ label: 'Hover (e.g. to open a menu)', run: now(() => manual('HOVER', tloc, '', '', 'Hover over ' + name)) });
    items.push({ label: 'Wait until visible', run: now(() => manual('WAIT_FOR_ELEMENT', loc, '', '', 'Wait for ' + name)) });
    items.push({ label: 'Click if visible (clicks it now too)', run: now(() => {
      manual('CLICK_IF_VISIBLE', tloc, '', '', 'Click ' + name + ' if it is shown');
      setTimeout(() => { try { target.click(); } catch (e) {} }, 50);
    }) });
    items.push({ label: 'Scroll into view', run: now(() => manual('SCROLL_TO', loc, '', '', 'Scroll to ' + name)) });
    items.push({ label: 'Screenshot of this element', run: form('Element screenshot', [{ label: 'Caption', value: name }], (v) => {
      manual('CAPTURE_SCREENSHOT', loc, v[0].trim(), '', v[0].trim() || 'Element screenshot');
    }) });
    items.push({ label: 'Run a reusable block if this is visible', run: form('Run a block if visible', [{ label: 'Block name', value: '' }], (v) => {
      if (!v[0].trim()) return 'Enter the block name';
      manual('CALL_BLOCK_IF_VISIBLE', tloc, v[0].trim(), '', 'Run ' + v[0].trim() + ' if ' + name + ' is shown');
    }) });
    flash(el);
    showMenu(at, loc.type + ' = ' + short(loc.value, 48), items);
  }
  function toolbarAction(action) {
    const bar = hudRoot.querySelector('.bar').getBoundingClientRect();
    const at = { x: bar.right - 300, y: bar.top - 8, above: true };
    if (action === 'check') {
      const on = !pickMode;
      closePanel();
      setPickEverywhere(on);
      status(on ? 'Click the thing you want to check (Esc cancels)' : 'Add check cancelled');
    } else if (action === 'undo') {
      call({ kind: 'undo' });
    } else if (action === 'pause') {
      call({ kind: 'pause', paused: !paused });
    } else if (action === 'finish') {
      status('Saving the test case ...');
      call({ kind: 'finish' });
    } else if (action === 'more') {
      const item = (label, name) => ({ label: label, run: () => toolbarAction(name) });
      showMenu(at, 'More steps', [
        item('Check the page address', 'url'), item('Wait a number of seconds', 'wait'),
        item('Scroll the page', 'scroll'), item('Run a reusable block', 'block'),
        item('Screenshot of the page', 'shot'),
      ]);
    } else if (action === 'url') {
      const here = location.pathname + (location.hash.indexOf('#/') === 0 ? location.hash : '');
      showForm(at, 'Verify the address', [{ label: 'Address contains', value: here }], (v) => {
        if (!v[0].trim()) return 'Enter part of the address';
        manual('VERIFY_URL', null, v[0].trim(), '', 'Page ' + v[0].trim() + ' is open');
      });
    } else if (action === 'wait') {
      showForm(at, 'Wait a fixed time', [{ label: 'Seconds', hint: ' prefer Wait until visible', value: '2' }], (v) => {
        if (!/^\d+(\.\d+)?$/.test(v[0].trim())) return 'Enter a number of seconds';
        manual('WAIT_SECONDS', null, v[0].trim(), '', 'Wait ' + v[0].trim() + ' seconds');
      });
    } else if (action === 'scroll') {
      showForm(at, 'Scroll the page', [{ label: 'Where', hint: ' bottom, top, 500 or -500', value: 'bottom' }], (v) => {
        const w = v[0].trim().toLowerCase();
        if (!/^(bottom|top|-?\d+)$/.test(w)) return 'Use bottom, top or a number of pixels';
        manual('SCROLL_TO', null, w, '', 'Scroll the page ' + w);
      });
    } else if (action === 'block') {
      showForm(at, 'Run a reusable block', [{ label: 'Block name', hint: ' or Login | USER=checker_01', value: '' }], (v) => {
        if (!v[0].trim()) return 'Enter the block name';
        manual('CALL_BLOCK', null, v[0].trim(), '', 'Run ' + v[0].trim().split('|')[0].trim());
      });
    } else if (action === 'shot') {
      showForm(at, 'Screenshot of the page', [{ label: 'Caption', value: short(document.title, 60) }], (v) => {
        manual('CAPTURE_SCREENSHOT', null, v[0].trim(), '', v[0].trim() || 'Page screenshot');
      });
    }
  }

  // ---- listeners (capture phase, so the page cannot hide events) ----------
  ['pointerdown', 'mousedown', 'pointerup', 'mouseup'].forEach((type) => {
    window.addEventListener(type, (e) => {
      if (!e.isTrusted || fromHud(e)) return;
      // A click outside an open panel only closes it: it is not passed on or recorded.
      if (type === 'pointerdown' && panelOpen()) { closePanel(); swallow = e.button === 0; }
      if (swallow || pickMode || isCheckGesture(e)) { e.preventDefault(); e.stopImmediatePropagation(); return; }
      if (type === 'pointerdown') {
        const active = document.activeElement;
        if (active && active !== realTarget(e)) flushField(active);
      }
    }, true);
  });

  window.addEventListener('contextmenu', (e) => {
    if (!e.isTrusted || fromHud(e) || !e.altKey) return;
    e.preventDefault(); e.stopImmediatePropagation();
    try { openElementMenu(realTarget(e), e.clientX, e.clientY); } catch (err) {}
  }, true);

  // Typing is saved after a short pause too, so it is not lost if the browser is closed
  // while the cursor is still in the field. A later save of the same field replaces it.
  window.addEventListener('input', (e) => {
    if (!e.isTrusted || fromHud(e)) return;
    const el = realTarget(e);
    if (!isTextField(el)) return;
    clearTimeout(el.__qaTimer);
    el.__qaTimer = setTimeout(() => flushField(el), 1200);
  }, true);

  let dragSource = null;
  window.addEventListener('dragstart', (e) => {
    if (!e.isTrusted || fromHud(e)) return;
    const el = realTarget(e);
    const src = (el.closest && el.closest('[draggable="true"]')) || el;
    dragSource = { loc: locate(src, 'click'), name: short(accName(src) || src.innerText || src.textContent || kindWord(src), 30) };
  }, true);
  window.addEventListener('drop', (e) => {
    if (!e.isTrusted || fromHud(e) || !dragSource) return;
    const dst = realTarget(e);
    const name = short(accName(dst) || dst.innerText || dst.textContent || kindWord(dst), 30);
    send({ kind: 'drag', loc: dragSource.loc, dest: locate(dst, 'check'), desc: 'Drag ' + dragSource.name + ' onto ' + name });
    dragSource = null;
  }, true);
  window.addEventListener('dragend', () => { dragSource = null; }, true);

  window.addEventListener('click', (e) => {
    if (!e.isTrusted || fromHud(e)) return;
    if (swallow) { swallow = false; e.preventDefault(); e.stopImmediatePropagation(); return; }
    const raw = realTarget(e);
    if (pickMode) {
      // Add check: the click chooses the element, and the application never sees it.
      e.preventDefault(); e.stopImmediatePropagation();
      setPickEverywhere(false);
      try { openElementMenu(raw, e.clientX, e.clientY); } catch (err) {}
      return;
    }
    if (isCheckGesture(e)) {
      e.preventDefault(); e.stopImmediatePropagation();
      const el = raw;
      flash(el);
      if (isTextField(el) || el.tagName === 'SELECT') {
        send({ kind: 'assert', mode: 'value', loc: locate(el, 'field'), text: el.value, desc: 'Check ' + fieldName(el) + ' value' });
        return;
      }
      const text = short(el.innerText != null ? el.innerText : el.textContent, 80);
      if (text) send({ kind: 'assert', mode: 'text', loc: locate(el, 'check'), text: text, desc: 'Check text "' + short(text, 40) + '"' });
      else send({ kind: 'assert', mode: 'visible', loc: locate(el, 'check'), desc: 'Check ' + kindWord(el) + ' is shown' });
      return;
    }
    if (e.detail > 1) return;  // second click of a double-click
    const el = clickTarget(raw);
    if (isTextField(el) || el.tagName === 'SELECT' || el.tagName === 'OPTION' || isToggle(el)) return;
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'file') return;
    if (el.isContentEditable) return;
    if (el.tagName === 'LABEL' && el.control) return;  // the change on its control is recorded
    if (el === document.body || el === document.documentElement) return;
    const name = accName(el) || short(el.innerText || el.textContent, 40);
    send({ kind: 'click', loc: locate(el, 'click'),
      desc: 'Click ' + (name ? '"' + short(name, 40) + '" ' : '') + kindWord(el) });
  }, true);

  window.addEventListener('dblclick', (e) => {
    if (!e.isTrusted || isCheckGesture(e) || fromHud(e)) return;
    const el = clickTarget(realTarget(e));
    if (isTextField(el)) return;
    const name = accName(el) || short(el.innerText || el.textContent, 40);
    send({ kind: 'dblclick', loc: locate(el, 'click'),
      desc: 'Double-click ' + (name ? '"' + short(name, 40) + '" ' : '') + kindWord(el) });
  }, true);

  window.addEventListener('change', (e) => {
    if (!e.isTrusted || fromHud(e)) return;
    const el = realTarget(e);
    if (isTextField(el)) { flushField(el); return; }
    if (el.tagName === 'SELECT') {
      const opt = el.selectedOptions && el.selectedOptions[0];
      send({ kind: 'select', loc: locate(el, 'field'), value: opt ? norm(opt.text) || opt.value : el.value,
        desc: 'Choose ' + (opt ? '"' + short(opt.text, 40) + '" in ' : '') + fieldName(el) });
      return;
    }
    if (isToggle(el)) {
      let target = el, viaLabel = false;
      if (!isVisible(el) && el.labels && el.labels[0] && isVisible(el.labels[0])) { target = el.labels[0]; viaLabel = true; }
      send({ kind: 'toggle', loc: locate(target, viaLabel ? 'click' : 'field'), checked: el.checked,
        radio: el.type === 'radio', viaLabel: viaLabel,
        desc: (el.type === 'radio' ? 'Select ' : (el.checked ? 'Tick ' : 'Untick ')) + fieldName(el) });
      return;
    }
    if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'file') {
      const names = Array.from(el.files || []).map((f) => f.name);
      if (names.length) send({ kind: 'upload', loc: locate(el, 'field'), files: names, desc: 'Upload ' + names.join(', ') });
    }
  }, true);

  window.addEventListener('focusout', (e) => {
    const el = realTarget(e);
    if (!e.isTrusted || fromHud(e) || !el.isContentEditable || el.closest('[contenteditable]') !== el) return;
    const text = el.innerText;
    if (text === el.__qaLast) return;
    el.__qaLast = text;
    send({ kind: 'fill', loc: locate(el, 'field'), value: text, typed: true, secret: false, desc: 'Type in ' + fieldName(el) });
  }, true);

  window.addEventListener('keydown', (e) => {
    if (!e.isTrusted) return;
    // Checked before the toolbar test: after pressing Add check the focus is still on
    // that button, and Esc must cancel from there too.
    if (pickMode && e.key === 'Escape') {
      e.preventDefault(); e.stopImmediatePropagation();
      setPickEverywhere(false);
      showState({ message: 'Add check cancelled' });
      return;
    }
    if (fromHud(e)) return;
    if (e.shiftKey && e.altKey && e.code === 'KeyS') {
      e.preventDefault(); e.stopImmediatePropagation();
      send({ kind: 'screenshot', caption: short(document.title, 60) });
      return;
    }
    const el = realTarget(e);
    if (e.key === 'Enter' && el.tagName === 'INPUT' && isTextField(el)) {
      flushField(el);
      send({ kind: 'key', key: 'Enter', loc: locate(el, 'field'), desc: 'Press Enter in ' + fieldName(el) });
    } else if (e.key === 'Escape') {
      send({ kind: 'key', key: 'Escape', loc: null, desc: 'Press Escape' });
    }
  }, true);

  // Browser pop-ups: report what the user answered.
  ['alert', 'confirm', 'prompt'].forEach((type) => {
    const native = window[type];
    if (typeof native !== 'function') return;
    window[type] = function () {
      const result = native.apply(window, arguments);
      send({ kind: 'dialog', type: type, message: norm(arguments[0]), result: result == null ? null : result });
      return result;
    };
  });

  // Used from Python to name the iframe a frame lives in.
  window.__qaFrameSelector = (el) => {
    const tag = el.tagName.toLowerCase();
    const one = (sel) => qsa(sel).length === 1;
    if (el.id && !dynamicId(el.id) && one('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);
    for (const a of ['name', 'title']) {
      const v = el.getAttribute(a);
      if (v) { const sel = tag + '[' + a + '="' + attrEsc(v) + '"]'; if (one(sel)) return sel; }
    }
    return cssPath(el);
  };

  // The page asked to print: recorded as a check, and the print still happens.
  try {
    const nativePrint = window.print;
    window.print = function () {
      send({ kind: 'print', desc: 'The page asks the browser to print' });
      return nativePrint.apply(window, arguments);
    };
  } catch (e) {}

  // The toolbar, on the top page only; put back if the page's script removes it.
  if (IS_TOP) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ensureHud);
    else ensureHud();
    setInterval(() => { if (document.documentElement && !(hudHost && hudHost.isConnected)) ensureHud(); }, 2000);
  }
})();
"""


# --------------------------------------------------------------------------
# Python side: turns events into portal steps.
# --------------------------------------------------------------------------
def escape_locator(value):
    """Text that happens to contain || or end in ' @2' must stay literal in the portal."""
    value = value.replace("||", "\\||")
    return _INDEX_SUFFIX_RE.sub(lambda m: m.group(1) + "\\@" + m.group(2), value)


def one_line(text, limit=MAX_TEXT):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return cut[:space] if space > limit // 3 else cut


class Recorder:
    def __init__(self, start_url, name, description="", keep_username=False, absolute_urls=False,
                 out_path=None, verbose=True, is_block=False, depends_on="", teardown="",
                 start_paused=False):
        # Paused: nothing the user does is recorded, for example while signing in.
        self.paused = bool(start_paused)
        self._paused_url = ""
        self._message = ""   # shown on the toolbar with the answer to the current event
        self.is_block = is_block
        self.depends_on = depends_on or ""
        self.teardown = teardown or ""
        self.start_url = start_url
        self.origin = self._origin(start_url)
        self.name = name
        self.description = description or f"Recorded on {datetime.now():%Y-%m-%d %H:%M} from {start_url}"
        self.keep_username = keep_username
        self.absolute_urls = absolute_urls
        self.out_path = Path(out_path) if out_path else self._default_out()
        self.verbose = verbose
        self.steps = []
        self.notes = []          # (step dict or None, text)
        self.pending_nav = None
        self.last_action_ts = 0.0
        self.last_action_page = None
        self.page_action_ts = {}
        self.current = None
        self.main = None
        self.windows = []
        self.starting = True
        self.frame_cache = {}
        self.deferred_frames = []   # (step, frame) for steps inside cross-site iframes
        self._frame_for_next = None
        self.unconfirmed = {}       # new tabs not yet known to be application windows
        self.dirty = False
        self.saved_ok = False
        self.stopped = False
        self._save_warned = False
        # Set when a page load is answered with 401 + WWW-Authenticate (Windows /
        # basic sign-in). Playwright never shows the browser's own sign-in pop-up:
        # with no http_credentials it cancels the challenge and the page stays blank.
        self.auth_challenge = None   # {"host", "scheme", "with_credentials", "since"}
        self.auth_blocked = None     # the challenge that stopped the recording

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _origin(url):
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    def _default_out(self):
        slug = re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_")[:60] or "Recorded_test"
        return DEFAULT_OUT_DIR / f"{slug}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"

    def _log(self, text):
        if self.verbose:
            print(text, flush=True)

    def _note(self, step, text):
        self.notes.append((step, text))

    def add(self, keyword, locator_type="", locator_value="", input_data="", description="",
            store="", page=None, note=""):
        step = {
            "keyword": keyword, "locator_type": locator_type or "", "locator_value": locator_value or "",
            "input_data": "" if input_data is None else str(input_data), "store": store,
            "description": description or "", "page": page,
        }
        if self.paused:
            return step  # not part of the test case
        self.steps.append(step)
        if self._frame_for_next is not None and step["locator_value"]:
            self.deferred_frames.append((step, self._frame_for_next))
            self._frame_for_next = None
        if note:
            self._note(step, note)
        if keyword in ACTION_KEYWORDS:
            self.last_action_ts = time.time()
            self.last_action_page = page
            if page is not None:
                self.page_action_ts[page] = self.last_action_ts
        self.dirty = True
        shown = " ".join(part for part in (
            keyword, f"[{locator_type}={locator_value}]" if locator_value else "",
            "" if not input_data else f'"{one_line(input_data, 50)}"') if part)
        self._log(f"  {len(self.steps):3d}. {shown}")
        return step

    def _relative(self, url):
        if self.absolute_urls or self._origin(url) != self.origin:
            return url
        parts = urlsplit(url)
        rel = parts.path or "/"
        if parts.query:
            rel += "?" + parts.query
        if parts.fragment:
            rel += "#" + parts.fragment
        return rel

    def _url_check(self, url):
        """Part of the address a VERIFY_URL can look for: the path (and an SPA #/route)."""
        parts = urlsplit(url)
        path = parts.path or ""
        if parts.fragment.startswith(("/", "!")):
            path += "#" + parts.fragment
        if path in ("", "/"):
            return None
        if self._origin(url) != self.origin:
            return parts.netloc + path
        return path

    def _window_hint(self, page):
        # Only page.url here: it is known locally, and a Playwright call from inside an
        # event callback can hang the recording.
        if page is self.main:
            return "main"
        path = urlsplit(page.url).path
        return path if path not in ("", "/") else page.url

    # ---- locators ----------------------------------------------------------
    def _frame_parts(self, frame):
        """Frame selectors asked from Playwright. Never call this from an event callback:
        a Playwright call made inside the binding callback hangs the recording."""
        parts = []
        while frame is not None and frame.parent_frame is not None:
            selector = self.frame_cache.get(frame)
            if selector is None:
                try:
                    handle = frame.frame_element()
                    selector = handle.evaluate(
                        "el => window.__qaFrameSelector ? window.__qaFrameSelector(el) : ''") or "iframe"
                except PlaywrightError:
                    selector = "iframe"
                self.frame_cache[frame] = selector
            parts.insert(0, selector)
            frame = frame.parent_frame
        return parts

    def resolve_deferred_frames(self):
        """Prefix the steps recorded inside cross-site iframes. Called from the main loop."""
        pending, self.deferred_frames = self.deferred_frames, []
        for step, frame in pending:
            parts = self._frame_parts(frame)
            if parts and step["locator_value"]:
                step["locator_value"] = " || ".join(parts + [step["locator_value"]])
                self.dirty = True

    def _locator(self, frame, loc, frames, pick=True):
        """(locator_type, locator_value, warning, needs_frames) from the browser's pick.
        pick=False leaves out @N, for counts, which must match every element."""
        if not loc:
            return "", "", "", False
        locator_type = loc.get("type") if loc.get("type") in LOCATOR_TYPES else "css"
        value = escape_locator(str(loc.get("value") or ""))
        if pick and int(loc.get("count") or 1) > 1:
            value += f" @{int(loc.get('index') or 1)}"
        in_frame = frame is not None and frame.parent_frame is not None
        if frames:
            value = " || ".join(list(frames) + [value])
        return locator_type, value, loc.get("warn") or "", in_frame and frames is None

    # ---- navigation ----------------------------------------------------------
    def on_navigated(self, page, frame):
        if frame.parent_frame is not None or self.paused:
            return
        url = frame.url
        if not url.lower().startswith(("http://", "https://")):
            return
        if page in self.unconfirmed:
            self._confirm_window(page)
            # The pop-up's first page was opened by the click: only a later redirect counts.
            self.last_action_ts, self.last_action_page = time.time(), page
        now = time.time()
        pending = self.pending_nav
        if pending and pending["page"] is page and now - pending["last"] < CHAIN_NAV_SECONDS:
            pending["url"], pending["last"] = url, now
            return
        self.flush_nav()
        caused = (now - self.last_action_ts < CAUSED_NAV_SECONDS) and self.last_action_page is page
        self.pending_nav = {"page": page, "first": url, "url": url, "caused": caused, "last": now}

    def flush_nav(self):
        pending, self.pending_nav = self.pending_nav, None
        if not pending:
            return
        page = pending["page"]
        if not pending["caused"]:
            self._switch_to(page)
            self.add("NAVIGATE", input_data=self._relative(pending["first"]),
                     description="Open " + (self._url_check(pending["first"]) or "home page"), page=page)
            self.last_action_ts = 0.0  # a typed address is not a step that causes later changes
        check = self._url_check(pending["url"])
        if not check:
            return
        previous = self.steps[-1] if self.steps else None
        if previous and previous["keyword"] == "NAVIGATE" and self._url_check(
                previous["input_data"] if "://" in previous["input_data"] else self.origin + previous["input_data"]) == check:
            return
        if previous and previous["keyword"] == "VERIFY_URL" and previous["input_data"] == check:
            return
        self.add("VERIFY_URL", input_data=check, description=f"Page {check} is open", page=page)

    # ---- windows ---------------------------------------------------------------
    def _switch_to(self, page):
        if page is None or page is self.current:
            return
        if page in self.windows:
            self.add("SWITCH_TO_WINDOW", input_data=self._window_hint(page),
                     description="Go to the " + ("main window" if page is self.main else "other window"), page=page)
        self.current = page

    def on_page(self, page):
        page.on("framenavigated", lambda frame: self._safe(self.on_navigated, page, frame))
        page.on("close", lambda _p=None: self._safe(self.on_close, page))
        page.on("dialog", lambda dialog: None)  # leave it open for the user to answer
        page.on("download", lambda download: self._safe(self.on_download, page, download))
        if self.starting:
            return
        # Decided when it shows a web page: Edge opens edge://downloads-hub/ as a tab on
        # every download, and that is not a window of the application.
        self.unconfirmed[page] = (time.time(), self.last_action_ts)

    def _confirm_window(self, page):
        created, action_ts = self.unconfirmed.pop(page)
        self.flush_nav()
        last = self.steps[-1] if self.steps else None
        recent = created - action_ts < NEW_WINDOW_SECONDS
        if last and recent and last["keyword"] == "CLICK":
            last["keyword"] = "CLICK_AND_SWITCH_WINDOW"
            last["description"] += " (opens a new window)"
            self._log("        -> click opens a new window: CLICK_AND_SWITCH_WINDOW")
            self.dirty = True
        else:
            note = "" if recent else ("If you opened this tab yourself, delete this step: the portal can "
                                      "only follow windows the application opens.")
            self.add("SWITCH_TO_NEW_WINDOW", description="Carry on in the new window", page=page, note=note)
        self.windows.append(page)
        self.current = page

    def on_close(self, page):
        if self.unconfirmed.pop(page, None) is not None:
            return
        if page in self.windows:
            self.windows.remove(page)
        if self.pending_nav and self.pending_nav["page"] is page:
            self.pending_nav = None
        if page is not self.current:
            return
        closed_by_app = time.time() - self.page_action_ts.get(page, 0) < SELF_CLOSE_SECONDS
        if self.windows and not closed_by_app and not self.stopped:
            self.add("CLOSE_WINDOW", description="Close this window", page=page)
        self.current = self.windows[-1] if self.windows else None

    def on_download(self, page, download):
        self.flush_nav()
        name = download.suggested_filename or ""
        pattern = re.sub(r"\d{4,}", "*", name)
        pattern = re.sub(r"\*+", "*", pattern)
        self.add("VERIFY_DOWNLOAD", input_data=pattern or "*",
                 description=f"File {name} is downloaded" if name else "A file is downloaded", page=page,
                 note="Digits in the file name were replaced with *" if pattern != name else "")

    # ---- events from the page ------------------------------------------------
    def _safe(self, func, *args):
        try:
            func(*args)
        except Exception as exc:  # A recorder bug must never stop the recording.
            self._log(f"  [recorder] {type(exc).__name__}: {exc}")

    def on_event(self, source, event):
        """Returns what the toolbar shows. Only plain data: a Playwright call made here,
        inside the binding callback, would hang the recording."""
        self._safe(self._handle, source, event)
        message, self._message = self._message, ""
        return {"count": len(self.steps), "paused": self.paused, "message": message}

    def _control(self, page, ev):
        """Toolbar buttons that are not steps. True when the event was one of them."""
        kind = ev.get("kind")
        if kind == "hello":
            return True
        if kind == "finish":
            self.flush_nav()
            self.stopped = True
            return True
        if kind == "pause":
            self._set_paused(page, bool(ev.get("paused")))
            return True
        if kind == "undo":
            self.flush_nav()
            only_start = len(self.steps) == 1 and self.steps[0]["keyword"] == "NAVIGATE"
            if self.paused or not self.steps or only_start:
                self._message = "Nothing to undo."
                return True
            step = self.steps[-1]
            self._drop_last()
            self.dirty = True
            shown = one_line(step["description"] or step["keyword"], 50)
            self._log(f"        -> undo: removed step {len(self.steps) + 1} ({step['keyword']})")
            self._message = f"Removed: {shown}. The page itself is not changed back."
            return True
        return False

    def _set_paused(self, page, paused):
        if paused == self.paused:
            return
        if paused:
            self.flush_nav()
            self.paused = True
            self._paused_url = page.url if page is not None else ""
            self._log("        -- paused: nothing is recorded until Resume --")
            self._message = "Paused. What you do now is not recorded."
            return
        self.paused = False
        self.pending_nav = None
        self._log("        -- recording again --")
        self._message = "Recording again."
        if page is None:
            return
        self.unconfirmed.pop(page, None)
        if page not in self.windows:
            self.windows.append(page)
        self.current = page
        url = page.url  # known locally: not a call to the browser
        if url.lower().startswith(("http://", "https://")) and (not self.steps or url != self._paused_url):
            self.add("NAVIGATE", input_data=self._relative(url),
                     description="Open " + (self._url_check(url) or "home page"), page=page,
                     note="Recording was paused before this step. What you did while paused is not in "
                          "the test case; this step opens the page you were on when you resumed.")

    def _last_action_index(self, page):
        for index in range(len(self.steps) - 1, -1, -1):
            if self.steps[index]["keyword"] in ACTION_KEYWORDS and self.steps[index]["page"] is page:
                return index
        return None

    def _handle(self, source, ev):
        page, frame = source.get("page"), source.get("frame")
        kind = ev.get("kind")
        if self._control(page, ev):
            return
        if self.paused:
            self._message = "Paused: this is not recorded. Press Resume to carry on."
            return
        if page in self.unconfirmed:
            self._confirm_window(page)
        if kind == "dialog":
            self._on_dialog(page, ev)
            return
        self.flush_nav()
        self._switch_to(page)
        desc = one_line(ev.get("desc"), 120)
        if kind == "screenshot":
            self.add("CAPTURE_SCREENSHOT", input_data=ev.get("caption") or "Screenshot",
                     description="Screenshot as evidence", page=page)
            return
        if kind == "print":
            self.add("VERIFY_PRINT_CALLED", description=desc or "The page asks the browser to print", page=page)
            return
        ltype, lvalue, warn, needs_frames = self._locator(
            frame, ev.get("loc"), ev.get("frames"), pick=not ev.get("noPick"))
        self._frame_for_next = frame if needs_frames else None
        if kind == "manual":
            keyword = str(ev.get("keyword") or "")
            if keyword not in MANUAL_KEYWORDS:
                return
            store = ""
            if ev.get("store"):
                store = re.sub(r"[^A-Za-z0-9_]+", "_", str(ev["store"])).strip("_").upper() or "VALUE"
                if store[0].isdigit():
                    store = "V_" + store
            self.add(keyword, ltype, lvalue, ev.get("input") or "", desc, store=store, page=page, note=warn)
            return
        if kind == "drag":
            dest = ev.get("dest") or {}
            dtype = dest.get("type") if dest.get("type") in LOCATOR_TYPES else "css"
            dvalue = escape_locator(str(dest.get("value") or ""))
            if int(dest.get("count") or 1) > 1:
                dvalue += f" @{int(dest.get('index') or 1)}"
            note = warn
            if dest.get("warn"):
                note = (note + " " if note else "") + "Destination: " + dest["warn"]
            self.add("DRAG_AND_DROP", ltype, lvalue, f"{dtype}|{dvalue}", desc, page=page, note=note)
            return
        if kind == "click":
            self.add("CLICK", ltype, lvalue, description=desc, page=page, note=warn)
        elif kind == "dblclick":
            # The browser sent click, click, dblclick: keep one DOUBLE_CLICK.
            for _ in range(2):
                last = self.steps[-1] if self.steps else None
                if last and last["keyword"] == "CLICK" and last["locator_value"] == lvalue:
                    self._drop_last()
            self.add("DOUBLE_CLICK", ltype, lvalue, description=desc, page=page, note=warn)
        elif kind == "fill":
            self._on_fill(page, ev, ltype, lvalue, desc, warn)
        elif kind == "select":
            self.add("SELECT_DROPDOWN", ltype, lvalue, ev.get("value") or "", desc, page=page, note=warn)
        elif kind == "toggle":
            if ev.get("viaLabel"):
                self.add("CLICK", ltype, lvalue, description=desc, page=page, note=warn)
            else:
                keyword = "CHECK" if ev.get("checked") or ev.get("radio") else "UNCHECK"
                self.add(keyword, ltype, lvalue, description=desc, page=page, note=warn)
        elif kind == "upload":
            files = ev.get("files") or []
            if len(files) > 1:
                self._note(None, f"Several files were chosen ({', '.join(files)}); UPLOAD_FILE takes one.")
            self.add("UPLOAD_FILE", ltype, lvalue, files[0] if files else "", desc, page=page,
                     note=f"Copy {files[0] if files else 'the file'} into the portal's test_data folder."
                          + (" " + warn if warn else ""))
        elif kind == "key":
            self.add("PRESS_KEY", ltype, lvalue, ev.get("key") or "Enter", desc, page=page, note=warn)
        elif kind == "assert":
            mode = ev.get("mode")
            text = one_line(ev.get("text"), MAX_TEXT)
            if mode == "value":
                self.add("VERIFY_VALUE", ltype, lvalue, text, desc, page=page, note=warn)
            elif mode == "text" and text:
                self.add("VERIFY_TEXT_CONTAINS", ltype, lvalue, text, desc, page=page, note=warn)
            else:
                self.add("VERIFY_ELEMENT_VISIBLE", ltype, lvalue, description=desc, page=page, note=warn)

    def _drop_last(self):
        step = self.steps.pop()
        self.notes = [(s, t) for s, t in self.notes if s is not step]

    def _on_fill(self, page, ev, ltype, lvalue, desc, warn):
        value = ev.get("value")
        value = "" if value is None else str(value)
        keyword = "TYPE_SEQUENTIALLY" if ev.get("typed") and value else "FILL"
        # Typing again into the same field replaces the earlier value.
        last = self.steps[-1] if self.steps else None
        if last and last["keyword"] in ("FILL", "TYPE_SEQUENTIALLY") and last["locator_value"] == lvalue:
            self._drop_last()
        if ev.get("secret"):
            self.add(keyword, ltype, lvalue, "{AUTH_PASSWORD}", desc, page=page,
                     note="Password not saved: uses {AUTH_PASSWORD} (the run's credentials)."
                          + (" " + warn if warn else ""))
            self._tokenise_username(page)
            return
        if "{" in value and re.search(r"\{[A-Za-z_][A-Za-z0-9_+-]*\}", value):
            warn = (warn + " " if warn else "") + "The typed text contains {...}: the portal reads that as a variable."
        self.add(keyword, ltype, lvalue, value, desc, page=page, note=warn)

    def _tokenise_username(self, page):
        if self.keep_username:
            return
        for step in reversed(self.steps[-5:-1]):
            if step["keyword"] in ("FILL", "TYPE_SEQUENTIALLY") and step["page"] is page:
                hint = step["locator_value"] + " " + step["description"]
                if _USERNAME_HINT_RE.search(hint) and step["input_data"] not in ("", "{AUTH_USERNAME}"):
                    step["input_data"] = "{AUTH_USERNAME}"
                    self._note(step, "User name replaced with {AUTH_USERNAME} (the run's credentials).")
                    self._log("        -> user name field now uses {AUTH_USERNAME}")
                return

    def _on_dialog(self, page, ev):
        kind, result = ev.get("type"), ev.get("result")
        if kind == "confirm":
            answer = "accept" if result else "dismiss"
        elif kind == "prompt":
            answer = "dismiss" if result is None else f"accept|{result}"
        else:
            answer = "accept"
        step = {"keyword": "PREPARE_DIALOG", "locator_type": "", "locator_value": "", "input_data": answer,
                "store": "", "description": f"Answer the next {kind} with {answer.split('|')[0]}", "page": page}
        index = self._last_action_index(page)
        if index is None:
            self.steps.append(step)
        else:
            self.steps.insert(index, step)
        self._log(f"        -> {kind} answered '{answer}': PREPARE_DIALOG put before step {(index or len(self.steps) - 1) + 1}")
        message = one_line(ev.get("message"), MAX_TEXT)
        if message:
            self.add("VERIFY_DIALOG_TEXT", input_data=message, description=f"The {kind} says the expected text",
                     page=page)
        self.dirty = True

    # ---- saving --------------------------------------------------------------
    def rows(self):
        out = []
        for order, step in enumerate(self.steps, start=1):
            first = order == 1
            out.append([
                self.name, self.description if first else "",
                ("yes" if self.is_block else "no") if first else "",
                self.depends_on if first else "", order,
                step["keyword"], step["locator_type"], step["locator_value"], step["input_data"],
                step["store"], step["description"], self.teardown if first else "",
            ])
        return out

    def save(self):
        path = self.out_path
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook = build_workbook(self.rows(), self._note_rows(), self.start_url)
        temp = path.with_name(path.stem + ".tmp.xlsx")
        try:
            workbook.save(temp)
            os.replace(temp, path)
            self.saved_ok = True
        except PermissionError:
            if not self._save_warned:
                self._log(f"  [recorder] {path.name} is open in Excel; close it so it can be saved.")
                self._save_warned = True
            try:
                temp.unlink()
            except OSError:
                pass
            return False
        self.dirty = False
        return True

    def _note_rows(self):
        index = {id(step): n for n, step in enumerate(self.steps, start=1)}
        rows = []
        for step, text in self.notes:
            if step is None:
                rows.append(["", text])
            elif id(step) in index:
                rows.append([index[id(step)], text])
        rows.sort(key=lambda r: (r[0] == "", r[0] if r[0] != "" else 0))
        return rows


def build_workbook(rows, notes, start_url):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Test Steps"   # The portal imports the first sheet.
    sheet.append(IMPORT_COLUMNS)
    head_fill = PatternFill("solid", fgColor="2B2722")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="F3E6C8")
        cell.fill = head_fill
    for number, row in enumerate(rows, start=2):
        for column, value in enumerate(row, start=1):
            cell = sheet.cell(row=number, column=column)
            if isinstance(value, str):
                # A step's text is never a formula, even one like =Total.
                cell.value = ILLEGAL_CHARACTERS_RE.sub("", value)
                cell.data_type = "s"
                cell.quotePrefix = value.startswith(("=", "+", "-", "@"))
            else:
                cell.value = value
    for column, width in zip("ABCDEFGHIJKL", (22, 34, 12, 14, 8, 26, 14, 42, 30, 18, 40, 16)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"
    info = workbook.create_sheet("Recorder Notes")
    info.append(["step_order", "note"])
    for cell in info[1]:
        cell.font = Font(bold=True)
    info.append(["", f"Recorded from {start_url} on {datetime.now():%Y-%m-%d %H:%M}."])
    info.append(["", "Import: portal > Applications > your application > Test cases > Import this file."])
    info.append(["", "Set the application's Base URL to the site you recorded: same-site addresses are relative."])
    for row in notes:
        info.append(row)
    info.column_dimensions["A"].width = 11
    info.column_dimensions["B"].width = 110
    for row in info.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    # Reference sheet and drop-downs, as in the portal's own import template.
    reference = workbook.create_sheet("Keywords")
    reference.append(["keyword", "category", "locator", "input_data", "stores_variable", "what_it_does"])
    for cell in reference[1]:
        cell.font = Font(bold=True, color="F3E6C8")
        cell.fill = head_fill
    portal = _portal_keywords()
    if portal:
        for name, spec in portal.items():
            stores = {True: "yes", False: "no"}.get(spec["store"], spec["store"])
            reference.append([name, spec["category"], spec["locator"], spec["input"], stores, spec["help"]])
        count = len(portal)
    else:
        for name in FALLBACK_KEYWORDS:
            reference.append([name])
        count = len(FALLBACK_KEYWORDS)
    for column, width in zip("ABCDEF", (26, 26, 10, 10, 15, 90)):
        reference.column_dimensions[column].width = width
    reference.freeze_panes = "A2"
    keywords = DataValidation(type="list", formula1=f"=Keywords!$A$2:$A${count + 1}", allow_blank=True)
    locators = DataValidation(type="list", formula1='"' + ",".join(LOCATOR_TYPES) + '"', allow_blank=True)
    sheet.add_data_validation(keywords)
    sheet.add_data_validation(locators)
    keywords.add("F2:F5001")
    locators.add("G2:G5001")
    return workbook


_PORTAL_KEYWORDS = []


def _portal_keywords():
    """The portal's keyword registry when this script sits in the portal, else None."""
    if not _PORTAL_KEYWORDS:
        try:
            sys.path.insert(0, str(PORTAL_DIR))
            from engine.keywords import KEYWORDS  # optional: only inside the portal
            _PORTAL_KEYWORDS.append(KEYWORDS)
        except Exception:
            _PORTAL_KEYWORDS.append(None)
        finally:
            if sys.path and sys.path[0] == str(PORTAL_DIR):
                sys.path.pop(0)
    return _PORTAL_KEYWORDS[0]


def validate_with_portal(recorder):
    """Run every step through the portal's own validator. Returns a list of problems."""
    try:
        sys.path.insert(0, str(PORTAL_DIR))
        from engine.keywords import validate_step
    except Exception:
        return None
    finally:
        if sys.path and sys.path[0] == str(PORTAL_DIR):
            sys.path.pop(0)
    problems = []
    for order, step in enumerate(recorder.steps, start=1):
        _clean, errors = validate_step({
            "action_keyword": step["keyword"], "locator_type": step["locator_type"],
            "locator_value": step["locator_value"], "input_data": step["input_data"],
            "store_variable_name": step["store"], "description": step["description"],
        })
        problems.extend(f"Step {order}: {error}" for error in errors)
    return problems


# --------------------------------------------------------------------------
# Browser start-up and the recording loop
# --------------------------------------------------------------------------
def launch(playwright, browser_name, headless, context_options=None, profile_dir=None):
    """Returns (browser, context). With profile_dir the context is a normal
    (not private) browser profile in that folder and browser is None.

    Why a normal profile: Edge/Chrome never sign in with the logged-in Windows
    account (Kerberos/NTLM single sign-on) in a private window, and every
    browser.new_context() is a private window. The site then asks for the
    Windows Security pop-up, which Playwright cancels: the page shows 401."""
    args = ["--start-maximized"]
    order = [browser_name] + [b for b in ("msedge", "chrome", "chromium") if b != browser_name]
    last_error = None
    for name in order:
        try:
            options = {"headless": headless, "args": args}
            if name != "chromium":
                options["channel"] = name
            if profile_dir:
                context = playwright.chromium.launch_persistent_context(
                    str(profile_dir), **options, **(context_options or {}))
                browser = None
            else:
                browser = playwright.chromium.launch(**options)
                context = None
            if name != browser_name:
                print(f"{browser_name} could not start; using {name} instead.")
            if browser is not None and context_options is not None:
                context = browser.new_context(**context_options)
            return browser, context
        except PlaywrightError as exc:
            last_error = exc
    raise SystemExit(f"No browser could be started: {last_error}")


AUTH_CHALLENGE_WAIT = 4  # seconds a page may stay on a sign-in challenge before the recording stops


def auth_blocked_lines(challenge):
    """What the tester is told when the site's Windows/basic sign-in stopped the recording."""
    host, scheme = challenge["host"], challenge["scheme"]
    if challenge["with_credentials"]:
        return [f"{host} did not accept the Windows sign-in ({scheme}) typed in section 3.",
                "Check Domain (for example VOLT), User name and Password, and record again.",
                "If a normal Edge window opens the site without asking, leave section 3 EMPTY: "
                "the recorder then signs in with your Windows login."]
    return [f"{host} asks for a Windows sign-in ({scheme}) and your Windows login was not accepted.",
            "The recording browser cannot show the Windows Security pop-up.",
            "Fill section 3 (Windows sign-in): Domain, User name and Password, the same ones you type "
            "in the pop-up, and press Start recording again."]


def record(recorder, browser_name="msedge", headless=False, ignore_https_errors=False,
           on_ready=None, http_credentials=None):
    """Record until Finish is pressed or the browser is closed (or on_ready returns).
    on_ready(page) is for tests. http_credentials answers the browser's own sign-in
    prompt: {"username": "DOMAIN\\user", "password": "..."}. Without them the
    browser runs on a fresh normal profile so it signs in with the logged-in
    Windows account, as a normal Edge window does; the profile is deleted after."""
    options = {"no_viewport": not headless, "ignore_https_errors": ignore_https_errors,
               "accept_downloads": True}
    profile_dir = None
    if http_credentials:
        options["http_credentials"] = http_credentials
    else:
        profile_dir = tempfile.mkdtemp(prefix="qa_recorder_profile_")
    with sync_playwright() as playwright:
        browser, context = launch(playwright, browser_name, headless, options, profile_dir)
        stop = lambda _x=None: setattr(recorder, "stopped", True)  # noqa: E731
        if browser is not None:
            browser.on("disconnected", stop)
        else:
            context.on("close", stop)

        def on_response(response):
            # Local properties only (no Playwright round trip inside an event handler).
            # An NTLM/Negotiate handshake passes through 401s even when it succeeds,
            # so only the latest page load counts; the main loop acts when it stays 401.
            try:
                if not response.request.is_navigation_request() or response.frame.parent_frame is not None:
                    return
                challenge = response.headers.get("www-authenticate", "") if response.status == 401 else ""
                if not challenge:
                    recorder.auth_challenge = None  # signed in, or the application's own 401 page
                    return
                recorder.auth_challenge = {
                    "host": urlsplit(response.url).netloc,
                    "scheme": challenge.split()[0],
                    "with_credentials": bool(http_credentials),
                    "since": time.time(),
                }
            except Exception:
                pass

        context.on("response", on_response)
        context.expose_binding("__qaRecord", recorder.on_event)
        context.add_init_script(RECORDER_JS)
        context.on("page", recorder.on_page)
        existing = [p for p in context.pages if not p.is_closed()]
        if existing:  # a normal profile opens with one blank tab: record in that one
            page = existing[0]
            recorder.on_page(page)
            for extra in existing[1:]:
                extra.close()
        else:
            page = context.new_page()
        recorder.main = recorder.current = page
        recorder.windows = [page]
        recorder.add("NAVIGATE", input_data=recorder._relative(recorder.start_url),
                     description="Open the application", page=page)
        recorder.starting = False
        # The first page load is caused by this step: only a redirect adds a VERIFY_URL.
        recorder.last_action_ts, recorder.last_action_page = time.time(), page
        recorder._log(f"Opening {recorder.start_url}")
        try:
            page.goto(recorder.start_url, wait_until="commit")
        except PlaywrightError as exc:
            recorder._log(f"  [recorder] the page did not open: {exc}")
        if recorder.paused:
            recorder._log("Paused. Sign in, go to where the test case starts, then press Resume on the toolbar.\n")
        else:
            recorder._log("Recording. Use the application; press Finish on the toolbar when you are done.\n")
        try:
            if on_ready is not None:
                on_ready(page)
            else:
                while not recorder.stopped:
                    pages = [p for p in context.pages if not p.is_closed()]
                    if not pages:
                        break
                    try:
                        pages[-1].wait_for_timeout(250)
                    except PlaywrightError:
                        pass
                    challenge = recorder.auth_challenge
                    if challenge and time.time() - challenge["since"] > AUTH_CHALLENGE_WAIT:
                        recorder.auth_blocked = challenge
                        recorder._log(f"  [recorder] {challenge['host']} asks for a {challenge['scheme']} "
                                      "sign-in that the recording browser cannot show. Stopping.")
                        break
                    if recorder.deferred_frames:
                        recorder.resolve_deferred_frames()
                    if recorder.dirty:
                        recorder.save()
        except KeyboardInterrupt:
            recorder._log("\nStopped with Ctrl+C.")
        finally:
            recorder.stopped = True
            recorder.flush_nav()
            try:
                recorder.resolve_deferred_frames()
            except Exception:
                pass
            recorder.save()
            try:
                (browser or context).close()
            except Exception:
                pass
    if profile_dir:
        # Nothing of the recording session (cookies, cache) is kept for the next one.
        shutil.rmtree(profile_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Sending the test case to the portal. Signs in the way a browser does (form
# and session cookie), so the portal needs no separate keys. Standard library
# only: the laptop that records has no internet to install anything from.
# --------------------------------------------------------------------------
class PortalError(Exception):
    """A problem talking to the portal, worded for the tester."""


class PortalClient:
    def __init__(self, base_url, verify_ssl=True):
        base = (base_url or "").strip().rstrip("/")
        if base and not base.lower().startswith(("http://", "https://")):
            base = "http://" + base
        self.base = base
        self.user = ""
        self._token = ""
        # The portal is on the intranet: never send it through an internet proxy.
        handlers = [urllib.request.ProxyHandler({}),
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if not verify_ssl:
            handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def _open(self, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with self._opener.open(request, timeout=60) as response:
                return response.status, response.geturl(), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.geturl(), exc.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PortalError(f"Could not reach the portal at {self.base} ({getattr(exc, 'reason', exc)}).") from None

    @staticmethod
    def _json(body):
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def login(self, username, password):
        if not self.base:
            raise PortalError("Enter the portal address.")
        _status, _url, body = self._open("/login")
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body.decode("utf-8", "replace"))
        if not match:
            raise PortalError(f"{self.base} does not look like the QA Automation Portal.")
        form = urllib.parse.urlencode(
            {"csrf_token": match.group(1), "username": username, "password": password}).encode()
        status, url, _body = self._open("/login", form, {"Content-Type": "application/x-www-form-urlencoded"})
        if status == 429:
            raise PortalError("Too many failed sign-in attempts. Try again in a few minutes.")
        if status == 403:
            raise PortalError("This portal account is deactivated.")
        if status != 200 or urlsplit(url).path.rstrip("/").endswith("/login"):
            raise PortalError("The portal did not accept that user name and password.")
        return self.applications()

    def applications(self):
        """[{id, name, base_url}], and the token later posts need."""
        status, _url, body = self._open("/api/recorder/applications", headers={"Accept": "application/json"})
        data = self._json(body)
        if status == 403:
            raise PortalError("Your portal account may not create test cases (it is a Guest).")
        if status == 404:
            raise PortalError("This portal is an older version that cannot take recordings. "
                              "Import the saved Excel file in the portal instead.")
        if status != 200 or data is None:
            raise PortalError("The portal session ended. Sign in again.")
        self._token = data.get("csrf_token") or ""
        self.user = data.get("user") or ""
        return data.get("applications") or []

    def upload(self, app_id, path, overwrite):
        """Returns (ok, lines to show)."""
        boundary = "----qarecorder" + uuid.uuid4().hex
        parts = []

        def field(name, value):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())

        field("csrf_token", self._token)
        if overwrite:
            field("overwrite_existing", "on")
        parts.append((
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.xlsx"\r\n'
            "Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n").encode())
        parts.append(Path(path).read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        status, _url, body = self._open(
            f"/api/recorder/applications/{int(app_id)}/import", b"".join(parts),
            {"Content-Type": f"multipart/form-data; boundary={boundary}", "X-CSRF-Token": self._token,
             "Accept": "application/json"})
        data = self._json(body)
        if data is None:
            raise PortalError("The portal session ended before the test case was sent. "
                              "Import the saved Excel file in the portal instead.")
        if data.get("ok"):
            return True, [data.get("message") or "Imported."]
        return False, list(data.get("errors") or [f"The portal refused the test case (HTTP {status})."])


def send_to_portal(client, app_id, recorder, overwrite):
    """Lines describing what happened. Never raises: the Excel file is the fallback."""
    try:
        ok, lines = client.upload(app_id, recorder.out_path, overwrite)
    except PortalError as exc:
        return False, [str(exc)]
    if ok:
        return True, ["Sent to the portal: " + lines[0]]
    shown = lines[:10] + ([f"... and {len(lines) - 10} more"] if len(lines) > 10 else [])
    return False, ["The portal did not accept the test case:"] + ["  - " + line for line in shown]


# --------------------------------------------------------------------------
# One recording, from settings to result. Used by the command line and the
# start window alike.
# --------------------------------------------------------------------------
def run_recording(settings, client=None, app_id=None):
    """settings: dict with url, name and the optional keys read below. Returns
    (exit code, lines to show the tester)."""
    name = settings["name"]
    out_path = settings.get("out") or build_output_path(
        "record_test_case", DEFAULT_OUT_DIR, "{name}", {"name": name}, settings.get("config") or None)
    recorder = Recorder(
        settings["url"], name, settings.get("description", ""),
        keep_username=settings.get("keep_username", False), absolute_urls=settings.get("absolute_urls", False),
        out_path=out_path, is_block=settings.get("block", False), depends_on=settings.get("depends_on", ""),
        teardown=settings.get("teardown", ""), start_paused=settings.get("start_paused", False))
    credentials = None
    if settings.get("auth_user") and settings.get("auth_password"):
        domain = (settings.get("auth_domain") or "").strip().rstrip("\\")
        user = settings["auth_user"].strip()
        username = f"{domain}\\{user}" if (domain and "\\" not in user and "@" not in user) else user
        credentials = {"username": username, "password": settings["auth_password"]}
    record(recorder, settings.get("browser", "msedge"), ignore_https_errors=settings.get("ignore_https", False),
           http_credentials=credentials)

    if recorder.auth_blocked:
        return 1, auth_blocked_lines(recorder.auth_blocked) + ["Nothing was sent to the portal."]
    lines = [f"{len(recorder.steps)} step(s) recorded."]
    if not recorder.saved_ok:
        return 1, lines + [f"Could not save {recorder.out_path}: close it in Excel and record again."]
    if not recorder.steps:
        return 1, lines + ["Nothing was recorded, so nothing was sent."]
    problems = validate_with_portal(recorder)
    if problems:
        lines.append("These steps need fixing in the portal or the workbook:")
        lines += ["  - " + problem for problem in problems]
    elif problems is not None:
        lines.append("All steps pass the portal's step validation.")
    code = 0
    if client is not None and app_id is not None:
        ok, sent = send_to_portal(client, app_id, recorder, settings.get("overwrite", False))
        lines += sent
        if not ok:
            code = 2
            lines.append("The Excel file below can be imported by hand: portal > application > Test cases > Import.")
    lines.append(f"Saved: {recorder.out_path}")
    return code, lines


# --------------------------------------------------------------------------
# Start window (Tk, part of Python): what the tester sees after a double-click.
# --------------------------------------------------------------------------
SETTINGS_FILE = Path.home() / ".qa_recorder.json"
REMEMBERED = ("portal", "portal_user", "url", "browser", "auth_domain", "auth_user", "ignore_https", "app_name")


def load_remembered():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_remembered(settings):
    """Addresses and names only. Passwords are never written to disk."""
    try:
        SETTINGS_FILE.write_text(
            json.dumps({key: settings.get(key, "") for key in REMEMBERED}, indent=2), encoding="utf-8")
    except OSError:
        pass


def start_window(remembered, session):
    """Ask what to record. Returns the settings dict, or None when the window is closed.
    session keeps the portal connection between recordings: {client, applications}."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Test Case Recorder")
    root.resizable(False, False)
    result = {}
    pad = {"padx": 8, "pady": 3}

    def text_var(key, default=""):
        return tk.StringVar(value=str(remembered.get(key) or default))

    portal, portal_user, portal_password = text_var("portal"), text_var("portal_user"), tk.StringVar()
    app_name, url, name = tk.StringVar(), text_var("url"), tk.StringVar()
    overwrite, start_paused = tk.BooleanVar(), tk.BooleanVar()
    auth_domain, auth_user, auth_password = text_var("auth_domain"), text_var("auth_user"), tk.StringVar()
    browser = text_var("browser", "msedge")
    ignore_https = tk.BooleanVar(value=bool(remembered.get("ignore_https")))
    block, depends_on, teardown = tk.BooleanVar(), tk.StringVar(), tk.StringVar()
    status = tk.StringVar(value="Not connected. Without the portal, the test case is saved as an Excel file only.")

    def row(parent, line, label, variable, show="", width=44):
        ttk.Label(parent, text=label).grid(row=line, column=0, sticky="w", **pad)
        entry = ttk.Entry(parent, textvariable=variable, show=show, width=width)
        entry.grid(row=line, column=1, sticky="we", **pad)
        return entry

    box = ttk.LabelFrame(root, text="1. Portal (where the test case is sent)")
    box.grid(row=0, column=0, sticky="we", padx=10, pady=(10, 4))
    row(box, 0, "Portal address", portal)
    row(box, 1, "Your portal user name", portal_user)
    password_entry = row(box, 2, "Your portal password", portal_password, show="*")
    apps = ttk.Combobox(box, textvariable=app_name, state="readonly", width=41)

    def show_applications():
        names = [item["name"] for item in session.get("applications", [])]
        apps["values"] = names
        wanted = remembered.get("app_name")
        if names and app_name.get() not in names:
            app_name.set(wanted if wanted in names else names[0])
            application_chosen()
        if session.get("client") is not None:
            status.set(f"Connected as {session['client'].user}. {len(names)} application(s).")

    def connect():
        client = PortalClient(portal.get(), verify_ssl=not ignore_https.get())
        root.config(cursor="watch")
        root.update_idletasks()
        try:
            session["applications"] = client.login(portal_user.get().strip(), portal_password.get())
            session["client"] = client
        except PortalError as exc:
            session.pop("client", None)
            session["applications"] = []
            status.set(str(exc))
            apps["values"] = []
            app_name.set("")
            return
        finally:
            root.config(cursor="")
        show_applications()

    def application_chosen(_event=None):
        for item in session.get("applications", []):
            if item["name"] == app_name.get() and item.get("base_url"):
                url.set(item["base_url"])

    ttk.Button(box, text="Connect", command=connect).grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(box, text="Application").grid(row=4, column=0, sticky="w", **pad)
    apps.grid(row=4, column=1, sticky="we", **pad)
    apps.bind("<<ComboboxSelected>>", application_chosen)
    ttk.Label(box, textvariable=status, wraplength=430, foreground="#555").grid(
        row=5, column=0, columnspan=2, sticky="w", **pad)
    password_entry.bind("<Return>", lambda _event: connect())

    box = ttk.LabelFrame(root, text="2. What to record")
    box.grid(row=1, column=0, sticky="we", padx=10, pady=4)
    row(box, 0, "Address to open", url)
    name_entry = row(box, 1, "Test case name", name)
    ttk.Checkbutton(box, variable=overwrite,
                    text="Replace the test case in the portal if this name already exists").grid(
        row=2, column=0, columnspan=2, sticky="w", **pad)
    ttk.Checkbutton(box, variable=start_paused,
                    text="Start paused: I will sign in first, then press Resume on the toolbar").grid(
        row=3, column=0, columnspan=2, sticky="w", **pad)

    box = ttk.LabelFrame(root, text="3. Windows sign-in (leave EMPTY to sign in with your Windows login)")
    box.grid(row=2, column=0, sticky="we", padx=10, pady=4)
    row(box, 0, "Domain", auth_domain)
    row(box, 1, "User name", auth_user)
    row(box, 2, "Password", auth_password, show="*")

    box = ttk.LabelFrame(root, text="More options")
    box.grid(row=3, column=0, sticky="we", padx=10, pady=4)
    ttk.Label(box, text="Browser").grid(row=0, column=0, sticky="w", **pad)
    ttk.Combobox(box, textvariable=browser, state="readonly", values=["msedge", "chrome"], width=41).grid(
        row=0, column=1, sticky="we", **pad)
    ttk.Checkbutton(box, variable=ignore_https, text="Accept untrusted (self-signed) certificates").grid(
        row=1, column=0, columnspan=2, sticky="w", **pad)
    ttk.Checkbutton(box, variable=block, text="Save as a reusable block").grid(
        row=2, column=0, columnspan=2, sticky="w", **pad)
    row(box, 3, "Depends on (test case)", depends_on)
    row(box, 4, "Teardown block", teardown)

    def start():
        address = url.get().strip()
        if not address.lower().startswith(("http://", "https://")):
            messagebox.showerror("Test Case Recorder", "The address to open must start with http:// or https://")
            return
        if not name.get().strip():
            messagebox.showerror("Test Case Recorder", "Give the test case a name.")
            name_entry.focus_set()
            return
        chosen = next((item for item in session.get("applications", []) if item["name"] == app_name.get()), None)
        if session.get("client") is None and not messagebox.askyesno(
                "Test Case Recorder", "You are not connected to the portal, so the test case will only be "
                "saved as an Excel file to import by hand.\n\nRecord anyway?"):
            return
        result.update({
            "url": address, "name": name.get().strip(), "overwrite": overwrite.get(),
            "start_paused": start_paused.get(), "auth_domain": auth_domain.get().strip(),
            "auth_user": auth_user.get().strip(), "auth_password": auth_password.get(),
            "browser": browser.get() or "msedge", "ignore_https": ignore_https.get(), "block": block.get(),
            "depends_on": depends_on.get().strip(), "teardown": teardown.get().strip(),
            "portal": portal.get().strip(), "portal_user": portal_user.get().strip(),
            "app_name": app_name.get(), "app_id": chosen["id"] if chosen else None,
        })
        root.destroy()

    buttons = ttk.Frame(root)
    buttons.grid(row=4, column=0, sticky="e", padx=10, pady=(4, 10))
    ttk.Button(buttons, text="Close", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(buttons, text="Start recording", command=start).grid(row=0, column=1, padx=4)

    show_applications()
    (name_entry if portal.get() and session.get("client") is not None else password_entry).focus_set()
    root.mainloop()
    return result or None


def show_result(title, lines):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(title, "\n".join(lines), parent=root)
    root.destroy()


def run_window():
    """Start window, record, show the result, and back to the start window."""
    session = {}
    remembered = load_remembered()
    while True:
        settings = start_window(remembered, session)
        if settings is None:
            return 0
        remembered.update(settings)
        save_remembered(remembered)
        try:
            _code, lines = run_recording(settings, session.get("client"), settings.get("app_id"))
        except SystemExit as exc:  # no browser could be started
            lines = [str(exc)]
        except Exception as exc:
            lines = [f"The recording stopped: {type(exc).__name__}: {exc}"]
        for line in lines:
            print(line)
        show_result("Test Case Recorder", lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record a test case in a browser and send it to the QA "
                                                 "Automation Portal (and save it as an import workbook). "
                                                 "With no --url, a start window asks for everything.")
    parser.add_argument("--url", help="Application URL to open (http or https)")
    parser.add_argument("--name", help="Test case name, as it will appear in the portal")
    parser.add_argument("--description", default="", help="Test case description")
    parser.add_argument("--out", help="Output .xlsx path (default: output_folder/file_name from "
                                      "tools_config.ini plus _<date>_<time>.xlsx)")
    parser.add_argument("--config", default="", help="Settings file with output_folder and file_name "
                                                     "(default: tools_config.ini next to this script)")
    parser.add_argument("--browser", default="msedge", choices=["msedge", "chrome", "chromium"],
                        help="Installed browser to use (default msedge)")
    parser.add_argument("--auth-user", default="", metavar="USER",
                        help="User name for the browser's own sign-in prompt; the password is asked for")
    parser.add_argument("--auth-domain", default="", metavar="DOMAIN", help="Windows domain of --auth-user")
    parser.add_argument("--ignore-https-errors", action="store_true", help="Accept self-signed certificates")
    parser.add_argument("--keep-typed-username", action="store_true",
                        help="Keep the typed user name instead of {AUTH_USERNAME}")
    parser.add_argument("--absolute-urls", action="store_true",
                        help="Write full addresses in NAVIGATE steps instead of relative paths")
    parser.add_argument("--block", action="store_true",
                        help="Save it as a reusable block (is_reusable_block = yes)")
    parser.add_argument("--depends-on", default="", metavar="TEST_CASE",
                        help="Name of the test case that must pass first")
    parser.add_argument("--teardown", default="", metavar="BLOCK",
                        help="Name of the reusable block to run after this test case")
    parser.add_argument("--start-paused", action="store_true",
                        help="Record nothing until Resume is pressed on the toolbar (sign in first)")
    parser.add_argument("--portal", default="", metavar="URL",
                        help="Portal address: the test case is sent there when recording ends")
    parser.add_argument("--portal-user", default="", metavar="USER", help="Your portal user name")
    parser.add_argument("--app", default="", metavar="NAME", help="Application in the portal to add the test case to")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace the test case in the portal if the name already exists")
    parser.add_argument("--no-window", action="store_true",
                        help="Ask in the console instead of opening the start window")
    args = parser.parse_args(argv)

    if not args.url and not args.no_window:
        try:
            import tkinter  # noqa: F401  (missing on some minimal Python installs)
        except ImportError as exc:
            print(f"The start window cannot open: tkinter is not available ({exc}).")
            print(f"Python used: {sys.executable}")
            print("Install Python with the 'tcl/tk and IDLE' option, or use TestCaseRecorder.exe.")
            print("Asking in the console instead.\n")
        else:
            return run_window()

    url = args.url or input("Application URL: ").strip()
    if not url.lower().startswith(("http://", "https://")):
        parser.error("the URL must start with http:// or https://")
    name = (args.name or input("Test case name: ").strip() or "Recorded test").strip()

    client = app_id = None
    if args.portal:
        if not args.app:
            parser.error("--portal also needs --app, the application to add the test case to")
        client = PortalClient(args.portal, verify_ssl=not args.ignore_https_errors)
        user = args.portal_user or input("Portal user name: ").strip()
        password = os.environ.get("QA_PORTAL_PASSWORD") or getpass.getpass("Portal password: ")
        try:
            applications = client.login(user, password)
        except PortalError as exc:
            parser.error(str(exc))
        app_id = next((item["id"] for item in applications if item["name"].lower() == args.app.lower()), None)
        if app_id is None:
            parser.error(f"the portal has no application named '{args.app}'")
    auth_password = ""
    if args.auth_user:
        auth_password = os.environ.get("QA_AUTH_PASSWORD") or getpass.getpass(f"Password of {args.auth_user}: ")

    print(CONTROLS_HELP)
    code, lines = run_recording({
        "url": url, "name": name, "description": args.description, "out": args.out, "config": args.config,
        "browser": args.browser, "ignore_https": args.ignore_https_errors,
        "keep_username": args.keep_typed_username, "absolute_urls": args.absolute_urls, "block": args.block,
        "depends_on": args.depends_on, "teardown": args.teardown, "start_paused": args.start_paused,
        "auth_user": args.auth_user, "auth_domain": args.auth_domain, "auth_password": auth_password,
        "overwrite": args.overwrite,
    }, client, app_id)
    print()
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
