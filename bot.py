import os
import re
import json
import logging
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, date, timedelta
from collections import defaultdict
from flask import Flask, request, abort, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer,
)
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── 設定 ────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
LINE_CHANNEL_SECRET       = os.environ['LINE_CHANNEL_SECRET']
GROUP_ID                  = os.environ['LINE_GROUP_ID']
SUPABASE_URL              = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY              = os.environ.get('SUPABASE_KEY', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

TZ         = pytz.timezone('Asia/Taipei')
MANAGERS   = ['Andy', '小陳', 'Hank', '小楊']
STATE_FILE = 'state.json'

# ── Supabase ─────────────────────────────────────────────────
supabase_client = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info('Supabase 連線成功')
    except Exception as e:
        logger.error(f'Supabase 連線失敗：{e}')

# ── 解析工具 ─────────────────────────────────────────────────
def parse_morning_todos(text):
    """把多行待辦文字拆成獨立項目清單"""
    lines = text.strip().split('\n')
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(r'^[\d一二三四五六七八九十]+[\.、。\)）:：]\s*', '', line)
        items.append(cleaned if cleaned else line)
    return items if items else [text.strip()]

def parse_evening_report(text):
    """把 ✅/❌ 回報文字拆成項目 + 狀態 + 原因

    支援格式（emoji 可在行首或行尾）：
      任務✅  /  ✅任務                   → done
      任務❌  /  ❌任務                   → incomplete
      任務❌（原因）                       → incomplete，同行括號原因
      任務❌                              → incomplete
      原因說明文字（緊接在 ❌ 行後、無 ✅/❌）→ 自動併入上一筆的 reason
    """
    DONE_MARKS = ('✅', '✔', '☑')
    FAIL_MARKS = ('❌', '✗')
    ALL_MARKS  = DONE_MARKS + FAIL_MARKS

    def has_mark(line, marks):
        return any(m in line for m in marks)

    def strip_marks(line, marks):
        for m in marks:
            line = line.replace(m, '')
        return line.strip()

    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    items = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if has_mark(line, FAIL_MARKS):
            # 移除所有 ❌ 符號
            content = strip_marks(line, FAIL_MARKS)
            # 提取同行括號原因
            match = re.search(r'[（(](.+?)[）)]', content)
            reason = match.group(1) if match else ''
            item_text = re.sub(r'\s*[（(].+?[）)]\s*', '', content).strip()

            # 檢查下一行：若無 ✅/❌，視為原因說明，合併並跳過
            if not reason and i + 1 < len(lines):
                next_line = lines[i + 1]
                if not has_mark(next_line, ALL_MARKS):
                    reason = next_line
                    i += 1  # 跳過原因行

            items.append({'item': item_text, 'status': 'incomplete', 'reason': reason})

        elif has_mark(line, DONE_MARKS):
            content = strip_marks(line, DONE_MARKS)
            items.append({'item': content, 'status': 'done', 'reason': ''})

        else:
            # 純文字行（無 ✅/❌）→ 預設完成
            items.append({'item': line, 'status': 'done', 'reason': ''})

        i += 1

    return items if items else [{'item': text.strip(), 'status': 'done', 'reason': ''}]

# ── Supabase 寫入 ────────────────────────────────────────────
def save_morning_to_db(todos, date_str, carryover=None):
    """儲存早報資料到 Supabase
    carryover: {manager: [(item_text, carryover_count), ...]}
    """
    if not supabase_client:
        return
    try:
        rows = []
        for manager in MANAGERS:
            if manager in todos:
                for item in parse_morning_todos(todos[manager]):
                    rows.append({
                        'report_date': date_str,
                        'manager': manager,
                        'session': 'morning',
                        'item_text': item,
                        'status': 'reported',
                        'reason': ''
                    })
            else:
                rows.append({
                    'report_date': date_str,
                    'manager': manager,
                    'session': 'morning',
                    'item_text': '（未回報）',
                    'status': 'not_reported',
                    'reason': ''
                })
        # 加入自動結轉任務
        if carryover:
            for manager, items in carryover.items():
                for item_text, count in items:
                    rows.append({
                        'report_date': date_str,
                        'manager': manager,
                        'session': 'morning',
                        'item_text': item_text,
                        'status': 'reported',
                        'reason': f'結轉×{count}'
                    })
        if rows:
            supabase_client.table('daily_reports').insert(rows).execute()
            logger.info(f'早晨資料已存入 Supabase：{len(rows)} 筆')
    except Exception as e:
        logger.error(f'早晨資料存入 Supabase 失敗：{e}')

def save_evening_to_db(reports, date_str):
    if not supabase_client:
        return
    try:
        rows = []
        for manager in MANAGERS:
            if manager in reports:
                for item_data in parse_evening_report(reports[manager]):
                    rows.append({
                        'report_date': date_str,
                        'manager': manager,
                        'session': 'evening',
                        'item_text': item_data['item'],
                        'status': item_data['status'],
                        'reason': item_data['reason']
                    })
            else:
                rows.append({
                    'report_date': date_str,
                    'manager': manager,
                    'session': 'evening',
                    'item_text': '（未回報）',
                    'status': 'not_reported',
                    'reason': ''
                })
        if rows:
            supabase_client.table('daily_reports').insert(rows).execute()
            logger.info(f'晚間資料已存入 Supabase：{len(rows)} 筆')
    except Exception as e:
        logger.error(f'晚間資料存入 Supabase 失敗：{e}')

# ── 昨日未完成結轉 ───────────────────────────────────────────
def get_yesterday_incomplete():
    """取得昨日未完成項目，依主管分組（純文字清單，用於早晨提示）"""
    if not supabase_client:
        return {}
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        result = supabase_client.table('daily_reports')\
            .select('manager, item_text')\
            .eq('report_date', yesterday)\
            .eq('session', 'evening')\
            .eq('status', 'incomplete')\
            .execute()
        by_mgr = defaultdict(list)
        for row in result.data:
            by_mgr[row['manager']].append(row['item_text'])
        return dict(by_mgr)
    except Exception as e:
        logger.error(f'取得昨日未完成失敗：{e}')
        return {}

def get_carryover_items(today_str):
    """取得需要結轉到今天的任務：昨日晚報 incomplete，且今日早報尚未包含
    回傳格式：{manager: [(item_text, carryover_count), ...]}
    carryover_count 從昨日早報的 reason 欄位讀取（格式：'結轉×N'）
    """
    if not supabase_client:
        return {}
    yesterday = (date.fromisoformat(today_str) - timedelta(days=1)).isoformat()
    try:
        # 昨日晚報未完成
        eve_result = supabase_client.table('daily_reports')\
            .select('manager, item_text')\
            .eq('report_date', yesterday)\
            .eq('session', 'evening')\
            .eq('status', 'incomplete')\
            .execute()
        if not eve_result.data:
            return {}

        # 昨日早報（含結轉次數）
        morn_result = supabase_client.table('daily_reports')\
            .select('manager, item_text, reason')\
            .eq('report_date', yesterday)\
            .eq('session', 'morning')\
            .execute()
        # 建立 {(manager, item_text): carryover_count} 的查找表
        prev_count = {}
        for row in morn_result.data:
            r = row.get('reason', '') or ''
            if r.startswith('結轉×'):
                try:
                    cnt = int(r.replace('結轉×', '').strip())
                except ValueError:
                    cnt = 1
            else:
                cnt = 0
            prev_count[(row['manager'], row['item_text'])] = cnt

        # 今日早報已登記的項目（避免重複結轉）
        today_result = supabase_client.table('daily_reports')\
            .select('manager, item_text')\
            .eq('report_date', today_str)\
            .eq('session', 'morning')\
            .execute()
        today_items = set(
            (r['manager'], r['item_text']) for r in (today_result.data or [])
        )

        by_mgr = defaultdict(list)
        for row in eve_result.data:
            mgr, item = row['manager'], row['item_text']
            if item in ('（未回報）',):
                continue
            # 今日早報裡已有這項 → 不重複結轉
            if (mgr, item) in today_items:
                continue
            new_count = prev_count.get((mgr, item), 0) + 1
            by_mgr[mgr].append((item, new_count))
        return dict(by_mgr)
    except Exception as e:
        logger.error(f'取得結轉任務失敗：{e}')
        return {}

# ── 月度報告 ──────────────────────────────────────────────────
def send_monthly_report():
    """每月 1 號 08:00 發送上月完成率報告"""
    if not supabase_client:
        return
    try:
        today = date.today()
        if today.month == 1:
            m_start = date(today.year - 1, 12, 1)
            m_end   = date(today.year, 1, 1) - timedelta(days=1)
        else:
            m_start = date(today.year, today.month - 1, 1)
            m_end   = date(today.year, today.month, 1) - timedelta(days=1)

        result = supabase_client.table('daily_reports')\
            .select('manager, status, item_text')\
            .eq('session', 'evening')\
            .gte('report_date', m_start.isoformat())\
            .lte('report_date', m_end.isoformat())\
            .execute()

        if not result.data:
            push(TextMessage(text=f"📊 {m_start.month}月 尚無回報資料"))
            return

        stats = defaultdict(lambda: {'done': 0, 'total': 0})
        incomplete_cnt = defaultdict(lambda: defaultdict(int))
        skip = {'休假', '（未回報）'}

        for row in result.data:
            if row['item_text'] in skip or row['status'] == 'not_reported':
                continue
            m = row['manager']
            stats[m]['total'] += 1
            if row['status'] == 'done':
                stats[m]['done'] += 1
            elif row['status'] == 'incomplete':
                incomplete_cnt[m][row['item_text']] += 1

        month_label = f"{m_start.year}/{m_start.month}月"
        lines = [f"📊 {month_label} 月度完成率報告\n"]

        for mgr in MANAGERS:
            s = stats.get(mgr, {'done': 0, 'total': 0})
            if s['total'] == 0:
                lines.append(f"{mgr}：無資料")
                continue
            rate = int(s['done'] / s['total'] * 100)
            bar = '█' * (rate // 10) + '░' * (10 - rate // 10)
            lines.append(f"{mgr}  {bar} {rate}%")

        all_inc = []
        for mgr in MANAGERS:
            for item, cnt in sorted(incomplete_cnt[mgr].items(), key=lambda x: -x[1])[:3]:
                all_inc.append((mgr, item, cnt))

        if all_inc:
            lines.append("\n常見未完成項目：")
            for mgr, item, cnt in sorted(all_inc, key=lambda x: -x[2])[:6]:
                lines.append(f"• {mgr}：{item}（{cnt}次）")

        lines.append(f"\n詳細紀錄：https://aivy-line-bot.onrender.com/dashboard")
        push(TextMessage(text='\n'.join(lines)))
        logger.info('月度報告發送完成')
    except Exception as e:
        logger.error(f'月度報告失敗：{e}')

# ── 連續缺報警告 ─────────────────────────────────────────────
def check_missing_reports():
    """連續 2 天完全未回報 → 推播提醒 Andy"""
    if not supabase_client:
        return
    try:
        today = date.today()
        four_days_ago = (today - timedelta(days=4)).isoformat()

        result = supabase_client.table('daily_reports')\
            .select('report_date, manager')\
            .eq('session', 'evening')\
            .eq('status', 'not_reported')\
            .gte('report_date', four_days_ago)\
            .execute()

        missing_dates = defaultdict(set)
        for row in result.data:
            missing_dates[row['manager']].add(row['report_date'])

        alerts = []
        for manager in MANAGERS:
            consecutive = 0
            check = today - timedelta(days=1)
            for _ in range(4):
                if check.isoformat() in missing_dates[manager]:
                    consecutive += 1
                    check -= timedelta(days=1)
                else:
                    break
            if consecutive >= 2:
                alerts.append(f"🚨 {manager} 已連續 {consecutive} 天未回報，建議主動聯繫了解狀況")

        if alerts:
            msg = "⚠️ 連續缺報警告\n\n" + '\n'.join(alerts)
            push(TextMessage(text=msg))
            logger.info(f'已發送缺報警告：{len(alerts)} 人')
    except Exception as e:
        logger.error(f'檢查缺報失敗：{e}')

# ── 週報 ─────────────────────────────────────────────────────
def send_weekly_report():
    """每週一 08:00 發送上週完成率報告給群組"""
    if not supabase_client:
        return
    try:
        today = date.today()
        week_start = (today - timedelta(days=7)).isoformat()
        week_end   = (today - timedelta(days=1)).isoformat()

        result = supabase_client.table('daily_reports')\
            .select('manager, status')\
            .eq('session', 'evening')\
            .gte('report_date', week_start)\
            .lte('report_date', week_end)\
            .execute()

        if not result.data:
            push(TextMessage(text="📊 上週尚無回報資料"))
            return

        # 計算每人完成率
        stats = defaultdict(lambda: {'done': 0, 'total': 0, 'incomplete': []})
        for row in result.data:
            mgr = row['manager']
            if row['status'] == 'not_reported':
                continue
            stats[mgr]['total'] += 1
            if row['status'] == 'done':
                stats[mgr]['done'] += 1

        # 查未完成項目
        incomplete_result = supabase_client.table('daily_reports')\
            .select('manager, item_text, reason')\
            .eq('session', 'evening')\
            .eq('status', 'incomplete')\
            .gte('report_date', week_start)\
            .lte('report_date', week_end)\
            .execute()

        incomplete_by_mgr = defaultdict(list)
        for row in incomplete_result.data:
            incomplete_by_mgr[row['manager']].append(row['item_text'])

        # 組成報告文字
        date_range = f"{week_start[5:]} – {week_end[5:]}"
        lines = [f"📊 上週完成率報告（{date_range}）\n"]

        for mgr in MANAGERS:
            s = stats.get(mgr, {'done': 0, 'total': 0})
            if s['total'] == 0:
                lines.append(f"{mgr}：無資料")
                continue
            rate = int(s['done'] / s['total'] * 100)
            filled = rate // 10
            bar = '█' * filled + '░' * (10 - filled)
            lines.append(f"{mgr}    {bar} {rate}%")

        incomplete_lines = []
        for mgr in MANAGERS:
            items = incomplete_by_mgr.get(mgr, [])
            if items:
                from collections import Counter
                counted = Counter(items)
                for item, cnt in counted.most_common():
                    suffix = f"（出現 {cnt} 次）" if cnt > 1 else ""
                    incomplete_lines.append(f"• {mgr}：{item}{suffix}")

        if incomplete_lines:
            lines.append("\n未完成項目：")
            lines.extend(incomplete_lines)

        lines.append(f"\n詳細紀錄：https://aivy-line-bot.onrender.com/dashboard")
        push(TextMessage(text='\n'.join(lines)))
        logger.info('週報發送完成')

    except Exception as e:
        logger.error(f'週報發送失敗：{e}')

# ── 連續未完成提醒 ───────────────────────────────────────────
def check_overdue_items():
    """檢查連續 3 天以上未完成的項目，提醒 Andy"""
    if not supabase_client:
        return
    try:
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        result = supabase_client.table('daily_reports')\
            .select('report_date, manager, item_text, status')\
            .eq('session', 'evening')\
            .eq('status', 'incomplete')\
            .gte('report_date', seven_days_ago)\
            .execute()

        if not result.data:
            return

        # 整理每個人每個項目的未完成日期
        item_dates = defaultdict(set)
        for row in result.data:
            key = (row['manager'], row['item_text'])
            item_dates[key].add(row['report_date'])

        # 檢查連續天數
        alerts = []
        today = date.today()
        for (manager, item), dates in item_dates.items():
            consecutive = 0
            check = today - timedelta(days=1)  # 從昨天開始往回數
            for _ in range(7):
                if check.isoformat() in dates:
                    consecutive += 1
                    check -= timedelta(days=1)
                else:
                    break
            if consecutive >= 3:
                alerts.append(f"⚠️ {manager}：「{item}」已連續 {consecutive} 天未完成")

        if alerts:
            msg = "📊 連續未完成項目提醒\n\n" + '\n'.join(alerts) + "\n\n建議主動了解最新狀況 🔍"
            push(TextMessage(text=msg))
            logger.info(f'已發送連續未完成提醒：{len(alerts)} 筆')

    except Exception as e:
        logger.error(f'檢查連續未完成失敗：{e}')

# ── 狀態管理 ─────────────────────────────────────────────────
def now_taipei():
    return datetime.now(TZ)

def today_key():
    return now_taipei().date().isoformat()

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def ensure_today(state):
    today = today_key()
    if today not in state:
        state[today] = {
            'morning': {'sent': False, 'summary_sent': False, 'todos': {}},
            'evening': {'sent': False, 'summary_sent': False, 'reports': {}},
        }
    return state, today

# ── 使用者身份對應 ────────────────────────────────────────────
STATIC_USER_MAP = {}
for _mgr, _env in [('Andy', 'LINE_USER_ANDY'), ('小陳', 'LINE_USER_XIAOCHEN'),
                    ('Hank', 'LINE_USER_HANK'), ('小楊', 'LINE_USER_XIAOYANG')]:
    _uid = os.environ.get(_env, '').strip()
    if _uid:
        STATIC_USER_MAP[_uid] = _mgr
        logger.info(f'已從環境變數載入：{_mgr}')

def register_user(user_id, manager):
    state = load_state()
    if '_user_map' not in state:
        state['_user_map'] = {}
    state['_user_map'][user_id] = manager
    save_state(state)

def get_manager_for_user(user_id):
    if user_id in STATIC_USER_MAP:
        return STATIC_USER_MAP[user_id]
    state = load_state()
    return state.get('_user_map', {}).get(user_id)

# ── 早晨狀態 ─────────────────────────────────────────────────
def mark_morning_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning']['sent'] = True
    save_state(state)

def mark_morning_summary_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning']['summary_sent'] = True
    save_state(state)

def store_morning_todos(manager, text):
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning']['todos'][manager] = text
    save_state(state)

def get_morning_todos():
    state = load_state()
    state, today = ensure_today(state)
    return state[today]['morning']['todos']

def is_morning_window():
    now = now_taipei()
    state = load_state()
    state, today = ensure_today(state)
    m = state[today]['morning']
    if m['summary_sent']:
        return False
    # 正常早晨視窗：09:00 提示發出後
    if m['sent']:
        return True
    # 深夜提前登記視窗：00:00 ~ 08:59（跨日後至上班前）
    if 0 <= now.hour <= 8:
        return True
    return False

def is_prenoon_presubmit():
    """判斷是否為深夜提前登記（09:00 提示尚未發出）"""
    now = now_taipei()
    state = load_state()
    state, today = ensure_today(state)
    return 0 <= now.hour <= 8 and not state[today]['morning']['sent']

def get_unreported_morning():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['morning']['sent']:
        return []
    return [m for m in MANAGERS if m not in state[today]['morning']['todos']]

# ── 晚間狀態 ─────────────────────────────────────────────────
def mark_evening_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['evening']['sent'] = True
    save_state(state)

def mark_evening_summary_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['evening']['summary_sent'] = True
    save_state(state)

def store_evening_report(manager, text):
    state = load_state()
    state, today = ensure_today(state)
    state[today]['evening']['reports'][manager] = text
    save_state(state)

def get_evening_reports():
    state = load_state()
    state, today = ensure_today(state)
    return state[today]['evening']['reports']

def get_evening_reports_by_key(date_key):
    """取得指定日期的晚間回報（用於跨日 00:00 彙整）"""
    state = load_state()
    return state.get(date_key, {}).get('evening', {}).get('reports', {})

def mark_evening_summary_sent_by_key(date_key):
    """標記指定日期晚間彙整已發送"""
    state = load_state()
    if date_key not in state:
        state[date_key] = {
            'morning': {'sent': False, 'summary_sent': False, 'todos': {}},
            'evening': {'sent': False, 'summary_sent': False, 'reports': {}},
        }
    state[date_key]['evening']['summary_sent'] = True
    save_state(state)

def is_evening_window():
    state = load_state()
    state, today = ensure_today(state)
    e = state[today]['evening']
    return e['sent'] and not e['summary_sent']

def get_unreported_evening():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['evening']['sent']:
        return []
    return [m for m in MANAGERS if m not in state[today]['evening']['reports']]

# ── Flex Message 建立 ────────────────────────────────────────
def build_morning_summary_flex(todos, carryover=None):
    """carryover: {manager: [(item_text, count), ...]}"""
    today = today_key()
    carryover = carryover or {}
    rows = []
    for mgr in MANAGERS:
        has_todo = mgr in todos
        has_carry = mgr in carryover
        if has_todo or has_carry:
            rows.append({"type": "text", "text": f"✅  {mgr}",
                         "weight": "bold", "size": "sm", "color": "#06C755"})
            # 一般待辦
            if has_todo:
                for line in todos[mgr].strip().split('\n'):
                    if line.strip():
                        rows.append({"type": "text", "text": f"    {line.strip()}",
                                     "size": "sm", "color": "#444444", "wrap": True, "margin": "xs"})
            # 結轉任務
            if has_carry:
                for item_text, count in carryover[mgr]:
                    label = f"    🔁 {item_text}（結轉×{count}）"
                    rows.append({"type": "text", "text": label,
                                 "size": "sm", "color": "#7C3AED", "wrap": True, "margin": "xs"})
        else:
            rows.append({"type": "text", "text": f"❌  {mgr} 尚未回報",
                         "weight": "bold", "size": "sm", "color": "#BBBBBB"})
        rows.append({"type": "separator", "margin": "md"})
    if rows and rows[-1].get("type") == "separator":
        rows.pop()

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A73E8", "paddingAll": "lg",
            "contents": [{"type": "text", "text": f"📋  {today}  今日待辦彙整",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {"type": "box", "layout": "vertical",
                 "paddingAll": "lg", "spacing": "sm", "contents": rows}
    }
    return FlexMessage(alt_text=f"{today} 今日待辦彙整",
                       contents=FlexContainer.from_dict(bubble))

def build_evening_summary_flex(reports, report_date=None):
    today = report_date or today_key()
    rows = []
    for mgr in MANAGERS:
        if mgr in reports:
            rows.append({"type": "text", "text": mgr,
                         "weight": "bold", "size": "sm"})
            for line in reports[mgr].strip().split('\n'):
                if line.strip():
                    color = "#1AAE1A" if line.strip().startswith('✅') else \
                            "#E53935" if line.strip().startswith('❌') else "#444444"
                    rows.append({"type": "text", "text": f"    {line.strip()}",
                                 "size": "sm", "color": color, "wrap": True, "margin": "xs"})
        else:
            rows.append({"type": "text", "text": mgr, "weight": "bold", "size": "sm"})
            rows.append({"type": "text", "text": "    ⏳ 未回報",
                         "size": "sm", "color": "#BBBBBB", "margin": "xs"})
        rows.append({"type": "separator", "margin": "md"})
    if rows and rows[-1].get("type") == "separator":
        rows.pop()

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#5C5CE6", "paddingAll": "lg",
            "contents": [{"type": "text", "text": f"🌙  {today}  今日完成狀況彙整",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {"type": "box", "layout": "vertical",
                 "paddingAll": "lg", "spacing": "sm", "contents": rows}
    }
    return FlexMessage(alt_text=f"{today} 今日完成狀況彙整",
                       contents=FlexContainer.from_dict(bubble))

# ── 推播 / 回覆 ───────────────────────────────────────────────
def push(msg):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=GROUP_ID, messages=[msg])
        )

def reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(reply_token=reply_token,
                                messages=[TextMessage(text=text)])
        )

# ── 排程任務 ─────────────────────────────────────────────────
def send_morning_prompt():
    logger.info('發送早晨待辦回報提示')
    try:
        mark_morning_sent()
        # 帶出昨日未完成結轉
        yesterday_inc = get_yesterday_incomplete()
        carry = ''
        if yesterday_inc:
            lines = ['⚠️ 昨日未完成項目（請列入今日追蹤）：']
            for mgr in MANAGERS:
                for item in yesterday_inc.get(mgr, []):
                    lines.append(f"  • {mgr}：{item}")
            carry = '\n'.join(lines) + '\n\n'

        push(TextMessage(
            text=f"☀️ 早安！請各主管直接在群組輸入今日待辦事項\n\n"
                 f"{carry}"
                 "格式範例：\n1. 盤點配件庫存\n2. 同步官網庫存\n3. 整理展示機台\n\n"
                 "11:00 將自動彙整今日待辦 📋"
        ))
    except Exception as e:
        logger.error(f'早晨提示發送失敗：{e}')

def send_morning_reminder():
    unreported = get_unreported_morning()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日待辦事項，請在 11:00 前輸入！"))
    except Exception as e:
        logger.error(f'早晨提醒失敗：{e}')

def send_morning_summary():
    todos = get_morning_todos()
    today = today_key()
    # 取得需要自動結轉的昨日未完成任務
    carryover = get_carryover_items(today)
    if carryover:
        mgr_list = ', '.join(f"{m}×{len(v)}項" for m, v in carryover.items())
        logger.info(f'自動結轉任務：{mgr_list}')
    try:
        push(build_morning_summary_flex(todos, carryover))
        mark_morning_summary_sent()
        save_morning_to_db(todos, today, carryover)
        logger.info('早晨彙整卡發送並存入資料庫完成')
    except Exception as e:
        logger.error(f'早晨彙整卡失敗：{e}')

def send_evening_prompt():
    logger.info('發送晚間完成狀況回報提示')
    try:
        mark_evening_sent()
        push(TextMessage(
            text="🌙 請各主管直接在群組輸入今日完成狀況\n\n"
                 "格式範例：\n✅ 盤點配件庫存\n✅ 同步官網庫存\n❌ 整理展示機台（客人太多）\n\n"
                 "00:00 將自動彙整完成狀況 📋"
        ))
    except Exception as e:
        logger.error(f'晚間提示發送失敗：{e}')

def send_evening_reminder():
    unreported = get_unreported_evening()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日完成狀況，請在 23:59 前輸入！"))
    except Exception as e:
        logger.error(f'晚間提醒失敗：{e}')

def send_evening_summary():
    # 00:00 執行時台北時間已跨日，彙整的是「昨天」的晚間回報
    now = now_taipei()
    if now.hour == 0:
        report_date = (now.date() - timedelta(days=1)).isoformat()
    else:
        report_date = now.date().isoformat()

    reports = get_evening_reports_by_key(report_date)
    try:
        push(build_evening_summary_flex(reports, report_date))
        mark_evening_summary_sent_by_key(report_date)
        save_evening_to_db(reports, report_date)
        logger.info(f'晚間彙整卡發送完成（回報日期：{report_date}）')
    except Exception as e:
        logger.error(f'晚間彙整卡失敗：{e}')

# ── Webhook ──────────────────────────────────────────────────
@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200

@app.route('/trigger/morning-prompt', methods=['GET'])
def trigger_morning_prompt():
    send_morning_prompt()
    return 'morning prompt sent', 200

@app.route('/trigger/morning-summary', methods=['GET'])
def trigger_morning_summary():
    send_morning_summary()
    return 'morning summary sent', 200

@app.route('/trigger/evening-prompt', methods=['GET'])
def trigger_evening_prompt():
    send_evening_prompt()
    return 'evening prompt sent', 200

@app.route('/trigger/evening-summary', methods=['GET'])
def trigger_evening_summary():
    send_evening_summary()
    return 'evening summary sent', 200

@app.route('/trigger/reset-morning', methods=['GET'])
def trigger_reset_morning():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning'] = {'sent': True, 'summary_sent': False, 'todos': {}}
    save_state(state)
    return 'morning reset', 200

@app.route('/trigger/check-overdue', methods=['GET'])
def trigger_check_overdue():
    check_overdue_items()
    return 'overdue check done', 200

@app.route('/trigger/check-missing', methods=['GET'])
def trigger_check_missing():
    check_missing_reports()
    return 'missing check done', 200

@app.route('/trigger/monthly-report', methods=['GET'])
def trigger_monthly_report():
    send_monthly_report()
    return 'monthly report sent', 200

@app.route('/trigger/weekly-report', methods=['GET'])
def trigger_weekly_report():
    send_weekly_report()
    return 'weekly report sent', 200

@app.route('/dashboard', methods=['GET'])
def dashboard():
    if not supabase_client:
        return '<h2>Supabase 未連線</h2>', 500

    import json as _json

    # ── 日期範圍解析 ─────────────────────────────────────────
    today = date.today()
    range_param   = request.args.get('range', '7')
    from_param    = request.args.get('from', '')
    to_param      = request.args.get('to', '')

    if from_param and to_param:
        try:
            date_from = date.fromisoformat(from_param)
            date_to   = date.fromisoformat(to_param)
            if date_from > date_to:
                date_from, date_to = date_to, date_from
            active_range = 'custom'
        except ValueError:
            date_from    = today - timedelta(days=7)
            date_to      = today
            active_range = '7'
    else:
        try:
            days = int(range_param)
        except ValueError:
            days = 7
        days         = days if days in (7, 14, 30) else 7
        date_from    = today - timedelta(days=days - 1)
        date_to      = today
        active_range = str(days)

    since_iso = date_from.isoformat()
    until_iso = date_to.isoformat()
    span_days  = (date_to - date_from).days + 1

    result = supabase_client.table('daily_reports')\
        .select('report_date, manager, session, item_text, status, reason')\
        .gte('report_date', since_iso)\
        .lte('report_date', until_iso)\
        .order('report_date', desc=True)\
        .order('manager')\
        .execute()

    rows = result.data or []

    # ── 完成率統計（選取區間） ────────────────────────────────
    mgr_stats = {m: {'done': 0, 'total': 0} for m in MANAGERS}
    for row in rows:
        if row['session'] != 'evening' or row['status'] == 'not_reported':
            continue
        m = row['manager']
        if m in mgr_stats:
            mgr_stats[m]['total'] += 1
            if row['status'] == 'done':
                mgr_stats[m]['done'] += 1

    # 趨勢比較：取同等長度的前一段
    prev_to   = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=span_days - 1)
    try:
        last_result = supabase_client.table('daily_reports')\
            .select('manager, status')\
            .eq('session', 'evening')\
            .gte('report_date', prev_from.isoformat())\
            .lte('report_date', prev_to.isoformat())\
            .execute()
        last_stats = {m: {'done': 0, 'total': 0} for m in MANAGERS}
        for row in last_result.data:
            if row['status'] == 'not_reported':
                continue
            m = row['manager']
            if m in last_stats:
                last_stats[m]['total'] += 1
                if row['status'] == 'done':
                    last_stats[m]['done'] += 1
    except Exception:
        last_stats = {m: {'done': 0, 'total': 0} for m in MANAGERS}

    def trend_arrow(this_rate, last_s):
        if last_s['total'] == 0:
            return '', '#888'
        last_rate = int(last_s['done'] / last_s['total'] * 100)
        diff = this_rate - last_rate
        if diff > 5:   return '↑', '#1AAE1A'
        if diff < -5:  return '↓', '#E53935'
        return '→', '#888'

    rate_cards = ''
    for mgr in MANAGERS:
        s = mgr_stats[mgr]
        if s['total'] == 0:
            rate, bar_w, color, label = 0, 0, '#ccc', '無資料'
            arrow, a_color = '', '#888'
        else:
            rate = int(s['done'] / s['total'] * 100)
            bar_w = rate
            color = '#1AAE1A' if rate >= 80 else '#FF9800' if rate >= 60 else '#E53935'
            label = f"{s['done']}/{s['total']} 件完成"
            arrow, a_color = trend_arrow(rate, last_stats[mgr])
        rate_cards += f'''
        <div class="rate-card">
          <div class="rate-name">{mgr} <span style="color:{a_color};font-size:1.1em">{arrow}</span></div>
          <div class="rate-num" style="color:{color}">{rate}%</div>
          <div class="rate-sub">{label}</div>
          <div class="bar-wrap"><div class="bar-fill" style="width:{bar_w}%;background:{color}"></div></div>
        </div>'''

    # ── 未完成原因統計 ────────────────────────────────────────
    incomplete_items = [r for r in rows if r['session'] == 'evening' and r['status'] == 'incomplete']
    incomplete_items.sort(key=lambda x: (x['manager'], x['report_date']))

    reason_rows = ''
    for row in incomplete_items:
        reason = row.get('reason', '') or '（未說明）'
        reason_rows += f'''<tr>
          <td style="font-weight:bold;white-space:nowrap">{row["manager"]}</td>
          <td style="white-space:nowrap;color:#888">{row["report_date"][5:]}</td>
          <td>{row["item_text"]}</td>
          <td style="color:#888">{reason}</td>
        </tr>'''
    if not reason_rows:
        reason_rows = '<tr><td colspan="4" style="text-align:center;color:#aaa;padding:20px">此區間無未完成紀錄 🎉</td></tr>'

    # ── 每日明細 ─────────────────────────────────────────────
    from collections import OrderedDict
    days_data = OrderedDict()
    for row in rows:
        d, s, m = row['report_date'], row['session'], row['manager']
        days_data.setdefault(d, {'morning': defaultdict(list), 'evening': defaultdict(list)})
        days_data[d][s][m].append(row)

    STATUS_COLOR = {'done':'#1AAE1A','incomplete':'#E53935','reported':'#FF9800','not_reported':'#BBBBBB'}
    STATUS_LABEL = {'done':'✅','incomplete':'❌','reported':'📋','not_reported':'⏳'}

    detail_rows = ''
    for day, sessions in days_data.items():
        for mgr in MANAGERS:
            # 優先顯示晚報（結果），無晚報才退回早報（待確認）
            evening_items = sessions['evening'].get(mgr, [])
            morning_items = sessions['morning'].get(mgr, [])
            items = evening_items if evening_items else morning_items
            if not items:
                continue
            # 若只有早報資料，標示「待確認」提示色
            is_pending = not evening_items and bool(morning_items)
            for i, item in enumerate(items):
                color  = STATUS_COLOR.get(item['status'], '#888')
                emoji  = STATUS_LABEL.get(item['status'], '')
                # 結轉標記：morning reason 欄位以 '結轉×' 開頭
                raw_reason = item.get('reason', '') or ''
                is_carryover = raw_reason.startswith('結轉×')
                if is_carryover:
                    try:
                        co_count = int(raw_reason.replace('結轉×', '').strip())
                    except ValueError:
                        co_count = 1
                    co_tag = f' <span style="font-size:11px;background:#ede9fe;color:#7C3AED;padding:1px 6px;border-radius:8px;font-weight:600">🔁 結轉×{co_count}</span>'
                    reason = ''
                else:
                    co_tag = ''
                    reason = f'<br><small style="color:#aaa">（{raw_reason}）</small>' if raw_reason else ''
                pending_hint = ' <small style="color:#bbb;font-size:11px">待晚報</small>' if is_pending and i == 0 else ''
                rs = len(items)
                detail_rows += f'''<tr>
                  {"<td rowspan='" + str(rs) + "' style='font-weight:bold;color:#555;white-space:nowrap'>" + day[5:] + "</td>" if i==0 else ""}
                  {"<td rowspan='" + str(rs) + "' style='font-weight:bold'>" + mgr + pending_hint + "</td>" if i==0 else ""}
                  <td>{item["item_text"]}{co_tag}{reason}</td>
                  <td style="color:{color};font-weight:bold;text-align:center">{emoji}</td>
                </tr>'''

    # ── ① 今日快照 ────────────────────────────────────────────
    today_result = supabase_client.table('daily_reports')\
        .select('manager, session, item_text, status')\
        .eq('report_date', today.isoformat()).execute()
    today_rows = today_result.data or []

    snap_cards = ''
    for mgr in MANAGERS:
        mgr_today = [r for r in today_rows if r['manager'] == mgr]
        evening = [r for r in mgr_today if r['session'] == 'evening']
        morning = [r for r in mgr_today if r['session'] == 'morning']
        eve_done  = sum(1 for r in evening if r['status'] == 'done')
        eve_total = sum(1 for r in evening if r['status'] in ('done','incomplete'))
        if evening and any(r['status'] in ('done','incomplete') for r in evening):
            snap_cls, icon, sub = 'done', '✅', f'晚報已回報・{eve_done}/{eve_total} 完成'
        elif morning and any(r['status'] == 'reported' for r in morning):
            snap_cls, icon, sub = 'pend', '📋', '早報已登記・待晚報'
        elif any(r['status'] == 'done' and r['item_text'] == '休假' for r in mgr_today):
            snap_cls, icon, sub = 'none', '🏖️', '今日休假'
        else:
            snap_cls, icon, sub = 'none', '⏳', '尚未回報'
        snap_cards += f'''
        <div class="snap-card {snap_cls}">
          <div class="snap-icon-wrap">{icon}</div>
          <div><div class="snap-name">{mgr}</div><div class="snap-sub">{sub}</div></div>
        </div>'''

    # ── ② 重複未完成追蹤（查最近 30 天，同任務出現 2 次以上）──
    thirty_ago = (today - timedelta(days=30)).isoformat()
    overdue_result = supabase_client.table('daily_reports')\
        .select('report_date, manager, item_text, status, reason')\
        .eq('session', 'evening').eq('status', 'incomplete')\
        .gte('report_date', thirty_ago)\
        .order('report_date', desc=False).execute()
    overdue_rows = overdue_result.data or []

    item_dates = defaultdict(list)
    item_reasons = defaultdict(list)
    for r in overdue_rows:
        key = (r['manager'], r['item_text'])
        item_dates[key].append(r['report_date'])
        if r.get('reason'):
            item_reasons[key].append(r['reason'])

    repeat_items = []
    for (mgr, item), dates in item_dates.items():
        sorted_dates = sorted(set(dates))
        count = len(sorted_dates)
        if count >= 2:
            latest_reason = item_reasons[(mgr, item)][-1] if item_reasons[(mgr, item)] else ''
            repeat_items.append((count, mgr, item, sorted_dates[0], sorted_dates[-1], latest_reason))
    repeat_items.sort(reverse=True)

    overdue_html = ''
    for count, mgr, item, first_date, last_date, reason in repeat_items[:10]:
        color = '#dc2626' if count >= 4 else '#ea580c' if count >= 3 else '#2563eb'
        bg    = '#fef2f2' if count >= 4 else '#fff7ed' if count >= 3 else '#eff6ff'
        warn  = '⚠️ 建議主動了解，已多次未完成' if count >= 4 else '已重複未完成，注意追蹤' if count >= 3 else '出現 2 次，留意後續'
        reason_tag = f'<div style="font-size:.73em;color:#aaa;margin-top:2px">最近原因：{reason}</div>' if reason else ''
        overdue_html += f'''
        <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;
                    border-radius:10px;border-left:4px solid {color};background:{bg};margin-bottom:8px">
          <span style="background:{color};color:#fff;border-radius:99px;
                       padding:3px 12px;font-size:.75em;font-weight:700;white-space:nowrap">{count} 次</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:.88em">{item}</div>
            <div style="font-size:.75em;color:#888;margin-top:2px">{mgr}・{first_date[5:]} ～ {last_date[5:]}</div>
            <div style="font-size:.75em;color:{color};margin-top:2px">{warn}</div>
            {reason_tag}
          </div>
        </div>'''
    if not overdue_html:
        overdue_html = '<div style="text-align:center;padding:20px;color:#aaa;font-size:.85em">近30天無重複未完成項目 🎉</div>'

    # ── ③ 完成率趨勢折線圖（依區間每天計算）─────────────────
    daily_rates = {mgr: {} for mgr in MANAGERS}
    rows_by_date = defaultdict(list)
    for r in rows:
        rows_by_date[r['report_date']].append(r)

    chart_labels = []
    cur = date_from
    while cur <= date_to:
        ds = cur.isoformat()
        chart_labels.append(ds[5:])
        day_rows = rows_by_date.get(ds, [])
        for mgr in MANAGERS:
            mgr_rows = [r for r in day_rows if r['manager'] == mgr and r['session'] == 'evening'
                        and r['status'] in ('done','incomplete')]
            if mgr_rows:
                r_done = sum(1 for r in mgr_rows if r['status'] == 'done')
                daily_rates[mgr][ds] = round(r_done / len(mgr_rows) * 100)
            else:
                daily_rates[mgr][ds] = None
        cur += timedelta(days=1)

    chart_colors = {'Andy':'#1A73E8','小陳':'#E53935','Hank':'#1AAE1A','小楊':'#FF9800'}
    datasets_js = []
    for mgr in MANAGERS:
        pts = [daily_rates[mgr].get(d) for d in [
            (date_from + timedelta(days=i)).isoformat() for i in range(span_days)]]
        datasets_js.append(f'''{{
          label:'{mgr}',data:{_json.dumps(pts)},
          borderColor:'{chart_colors[mgr]}',backgroundColor:'{chart_colors[mgr]}22',
          tension:.4,pointRadius:3,spanGaps:true,fill:false
        }}''')
    chart_data_js = f'{{labels:{_json.dumps(chart_labels)},datasets:[{",".join(datasets_js)}]}}'

    # ── ④ 未完成原因分類 ──────────────────────────────────────
    cat_map = [
        ('🔥','現場突發',  '#dc2626', '#fef2f2', '#fecaca', ['現場','門市','忙','客','插曲','交機','業務']),
        ('⏰','時間不夠',  '#ea580c', '#fff7ed', '#fed7aa', ['時間','太久','壓縮','來不及','沒時間','佔用','忙到']),
        ('🧠','個人因素',  '#7c3aed', '#faf5ff', '#ddd6fe', ['靈感','心態','腦袋','個人','沒有','沒辦法','思考']),
        ('🔗','等待外部',  '#2563eb', '#eff6ff', '#bfdbfe', ['等','待','未','放鳥','尚未','廠商','回覆','入帳']),
    ]
    # cat_items[i] = [(manager, report_date, item_text, reason), ...]
    cat_items = [[] for _ in cat_map]
    for row in incomplete_items:
        reason = row.get('reason', '') or ''
        matched = False
        for i, (_, _, _, _, _, kws) in enumerate(cat_map):
            if any(kw in reason for kw in kws):
                cat_items[i].append(row)
                matched = True
                break
        if not matched:
            cat_items[0].append(row)  # 歸入現場突發
    cat_counts = [len(c) for c in cat_items]
    total_reasons = sum(cat_counts) or 1

    reason_cats_html = ''
    max_c = max(cat_counts) or 1
    for i, (icon, name, color, bg_light, border_light, _) in enumerate(cat_map):
        c = cat_counts[i]
        pct = round(c / total_reasons * 100)
        bar_w = round(c / max_c * 100)

        # 人名 chips：統計每人次數
        mgr_count = defaultdict(int)
        for row in cat_items[i]:
            mgr_count[row['manager']] += 1
        chips_html = ''.join(
            f'<span class="chip" style="background:{bg_light};color:{color};border-color:{border_light}">'
            f'{mgr} ×{cnt}</span>'
            for mgr, cnt in sorted(mgr_count.items(), key=lambda x: -x[1])
        ) if mgr_count else '<span style="font-size:11px;color:#ccc">—</span>'

        # 展開明細
        detail_html = ''
        for row in sorted(cat_items[i], key=lambda x: (x['manager'], x['report_date'])):
            dt = row['report_date'][5:]  # MM-DD
            task = row.get('item_text', '')
            rsn  = row.get('reason', '') or '（未說明）'
            detail_html += (
                f'<tr>'
                f'<td style="white-space:nowrap">{row["manager"]}</td>'
                f'<td style="white-space:nowrap">{dt}</td>'
                f'<td>{task}</td>'
                f'<td>{rsn}</td>'
                f'</tr>'
            )

        details_block = ''
        if detail_html:
            details_block = f'''
            <details>
              <summary style="color:{color}">▶ 展開明細（{c} 筆）</summary>
              <table class="det-table">
                <tbody>{detail_html}</tbody>
              </table>
            </details>'''

        reason_cats_html += f'''
        <div class="cat-card">
          <div class="cat-top">
            <div class="cat-icon" style="background:{bg_light}">{icon}</div>
            <div><div class="cat-label">{name}</div><div class="cat-pct">{pct}%</div></div>
            <div class="cat-num" style="color:{color}">{c}</div>
          </div>
          <div class="bar-wrap" style="margin-bottom:10px">
            <div class="bar-fill" style="width:{bar_w}%;background:{color}"></div>
          </div>
          <div>{chips_html}</div>
          <div class="divider"></div>
          {details_block}
        </div>'''

    # ── ⑤ 每人平均每日任務量 ─────────────────────────────────
    taskload_html = ''
    for mgr in MANAGERS:
        mgr_eve = [r for r in rows if r['manager'] == mgr and r['session'] == 'evening'
                   and r['status'] in ('done','incomplete')]
        active_days = len(set(r['report_date'] for r in mgr_eve)) or 1
        avg = round(len(mgr_eve) / active_days, 1)
        done_cnt = sum(1 for r in mgr_eve if r['status'] == 'done')
        rate = round(done_cnt / len(mgr_eve) * 100) if mgr_eve else 0
        bar_color = '#1AAE1A' if rate >= 80 else '#FF9800' if rate >= 60 else '#E53935'
        bar_w = min(int(avg / 5 * 100), 100)
        taskload_html += f'''
        <div style="border:1px solid #e8eaed;border-radius:10px;padding:16px;text-align:center;background:#fff">
          <div style="font-size:12px;color:#9ca3af;font-weight:600;margin-bottom:8px">{mgr}</div>
          <div style="font-size:1.9em;font-weight:800;color:#2563eb;letter-spacing:-1px">{avg}</div>
          <div style="font-size:11px;color:#9ca3af;margin-top:2px">件 / 天</div>
          <div style="height:5px;background:#f5f6fa;border-radius:99px;margin-top:10px;overflow:hidden">
            <div style="height:5px;border-radius:99px;background:{bar_color};width:{bar_w}%"></div>
          </div>
          <div style="font-size:11px;color:#9ca3af;margin-top:6px">完成率 {rate}%</div>
        </div>'''

    # ── ⑥ 回報率（有回報的天數 / 區間總天數）────────────────
    punct_html = ''
    sorted_mgrs = []
    for mgr in MANAGERS:
        reported_days = len(set(
            r['report_date'] for r in rows
            if r['manager'] == mgr and r['session'] == 'evening'
            and r['status'] in ('done','incomplete')
        ))
        pct = round(reported_days / span_days * 100)
        sorted_mgrs.append((pct, mgr, reported_days))
    sorted_mgrs.sort(reverse=True)

    for pct, mgr, rdays in sorted_mgrs:
        bar_color = '#1AAE1A' if pct >= 80 else '#FF9800' if pct >= 60 else '#E53935'
        if pct >= 90:   label, lc = '🏆 最積極', '#1AAE1A'
        elif pct >= 75: label, lc = '準時', '#888'
        elif pct >= 60: label, lc = '偶爾缺報', '#FF9800'
        else:           label, lc = '⚠️ 常缺報', '#E53935'
        punct_html += f'''
        <div style="display:flex;align-items:center;gap:12px;padding:10px 16px;
                    background:#f5f6fa;border-radius:10px;margin-bottom:6px">
          <div style="font-weight:700;width:44px;font-size:13px;color:#1a1d23">{mgr}</div>
          <div style="flex:1;height:6px;background:#e8eaed;border-radius:99px;overflow:hidden">
            <div style="height:6px;border-radius:99px;background:{bar_color};width:{pct}%"></div>
          </div>
          <div style="width:40px;text-align:right;font-weight:700;font-size:13px;color:{bar_color}">{pct}%</div>
          <div style="width:80px;text-align:right;font-size:11px;color:{lc}">{label}</div>
        </div>'''

    # ── 時間軸選擇器 ─────────────────────────────────────────
    range_label_map = {'7':'近 7 天', '14':'近 14 天', '30':'近 30 天', 'custom':'自訂'}
    current_label   = range_label_map.get(active_range, '自訂')
    date_from_str   = date_from.isoformat()
    date_to_str     = date_to.isoformat()

    btn = lambda r, label: (
        f'<a href="/dashboard?range={r}" class="range-btn active">{label}</a>'
        if active_range == r else
        f'<a href="/dashboard?range={r}" class="range-btn">{label}</a>'
    )

    html = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>艾薇 回報系統儀表板</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg:     #f5f6fa;
      --white:  #ffffff;
      --border: #e8eaed;
      --text:   #1a1d23;
      --text2:  #4b5563;
      --gray:   #9ca3af;
      --accent: #2563eb;
      --green:  #16a34a;
      --red:    #dc2626;
      --orange: #ea580c;
      --purple: #7c3aed;
      --shadow: 0 1px 4px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text)}}

    /* ── Header ── */
    .header{{background:var(--white);border-bottom:1px solid var(--border);
             padding:0 28px;height:56px;display:flex;align-items:center;gap:16px;
             position:sticky;top:0;z-index:100}}
    .logo{{font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.3px}}
    .logo em{{color:var(--accent);font-style:normal}}
    .live-badge{{display:flex;align-items:center;gap:5px;background:#f0fdf4;
                 border:1px solid #bbf7d0;color:var(--green);padding:3px 10px;
                 border-radius:20px;font-size:11px;font-weight:700}}
    .live-dot{{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 1.6s infinite}}
    @keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .header-right{{margin-left:auto;font-size:12px;color:var(--gray)}}

    /* ── Time bar ── */
    .time-bar{{background:var(--white);border-bottom:1px solid var(--border);
               padding:0 28px;height:44px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .time-label{{font-size:12px;color:var(--gray);margin-right:6px;font-weight:500}}
    .range-btn{{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:600;
                cursor:pointer;border:1px solid var(--border);background:transparent;
                color:var(--text2);text-decoration:none;transition:all .15s;white-space:nowrap}}
    .range-btn:hover{{background:var(--bg);border-color:#d1d5db}}
    .range-btn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
    .custom-form{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .custom-form input[type=date]{{padding:4px 10px;border:1px solid var(--border);border-radius:6px;
      font-size:12px;color:var(--text);background:var(--bg);outline:none;cursor:pointer}}
    .custom-form input[type=date]:focus{{border-color:var(--accent)}}
    .custom-form button{{padding:5px 14px;border-radius:6px;border:none;
      background:var(--accent);color:#fff;font-size:12px;font-weight:700;cursor:pointer}}
    .divider-v{{width:1px;height:20px;background:var(--border);margin:0 4px}}

    /* ── Layout ── */
    .container{{max-width:1080px;margin:0 auto;padding:28px 20px}}

    /* ── Section header ── */
    .sec-hd{{margin:28px 0 12px}}
    .sec-title{{font-size:15px;font-weight:700;color:var(--text);display:flex;align-items:center;gap:8px;margin-bottom:3px}}
    .sec-icon{{width:28px;height:28px;border-radius:7px;display:flex;align-items:center;
               justify-content:center;font-size:14px;flex-shrink:0}}
    .sec-desc{{font-size:12px;color:var(--gray);margin-left:36px}}

    /* ── Card ── */
    .card{{background:var(--white);border:1px solid var(--border);border-radius:12px;
           box-shadow:var(--shadow);margin-bottom:24px;overflow:hidden}}
    .card-body{{padding:20px}}

    /* ── 今日快照 ── */
    .snap-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
    .snap-card{{border-radius:10px;padding:16px 14px;border:1.5px solid var(--border);
                display:flex;align-items:center;gap:12px;transition:box-shadow .15s}}
    .snap-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.08)}}
    .snap-icon-wrap{{width:40px;height:40px;border-radius:10px;display:flex;align-items:center;
                     justify-content:center;font-size:18px;flex-shrink:0}}
    .snap-card.done{{border-color:#bbf7d0}}
    .snap-card.done .snap-icon-wrap{{background:#f0fdf4}}
    .snap-card.pend{{border-color:#fed7aa}}
    .snap-card.pend .snap-icon-wrap{{background:#fff7ed}}
    .snap-card.none{{border-color:var(--border)}}
    .snap-card.none .snap-icon-wrap{{background:var(--bg)}}
    .snap-name{{font-size:13px;font-weight:700;color:var(--text)}}
    .snap-sub{{font-size:11px;color:var(--gray);margin-top:2px}}

    /* ── 完成率 ── */
    .rate-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}}
    .rate-card{{border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center;background:var(--white)}}
    .rate-name{{font-size:12px;color:var(--gray);font-weight:600;margin-bottom:8px}}
    .rate-num{{font-size:2em;font-weight:800;letter-spacing:-1px;margin-bottom:4px}}
    .rate-sub{{font-size:11px;color:var(--gray);margin-bottom:10px}}
    .bar-wrap{{height:5px;background:var(--bg);border-radius:99px;overflow:hidden}}
    .bar-fill{{height:5px;border-radius:99px}}

    /* ── 原因分類 ── */
    .cat-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
    .cat-card{{border:1px solid var(--border);border-radius:10px;padding:16px;background:var(--white);transition:box-shadow .15s}}
    .cat-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.07)}}
    .cat-top{{display:flex;align-items:center;gap:10px;margin-bottom:12px}}
    .cat-icon{{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}}
    .cat-label{{font-size:13px;font-weight:700;color:var(--text)}}
    .cat-pct{{font-size:11px;color:var(--gray)}}
    .cat-num{{margin-left:auto;font-size:1.6em;font-weight:800}}
    .chip{{display:inline-flex;align-items:center;padding:3px 9px;border-radius:20px;
           font-size:11px;font-weight:700;margin:2px 2px 6px 0;border:1px solid}}
    details summary{{font-size:11px;font-weight:600;cursor:pointer;padding:4px 0;
                     list-style:none;user-select:none}}
    details summary::-webkit-details-marker{{display:none}}
    .det-table{{width:100%;border-collapse:collapse;margin-top:8px;font-size:11px}}
    .det-table td{{padding:5px 6px;border-bottom:1px solid var(--border);color:var(--text2);vertical-align:top}}
    .det-table td:first-child{{font-weight:600;white-space:nowrap}}
    .det-table tr:last-child td{{border:none}}

    /* ── Table ── */
    table{{width:100%;border-collapse:collapse}}
    th{{background:var(--bg);color:var(--gray);font-size:11px;font-weight:700;
        text-transform:uppercase;letter-spacing:.5px;padding:10px 16px;
        text-align:left;border-bottom:1px solid var(--border)}}
    td{{padding:10px 16px;font-size:13px;border-bottom:1px solid var(--border);color:var(--text2);vertical-align:top}}
    tr:last-child td{{border:none}}
    tr:hover td{{background:#fafbff}}
    .empty{{text-align:center;padding:30px;color:var(--gray);font-size:13px}}

    /* ── 分隔線 ── */
    .divider{{height:1px;background:var(--border);margin:4px 0 12px}}

    /* ── 兩欄 ── */
    .two-col{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:0}}

    @media(max-width:700px){{
      .snap-grid,.rate-grid{{grid-template-columns:1fr 1fr}}
      .cat-grid,.two-col{{grid-template-columns:1fr}}
    }}
    @media(max-width:480px){{
      .snap-grid,.rate-grid{{grid-template-columns:1fr}}
    }}
  </style>
</head>
<body>

  <!-- Header -->
  <div class="header">
    <div class="logo"><em>艾薇</em> 回報系統儀表板</div>
    <div class="live-badge"><div class="live-dot"></div> LIVE</div>
    <div class="header-right">{today.isoformat()}　艾薇通訊科技有限公司</div>
  </div>

  <!-- Time bar -->
  <div class="time-bar">
    <span class="time-label">時間範圍</span>
    {btn('7',  '近 7 天')}
    {btn('14', '近 14 天')}
    {btn('30', '近 30 天')}
    <div class="divider-v"></div>
    <form class="custom-form" method="get" action="/dashboard">
      <input type="date" name="from" value="{date_from_str}" max="{today.isoformat()}">
      <span style="color:var(--gray);font-size:12px">～</span>
      <input type="date" name="to" value="{date_to_str}" max="{today.isoformat()}">
      <button type="submit">{'✔ 套用' if active_range == 'custom' else '自訂'}</button>
    </form>
    <span style="margin-left:auto;font-size:12px;color:var(--gray)">{since_iso[5:]} ～ {until_iso[5:]}　共 {span_days} 天</span>
  </div>

  <div class="container">

    <!-- ① 今日快照 -->
    <div class="sec-hd" style="margin-top:0">
      <div class="sec-title"><span class="sec-icon" style="background:#eff6ff">🗓</span>今日快照</div>
      <div class="sec-desc">即時顯示 4 位主管今日回報狀態與完成件數，不用往下滾動</div>
    </div>
    <div class="card card-body">
      <div class="snap-grid">{snap_cards}</div>
    </div>

    <!-- 完成率卡片 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#f0fdf4">📊</span>完成率（{current_label}）</div>
      <div class="sec-desc">區間內晚報完成件數比例，括號為實際件數</div>
    </div>
    <div class="rate-grid">{rate_cards}</div>

    <!-- ③ 完成率趨勢折線圖 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#eff6ff">📈</span>完成率趨勢</div>
      <div class="sec-desc">每人完成率走勢，一眼看出誰在退步、誰在進步，適合月中月底對焦</div>
    </div>
    <div class="card card-body">
      <div style="position:relative;height:160px">
        <canvas id="trendChart"></canvas>
      </div>
    </div>

    <!-- ② 重複未完成追蹤 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#fff7ed">🔁</span>重複未完成追蹤（近 30 天）</div>
      <div class="sec-desc">同一任務出現 2 次以上未完成即列出，快速找到需要追蹤的人</div>
    </div>
    <div class="card card-body">{overdue_html}</div>

    <!-- 未完成原因表 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#fef2f2">❌</span>未完成原因統計</div>
      <div class="sec-desc">區間內所有未完成任務的原因明細</div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>姓名</th><th>日期</th><th>任務</th><th>未完成原因</th></tr></thead>
        <tbody>{reason_rows}</tbody>
      </table>
    </div>

    <!-- ④ 原因分類分析 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#faf5ff">🗂</span>原因分類分析</div>
      <div class="sec-desc">自動歸類未完成原因，點「展開明細」可看具體任務與說明</div>
    </div>
    <div class="card card-body">
      <div class="cat-grid">{reason_cats_html}</div>
    </div>

    <!-- ⑤ 任務量 + ⑥ 回報率 -->
    <div class="two-col">
      <div>
        <div class="sec-hd">
          <div class="sec-title"><span class="sec-icon" style="background:#eff6ff">📦</span>平均每日任務量</div>
          <div class="sec-desc">看誰每天計畫太少或太多（完不成）</div>
        </div>
        <div class="card card-body">
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px">
            {taskload_html}
          </div>
        </div>
      </div>
      <div>
        <div class="sec-hd">
          <div class="sec-title"><span class="sec-icon" style="background:#f0fdf4">📬</span>晚報回報率</div>
          <div class="sec-desc">統計每人晚間準時回報的天數比例</div>
        </div>
        <div class="card card-body">{punct_html}
          <div style="font-size:11px;color:var(--gray);text-align:center;margin-top:8px">
            統計有回報的天數 / 區間總天數
          </div>
        </div>
      </div>
    </div>

    <!-- 每日明細 -->
    <div class="sec-hd">
      <div class="sec-title"><span class="sec-icon" style="background:#eff6ff">📅</span>每日回報明細</div>
      <div class="sec-desc">以晚報結果為主顯示，🔁 標記代表自動結轉的跨日追蹤任務</div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>日期</th><th>姓名</th><th>項目</th><th style="text-align:center">狀態</th></tr></thead>
        <tbody>{"".join(detail_rows) if detail_rows else '<tr><td colspan="4" class="empty">尚無資料</td></tr>'}</tbody>
      </table>
    </div>

  </div>
  <script>
  new Chart(document.getElementById('trendChart'),{{
    type:'line',
    data:{chart_data_js},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{position:'top',labels:{{font:{{size:12}},padding:14,color:'#4b5563'}}}},
               tooltip:{{callbacks:{{label:c=>` ${{c.dataset.label}}：${{c.raw}}%`}}}}}},
      scales:{{
        y:{{min:0,max:100,ticks:{{callback:v=>v+'%',font:{{size:11}},color:'#9ca3af'}},grid:{{color:'#f3f4f6'}}}},
        x:{{ticks:{{font:{{size:11}},color:'#9ca3af'}},grid:{{display:false}}}}
      }}
    }}
  }});
  </script>
</body>
</html>'''
    return html

@app.route('/sales-dashboard', methods=['GET'])
def sales_dashboard():
    import csv, io, json as _json
    from collections import defaultdict

    # ── 讀取主資料庫 CSV ──────────────────────────────────────────
    try:
        import requests as _req
        SHEET_ID = '11Qr4pn4J5zGUtd2EPLXbi00FpWZ59LHI9KAn0yChxjg'
        CSV_URL = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv'
        resp = _req.get(CSV_URL, timeout=15)
        resp.encoding = 'utf-8-sig'
        all_csv = list(csv.reader(io.StringIO(resp.text)))
        # skip header row, parse data rows
        data_rows = []
        for row in all_csv[1:]:
            if len(row) < 16: continue
            data_rows.append(row)
        data_ok = True
    except Exception as _e:
        logger.error(f'sales_dashboard CSV error: {_e}')
        data_rows = []
        data_ok = False

    def num(s):
        try: return float(s.replace(',','').replace('NT$','').replace('%','').strip())
        except Exception: return 0.0

    from datetime import datetime as _dt, date as _date

    # ── 日期區間篩選 ───────────────────────────────────────────────
    # columns: 月份(0) 入庫日期(1) 品牌(2) 型號(3) 編號(4) 容量(5) 顏色(6)
    #          收購(7) IMEI(8) 備註(9) 銷售日(10) 售價(11) 利潤(12)
    #          毛利率%(13) 客戶備註(14) 狀態(15)
    ALL_MONTHS = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月']
    today_d      = _date.today()
    current_mo   = f'{today_d.month}月'
    range_param  = request.args.get('range', 'year')

    sold_all  = [r for r in data_rows if r[15].strip() == '已售出']
    stock     = [r for r in data_rows if r[15].strip() != '已售出']

    if range_param == 'month':
        sold = [r for r in sold_all if r[0].strip() == current_mo]
        range_label = f'本月（{current_mo}）'
    elif range_param in ALL_MONTHS:
        sold = [r for r in sold_all if r[0].strip() == range_param]
        range_label = range_param
    else:  # 'year' or default
        sold = sold_all
        range_param = 'year'
        range_label = '今年度'

    # 各月份按鈕：只顯示有資料的月份
    existing_months = sorted({r[0].strip() for r in sold_all if r[0].strip() in ALL_MONTHS},
                              key=lambda m: ALL_MONTHS.index(m))

    def rbtn(rng, label):
        active = 'active' if range_param == rng else ''
        return f'<a href="/sales-dashboard?range={rng}" class="range-btn {active}">{label}</a>'

    month_btns = ''.join(rbtn(m, m) for m in existing_months)
    time_bar_html = f'''
    <div class="time-bar">
      <span class="time-label">區間</span>
      {rbtn("year","今年度")}
      {rbtn("month",f"本月（{current_mo}）")}
      <div class="divider-v"></div>
      {month_btns}
    </div>'''

    total_qty    = len(sold)
    total_stock  = len(stock)
    total_rev    = sum(num(r[11]) for r in sold)
    total_profit = sum(num(r[12]) for r in sold)
    margin_pct   = (total_profit / total_rev * 100) if total_rev else 0
    avg_profit   = (total_profit / total_qty) if total_qty else 0

    # ── 平均在庫天數（已售出：入庫→銷售；庫存中：入庫→今天）────
    def days_held(r):
        try:
            d_in = _dt.strptime(r[1].strip(), '%Y-%m-%d')
            d_out_str = r[10].strip()
            d_out = _dt.strptime(d_out_str, '%Y-%m-%d') if d_out_str else _dt.today()
            return max((d_out - d_in).days, 0)
        except Exception:
            return None

    sold_days  = [d for r in sold  if (d := days_held(r)) is not None]
    stock_days = [d for r in stock if (d := days_held(r)) is not None]
    avg_days_sold  = (sum(sold_days)  / len(sold_days))  if sold_days  else 0
    avg_days_stock = (sum(stock_days) / len(stock_days)) if stock_days else 0

    # ── 庫存總成本 ─────────────────────────────────────────────────
    stock_cost = sum(num(r[7]) for r in stock)

    # ── 品牌平均在庫天數 & 績效表格 ──────────────────────────────
    b_days   = defaultdict(list)
    b_profit = defaultdict(float)
    b_rev    = defaultdict(float)
    b_qty    = defaultdict(int)
    for r in sold:
        b = r[2].strip()
        d = days_held(r)
        if d is not None: b_days[b].append(d)
        b_profit[b] += num(r[12])
        b_rev[b]    += num(r[11])
        b_qty[b]    += 1

    brand_day_labels = []
    brand_day_vals   = []
    for b, ds in sorted(b_days.items(), key=lambda x: sum(x[1])/len(x[1])):
        brand_day_labels.append(b)
        brand_day_vals.append(round(sum(ds)/len(ds), 1))

    brand_perf_rows = ''
    for b in sorted(b_qty, key=lambda x: b_qty[x], reverse=True):
        qty  = b_qty[b]
        avg_d = round(sum(b_days[b])/len(b_days[b]), 1) if b_days[b] else 0
        mgr  = (b_profit[b]/b_rev[b]*100) if b_rev[b] else 0
        dc   = '#16a34a' if avg_d <= 7 else '#ea580c' if avg_d <= 14 else '#dc2626'
        mc   = '#16a34a' if mgr >= 30 else '#ea580c' if mgr >= 20 else '#dc2626'
        brand_perf_rows += f'''<tr>
          <td style="font-weight:700">{b}</td>
          <td style="text-align:right;font-weight:700;color:{dc}">{avg_d:.1f} 天</td>
          <td style="text-align:right">{qty:,}</td>
          <td style="text-align:right;font-weight:700;color:{mc}">{mgr:.1f}%</td>
        </tr>'''

    # ── 各型號統計（排行榜用）────────────────────────────────────
    mo_sold   = defaultdict(int)    # 已售台數
    mo_stock  = defaultdict(int)    # 庫存台數
    mo_profit = defaultdict(float)  # 已售總利潤
    mo_rev    = defaultdict(float)  # 已售總售價
    mo_days   = defaultdict(list)   # 已售在庫天數
    mo_stock_days = defaultdict(list)  # 庫存等待天數

    for r in sold:
        m = r[3].strip()
        mo_sold[m]   += 1
        mo_profit[m] += num(r[12])
        mo_rev[m]    += num(r[11])
        d = days_held(r)
        if d is not None: mo_days[m].append(d)
    for r in stock:
        m = r[3].strip()
        mo_stock[m] += 1
        d = days_held(r)
        if d is not None: mo_stock_days[m].append(d)

    all_models = set(mo_sold) | set(mo_stock)

    def turnover(m):
        total = mo_sold[m] + mo_stock[m]
        return (mo_sold[m] / total * 100) if total else 0

    def avg_margin(m):
        return (mo_profit[m] / mo_rev[m] * 100) if mo_rev[m] else 0

    def stagnant_days(m):
        ds = mo_stock_days[m]
        return (sum(ds) / len(ds)) if ds else 0

    # 只取有足夠銷售紀錄的型號（≥3台）用於排行
    ranked_models = [m for m in all_models if mo_sold[m] >= 3]

    # 毛利率排行（高→低）
    rank_margin = sorted(ranked_models, key=avg_margin, reverse=True)[:10]
    # 週轉率排行（高→低）
    rank_turnover = sorted(ranked_models, key=turnover, reverse=True)[:10]
    # 滯銷排行（庫存中等待天數最長，只取有庫存的型號）
    stagnant_models = [m for m in all_models if mo_stock[m] > 0]
    rank_stagnant = sorted(stagnant_models, key=stagnant_days, reverse=True)[:10]

    # 綜合排名：毛利率(40%) + 週轉率(40%) + 滯銷懲罰(20%)
    def norm(vals, reverse=False):
        if not vals: return {}
        mn, mx = min(vals.values()), max(vals.values())
        if mx == mn: return {k: 50 for k in vals}
        return {k: (v-mn)/(mx-mn)*100 if not reverse else (mx-v)/(mx-mn)*100
                for k,v in vals.items()}

    mg_map  = {m: avg_margin(m)    for m in ranked_models}
    tr_map  = {m: turnover(m)      for m in ranked_models}
    sg_map  = {m: stagnant_days(m) for m in ranked_models}
    mg_norm = norm(mg_map)
    tr_norm = norm(tr_map)
    sg_norm = norm(sg_map, reverse=True)
    composite = {m: mg_norm[m]*0.4 + tr_norm[m]*0.4 + sg_norm[m]*0.2
                 for m in ranked_models}
    rank_composite = sorted(ranked_models, key=lambda m: composite[m], reverse=True)[:10]

    def rank_row(i, m, score_str, score_color, extra=''):
        medal = ['🥇','🥈','🥉'][i] if i < 3 else f'{i+1}'
        return f'''<tr>
          <td style="text-align:center;font-size:1.1em">{medal}</td>
          <td style="font-weight:600;font-size:12px">{m}</td>
          <td style="text-align:right;font-weight:700;color:{score_color}">{score_str}</td>
          <td style="text-align:right;font-size:11px;color:#9ca3af">{extra}</td>
        </tr>'''

    def rank_table(rows_html, h1, h2, h3):
        return f'''<table class="mtable" style="font-size:12px">
          <thead><tr>
            <th style="width:36px;text-align:center">#</th>
            <th>{h1}</th><th style="text-align:right">{h2}</th>
            <th style="text-align:right">{h3}</th>
          </tr></thead><tbody>{rows_html}</tbody></table>'''

    rows_mg = ''.join(rank_row(i, m,
        f'{avg_margin(m):.1f}%',
        '#16a34a' if avg_margin(m)>=30 else '#ea580c' if avg_margin(m)>=20 else '#dc2626',
        f'{mo_sold[m]} 台') for i,m in enumerate(rank_margin))

    rows_tr = ''.join(rank_row(i, m,
        f'{turnover(m):.0f}%',
        '#16a34a' if turnover(m)>=80 else '#ea580c' if turnover(m)>=50 else '#dc2626',
        f'{mo_sold[m]}售/{mo_stock[m]}庫') for i,m in enumerate(rank_turnover))

    rows_sg = ''.join(rank_row(i, m,
        f'{stagnant_days(m):.0f} 天',
        '#dc2626' if stagnant_days(m)>30 else '#ea580c' if stagnant_days(m)>14 else '#0d9488',
        f'{mo_stock[m]} 台') for i,m in enumerate(rank_stagnant))

    rows_cp = ''.join(rank_row(i, m,
        f'{composite[m]:.0f} 分',
        '#2563eb',
        f'毛{avg_margin(m):.0f}%/轉{turnover(m):.0f}%') for i,m in enumerate(rank_composite))

    tbl_margin   = rank_table(rows_mg, '型號', '毛利率', '銷售台數')
    tbl_turnover = rank_table(rows_tr, '型號', '週轉率', '售/庫')
    tbl_stagnant = rank_table(rows_sg, '型號', '滯銷天數', '庫存台數')
    tbl_composite= rank_table(rows_cp, '型號', '綜合分數', '毛利/週轉')

    kpi_data = [
        ('📦', '總銷售台數',   f'{total_qty:,} 台',              '#2563eb'),
        ('🏪', '庫存總數量',   f'{total_stock:,} 台',            '#7c3aed'),
        ('💰', '總銷售收入',   f'NT${total_rev:,.0f}',           '#16a34a'),
        ('📈', '總毛利',       f'NT${total_profit:,.0f}',        '#ea580c'),
        ('🎯', '整體毛利率',   f'{margin_pct:.1f}%',             '#dc2626'),
        ('⭐', '平均單台利潤', f'NT${avg_profit:,.0f}',          '#0891b2'),
        ('⏱', '平均在庫天數', f'{avg_days_sold:.0f} 天',        '#0d9488'),
        ('💼', '庫存總成本',   f'NT${stock_cost:,.0f}',          '#b45309'),
    ]
    kpi_cards = ''
    for icon, label, disp, color in kpi_data:
        kpi_cards += f'''
        <div class="kpi-card">
          <div class="kpi-icon" style="background:{color}22">{icon}</div>
          <div class="kpi-label">{label}</div>
          <div class="kpi-val" style="color:{color}">{disp}</div>
        </div>'''

    # ── 月份統計 ──────────────────────────────────────────────────
    MONTH_ORDER = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月']
    m_qty    = defaultdict(int)
    m_rev    = defaultdict(float)
    m_profit = defaultdict(float)
    for r in sold:
        mo = r[0].strip()
        m_qty[mo]    += 1
        m_rev[mo]    += num(r[11])
        m_profit[mo] += num(r[12])

    month_labels, month_qty, month_rev, month_profit, month_margin = [], [], [], [], []
    month_rows_html = ''
    for mo in MONTH_ORDER:
        if mo not in m_qty: continue
        qty = m_qty[mo]
        rev = m_rev[mo]
        prf = m_profit[mo]
        mg  = (prf / rev * 100) if rev else 0
        month_labels.append(mo)
        month_qty.append(qty)
        month_rev.append(int(rev))
        month_profit.append(int(prf))
        month_margin.append(round(mg, 1))
        mc = '#16a34a' if mg >= 30 else '#ea580c' if mg >= 20 else '#dc2626'
        month_rows_html += f'''<tr>
          <td style="font-weight:700">{mo}</td>
          <td style="text-align:right">{qty:,}</td>
          <td style="text-align:right">NT${int(rev):,}</td>
          <td style="text-align:right">NT${int(prf):,}</td>
          <td style="text-align:right;font-weight:700;color:{mc}">{mg:.1f}%</td>
        </tr>'''
    # 合計列
    if month_labels:
        tr = int(total_rev); tp = int(total_profit)
        tmc = '#16a34a' if margin_pct >= 30 else '#ea580c' if margin_pct >= 20 else '#dc2626'
        month_rows_html += f'''<tr style="background:#f0fdf4;font-weight:700">
          <td>合計</td>
          <td style="text-align:right">{total_qty:,}</td>
          <td style="text-align:right">NT${tr:,}</td>
          <td style="text-align:right">NT${tp:,}</td>
          <td style="text-align:right;font-weight:700;color:{tmc}">{margin_pct:.1f}%</td>
        </tr>'''

    # ── 品牌統計 ──────────────────────────────────────────────────
    b_cnt = defaultdict(int)
    for r in sold:
        b_cnt[r[2].strip()] += 1
    brand_sorted = sorted(b_cnt.items(), key=lambda x: x[1], reverse=True)
    brand_labels = [b for b, _ in brand_sorted]
    brand_vals   = [c for _, c in brand_sorted]
    brand_colors = ['#2563eb','#16a34a','#ea580c','#dc2626','#7c3aed',
                    '#0891b2','#ca8a04','#db2777','#64748b','#475569',
                    '#0d9488','#b45309']

    # ── Top 10 型號 ───────────────────────────────────────────────
    mo_cnt = defaultdict(int)
    for r in sold:
        mo_cnt[r[3].strip()] += 1
    top10 = sorted(mo_cnt.items(), key=lambda x: x[1], reverse=True)[:10]
    top10_labels = [m for m, _ in top10]
    top10_vals   = [c for _, c in top10]
    top10_colors = ['#2563eb','#16a34a','#ea580c','#dc2626','#7c3aed',
                    '#0891b2','#ca8a04','#db2777','#64748b','#475569']

    html = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>艾薇通訊 — 二手機銷售儀表板</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg:#f5f6fa;--white:#fff;--border:#e8eaed;--text:#1a1d23;
      --text2:#4b5563;--gray:#9ca3af;--accent:#2563eb;
      --green:#16a34a;--red:#dc2626;--orange:#ea580c;
      --shadow:0 1px 4px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text)}}
    .header{{background:var(--white);border-bottom:1px solid var(--border);
             padding:0 28px;height:56px;display:flex;align-items:center;gap:16px;
             position:sticky;top:0;z-index:100}}
    .logo{{font-size:16px;font-weight:700;color:var(--text);letter-spacing:-.3px}}
    .logo em{{color:var(--accent);font-style:normal}}
    .live-badge{{display:flex;align-items:center;gap:5px;background:#f0fdf4;
                 border:1px solid #bbf7d0;color:var(--green);padding:3px 10px;
                 border-radius:20px;font-size:11px;font-weight:700}}
    .live-dot{{width:6px;height:6px;border-radius:50%;background:var(--green);animation:blink 1.6s infinite}}
    @keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
    .header-right{{margin-left:auto;font-size:12px;color:var(--gray)}}
    .container{{max-width:1080px;margin:0 auto;padding:28px 20px}}
    .sec-hd{{margin:28px 0 12px}}
    .sec-title{{font-size:15px;font-weight:700;color:var(--text);display:flex;align-items:center;gap:8px;margin-bottom:3px}}
    .sec-icon{{width:28px;height:28px;border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}}
    .sec-desc{{font-size:12px;color:var(--gray);margin-left:36px}}
    .card{{background:var(--white);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);margin-bottom:24px;overflow:hidden}}
    .card-body{{padding:20px}}
    .kpi-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:24px}}
    @media(min-width:520px){{.kpi-grid{{grid-template-columns:repeat(4,1fr)}}}}
    @media(min-width:900px){{.kpi-grid{{grid-template-columns:repeat(8,1fr)}}}}
    .kpi-card{{background:var(--white);border:1px solid var(--border);border-radius:12px;
               padding:18px 12px;text-align:center;box-shadow:var(--shadow)}}
    .kpi-icon{{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;
               justify-content:center;font-size:17px;margin:0 auto 10px}}
    .kpi-label{{font-size:11px;color:var(--gray);font-weight:600;margin-bottom:6px}}
    .kpi-val{{font-size:1.3em;font-weight:800;letter-spacing:-.5px;line-height:1}}
    .two-col{{display:grid;grid-template-columns:1fr;gap:16px}}
    @media(min-width:700px){{.two-col{{grid-template-columns:1fr 1fr}}}}
    .four-col{{display:grid;grid-template-columns:1fr;gap:16px;margin-bottom:24px}}
    @media(min-width:700px){{.four-col{{grid-template-columns:1fr 1fr}}}}
    @media(min-width:1000px){{.four-col{{grid-template-columns:repeat(4,1fr)}}}}
    .chart-wrap{{position:relative;height:260px}}
    table.mtable{{width:100%;border-collapse:collapse;font-size:13px}}
    table.mtable th{{background:var(--bg);padding:8px 12px;text-align:left;
                     font-weight:700;font-size:12px;color:var(--text2);border-bottom:2px solid var(--border)}}
    table.mtable td{{padding:9px 12px;border-bottom:1px solid var(--border);color:var(--text)}}
    table.mtable tr:last-child td{{border-bottom:none}}
    .time-bar{{background:var(--white);border-bottom:1px solid var(--border);
               padding:0 28px;height:44px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .time-label{{font-size:12px;color:var(--gray);margin-right:6px;font-weight:500}}
    .range-btn{{padding:5px 14px;border-radius:6px;font-size:12px;font-weight:600;
                cursor:pointer;border:1px solid var(--border);background:transparent;
                color:var(--text2);text-decoration:none;transition:all .15s;white-space:nowrap}}
    .range-btn:hover{{background:var(--bg);border-color:#d1d5db}}
    .range-btn.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
    .divider-v{{width:1px;height:20px;background:var(--border);margin:0 4px}}
    .error-banner{{background:#fef2f2;border:1px solid #fecaca;color:#dc2626;
                   padding:14px 20px;border-radius:10px;margin-bottom:20px;font-size:13px}}
  </style>
</head>
<body>
  <div class="header">
    <div class="logo"><em>艾薇</em>通訊 — 二手機銷售儀表板</div>
    <div class="live-badge"><div class="live-dot"></div>115 年度</div>
    <div class="header-right">資料來源：Google Sheets &nbsp;·&nbsp; 每日 08:00 自動同步</div>
  </div>
  {time_bar_html}

  <div class="container">
    {'<div class="error-banner">⚠️ 無法載入試算表資料，請確認試算表已設定為「知道連結的人均可查看」。</div>' if not data_ok else ''}

    <div class="sec-hd">
      <div class="sec-title"><div class="sec-icon" style="background:#eff6ff">📊</div>整體績效指標</div>
      <div class="sec-desc">篩選區間：{range_label} · 已售 {len(sold):,} 台 · 庫存 {len(stock):,} 台</div>
    </div>
    <div class="kpi-grid">{kpi_cards}</div>

    <div class="sec-hd">
      <div class="sec-title"><div class="sec-icon" style="background:#f0fdf4">📅</div>各月份銷售趨勢</div>
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-body">
          <div class="chart-wrap"><canvas id="monthChart"></canvas></div>
        </div>
      </div>
      <div class="card">
        <div class="card-body" style="padding:0">
          <table class="mtable">
            <thead><tr>
              <th>月份</th>
              <th style="text-align:right">台數</th>
              <th style="text-align:right">銷售額</th>
              <th style="text-align:right">毛利</th>
              <th style="text-align:right">毛利率</th>
            </tr></thead>
            <tbody>{month_rows_html}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="sec-hd">
      <div class="sec-title"><div class="sec-icon" style="background:#f0fdfa">⏱</div>在庫天數分析</div>
      <div class="sec-desc">平均賣出需要幾天 — 越短代表周轉越快、資金佔用越少</div>
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-body">
          <div style="display:flex;gap:24px;margin-bottom:16px">
            <div style="text-align:center;flex:1;padding:14px;background:#f0fdfa;border-radius:10px">
              <div style="font-size:11px;color:#9ca3af;font-weight:600;margin-bottom:6px">已售出平均在庫</div>
              <div style="font-size:2em;font-weight:800;color:#0d9488">{avg_days_sold:.0f} <span style="font-size:.5em;font-weight:500">天</span></div>
            </div>
            <div style="text-align:center;flex:1;padding:14px;background:#fff7ed;border-radius:10px">
              <div style="font-size:11px;color:#9ca3af;font-weight:600;margin-bottom:6px">現有庫存平均等待</div>
              <div style="font-size:2em;font-weight:800;color:#b45309">{avg_days_stock:.0f} <span style="font-size:.5em;font-weight:500">天</span></div>
            </div>
          </div>
          <div class="chart-wrap"><canvas id="daysChart"></canvas></div>
        </div>
      </div>
      <div class="card">
        <div class="card-body" style="padding:0">
          <table class="mtable">
            <thead><tr>
              <th>品牌</th>
              <th style="text-align:right">平均在庫天</th>
              <th style="text-align:right">銷售台數</th>
              <th style="text-align:right">毛利率</th>
            </tr></thead>
            <tbody>{brand_perf_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="two-col">
      <div>
        <div class="sec-hd">
          <div class="sec-title"><div class="sec-icon" style="background:#faf5ff">🏷</div>品牌銷售佔比</div>
        </div>
        <div class="card">
          <div class="card-body">
            <div class="chart-wrap"><canvas id="brandChart"></canvas></div>
          </div>
        </div>
      </div>
      <div>
        <div class="sec-hd">
          <div class="sec-title"><div class="sec-icon" style="background:#fff7ed">🏆</div>Top 10 熱銷型號</div>
        </div>
        <div class="card">
          <div class="card-body">
            <div class="chart-wrap"><canvas id="topChart"></canvas></div>
          </div>
        </div>
      </div>
    </div>

    <!-- 排行榜 -->
    <div class="sec-hd">
      <div class="sec-title"><div class="sec-icon" style="background:#fef9c3">🏅</div>型號排行榜</div>
      <div class="sec-desc">僅統計銷售 3 台以上型號 · 綜合分數 = 毛利率 40% + 週轉率 40% + 不滯銷 20%</div>
    </div>
    <div class="four-col">
      <div>
        <div style="font-size:12px;font-weight:700;color:#16a34a;margin-bottom:8px;padding:0 4px">💰 毛利率排行</div>
        <div class="card"><div class="card-body" style="padding:0">{tbl_margin}</div></div>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:#2563eb;margin-bottom:8px;padding:0 4px">🔄 週轉率排行</div>
        <div class="card"><div class="card-body" style="padding:0">{tbl_turnover}</div></div>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:#dc2626;margin-bottom:8px;padding:0 4px">🐌 滯銷排行（庫存等待最久）</div>
        <div class="card"><div class="card-body" style="padding:0">{tbl_stagnant}</div></div>
      </div>
      <div>
        <div style="font-size:12px;font-weight:700;color:#7c3aed;margin-bottom:8px;padding:0 4px">⭐ 綜合排名</div>
        <div class="card"><div class="card-body" style="padding:0">{tbl_composite}</div></div>
      </div>
    </div>

  </div>

  <script>
  new Chart(document.getElementById('monthChart'),{{
    type:'bar',
    data:{{
      labels:{_json.dumps(month_labels,ensure_ascii=False)},
      datasets:[
        {{type:'bar',label:'銷售額',data:{_json.dumps(month_rev)},
          backgroundColor:'#2563eb33',borderColor:'#2563eb',borderWidth:1.5,yAxisID:'y'}},
        {{type:'bar',label:'毛利',data:{_json.dumps(month_profit)},
          backgroundColor:'#16a34a33',borderColor:'#16a34a',borderWidth:1.5,yAxisID:'y'}},
        {{type:'line',label:'毛利率%',data:{_json.dumps(month_margin)},
          borderColor:'#ea580c',backgroundColor:'#ea580c22',
          tension:.4,pointRadius:4,fill:false,yAxisID:'y2'}}
      ]
    }},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{
        legend:{{position:'top',labels:{{font:{{size:11}},padding:12,color:'#4b5563'}}}},
        tooltip:{{callbacks:{{
          label:ctx=>ctx.dataset.yAxisID==='y2'
            ?` ${{ctx.dataset.label}}: ${{ctx.raw}}%`
            :` ${{ctx.dataset.label}}: NT$${{ctx.raw.toLocaleString()}}`
        }}}}
      }},
      scales:{{
        y:{{position:'left',ticks:{{callback:v=>'NT$'+v.toLocaleString(),font:{{size:10}},color:'#9ca3af'}},grid:{{color:'#f3f4f6'}}}},
        y2:{{position:'right',min:0,max:50,ticks:{{callback:v=>v+'%',font:{{size:10}},color:'#9ca3af'}},grid:{{drawOnChartArea:false}}}}
      }}
    }}
  }});

  new Chart(document.getElementById('daysChart'),{{
    type:'bar',
    data:{{
      labels:{_json.dumps(brand_day_labels,ensure_ascii=False)},
      datasets:[{{
        label:'平均在庫天數',
        data:{_json.dumps(brand_day_vals)},
        backgroundColor:{_json.dumps(brand_day_labels)}.map((_,i)=>
          {_json.dumps(brand_day_vals)}[i]<=7?'#0d948888':
          {_json.dumps(brand_day_vals)}[i]<=14?'#ea580c88':'#dc262688'
        ),
        borderRadius:4
      }}]
    }},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{
        legend:{{display:false}},
        tooltip:{{callbacks:{{label:ctx=>` ${{ctx.raw}} 天`}}}}
      }},
      scales:{{
        x:{{ticks:{{font:{{size:10}},color:'#4b5563'}},grid:{{display:false}}}},
        y:{{ticks:{{callback:v=>v+'天',font:{{size:10}},color:'#9ca3af'}},grid:{{color:'#f3f4f6'}}}}
      }}
    }}
  }});

  new Chart(document.getElementById('brandChart'),{{
    type:'doughnut',
    data:{{
      labels:{_json.dumps(brand_labels,ensure_ascii=False)},
      datasets:[{{data:{_json.dumps(brand_vals)},
        backgroundColor:{_json.dumps(brand_colors[:len(brand_labels)])},borderWidth:2}}]
    }},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{
        legend:{{position:'right',labels:{{font:{{size:11}},padding:10,color:'#4b5563'}}}},
        tooltip:{{callbacks:{{label:ctx=>` ${{ctx.label}}: ${{ctx.raw}} 台`}}}}
      }}
    }}
  }});

  new Chart(document.getElementById('topChart'),{{
    type:'bar',
    data:{{
      labels:{_json.dumps(top10_labels,ensure_ascii=False)},
      datasets:[{{
        label:'銷售台數',data:{_json.dumps(top10_vals)},
        backgroundColor:{_json.dumps([c+'cc' for c in top10_colors[:len(top10_vals)]])},
        borderRadius:4
      }}]
    }},
    options:{{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{{
        legend:{{display:false}},
        tooltip:{{callbacks:{{label:ctx=>` ${{ctx.raw}} 台`}}}}
      }},
      scales:{{
        x:{{ticks:{{font:{{size:10}},color:'#9ca3af'}},grid:{{color:'#f3f4f6'}}}},
        y:{{ticks:{{font:{{size:10}},color:'#4b5563'}}}}
      }}
    }}
  }});
  </script>
</body>
</html>'''
    return html

@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        logger.error(f'Webhook error: {e}')
    return 'OK'

@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else 'N/A'
    logger.info(f'機器人加入群組！Group ID = {group_id}')
    reply(event.reply_token,
          f"大家好！我是艾薇AI助理 🤖\n\n"
          f"09:00 待辦回報 → 10:00 提醒 → 11:00 彙整早報\n"
          f"21:00 完成回報 → 23:00 提醒 → 00:00 彙整晚報\n\n"
          f"📋 Group ID：{group_id}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    group_id = getattr(event.source, 'group_id', None)
    if not group_id:
        return
    user_id = event.source.user_id
    text    = event.message.text.strip()

    # 首次身份註冊
    for mgr in MANAGERS:
        if text in (f'我是{mgr}', f'我是 {mgr}'):
            register_user(user_id, mgr)
            already_permanent = user_id in STATIC_USER_MAP
            if already_permanent:
                reply(event.reply_token, f"✅ {mgr} 身份已永久綁定，直接在群組打字回報即可 👍")
            else:
                reply(event.reply_token,
                      f"✅ 暫時記住 {mgr} 了！\n\n📋 你的 LINE ID：\n{user_id}\n\n請把這串 ID 傳給管理員，設定後永久生效 🔒")
            return

    manager = get_manager_for_user(user_id)
    if not manager:
        return

    # 早晨收集視窗（含深夜 00:00~08:59 提前登記）
    if is_morning_window():
        store_morning_todos(manager, text)
        if is_prenoon_presubmit():
            reply(event.reply_token,
                  f"✅ 收到 {manager} 的今日待辦（提前登記）！\n明早 11:00 彙整早報 📋\n晚安 🌙")
        else:
            reply(event.reply_token, f"✅ 收到 {manager} 的今日待辦！11:00 彙整 📋")
        return

    # 晚間收集視窗
    if is_evening_window():
        store_evening_report(manager, text)
        reply(event.reply_token, f"✅ 收到 {manager} 的完成回報！00:00 彙整 📋")
        return

# ── 排程器 ───────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=TZ)
def self_ping():
    """每 10 分鐘自我喚醒，防止 Render 免費版睡著"""
    try:
        import urllib.request
        urllib.request.urlopen('https://aivy-line-bot.onrender.com/ping', timeout=10)
        logger.info('自我喚醒 ping 成功')
    except Exception as e:
        logger.warning(f'自我喚醒失敗：{e}')

scheduler.add_job(self_ping,             'interval', minutes=10)
scheduler.add_job(send_monthly_report,   'cron', day=1, hour=8, minute=0)
scheduler.add_job(send_weekly_report,    'cron', day_of_week='mon', hour=8, minute=0)
scheduler.add_job(check_missing_reports, 'cron', hour=8,  minute=20)
scheduler.add_job(check_overdue_items,   'cron', hour=8,  minute=30)
scheduler.add_job(send_morning_prompt,   'cron', hour=9,  minute=0)
scheduler.add_job(send_morning_reminder, 'cron', hour=10, minute=0)
scheduler.add_job(send_morning_summary,  'cron', hour=11, minute=0)
scheduler.add_job(send_evening_prompt,   'cron', hour=21, minute=0)
scheduler.add_job(send_evening_reminder, 'cron', hour=23, minute=0)
scheduler.add_job(send_evening_summary,  'cron', hour=0,  minute=0)
scheduler.start()
logger.info('排程器已啟動')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
