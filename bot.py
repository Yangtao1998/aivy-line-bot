import os
import json
import logging
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, PostbackEvent, TextMessageContent, JoinEvent
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

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

TZ         = pytz.timezone('Asia/Taipei')
MANAGERS   = ['小陳', 'Hank', '小羊']
STATE_FILE = 'state.json'

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
            'morning': {
                'sent': False,
                'todos': {},      # manager -> 原始文字
                'waiting': {},    # user_id -> manager
            },
            'evening': {
                'sent': False,
                'reports': {},    # manager -> 原始文字
                'waiting': {},    # user_id -> manager
            },
        }
    return state, today

# ── 早晨狀態 ─────────────────────────────────────────────────
def mark_morning_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning']['sent'] = True
    save_state(state)

def set_waiting_morning(user_id, manager):
    state = load_state()
    state, today = ensure_today(state)
    state[today]['morning']['waiting'][user_id] = manager
    save_state(state)

def store_morning_todos(user_id, text):
    state = load_state()
    state, today = ensure_today(state)
    morning = state[today]['morning']
    manager = morning['waiting'].get(user_id)
    if not manager:
        return None
    morning['todos'][manager] = text
    del morning['waiting'][user_id]
    save_state(state)
    return manager

def get_morning_todos():
    state = load_state()
    state, today = ensure_today(state)
    return state[today]['morning']['todos']

def get_unreported_morning():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['morning']['sent']:
        return []
    reported = set(state[today]['morning']['todos'].keys())
    return [m for m in MANAGERS if m not in reported]

# ── 晚間狀態 ─────────────────────────────────────────────────
def mark_evening_sent():
    state = load_state()
    state, today = ensure_today(state)
    state[today]['evening']['sent'] = True
    save_state(state)

def set_waiting_evening(user_id, manager):
    state = load_state()
    state, today = ensure_today(state)
    state[today]['evening']['waiting'][user_id] = manager
    save_state(state)

def store_evening_report(user_id, text):
    state = load_state()
    state, today = ensure_today(state)
    evening = state[today]['evening']
    manager = evening['waiting'].get(user_id)
    if not manager:
        return None
    evening['reports'][manager] = text
    del evening['waiting'][user_id]
    save_state(state)
    return manager

def get_evening_reports():
    state = load_state()
    state, today = ensure_today(state)
    return state[today]['evening']['reports']

def get_unreported_evening():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['evening']['sent']:
        return []
    reported = set(state[today]['evening']['reports'].keys())
    return [m for m in MANAGERS if m not in reported]

# ── Flex Message 建立 ────────────────────────────────────────
def build_morning_prompt_flex():
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#06C755", "paddingAll": "lg",
            "contents": [{"type": "text", "text": "☀️ 早安！請回報今日待辦事項",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "md",
            "contents": [
                {"type": "text",
                 "text": "請點選自己的名字，再輸入今日待辦事項",
                 "size": "sm", "color": "#555555", "wrap": True},
                {"type": "text",
                 "text": "格式範例：\n1. 盤點配件庫存\n2. 同步官網庫存\n3. 整理展示機台",
                 "size": "sm", "color": "#888888", "wrap": True, "margin": "md"},
                {"type": "separator", "margin": "md"},
                *[{
                    "type": "button",
                    "action": {"type": "postback",
                               "label": f"📋 我是 {mgr}，開始回報",
                               "data": f"action=morning_id&manager={mgr}"},
                    "style": "primary", "color": "#06C755", "margin": "sm", "height": "sm"
                } for mgr in MANAGERS]
            ]
        }
    }
    return FlexMessage(alt_text="早安！請回報今日待辦事項",
                       contents=FlexContainer.from_dict(bubble))

def build_morning_summary_flex(todos):
    today = today_key()
    rows = []
    for mgr in MANAGERS:
        if mgr in todos:
            rows.append({"type": "text", "text": f"✅ {mgr}",
                         "weight": "bold", "size": "sm", "color": "#06C755"})
            for line in todos[mgr].strip().split('\n'):
                if line.strip():
                    rows.append({"type": "text", "text": line.strip(),
                                 "size": "sm", "color": "#444444",
                                 "wrap": True, "margin": "xs"})
        else:
            rows.append({"type": "text", "text": f"❌ {mgr} 尚未回報",
                         "weight": "bold", "size": "sm", "color": "#AAAAAA"})
        rows.append({"type": "separator", "margin": "md"})
    if rows and rows[-1].get("type") == "separator":
        rows.pop()

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A73E8", "paddingAll": "lg",
            "contents": [{"type": "text", "text": f"📋 {today} 今日待辦彙整",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "sm",
            "contents": rows
        }
    }
    return FlexMessage(alt_text=f"{today} 今日待辦彙整",
                       contents=FlexContainer.from_dict(bubble))

def build_evening_prompt_flex():
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#5C5CE6", "paddingAll": "lg",
            "contents": [{"type": "text", "text": "🌙 請回報今日完成狀況",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "md",
            "contents": [
                {"type": "text",
                 "text": "請點選自己的名字，再逐項回報完成狀況",
                 "size": "sm", "color": "#555555", "wrap": True},
                {"type": "text",
                 "text": "格式範例：\n✅ 盤點配件庫存\n✅ 同步官網庫存\n❌ 整理展示機台（客人太多）",
                 "size": "sm", "color": "#888888", "wrap": True, "margin": "md"},
                {"type": "separator", "margin": "md"},
                *[{
                    "type": "button",
                    "action": {"type": "postback",
                               "label": f"📝 我是 {mgr}，開始回報",
                               "data": f"action=evening_id&manager={mgr}"},
                    "style": "primary", "color": "#5C5CE6", "margin": "sm", "height": "sm"
                } for mgr in MANAGERS]
            ]
        }
    }
    return FlexMessage(alt_text="請回報今日完成狀況",
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
                    rows.append({"type": "text", "text": line.strip(),
                                 "size": "sm", "color": "#444444",
                                 "wrap": True, "margin": "xs"})
        else:
            rows.append({"type": "text", "text": mgr,
                         "weight": "bold", "size": "sm"})
            rows.append({"type": "text", "text": "⏳ 未回報",
                         "size": "sm", "color": "#AAAAAA", "margin": "xs"})
        rows.append({"type": "separator", "margin": "md"})
    if rows and rows[-1].get("type") == "separator":
        rows.pop()

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#5C5CE6", "paddingAll": "lg",
            "contents": [{"type": "text", "text": f"🌙 {today} 今日完成狀況彙整",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "sm",
            "contents": rows
        }
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
        push(build_morning_prompt_flex())
    except Exception as e:
        logger.error(f'早晨提示發送失敗：{e}')

def send_morning_reminder():
    unreported = get_unreported_morning()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日待辦事項，請在 11:00 彙整前完成！"))
        logger.info(f'早晨提醒發送：{names}')
    except Exception as e:
        logger.error(f'早晨提醒失敗：{e}')

def send_morning_summary():
    todos = get_morning_todos()
    try:
        push(build_morning_summary_flex(todos))
        logger.info('早晨彙整卡發送完成')
    except Exception as e:
        logger.error(f'早晨彙整卡失敗：{e}')

def send_evening_prompt():
    logger.info('發送晚間完成狀況回報提示')
    try:
        mark_evening_sent()
        push(build_evening_prompt_flex())
    except Exception as e:
        logger.error(f'晚間提示發送失敗：{e}')

def send_evening_reminder():
    unreported = get_unreported_evening()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日完成狀況，請在 23:59 前完成！"))
        logger.info(f'晚間提醒發送：{names}')
    except Exception as e:
        logger.error(f'晚間提醒失敗：{e}')

def send_evening_summary():
    reports = get_evening_reports()
    try:
        push(build_evening_summary_flex(reports))
        logger.info('晚間彙整卡發送完成')
    except Exception as e:
        logger.error(f'晚間彙整卡失敗：{e}')

# ── Webhook ──────────────────────────────────────────────────
@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200

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

@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    try:
        params = dict(p.split('=', 1) for p in data.split('&') if '=' in p)
    except Exception:
        return

    action  = params.get('action')
    manager = params.get('manager')
    user_id = event.source.user_id

    if manager not in MANAGERS:
        return

    if action == 'morning_id':
        set_waiting_morning(user_id, manager)
        reply(event.reply_token,
              f"👋 {manager}，請直接在群組輸入今日待辦事項（可多行）：\n\n範例：\n1. 盤點配件庫存\n2. 同步官網庫存\n3. 整理展示機台")

    elif action == 'evening_id':
        set_waiting_evening(user_id, manager)
        reply(event.reply_token,
              f"👋 {manager}，請直接在群組逐項回報完成狀況：\n\n範例：\n✅ 盤點配件庫存\n✅ 同步官網庫存\n❌ 整理展示機台（客人太多）")

@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else 'N/A'
    logger.info(f'機器人加入群組！Group ID = {group_id}')
    reply(event.reply_token,
          f"大家好！我是艾薇AI助理 🤖\n"
          f"09:00 發送待辦回報 → 10:00 提醒 → 11:00 彙整早報\n"
          f"21:00 發送完成回報 → 23:00 提醒 → 00:00 彙整晚報\n\n"
          f"📋 Group ID：\n{group_id}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    group_id = getattr(event.source, 'group_id', None)
    if not group_id:
        return
    user_id = event.source.user_id
    text    = event.message.text.strip()

    state = load_state()
    state, today = ensure_today(state)

    # 早晨待辦收集
    if user_id in state[today]['morning']['waiting']:
        manager = store_morning_todos(user_id, text)
        if manager:
            reply(event.reply_token,
                  f"✅ 已記錄 {manager} 的今日待辦事項！\n11:00 將彙整今日所有待辦。")
        return

    # 晚間完成回報收集
    if user_id in state[today]['evening']['waiting']:
        manager = store_evening_report(user_id, text)
        if manager:
            reply(event.reply_token,
                  f"✅ 已記錄 {manager} 的今日完成狀況！\n00:00 將發送今日彙整報告。")
        return

# ── 排程器 ───────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=TZ)
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
