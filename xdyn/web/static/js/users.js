(() => {
    const page = document.getElementById('usersPage');
    if (!page) return;
    const { api, toast } = window.xdyn;

    function selected() {
        return Array.from(document.querySelectorAll('.user-check:checked')).map((node) => Number(node.value));
    }

    async function startJob(url, payload) {
        try {
            await api(url, payload);
            toast('任务已加入队列', 'ok');
            setTimeout(() => { window.location.href = '/jobs'; }, 350);
        } catch (error) {
            toast(error.message, 'error');
        }
    }

    const selectAll = document.getElementById('selectAllUsers');
    if (selectAll) {
        selectAll.addEventListener('change', () => {
            document.querySelectorAll('.user-check').forEach((node) => { node.checked = selectAll.checked; });
        });
    }

    const addForm = document.getElementById('addUserForm');
    if (addForm) {
        addForm.addEventListener('submit', async (event) => {
            event.preventDefault();
            const input = document.getElementById('addUserInput').value.trim();
            if (!input) return;
            const button = addForm.querySelector('button[type="submit"]');
            if (button) { button.disabled = true; button.textContent = '获取资料...'; }
            try {
                const data = await api('/api/users/add', { input });
                toast(`已添加 @${data.screen_name || data.name}`, 'ok');
                setTimeout(() => window.location.reload(), 400);
            } catch (error) {
                toast(error.message, 'error');
            } finally {
                if (button) { button.disabled = false; button.textContent = '添加'; }
            }
        });
    }

    document.querySelectorAll('[data-toggle]').forEach((button) => {
        button.addEventListener('click', async () => {
            const id = Number(button.dataset.toggle);
            const enabled = button.dataset.enabled !== '1';
            try {
                await api(`/api/users/${id}/toggle`, { enabled });
                button.dataset.enabled = enabled ? '1' : '0';
                button.classList.toggle('on', enabled);
                const label = button.querySelector('.toggle-label');
                if (label) label.textContent = enabled ? '已启用' : '已停用';
                const row = button.closest('.user-row');
                if (row) row.classList.toggle('is-disabled', !enabled);
            } catch (error) {
                toast(error.message, 'error');
            }
        });
    });

    document.querySelectorAll('[data-sync]').forEach((button) => {
        button.addEventListener('click', () => startJob('/api/sync', {
            user_ids: [Number(button.dataset.sync)],
            full: document.getElementById('fullSync')?.checked || false
        }));
    });

    document.querySelectorAll('[data-retry]').forEach((button) => {
        button.addEventListener('click', () => startJob('/api/retry', { user_ids: [Number(button.dataset.retry)] }));
    });

    const syncAll = document.getElementById('syncAll');
    if (syncAll) syncAll.addEventListener('click', () => startJob('/api/sync', { full: document.getElementById('fullSync')?.checked || false }));

    const syncSelected = document.getElementById('syncSelected');
    if (syncSelected) syncSelected.addEventListener('click', () => {
        const user_ids = selected();
        if (!user_ids.length) return toast('先勾选用户', 'error');
        startJob('/api/sync', { user_ids, full: document.getElementById('fullSync')?.checked || false });
    });

    const retrySelected = document.getElementById('retrySelected');
    if (retrySelected) retrySelected.addEventListener('click', () => {
        const user_ids = selected();
        if (!user_ids.length) return toast('先勾选用户', 'error');
        startJob('/api/retry', { user_ids });
    });

    const retryAll = document.getElementById('retryAll');
    if (retryAll) retryAll.addEventListener('click', () => startJob('/api/retry', {}));

    const refreshAvatars = document.getElementById('refreshAvatars');
    if (refreshAvatars) refreshAvatars.addEventListener('click', () => {
        const user_ids = selected();
        startJob('/api/avatars/refresh', user_ids.length ? { user_ids } : {});
    });
})();
