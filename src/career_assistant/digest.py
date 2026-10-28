"""Daily local candidates, using the existing task and email attempt records."""
from datetime import datetime
import html
import json
import re
from career_assistant.models import HK_TZ, JobPosting
from career_assistant.notify.report import build_html_card
from .store import encode, decode, stamp

KEY = 'course_daily_email'


def settings(store):
    with store.connect() as c:
        row = c.execute('SELECT value FROM kv_store WHERE key=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else {'enabled': False, 'time': '09:00'}


def save_settings(store, enabled, time):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time):
        raise ValueError('每日时间应为 HH:MM（Asia/Hong_Kong）')
    with store.connect() as c:
        c.execute('INSERT OR REPLACE INTO kv_store VALUES(?,?)', (KEY, encode({'enabled': bool(enabled), 'time': time})))


def tasks(store):
    return [t for t in store.list('tasks', kind='email') if t['payload'].get('type') == 'digest']


def enqueue_due(store, now=None):
    now = (now or datetime.now(HK_TZ)).astimezone(HK_TZ)
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT value FROM kv_store WHERE key=?', (KEY,)).fetchone()
        config = json.loads(row[0]) if row else {}
        if not config.get('enabled') or now.strftime('%H:%M') < config['time']:
            return None
        key = f'daily-email:{now:%Y-%m-%d}'
        old = c.execute('SELECT id FROM course_tasks WHERE idempotency_key=?', (key,)).fetchone()
        if old:
            return old[0]
        included = set()
        for row in c.execute("SELECT payload_json FROM course_tasks WHERE kind='email'"):
            payload = json.loads(row[0])
            if payload.get('type') == 'digest': included.update(payload['job_uids'])
        snapshots = c.execute('SELECT * FROM course_job_snapshots WHERE id IN (SELECT MAX(id) FROM course_job_snapshots GROUP BY job_uid) ORDER BY id').fetchall()
        selected = [s for s in snapshots if s['job_uid'] not in included]
        body = '<h1>每日岗位汇总</h1><p>本地新增候选；覆盖率不是录用概率。缓存期限不代表招聘截止。</p>'
        search = c.execute("SELECT * FROM course_tasks WHERE kind='search' ORDER BY id DESC LIMIT 1").fetchone()
        if search:
            search = decode(search)
            body += '<p>最近搜索：' + html.escape(search['created_at'] + ' · ' + search['status']) + '</p>'
            for source, result in (search['result'] or {}).get('sources', {}).items():
                body += '<p>' + html.escape(source + '：' + result['status'] + ' ' + result.get('error', '')) + '</p>'
        else:
            body += '<p>暂无搜索记录；以下仅为本地保存候选。</p>'
        for s in selected:
            match = c.execute('SELECT m.* FROM course_matches m JOIN course_job_snapshots s ON s.id=m.snapshot_id WHERE s.job_uid=? ORDER BY m.id DESC LIMIT 1', (s['job_uid'],)).fetchone()
            label = '未分析'
            if match:
                data = decode(match)['data']
                coverage = data.get('coverage', {})
                coverage = coverage.get('required', {}) if isinstance(coverage, dict) else {}
                coverage = coverage if isinstance(coverage, dict) else {}
                label = '已有匹配（历史依据）；必须条件覆盖率：' + str(coverage.get('percent') if coverage.get('percent') is not None else '未知') + '；不是录用概率'
            body += '<p>' + html.escape(s['source'] + ' · ' + label) + '</p>'
            if s['url'].startswith(('https://', 'http://')):
                job = JobPosting(s['source'], s['external_id'], s['title'], s['company'], s['url'], s['url'])
                body += build_html_card(job).replace('未打分', html.escape(label))
            else:
                body += '<h2>' + html.escape(s['title']) + '</h2><p>' + html.escape(s['company']) + ' · 未提供原链接</p>'
        if not selected: body += '<p>无新增本地候选，跳过发送；不代表网站没有岗位。</p>'
        payload = {'type': 'digest', 'day': now.strftime('%Y-%m-%d'), 'html': body,
                   'job_uids': [s['job_uid'] for s in selected], 'snapshot_ids': [s['id'] for s in selected]}
        return store._insert_on(c, 'tasks', dict(kind='email', payload_json=encode(payload), idempotency_key=key,
            max_attempts=1, side_effect=1, available_at=stamp(), created_at=stamp(), updated_at=stamp()))
