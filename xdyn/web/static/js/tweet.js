(() => {
    const input = document.getElementById('tweetInput');
    if (!input) return;
    const { api, toast } = window.xdyn;
    const previewButton = document.getElementById('previewTweet');
    const downloadButton = document.getElementById('downloadTweet');
    const preview = document.getElementById('tweetPreview');
    let current = null;

    function reset() {
        current = null;
        downloadButton.disabled = true;
        preview.hidden = true;
    }
    input.addEventListener('input', reset);

    previewButton.addEventListener('click', async () => {
        const value = input.value.trim();
        if (!value) return toast('先输入推文链接或 ID', 'error');
        previewButton.disabled = true;
        previewButton.textContent = '获取中...';
        try {
            const data = await api('/api/tweet/preview', { input: value });
            current = data.post;
            document.getElementById('tweetAuthor').textContent = `${data.post.author || ''} @${data.post.screen_name || ''}`;
            document.getElementById('tweetMeta').textContent =
                `${data.post.media_count} 个媒体` + (data.post.has_video ? ' · 含视频' : '') +
                (data.post.published_at ? ' · ' + new Date(data.post.published_at * 1000).toLocaleString() : '');
            document.getElementById('tweetText').textContent = data.post.text || '';
            document.getElementById('tweetText').hidden = !data.post.text;
            document.getElementById('tweetLink').href = data.post.url;
            const avatar = document.getElementById('tweetAvatar');
            avatar.textContent = (data.post.author || '?').slice(0, 1);
            const quote = document.getElementById('tweetQuote');
            if (data.post.quoted && (data.post.quoted.text || data.post.quoted.author)) {
                quote.hidden = false;
                quote.innerHTML = '';
                const head = document.createElement('div');
                head.className = 'post-quote-head';
                head.textContent = '@' + (data.post.quoted.author || '引用推文');
                const body = document.createElement('div');
                body.textContent = data.post.quoted.text || '';
                quote.appendChild(head);
                quote.appendChild(body);
            } else {
                quote.hidden = true;
            }
            const thumbs = document.getElementById('tweetThumbs');
            thumbs.innerHTML = '';
            (data.post.media || []).forEach((item) => {
                const fig = document.createElement('div');
                fig.className = 'dynamic-thumb';
                if (item.type === 'video' || item.type === 'animated_gif') {
                    const video = document.createElement('video');
                    video.controls = true;
                    video.loop = item.type === 'animated_gif';
                    video.muted = item.type === 'animated_gif';
                    if (item.poster) video.poster = item.poster;
                    video.src = item.url;
                    video.preload = 'metadata';
                    fig.appendChild(video);
                } else {
                    const img = document.createElement('img');
                    img.src = item.url;
                    img.alt = '';
                    img.loading = 'lazy';
                    img.referrerPolicy = 'no-referrer';
                    img.onerror = () => { fig.textContent = '预览不可用'; fig.className = 'dynamic-thumb-missing'; };
                    fig.appendChild(img);
                }
                thumbs.appendChild(fig);
            });
            preview.hidden = false;
            downloadButton.disabled = false;
        } catch (error) {
            toast(error.message, 'error');
        } finally {
            previewButton.disabled = false;
            previewButton.textContent = '预览';
        }
    });

    downloadButton.addEventListener('click', async () => {
        if (!current) return;
        try {
            await api('/api/tweet/download', {
                input: input.value.trim(),
                add_user: document.getElementById('addTweetUser').checked
            });
            toast('下载任务已加入队列', 'ok');
            setTimeout(() => { window.location.href = '/jobs'; }, 350);
        } catch (error) {
            toast(error.message, 'error');
        }
    });
})();
