(() => {
    'use strict';

    async function api(url, payload) {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Xdyn': '1',
                'Accept': 'application/json'
            },
            body: JSON.stringify(payload || {})
        });
        let data = {};
        try { data = await response.json(); } catch (e) {}
        if (!response.ok || data.ok === false) {
            throw new Error(data.error || data.message || ('HTTP ' + response.status));
        }
        return data;
    }

    function toast(message, kind) {
        const host = document.getElementById('toastHost');
        if (!host) return;
        const node = document.createElement('div');
        node.className = 'toast ' + (kind || '');
        node.textContent = message;
        host.appendChild(node);
        setTimeout(() => node.remove(), 4200);
    }

    window.xdyn = { api, toast };

    // theme
    const root = document.documentElement;
    const themeBtn = document.getElementById('themeToggle');
    const themeIcon = themeBtn ? themeBtn.querySelector('.theme-icon') : null;
    function applyTheme(theme) {
        root.dataset.theme = theme;
        if (themeIcon) themeIcon.textContent = theme === 'dark' ? '☀' : '☾';
        try { localStorage.setItem('theme', theme); } catch (e) {}
    }
    if (themeBtn) {
        applyTheme(root.dataset.theme || 'light');
        themeBtn.addEventListener('click', () => applyTheme(root.dataset.theme === 'dark' ? 'light' : 'dark'));
    }

    // author filter
    const authorFilter = document.getElementById('authorFilter');
    if (authorFilter) {
        const authorList = document.getElementById('authorList');
        const links = authorList ? Array.from(authorList.querySelectorAll('.side-user[data-name]')) : [];
        const totalNode = document.getElementById('sideTotal');
        const allCount = links.length;
        const applyFilter = () => {
            const query = authorFilter.value.trim().toLowerCase();
            let visible = 0;
            links.forEach((link) => {
                const haystack = ((link.dataset.name || '') + ' ' + (link.dataset.screen || '') + ' ' + (link.dataset.id || '')).toLowerCase();
                const match = !query || haystack.includes(query);
                link.hidden = !match;
                if (match) visible += 1;
            });
            if (totalNode) totalNode.textContent = query ? `${visible}/${allCount}` : String(allCount);
        };
        authorFilter.addEventListener('input', applyFilter);
        authorFilter.addEventListener('search', applyFilter);
    }

    // active job pill
    async function pollJob() {
        try {
            const response = await fetch('/api/jobs', { headers: { 'Accept': 'application/json' } });
            if (!response.ok) return;
            const data = await response.json();
            const active = (data.jobs || []).find((job) => job.active);
            let pill = document.getElementById('globalJobPill');
            const actions = document.querySelector('.top-actions');
            if (!active) { if (pill) pill.remove(); return; }
            if (!pill && actions) {
                pill = document.createElement('a');
                pill.id = 'globalJobPill';
                pill.className = 'job-pill';
                pill.href = '/jobs';
                pill.innerHTML = '<span class="job-dot"></span><span></span>';
                actions.insertBefore(pill, actions.firstChild);
            }
            if (pill) {
                const label = pill.querySelector('span:last-child');
                if (label) label.textContent = `${active.kind}${active.total ? ' ' + active.done + '/' + active.total : ''}`;
                pill.title = active.message || '当前任务';
            }
        } catch (e) {}
    }
    pollJob();
    setInterval(pollJob, 3000);

    // feed / infinite scroll
    const feed = document.getElementById('feed');
    if (!feed) return;
    const list = document.getElementById('postList');
    const moreWrap = document.getElementById('feedMore');
    const moreBtn = document.getElementById('loadMore');
    const sentinel = document.getElementById('feedSentinel');
    const empty = document.getElementById('feedEmpty');
    const mode = feed.dataset.mode || 'dynamic';
    const loadMode = feed.dataset.load || 'infinite';
    let nextOffset = Number(feed.dataset.nextOffset || 0);
    let loading = false;
    let observer = null;

    function buildUrl(offset) {
        const params = new URLSearchParams();
        params.set('offset', String(offset));
        if (feed.dataset.userId) params.set('user_id', feed.dataset.userId);
        if (feed.dataset.query) params.set('q', feed.dataset.query);
        if (feed.dataset.sort) params.set('sort', feed.dataset.sort);
        return (mode === 'images' ? '/api/media' : '/api/posts') + '?' + params.toString();
    }

    async function loadMore() {
        if (loading || !nextOffset) return;
        loading = true;
        if (moreBtn) { moreBtn.disabled = true; moreBtn.textContent = '加载中...'; }
        try {
            const response = await fetch(buildUrl(nextOffset), { headers: { 'Accept': 'application/json' } });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const data = await response.json();
            if (data.html && list) list.insertAdjacentHTML('beforeend', data.html);
            nextOffset = Number(data.next_offset || 0);
            feed.dataset.nextOffset = String(nextOffset);
            if (empty) empty.hidden = true;
            if (!nextOffset) {
                if (moreWrap) moreWrap.hidden = true;
                if (observer) observer.disconnect();
            }
        } catch (error) {
            console.error(error);
            if (moreBtn) moreBtn.textContent = '加载失败，点击重试';
        } finally {
            loading = false;
            if (moreBtn) {
                moreBtn.disabled = false;
                if (nextOffset && moreBtn.textContent !== '加载失败，点击重试') moreBtn.textContent = '加载更多';
            }
        }
    }

    if (loadMode === 'infinite') {
        if (moreBtn) moreBtn.addEventListener('click', loadMore);
        if ('IntersectionObserver' in window && sentinel) {
            observer = new IntersectionObserver((entries) => {
                if (entries.some((entry) => entry.isIntersecting)) loadMore();
            }, { rootMargin: '700px 0px' });
            observer.observe(sentinel);
        }
    }

    document.addEventListener('error', (event) => {
        const img = event.target;
        if (img && img.tagName === 'IMG') {
            const cell = img.closest('.media-cell');
            if (cell) cell.classList.add('is-error');
        }
    }, true);

    // lightbox
    const lb = document.getElementById('lightbox');
    if (!lb) return;
    const lbImage = document.getElementById('lbImage');
    const lbVideo = document.getElementById('lbVideo');
    const lbCounter = document.getElementById('lbCounter');
    const lbCaption = document.getElementById('lbCaption');
    const lbPrev = document.getElementById('lbPrev');
    const lbNext = document.getElementById('lbNext');
    const lbDownload = document.getElementById('lbDownload');
    const lbTweet = document.getElementById('lbTweet');
    let group = [];
    let index = 0;
    let lastFocus = null;

    function hideStage() {
        if (lbImage) { lbImage.hidden = true; lbImage.src = ''; }
        if (lbVideo) { lbVideo.hidden = true; lbVideo.pause(); lbVideo.removeAttribute('src'); lbVideo.load(); }
    }

    function showItem() {
        if (!group.length) return;
        const item = group[index];
        hideStage();
        if (item.video) {
            if (lbVideo) {
                lbVideo.hidden = false;
                if (item.poster) lbVideo.poster = item.poster;
                lbVideo.src = item.full;
                lbVideo.play().catch(() => {});
            }
        } else if (lbImage) {
            lbImage.hidden = false;
            lbImage.src = item.full;
        }
        if (lbCounter) lbCounter.textContent = `${index + 1} / ${group.length}`;
        if (lbDownload) lbDownload.href = item.full + (item.full.includes('?') ? '&' : '?') + 'download=1';
        if (lbCaption) lbCaption.textContent = item.author ? `${item.author}${item.time ? ' · ' + item.time : ''}` : '';
        if (lbTweet) {
            if (item.tweet) { lbTweet.href = item.tweet; lbTweet.hidden = false; } else { lbTweet.hidden = true; }
        }
        if (lbPrev) lbPrev.hidden = group.length <= 1;
        if (lbNext) lbNext.hidden = group.length <= 1;
    }

    function openLightbox(cell) {
        const wrap = cell.closest('[data-lightbox-group]') || cell.closest('.post-media');
        if (!wrap) return;
        const selector = '.media-cell[data-full]';
        group = Array.from(wrap.querySelectorAll(selector)).map((node) => ({
            full: node.dataset.full,
            video: node.dataset.video === '1',
            poster: node.dataset.poster || '',
            author: node.dataset.author || '',
            time: node.dataset.time || '',
            tweet: node.dataset.tweet || ''
        }));
        index = Math.max(0, group.findIndex((item) => item.full === cell.dataset.full));
        lastFocus = document.activeElement;
        showItem();
        lb.hidden = false;
        lb.setAttribute('aria-hidden', 'false');
        document.body.classList.add('lb-open');
        const closeBtn = lb.querySelector('.lb-close');
        if (closeBtn) closeBtn.focus();
    }

    function closeLightbox() {
        lb.hidden = true;
        lb.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('lb-open');
        hideStage();
        if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    function step(delta) {
        if (group.length <= 1) return;
        index = (index + delta + group.length) % group.length;
        showItem();
    }

    document.addEventListener('click', (event) => {
        const cell = event.target.closest('.media-cell[data-full]');
        if (cell) { event.preventDefault(); openLightbox(cell); return; }
        if (event.target.closest('[data-close]')) closeLightbox();
    });
    if (lbPrev) lbPrev.addEventListener('click', () => step(-1));
    if (lbNext) lbNext.addEventListener('click', () => step(1));
    document.addEventListener('keydown', (event) => {
        if (lb.hidden) return;
        if (event.key === 'Escape') { event.preventDefault(); closeLightbox(); }
        else if (event.key === 'ArrowLeft') { event.preventDefault(); step(-1); }
        else if (event.key === 'ArrowRight') { event.preventDefault(); step(1); }
    });
})();
