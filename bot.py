import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"

# For a real rollout set TEST_MODE=false and specify Telegram IDs.
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
DRIVER_TELEGRAM_ID = os.environ.get("DRIVER_TELEGRAM_ID")
LOGISTICIAN_TELEGRAM_ID = os.environ.get("LOGISTICIAN_TELEGRAM_ID")

# Tech spec says idle > 10 minutes. Keep 600 in production.
WAIT_THRESHOLD_SECONDS = int(os.environ.get("WAIT_THRESHOLD_SECONDS", "600"))

ROUTE_NAME = "СПб → МСК"
ROUTE_POINTS = [
    {
        "address": "МО, Каширское шоссе, 14",
        "contact": "+7 900 000-00-01",
        "window": "14:00–16:00",
        "comment": "Позвонить за 15 минут до прибытия",
    },
    {
        "address": "Москва, Варшавское шоссе, 95",
        "contact": "+7 900 000-00-02",
        "window": "17:00–19:00",
        "comment": "Въезд через КПП №2",
    },
]

USERS = {}
SEEN_UPDATES = set()
RUNTIME_LOGISTICIAN_CHAT_ID = None
EVENTS = []


def now_ts():
    return int(time.time())


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def api(method, payload=None):
    payload = payload or {}
    encoded = {}
    for k, v in payload.items():
        encoded[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    data = urllib.parse.urlencode(encoded).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=65) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print("Telegram HTTP error:", e.code, body, flush=True)
        return {"ok": False, "error_code": e.code, "description": body}
    except Exception as e:
        print("Telegram API error:", repr(e), flush=True)
        return {"ok": False, "description": repr(e)}


def send(chat_id, text, inline_keyboard=None, reply_keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif reply_keyboard is not None:
        payload["reply_markup"] = reply_keyboard
    return api("sendMessage", payload)


def edit(chat_id, message_id, text, inline_keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": inline_keyboard or []},
    }
    return api("editMessageText", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    api("answerCallbackQuery", payload)


def remove_reply_keyboard(chat_id, text="✅ Геопозиция получена"):
    send(
        chat_id,
        text,
        reply_keyboard={"remove_keyboard": True},
    )


def location_keyboard():
    return {
        "keyboard": [[{"text": "📍 Отправить геопозицию", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def role_keyboard():
    return [
        [{"text": "🚚 Водитель", "callback_data": "role:driver"}],
        [{"text": "👤 Логист", "callback_data": "role:logistician"}],
    ]


def driver_home_keyboard():
    return [
        [{"text": "▶️ Начать маршрут", "callback_data": "driver:start_route"}],
        [{"text": "📍 Мои точки", "callback_data": "driver:points"}],
        [{"text": "📄 Документы", "callback_data": "driver:documents"}],
    ]


def documents_keyboard():
    return [
        [{"text": "📄 Загрузить ТТН", "callback_data": "docs:ttn"}],
        [{"text": "📝 Загрузить акт", "callback_data": "docs:act"}],
        [{"text": "📦 Загрузить фото контейнера", "callback_data": "docs:container"}],
        [{"text": "✅ Проверить комплект", "callback_data": "docs:check"}],
        [{"text": "📋 Что уже загружено", "callback_data": "docs:list"}],
        [{"text": "↩️ Назад к маршруту", "callback_data": "role:driver"}],
    ]


def point_keyboard():
    return [
        [{"text": "🚗 В пути", "callback_data": "point:en_route"}],
        [{"text": "📍 Прибыл", "callback_data": "point:arrived"}],
        [{"text": "⚠️ Проблема", "callback_data": "point:problem"}],
    ]


def arrived_keyboard():
    return [
        [{"text": "✅ Доставлено", "callback_data": "point:delivered"}],
        [{"text": "⚠️ Проблема", "callback_data": "point:problem"}],
    ]


def problem_keyboard():
    return [
        [{"text": "📞 Клиент не отвечает", "callback_data": "problem:no_answer"}],
        [{"text": "⏳ Долгая разгрузка", "callback_data": "problem:long_unload"}],
        [{"text": "📍 Неверный адрес", "callback_data": "problem:wrong_address"}],
        [{"text": "🔧 Поломка", "callback_data": "problem:breakdown"}],
        [{"text": "✍️ Другое", "callback_data": "problem:other"}],
        [{"text": "↩️ Назад", "callback_data": "driver:current_point"}],
    ]


def logist_home_keyboard():
    return [
        [{"text": "📅 Сегодня", "callback_data": "log:today"}],
        [{"text": "🚚 Водители", "callback_data": "log:drivers"}],
        [{"text": "⚠️ Активные проблемы", "callback_data": "log:problems"}],
        [{"text": "📄 Документы", "callback_data": "log:documents"}],
        [{"text": "📊 Отчёт", "callback_data": "log:report"}],
    ]


def logist_decision_keyboard(driver_chat_id):
    return [
        [{"text": "⏳ Ждать", "callback_data": f"logact:wait:{driver_chat_id}"}],
        [{"text": "➡️ Ехать дальше", "callback_data": f"logact:next:{driver_chat_id}"}],
        [{"text": "❌ Отменить точку", "callback_data": f"logact:cancel:{driver_chat_id}"}],
    ]


def user_state(chat_id):
    if chat_id not in USERS:
        USERS[chat_id] = {
            "role": None,
            "route_started": False,
            "route_started_at": None,
            "current_point": 0,
            "stage": "not_started",
            "arrival_ts": None,
            "awaiting_location_action": None,
            "awaiting_photo": False,
            "awaiting_other_problem": False,
            "wait_notified": False,
            "active_problem": None,
            "completed_points": 0,
            "document_mode": False,
            "awaiting_document_type": None,
            "documents": {},
            "document_status": "DOCUMENTS_NOT_STARTED",
        }
    return USERS[chat_id]


def log_event(chat_id, event, detail=""):
    EVENTS.append(
        {
            "chat_id": chat_id,
            "event": event,
            "detail": detail,
            "ts": now_ts(),
        }
    )


def route_summary():
    return (
        f"🚚 Маршрут на сегодня готов\n"
        f"{ROUTE_NAME}\n"
        f"{len(ROUTE_POINTS)} точки доставки"
    )


def point_text(state):
    idx = state["current_point"]
    if idx >= len(ROUTE_POINTS):
        return "🏁 Все точки завершены"
    p = ROUTE_POINTS[idx]
    return (
        f"📍 Точка {idx + 1} из {len(ROUTE_POINTS)}\n\n"
        f"Адрес: {p['address']}\n"
        f"Контакт: {p['contact']}\n"
        f"Окно доставки: {p['window']}\n"
        f"Комментарий: {p['comment']}"
    )


def determine_role(chat_id):
    sid = str(chat_id)
    if DRIVER_TELEGRAM_ID and sid == DRIVER_TELEGRAM_ID:
        return "driver"
    if LOGISTICIAN_TELEGRAM_ID and sid == LOGISTICIAN_TELEGRAM_ID:
        return "logistician"
    return None


def start_screen(chat_id):
    state = user_state(chat_id)
    role = determine_role(chat_id)
    if role:
        state["role"] = role
        if role == "driver":
            show_driver_home(chat_id)
        else:
            remember_logistician(chat_id)
            show_logistician_home(chat_id)
        return

    if TEST_MODE:
        send(
            chat_id,
            "Привет, это тестовый бот компании FENIX LOGISTIC.\n"
            "Выбери свою роль",
            inline_keyboard=role_keyboard(),
        )
    else:
        send(chat_id, "⛔ Ваш Telegram ID не зарегистрирован в системе.")


def remember_logistician(chat_id):
    global RUNTIME_LOGISTICIAN_CHAT_ID
    RUNTIME_LOGISTICIAN_CHAT_ID = chat_id


def logist_chat_id(fallback_driver_chat_id):
    if LOGISTICIAN_TELEGRAM_ID:
        return int(LOGISTICIAN_TELEGRAM_ID)
    if RUNTIME_LOGISTICIAN_CHAT_ID:
        return RUNTIME_LOGISTICIAN_CHAT_ID
    # In one-account UX testing, show the logistician notification in the same chat.
    return fallback_driver_chat_id


def show_driver_home(chat_id, message_id=None):
    state = user_state(chat_id)
    state["document_mode"] = False
    state["awaiting_document_type"] = None
    text = "🚚 Добро пожаловать\n\n" + route_summary()
    if message_id:
        edit(chat_id, message_id, text, driver_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=driver_home_keyboard())


def show_current_point(chat_id, message_id=None):
    state = user_state(chat_id)
    if state["current_point"] >= len(ROUTE_POINTS):
        finish_route(chat_id, message_id)
        return
    text = point_text(state)
    keyboard = arrived_keyboard() if state["stage"] == "arrived" else point_keyboard()
    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, inline_keyboard=keyboard)


def show_logistician_home(chat_id, message_id=None):
    remember_logistician(chat_id)
    text = "👤 ЛОГИСТ\n\nВыберите раздел:"
    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def request_location(chat_id, action, prompt):
    state = user_state(chat_id)
    state["awaiting_location_action"] = action
    send(chat_id, prompt, reply_keyboard=location_keyboard())


def handle_location(chat_id, location):
    state = user_state(chat_id)
    action = state.get("awaiting_location_action")
    if not action:
        send(chat_id, "📍 Геопозиция получена, но сейчас она не запрашивалась.")
        return

    state["awaiting_location_action"] = None
    lat = location.get("latitude")
    lon = location.get("longitude")
    log_event(chat_id, "location", f"{lat},{lon} action={action}")
    remove_reply_keyboard(chat_id)

    if action == "start_route":
        state["route_started"] = True
        state["route_started_at"] = now_ts()
        state["stage"] = "started"
        log_event(chat_id, "route_started")
        send(
            chat_id,
            "✅ Маршрут начат\nХорошей дороги",
            inline_keyboard=[[{"text": "➡️ Следующая точка", "callback_data": "driver:current_point"}]],
        )
    elif action == "en_route":
        state["stage"] = "en_route"
        log_event(chat_id, "en_route", f"point={state['current_point'] + 1}")
        send(
            chat_id,
            "🚗 Статус «В пути» зафиксирован.",
            inline_keyboard=[[{"text": "📍 Открыть точку", "callback_data": "driver:current_point"}]],
        )
    elif action == "arrived":
        state["stage"] = "arrived"
        state["arrival_ts"] = now_ts()
        state["wait_notified"] = False
        log_event(chat_id, "arrived", f"point={state['current_point'] + 1}")
        send(
            chat_id,
            f"📍 Прибытие зафиксировано в {fmt_time(state['arrival_ts'])}\n"
            f"⏱️ Таймер ожидания запущен.",
            inline_keyboard=arrived_keyboard(),
        )


def problem_label(code):
    return {
        "no_answer": "Клиент не отвечает",
        "long_unload": "Долгая разгрузка",
        "wrong_address": "Неверный адрес",
        "breakdown": "Поломка",
    }.get(code, code)


def notify_logistician(driver_chat_id, reason, waiting_minutes=None):
    state = user_state(driver_chat_id)
    idx = state["current_point"]
    point = ROUTE_POINTS[idx] if idx < len(ROUTE_POINTS) else ROUTE_POINTS[-1]
    state["active_problem"] = reason
    wait_line = (
        f"\n⏱️ Ожидание: {waiting_minutes} мин."
        if waiting_minutes is not None
        else ""
    )
    target = logist_chat_id(driver_chat_id)
    prefix = ""
    if target == driver_chat_id and not LOGISTICIAN_TELEGRAM_ID:
        prefix = "🧪 TEST: так это уведомление увидит логист\n\n"

    send(
        target,
        prefix
        + "⚠️ Проблема на точке\n\n"
        + f"Водитель: TEST DRIVER\n"
        + f"Маршрут: {ROUTE_NAME}\n"
        + f"Адрес: {point['address']}\n"
        + f"Причина: {reason}"
        + wait_line,
        inline_keyboard=logist_decision_keyboard(driver_chat_id),
    )
    log_event(driver_chat_id, "problem_notified", reason)


def handle_photo(chat_id, message):
    state = user_state(chat_id)

    if handle_document_photo(chat_id, message):
        return

    if not state.get("awaiting_photo"):
        send(
            chat_id,
            "📸 Фото получено.\n"
            "Если это документ, сначала откройте раздел «📄 Документы» и выберите его тип."
        )
        return

    state["awaiting_photo"] = False
    idx = state["current_point"]
    arrival = state.get("arrival_ts")
    dwell_minutes = max(0, int((now_ts() - arrival) / 60)) if arrival else 0

    state["completed_points"] += 1
    state["stage"] = "delivered"
    state["active_problem"] = None
    state["wait_notified"] = False
    log_event(chat_id, "delivered", f"point={idx + 1}; dwell={dwell_minutes}")

    state["current_point"] += 1
    state["arrival_ts"] = None

    if state["current_point"] >= len(ROUTE_POINTS):
        send(
            chat_id,
            f"✅ Доставка завершена\n"
            f"⏱️ Время на точке: {dwell_minutes} минут\n\n"
            f"🏁 Маршрут завершён\nСпасибо за работу",
        )
        state["stage"] = "finished"
        log_event(chat_id, "route_finished")
    else:
        state["stage"] = "between_points"
        send(
            chat_id,
            f"✅ Доставка завершена\n"
            f"⏱️ Время на точке: {dwell_minutes} минут",
            inline_keyboard=[[{"text": "➡️ Следующая точка", "callback_data": "driver:current_point"}]],
        )



REQUIRED_DOCS = ("ttn", "act", "container")
DOC_LABELS = {
    "ttn": "ТТН",
    "act": "Акт",
    "container": "Фото контейнера",
}


def documents_status_text(state):
    lines = ["📄 Комплект документов:"]
    for code in REQUIRED_DOCS:
        mark = "✅" if code in state["documents"] else "❌"
        lines.append(f"{mark} {DOC_LABELS[code]}")
    return "\n".join(lines)


def show_documents_home(chat_id, message_id=None):
    state = user_state(chat_id)
    state["document_mode"] = True
    text = (
        "📄 Документы по перевозке\n\n"
        "Обязательный комплект:\n"
        "• ТТН\n"
        "• Акт\n"
        "• Фото контейнера\n\n"
        "Для теста сначала выберите тип документа, затем отправьте его фото."
    )
    if message_id:
        edit(chat_id, message_id, text, documents_keyboard())
    else:
        send(chat_id, text, inline_keyboard=documents_keyboard())


def evaluate_documents(chat_id, notify=True):
    state = user_state(chat_id)
    missing = [code for code in REQUIRED_DOCS if code not in state["documents"]]

    if missing:
        state["document_status"] = "DOCUMENTS_INCOMPLETE"
        if notify:
            missing_text = "\n".join(f"❌ {DOC_LABELS[code]}" for code in missing)
            send(
                chat_id,
                documents_status_text(state)
                + "\n\nНе хватает:\n"
                + missing_text,
                inline_keyboard=documents_keyboard(),
            )
        return False

    if state["document_status"] != "DOCUMENTS_COMPLETE":
        state["document_status"] = "DOCUMENTS_COMPLETE"
        log_event(chat_id, "documents_complete")
        send(
            chat_id,
            "✅ Комплект документов полный\n\n"
            + documents_status_text(state)
            + "\n\n"
            + "✅ Логист уведомлён\n"
            + "✉️ Отправку письма подключим позже\n"
            + "🚚 Статус перевозки: ДОКУМЕНТЫ ПОЛНЫЕ",
            inline_keyboard=driver_home_keyboard(),
        )

        target = logist_chat_id(chat_id)
        prefix = ""
        if target == chat_id and not LOGISTICIAN_TELEGRAM_ID:
            prefix = "🧪 TEST: уведомление для логиста\n\n"

        send(
            target,
            prefix
            + "📄 Комплект документов полный\n\n"
            + "Водитель: TEST DRIVER\n"
            + f"Маршрут: {ROUTE_NAME}\n\n"
            + documents_status_text(state)
            + "\n\n"
            + "Статус: ✅ ДОКУМЕНТЫ ПОЛНЫЕ\n"
            + "✉️ Письмо пока не отправляется.",
        )

    return True


def show_log_documents(chat_id, message_id=None):
    blocks = []
    for cid, st in USERS.items():
        if st.get("documents") or st.get("document_status") != "DOCUMENTS_NOT_STARTED":
            blocks.append(
                "🚚 TEST DRIVER\n"
                + documents_status_text(st)
                + "\n"
                + f"Статус: {st['document_status']}"
            )

    text = "📄 Документы\n\n" + (
        "\n\n".join(blocks) if blocks else "Документы ещё не загружались."
    )

    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def handle_document_photo(chat_id, message):
    state = user_state(chat_id)
    doc_type = state.get("awaiting_document_type")

    if not state.get("document_mode") or not doc_type:
        return False

    state["awaiting_document_type"] = None
    state["documents"][doc_type] = {
        "received_at": now_ts(),
        "telegram_message_id": message.get("message_id"),
    }
    state["document_status"] = "DOCUMENTS_COLLECTING"
    log_event(chat_id, "document_received", doc_type)

    send(
        chat_id,
        f"✅ Получено: {DOC_LABELS[doc_type]}\n\n"
        + documents_status_text(state),
        inline_keyboard=documents_keyboard(),
    )

    evaluate_documents(chat_id, notify=False)
    return True


def finish_route(chat_id, message_id=None):
    text = "🏁 Маршрут завершён\nСпасибо за работу"
    if message_id:
        edit(chat_id, message_id, text, [])
    else:
        send(chat_id, text)


def handle_text(chat_id, text):
    state = user_state(chat_id)

    if text in ("/start", "/старт"):
        start_screen(chat_id)
        return

    if text in ("/маршрут", "/route"):
        show_driver_home(chat_id)
        return

    if text in ("/следующая", "/следующая_точка", "/next"):
        show_current_point(chat_id)
        return

    if text in ("/финиш", "/finish"):
        finish_route(chat_id)
        return

    if text in ("/сегодня", "/today"):
        show_today(chat_id)
        return

    if text in ("/водители", "/drivers"):
        show_drivers(chat_id)
        return

    if text in ("/проблема", "/problem"):
        show_problems(chat_id)
        return

    if text in ("/отчет", "/report"):
        show_report(chat_id)
        return

    if state.get("awaiting_other_problem"):
        state["awaiting_other_problem"] = False
        reason = text.strip()[:500] or "Другая проблема"
        notify_logistician(chat_id, reason)
        send(chat_id, "✅ Проблема передана логисту.")
        return

    send(
        chat_id,
        "В этой версии свободный чат отключён.\n"
        "Используйте кнопки или /start."
    )


def show_today(chat_id, message_id=None):
    active = []
    for cid, st in USERS.items():
        if st.get("role") == "driver" and st.get("route_started"):
            active.append(
                f"• TEST DRIVER — точка {min(st['current_point'] + 1, len(ROUTE_POINTS))}/"
                f"{len(ROUTE_POINTS)}, статус: {st['stage']}"
            )
    text = "📅 Маршруты сегодня\n\n" + ("\n".join(active) if active else "Активных маршрутов пока нет.")
    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def show_drivers(chat_id, message_id=None):
    drivers = []
    for cid, st in USERS.items():
        if st.get("role") == "driver":
            drivers.append(f"• TEST DRIVER — {st['stage']}")
    text = "🚚 Статус водителей\n\n" + ("\n".join(drivers) if drivers else "Нет данных.")
    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def show_problems(chat_id, message_id=None):
    problems = []
    for cid, st in USERS.items():
        if st.get("active_problem"):
            problems.append(f"• TEST DRIVER — {st['active_problem']}")
    text = "⚠️ Активные проблемы\n\n" + ("\n".join(problems) if problems else "Активных проблем нет.")
    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def show_report(chat_id, message_id=None):
    delivered = sum(st.get("completed_points", 0) for st in USERS.values())
    problem_events = sum(1 for e in EVENTS if e["event"] == "problem_notified")
    text = (
        "📊 Отчёт за день\n\n"
        f"Завершено точек: {delivered}\n"
        f"Проблем зарегистрировано: {problem_events}\n"
        f"Событий в журнале: {len(EVENTS)}"
    )
    if message_id:
        edit(chat_id, message_id, text, logist_home_keyboard())
    else:
        send(chat_id, text, inline_keyboard=logist_home_keyboard())


def handle_callback(q):
    data = q["data"]
    chat_id = q["message"]["chat"]["id"]
    message_id = q["message"]["message_id"]
    state = user_state(chat_id)
    answer_callback(q["id"])

    if data == "role:driver":
        state["role"] = "driver"
        show_driver_home(chat_id, message_id)
        return

    if data == "role:logistician":
        state["role"] = "logistician"
        remember_logistician(chat_id)
        show_logistician_home(chat_id, message_id)
        return

    if data == "driver:documents":
        show_documents_home(chat_id, message_id)
        return

    if data in ("docs:ttn", "docs:act", "docs:container"):
        code = data.split(":", 1)[1]
        state["document_mode"] = True
        state["awaiting_document_type"] = code
        send(
            chat_id,
            f"📷 Отправьте фото: {DOC_LABELS[code]}",
            inline_keyboard=documents_keyboard(),
        )
        return

    if data == "docs:check":
        evaluate_documents(chat_id, notify=True)
        return

    if data == "docs:list":
        send(
            chat_id,
            documents_status_text(state),
            inline_keyboard=documents_keyboard(),
        )
        return

    if data == "driver:start_route":
        request_location(
            chat_id,
            "start_route",
            "▶️ Начинаем маршрут.\n\n📍 Отправьте текущую геопозицию."
        )
        return

    if data == "driver:points":
        points = "\n\n".join(
            f"{i + 1}. {p['address']}\nОкно: {p['window']}"
            for i, p in enumerate(ROUTE_POINTS)
        )
        edit(
            chat_id,
            message_id,
            "📍 Мои точки\n\n" + points,
            [[{"text": "↩️ Назад", "callback_data": "role:driver"}]],
        )
        return

    if data == "driver:current_point":
        show_current_point(chat_id, message_id)
        return

    if data == "point:en_route":
        request_location(
            chat_id,
            "en_route",
            "🚗 Статус «В пути».\n\n📍 Отправьте геопозицию."
        )
        return

    if data == "point:arrived":
        request_location(
            chat_id,
            "arrived",
            "📍 Для фиксации прибытия отправьте геопозицию."
        )
        return

    if data == "point:delivered":
        state["awaiting_photo"] = True
        send(
            chat_id,
            "📸 Загрузите фото подтверждения доставки (минимум 1)."
        )
        return

    if data == "point:problem":
        edit(chat_id, message_id, "⚠️ Что произошло?", problem_keyboard())
        return

    if data.startswith("problem:"):
        code = data.split(":", 1)[1]
        if code == "other":
            state["awaiting_other_problem"] = True
            send(chat_id, "✍️ Опишите проблему одним коротким сообщением.")
            return
        reason = problem_label(code)
        notify_logistician(chat_id, reason)
        send(chat_id, "✅ Проблема передана логисту.")
        return

    if data == "log:today":
        show_today(chat_id, message_id)
        return
    if data == "log:drivers":
        show_drivers(chat_id, message_id)
        return
    if data == "log:problems":
        show_problems(chat_id, message_id)
        return
    if data == "log:documents":
        show_log_documents(chat_id, message_id)
        return

    if data == "log:report":
        show_report(chat_id, message_id)
        return

    if data.startswith("logact:"):
        _, action, driver_id_text = data.split(":", 2)
        driver_id = int(driver_id_text)
        driver_state = user_state(driver_id)

        if action == "wait":
            driver_state["active_problem"] = None
            send(driver_id, "👤 Решение логиста: ⏳ продолжайте ждать.")
            edit(chat_id, message_id, q["message"]["text"] + "\n\n✅ Решение: ЖДАТЬ", [])
            log_event(driver_id, "logistician_decision", "wait")
            return

        if action in ("next", "cancel"):
            decision = "➡️ ехать дальше" if action == "next" else "❌ точка отменена"
            driver_state["active_problem"] = None
            driver_state["current_point"] += 1
            driver_state["arrival_ts"] = None
            driver_state["stage"] = "between_points"
            send(
                driver_id,
                f"👤 Решение логиста: {decision}.",
                inline_keyboard=(
                    [[{"text": "➡️ Следующая точка", "callback_data": "driver:current_point"}]]
                    if driver_state["current_point"] < len(ROUTE_POINTS)
                    else None
                ),
            )
            edit(chat_id, message_id, q["message"]["text"] + f"\n\n✅ Решение: {decision}", [])
            log_event(driver_id, "logistician_decision", action)
            return


def check_wait_timers():
    now = now_ts()
    for chat_id, state in list(USERS.items()):
        if state.get("stage") != "arrived":
            continue
        arrival = state.get("arrival_ts")
        if not arrival or state.get("wait_notified"):
            continue
        elapsed = now - arrival
        if elapsed >= WAIT_THRESHOLD_SECONDS:
            state["wait_notified"] = True
            minutes = max(1, int(elapsed / 60))
            notify_logistician(chat_id, "Простой на точке", minutes)
            send(
                chat_id,
                f"⏱️ Ожидание превысило {int(WAIT_THRESHOLD_SECONDS / 60)} минут.\n"
                "Логист уведомлён."
            )


def handle_message(message):
    chat_id = message["chat"]["id"]
    if "location" in message:
        handle_location(chat_id, message["location"])
        return
    if "photo" in message:
        handle_photo(chat_id, message)
        return
    text = message.get("text", "")
    handle_text(chat_id, text)


def main():
    # Ensure long polling is the only update mechanism and discard stale test clicks.
    api("deleteWebhook", {"drop_pending_updates": True})
    print("Fenix simple TZ v1 started", flush=True)
    print(f"TEST_MODE={TEST_MODE}, WAIT_THRESHOLD_SECONDS={WAIT_THRESHOLD_SECONDS}", flush=True)

    offset = None
    while True:
        try:
            check_wait_timers()

            payload = {"timeout": 25}
            if offset is not None:
                payload["offset"] = offset

            result = api("getUpdates", payload)
            if not result.get("ok"):
                time.sleep(3)
                continue

            for update in result.get("result", []):
                update_id = update["update_id"]
                offset = update_id + 1

                if update_id in SEEN_UPDATES:
                    continue
                SEEN_UPDATES.add(update_id)
                if len(SEEN_UPDATES) > 1000:
                    SEEN_UPDATES.clear()
                    SEEN_UPDATES.add(update_id)

                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])

        except Exception as e:
            print("Main loop error:", repr(e), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
