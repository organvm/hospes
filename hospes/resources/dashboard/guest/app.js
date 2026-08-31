/* Guest portal client.
 *
 * The one-time token arrives in the URL fragment, which browsers never place in
 * a Referer header and servers never see in a request line or an access log.
 * It is read once, exchanged for a short server-side session, and then erased
 * from the address bar. The session lives in this closure only — never in any
 * browser-durable store, which would outlive the tab and turn a deliberately
 * ephemeral credential into a lasting one.
 */
(() => {
  const status = document.getElementById('status');
  const form = document.getElementById('intake');
  const clauseList = document.getElementById('consent-clauses');
  const dateChoices = document.getElementById('date-choices');
  const dateHint = document.getElementById('dates-hint');
  const consentAccept = document.getElementById('consent-accept');
  const submitButton = document.getElementById('submit');
  const done = document.getElementById('done');
  const receiptList = document.getElementById('receipts');

  const RANKS = ['First choice', 'Second choice', 'Third choice'];
  const LIST_FIELDS = ['dietary_restrictions', 'allergies', 'accessibility_needs', 'topics_off_limits'];
  const TEXT_FIELDS = ['name_pronunciation', 'pronouns', 'preferred_beverage', 'notes'];

  let session = null;
  let consentVersion = null;

  function setStatus(message, isError) {
    status.textContent = message;
    status.classList.toggle('error', Boolean(isError));
  }

  function value(id) {
    const element = document.getElementById(id);
    return element ? element.value.trim() : '';
  }

  function lines(id) {
    return value(id)
      .split('\n')
      .map(entry => entry.trim())
      .filter(entry => entry.length > 0);
  }

  async function post(path, body) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.detail || 'The portal could not process that request.');
    }
    return payload;
  }

  function renderClauses(clauses) {
    clauseList.replaceChildren();
    clauses.forEach(clause => {
      const item = document.createElement('li');
      item.textContent = clause.text;
      clauseList.appendChild(item);
    });
  }

  function renderDates(offered) {
    dateChoices.replaceChildren();
    dateHint.textContent = offered.length
      ? 'Pick three different dates from the ones the production has open.'
      : 'The production has not published its open dates yet — tell us the three that suit you.';
    RANKS.forEach((title, index) => {
      const id = `date-${index + 1}`;
      const label = document.createElement('label');
      label.setAttribute('for', id);
      label.textContent = title;
      dateChoices.appendChild(label);
      let field;
      if (offered.length) {
        field = document.createElement('select');
        const blank = document.createElement('option');
        blank.value = '';
        blank.textContent = 'Select a date';
        field.appendChild(blank);
        offered.forEach(date => {
          const option = document.createElement('option');
          option.value = date;
          option.textContent = date;
          field.appendChild(option);
        });
      } else {
        field = document.createElement('input');
        field.type = 'date';
      }
      field.id = id;
      field.name = id;
      dateChoices.appendChild(field);
    });
  }

  function preferredDates() {
    return RANKS.map((title, index) => value(`date-${index + 1}`)).filter(entry => entry.length > 0);
  }

  function careProfile() {
    const profile = {};
    TEXT_FIELDS.forEach(field => {
      const entry = value(field.replace(/_/g, '-'));
      if (entry) profile[field] = entry;
    });
    LIST_FIELDS.forEach(field => {
      const entries = lines(field.replace(/_/g, '-'));
      if (entries.length) profile[field] = entries;
    });
    return profile;
  }

  async function start() {
    const token = new URLSearchParams(window.location.hash.slice(1)).get('token');  // allow-secret: the one-time token is read from the fragment, never stored
    history.replaceState(null, '', window.location.pathname + window.location.search);
    if (!token) {
      setStatus('This one-time link is missing or invalid. Ask the producer who invited you to send a fresh one.', true);
      return;
    }
    let opened;
    try {
      opened = await post('/guest/session', { token });
    } catch (error) {
      setStatus(error.message, true);
      return;
    }
    session = opened.session;
    consentVersion = opened.consent_version;
    renderClauses(opened.consent_clauses || []);
    renderDates(opened.offered_dates || []);
    form.hidden = false;
    setStatus(`Your link is open until ${opened.expires_at}. Nothing is saved until you submit.`);
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!consentAccept.checked) {
      setStatus('Tick the consent box before submitting — nothing is recorded without it.', true);
      return;
    }
    const dates = preferredDates();
    if (dates.length !== RANKS.length) {
      setStatus('Choose all three dates before submitting.', true);
      return;
    }
    submitButton.disabled = true;
    setStatus('Submitting securely…');
    try {
      const result = await post('/guest/intake', {
        session,
        care_profile: careProfile(),
        consent: { accepted: true, version: consentVersion },
        preferred_dates: dates,
      });
      form.remove();
      receiptList.replaceChildren();
      (result.receipts || []).forEach(receipt => {
        const item = document.createElement('li');
        item.textContent = receipt;
        receiptList.appendChild(item);
      });
      done.hidden = false;
      session = null;
      setStatus('Intake received. Thank you — the production has everything it needs.');
    } catch (error) {
      submitButton.disabled = false;
      setStatus(error.message, true);
    }
  });

  start();
})();
