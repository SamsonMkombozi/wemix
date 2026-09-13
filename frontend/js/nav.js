/* Renders the top nav into #topnav-root, adapting to logged in/out state. */

const NAV_ICONS = {
  home: `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9.5 4.2 4h15.6l1.2 5.5"/><path d="M3 9.5a2.3 2.3 0 0 0 4.6 0 2.3 2.3 0 0 0 4.6 0 2.3 2.3 0 0 0 4.6 0 2.3 2.3 0 0 0 4.6 0"/><path d="M5 9.8V20h14V9.8"/><path d="M9.5 20v-5.5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V20"/></svg>`,
  bell: `<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`,
  gauge: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>`,
  shield: `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2 4 5v6c0 5 3.4 8.5 8 11 4.6-2.5 8-6 8-11V5l-8-3Z"/><path d="m9.5 12 2 2 3.5-4"/></svg>`,
  lock: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10.5" width="16" height="10" rx="2"/><path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5"/></svg>`,
  logout: `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="M16 17l5-5-5-5"/><path d="M21 12H9"/></svg>`,
};

function renderNav() {
  const root = document.getElementById('topnav-root');
  if (!root) return;

  const user = Auth.getUser();
  const loggedIn = Auth.isLoggedIn();
  const isStaff = loggedIn && ['moderator', 'admin', 'super_admin'].includes(user?.role);
  const initial = ((user?.first_name || user?.username || '?').trim().charAt(0) || '?').toUpperCase();
  const displayName = (user?.first_name ? `${user.first_name} ${user.last_name || ''}`.trim() : user?.username) || '';

  root.innerHTML = `
    <nav class="topnav">
      <div class="topnav-inner">
        <a href="index.html" class="brand">
          <img src="images/wemix.jpeg" alt="Wemix" class="brand-logo">
        </a>
        <div class="nav-links">
          ${loggedIn ? `
            <a href="index.html" class="nav-icon-btn" title="Marketplace" aria-label="Marketplace">${NAV_ICONS.home}</a>
            ${isStaff
              ? `<a href="moderator.html" class="nav-icon-btn" title="Moderation" aria-label="Moderation">${NAV_ICONS.shield}</a>`
              : `<a href="dashboard.html" class="nav-icon-btn" title="Dashboard" aria-label="Dashboard">${NAV_ICONS.gauge}</a>`
            }
            <div class="notif-bell-wrap" id="notif-bell-wrap">
              <button class="nav-icon-btn" id="notif-bell-btn" aria-label="Notifications" title="Notifications">
                ${NAV_ICONS.bell}<span class="notif-badge hidden" id="notif-badge">0</span>
              </button>
              <div class="notif-dropdown hidden" id="notif-dropdown">
                <div class="notif-dropdown-header">
                  <strong>Notifications</strong>
                  <a href="#" id="notif-mark-all">Mark all read</a>
                </div>
                <div id="notif-list"><p class="text-muted" style="padding:14px;">Loading&hellip;</p></div>
              </div>
            </div>
            <div class="avatar-wrap" id="avatar-wrap">
              <button class="avatar-btn" id="avatar-btn" aria-label="Account menu" title="${escapeHtml(displayName)}">${user?.avatar ? `<img src="${user.avatar}" alt="">` : escapeHtml(initial)}</button>
              <div class="avatar-dropdown hidden" id="avatar-dropdown">
                <div class="avatar-dropdown-header">
                  <div class="avatar-circle-lg" id="avatar-circle-lg-display">${user?.avatar ? `<img src="${user.avatar}" alt="">` : escapeHtml(initial)}</div>
                  <div>
                    <div class="avatar-name">${escapeHtml(displayName)}</div>
                    <div class="avatar-email">${escapeHtml(user?.email || '')}</div>
                    <span class="pill pill-draft" style="margin-top:4px; display:inline-block;">${escapeHtml((user?.role || '').replace('_', ' '))}</span>
                  </div>
                </div>
                <div class="avatar-dropdown-body">
                  <button class="avatar-menu-item" id="avatar-toggle-profile">${NAV_ICONS.gauge}<span>Edit profile</span></button>
                  <div class="avatar-password-form hidden" id="avatar-profile-form">
                    <div id="avatar-profile-alert"></div>
                    <label style="font-size:0.75rem; font-weight:600; color:var(--color-text-muted); margin-bottom:-4px;">Profile photo</label>
                    <input type="file" id="avatar-file-input" accept="image/*">
                    <label style="font-size:0.75rem; font-weight:600; color:var(--color-text-muted); margin-bottom:-4px;">Bio</label>
                    <textarea id="avatar-bio-input" maxlength="500" placeholder="A short bio shown on your listings." style="min-height:60px; padding:8px 10px; border:1px solid var(--color-border); border-radius:6px; font-size:0.82rem; font-family:inherit;">${escapeHtml(user?.bio || '')}</textarea>
                    <label style="font-size:0.75rem; font-weight:600; color:var(--color-text-muted); margin-bottom:-4px;">Website</label>
                    <input type="url" id="avatar-website-input" value="${escapeHtml(user?.website_url || '')}" placeholder="https://" style="padding:8px 10px; border:1px solid var(--color-border); border-radius:6px; font-size:0.82rem;">
                    <label style="font-size:0.75rem; font-weight:600; color:var(--color-text-muted); margin-bottom:-4px;">Twitter/X</label>
                    <input type="url" id="avatar-twitter-input" value="${escapeHtml(user?.twitter_url || '')}" placeholder="https://twitter.com/" style="padding:8px 10px; border:1px solid var(--color-border); border-radius:6px; font-size:0.82rem;">
                    <label style="font-size:0.75rem; font-weight:600; color:var(--color-text-muted); margin-bottom:-4px;">Facebook</label>
                    <input type="url" id="avatar-facebook-input" value="${escapeHtml(user?.facebook_url || '')}" placeholder="https://facebook.com/" style="padding:8px 10px; border:1px solid var(--color-border); border-radius:6px; font-size:0.82rem;">
                    <button class="btn btn-sm btn-block" id="avatar-profile-submit" style="margin-top:4px;">Save profile</button>
                  </div>
                  <button class="avatar-menu-item" id="avatar-toggle-password">${NAV_ICONS.lock}<span>Change password</span></button>
                  <div class="avatar-password-form hidden" id="avatar-password-form">
                    <div id="avatar-password-alert"></div>
                    <input type="password" id="avatar-current-password" placeholder="Current password" autocomplete="current-password">
                    <input type="password" id="avatar-new-password" placeholder="New password" autocomplete="new-password">
                    <button class="btn btn-sm btn-block" id="avatar-password-submit" style="margin-top:8px;">Update password</button>
                  </div>
                  <button class="avatar-menu-item avatar-menu-item-danger" id="avatar-logout-btn">${NAV_ICONS.logout}<span>Log out</span></button>
                </div>
              </div>
            </div>
          ` : `<a href="login.html" class="btn btn-sm">Log in</a>`}
        </div>
      </div>
    </nav>
  `;

  if (loggedIn) {
    initNotificationBell();
    initAvatarMenu();
  }
}

function notifTimeAgo(isoString) {
  const diff = (Date.now() - new Date(isoString).getTime()) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function initAvatarMenu() {
  const btn = document.getElementById('avatar-btn');
  const dropdown = document.getElementById('avatar-dropdown');
  const toggleBtn = document.getElementById('avatar-toggle-password');
  const passwordForm = document.getElementById('avatar-password-form');
  const submitBtn = document.getElementById('avatar-password-submit');
  const logoutBtn = document.getElementById('avatar-logout-btn');
  const toggleProfileBtn = document.getElementById('avatar-toggle-profile');
  const profileForm = document.getElementById('avatar-profile-form');
  const profileSubmitBtn = document.getElementById('avatar-profile-submit');

  toggleProfileBtn.addEventListener('click', () => {
    profileForm.classList.toggle('hidden');
    passwordForm.classList.add('hidden');
  });

  profileSubmitBtn.addEventListener('click', async () => {
    const alertBox = document.getElementById('avatar-profile-alert');
    const fileInput = document.getElementById('avatar-file-input');
    const bio = document.getElementById('avatar-bio-input').value;
    alertBox.innerHTML = '';
    profileSubmitBtn.disabled = true;
    profileSubmitBtn.innerHTML = '<span class="spinner"></span>';
    try {
      const formData = new FormData();
      formData.append('bio', bio);
      formData.append('website_url', document.getElementById('avatar-website-input').value);
      formData.append('twitter_url', document.getElementById('avatar-twitter-input').value);
      formData.append('facebook_url', document.getElementById('avatar-facebook-input').value);
      if (fileInput.files.length) {
        formData.append('avatar', fileInput.files[0]);
      }
      const updated = await apiFetch('/api/accounts/me/', { method: 'PATCH', body: formData, isForm: true });
      Auth.setSession({ user: updated });
      alertBox.innerHTML = `<div class="alert alert-success" style="font-size:0.78rem; padding:8px;">Profile updated.</div>`;
      renderNav();
    } catch (err) {
      alertBox.innerHTML = `<div class="alert alert-error" style="font-size:0.78rem; padding:8px;">${escapeHtml(formatApiError(err))}</div>`;
    } finally {
      profileSubmitBtn.disabled = false;
      profileSubmitBtn.textContent = 'Save profile';
    }
  });

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.classList.toggle('hidden');
    document.getElementById('notif-dropdown')?.classList.add('hidden');
  });

  document.addEventListener('click', (e) => {
    if (!document.getElementById('avatar-wrap')?.contains(e.target)) {
      dropdown.classList.add('hidden');
      passwordForm.classList.add('hidden');
    }
  });

  toggleBtn.addEventListener('click', () => {
    passwordForm.classList.toggle('hidden');
  });

  submitBtn.addEventListener('click', async () => {
    const alertBox = document.getElementById('avatar-password-alert');
    const currentPassword = document.getElementById('avatar-current-password').value;
    const newPassword = document.getElementById('avatar-new-password').value;
    alertBox.innerHTML = '';
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="spinner"></span>';
    try {
      await apiFetch('/api/accounts/me/change-password/', {
        method: 'POST',
        body: { current_password: currentPassword, new_password: newPassword },
      });
      alertBox.innerHTML = `<div class="alert alert-success" style="font-size:0.78rem; padding:8px;">Password updated.</div>`;
      document.getElementById('avatar-current-password').value = '';
      document.getElementById('avatar-new-password').value = '';
    } catch (err) {
      alertBox.innerHTML = `<div class="alert alert-error" style="font-size:0.78rem; padding:8px;">${escapeHtml(formatApiError(err))}</div>`;
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Update password';
    }
  });

  logoutBtn.addEventListener('click', () => {
    Auth.logout();
  });
}

async function initNotificationBell() {
  const btn = document.getElementById('notif-bell-btn');
  const dropdown = document.getElementById('notif-dropdown');
  const badge = document.getElementById('notif-badge');
  const markAllLink = document.getElementById('notif-mark-all');

  async function refreshBadge() {
    try {
      const data = await apiFetch('/api/notifications/unread-count/');
      const count = data.unread_count || 0;
      if (count > 0) {
        badge.textContent = count > 9 ? '9+' : String(count);
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
    } catch (e) { /* silent -- bell just stays without a badge */ }
  }

  async function loadDropdownList() {
    const listEl = document.getElementById('notif-list');
    try {
      const data = await apiFetch('/api/notifications/');
      const results = (data.results || data).slice(0, 10);
      if (!results.length) {
        listEl.innerHTML = '<p class="text-muted" style="padding:14px;">No notifications yet.</p>';
        return;
      }
      listEl.innerHTML = results.map((n) => `
        <div class="notif-item ${n.is_read ? '' : 'notif-unread'}" data-id="${n.id}" data-link="${escapeHtml(n.link_path || '')}">
          <div class="notif-item-title">${escapeHtml(n.title)}</div>
          ${n.message ? `<div class="notif-item-message">${escapeHtml(n.message)}</div>` : ''}
          <div class="notif-item-time">${notifTimeAgo(n.created_at)}</div>
        </div>
      `).join('');

      listEl.querySelectorAll('.notif-item').forEach((item) => {
        item.addEventListener('click', async () => {
          const id = item.dataset.id;
          const link = item.dataset.link;
          try {
            await apiFetch(`/api/notifications/${id}/read/`, { method: 'POST' });
          } catch (e) { /* non-fatal */ }
          if (link) {
            window.location.href = link;
          } else {
            item.classList.remove('notif-unread');
            refreshBadge();
          }
        });
      });
    } catch (e) {
      listEl.innerHTML = '<p class="text-muted" style="padding:14px;">Could not load notifications.</p>';
    }
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const isHidden = dropdown.classList.contains('hidden');
    dropdown.classList.toggle('hidden');
    document.getElementById('avatar-dropdown')?.classList.add('hidden');
    if (isHidden) loadDropdownList();
  });

  document.addEventListener('click', (e) => {
    if (!document.getElementById('notif-bell-wrap')?.contains(e.target)) {
      dropdown.classList.add('hidden');
    }
  });

  markAllLink.addEventListener('click', async (e) => {
    e.preventDefault();
    try {
      await apiFetch('/api/notifications/mark-all-read/', { method: 'POST' });
      loadDropdownList();
      refreshBadge();
    } catch (err) { /* non-fatal */ }
  });

  refreshBadge();
}

document.addEventListener('DOMContentLoaded', renderNav);
