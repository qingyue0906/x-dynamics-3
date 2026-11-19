(() => {
    const { api, toast } = window.xdyn;
    document.querySelectorAll('[data-sync-user]').forEach((button) => {
        button.addEventListener('click', async () => {
            try {
                await api('/api/sync', { user_ids: [Number(button.dataset.syncUser)] });
                toast('同步任务已加入队列', 'ok');
                setTimeout(() => { window.location.href = '/jobs'; }, 350);
            } catch (error) {
                toast(error.message, 'error');
            }
        });
    });
})();
