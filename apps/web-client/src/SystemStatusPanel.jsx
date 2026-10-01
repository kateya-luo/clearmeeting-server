import React, {useCallback, useEffect, useState} from 'react';
import {getSystemStatus, resolveOfflineJob} from './api';

const STATUS_TEXT = {
  healthy: '运行正常',
  busy: '正在处理',
  attention: '需要关注',
  degraded: '服务异常',
};

const JOB_TEXT = {
  queued: '排队中',
  running: '转录中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

function formatBytes(value) {
  const gib = Number(value || 0) / 1024 / 1024 / 1024;
  return `${gib.toFixed(gib >= 10 ? 1 : 2)} GB`;
}

function formatUptime(seconds) {
  const days = Math.floor(Number(seconds || 0) / 86400);
  const hours = Math.floor((Number(seconds || 0) % 86400) / 3600);
  return days ? `${days} 天 ${hours} 小时` : `${hours} 小时`;
}

function serviceReady(services) {
  return Object.values(services || {}).filter((service) => service?.ready).length;
}

function formatMeetingTime(value) {
  if (!value) return '时间未知';
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  });
}

function formatAudioRange(startMs, endMs) {
  const clock = (ms) => {
    const seconds = Math.floor(Number(ms || 0) / 1000);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remain = seconds % 60;
    return [hours, minutes, remain].map((part) => String(part).padStart(2, '0')).join(':');
  };
  return `${clock(startMs)} – ${clock(endMs)}`;
}

export default function SystemStatusPanel() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [actionBusy, setActionBusy] = useState(null);
  const [actionMessage, setActionMessage] = useState('');

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      setStatus(await getSystemStatus());
      setError('');
    } catch (requestError) {
      setError(requestError.message || '无法读取服务器状态');
    } finally {
      setBusy(false);
    }
  }, []);

  const handleFailedJob = async (job, action) => {
    const skip = action === 'skip';
    const confirmed = window.confirm(skip
      ? `确定跳过 ${formatAudioRange(job.start_ms, job.end_ms)} 这段转录并继续整理会议吗？\n\n原始录音仍会保留。`
      : `确定重新识别 ${formatAudioRange(job.start_ms, job.end_ms)} 这段音频吗？`);
    if (!confirmed) return;
    setActionBusy(`${job.id}:${action}`);
    setActionMessage('');
    try {
      await resolveOfflineJob(job.id, action);
      setActionMessage(skip ? '已跳过该片段，正在继续整理会议。' : '已重新加入离线识别队列。');
      await refresh();
    } catch (actionError) {
      setActionMessage(actionError.message || '操作失败，请稍后重试');
    } finally {
      setActionBusy(null);
    }
  };

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const host = status?.host;
  const counts = status?.offline?.counts || {};
  const active = Number(counts.queued || 0) + Number(counts.running || 0);
  const meetings = status?.offline?.meetings || [];
  const readyServices = serviceReady(status?.services);
  const totalServices = Object.keys(status?.services || {}).length;

  return (
    <div className="set-card system-monitor-card">
      <div className="system-monitor-head">
        <div>
          <div className="system-title-line">
            <h3>服务器运行状态</h3>
            {status && <span className={`system-badge ${status.status}`}>
              <i />{STATUS_TEXT[status.status] || status.status}
            </span>}
          </div>
          <p className="desc">自动刷新服务器资源与离线转录队列。</p>
        </div>
        <button className="btn ghost system-refresh" type="button" onClick={refresh} disabled={busy}>
          {busy ? '刷新中…' : '立即刷新'}
        </button>
      </div>

      {error && <div className="system-error">{error}，10 秒后自动重试。</div>}
      {actionMessage && <div className="system-action-message">{actionMessage}</div>}
      {!status && !error && <div className="system-loading">正在读取服务器状态…</div>}

      {status && <>
        <div className="system-metrics">
          <div><span>服务器版本</span><b>V{status.version?.server_version || '—'}</b><small>已运行 {formatUptime(host?.uptime_seconds)}</small></div>
          <div><span>CPU 负载</span><b>{host?.load_percent ?? 0}%</b><small>{host?.cpu_count || 0} 线程 · 1分钟 {host?.load_1m ?? 0}</small></div>
          <div><span>可用内存</span><b>{formatBytes(host?.memory?.available_bytes)}</b><small>已使用 {host?.memory?.used_percent ?? 0}%</small></div>
          <div><span>数据磁盘</span><b>{host?.disk?.used_percent ?? 0}%</b><small>剩余 {formatBytes(host?.disk?.free_bytes)}</small></div>
        </div>

        <div className="system-services" aria-label="服务状态">
          <span>服务 {readyServices}/{totalServices}</span>
          {Object.entries({api: '后台', online_asr: '在线转录', offline_asr: '离线转录', speaker: '声纹'}).map(([key, label]) => (
            <span className={status.services?.[key]?.ready ? 'ok' : 'off'} key={key}>
              <i />{label}
            </span>
          ))}
        </div>

        <div className="offline-summary">
          <div className="offline-summary-head">
            <div><b>离线任务</b><span>{active ? `${counts.running || 0} 个处理中，${counts.queued || 0} 个等待` : '当前没有等待任务'}</span></div>
            <div className="queue-totals">
              <span className="running">处理中 {counts.running || 0}</span>
              <span>排队 {counts.queued || 0}</span>
              <span className={counts.failed ? 'failed' : ''}>失败 {counts.failed || 0}</span>
            </div>
          </div>

          {meetings.length > 0 ? <div className="monitor-jobs">
            {meetings.slice(0, 5).map((meeting) => (
              <div className="monitor-job" key={meeting.session_id}>
                <div className="monitor-job-top">
                  <div>
                    <b>{meeting.title}</b>
                    <small>{formatMeetingTime(meeting.meeting_created_at)} · 编号 {meeting.session_id.slice(-8).toUpperCase()}</small>
                    <small>{JOB_TEXT[meeting.state] || meeting.state} · {meeting.done}/{meeting.total} 项</small>
                  </div>
                  <strong className={meeting.state}>{meeting.progress_percent}%</strong>
                </div>
                <div className={`monitor-progress ${meeting.state}`}>
                  <i style={{width: `${Math.max(2, Math.min(100, meeting.progress_percent))}%`}} />
                </div>
                {(meeting.failed_jobs || []).map((job) => <div className="failed-job-actions" key={job.id}>
                  <div>
                    <b>失败片段 {formatAudioRange(job.start_ms, job.end_ms)}</b>
                    <span>已尝试 {job.attempts} 次 · {job.error || '识别失败'}</span>
                  </div>
                  <button type="button" className="btn ghost" disabled={Boolean(actionBusy)}
                    onClick={() => handleFailedJob(job, 'retry')}>
                    {actionBusy === `${job.id}:retry` ? '处理中…' : '重新识别'}
                  </button>
                  <button type="button" className="btn ghost danger-outline" disabled={Boolean(actionBusy)}
                    onClick={() => handleFailedJob(job, 'skip')}>
                    {actionBusy === `${job.id}:skip` ? '处理中…' : '跳过并继续'}
                  </button>
                </div>)}
              </div>
            ))}
          </div> : <div className="monitor-empty">尚无离线转录记录</div>}
        </div>

        <div className="system-foot">
          <span>每 10 秒自动刷新</span>
          <span>更新于 {new Date(status.generated_at).toLocaleTimeString('zh-CN', {hour12: false})}</span>
        </div>
      </>}
    </div>
  );
}
