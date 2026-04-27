/**
 * SoCandyShop TikTok Live Banner — Enhanced Edition
 *
 * Works with the new FastAPI backend at socandyshopfr:3100
 * Shows live status, viewer count, avatar, and live title.
 * Falls back gracefully if the backend is unreachable.
 */
(() => {
  const sections = document.querySelectorAll('[id^="tiktok-live-"]');
  if (!sections.length) return;

  const TIKTOK_SVG = `<svg class="tiktok-live-banner__icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-2.88 2.5 2.89 2.89 0 0 1-2.89-2.89 2.89 2.89 0 0 1 2.89-2.89c.28 0 .54.04.79.1V9.01a6.34 6.34 0 0 0-.79-.05 6.34 6.34 0 0 0-6.34 6.34 6.34 6.34 0 0 0 6.34 6.34 6.34 6.34 0 0 0 6.33-6.34V8.69a8.13 8.13 0 0 0 4.77 1.52V6.76a4.85 4.85 0 0 1-1-.07z" fill="currentColor"/></svg>`;

  const EYE_SVG = `<svg class="tiktok-live-banner__eye-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 5C5.636 5 2 12 2 12s3.636 7 10 7 10-7 10-7-3.636-7-10-7z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2"/></svg>`;

  const escapeHTML = (value = '') => String(value).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[c]);

  const formatViewers = (n) => {
    if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
    return String(n);
  };

  sections.forEach((section) => {
    if (section.dataset.tiktokLiveInitialized === 'true') return;

    const inner = section.querySelector('.tiktok-live-banner__inner');
    const ctaWrap = section.querySelector('.tiktok-live-banner__cta-wrap');
    const mark = section.querySelector('.tiktok-live-banner__mark');
    if (!inner) return;

    section.dataset.tiktokLiveInitialized = 'true';

    // Backend URL — uses proxy_url from section settings, falls back to Tailscale
    const PROXY_URL = section.dataset.proxyUrl || 'http://socandyshopfr:3100/api/tiktok-live';
    const FORCE_LIVE = section.dataset.forceLive === 'true';
    const POLL_INTERVAL = Math.max(
      parseInt(section.dataset.pollInterval || '60', 10) * 1000,
      30000
    );
    const LIVE_URL = section.dataset.liveUrl;
    const PROFILE_URL = section.dataset.profileUrl;
    const SHOW_BADGE = section.dataset.showBadge === 'true';
    const SHOW_HINT = section.dataset.showHint === 'true';

    const TEXT = {
      headingLive: section.dataset.headingLive,
      headingOffline: section.dataset.headingOffline,
      subheadingLive: section.dataset.subheadingLive,
      subheadingOffline: section.dataset.subheadingOffline,
      buttonLive: section.dataset.buttonLive,
      buttonOffline: section.dataset.buttonOffline,
      schedule: section.dataset.schedule,
      offlineHint: section.dataset.offlineHint,
    };

    let isLive = false;
    let liveData = null;
    let pollTimer = null;

    function setSectionState(state) {
      section.dataset.liveState = state;
      section.classList.toggle('tiktok-live-banner--live', state === 'live');
      section.classList.toggle('tiktok-live-banner--offline', state === 'offline');
      section.classList.toggle('tiktok-live-banner--loading', state === 'loading');
      if (mark) mark.hidden = state !== 'live';
      inner.setAttribute('data-live-state', state);
      inner.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
    }

    function render(state) {
      setSectionState(state);

      const stateClass = state === 'live' ? 'live' : state === 'loading' ? 'loading' : 'offline';
      const badgeText = state === 'live' ? 'Live' : state === 'loading' ? 'Vérification...' : 'Hors-ligne';
      const heading = state === 'live' ? TEXT.headingLive : TEXT.headingOffline;
      const subheading = state === 'live' ? TEXT.subheadingLive : TEXT.subheadingOffline;
      const btnLabel = state === 'live' ? TEXT.buttonLive : TEXT.buttonOffline;
      const btnUrl = state === 'live' ? LIVE_URL : PROFILE_URL;
      const btnClass = state === 'live' ? 'tiktok-live-banner__cta--live' : 'tiktok-live-banner__cta--offline';

      // Use live title from API if available
      const displayHeading = (state === 'live' && liveData?.title) ? liveData.title : heading;

      let html = '';

      if (SHOW_BADGE) {
        html += `<div class="tiktok-live-banner__badge-row">`;
        html += `<span class="tiktok-live-banner__live-dot tiktok-live-banner__live-dot--${stateClass}">
          <span class="tiktok-live-banner__pulse tiktok-live-banner__pulse--${stateClass}"></span>${escapeHTML(badgeText)}</span>`;

        // Show viewer count when live (from the new backend)
        if (state === 'live' && liveData?.viewer_count > 0) {
          html += `<span class="tiktok-live-banner__viewers">
            ${EYE_SVG}<span class="tiktok-live-banner__viewers-count">${escapeHTML(formatViewers(liveData.viewer_count))}</span>
          </span>`;
        }
        html += `</div>`;
      }

      if (displayHeading) {
        html += `<h2 class="tiktok-live-banner__heading">${escapeHTML(displayHeading)}</h2>`;
      }

      if (subheading && state !== 'live') {
        html += `<p class="tiktok-live-banner__subheading">${escapeHTML(subheading)}</p>`;
      }

      if (TEXT.schedule) {
        html += `<p class="tiktok-live-banner__schedule">${escapeHTML(TEXT.schedule)}</p>`;
      }

      if (state === 'offline' && SHOW_HINT && TEXT.offlineHint) {
        html += `<p class="tiktok-live-banner__status-text">${escapeHTML(TEXT.offlineHint)}</p>`;
      }

      const noscript = inner.querySelector('noscript');
      inner.innerHTML = html;
      if (noscript) inner.appendChild(noscript);

      // Render CTA button
      if (ctaWrap && state !== 'loading') {
        const ctaHtml = `<a href="${escapeHTML(btnUrl)}" target="_blank" rel="noopener" class="tiktok-live-banner__cta ${btnClass}">
        ${TIKTOK_SVG}${escapeHTML(btnLabel)}</a>`;
        ctaWrap.innerHTML = ctaHtml;
      }

      // Update mark with avatar when live (from backend's avatar_url)
      if (mark && state === 'live' && liveData?.avatar_url) {
        mark.innerHTML = `<img class="tiktok-live-banner__avatar" src="${escapeHTML(liveData.avatar_url)}" alt="" loading="lazy">${TIKTOK_SVG}`;
      } else if (mark) {
        mark.innerHTML = TIKTOK_SVG;
      }
    }

    async function checkLiveStatus() {
      try {
        // Use the new backend API
        const apiUrl = PROXY_URL.endsWith('/api/tiktok-live')
          ? PROXY_URL
          : `${PROXY_URL.replace(/\/+$/, '')}/api/tiktok-live`;

        const response = await fetch(apiUrl, {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          cache: 'no-store',
        });

        if (!response.ok) {
          console.warn('[TikTok Live] Backend returned', response.status);
          render('offline');
          return;
        }

        const data = await response.json();
        isLive = !!data.live;
        liveData = data;

        render(isLive ? 'live' : 'offline');
      } catch (err) {
        console.warn('[TikTok Live] Backend error:', err.message);
        render('offline');
      }
    }

    // If force_live is set, skip backend polling entirely
    if (FORCE_LIVE) {
      render('live');
      return;
    }

    render('loading');

    const scheduleIdle = (fn) => {
      if (typeof window.requestIdleCallback === 'function') {
        window.requestIdleCallback(fn, { timeout: 2000 });
      } else {
        setTimeout(fn, 200);
      }
    };

    scheduleIdle(() => {
      checkLiveStatus();
      if (POLL_INTERVAL >= 30000) {
        pollTimer = setInterval(checkLiveStatus, POLL_INTERVAL);
      }
    });

    window.addEventListener('beforeunload', () => {
      if (pollTimer) clearInterval(pollTimer);
    });
  });
})();
