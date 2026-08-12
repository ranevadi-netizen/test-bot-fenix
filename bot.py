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
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
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

REQUIRED_DOCS = ("ttn", "act", "container")
DOC_LABELS = {
    "ttn": "ТТН",
    "act": "Акт",
    "container": "Фото контейнера",
}

USERS = {}
SEEN_UPDATES = set()
EVENTS = []
RUNTIME_LOGISTICIAN_CHAT_ID = None


def now_ts():
    return int(time.time())


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def api(method, payload=None):
    payload = payload or {}
    encoded = {}
    for key, value in payload.items():
        encoded[key] = (
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else value
        )
    data = urllib.parse.urlencode(encoded).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)

    try:
        with urllib.request.urlopen(req, timeout=65) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print("Telegram HTTP error:", exc.code, body, flush=True)
        return {"ok": False, "description": body}
    except Exception as exc:
        print("Telegram API error:", repr(exc), flush=True)
        return {"ok": False, "description": repr(exc)}


def send(chat_id, text, inline_keyboard=None, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    elif reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)


def edit(chat_id, message_id, text, inline_keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": inline_keyboard or []},
    }
    return api("editMessageText", payload)


def delete_message(chat_id, message_id):
    try:
        api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass


def answer_callback(callback_id):
    api("answerCallbackQuery", {"callback_query_id": callback_id})


def state(chat_id):
    if chat_id not in USERS:
        USERS[chat_id] = {
            "role": None,
            "workspace_message_id": None,
            "route_started": False,
            "route_started_at": None,
            "current_point": 0,
            "stage": "not_started",
            "arrival_ts": None,
            "last_location": None,
            "last_location_at": None,
            "awaiting_location_action": None,
            "awaiting_delivery_photo": False,
            "awaiting_other_help": False,
            "active_help": None,
            "wait_notified": False,
            "completed_points": 0,
            "document_mode": False,
            "pending_document_photo": None,
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


def driver_menu():
    return {
        "keyboard": [
            [
                {"text": "🚚 Маршрут"},
                {"text": "📄 Документы"},
                {"text": "📞 Помощь"},
            ]
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def logistician_menu():
    return {
        "keyboard": [
            [{"text": "📅 Сегодня"}, {"text": "🚚 Водители"}],
            [{"text": "📄 Документы"}, {"text": "📞 Помощь"}],
            [{"text": "📊 Отчёт"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def location_menu():
    return {
        "keyboard": [[{"text": "📍 Отправить геопозицию", "request_location": True}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def start_role_keyboard():
    return [
        [{"text": "🚚 Водитель", "callback_data": "role:driver"}],
        [{"text": "👤 Логист", "callback_data": "role:logistician"}],
    ]


def route_start_keyboard():
    return [
        [{"text": "▶️ Начать маршрут", "callback_data": "route:start"}],
        [{"text": "📍 Мои точки", "callback_data": "route:points"}],
    ]


def route_point_keyboard(st):
    if st["stage"] == "arrived":
        return [[{"text": "✅ Доставлено", "callback_data": "route:delivered"}]]
    return [
        [{"text": "🚗 В пути", "callback_data": "route:en_route"}],
        [{"text": "📍 Прибыл", "callback_data": "route:arrived"}],
    ]


def help_keyboard():
    return [
        [{"text": "📞 Клиент не отвечает", "callback_data": "help:no_answer"}],
        [{"text": "⏳ Долгая разгрузка", "callback_data": "help:long_unload"}],
        [{"text": "📍 Неверный адрес", "callback_data": "help:wrong_address"}],
        [{"text": "🔧 Поломка", "callback_data": "help:breakdown"}],
        [{"text": "✍️ Другое", "callback_data": "help:other"}],
    ]


def document_classification_keyboard():
    return [
        [
            {"text": "📄 ТТН", "callback_data": "docclass:ttn"},
            {"text": "📝 Акт", "callback_data": "docclass:act"},
        ],
        [{"text": "📦 Фото контейнера", "callback_data": "docclass:container"}],
    ]


def logistician_decision_keyboard(driver_chat_id):
    return [
        [{"text": "⏳ Ждать", "callback_data": f"logact:wait:{driver_chat_id}"}],
        [{"text": "➡️ Ехать дальше", "callback_data": f"logact:next:{driver_chat_id}"}],
        [{"text": "❌ Отменить точку", "callback_data": f"logact:cancel:{driver_chat_id}"}],
    ]


def set_workspace(chat_id, text, inline_keyboard=None):
    st = state(chat_id)
    current_id = st.get("workspace_message_id")

    if current_id:
        result = edit(chat_id, current_id, text, inline_keyboard)
        if result.get("ok"):
            return current_id

    result = send(chat_id, text, inline_keyboard=inline_keyboard)
    if result.get("ok"):
        st["workspace_message_id"] = result["result"]["message_id"]
        return st["workspace_message_id"]
    return None


def install_role_menu(chat_id, role):
    if role == "driver":
        send(
            chat_id,
            "Меню водителя доступно внизу.",
            reply_markup=driver_menu(),
        )
    else:
        send(
            chat_id,
            "Меню логиста доступно внизу.",
            reply_markup=logistician_menu(),
        )


def start_screen(chat_id):
    st = state(chat_id)
    st["workspace_message_id"] = None
    greeting = (
        "Привет, это бот компании FENIX LOGISTIC. Это тестовая версия. "
        "Выбери свою роль"
    )
    result = send(chat_id, greeting, inline_keyboard=start_role_keyboard())
    if result.get("ok"):
        st["workspace_message_id"] = result["result"]["message_id"]


def route_summary():
    return (
        "🚚 Маршрут на сегодня готов\n"
        f"{ROUTE_NAME}\n"
        f"{len(ROUTE_POINTS)} точки доставки"
    )


def route_screen(chat_id):
    st = state(chat_id)
    st["document_mode"] = False
    st["pending_document_photo"] = None

    if not st["route_started"]:
        set_workspace(chat_id, route_summary(), route_start_keyboard())
        return

    if st["current_point"] >= len(ROUTE_POINTS):
        set_workspace(
            chat_id,
            "🏁 Маршрут завершён\nСпасибо за работу",
            [],
        )
        return

    point = ROUTE_POINTS[st["current_point"]]
    status = {
        "started": "Маршрут начат",
        "between_points": "Следующая точка",
        "en_route": "🚗 В пути",
        "arrived": "📍 Прибыл",
        "delivered": "✅ Доставлено",
    }.get(st["stage"], st["stage"])

    location_line = ""
    if st["last_location_at"]:
        location_line = f"\n📍 Последняя геопозиция: {fmt_time(st['last_location_at'])}"

    text = (
        f"📍 Точка {st['current_point'] + 1} из {len(ROUTE_POINTS)}\n\n"
        f"Адрес: {point['address']}\n"
        f"Контакт: {point['contact']}\n"
        f"Окно доставки: {point['window']}\n"
        f"Комментарий: {point['comment']}\n\n"
        f"Статус: {status}"
        f"{location_line}"
    )
    set_workspace(chat_id, text, route_point_keyboard(st))


def points_screen(chat_id):
    lines = []
    for index, point in enumerate(ROUTE_POINTS, start=1):
        lines.append(
            f"{index}. {point['address']}\n"
            f"Окно: {point['window']}"
        )
    set_workspace(chat_id, "📍 Мои точки\n\n" + "\n\n".join(lines), [])


def documents_status_text(st):
    lines = []
    for code in REQUIRED_DOCS:
        mark = "✅" if code in st["documents"] else "❌"
        lines.append(f"{mark} {DOC_LABELS[code]}")
    return "\n".join(lines)


def documents_screen(chat_id):
    st = state(chat_id)
    st["document_mode"] = True
    st["pending_document_photo"] = None

    missing = [code for code in REQUIRED_DOCS if code not in st["documents"]]
    if not missing:
        st["document_status"] = "DOCUMENTS_COMPLETE"
        text = (
            "📄 Документы по перевозке\n\n"
            f"{documents_status_text(st)}\n\n"
            "✅ Комплект документов полный\n"
            "✅ Логист уведомлён\n"
            "🚚 Статус: ДОКУМЕНТЫ ПОЛНЫЕ"
        )
    else:
        st["document_status"] = (
            "DOCUMENTS_COLLECTING" if st["documents"] else "DOCUMENTS_NOT_STARTED"
        )
        text = (
            "📄 Документы по перевозке\n\n"
            f"{documents_status_text(st)}\n\n"
            "Отправьте фото документа. После фото бот спросит, что это."
        )

    set_workspace(chat_id, text, [])


def help_screen(chat_id):
    st = state(chat_id)
    st["document_mode"] = False
    set_workspace(
        chat_id,
        "📞 Помощь\n\nВыберите, что произошло:",
        help_keyboard(),
    )


def request_location(chat_id, action, prompt):
    st = state(chat_id)
    st["awaiting_location_action"] = action
    send(chat_id, prompt, reply_markup=location_menu())


def restore_driver_menu(chat_id):
    send(
        chat_id,
        "Геопозиция получена. Основное меню снова доступно внизу.",
        reply_markup=driver_menu(),
    )


def handle_location(chat_id, location):
    st = state(chat_id)
    action = st.get("awaiting_location_action")
    if not action:
        return

    st["awaiting_location_action"] = None
    st["last_location"] = {
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
    }
    st["last_location_at"] = now_ts()
    log_event(chat_id, "location", action)
    restore_driver_menu(chat_id)

    if action == "start":
        st["route_started"] = True
        st["route_started_at"] = now_ts()
        st["stage"] = "started"
        log_event(chat_id, "route_started")
    elif action == "en_route":
        st["stage"] = "en_route"
        log_event(chat_id, "en_route", str(st["current_point"] + 1))
    elif action == "arrived":
        st["stage"] = "arrived"
        st["arrival_ts"] = now_ts()
        st["wait_notified"] = False
        log_event(chat_id, "arrived", str(st["current_point"] + 1))

    route_screen(chat_id)


def handle_delivery_photo(chat_id):
    st = state(chat_id)
    st["awaiting_delivery_photo"] = False

    arrival = st.get("arrival_ts")
    dwell_minutes = max(0, int((now_ts() - arrival) / 60)) if arrival else 0

    st["completed_points"] += 1
    st["current_point"] += 1
    st["arrival_ts"] = None
    st["active_help"] = None
    st["wait_notified"] = False
    log_event(chat_id, "delivered", f"dwell={dwell_minutes}")

    if st["current_point"] >= len(ROUTE_POINTS):
        st["stage"] = "finished"
        set_workspace(
            chat_id,
            f"✅ Доставка завершена\n"
            f"⏱️ Время на точке: {dwell_minutes} минут\n\n"
            f"🏁 Маршрут завершён\nСпасибо за работу",
            [],
        )
    else:
        st["stage"] = "between_points"
        route_screen(chat_id)


def handle_document_photo(chat_id, message):
    st = state(chat_id)
    photos = message.get("photo") or []
    if not photos:
        return

    st["pending_document_photo"] = {
        "file_id": photos[-1]["file_id"],
        "message_id": message.get("message_id"),
    }
    set_workspace(
        chat_id,
        "📄 Фото получено.\n\nЧто это за документ?",
        document_classification_keyboard(),
    )


def classify_document(chat_id, doc_type):
    st = state(chat_id)
    pending = st.get("pending_document_photo")
    if not pending:
        documents_screen(chat_id)
        return

    st["documents"][doc_type] = {
        "file_id": pending["file_id"],
        "received_at": now_ts(),
    }
    st["pending_document_photo"] = None
    log_event(chat_id, "document_received", doc_type)

    missing = [code for code in REQUIRED_DOCS if code not in st["documents"]]
    if missing:
        st["document_status"] = "DOCUMENTS_COLLECTING"
        set_workspace(
            chat_id,
            "📄 Документы по перевозке\n\n"
            f"{documents_status_text(st)}\n\n"
            f"✅ Получено: {DOC_LABELS[doc_type]}\n"
            "Отправьте следующее фото документа.",
            [],
        )
        return

    if st["document_status"] != "DOCUMENTS_COMPLETE":
        st["document_status"] = "DOCUMENTS_COMPLETE"
        log_event(chat_id, "documents_complete")
        notify_documents_complete(chat_id)

    documents_screen(chat_id)


def logist_chat_id(driver_chat_id):
    if RUNTIME_LOGISTICIAN_CHAT_ID:
        return RUNTIME_LOGISTICIAN_CHAT_ID
    return driver_chat_id


def notify_documents_complete(driver_chat_id):
    target = logist_chat_id(driver_chat_id)
    prefix = ""
    if target == driver_chat_id:
        prefix = "🧪 TEST: уведомление для логиста\n\n"

    send(
        target,
        prefix
        + "📄 Комплект документов полный\n\n"
        + "Водитель: TEST DRIVER\n"
        + f"Маршрут: {ROUTE_NAME}\n\n"
        + documents_status_text(state(driver_chat_id))
        + "\n\n"
        + "Статус: ✅ ДОКУМЕНТЫ ПОЛНЫЕ\n"
        + "✉️ Отправку письма подключим позже.",
    )


def notify_help(driver_chat_id, reason, waiting_minutes=None):
    st = state(driver_chat_id)
    st["active_help"] = reason
    target = logist_chat_id(driver_chat_id)

    wait_line = (
        f"\n⏱️ Ожидание: {waiting_minutes} мин."
        if waiting_minutes is not None
        else ""
    )
    prefix = ""
    if target == driver_chat_id:
        prefix = "🧪 TEST: уведомление для логиста\n\n"

    point = (
        ROUTE_POINTS[st["current_point"]]
        if st["current_point"] < len(ROUTE_POINTS)
        else ROUTE_POINTS[-1]
    )

    send(
        target,
        prefix
        + "📞 Нужна помощь\n\n"
        + "Водитель: TEST DRIVER\n"
        + f"Маршрут: {ROUTE_NAME}\n"
        + f"Адрес: {point['address']}\n"
        + f"Причина: {reason}"
        + wait_line,
        inline_keyboard=logistician_decision_keyboard(driver_chat_id),
    )
    log_event(driver_chat_id, "help_requested", reason)


def help_label(code):
    return {
        "no_answer": "Клиент не отвечает",
        "long_unload": "Долгая разгрузка",
        "wrong_address": "Неверный адрес",
        "breakdown": "Поломка",
    }.get(code, code)


def show_log_today(chat_id):
    blocks = []
    for driver_chat_id, st in USERS.items():
        if st["route_started"] or st["documents"]:
            blocks.append(log_driver_card(driver_chat_id, st))
    set_workspace(
        chat_id,
        "📅 Сегодня\n\n" + ("\n\n".join(blocks) if blocks else "Активных маршрутов нет."),
        [],
    )


def log_driver_card(driver_chat_id, st):
    point_text = (
        f"{min(st['current_point'] + 1, len(ROUTE_POINTS))} из {len(ROUTE_POINTS)}"
        if st["current_point"] < len(ROUTE_POINTS)
        else "завершён"
    )
    last_geo = fmt_time(st["last_location_at"]) if st["last_location_at"] else "нет данных"
    docs = sum(1 for code in REQUIRED_DOCS if code in st["documents"])
    help_status = st["active_help"] or "нет"

    return (
        "🚚 TEST DRIVER\n"
        f"Маршрут: {ROUTE_NAME}\n"
        f"Текущая точка: {point_text}\n"
        f"Статус: {st['stage']}\n"
        f"📍 Последняя геопозиция: {last_geo}\n"
        f"📄 Документы: {docs}/3\n"
        f"📞 Помощь: {help_status}"
    )


def show_log_drivers(chat_id):
    blocks = []
    for driver_chat_id, st in USERS.items():
        if st["route_started"] or st["documents"]:
            blocks.append(log_driver_card(driver_chat_id, st))
    set_workspace(
        chat_id,
        "🚚 Водители\n\n" + ("\n\n".join(blocks) if blocks else "Нет данных."),
        [],
    )


def show_log_documents(chat_id):
    blocks = []
    for driver_chat_id, st in USERS.items():
        if st["documents"] or st["document_status"] != "DOCUMENTS_NOT_STARTED":
            blocks.append(
                "🚚 TEST DRIVER\n"
                + documents_status_text(st)
                + f"\nСтатус: {st['document_status']}"
            )

    set_workspace(
        chat_id,
        "📄 Документы\n\n"
        + ("\n\n".join(blocks) if blocks else "Документы ещё не загружались."),
        [],
    )


def show_log_help(chat_id):
    blocks = []
    for driver_chat_id, st in USERS.items():
        if st["active_help"]:
            blocks.append(
                f"🚚 TEST DRIVER — {st['active_help']}"
            )

    set_workspace(
        chat_id,
        "📞 Помощь\n\n"
        + ("\n".join(blocks) if blocks else "Активных обращений нет."),
        [],
    )


def show_log_report(chat_id):
    delivered = sum(st["completed_points"] for st in USERS.values())
    docs_complete = sum(
        1 for st in USERS.values()
        if st["document_status"] == "DOCUMENTS_COMPLETE"
    )
    help_count = sum(
        1 for event in EVENTS
        if event["event"] == "help_requested"
    )

    set_workspace(
        chat_id,
        "📊 Отчёт за день\n\n"
        f"Завершено точек: {delivered}\n"
        f"Полных комплектов документов: {docs_complete}\n"
        f"Обращений за помощью: {help_count}",
        [],
    )


def handle_role_callback(chat_id, callback_data):
    global RUNTIME_LOGISTICIAN_CHAT_ID

    st = state(chat_id)
    if callback_data == "role:driver":
        st["role"] = "driver"
        install_role_menu(chat_id, "driver")
        route_screen(chat_id)
        return

    if callback_data == "role:logistician":
        st["role"] = "logistician"
        RUNTIME_LOGISTICIAN_CHAT_ID = chat_id
        install_role_menu(chat_id, "logistician")
        show_log_today(chat_id)
        return


def handle_callback(query):
    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    answer_callback(query["id"])
    st = state(chat_id)

    if data.startswith("role:"):
        handle_role_callback(chat_id, data)
        return

    if data == "route:start":
        request_location(
            chat_id,
            "start",
            "▶️ Начинаем маршрут. Отправьте текущую геопозицию.",
        )
        return

    if data == "route:points":
        points_screen(chat_id)
        return

    if data == "route:en_route":
        request_location(
            chat_id,
            "en_route",
            "🚗 Для статуса «В пути» отправьте геопозицию.",
        )
        return

    if data == "route:arrived":
        request_location(
            chat_id,
            "arrived",
            "📍 Для фиксации прибытия отправьте геопозицию.",
        )
        return

    if data == "route:delivered":
        st["awaiting_delivery_photo"] = True
        st["document_mode"] = False
        set_workspace(
            chat_id,
            "📸 Загрузите фото подтверждения доставки (минимум 1).",
            [],
        )
        return

    if data.startswith("docclass:"):
        classify_document(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("help:"):
        code = data.split(":", 1)[1]
        if code == "other":
            st["awaiting_other_help"] = True
            set_workspace(
                chat_id,
                "✍️ Опишите ситуацию одним коротким сообщением.",
                [],
            )
            return

        reason = help_label(code)
        notify_help(chat_id, reason)
        set_workspace(
            chat_id,
            f"📞 Запрос передан логисту.\nПричина: {reason}",
            [],
        )
        return

    if data.startswith("logact:"):
        _, action, driver_id_text = data.split(":", 2)
        driver_chat_id = int(driver_id_text)
        driver_st = state(driver_chat_id)

        if action == "wait":
            driver_st["active_help"] = None
            send(
                driver_chat_id,
                "👤 Решение логиста: ⏳ продолжайте ждать.",
            )
            log_event(driver_chat_id, "logistician_decision", "wait")
            return

        if action in ("next", "cancel"):
            decision = "➡️ ехать дальше" if action == "next" else "❌ точка отменена"
            driver_st["active_help"] = None
            driver_st["current_point"] += 1
            driver_st["arrival_ts"] = None
            driver_st["stage"] = "between_points"
            send(
                driver_chat_id,
                f"👤 Решение логиста: {decision}.",
            )
            log_event(driver_chat_id, "logistician_decision", action)
            return


def handle_text(chat_id, message_id, text):
    global RUNTIME_LOGISTICIAN_CHAT_ID
    st = state(chat_id)

    if text in ("/start", "/старт"):
        start_screen(chat_id)
        return

    if st["awaiting_other_help"]:
        st["awaiting_other_help"] = False
        reason = text.strip()[:500] or "Другая ситуация"
        notify_help(chat_id, reason)
        set_workspace(
            chat_id,
            f"📞 Запрос передан логисту.\nПричина: {reason}",
            [],
        )
        delete_message(chat_id, message_id)
        return

    if st["role"] == "driver":
        if text == "🚚 Маршрут":
            delete_message(chat_id, message_id)
            route_screen(chat_id)
            return
        if text == "📄 Документы":
            delete_message(chat_id, message_id)
            documents_screen(chat_id)
            return
        if text == "📞 Помощь":
            delete_message(chat_id, message_id)
            help_screen(chat_id)
            return

    if st["role"] == "logistician":
        RUNTIME_LOGISTICIAN_CHAT_ID = chat_id

        if text == "📅 Сегодня":
            delete_message(chat_id, message_id)
            show_log_today(chat_id)
            return
        if text == "🚚 Водители":
            delete_message(chat_id, message_id)
            show_log_drivers(chat_id)
            return
        if text == "📄 Документы":
            delete_message(chat_id, message_id)
            show_log_documents(chat_id)
            return
        if text == "📞 Помощь":
            delete_message(chat_id, message_id)
            show_log_help(chat_id)
            return
        if text == "📊 Отчёт":
            delete_message(chat_id, message_id)
            show_log_report(chat_id)
            return

    set_workspace(
        chat_id,
        "Используйте кнопки меню внизу.",
        [],
    )


def handle_photo(chat_id, message):
    st = state(chat_id)

    if st["awaiting_delivery_photo"]:
        handle_delivery_photo(chat_id)
        return

    if st["role"] == "driver" and st["document_mode"]:
        handle_document_photo(chat_id, message)
        return

    set_workspace(
        chat_id,
        "Если это документ, нажмите «📄 Документы» в нижнем меню и отправьте фото ещё раз.",
        [],
    )


def check_wait_timers():
    current = now_ts()

    for chat_id, st in list(USERS.items()):
        if st["stage"] != "arrived":
            continue
        if not st["arrival_ts"] or st["wait_notified"]:
            continue

        elapsed = current - st["arrival_ts"]
        if elapsed >= WAIT_THRESHOLD_SECONDS:
            st["wait_notified"] = True
            minutes = max(1, int(elapsed / 60))
            notify_help(chat_id, "Простой на точке", minutes)
            send(
                chat_id,
                f"⏱️ Ожидание превысило {int(WAIT_THRESHOLD_SECONDS / 60)} минут. "
                "Логист уведомлён.",
            )


def handle_message(message):
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    if "location" in message:
        handle_location(chat_id, message["location"])
        return

    if "photo" in message:
        handle_photo(chat_id, message)
        return

    text = message.get("text", "")
    handle_text(chat_id, message_id, text)


def main():
    api("deleteWebhook", {"drop_pending_updates": True})
    print("FENIX UX v2 started", flush=True)

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

        except Exception as exc:
            print("Main loop error:", repr(exc), flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
