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

TZ       = pytz.timezone('Asia/Taipei')
MANAGERS = ['小陳', 'Hank', '小羊']
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
            'morning': {'sent': False, 'reported': []},
            'evening': {'sent': False, 'completed': [], 'incomplete': [], 'reasons': {}},
            'waiting_reason': {},
        }
    return state, today

def mark_morning_reported(manager):
    state = load_state()
    state, today = ensure_today(state)
    if manager not in state[today]['morning']['reported']:
        state[today]['morning']['reported'].append(manager)
    save_state(state)

def mark_evening_completed(manager):
    state = load_state()
    state, today = ensure_today(state)
    ev = state[today]['evening']
    if manager not in ev['completed']:
        ev['completed'].append(manager)
    state[today]['waiting_reason'] = {
        uid: m for uid, m in state[today]['waiting_reason'].items() if m != manager
    }
    save_state(state)

def mark_evening_incomplete(manager, user_id):
    state = load_state()
    state, today = ensure_today(state)
    state[today]['waiting_reason'][user_id] = manager
    save_state(state)

def record_reason(user_id, reason_text):
    state = load_state()
    state, today = ensure_today(state)
    manager = state[today]['waiting_reason'].get(user_id)
    if not manager:
        return None
    ev = state[today]['evening']
    ev['reasons'][manager] = reason_text
    if manager not in ev['incomplete']:
        ev['incomplete'].append(manager)
    del state[today]['waiting_reason'][user_id]
    save_state(state)
    return manager, reason_text

def mark_sent(session):
    state = load_state()
    state, today = ensure_today(state)
    state[today][session]['sent'] = True
    if session == 'morning':
        state[today]['morning']['reported'] = []
    else:
        state[today]['evening']['completed'] = []
        state[today]['evening']['incomplete'] = []
        state[today]['evening']['reasons'] = {}
        state[today]['waiting_reason'] = {}
    save_state(state)

def get_unreported_morning():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['morning']['sent']:
        return []
    return [m for m in MANAGERS if m not in state[today]['morning']['reported']]

def get_unreported_evening():
    state = load_state()
    state, today = ensure_today(state)
    if not state[today]['evening']['sent']:
        return []
    ev = state[today]['evening']
    responded = set(ev['completed']) | set(ev['incomplete'])
    return [m for m in MANAGERS if m not in responded]

# ── Flex Message 建立 ────────────────────────────────────────
def build_morning_flex():
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#06C755", "paddingAll": "lg",
            "contents": [{"type": "text", "text": "☀️ 早安！今日工作待辦回報",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "md",
            "contents": [
                {"type": "text", "text": "各主管請點選自己的按鈕，確認已回報今日待辦事項",
                 "size": "sm", "color": "#555555", "wrap": True},
                {"type": "separator", "margin": "md"},
                *[{
                    "type": "button",
                    "action": {"type": "postback", "label": f"✅  {mgr} 已回報",
                               "data": f"action=morning&manager={mgr}"},
                    "style": "primary", "color": "#06C755", "margin": "sm", "height": "sm"
                } for mgr in MANAGERS]
            ]
        }
    }
    return FlexMessage(alt_text="☀️ 早安！今日工作待辦回報",
                       contents=FlexContainer.from_dict(bubble))

def build_evening_flex():
    rows = [
        {"type": "text", "text": "各主管請選擇完成狀況。若有未完成項目，點選後請在群組輸入原因。",
         "size": "sm", "color": "#555555", "wrap": True},
        {"type": "separator", "margin": "md"},
    ]
    for mgr in MANAGERS:
        rows += [
            {"type": "text", "text": f"▌ {mgr}", "weight": "bold", "size": "sm", "margin": "md"},
            {
                "type": "box", "layout": "horizontal", "margin": "sm",
                "contents": [
                    {"type": "button", "flex": 1, "height": "sm", "style": "primary",
                     "color": "#06C755", "margin": "sm",
                     "action": {"type": "postback", "label": "✅ 全部完成",
                                "data": f"action=evening_done&manager={mgr}"}},
                    {"type": "button", "flex": 1, "height": "sm", "style": "primary",
                     "color": "#FF6B35", "margin": "sm",
                     "action": {"type": "postback", "label": "⚠️ 有未完成",
                                "data": f"action=evening_incomplete&manager={mgr}"}},
                ]
            }
        ]
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#5C5CE6", "paddingAll": "lg",
            "contents": [{"type": "text", "text": "🌙 今日工作完成狀況回報",
                          "weight": "bold", "size": "md", "color": "#FFFFFF", "wrap": True}]
        },
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "lg", "spacing": "none",
            "contents": rows
        }
    }
    return FlexMessage(alt_text="🌙 今日工作完成狀況回報",
                       contents=FlexContainer.from_dict(bubble))

# ── 推播訊息工具 ──────────────────────────────────────────────
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
def send_morning_checkin():
    logger.info('發送早晨待辦回報問卷')
    try:
        mark_sent('morning')
        push(build_morning_flex())
    except Exception as e:
        logger.error(f'早晨問卷發送失敗：{e}')

def send_morning_reminder():
    unreported = get_unreported_morning()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日工作待辦事項，請盡快完成！"))
        logger.info(f'早晨提醒已發送：{names}')
    except Exception as e:
        logger.error(f'早晨提醒發送失敗：{e}')

def send_evening_checkin():
    logger.info('發送晚間完成狀況回報問卷')
    try:
        mark_sent('evening')
        push(build_evening_flex())
    except Exception as e:
        logger.error(f'晚間問卷發送失敗：{e}')

def send_evening_reminder():
    unreported = get_unreported_evening()
    if not unreported:
        return
    names = '、'.join(unreported)
    try:
        push(TextMessage(text=f"⏰ 提醒：{names} 尚未回報今日完成狀況，請在今天結束前完成回報！"))
        logger.info(f'晚間提醒已發送：{names}')
    except Exception as e:
        logger.error(f'晚間提醒發送失敗：{e}')

# ── Webhook ──────────────────────────────────────────────────
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

    if action == 'morning':
        mark_morning_reported(manager)
        reply(event.reply_token, f"✅ 已記錄 {manager} 的今日待辦回報，辛苦了！")

    elif action == 'evening_done':
        mark_evening_completed(manager)
        reply(event.reply_token, f"✅ 已記錄 {manager} 今日全部完成，太棒了！")

    elif action == 'evening_incomplete':
        mark_evening_incomplete(manager, user_id)
        reply(event.reply_token,
              f"⚠️ 已記錄 {manager} 有未完成項目。\n\n請直接在群組輸入未完成的原因，機器人會自動記錄。")

@handler.add(JoinEvent)
def handle_join(event):
    group_id = event.source.group_id if hasattr(event.source, 'group_id') else 'N/A'
    logger.info(f'✅ 機器人加入群組！Group ID = {group_id}')
    reply(event.reply_token,
          f"大家好！我是艾薇AI助理 🤖\n"
          f"我會在每天早上 9:00 和晚上 9:00 發送回報問卷。\n\n"
          f"📋 Group ID（設定用）：\n{group_id}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    group_id = getattr(event.source, 'group_id', None)
    if not group_id:
        return
    user_id = event.source.user_id
    text    = event.message.text.strip()
    result  = record_reason(user_id, text)
    if result:
        manager, reason = result
        reply(event.reply_token,
              f"📝 已記錄 {manager} 的未完成原因：\n「{reason}」\n謝謝說明！")

# ── 排程器 ───────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(send_morning_checkin,  'cron', hour=9,  minute=0)
scheduler.add_job(send_morning_reminder, 'cron', hour=10, minute=0)
scheduler.add_job(send_evening_checkin,  'cron', hour=21, minute=0)
scheduler.add_job(send_evening_reminder, 'cron', hour=23, minute=0)
scheduler.start()
logger.info('排程器已啟動')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
