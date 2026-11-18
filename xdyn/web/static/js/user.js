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
    document.querySelectorAll('[data-mode-user]').forEach((select) => {
        select.addEventListener('change', async () => {
            try {
                await api(`/api/users/${Number(select.dataset.modeUser)}/mode`, { mode: select.value });
                toast('同步模式已更新', 'ok');
            } catch (error) {
                toast(error.message, 'error');
            }
        });
    });
})();
