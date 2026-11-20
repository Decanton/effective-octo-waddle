"""Finds the questions on an application form and fills them.

Works by reading labels the way a person would, so the same code handles
Greenhouse, Lever, Ashby and most plain HTML forms. It never clicks a submit
button: that lives only in form_submitter.py and runs after your approval.
"""
from __future__ import annotations

import re

from ..intelligence.answer_engine import Question

DISCOVER_JS = r"""
(rootSel) => {
  const pickRoot = (sel) => {
    if (sel) { const r = document.querySelector(sel); if (r) return r; }
    // The application form is the one with the most fields (not a search or newsletter form).
    const forms = [...document.querySelectorAll('form')];
    const count = f => f.querySelectorAll('input:not([type=hidden]), textarea, select').length;
    const best = forms.sort((a, b) => count(b) - count(a))[0];
    return best && count(best) >= 2 ? best : document;
  };
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') || el.type === 'file'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(' ').map(i => document.getElementById(i)).filter(Boolean).map(n => n.innerText).join(' '); if (clean(t)) return clean(t); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const wrap = el.closest('label'); if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const l = p.querySelector(':scope > label, :scope > legend, :scope > .application-label, :scope > div > label');
      if (l && clean(l.innerText)) return clean(l.innerText);
    }
    return clean(el.placeholder || el.name || '');
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg && clean(lg.innerText)) return clean(lg.innerText); }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      const n = p.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length;
      const lab = p.querySelector(':scope > label, :scope > legend, :scope > .application-label, :scope > div:first-child');
      if (n > 1 && lab && !lab.contains(el) && clean(lab.innerText)) return clean(lab.innerText);
    }
    return clean(el.name);
  };
  // A checkbox drawn as a toggle hides the real input; its label is what you see.
  const labelVisible = el => { const l = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el.closest('label');
    if (!l) return false; const r = l.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const out = []; const groups = {}; let n = 0;
  const root = pickRoot(rootSel);
  // Yes/No questions drawn as two buttons (Ashby and others), often over a hidden checkbox.
  const btnFields = new Set();
  for (const b of root.querySelectorAll('button')) {
    if (!/^\s*yes\s*$/i.test(b.innerText || '')) continue;
    const grp = b.parentElement; if (!grp || grp.dataset.jsaGroup) continue;
    const btns = [...grp.querySelectorAll(':scope > button, :scope > * > button')].filter(x => (x.innerText || '').trim().length <= 40);
    if (btns.length < 2 || btns.length > 4 || !btns.some(x => /^\s*no\s*$/i.test(x.innerText || ''))) continue;
    let field = grp.parentElement, lab = null;
    for (let i = 0; i < 5 && field; i++, field = field.parentElement) {
      lab = field.querySelector(':scope > label, :scope > legend, :scope > div > label, :scope > [class*="label" i]');
      if (lab && !lab.contains(grp) && clean(lab.innerText)) break;
      lab = null;
    }
    if (!lab) continue;
    const id = 'b' + (n++); grp.dataset.jsaGroup = id;
    btns.forEach((x, i) => x.setAttribute('data-jsa', id + ':' + i));
    const hidden = field.querySelector('input[type=checkbox], input[type=hidden]');
    const req = /\*\s*$/.test(clean(lab.innerText)) || !!field.querySelector('[required], [aria-required="true"]');
    if (field) btnFields.add(field);
    out.push({ id, kind: 'buttons', label: clean(lab.innerText), options: btns.map(x => clean(x.innerText)), required: req, name: hidden ? hidden.name || '' : '' });
  }
  const inBtnField = el => [...btnFields].some(f => f.contains(el));
  for (const el of root.querySelectorAll('input, textarea, select')) {
    if ((el.type === 'checkbox' || el.type === 'hidden') && inBtnField(el)) { el.setAttribute('data-jsa-skip', '1'); continue; }
    const t = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(t) || el.disabled) continue;
    if (t !== 'file' && (el.getAttribute('aria-hidden') === 'true' || /requiredInput/i.test(el.className || ''))) continue;
    if (!vis(el) && !((t === 'checkbox' || t === 'radio') && labelVisible(el))) continue;
    const req = el.required || el.getAttribute('aria-required') === 'true';
    if (t === 'radio' || (t === 'checkbox' && el.name && root.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1)) {
      const key = t + ':' + el.name;
      if (!groups[key]) { groups[key] = { id: 'g' + (n++), kind: t === 'radio' ? 'radio' : 'checkboxes', label: groupLabel(el), options: [], required: false, name: el.name }; out.push(groups[key]); }
      el.setAttribute('data-jsa', groups[key].id + ':' + groups[key].options.length);
      groups[key].options.push(labelOf(el) || el.value);
      groups[key].required = groups[key].required || req;
      continue;
    }
    const id = 'f' + (n++); el.setAttribute('data-jsa', id);
    const kind = el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' :
                 (el.getAttribute('role') === 'combobox' ? 'combobox' : t);
    const options = el.tagName === 'SELECT' ? [...el.options].filter(o => o.value !== '').map(o => clean(o.text)) : [];
    out.push({ id, kind, label: labelOf(el), options, required: req, name: el.name || '', elid: el.id || '' });
  }
  return out;
}
"""

TEXT_KINDS = {"text", "email", "tel", "url", "number", "textarea", "combobox", "date"}


def discover(page, root: str | None = None, read_options: bool = True) -> list[dict]:
    """Fields of the application form (or of `root`, a CSS selector such as a modal)."""
    fields = page.evaluate(DISCOVER_JS, root)
    for f in fields:
        f["required"] = bool(f["required"] or "*" in f["label"])
        f["label"] = re.sub(r"\s*\*\s*$", "", f["label"]).strip()
        if f["kind"] == "file" and not re.search(r"resume|\bcv\b|curriculum|cover", f["label"], re.I):
            hint = f"{f.get('elid', '')} {f.get('name', '')}"
            if re.search(r"resume|\bcv\b|curriculum", hint, re.I):  # e.g. Greenhouse's hidden #resume behind an "Attach" button
                f["label"] = "Resume/CV"
            elif re.search(r"cover", hint, re.I):
                f["label"] = "Cover letter"
        if read_options and f["kind"] == "combobox" and not f["options"]:
            f["options"] = combo_options(page, page.locator(f'[data-jsa="{f["id"]}"]'))
    return fields


OPTION_SEL = "[role=option], [class*='option' i][id*='option' i]"


def _listed(page, loc) -> list:
    """The visible choices of an open dropdown (react-select, Greenhouse, Workable, ...)."""
    try:
        lb = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
    except Exception:
        lb = None
    scope = page.locator(f"#{lb}") if lb and re.fullmatch(r"[A-Za-z][\w\-:.]*", lb) and page.locator(f"#{lb}").count() else page
    try:
        return [o for o in scope.locator(OPTION_SEL).all()[:300] if o.is_visible()]
    except Exception:
        return []


def combo_options(page, loc) -> list[str]:
    """Open a searchable dropdown, read its choices, close it again. [] if it only searches as you type."""
    try:
        loc.click(timeout=3000)
        page.wait_for_timeout(500)
        opts = [" ".join((o.inner_text() or "").split()) for o in _listed(page, loc)]
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        return [o for o in opts if o and not re.fullmatch(r"(no options|loading\.*|select\.*)", o, re.I)][:200]
    except Exception:
        return []


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+ ]", " ", (s or "").lower()).split())


def _best_option(texts: list[str], wanted: str) -> int | None:
    w = _norm(wanted)
    if not w:
        return None
    normed = [_norm(t) for t in texts]
    for test in (lambda t: t == w, lambda t: t.startswith(w + " ") or t.startswith(w),
                 lambda t: w in t, lambda t: t and t in w):
        for i, t in enumerate(normed):
            if test(t):
                return i
    return None


def _fill_combobox(page, loc, value) -> bool:
    text = "Yes" if value is True else "No" if value is False else str(value)
    loc.click(timeout=3000)
    page.wait_for_timeout(300)
    opts = _listed(page, loc)
    i = _best_option([o.inner_text() for o in opts], text)
    # Long or search-as-you-type lists: type, then pick from what's shown. "Nairobi, Kenya" is retried as "Nairobi".
    tries = [text[:40]] + ([text.split(",")[0].strip()] if "," in text else [])
    for typed in tries:
        if i is not None:
            break
        loc.fill(typed)
        page.wait_for_timeout(1500)
        opts = _listed(page, loc)
        texts = [o.inner_text() for o in opts]
        i = _best_option(texts, text)
        if i is None and typed != text[:40]:
            i = _best_option(texts, typed)
        if i is None and len(opts) == 1 and len(text) <= 40:
            i = 0  # the only match for what was typed (e.g. a city search)
    if i is None:
        page.keyboard.press("Escape")
        return False
    opts[i].click(timeout=3000)
    page.wait_for_timeout(300)
    return True


def to_question(f: dict) -> Question:
    kind = {"checkboxes": "checkbox", "buttons": "radio"}.get(f["kind"], f["kind"])
    if kind == "combobox":
        kind = "select" if f.get("options") else "text"
    return Question(label=f["label"], kind=kind, options=f.get("options", []), required=f["required"])


def fill_field(page, f: dict, value) -> bool:
    """Put one answer into one field. Returns True if it worked."""
    if value is None or value == "":
        return False
    fid, kind = f["id"], f["kind"]
    try:
        if kind == "buttons":  # Yes/No drawn as buttons: click the one wanted, as a person would
            text = "Yes" if value is True else "No" if value is False else str(value)
            for i, opt in enumerate(f["options"]):
                if opt.strip().lower() == text.strip().lower():
                    b = page.locator(f'[data-jsa="{fid}:{i}"]')
                    b.click(timeout=3000)
                    b.evaluate("e => e.parentElement.setAttribute('data-jsa-chosen', e.innerText.trim())")
                    return True
            return False
        if kind in ("radio", "checkboxes"):
            wanted = value if isinstance(value, list) else [value]
            ok = False
            for i, opt in enumerate(f["options"]):
                if any(str(w).strip().lower() == opt.strip().lower() for w in wanted):
                    el = page.locator(f'[data-jsa="{fid}:{i}"]')
                    try:
                        el.check(force=True, timeout=3000)
                    except Exception:
                        el.evaluate("e => { if (!e.checked) e.click(); }")
                    ok = True
            return ok
        loc = page.locator(f'[data-jsa="{fid}"]')
        if kind == "file":
            loc.set_input_files(str(value))
        elif kind == "checkbox":
            if value is not True:
                return False
            try:
                loc.check(force=True, timeout=3000)
            except Exception:  # a hidden input behind a toggle: click it the way the page's own label would
                loc.evaluate("e => { if (!e.checked) e.click(); }")
            return bool(loc.evaluate("e => e.checked"))
        elif kind == "select":
            loc.select_option(label=str(value))
        elif kind == "combobox":
            return _fill_combobox(page, loc, value)
        else:
            loc.fill(str(value))
        return True
    except Exception:
        return False


VERIFY_JS = r"""
([consentLabels, rootSel]) => {
  const pickRoot = (sel) => {
    if (sel) { const r = document.querySelector(sel); if (r) return r; }
    // The application form is the one with the most fields (not a search or newsletter form).
    const forms = [...document.querySelectorAll('form')];
    const count = f => f.querySelectorAll('input:not([type=hidden]), textarea, select').length;
    const best = forms.sort((a, b) => count(b) - count(a))[0];
    return best && count(best) >= 2 ? best : document;
  };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const problems = [];
  const skip = new Set(consentLabels || []);
  const root = pickRoot(rootSel);
  for (const g of root.querySelectorAll('[data-jsa-group]')) {  // Yes/No button questions
    if (skip.has(g.dataset.jsaGroup)) continue;
    let field = g.parentElement, req = false, lab = '';
    for (let i = 0; i < 5 && field; i++, field = field.parentElement) {
      const l = field.querySelector(':scope > label, :scope > legend, :scope > div > label, :scope > [class*="label" i]');
      if (l && !l.contains(g)) { lab = clean(l.innerText); req = /\*\s*$/.test(lab) || !!field.querySelector('[required], [aria-required="true"]'); break; }
    }
    const picked = g.getAttribute('data-jsa-chosen') || [...g.querySelectorAll('button')].some(b =>
      b.getAttribute('aria-pressed') === 'true' || b.getAttribute('aria-checked') === 'true' || /(^|[\s_-])(active|selected|checked|is-selected)/i.test(b.className || ''));
    if (req && !picked) problems.push('unanswered: ' + (lab || g.dataset.jsaGroup).slice(0, 80));
  }
  const seenGroups = new Set();
  // Ashby and others add a second "autofill from resume" upload; once the CV is in one, the other isn't missing.
  const cvAttached = [...root.querySelectorAll('input[type=file]')].some(x => x.files && x.files.length &&
      /\.(docx|pdf|doc)$/i.test(x.files[0].name));
  const chosen = el => { if (clean(el.value)) return true; let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      const v = p.querySelector('[class*="singleValue" i], [class*="single-value" i], [class*="multiValue" i], [class*="multi-value" i], [class*="selected-value" i]');
      if (v && clean(v.innerText)) return true; }
    return false; };
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const t = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(t) || el.disabled) continue;
    if (t !== 'file' && (el.getAttribute('aria-hidden') === 'true' || /requiredInput/i.test(el.className || ''))) continue;
    if (t !== 'file' && !vis(el) && !((t === 'checkbox' || t === 'radio') && el.getAttribute('data-jsa'))) continue;
    const req = el.required || el.getAttribute('aria-required') === 'true';
    if (el.hasAttribute('data-jsa-skip')) continue;
    const tag = el.getAttribute('data-jsa') || el.name || el.id || t;
    if (skip.has(tag.split(':')[0])) continue;
    if (t === 'file') {
      const isCv = /resume|\bcv\b|curriculum/i.test(el.name + ' ' + el.id + ' ' + (el.getAttribute('aria-label') || ''))
                   || (el.closest('div,fieldset,label') && /resume|\bcv\b|curriculum/i.test(el.closest('div,fieldset,label').innerText || ''));
      let f = el.files && el.files[0];
      if (!f) {  // Greenhouse and others upload at once and empty the input, then show the file's name next to it
        let p = el.parentElement;
        for (let i = 0; i < 4 && p && !f; i++, p = p.parentElement)
          if (/[\w-]+\.(docx|pdf|doc)\b/i.test(p.innerText || '')) f = { name: (p.innerText.match(/[\w-]+\.(docx|pdf|doc)\b/i) || [''])[0] };
      }
      if ((req || (isCv && !cvAttached)) && !f) problems.push('no file attached: ' + tag);
      if (f && isCv && !/\.(docx|pdf|doc)$/i.test(f.name)) problems.push('CV is not a .docx/.pdf: ' + f.name);
      continue;
    }
    if (!req) continue;
    if (t === 'radio' || t === 'checkbox') {
      const key = t + ':' + el.name;
      if (el.name && seenGroups.has(key)) continue;
      seenGroups.add(key);
      const group = el.name ? root.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`) : [el];
      if (![...group].some(x => x.checked)) problems.push('unanswered: ' + tag);
      continue;
    }
    if (el.getAttribute('role') === 'combobox' ? !chosen(el) : !clean(el.value)) problems.push('empty: ' + tag);
  }
  const errSel = '[aria-invalid="true"], .error, .errors, .field-error, .invalid-feedback, .error-message, .form-error, [role="alert"], [class*="error" i]';
  for (const el of document.querySelectorAll(errSel)) {
    if (!vis(el)) continue;
    const txt = clean(el.innerText || el.getAttribute('aria-label') || '');
    if (el.matches('[aria-invalid="true"]') || (txt && txt.length < 300 && /required|invalid|please|must|error|can.?t be blank|missing/i.test(txt)))
      problems.push('error shown: ' + (txt || el.getAttribute('data-jsa') || el.name || 'field').slice(0, 120));
  }
  return problems;
}
"""


def verify(page, consent_ids: tuple[str, ...] = (), root: str | None = None) -> list[str]:
    """Problems that would stop a real submission: empty required fields, no CV attached,
    or visible validation errors. An empty list means the form is verified ready.
    consent_ids: data-jsa ids of consent boxes deliberately left for you (not counted)."""
    try:
        problems = page.evaluate(VERIFY_JS, [list(consent_ids), root])
    except Exception as exc:
        return [f"could not check the form: {exc}"]
    return problems


SUBMIT_TEXT = re.compile(r"^\s*(submit|send|apply)( application| my application| now)?\s*$", re.I)
CAPTCHA = "iframe[src*='hcaptcha'], iframe[src*='recaptcha'], iframe[src*='turnstile'], .g-recaptcha, .h-captcha"


def has_captcha(page) -> bool:
    return page.locator(CAPTCHA).count() > 0


def find_submit(page):
    for sel in ("button[type=submit]", "input[type=submit]", "button"):
        for el in page.locator(sel).all():
            try:
                txt = el.inner_text() if sel != "input[type=submit]" else (el.get_attribute("value") or "")
            except Exception:
                continue
            if SUBMIT_TEXT.match(txt or "") and el.is_visible():
                return el
    return None
