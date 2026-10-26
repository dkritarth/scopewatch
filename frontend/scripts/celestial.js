const pages = new Set(['home', 'activity', 'requests', 'test', 'badges']);
const visited = new Set();
function showView(view, moveFocus = false) {
  if (!pages.has(view)) view = 'home';
  document.body.dataset.view = view;
  document.querySelectorAll('[data-page]').forEach(page => { page.hidden = page.dataset.page !== view; });
  document.querySelectorAll('.rail-nav [data-view-target]').forEach(button => {
    button.setAttribute('aria-current', button.dataset.viewTarget === view ? 'page' : 'false');
  });
  if (view !== 'home' && view !== 'badges') visited.add(view);
  updateMilestones();
  if (moveFocus) {
    const heading = document.querySelector(`[data-page="${view}"] h1`);
    heading?.setAttribute('tabindex', '-1');
    heading?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: 'instant' });
  }
}
function updateMilestones() {
  document.querySelectorAll('[data-milestone]').forEach(node => node.classList.toggle('is-lit', visited.has(node.dataset.milestone)));
  document.getElementById('review-progress').textContent = `${visited.size} review milestone${visited.size === 1 ? '' : 's'}`;
}
document.addEventListener('click', event => {
  const target = event.target.closest('[data-view-target]');
  if (target) {
    const view = target.dataset.viewTarget;
    history.replaceState(null, '', `${location.pathname}${location.search}#${view}`);
    showView(view, true);
  }
  const theme = event.target.closest('[data-theme-target]');
  if (theme) {
    document.body.dataset.theme = theme.dataset.themeTarget;
    document.querySelectorAll('[data-theme-target]').forEach(button => button.setAttribute('aria-pressed', String(button === theme)));
  }
  const preset = event.target.closest('[data-preset]');
  if (preset) {
    const select = document.getElementById('action-preset');
    select.value = preset.dataset.preset;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    document.querySelectorAll('[data-preset]').forEach(button => button.setAttribute('aria-pressed', String(button === preset)));
    document.getElementById('simulator-panel').scrollIntoView({ behavior: 'instant', block: 'start' });
  }
  if (event.target.closest('[data-event-id]')) { visited.add('evidence'); updateMilestones(); }
  if (event.target.closest('.approval-card-link')) showView('activity', true);
});
document.addEventListener('scopewatch:summary', event => {
  const { runName, executed, denied, pending, live } = event.detail;
  for (const [id, value] of Object.entries({ 'home-executed': executed, 'home-denied': denied, 'home-pending': pending, 'nav-request-count': pending })) {
    document.getElementById(id).textContent = String(value);
  }
  document.getElementById('nav-request-count').hidden = pending === 0;
  document.getElementById('home-run-name').textContent = runName || 'Select a run to begin your review.';
  document.getElementById('journey-pending').textContent = `${pending} pending in the selected run`;
  document.getElementById('run-mode-label').textContent = live ? 'Gateway data · model provenance in evidence' : 'Static replay · no live model calls';
});
window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
showView(location.hash.slice(1) || 'home');
