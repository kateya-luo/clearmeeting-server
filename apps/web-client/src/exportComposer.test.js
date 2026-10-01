import assert from 'node:assert/strict';
import test from 'node:test';

import {toWordHtml, toText, toMarkdown, toJson} from './exportComposer.js';

const options = {includeInfo: false, sections: {minutes: true, captions: false, mindmap: false}, watermark: false};

for (let id = 1; id <= 30; id++) {
  test(`template ${id} exports every canonical section, item and action field`, () => {
    const sections = Array.from({length: 10}, (_, i) => ({heading: `章节-${i}`, items: Array.from({length: 9}, (_, j) => `完整条目-${i}-${j}-结束`)}));
    const action_items = Array.from({length: 9}, (_, i) => ({task: `任务-${i}`, assignee: `责任人-${i}`, collaborators: [`配合人-${i}`], deliverable: `成果-${i}`, deadline: `日期-${i}`, closure_standard: `标准-${i}`, status: `状态-${i}`}));
    const summary = {
      template: {id: String(id).padStart(2, '0'), name: `模板-${id}`}, summary: '完整摘要', sections, action_items,
      conclusions: Object.fromEntries(['decisions', 'consensus', 'tendencies', 'suggestions', 'disagreements', 'unresolved'].map(kind => [kind, [`结论-${kind}`]])),
      mindmap: {branches: sections.slice(0, 8).map(s => ({title: s.heading, items: s.items.slice(0, 5)}))},
      timeline_chapters: [{title: '过时的时间轴', items: ['不应替代模板正文']}],
    };
    const meeting = {title: '测试会议', summary};
    for (const build of [toWordHtml, toText, toMarkdown, toJson]) {
      const result = build(meeting, options);
      for (const section of sections) {
        assert.ok(result.includes(section.heading));
        for (const item of section.items) assert.ok(result.includes(item), `${build.name} missing ${item}`);
      }
      for (const action of action_items) for (const value of Object.values(action).flat()) assert.ok(result.includes(value), `${build.name} missing ${value}`);
      for (const value of Object.values(summary.conclusions).flat()) assert.ok(result.includes(value));
      assert.ok(!result.includes('不应替代模板正文'));
      assert.ok(!build(meeting, {...options, sections: {minutes: false}}).includes('完整条目-'));
    }
    assert.deepEqual(JSON.parse(toJson(meeting, options)).summary.sections, sections);
  });
}

test('Word preserves long multiline text and escapes markup', () => {
  const text = '长内容'.repeat(3000) + '\n末尾 <标签> & 内容';
  const html = toWordHtml({summary: {sections: [{heading: '长章节', items: [text]}]}}, options);
  assert.ok(html.includes('长内容'.repeat(3000)));
  assert.ok(html.includes('<br>末尾 &lt;标签&gt; &amp; 内容'));
});

test('generated Word minutes omit chapter timestamps', () => {
  const html = toWordHtml({
    title: '设备方案评审会',
    created_at: '',
    segments: [],
    summary: {
      summary: '讨论设备方案。',
      decisions: [],
      action_items: [],
      timeline_chapters: [{chapter_no: 1, start_ms: 300000, title: '评审范围', items: ['覆盖通信方案']}],
    },
  }, {
    includeInfo: false,
    sections: {minutes: true, captions: false, mindmap: false},
    watermark: false,
  });

  assert.match(html, /评审范围/);
  assert.doesNotMatch(html, /\b\d{2}:\d{2}\b/);
});
