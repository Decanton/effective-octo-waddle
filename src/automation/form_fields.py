"""Finds the questions on an application form and fills them.

Works by reading labels the way a person would, so the same code handles
Greenhouse, Lever, Ashby and most plain HTML forms. It never clicks a submit
button: that lives only in form_submitter.py and runs after your approval.
"""
from __future__ import annotations

import re

from ..intelligence.answer_engine import Question

DISCOVER_JS = r"""
() => {
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
  const out = []; const groups = {}; let n = 0;
  const root = document.querySelector('form') || document;
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const t = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(t) || el.disabled || !vis(el)) continue;
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
    out.push({ id, kind, label: labelOf(el), options, required: req, name: el.name || '' });
  }
  return out;
}
"""

TEXT_KINDS = {"text", "email", "tel", "url", "number", "textarea", "combobox", "date"}


def discover(page) -> list[dict]:
    fields = page.evaluate(DISCOVER_JS)
    for f in fields:
        f["required"] = bool(f["required"] or "*" in f["label"])
        f["label"] = re.sub(r"\s*\*\s*$", "", f["label"]).strip()
    return fields


def to_question(f: dict) -> Question:
    kind = {"checkboxes": "checkbox"}.get(f["kind"], f["kind"])
    if kind == "combobox":
        kind = "text"
    return Question(label=f["label"], kind=kind, options=f.get("options", []), required=f["required"])


def fill_field(page, f: dict, value) -> bool:
    """Put one answer into one field. Returns True if it worked."""
    if value is None or value == "":
        return False
    fid, kind = f["id"], f["kind"]
    try:
        if kind in ("radio", "checkboxes"):
            wanted = value if isinstance(value, list) else [value]
            ok = False
            for i, opt in enumerate(f["options"]):
                if any(str(w).strip().lower() == opt.strip().lower() for w in wanted):
                    page.locator(f'[data-jsa="{fid}:{i}"]').check(force=True)
                    ok = True
            return ok
        loc = page.locator(f'[data-jsa="{fid}"]')
        if kind == "file":
            loc.set_input_files(str(value))
        elif kind == "checkbox":
            if value is True:
                loc.check(force=True)
            else:
                return False
        elif kind == "select":
            loc.select_option(label=str(value))
        elif kind == "combobox":
            loc.click()
            loc.fill(str(value))
            page.wait_for_timeout(600)
            page.keyboard.press("Enter")
        else:
            loc.fill(str(value))
        return True
    except Exception:
        return False


VERIFY_JS = r"""
(consentLabels) => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const problems = [];
  const skip = new Set(consentLabels || []);
  const root = document.querySelector('form') || document;
  const seenGroups = new Set();
  for (const el of root.querySelectorAll('input, textarea, select')) {
    const t = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(t) || el.disabled) continue;
    if (t !== 'file' && !vis(el)) continue;
    const req = el.required || el.getAttribute('aria-required') === 'true';
    const tag = el.getAttribute('data-jsa') || el.name || el.id || t;
    if (skip.has(tag.split(':')[0])) continue;
    if (t === 'file') {
      const isCv = /resume|\bcv\b|curriculum/i.test(el.name + ' ' + el.id + ' ' + (el.getAttribute('aria-label') || ''))
                   || (el.closest('div,fieldset,label') && /resume|\bcv\b|curriculum/i.test(el.closest('div,fieldset,label').innerText || ''));
      const f = el.files && el.files[0];
      if ((req || isCv) && !f) problems.push('no file attached: ' + tag);
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
    if (!clean(el.value)) problems.push('empty: ' + tag);
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


def verify(page, consent_ids: tuple[str, ...] = ()) -> list[str]:
    """Problems that would stop a real submission: empty required fields, no CV attached,
    or visible validation errors. An empty list means the form is verified ready.
    consent_ids: data-jsa ids of consent boxes deliberately left for you (not counted)."""
    try:
        problems = page.evaluate(VERIFY_JS, list(consent_ids))
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
