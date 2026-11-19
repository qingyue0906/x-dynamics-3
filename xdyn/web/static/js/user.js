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
    document.querySelectorAll('[data-reset-sync]').forEach((button) => {
        button.addEventListener('click', async () => {
            if (!confirm('放弃当前同步断点？下次同步会从已有水位重新开始。')) return;
            try {
                await api(`/api/users/${Number(button.dataset.resetSync)}/reset-sync`, {});
                toast('同步断点已清除', 'ok');
                setTimeout(() => window.location.reload(), 300);
            } catch (error) {
                toast(error.message, 'error');
            }
        });
    });
})();
