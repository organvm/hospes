export function clipboardText(preview, { completed, canDraft, reviewed }) {
  if (completed) throw new Error('Completed specimens expose no copy surface.');
  if (!canDraft) throw new Error('This role cannot copy a correspondence draft.');
  if (!reviewed) throw new Error('Human-reviewed draft metadata is required.');
  const value = String(preview || '');
  if (!value.trim()) throw new Error('There is no draft preview to copy.');
  return value;
}
