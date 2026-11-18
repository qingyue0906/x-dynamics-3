(() => {
    const list = document.getElementById('jobsList');
    if (!list) return;
    const { api, toast } = window.xdyn;
    let timer = null;

    function statusClass(status) { return 'status-pill status-' + status; }

    function applyJob(job) {
        const card = list.querySelector(`[data-job-id="${job.id}"]`);
        if (!card) return;
        const pill = card.querySelector('.status-pill');
        if (pill) { pill.className = statusClass(job.status); pill.textContent = job.status; }
        const message = card.querySelector('[data-job-message]');
        if (message) message.textContent = job.message || '等待执行';
        const bar = card.querySelector('[data-job-bar]');
        if (bar) bar.style.width = `${job.percent || 0}%`;
        const progress = card.querySelector('[data-job-progress]');
        if (progress) progress.textContent = `${job.done || 0}/${job.total || 0}`;
        const cancel = card.querySelector('[data-job-cancel]');
        if (cancel && !job.active) cancel.remove();
    }

    async function refresh() {
        try {
            const response = await fetch('/api/jobs', { headers: { 'Accept': 'application/json' } });
            if (!response.ok) return;
            const data = await response.json();
            let active = false;
            (data.jobs || []).forEach((job) => { if (job.active) active = true; applyJob(job); });
            if (!active && timer) { clearInterval(timer); timer = null; }
        } catch (e) {}
    }

    document.querySelectorAll('[data-job-log]').forEach((button) => {
        button.addEventListener('click', async () => {
            const id = button.dataset.jobLog;
            const pre = list.querySelector(`[data-log-for="${id}"]`);
            if (!pre) return;
            try {
                const response = await fetch(`/api/jobs/${id}`, { headers: { 'Accept': 'application/json' } });
                const data = await response.json();
                if (!response.ok || data.ok === false) throw new Error(data.error || '读取失败');
                pre.textContent = (data.job.log || []).join('\n') || '（暂无日志）';
                pre.hidden = !pre.hidden;
            } catch (error) { toast(error.message, 'error'); }
        });
    });

    document.querySelectorAll('[data-job-cancel]').forEach((button) => {
        button.addEventListener('click', async () => {
            try {
                await api(`/api/jobs/${Number(button.dataset.jobCancel)}/cancel`, {});
                toast('已发送取消请求', 'ok');
                refresh();
            } catch (error) { toast(error.message, 'error'); }
        });
    });

    const newSync = document.getElementById('newSyncJob');
    if (newSync) newSync.addEventListener('click', async () => {
        try {
            await api('/api/sync', {});
            toast('同步任务已加入队列', 'ok');
            setTimeout(() => window.location.reload(), 400);
        } catch (error) { toast(error.message, 'error'); }
    });

    if (list.querySelector('.status-running, .status-queued, .status-cancelling')) timer = setInterval(refresh, 1200);
})();
