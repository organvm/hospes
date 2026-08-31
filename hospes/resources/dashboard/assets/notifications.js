import {
  completeQueueTask,
  loadMyQueue,
  loadNotificationSummary,
  loadNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from './api.js';
import { showView } from './views.mjs';

const $ = selector => document.querySelector(selector);

const TYPE_LABELS = Object.freeze({
  draft_ready: 'Draft ready',
  brief_ready: 'Brief ready',
  clips_needed: 'Clips needed',
  booking_confirmed: 'Booking confirmed',
  assignment_assigned: 'Assigned',
});

let started = false;

export function escapeHTML(value) {
  return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
}

export function activeShow(doc = document) {
  return doc.querySelector('meta[name="hospes-active-show"]')?.content || '';
}

export function badgeText(summary) {
  const unread = Number(summary?.unread || 0);
  if (!Number.isFinite(unread) || unread <= 0) return '0';
  return unread > 99 ? '99+' : String(unread);
}

export function typeLabel(type) {
  return TYPE_LABELS[type] || String(type ?? '').replaceAll('_', ' ');
}

export function formatDue(value) {
  if (!value) return 'no due date';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'no due date' : parsed.toLocaleString();
}

function setStatus(selector, message, error = false) {
  const element = $(selector);
  if (!element) return;
  element.textContent = message;
  element.classList.toggle('error', error);
}

function renderBadge(summary) {
  const badge = $('#notification-badge');
  if (!badge) return;
  badge.textContent = badgeText(summary);
  badge.classList.toggle('unread', Number(summary?.unread || 0) > 0);
  badge.classList.toggle('critical', Number(summary?.critical_unread || 0) > 0);
  const bell = $('#btn-notifications');
  if (bell) {
    bell.setAttribute(
      'aria-label',
      `Notifications: ${Number(summary?.unread || 0)} unread, `
      + `${Number(summary?.open_assignments || 0)} open tasks`,
    );
  }
}

function notificationItem(item) {
  const unread = !item.read_at;
  return `<li class="notification ${unread ? 'unread' : 'read'}${item.severity === 'critical' ? ' critical' : ''}">`
    + `<div class="notification-head"><b>${escapeHTML(item.title)}</b>`
    + `<span class="pill">${escapeHTML(typeLabel(item.notification_type))}</span></div>`
    + `<div class="notification-meta"><code>${escapeHTML(item.entity_ref)}</code>`
    + `<span>Due ${escapeHTML(formatDue(item.due_at))}</span></div>`
    + (unread
      ? `<button class="quiet act-read" type="button" data-notification="${escapeHTML(item.id)}">Mark read</button>`
      : '<span class="notification-read-flag">Read</span>')
    + '</li>';
}

function queueItem(item) {
  return `<li class="queue-task${item.overdue ? ' overdue' : ''}">`
    + `<div class="notification-head"><b>${escapeHTML(item.title)}</b>`
    + `<span class="pill">${escapeHTML(typeLabel(item.assignment_type))}</span></div>`
    + `<div class="notification-meta"><code>${escapeHTML(item.entity_ref)}</code>`
    + `<span>Due ${escapeHTML(formatDue(item.due_at))}</span>`
    + `<span>${escapeHTML(item.status)}</span></div>`
    + (item.status === 'done'
      ? '<span class="notification-read-flag">Completed</span>'
      : `<button class="quiet act-complete" type="button" data-task="${escapeHTML(item.id)}">Mark done</button>`)
    + '</li>';
}

export async function refreshBadge() {
  const show = activeShow();
  if (!show) return null;
  try {
    const summary = await loadNotificationSummary(show);
    renderBadge(summary);
    return summary;
  } catch (error) {
    setStatus('#notification-status', error.message, true);
    return null;
  }
}

export async function refreshNotificationCenter() {
  const show = activeShow();
  if (!show) return;
  const filter = $('#notification-filter')?.value || '';
  const unreadOnly = Boolean($('#notification-unread-only')?.checked);
  setStatus('#notification-status', 'Loading notifications…');
  try {
    const items = await loadNotifications(show, {
      notification_type: filter,
      unread_only: unreadOnly ? 'true' : '',
    });
    const list = $('#notification-list');
    if (list) {
      list.innerHTML = items.length
        ? items.map(notificationItem).join('')
        : '<li class="empty">No notifications for your role.</li>';
    }
    setStatus('#notification-status', `${items.length} notification(s) for your role.`);
    await refreshBadge();
  } catch (error) {
    setStatus('#notification-status', error.message, true);
  }
}

export async function refreshQueue() {
  const show = activeShow();
  if (!show) return;
  setStatus('#queue-status', 'Loading your queue…');
  try {
    const queue = await loadMyQueue(show, { include_done: Boolean($('#queue-include-done')?.checked) });
    const list = $('#queue-list');
    if (list) {
      list.innerHTML = queue.assignments.length
        ? queue.assignments.map(queueItem).join('')
        : '<li class="empty">No work is assigned to your role.</li>';
    }
    const role = $('#queue-role');
    if (role) role.textContent = String(queue.role).replaceAll('_', ' ');
    renderBadge(queue.summary);
    setStatus(
      '#queue-status',
      `${queue.summary.open_assignments} open · ${queue.summary.overdue_assignments} overdue · `
      + `${queue.summary.unread} unread notification(s).`,
    );
  } catch (error) {
    setStatus('#queue-status', error.message, true);
  }
}

export async function activateQueueView() {
  showView('queue');
  await refreshQueue();
}

function toggleCenter(open) {
  const panel = $('#notification-center');
  const bell = $('#btn-notifications');
  if (!panel || !bell) return false;
  const next = open === undefined ? panel.classList.contains('hidden') : open;
  panel.classList.toggle('hidden', !next);
  bell.setAttribute('aria-expanded', String(next));
  return next;
}

export function startNotifications() {
  if (started || !$('#btn-notifications')) return;
  started = true;
  $('#btn-notifications').addEventListener('click', async () => {
    if (toggleCenter()) await refreshNotificationCenter();
  });
  $('#btn-notification-close')?.addEventListener('click', () => toggleCenter(false));
  $('#btn-notification-refresh')?.addEventListener('click', () => void refreshNotificationCenter());
  $('#notification-filter')?.addEventListener('change', () => void refreshNotificationCenter());
  $('#notification-unread-only')?.addEventListener('change', () => void refreshNotificationCenter());
  $('#btn-notification-read-all')?.addEventListener('click', async () => {
    try {
      await markAllNotificationsRead(activeShow(), $('#notification-filter')?.value || '');
      await refreshNotificationCenter();
    } catch (error) {
      setStatus('#notification-status', error.message, true);
    }
  });
  $('#notification-list')?.addEventListener('click', async event => {
    const button = event.target.closest('.act-read');
    if (!button) return;
    try {
      await markNotificationRead(activeShow(), button.dataset.notification);
      await refreshNotificationCenter();
    } catch (error) {
      setStatus('#notification-status', error.message, true);
    }
  });
  $('#btn-queue-refresh')?.addEventListener('click', () => void refreshQueue());
  $('#queue-include-done')?.addEventListener('change', () => void refreshQueue());
  $('#queue-list')?.addEventListener('click', async event => {
    const button = event.target.closest('.act-complete');
    if (!button) return;
    try {
      await completeQueueTask(activeShow(), button.dataset.task);
      await refreshQueue();
    } catch (error) {
      setStatus('#queue-status', error.message, true);
    }
  });
  void refreshBadge();
}
