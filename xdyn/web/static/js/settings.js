(() => {
    const form = document.getElementById('cookieForm');
    if (!form) return;
    const { api, toast } = window.xdyn;

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(form);
        const payload = {
            cookie: String(data.get('cookie') || ''),
            auth_token: String(data.get('auth_token') || ''),
            ct0: String(data.get('ct0') || '')
        };
        if (!payload.cookie && !payload.auth_token && !payload.ct0) {
            return toast('三个字段都为空，没有修改', 'error');
        }
        try {
            const result = await api('/api/settings/cookies', payload);
            toast(result.message || (result.valid ? 'Cookie 已保存并验证' : 'Cookie 已保存，但验证未通过'), result.valid ? 'ok' : 'error');
            if (result.valid) setTimeout(() => window.location.reload(), 500);
        } catch (error) {
            toast(error.message, 'error');
        }
    });

    const test = document.getElementById('testCookie');
    if (test) test.addEventListener('click', async () => {
        try {
            const data = await api('/api/settings/cookies/test', {});
            toast(data.message || 'Cookie 有效', 'ok');
        } catch (error) {
            toast(error.message, 'error');
        }
    });

    const clear = document.getElementById('clearCookie');
    if (clear) clear.addEventListener('click', async () => {
        if (!confirm('确定清除当前 Cookie？清除后无法同步。')) return;
        try {
            await api('/api/settings/cookies/clear', {});
            toast('Cookie 已清除', 'ok');
            setTimeout(() => window.location.reload(), 300);
        } catch (error) {
            toast(error.message, 'error');
        }
    });

    const syncOptions = document.getElementById('syncOptionsForm');
    if (syncOptions) syncOptions.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(syncOptions);
        const sync = {
            sleep: data.get('sleep'),
            jitter: data.get('jitter'),
            retries: data.get('retries'),
            download_concurrency: data.get('download_concurrency'),
            avatar_sleep: data.get('avatar_sleep'),
            page_size: data.get('page_size'),
            max_pages: data.get('max_pages'),
            max_media: data.get('max_media'),
            max_empty_pages: data.get('max_empty_pages'),
            image_format: data.get('image_format'),
            proxy: data.get('proxy'),
            api_backend: data.get('api_backend'),
            include_retweets: data.get('include_retweets') === 'on',
            download_quoted_media: data.get('download_quoted_media') === 'on',
            export_json: data.get('export_json') === 'on'
        };
        try {
            await api('/api/settings/options', { sync });
            toast('同步参数已保存', 'ok');
        } catch (error) {
            toast(error.message, 'error');
        }
    });

    const webOptions = document.getElementById('webOptionsForm');
    if (webOptions) webOptions.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(webOptions);
        const web = {
            page_size: data.get('page_size'),
            media_page_size: data.get('media_page_size'),
            default_view: data.get('default_view'),
            default_sort: data.get('default_sort'),
            default_load: data.get('default_load'),
            show_empty_posts: data.get('show_empty_posts') === 'on',
            show_missing_media: data.get('show_missing_media') === 'on',
            blur_sensitive: data.get('blur_sensitive') === 'on'
        };
        try {
            await api('/api/settings/options', { web });
            toast('显示设置已保存', 'ok');
        } catch (error) {
            toast(error.message, 'error');
        }
    });

    function startJob(url, payload) {
        api(url, payload || {})
            .then(() => {
                toast('任务已加入队列', 'ok');
                setTimeout(() => { window.location.href = '/jobs'; }, 350);
            })
            .catch((error) => toast(error.message, 'error'));
    }

    const mapping = {
        verifyOnly: ['/api/verify', { repair: false }],
        verifyRepair: ['/api/verify', { repair: true }],
        retryAllSettings: ['/api/retry', {}],
        refreshAvatarsSettings: ['/api/avatars/refresh', {}],
        syncAllSettings: ['/api/sync', {}]
    };
    Object.entries(mapping).forEach(([id, [url, payload]]) => {
        const button = document.getElementById(id);
        if (button) button.addEventListener('click', () => startJob(url, payload));
    });
})();
