"""Cached short greeting: one persistent AI attempt per account/displayed date."""
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import unicodedata

import httpx

logger = logging.getLogger('ai_recorder.daily_message')
FALLBACKS = ('新的一周，从容出发', '认真前行，也记得休息', '稳稳前进，自有收获',
             '保持热爱，慢慢成长', '忙有所获，闲有所乐', '放慢脚步，享受生活', '好好休息，迎接明天')


def safe_text(value):
    if not isinstance(value, str):
        raise ValueError('invalid_text_type')
    value = value.strip()
    if not value or len(value) > 12 or len(value.encode('utf-8')) > 48:
        raise ValueError('invalid_text_length')
    for ch in value:
        code = ord(ch)
        allowed = (0x3400 <= code <= 0x9FFF or ch.isascii() and ch.isalnum()
                   or ch in '，。！？、；：,.!?;:')
        if not allowed or unicodedata.category(ch).startswith('C'):
            raise ValueError('invalid_text_character')
    return value


class DailyMessageService:
    def __init__(self, storage):
        self.db = storage.db
        self.tasks = set()
        self.semaphore = asyncio.Semaphore(2)
        self.db.execute('''CREATE TABLE IF NOT EXISTS device_daily_messages (
            owner_user_id TEXT NOT NULL, displayed_date TEXT NOT NULL,
            text TEXT NOT NULL, state TEXT NOT NULL, model TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_user_id, displayed_date))''')

    def get(self, owner, offset_minutes=480, now=None):
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(timezone(timedelta(minutes=offset_minutes)))
        date = local.date().isoformat()
        key = os.getenv('DEEPSEEK_API_KEY', '')
        model = os.getenv('DAILY_MESSAGE_MODEL', os.getenv('DEEPSEEK_MINUTES_MODEL', 'deepseek-v4-flash'))
        enabled = bool(key) and os.getenv('DAILY_MESSAGE_AI_ENABLED', '1') == '1'
        # Reserve the paid attempt before creating a task. Unique key also protects
        # across processes/restarts; an interrupted attempt is never retried that day.
        with self.db.transaction() as conn:
            inserted = conn.execute(
                'INSERT OR IGNORE INTO device_daily_messages'
                '(owner_user_id,displayed_date,text,state,model,attempts,created_at,updated_at)'
                ' VALUES(?,?,?,?,?,?,?,?)',
                (owner, date, FALLBACKS[local.weekday()], 'reserved' if enabled else 'fallback',
                 model, 1 if enabled else 0, now.isoformat(), now.isoformat())).rowcount
            row = conn.execute('SELECT text FROM device_daily_messages WHERE owner_user_id=? AND displayed_date=?',
                               (owner, date)).fetchone()
        if inserted and enabled:
            task = asyncio.create_task(self._generate(owner, date, local.weekday(), key, model))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return {'date': date, 'text': row['text']}

    async def _request(self, date, weekday, key, model):
        base = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com').rstrip('/')
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(base + '/chat/completions',
                headers={'Authorization': 'Bearer ' + key},
                json={'model': model, 'max_tokens': 128, 'temperature': 0.8,
                      'thinking': {'type': 'disabled'},
                      'messages': [
                          {'role': 'system', 'content': '为墨水屏首页写一句温暖自然的中文今日寄语。最多12个字符，包括标点。不含标题、引号、换行、空格、emoji。只输出寄语正文。不要编造天气、用户日程或节假日安排。'},
                          {'role': 'user', 'content': f'日期{date}，星期{weekday + 1}。'}]})
            response.raise_for_status()
            choice = response.json()['choices'][0]
            if choice.get('finish_reason') not in ('stop', 'end_turn'):
                raise ValueError('incomplete_output')
            return safe_text(choice['message']['content'])

    async def _generate(self, owner, date, weekday, key, model):
        try:
            async with self.semaphore:
                self.db.execute("UPDATE device_daily_messages SET state='generating' WHERE owner_user_id=? AND displayed_date=?",
                                (owner, date))
                text = await self._request(date, weekday, key, model)
            self.db.execute("UPDATE device_daily_messages SET text=?,state='ready',last_error=NULL,updated_at=?"
                            ' WHERE owner_user_id=? AND displayed_date=?',
                            (text, datetime.now(timezone.utc).isoformat(), owner, date))
            logger.info('daily_message_ready date=%s chars=%d', date, len(text))
        except asyncio.CancelledError:
            self._failed(owner, date, 'interrupted')
            raise
        except Exception as exc:
            self._failed(owner, date, type(exc).__name__)

    def _failed(self, owner, date, error):
        self.db.execute("UPDATE device_daily_messages SET state='fallback',last_error=?,updated_at=?"
                        ' WHERE owner_user_id=? AND displayed_date=?',
                        (error, datetime.now(timezone.utc).isoformat(), owner, date))
        logger.warning('daily_message_fallback date=%s reason=%s', date, error)

    async def stop(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
