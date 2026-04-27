/**
 * SoCandyShop TikTok Feed — Show shop's TikTok videos on the store
 *
 * Fetches videos from the FastAPI backend and renders them as a grid.
 */
(() => {
  const feeds = document.querySelectorAll('[id^="tiktok-feed-"]');
  if (!feeds.length) return;

  feeds.forEach((feed) => {
    if (feed.dataset.tiktokFeedInit === 'true') return;
    feed.dataset.tiktokFeedInit = 'true';

    const API_BASE = feed.dataset.apiBase || 'http://socandyshopfr:3100/api';
    const USERNAME = feed.dataset.username || 'soetsopains';
    const COUNT = parseInt(feed.dataset.count || '8', 10);
    const SOURCE = feed.dataset.source || 'user'; // 'user', 'trending', 'hashtag'
    const HASHTAG = feed.dataset.hashtag || '';
    const LINK_TARGET = feed.dataset.linkTarget || '_blank';

    const grid = feed.querySelector('.tiktok-feed__grid');
    const loading = feed.querySelector('.tiktok-feed__loading');
    const error = feed.querySelector('.tiktok-feed__error');

    async function loadVideos() {
      if (loading) loading.style.display = '';
      if (error) error.style.display = 'none';

      try {
        let url;
        if (SOURCE === 'trending') {
          url = `${API_BASE}/trending?count=${COUNT}`;
        } else if (SOURCE === 'hashtag') {
          url = `${API_BASE}/hashtag/${encodeURIComponent(HASHTAG)}?count=${COUNT}`;
        } else {
          url = `${API_BASE}/user/${encodeURIComponent(USERNAME)}/videos?count=${COUNT}`;
        }

        const resp = await fetch(url, {
          headers: { 'Accept': 'application/json' },
          cache: 'no-store',
        });

        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        const data = await resp.json();
        const videos = data.videos || [];

        if (videos.length === 0) {
          if (loading) loading.style.display = 'none';
          if (error) {
            error.textContent = 'Aucune vidéo trouvée pour le moment.';
            error.style.display = '';
          }
          return;
        }

        renderVideos(videos);
      } catch (err) {
        console.warn('[TikTok Feed] Error:', err.message);
        if (loading) loading.style.display = 'none';
        if (error) {
          error.textContent = 'Impossible de charger les vidéos TikTok.';
          error.style.display = '';
        }
      }
    }

    function renderVideos(videos) {
      if (!grid) return;

      grid.innerHTML = videos.map((video) => `
        <a href="${escapeHTML(video.url)}" target="${LINK_TARGET}" rel="noopener" class="tiktok-feed__card">
          <div class="tiktok-feed__media">
            <img
              src="${escapeHTML(video.cover_url || '')}"
              alt="${escapeHTML(video.description || 'TikTok Video')}"
              loading="lazy"
              class="tiktok-feed__cover"
              onerror="this.parentElement.classList.add('tiktok-feed__media--broken')"
            />
            ${video.duration ? `<span class="tiktok-feed__duration">${formatDuration(video.duration)}</span>` : ''}
            <div class="tiktok-feed__play-overlay">
              <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24"><path d="M8 5v14l11-7z"/></svg>
            </div>
          </div>
          <div class="tiktok-feed__info">
            <p class="tiktok-feed__desc">${escapeHTML(truncate(video.description, 80))}</p>
            <div class="tiktok-feed__stats">
              <span class="tiktok-feed__stat">
                <svg viewBox="0 0 24 24" fill="currentColor" width="12" height="12"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>
                ${formatCount(video.likes)}
              </span>
              <span class="tiktok-feed__stat">
                <svg viewBox="0 0 24 24" fill="currentColor" width="12" height="12"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
                ${formatCount(video.views)}
              </span>
            </div>
          </div>
        </a>
      `).join('');

      if (loading) loading.style.display = 'none';
    }

    function escapeHTML(v) {
      return String(v).replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      })[c]);
    }

    function truncate(str, len) {
      if (!str) return '';
      return str.length > len ? str.substring(0, len) + '...' : str;
    }

    function formatCount(n) {
      if (!n) return '0';
      if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'M';
      if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
      return String(n);
    }

    function formatDuration(seconds) {
      if (!seconds) return '';
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      return `${m}:${String(s).padStart(2, '0')}`;
    }

    // Load on idle
    if (typeof window.requestIdleCallback === 'function') {
      window.requestIdleCallback(loadVideos, { timeout: 3000 });
    } else {
      setTimeout(loadVideos, 200);
    }
  });
})();
