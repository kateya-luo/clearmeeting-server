// Word-compatible flowing HTML: headings stay with the next paragraph; body text
// and action tables can grow across pages without fixed heights or item limits.
const escapeHtml = value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\r?\n/g, '<br>');
const LABELS = {decisions: '会议决定', consensus: '已达成共识', tendencies: '倾向意见', suggestions: '个人建议', disagreements: '分歧意见', unresolved: '待确认事项'};

export function renderTemplateWord(meeting, opts, {title, info, duration}) {
  const s = meeting.summary || {};
  const id = Number(s.template?.id) || 1;
  const accent = id <= 6 ? '#36546b' : id <= 14 ? '#426357' : id <= 24 ? '#334e77' : id <= 28 ? '#625375' : '#71563c';
  const e = escapeHtml;
  const body = [`<p class="eyebrow">会议纪要${s.template?.name ? `　 /　${e(s.template.name)}` : ''}</p>`,
    `<h1 class="document-title">${e(title)}</h1>`];
  if (opts.includeInfo) {
    body.push(`<p class="subtitle">${e(meeting.created_at || '')}${duration && duration !== '—' ? `　 ·　 时长 ${e(duration)}` : ''}</p>`);
    body.push(`<p class="metadata">${info.filter(([key, value]) => ['参会人数', '创建者'].includes(key) && value && value !== '0 人').map(([key, value]) => `${e(key)}　${e(value)}`).join('　 ·　 ')}</p>`);
  }
  let number = 0;
  const heading = text => `<h2><span class="section-number">${String(++number).padStart(2, '0')}</span>　${e(text)}</h2>`;
  const list = items => `<ul>${items.map(item => `<li>${e(item)}</li>`).join('')}</ul>`;
  if (opts.sections.minutes) {
    body.push('<h2 class="summary-heading">会议摘要</h2>', `<p class="summary">${e(s.summary || '暂无摘要')}</p>`);
    for (const section of s.sections || []) {
      body.push(heading(section.heading), section.items?.length ? list(section.items) : '<p class="muted">本场会议未形成相关内容</p>');
    }
    const conclusions = Object.entries(s.conclusions || {}).filter(([, values]) => values?.length);
    if (conclusions.length) {
      body.push(heading('结论与待确认事项'));
      for (const [kind, items] of conclusions) body.push(`<h3>${e(LABELS[kind] || kind)}</h3>`, list(items));
    }
    if (s.action_items?.length) {
      body.push(heading('行动安排'));
      for (const [index, action] of s.action_items.entries()) {
        body.push(`<h3 class="action-title">${String(index + 1).padStart(2, '0')}　${e(action.task)}</h3>`);
        const cell = (text, label = false, span = 1) => `<td${label ? ' class="label"' : ''}${span > 1 ? ` colspan="${span}"` : ''}><p class="cell">${e(text || '待确认')}</p></td>`;
        body.push(`<table class="action-details"><colgroup><col width="16%"><col width="34%"><col width="16%"><col width="34%"></colgroup><tbody>
          <tr>${cell('责任人', true)}${cell(action.assignee)}${cell('当前状态', true)}${cell(action.status)}</tr>
          <tr>${cell('配合人', true)}${cell((action.collaborators || []).join('、'))}${cell('完成日期', true)}${cell(action.deadline)}</tr>
          <tr>${cell('输出成果', true)}${cell(action.deliverable, false, 3)}</tr>
          <tr>${cell('关闭标准', true)}${cell(action.closure_standard, false, 3)}</tr>
        </tbody></table>`);
      }
    }
    if (s.memory_candidates?.length) body.push(heading('会议记忆候选'), list(s.memory_candidates.map(item => item.content)));
  }
  if (opts.sections.mindmap && s.mindmap?.branches?.length) {
    body.push(heading('主题概览'));
    for (const branch of s.mindmap.branches) body.push(`<h3>${e(branch.title)}</h3>`, list(branch.items || []));
  }
  if (opts.sections.captions) {
    body.push(heading('完整字幕记录'));
    if (meeting.segments?.length) {
      for (const seg of meeting.segments) {
        const seconds = Math.floor((seg.start_ms || 0) / 1000);
        const clock = `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
        body.push(`<h3 class="transcript-label">${clock}　${e(seg.speaker_label || '未知说话人')}</h3><p>${e(seg.text)}</p>`);
        if (seg.translation) body.push(`<p class="translation">${e(seg.translation)}</p>`);
      }
    } else for (const line of meeting.transcript || []) body.push(`<p>${e(line)}</p>`);
  }
  if (opts.watermark) body.push(`<p class="export-note">Clear Meeting　导出于 ${e(new Date().toLocaleString('zh-CN', {hour12: false}))}</p>`);
  return `<!doctype html><html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word"><head><meta charset="utf-8">
<style>
@page{size:595.3pt 841.9pt;margin:56.7pt 56.7pt 56.7pt 56.7pt}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;font-size:11pt;color:#202c38;line-height:1.65}
p,li,td{word-wrap:break-word;overflow-wrap:anywhere;orphans:2;widows:2}
p{margin:0 0 8pt}h1,h2,h3{page-break-after:avoid;page-break-inside:avoid}
.eyebrow{font-size:9pt;color:${accent};margin:0 0 10pt}
h1.document-title{mso-style-name:Title;font-size:25pt;line-height:1.3;color:#17212b;font-weight:bold;margin:0 0 14pt;border:none}
.subtitle{color:#6d7782;font-size:9.5pt;margin:0 0 14pt}
h2{font-size:14pt;color:${accent};margin:22pt 0 9pt;line-height:1.4}
.section-number{font-size:10pt;color:#82909c}
h3{font-size:11.5pt;color:#283c50;margin:12pt 0 6pt;line-height:1.5}
.summary-heading{font-size:10pt;margin:20pt 0 6pt;color:#6d7782}
.summary{font-size:11.5pt;line-height:1.8;margin:0 0 14pt}
ul{margin:0 0 10pt;padding-left:20pt}li{margin:0 0 6pt;padding-left:2pt}
table{border-collapse:collapse;width:100%;table-layout:fixed;page-break-inside:auto}
td{vertical-align:top;padding:5pt 9pt;font-size:10.5pt;line-height:1.55}
td.label{width:65pt;color:#687583;background:#f1f4f7}
.metadata{margin:0 0 16pt;font-size:9.5pt;color:#6d7782}
.cell{margin:0;line-height:1.5;font-size:10pt}
.action-title{margin:15pt 0 6pt}.action-details{margin:0 0 13pt}
.action-details td{border-bottom:0.5pt solid #e4e9ee}
.muted,.translation{color:#75808b}.transcript-label{font-size:9pt;color:#697785}
.export-note{margin:24pt 0 0;color:#89939d;font-size:8.5pt}
</style></head><body>${body.join('\n')}</body></html>`;
}
