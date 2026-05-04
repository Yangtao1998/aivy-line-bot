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
    state = load_state()
    state, today = ensure_today(state)
    m = state[today]['morning']
    return m['sent'] and not m['summary_sent']

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

def build_evening_summary_flex(reports):
    today = today_key()
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
        push(TextMessage(
            text="☀️ 早安！請各主管直接在群組輸入今日待辦事項\n\n"
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
    reports = get_evening_reports()
    try:
        push(build_evening_summary_flex(reports))
        mark_evening_summary_sent()
        save_evening_to_db(reports, today_key())
        logger.info('晚間彙整卡發送並存入資料庫完成')
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

@app.route('/trigger/weekly-report', methods=['GET'])
def trigger_weekly_report():
    send_weekly_report()
    return 'weekly report sent', 200

@app.route('/dashboard', methods=['GET'])
def dashboard():
    days = 7
    if not supabase_client:
        return '<h2>Supabase 未連線</h2>', 500

    since = (date.today() - timedelta(days=days)).isoformat()
    result = supabase_client.table('daily_reports')\
        .select('report_date, manager, session, item_text, status, reason')\
        .gte('report_date', since)\
        .order('report_date', desc=True)\
        .order('manager')\
        .execute()

    rows = result.data or []

    # 整理資料：日期 → 階段 → 人員 → 項目
    from collections import OrderedDict
    days_data = OrderedDict()
    for row in rows:
        d = row['report_date']
        s = row['session']
        m = row['manager']
        days_data.setdefault(d, {'morning': defaultdict(list), 'evening': defaultdict(list)})
        days_data[d][s][m].append(row)

    STATUS_COLOR = {'done': '#1AAE1A', 'incomplete': '#E53935',
                    'reported': '#1A73E8', 'not_reported': '#BBBBBB'}
    STATUS_LABEL = {'done': '✅', 'incomplete': '❌',
                    'reported': '✅', 'not_reported': '⏳'}

    html_rows = ''
    for day, sessions in days_data.items():
        for session_key, label in [('morning', '☀️ 早報'), ('evening', '🌙 晚報')]:
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
                    reason = f'<br><small style="color:#888">（{item["reason"]}）</small>' if item.get('reason') else ''
                    html_rows += f'''
                    <tr>
                      {"<td rowspan='" + str(len(items)) + "' style='font-weight:bold;color:#333'>" + day + "</td>" if i == 0 else ""}
                      {"<td rowspan='" + str(len(items)) + "'>" + label + "</td>" if i == 0 else ""}
                      {"<td rowspan='" + str(len(items)) + "' style='font-weight:bold'>" + mgr + "</td>" if i == 0 else ""}
                      <td>{item["item_text"]}{reason}</td>
                      <td style="color:{color};font-weight:bold">{emoji}</td>
                    </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>艾薇 每日回報紀錄</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, sans-serif; background: #f5f7fa; color: #333; }}
    .header {{ background: linear-gradient(135deg,#1A73E8,#5C5CE6); color:#fff; padding: 20px; text-align:center; }}
    .header h1 {{ font-size: 1.4em; }}
    .header p {{ font-size: 0.85em; opacity:.8; margin-top:4px; }}
    .container {{ max-width: 900px; margin: 20px auto; padding: 0 12px; }}
    table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:12px;
             overflow:hidden; box-shadow:0 2px 8px rgba(0,0,0,.08); }}
    th {{ background:#1A73E8; color:#fff; padding:10px 12px; text-align:left; font-size:.85em; }}
    td {{ padding:10px 12px; border-bottom:1px solid #f0f0f0; font-size:.85em; vertical-align:top; }}
    tr:last-child td {{ border-bottom:none; }}
    tr:hover td {{ background:#f9fbff; }}
    .empty {{ text-align:center; padding:40px; color:#aaa; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>📋 艾薇 每日回報紀錄</h1>
    <p>最近 {days} 天｜自動更新</p>
  </div>
  <div class="container">
    <table>
      <thead>
        <tr>
          <th>日期</th><th>回報</th><th>姓名</th><th>項目</th><th>狀態</th>
        </tr>
      </thead>
      <tbody>
        {"".join(html_rows) if html_rows else '<tr><td colspan="5" class="empty">尚無資料</td></tr>'}
      </tbody>
    </table>
  </div>
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

    # 早晨收集視窗
    if is_morning_window():
        store_morning_todos(manager, text)
        reply(event.reply_token, f"✅ 收到 {manager} 的今日待辦！11:00 彙整 📋")
        return

    # 晚間收集視窗
    if is_evening_window():
        store_evening_report(manager, text)
        reply(event.reply_token, f"✅ 收到 {manager} 的完成回報！00:00 彙整 📋")
        return

# ── 排程器 ───────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(send_weekly_report,    'cron', day_of_week='mon', hour=8, minute=0)
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
