/* ==========================================================================
   Shared password-strength meter -- used by register.html and
   reset-password.html so both pages give the same feedback with one
   implementation. Client-side heuristic only (length/case/digit/symbol);
   the two checks that can't run client-side (common-password list,
   similarity to the user's own name/email/username) are still enforced
   authoritatively by the backend's AUTH_PASSWORD_VALIDATORS on submit --
   this meter is guidance, not the source of truth.
   ========================================================================== */

function passwordStrength(pw) {
  const checks = {
    length: pw.length >= 10,
    upper: /[A-Z]/.test(pw),
    lower: /[a-z]/.test(pw),
    digit: /[0-9]/.test(pw),
    symbol: /[^A-Za-z0-9]/.test(pw),
    notAllNumeric: pw.length === 0 || !/^[0-9]+$/.test(pw),
  };
  const passed = Object.values(checks).filter(Boolean).length;

  let label = '';
  let level = 0; // 0 = empty, 1 = weak, 2 = fair, 3 = strong, 4 = excellent
  if (pw.length > 0) {
    if (passed <= 2) { label = 'Weak'; level = 1; }
    else if (passed <= 3) { label = 'Fair'; level = 2; }
    else if (passed <= 5) { label = 'Strong'; level = 3; }
    else { label = 'Excellent'; level = 4; }
  }
  return { checks, label, level, passed };
}

const PW_LEVEL_COLOR = { 0: 'var(--color-border)', 1: 'var(--color-danger)', 2: 'var(--color-gold-secondary)', 3: 'var(--color-gold)', 4: 'var(--color-success)' };
const PW_REQUIREMENTS = [
  ['length', 'At least 10 characters'],
  ['upper', 'One uppercase letter'],
  ['lower', 'One lowercase letter'],
  ['digit', 'One number'],
  ['symbol', 'One symbol (e.g. ! @ # $)'],
];

/* Renders (or re-renders) the meter + checklist into `container`, an
   already-in-the-DOM element. Call again on every keystroke -- cheap
   innerHTML rebuild, no diffing needed for something this small. */
function renderPasswordStrengthMeter(container, password) {
  const { checks, label, level } = passwordStrength(password);
  const segments = [1, 2, 3, 4].map((i) => `<span style="flex:1; height:4px; border-radius:2px; background:${i <= level ? PW_LEVEL_COLOR[level] : 'var(--color-border)'}; transition:background 0.15s;"></span>`).join('');
  const checklist = PW_REQUIREMENTS.map(([key, text]) => {
    const ok = checks[key];
    return `<li style="display:flex; align-items:center; gap:6px; font-size:0.76rem; color:${ok ? 'var(--color-success)' : 'var(--color-text-muted)'};">
      <span style="width:14px; display:inline-block;">${ok ? '&#10003;' : '&#183;'}</span>${text}
    </li>`;
  }).join('');

  container.innerHTML = `
    <div style="display:flex; gap:4px; margin:6px 0 4px 0;">${segments}</div>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
      <span style="font-size:0.76rem; font-weight:600; color:${level ? PW_LEVEL_COLOR[level] : 'var(--color-text-muted)'};">${label || 'Password strength'}</span>
    </div>
    <ul style="list-style:none; margin:0; padding:0; display:grid; grid-template-columns:1fr 1fr; gap:2px 10px;">${checklist}</ul>
  `;
}
