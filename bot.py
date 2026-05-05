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
    """把 ✅/❌ 回報文字拆成項目 + 狀態 + 原因"""
    lines = text.strip().split('\n')
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('✅') or line.startswith('✔') or line.startswith('☑'):
            content = line[1:].strip()
            items.append({'item': content, 'status': 'done', 'reason': ''})
        elif line.startswith('❌') or line.startswith('✗'):
            content = line[1:].strip()
            match = re.search(r'[（(](.+?)[）)]', content)
            reason = match.group(1) if match else ''
            item_text = re.sub(r'\s*[（(].+?[）)]\s*', '', content).strip()
            items.append({'item': item_text, 'status': 'incomplete', 'reason': reason})
        else:
            items.append({'item': line, 'status': 'done', 'reason': ''})
    return items if items else [{'item': text.strip(), 'status': 'done', 'reason': ''}]

# ── Supabase 寫入 ────────────────────────────────────────────
def save_morning_to_db(todos, date_str):
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
    """取得昨日未完成項目，依主管分組"""
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
def build_morning_summary_flex(todos):
    today = today_key()
    rows = []
    for mgr in MANAGERS:
        if mgr in todos:
            rows.append({"type": "text", "text": f"✅  {mgr}",
                         "weight": "bold", "size": "sm", "color": "#06C755"})
            for line in todos[mgr].strip().split('\n'):
                if line.strip():
                    rows.append({"type": "text", "text": f"    {line.strip()}",
                                 "size": "sm", "color": "#444444", "wrap": True, "margin": "xs"})
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
    try:
        push(build_morning_summary_flex(todos))
        mark_morning_summary_sent()
        save_morning_to_db(todos, today_key())
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
          <div class="rate-name">{mgr} <span style="font-size:1.1em;color:{a_color};font-weight:700">{arrow}</span></div>
          <div class="rate-bar-bg"><div class="rate-bar" style="width:{bar_w}%;background:{color}"></div></div>
          <div class="rate-pct" style="color:{color}">{rate}%</div>
          <div class="rate-label">{label}</div>
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
        for session_key, label in [('morning','☀️ 早報'),('evening','🌙 晚報')]:
            session_data = sessions[session_key]
            if not session_data:
                continue
            for mgr in MANAGERS:
                items = session_data.get(mgr, [])
                if not items:
                    continue
                for i, item in enumerate(items):
                    color  = STATUS_COLOR.get(item['status'], '#888')
                    emoji  = STATUS_LABEL.get(item['status'], '')
                    reason = f'<br><small style="color:#aaa">（{item["reason"]}）</small>' if item.get('reason') else ''
                    rs = len(items)
                    detail_rows += f'''<tr>
                      {"<td rowspan='" + str(rs) + "' style='font-weight:bold;color:#555;white-space:nowrap'>" + day[5:] + "</td>" if i==0 else ""}
                      {"<td rowspan='" + str(rs) + "'>" + label + "</td>" if i==0 else ""}
                      {"<td rowspan='" + str(rs) + "' style='font-weight:bold'>" + mgr + "</td>" if i==0 else ""}
                      <td>{item["item_text"]}{reason}</td>
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
            icon, sub, bg = '✅', f'晚報已回報<br>{eve_done}/{eve_total} 件完成', '#f0fff0'
            border = '#1AAE1A'
        elif morning and any(r['status'] == 'reported' for r in morning):
            icon, sub, bg = '📋', '早報已登記<br>等待晚間回報', '#fffbf0'
            border = '#FF9800'
        elif any(r['status'] == 'done' and r['item_text'] == '休假' for r in mgr_today):
            icon, sub, bg = '🏖️', '今日休假', '#f0f8ff'
            border = '#90CAF9'
        else:
            icon, sub, bg = '⏳', '尚未回報<br>—', '#fff5f5'
            border = '#E53935'
        snap_cards += f'''
        <div style="background:{bg};border:1.5px solid {border};border-radius:12px;
                    padding:16px;text-align:center;box-shadow:0 1px 6px rgba(0,0,0,.05)">
          <div style="font-weight:700;font-size:.95em;margin-bottom:8px">{mgr}</div>
          <div style="font-size:1.8em;margin-bottom:6px">{icon}</div>
          <div style="font-size:.72em;color:#666;line-height:1.6">{sub}</div>
        </div>'''

    # ── ② 連續未完成警示（查最近 30 天）──────────────────────
    thirty_ago = (today - timedelta(days=30)).isoformat()
    overdue_result = supabase_client.table('daily_reports')\
        .select('report_date, manager, item_text, status')\
        .eq('session', 'evening').eq('status', 'incomplete')\
        .gte('report_date', thirty_ago)\
        .order('report_date', desc=False).execute()
    overdue_rows = overdue_result.data or []

    from itertools import groupby as _groupby
    item_dates = defaultdict(list)
    for r in overdue_rows:
        item_dates[(r['manager'], r['item_text'])].append(r['report_date'])

    consecutive_items = []
    for (mgr, item), dates in item_dates.items():
        sorted_dates = sorted(set(dates))
        streak, cur = 1, 1
        for i in range(1, len(sorted_dates)):
            d1 = date.fromisoformat(sorted_dates[i-1])
            d2 = date.fromisoformat(sorted_dates[i])
            if (d2 - d1).days <= 3:
                cur += 1
                streak = max(streak, cur)
            else:
                cur = 1
        if streak >= 3:
            consecutive_items.append((streak, mgr, item, sorted_dates[0], sorted_dates[-1]))
    consecutive_items.sort(reverse=True)

    overdue_html = ''
    for streak, mgr, item, first_date, last_date in consecutive_items[:8]:
        color = '#E53935' if streak >= 5 else '#FF9800'
        bg    = '#fff5f5' if streak >= 5 else '#fffaf0'
        warn  = '⚠️ 建議主動了解狀況' if streak >= 5 else '注意持續追蹤'
        overdue_html += f'''
        <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;
                    border-radius:10px;border-left:4px solid {color};background:{bg};margin-bottom:8px">
          <span style="background:{color};color:#fff;border-radius:99px;
                       padding:3px 12px;font-size:.75em;font-weight:700;white-space:nowrap">{streak} 天</span>
          <div style="flex:1">
            <div style="font-weight:700;font-size:.88em">{item}</div>
            <div style="font-size:.75em;color:#888;margin-top:2px">{mgr}・{first_date[5:]} 起</div>
            <div style="font-size:.75em;color:{color};margin-top:2px">{warn}</div>
          </div>
        </div>'''
    if not overdue_html:
        overdue_html = '<div style="text-align:center;padding:20px;color:#aaa;font-size:.85em">近30天無連續未完成項目 🎉</div>'

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
        ('🔥','現場突發',  ['現場','門市','忙','客','插曲','交機','業務']),
        ('⏰','時間不夠',  ['時間','太久','壓縮','來不及','沒時間','佔用','忙到']),
        ('🧠','個人因素',  ['靈感','心態','腦袋','個人','沒有','沒辦法','思考']),
        ('🔗','等待外部',  ['等','待','未','放鳥','尚未','廠商','回覆','入帳']),
    ]
    cat_counts = [0]*4
    all_reasons = [r.get('reason','') for r in incomplete_items if r.get('reason')]
    for reason in all_reasons:
        matched = False
        for i, (_, _, kws) in enumerate(cat_map):
            if any(kw in reason for kw in kws):
                cat_counts[i] += 1
                matched = True
                break
        if not matched:
            cat_counts[0] += 1
    total_reasons = sum(cat_counts) or 1

    reason_cats_html = ''
    max_c = max(cat_counts) or 1
    for i, (icon, name, _) in enumerate(cat_map):
        c = cat_counts[i]
        pct = round(c / total_reasons * 100)
        bar_w = round(c / max_c * 100)
        reason_cats_html += f'''
        <div style="background:#f8f9ff;border-radius:10px;padding:14px;text-align:center">
          <div style="font-size:1.6em;margin-bottom:6px">{icon}</div>
          <div style="font-size:.8em;font-weight:700;color:#555;margin-bottom:4px">{name}</div>
          <div style="font-size:1.5em;font-weight:700;color:#5C5CE6">{c}</div>
          <div style="font-size:.72em;color:#888">佔 {pct}%</div>
          <div style="height:4px;border-radius:99px;background:#5C5CE6;margin-top:8px;width:{bar_w}%"></div>
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
        <div style="background:#f8f9ff;border-radius:10px;padding:14px;text-align:center;
                    border:1.5px solid #e8eaf6">
          <div style="font-weight:700;font-size:.9em;margin-bottom:8px">{mgr}</div>
          <div style="font-size:1.9em;font-weight:700;color:#1A73E8">{avg}</div>
          <div style="font-size:.72em;color:#888;margin-top:2px">件 / 天</div>
          <div style="height:6px;background:#eee;border-radius:99px;margin-top:10px">
            <div style="height:6px;border-radius:99px;background:{bar_color};width:{bar_w}%"></div>
          </div>
          <div style="font-size:.72em;color:#888;margin-top:6px">完成率 {rate}%</div>
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
                    background:#f8f9ff;border-radius:10px;margin-bottom:6px">
          <div style="font-weight:700;width:44px;font-size:.9em">{mgr}</div>
          <div style="flex:1;height:10px;background:#eee;border-radius:99px;overflow:hidden">
            <div style="height:10px;border-radius:99px;background:{bar_color};width:{pct}%"></div>
          </div>
          <div style="width:40px;text-align:right;font-weight:700;font-size:.88em;color:{bar_color}">{pct}%</div>
          <div style="width:80px;text-align:right;font-size:.72em;color:{lc}">{label}</div>
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
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,sans-serif;background:#f0f4f8;color:#333}}
    .header{{background:linear-gradient(135deg,#1A73E8,#5C5CE6);color:#fff;padding:20px 24px 16px;text-align:center}}
    .header h1{{font-size:1.4em;font-weight:700}}
    .header p{{font-size:.8em;opacity:.8;margin-top:4px}}
    .container{{max-width:960px;margin:20px auto;padding:0 14px}}
    .section-title{{font-size:.95em;font-weight:700;color:#555;margin:22px 0 10px;padding-left:4px}}
    .card{{background:#fff;border-radius:14px;box-shadow:0 2px 8px rgba(0,0,0,.07);overflow:hidden;margin-bottom:8px}}
    .card-body{{padding:16px 18px}}
    .grid4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}
    .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
    table{{width:100%;border-collapse:collapse}}
    th{{background:#5C5CE6;color:#fff;padding:10px 14px;text-align:left;font-size:.82em}}
    td{{padding:10px 14px;border-bottom:1px solid #f0f0f0;font-size:.83em;vertical-align:top}}
    tr:last-child td{{border-bottom:none}}
    tr:hover td{{background:#f8f9ff}}
    .empty{{text-align:center;padding:30px;color:#bbb;font-size:.85em}}
    .time-bar{{background:#fff;border-radius:14px;box-shadow:0 2px 8px rgba(0,0,0,.07);
               padding:14px 16px;margin-bottom:18px;display:flex;flex-wrap:wrap;align-items:center;gap:8px}}
    .time-bar-label{{font-size:.8em;font-weight:700;color:#888;margin-right:4px;white-space:nowrap}}
    .range-btn{{padding:6px 14px;border-radius:99px;border:1.5px solid #d0d6e8;font-size:.82em;
                font-weight:600;color:#5C5CE6;text-decoration:none;transition:.15s;white-space:nowrap}}
    .range-btn:hover{{background:#f0f0ff;border-color:#5C5CE6}}
    .range-btn.active{{background:#5C5CE6;color:#fff;border-color:#5C5CE6}}
    .divider{{width:1px;height:20px;background:#e0e0e0;margin:0 4px}}
    .custom-form{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
    .custom-form input[type=date]{{padding:5px 10px;border:1.5px solid #d0d6e8;border-radius:8px;
      font-size:.82em;color:#333;background:#f8f9ff;outline:none;cursor:pointer}}
    .custom-form input[type=date]:focus{{border-color:#5C5CE6}}
    .custom-form button{{padding:6px 14px;border-radius:99px;border:none;
      background:#5C5CE6;color:#fff;font-size:.82em;font-weight:700;cursor:pointer}}
    .range-tag{{font-size:.78em;color:#888;margin-left:auto;white-space:nowrap}}
    .rate-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:8px}}
    .rate-card{{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 8px rgba(0,0,0,.07)}}
    .rate-name{{font-weight:700;font-size:.95em;margin-bottom:8px}}
    .rate-bar-bg{{background:#eee;border-radius:99px;height:8px;margin-bottom:6px}}
    .rate-bar{{height:8px;border-radius:99px;transition:width .5s}}
    .rate-pct{{font-size:1.5em;font-weight:700;margin-bottom:2px}}
    .rate-label{{font-size:.75em;color:#888}}
    @media(max-width:600px){{.grid2{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
  <div class="header">
    <h1>📋 艾薇 回報系統儀表板</h1>
    <p>{since_iso[5:]} ～ {until_iso[5:]}　共 {span_days} 天</p>
  </div>
  <div class="container">

    <!-- 時間軸選擇器 -->
    <div class="time-bar">
      <span class="time-bar-label">📅 時間軸</span>
      {btn('7',  '近 7 天')}
      {btn('14', '近 14 天')}
      {btn('30', '近 30 天')}
      <div class="divider"></div>
      <form class="custom-form" method="get" action="/dashboard">
        <input type="date" name="from" value="{date_from_str}" max="{today.isoformat()}">
        <span style="color:#aaa;font-size:.85em">～</span>
        <input type="date" name="to" value="{date_to_str}" max="{today.isoformat()}">
        <button type="submit">{'✔ 套用' if active_range == 'custom' else '自訂'}</button>
      </form>
      <span class="range-tag">目前：{current_label}</span>
    </div>

    <!-- ① 今日快照 -->
    <div class="section-title">🗓 今日快照（{today.isoformat()[5:]}）</div>
    <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">頁面最頂端一眼看到今天4位主管的回報狀態與完成件數，不用往下滾動</div>
    <div class="card card-body">
      <div class="grid4">{snap_cards}</div>
    </div>

    <!-- 完成率卡片 -->
    <div class="section-title">📊 完成率（{current_label}）</div>
    <div class="rate-grid">{rate_cards}</div>

    <!-- ③ 完成率趨勢折線圖 -->
    <div class="section-title">📈 完成率趨勢</div>
    <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">每人完成率走勢，一眼看出誰在退步、誰在進步，適合月中／月底對焦</div>
    <div class="card card-body" style="padding-bottom:12px">
      <canvas id="trendChart" height="200"></canvas>
    </div>

    <!-- ② 連續未完成警示 -->
    <div class="section-title">🚨 連續未完成警示（近 30 天）</div>
    <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">同一件任務連續多天未完成自動高亮，依嚴重程度顯示橘色／紅色，讓你快速找到需要追蹤的人</div>
    <div class="card card-body">{overdue_html}</div>

    <!-- 未完成原因表 + ④ 分類 -->
    <div class="section-title">❌ 未完成原因統計</div>
    <div class="card">
      <table>
        <thead><tr><th>姓名</th><th>日期</th><th>任務</th><th>未完成原因</th></tr></thead>
        <tbody>{reason_rows}</tbody>
      </table>
    </div>
    <div class="section-title">🗂 原因分類分析</div>
    <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">把未完成原因自動歸類，看哪類問題最常出現，有助於制度面改善</div>
    <div class="card card-body">
      <div class="grid4">{reason_cats_html}</div>
    </div>

    <!-- ⑤ 任務量 + ⑥ 回報率 -->
    <div class="grid2">
      <div>
        <div class="section-title">📦 平均每日任務量</div>
        <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">看誰每天計畫太少或太多（完不成），找出任務設定合理性問題</div>
        <div class="card card-body">
          <div class="grid4">{taskload_html}</div>
        </div>
      </div>
      <div>
        <div class="section-title">📬 晚報回報率</div>
        <div style="font-size:.8em;color:#888;margin:-4px 0 10px 4px">統計每人在 23:59 前完成晚間回報的比例，看誰習慣拖到最後一刻才報</div>
        <div class="card card-body">{punct_html}
          <div style="font-size:.72em;color:#aaa;text-align:center;margin-top:8px">
            準時定義：23:59 前完成晚間回報
          </div>
        </div>
      </div>
    </div>

    <!-- 每日明細 -->
    <div class="section-title">📅 每日回報明細</div>
    <div class="card">
      <table>
        <thead><tr><th>日期</th><th>回報</th><th>姓名</th><th>項目</th><th style="text-align:center">狀態</th></tr></thead>
        <tbody>{"".join(detail_rows) if detail_rows else '<tr><td colspan="5" class="empty">尚無資料</td></tr>'}</tbody>
      </table>
    </div>

  </div>
  <script>
  new Chart(document.getElementById('trendChart'),{{
    type:'line',
    data:{chart_data_js},
    options:{{
      responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{position:'top',labels:{{font:{{size:12}},padding:14}}}},
               tooltip:{{callbacks:{{label:c=>` ${{c.dataset.label}}：${{c.raw}}%`}}}}}},
      scales:{{
        y:{{min:0,max:100,ticks:{{callback:v=>v+'%',font:{{size:11}}}},grid:{{color:'#f0f0f0'}}}},
        x:{{ticks:{{font:{{size:11}}}},grid:{{display:false}}}}
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
