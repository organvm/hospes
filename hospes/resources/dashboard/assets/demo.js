export function parseCSV(text) {
  const rows = [];
  let field = '';
  let row = [];
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else if (char !== '\r') {
      field += char;
    }
  }
  row.push(field);
  if (row.some(value => value.length)) rows.push(row);
  return rows;
}

export function csvToCandidates(text) {
  const rows = parseCSV(text);
  if (rows.length < 2) return [];
  const headers = rows[0].map(header => header.trim().toLowerCase());
  return rows.slice(1).map((row, rowIndex) => {
    const candidate = { id: `demo-${rowIndex + 1}` };
    headers.forEach((header, index) => { candidate[header] = (row[index] || '').trim(); });
    return candidate;
  }).filter(candidate => candidate.guest_name && candidate.guest_name !== '[CANDIDATE A]');
}

export function isProtectedCandidate(candidate) {
  return ['C4', 'C5'].includes(String(candidate?.relationship_class || '').trim().toUpperCase());
}

export function applyDemoDecision(candidate, action, note, current = {}) {
  const saved = {
    decision: current.decision || 'pending',
    note: current.note || '',
  };
  if (!['approve', 'reject', 'protect', 'note'].includes(action)) {
    return { saved, refused: true, reason: 'invalid-action' };
  }
  const protectedNow = isProtectedCandidate(candidate) || saved.decision === 'protect';
  if (protectedNow && !['protect', 'note'].includes(action)) {
    saved.decision = 'protect';
    saved.note = note;
    return { saved, refused: true, reason: 'protected' };
  }
  if (action !== 'note') saved.decision = action;
  saved.note = note;
  return { saved, refused: false, reason: null };
}

export function exportDemoDecisions(candidates, decisions) {
  const payload = candidates.map(candidate => {
    const saved = decisions.get(candidate.id) || {};
    return {
      guest_name: candidate.guest_name,
      decision: saved.decision || 'pending',
      note: saved.note || '',
    };
  });
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'decisions.json';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
