import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"
WAIT_THRESHOLD_SECONDS = int(os.environ.get("WAIT_THRESHOLD_SECONDS", "600"))

# ---------------- TEST DATA ----------------

DRIVERS = {
    "d1": {
        "name": "Иван Орлов",
        "truck": "Mercedes-Benz Axor",
        "plate": "А434ОЕ198",
    },
    "d2": {
        "name": "Сергей Соколов",
        "truck": "Mercedes-Benz Axor",
        "plate": "С294ВК178",
    },
    "d3": {
        "name": "Александр Сидоров",
        "truck": "Mercedes-Benz Axor",
        "plate": "Н364УМ178",
    },
}

ROUTES = {
    "r1": {
        "name": "Бронка → Шушары",
        "points": [
            {
                "address": "ММПК Бронка, Краснофлотское шоссе, 49",
                "contact": "Диспетчер терминала",
                "window": "08:00–10:00",
                "comment": "Получение контейнера",
            },
            {
                "address": "Грузовой терминал Шушары, Московское шоссе",
                "contact": "+7 900 100-00-01",
                "window": "11:30–14:00",
                "comment": "Въезд через грузовой КПП",
            },
        ],
        "assigned_driver_id": None,
    },
    "r2": {
        "name": "Петролеспорт → Парнас",
        "points": [
            {
                "address": "Петролеспорт, Гладкий остров",
                "contact": "Диспетчер терминала",
                "window": "09:00–11:00",
                "comment": "Получение контейнера",
            },
            {
                "address": "Логистический терминал Парнас",
                "contact": "+7 900 100-00-02",
                "window": "13:00–16:00",
                "comment": "Разгрузка по записи",
            },
        ],
        "assigned_driver_id": None,
    },
    "r3": {
        "name": "Моби Дик → Янино",
        "points": [
            {
                "address": "Контейнерный терминал Моби Дик, Кронштадт",
                "contact": "Диспетчер терминала",
                "window": "07:30–09:30",
                "comment": "Получение контейнера",
            },
            {
                "address": "Грузовой терминал Янино",
                "contact": "+7 900 100-00-03",
                "window": "11:00–14:00",
                "comment": "Прибыть к окну разгрузки",
            },
        ],
        "assigned_driver_id": None,
    },
}

REQUIRED_DOCS = ("ttn", "act", "container")
DOC_LABELS = {
    "ttn": "ТТН",
    "act": "Акт",
    "container": "Фото контейнера",
}

SESSIONS = {}
DRIVER_STATES = {}
EVENTS = []
SEEN_UPDATES = set()
RUNTIME_LOGISTICIAN_CHAT_ID = None


# ---------------- BASIC HELPERS ----------------

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
    api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def answer_callback(callback_id):
    api("answerCallbackQuery", {"callback_query_id": callback_id})


def session(chat_id):
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {
            "role": None,
            "selected_driver_id": None,
            "workspace_message_id": None,
            "wizard": None,
        }
    return SESSIONS[chat_id]


def driver_state(driver_id):
    if driver_id not in DRIVER_STATES:
        DRIVER_STATES[driver_id] = {
            "route_id": None,
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
            "last_chat_id": None,
        }
    return DRIVER_STATES[driver_id]


def log_event(driver_id, event, detail=""):
    EVENTS.append({
        "driver_id": driver_id,
        "event": event,
        "detail": detail,
        "ts": now_ts(),
    })


def selected_driver(chat_id):
    driver_id = session(chat_id).get("selected_driver_id")
    if not driver_id or driver_id not in DRIVERS:
        return None, None
    st = driver_state(driver_id)
    st["last_chat_id"] = chat_id
    return driver_id, st


def set_workspace(chat_id, text, inline_keyboard=None):
    s = session(chat_id)
    current_id = s.get("workspace_message_id")

    if current_id:
        result = edit(chat_id, current_id, text, inline_keyboard)
        if result.get("ok"):
            return current_id

    result = send(chat_id, text, inline_keyboard=inline_keyboard)
    if result.get("ok"):
        s["workspace_message_id"] = result["result"]["message_id"]
        return s["workspace_message_id"]
    return None


# ---------------- MENUS ----------------

def driver_menu():
    return {
        "keyboard": [[
            {"text": "Маршрут"},
            {"text": "Документы"},
            {"text": "Помощь"},
        ]],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Выберите раздел",
    }


def logistician_menu():
    return {
        "keyboard": [
            [{"text": "Сегодня"}, {"text": "Маршруты"}],
            [{"text": "Водители"}, {"text": "Документы"}],
            [{"text": "⚠️ Проблема"}, {"text": "Отчёт"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Выберите раздел",
    }


def location_menu():
    return {
        "keyboard": [[
            {"text": "📍 Отправить геопозицию", "request_location": True}
        ]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def role_keyboard():
    return [
        [{"text": "🚚 Водитель", "callback_data": "role:driver"}],
        [{"text": "👤 Логист", "callback_data": "role:logistician"}],
    ]


def driver_picker_keyboard():
    keyboard = []
    for driver_id, driver in DRIVERS.items():
        keyboard.append([{
            "text": f"{driver['name']} · {driver['plate']}",
            "callback_data": f"driverpick:{driver_id}",
        }])
    return keyboard


def route_start_keyboard():
    return [[{"text": "▶️ Начать маршрут", "callback_data": "route:start"}]]


def current_point_keyboard(st):
    if st["stage"] == "arrived":
        return [
            [{"text": "✅ Доставлено", "callback_data": "route:delivered"}],
            [{"text": "📞 Помощь", "callback_data": "open:help"}],
        ]
    return [
        [{"text": "🚗 В пути", "callback_data": "route:en_route"}],
        [{"text": "📍 Прибыл", "callback_data": "route:arrived"}],
        [{"text": "📞 Помощь", "callback_data": "open:help"}],
    ]


def help_keyboard():
    return [
        [{"text": "📞 Клиент не отвечает", "callback_data": "help:no_answer"}],
        [{"text": "⏳ Долгая разгрузка", "callback_data": "help:long_unload"}],
        [{"text": "📍 Неверный адрес", "callback_data": "help:wrong_address"}],
        [{"text": "🔧 Поломка", "callback_data": "help:breakdown"}],
        [{"text": "✍️ Другое", "callback_data": "help:other"}],
        [{"text": "↩️ К маршруту", "callback_data": "open:route"}],
    ]


def document_classification_keyboard():
    return [
        [
            {"text": "ТТН", "callback_data": "docclass:ttn"},
            {"text": "Акт", "callback_data": "docclass:act"},
        ],
        [{"text": "Фото контейнера", "callback_data": "docclass:container"}],
    ]


def routes_admin_keyboard():
    keyboard = []
    for route_id, route in ROUTES.items():
        assigned = route.get("assigned_driver_id")
        marker = "✅" if assigned else "➕"
        keyboard.append([{
            "text": f"{marker} {route['name']}",
            "callback_data": f"logroute:{route_id}",
        }])
    keyboard.append([{"text": "➕ Добавить маршрут", "callback_data": "log:add_route"}])
    return keyboard


def route_assign_keyboard(route_id):
    keyboard = []
    for driver_id, driver in DRIVERS.items():
        keyboard.append([{
            "text": f"{driver['name']} · {driver['plate']}",
            "callback_data": f"assign:{route_id}:{driver_id}",
        }])
    keyboard.append([{"text": "↩️ К маршрутам", "callback_data": "log:routes"}])
    return keyboard


def drivers_admin_keyboard():
    keyboard = []
    for driver_id, driver in DRIVERS.items():
        keyboard.append([{
            "text": f"{driver['name']} · {driver['plate']}",
            "callback_data": f"logdriver:{driver_id}",
        }])
    keyboard.append([
        {"text": "➕ Добавить водителя", "callback_data": "log:add_driver"}
    ])
    return keyboard


def logistician_decision_keyboard(driver_id):
    return [
        [{"text": "⏳ Ждать", "callback_data": f"logact:wait:{driver_id}"}],
        [{"text": "➡️ Ехать дальше", "callback_data": f"logact:next:{driver_id}"}],
        [{"text": "❌ Отменить точку", "callback_data": f"logact:cancel:{driver_id}"}],
    ]


# ---------------- START / ROLE ----------------

def start_screen(chat_id):
    s = session(chat_id)
    s["role"] = None
    s["selected_driver_id"] = None
    s["wizard"] = None
    s["workspace_message_id"] = None

    greeting = (
        "Привет, это AI-бот компании FENIX LOGISTIC.\n"
        "Это тестовая версия. Выбери свою роль"
    )
    result = send(chat_id, greeting, inline_keyboard=role_keyboard())
    if result.get("ok"):
        s["workspace_message_id"] = result["result"]["message_id"]


def choose_driver_screen(chat_id):
    set_workspace(
        chat_id,
        "🚚 Выберите водителя для теста:",
        driver_picker_keyboard(),
    )


def install_driver_menu(chat_id):
    send(chat_id, "Меню водителя", reply_markup=driver_menu())


def install_logistician_menu(chat_id):
    send(chat_id, "Меню логиста", reply_markup=logistician_menu())


# ---------------- DRIVER: ROUTE ----------------

def route_for_driver(driver_id):
    st = driver_state(driver_id)
    route_id = st.get("route_id")
    if not route_id:
        return None, None
    return route_id, ROUTES.get(route_id)


def reset_driver_route_progress(driver_id):
    st = driver_state(driver_id)
    st.update({
        "route_started": False,
        "route_started_at": None,
        "current_point": 0,
        "stage": "not_started",
        "arrival_ts": None,
        "last_location": None,
        "last_location_at": None,
        "awaiting_location_action": None,
        "awaiting_delivery_photo": False,
        "active_help": None,
        "wait_notified": False,
        "completed_points": 0,
    })


def route_overview_text(driver_id, route):
    driver = DRIVERS[driver_id]
    lines = [
        f"🚚 {driver['name']} · {driver['plate']}",
        f"Маршрут: {route['name']}",
        "",
        f"Точки: {len(route['points'])}",
    ]

    for index, point in enumerate(route["points"], start=1):
        lines.append(f"{index}. {point['address']} — {point['window']}")

    lines.extend(["", "Статус: маршрут не начат"])
    return "\n".join(lines)


def route_screen(chat_id):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        choose_driver_screen(chat_id)
        return

    st["document_mode"] = False
    st["pending_document_photo"] = None

    route_id, route = route_for_driver(driver_id)
    if not route:
        driver = DRIVERS[driver_id]
        set_workspace(
            chat_id,
            f"🚚 {driver['name']} · {driver['plate']}\n\n"
            "Маршрут пока не назначен.\n"
            "Переключитесь на роль логиста и назначьте маршрут.",
            [],
        )
        return

    if not st["route_started"]:
        set_workspace(
            chat_id,
            route_overview_text(driver_id, route),
            route_start_keyboard(),
        )
        return

    if st["current_point"] >= len(route["points"]):
        set_workspace(chat_id, "🏁 Маршрут завершён\nСпасибо за работу", [])
        return

    point = route["points"][st["current_point"]]
    status = {
        "started": "Готов к выезду",
        "between_points": "Следующая точка",
        "en_route": "🚗 В пути",
        "arrived": "📍 Прибыл",
        "finished": "Завершён",
    }.get(st["stage"], st["stage"])

    geo_line = ""
    if st["last_location_at"] and st["last_location"]:
        lat = st["last_location"]["latitude"]
        lon = st["last_location"]["longitude"]
        geo_line = (
            f"\n📍 Последняя геопозиция: {lat:.5f}, {lon:.5f}"
            f" · {fmt_time(st['last_location_at'])}"
        )

    text = (
        f"🚚 {DRIVERS[driver_id]['name']} · {DRIVERS[driver_id]['plate']}\n"
        f"Маршрут: {route['name']}\n"
        f"📍 Точка {st['current_point'] + 1} из {len(route['points'])}\n\n"
        f"{point['address']}\n"
        f"Контакт: {point['contact']}\n"
        f"Окно: {point['window']}\n"
        f"{point['comment']}\n\n"
        f"Статус: {status}"
        f"{geo_line}"
    )
    set_workspace(chat_id, text, current_point_keyboard(st))


def request_location(chat_id, action, prompt):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        choose_driver_screen(chat_id)
        return

    st["awaiting_location_action"] = action
    set_workspace(chat_id, prompt, [])
    send(
        chat_id,
        "Нажмите кнопку ниже, чтобы отправить текущую геопозицию.",
        reply_markup=location_menu(),
    )


def restore_driver_menu(chat_id):
    send(chat_id, "✅ Геопозиция получена", reply_markup=driver_menu())


def handle_location(chat_id, location):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        return

    action = st.get("awaiting_location_action")
    if not action:
        set_workspace(chat_id, "📍 Геопозиция получена. Сейчас бот её не запрашивал.", [])
        return

    st["awaiting_location_action"] = None
    st["last_location"] = {
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
    }
    st["last_location_at"] = now_ts()
    log_event(driver_id, "location", action)

    if action == "start":
        st["route_started"] = True
        st["route_started_at"] = now_ts()
        st["stage"] = "started"
        log_event(driver_id, "route_started")
    elif action == "en_route":
        st["stage"] = "en_route"
        log_event(driver_id, "en_route", str(st["current_point"] + 1))
    elif action == "arrived":
        st["stage"] = "arrived"
        st["arrival_ts"] = now_ts()
        st["wait_notified"] = False
        log_event(driver_id, "arrived", str(st["current_point"] + 1))

    restore_driver_menu(chat_id)
    route_screen(chat_id)


def handle_delivery_photo(chat_id):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        return

    route_id, route = route_for_driver(driver_id)
    if not route:
        return

    st["awaiting_delivery_photo"] = False

    arrival = st.get("arrival_ts")
    dwell_minutes = (
        max(0, int((now_ts() - arrival) / 60))
        if arrival else 0
    )

    st["completed_points"] += 1
    st["current_point"] += 1
    st["arrival_ts"] = None
    st["active_help"] = None
    st["wait_notified"] = False
    log_event(driver_id, "delivered", f"dwell={dwell_minutes}")

    if st["current_point"] >= len(route["points"]):
        st["stage"] = "finished"
        set_workspace(
            chat_id,
            "✅ Точка завершена\n"
            f"⏱️ Время на точке: {dwell_minutes} минут\n\n"
            "🏁 Маршрут завершён\nСпасибо за работу",
            [],
        )
    else:
        st["stage"] = "between_points"
        route_screen(chat_id)


# ---------------- DRIVER: DOCUMENTS ----------------

def documents_status_text(st):
    lines = []
    for code in REQUIRED_DOCS:
        mark = "✅" if code in st["documents"] else "❌"
        lines.append(f"{mark} {DOC_LABELS[code]}")
    return "\n".join(lines)


def documents_screen(chat_id):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        choose_driver_screen(chat_id)
        return

    st["document_mode"] = True
    st["pending_document_photo"] = None
    missing = [code for code in REQUIRED_DOCS if code not in st["documents"]]

    if missing:
        st["document_status"] = (
            "DOCUMENTS_COLLECTING" if st["documents"]
            else "DOCUMENTS_NOT_STARTED"
        )
        text = (
            f"📄 Документы · {DRIVERS[driver_id]['name']}\n\n"
            f"{documents_status_text(st)}\n\n"
            "Отправьте фото документа прямо в чат. "
            "После фото выберите его тип."
        )
    else:
        st["document_status"] = "DOCUMENTS_COMPLETE"
        text = (
            f"📄 Документы · {DRIVERS[driver_id]['name']}\n\n"
            f"{documents_status_text(st)}\n\n"
            "✅ Комплект документов полный\n"
            "✅ Логист уведомлён\n"
            "🚚 Статус: ДОКУМЕНТЫ ПОЛНЫЕ"
        )

    set_workspace(chat_id, text, [])


def handle_document_photo(chat_id, message):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        return

    photos = message.get("photo") or []
    if not photos:
        return

    st["pending_document_photo"] = {
        "file_id": photos[-1]["file_id"],
        "message_id": message.get("message_id"),
    }
    set_workspace(
        chat_id,
        "📄 Фото получено.\n\nЧто это?",
        document_classification_keyboard(),
    )


def classify_document(chat_id, doc_type):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        return

    pending = st.get("pending_document_photo")
    if not pending:
        documents_screen(chat_id)
        return

    st["documents"][doc_type] = {
        "file_id": pending["file_id"],
        "received_at": now_ts(),
    }
    st["pending_document_photo"] = None
    log_event(driver_id, "document_received", doc_type)

    missing = [code for code in REQUIRED_DOCS if code not in st["documents"]]

    if missing:
        st["document_status"] = "DOCUMENTS_COLLECTING"
        set_workspace(
            chat_id,
            f"📄 Документы · {DRIVERS[driver_id]['name']}\n\n"
            f"{documents_status_text(st)}\n\n"
            f"✅ Получено: {DOC_LABELS[doc_type]}\n"
            "Отправьте следующее фото.",
            [],
        )
        return

    if st["document_status"] != "DOCUMENTS_COMPLETE":
        st["document_status"] = "DOCUMENTS_COMPLETE"
        log_event(driver_id, "documents_complete")
        notify_documents_complete(driver_id)

    documents_screen(chat_id)


def notify_documents_complete(driver_id):
    target = RUNTIME_LOGISTICIAN_CHAT_ID
    if not target:
        return

    st = driver_state(driver_id)
    route_id, route = route_for_driver(driver_id)
    route_name = route["name"] if route else "не назначен"

    send(
        target,
        "📄 Комплект документов полный\n\n"
        f"Водитель: {DRIVERS[driver_id]['name']}\n"
        f"Машина: {DRIVERS[driver_id]['plate']}\n"
        f"Маршрут: {route_name}\n\n"
        f"{documents_status_text(st)}\n\n"
        "Статус: ✅ ДОКУМЕНТЫ ПОЛНЫЕ\n"
        "✉️ Отправку письма подключим позже.",
    )


# ---------------- DRIVER: HELP ----------------

def help_label(code):
    return {
        "no_answer": "Клиент не отвечает",
        "long_unload": "Долгая разгрузка",
        "wrong_address": "Неверный адрес",
        "breakdown": "Поломка",
    }.get(code, code)


def help_screen(chat_id):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        choose_driver_screen(chat_id)
        return

    st["document_mode"] = False
    set_workspace(chat_id, "📞 Помощь\n\nЧто произошло?", help_keyboard())


def notify_help(driver_id, reason, waiting_minutes=None):
    st = driver_state(driver_id)
    st["active_help"] = reason
    target = RUNTIME_LOGISTICIAN_CHAT_ID

    if not target:
        return

    route_id, route = route_for_driver(driver_id)
    route_name = route["name"] if route else "не назначен"

    point_address = "нет текущей точки"
    if route and st["current_point"] < len(route["points"]):
        point_address = route["points"][st["current_point"]]["address"]

    wait_line = (
        f"\n⏱️ Ожидание: {waiting_minutes} мин."
        if waiting_minutes is not None else ""
    )

    geo_line = ""
    if st["last_location"]:
        geo_line = (
            f"\n📍 {st['last_location']['latitude']:.5f}, "
            f"{st['last_location']['longitude']:.5f}"
        )

    send(
        target,
        "⚠️ Проблема\n\n"
        f"Водитель: {DRIVERS[driver_id]['name']}\n"
        f"Машина: {DRIVERS[driver_id]['plate']}\n"
        f"Маршрут: {route_name}\n"
        f"Точка: {point_address}\n"
        f"Причина: {reason}"
        f"{wait_line}"
        f"{geo_line}",
        inline_keyboard=logistician_decision_keyboard(driver_id),
    )
    log_event(driver_id, "help_requested", reason)


def notify_driver(driver_id, text):
    chat_id = driver_state(driver_id).get("last_chat_id")
    if chat_id:
        send(chat_id, text)


# ---------------- LOGISTICIAN SCREENS ----------------

def assignment_for_driver(driver_id):
    route_id = driver_state(driver_id).get("route_id")
    if route_id and route_id in ROUTES:
        return ROUTES[route_id]["name"]
    return "не назначен"


def route_assignment_text(route):
    assigned = route.get("assigned_driver_id")
    if not assigned or assigned not in DRIVERS:
        return "Свободен"
    driver = DRIVERS[assigned]
    return f"{driver['name']} · {driver['plate']}"


def show_log_routes(chat_id):
    set_workspace(
        chat_id,
        "Маршруты",
        routes_admin_keyboard(),
    )

def routes_for_driver_keyboard(driver_id):
    keyboard = []
    for route_id, route in ROUTES.items():
        assigned = route.get("assigned_driver_id")
        suffix = ""
        if assigned and assigned != driver_id and assigned in DRIVERS:
            suffix = f" · занят: {DRIVERS[assigned]['name']}"
        elif assigned == driver_id:
            suffix = " · назначен"

        keyboard.append([{
            "text": f"{route['name']}{suffix}",
            "callback_data": f"assignexisting:{driver_id}:{route_id}",
        }])

    keyboard.append([{
        "text": "➕ Добавить вручную",
        "callback_data": f"driverroute_manual:{driver_id}",
    }])
    keyboard.append([{
        "text": "↩️ К водителю",
        "callback_data": f"logdriver:{driver_id}",
    }])
    return keyboard


def show_routes_for_driver(chat_id, driver_id):
    if driver_id not in DRIVERS:
        show_log_drivers(chat_id)
        return

    set_workspace(
        chat_id,
        f"Маршрут для {DRIVERS[driver_id]['name']}",
        routes_for_driver_keyboard(driver_id),
    )

def show_route_assignment(chat_id, route_id):
    route = ROUTES.get(route_id)
    if not route:
        show_log_routes(chat_id)
        return

    set_workspace(
        chat_id,
        f"🗺 {route['name']}\n\n"
        f"Сейчас: {route_assignment_text(route)}\n\n"
        "Выберите водителя:",
        route_assign_keyboard(route_id),
    )


def assign_route(route_id, driver_id):
    route = ROUTES[route_id]

    # Free the route from its previous driver.
    old_driver_id = route.get("assigned_driver_id")
    if old_driver_id and old_driver_id in DRIVER_STATES:
        old_st = driver_state(old_driver_id)
        if old_st.get("route_id") == route_id:
            old_st["route_id"] = None
            reset_driver_route_progress(old_driver_id)

    # Free driver's previous route.
    new_st = driver_state(driver_id)
    previous_route_id = new_st.get("route_id")
    if previous_route_id and previous_route_id in ROUTES:
        ROUTES[previous_route_id]["assigned_driver_id"] = None

    route["assigned_driver_id"] = driver_id
    new_st["route_id"] = route_id
    reset_driver_route_progress(driver_id)
    log_event(driver_id, "route_assigned", route_id)


def driver_card(driver_id):
    driver = DRIVERS[driver_id]
    st = driver_state(driver_id)
    route_name = assignment_for_driver(driver_id)

    if st["last_location"] and st["last_location_at"]:
        geo = (
            f"{st['last_location']['latitude']:.5f}, "
            f"{st['last_location']['longitude']:.5f} "
            f"· {fmt_time(st['last_location_at'])}"
        )
    else:
        geo = "нет данных"

    docs = sum(1 for code in REQUIRED_DOCS if code in st["documents"])
    help_status = st["active_help"] or "нет"

    return (
        f"🚚 {driver['name']}\n"
        f"{driver['truck']} · {driver['plate']}\n"
        f"Маршрут: {route_name}\n"
        f"Статус: {st['stage']}\n"
        f"📍 Геопозиция: {geo}\n"
        f"📄 Документы: {docs}/3\n"
        f"⚠️ Проблема: {help_status}"
    )


def show_log_drivers(chat_id):
    set_workspace(
        chat_id,
        "Водители",
        drivers_admin_keyboard(),
    )

def show_log_driver_detail(chat_id, driver_id):
    if driver_id not in DRIVERS:
        show_log_drivers(chat_id)
        return

    set_workspace(
        chat_id,
        driver_card(driver_id),
        [
            [{"text": "➕ Добавить маршрут", "callback_data": f"driverroutepick:{driver_id}"}],
            [{"text": "↩️ К водителям", "callback_data": "log:drivers"}],
        ],
    )

def show_log_today(chat_id):
    assigned = []
    for driver_id in DRIVERS:
        route_id = driver_state(driver_id).get("route_id")
        if route_id:
            assigned.append(driver_card(driver_id))

    if not assigned:
        set_workspace(
            chat_id,
            "На сегодня маршруты ещё не назначены.",
            [
                [{"text": "🚚 Водители", "callback_data": "log:drivers"}],
                [{"text": "🗺 Маршруты", "callback_data": "log:routes"}],
            ],
        )
        return

    set_workspace(
        chat_id,
        "📅 Сегодня\n\n" + "\n\n".join(assigned),
        [
            [{"text": "🚚 Водители", "callback_data": "log:drivers"}],
            [{"text": "🗺 Маршруты", "callback_data": "log:routes"}],
        ],
    )

def logistician_documents_keyboard():
    keyboard = []
    for driver_id, driver in DRIVERS.items():
        keyboard.append([{
            "text": f"{driver['plate']} · {driver['name']}",
            "callback_data": f"logdocs:{driver_id}",
        }])
    return keyboard


def show_driver_documents(chat_id, driver_id):
    if driver_id not in DRIVERS:
        show_log_documents(chat_id)
        return

    st = driver_state(driver_id)
    route_id, route = route_for_driver(driver_id)
    route_name = route["name"] if route else "не назначен"

    set_workspace(
        chat_id,
        f"📄 {DRIVERS[driver_id]['name']} · {DRIVERS[driver_id]['plate']}\n"
        f"Маршрут: {route_name}\n\n"
        f"{documents_status_text(st)}\n"
        f"Статус: {st['document_status']}",
        [[{"text": "↩️ К документам", "callback_data": "log:documents"}]],
    )

def show_log_documents(chat_id):
    set_workspace(
        chat_id,
        "Документы",
        logistician_documents_keyboard(),
    )

def show_log_help(chat_id):
    blocks = []
    for driver_id in DRIVERS:
        st = driver_state(driver_id)
        if st["active_help"]:
            blocks.append(
                f"🚚 {DRIVERS[driver_id]['name']} · {DRIVERS[driver_id]['plate']}\n"
                f"{st['active_help']}"
            )

    if not blocks:
        set_workspace(
            chat_id,
            "⚠️ Активных проблем сейчас нет.",
            [[{"text": "🚚 К водителям", "callback_data": "log:drivers"}]],
        )
        return

    set_workspace(
        chat_id,
        "⚠️ Проблемы\n\n" + "\n\n".join(blocks),
        [[{"text": "🚚 К водителям", "callback_data": "log:drivers"}]],
    )

def show_log_report(chat_id):
    delivered = sum(driver_state(d)["completed_points"] for d in DRIVERS)
    docs_complete = sum(
        1 for d in DRIVERS
        if driver_state(d)["document_status"] == "DOCUMENTS_COMPLETE"
    )
    help_count = sum(1 for event in EVENTS if event["event"] == "help_requested")
    assigned_count = sum(1 for d in DRIVERS if driver_state(d).get("route_id"))

    set_workspace(
        chat_id,
        "📊 Отчёт за день\n\n"
        f"Назначено маршрутов: {assigned_count}\n"
        f"Завершено точек: {delivered}\n"
        f"Полных комплектов документов: {docs_complete}\n"
        f"Обращений за помощью: {help_count}",
        [],
    )


# ---------------- LOGISTICIAN: ADD WIZARDS ----------------

def start_add_driver(chat_id):
    session(chat_id)["wizard"] = {
        "type": "add_driver",
        "step": "one_message",
        "data": {},
    }
    set_workspace(
        chat_id,
        "➕ Новый водитель\n\n"
        "Пришлите одним сообщением две строки:\n"
        "ФИО\n"
        "Госномер\n\n"
        "Например:\n"
        "Павел Иванов\n"
        "К123МР178\n\n"
        "Машина для теста: Mercedes-Benz Axor.",
        [],
    )

def start_add_route(chat_id, target_driver_id=None):
    session(chat_id)["wizard"] = {
        "type": "add_route",
        "step": "name",
        "data": {
            "target_driver_id": target_driver_id,
            "points": [],
        },
    }
    set_workspace(
        chat_id,
        "➕ Новый маршрут\n\nВведите название маршрута.\n"
        "Например: Бронка → Колпино",
        [],
    )

def parse_driver_input(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[0], lines[1].upper()

    parts = [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1].upper()

    return None, None


def parse_route_point(text):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 4:
        return None

    address = parts[0]
    contact = parts[1]
    window = parts[2]
    comment = "|".join(parts[3:]).strip()

    if not all([address, contact, window, comment]):
        return None

    return {
        "address": address,
        "contact": contact,
        "window": window,
        "comment": comment,
    }


def route_point_actions_keyboard():
    return [
        [{"text": "➕ Добавить ещё точку", "callback_data": "routewizard:add_point"}],
        [{"text": "✅ Сохранить маршрут", "callback_data": "routewizard:save"}],
        [{"text": "❌ Отмена", "callback_data": "routewizard:cancel"}],
    ]

def handle_wizard_text(chat_id, text):
    s = session(chat_id)
    wizard = s.get("wizard")
    if not wizard:
        return False

    text = (text or "").strip()
    if not text:
        return True

    if wizard["type"] == "add_driver":
        name, plate = parse_driver_input(text)
        if not name or not plate:
            set_workspace(
                chat_id,
                "Пришлите ФИО и госномер двумя строками.\n"
                "Например:\nПавел Иванов\nК123МР178",
                [],
            )
            return True

        next_number = len(DRIVERS) + 1
        driver_id = f"d{next_number}"
        while driver_id in DRIVERS:
            next_number += 1
            driver_id = f"d{next_number}"

        DRIVERS[driver_id] = {
            "name": name,
            "plate": plate,
            "truck": "Mercedes-Benz Axor",
        }
        driver_state(driver_id)
        s["wizard"] = None
        show_log_drivers(chat_id)
        return True

    if wizard["type"] == "add_route":
        if wizard["step"] == "name":
            wizard["data"]["name"] = text
            wizard["step"] = "point"
            set_workspace(
                chat_id,
                "Точка 1. Пришлите одной строкой через | :\n\n"
                "Адрес | Контакт | Окно | Комментарий\n\n"
                "Пример:\n"
                "ММПК Бронка, Краснофлотское шоссе, 49 | "
                "Диспетчер терминала | 08:00–10:00 | Получение контейнера",
                [],
            )
            return True

        if wizard["step"] == "point":
            point = parse_route_point(text)
            if not point:
                set_workspace(
                    chat_id,
                    "Не получилось разобрать точку. Используйте формат:\n"
                    "Адрес | Контакт | Окно | Комментарий",
                    [],
                )
                return True

            wizard["data"]["points"].append(point)
            wizard["step"] = "point_action"

            set_workspace(
                chat_id,
                f"✅ Точка {len(wizard['data']['points'])} добавлена.\n\n"
                f"{point['address']}\n"
                f"Контакт: {point['contact']}\n"
                f"Окно: {point['window']}\n"
                f"{point['comment']}",
                route_point_actions_keyboard(),
            )
            return True

    return False

def handle_callback(query):
    global RUNTIME_LOGISTICIAN_CHAT_ID

    data = query["data"]
    chat_id = query["message"]["chat"]["id"]
    answer_callback(query["id"])
    s = session(chat_id)

    if data == "role:driver":
        s["role"] = "driver"
        s["wizard"] = None
        choose_driver_screen(chat_id)
        return

    if data == "role:logistician":
        s["role"] = "logistician"
        s["selected_driver_id"] = None
        s["wizard"] = None
        RUNTIME_LOGISTICIAN_CHAT_ID = chat_id
        install_logistician_menu(chat_id)
        show_log_drivers(chat_id)
        return

    if data.startswith("driverpick:"):
        driver_id = data.split(":", 1)[1]
        if driver_id in DRIVERS:
            s["role"] = "driver"
            s["selected_driver_id"] = driver_id
            driver_state(driver_id)["last_chat_id"] = chat_id
            install_driver_menu(chat_id)
            route_screen(chat_id)
        return

    if data == "open:route":
        route_screen(chat_id)
        return

    if data == "open:help":
        help_screen(chat_id)
        return

    if data == "route:start":
        request_location(
            chat_id,
            "start",
            "▶️ Чтобы начать маршрут, отправьте текущую геопозицию.",
        )
        return

    if data == "route:en_route":
        request_location(
            chat_id,
            "en_route",
            "🚗 Чтобы зафиксировать статус «В пути», отправьте геопозицию.",
        )
        return

    if data == "route:arrived":
        request_location(
            chat_id,
            "arrived",
            "📍 Чтобы зафиксировать прибытие, отправьте геопозицию.",
        )
        return

    if data == "route:delivered":
        driver_id, st = selected_driver(chat_id)
        if driver_id:
            st["awaiting_delivery_photo"] = True
            st["document_mode"] = False
            set_workspace(chat_id, "📸 Загрузите фото подтверждения доставки.", [])
        return

    if data.startswith("docclass:"):
        classify_document(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("help:"):
        driver_id, st = selected_driver(chat_id)
        if not driver_id:
            return

        code = data.split(":", 1)[1]
        if code == "other":
            st["awaiting_other_help"] = True
            set_workspace(chat_id, "✍️ Опишите ситуацию одним коротким сообщением.", [])
            return

        reason = help_label(code)
        notify_help(driver_id, reason)
        set_workspace(
            chat_id,
            f"📞 Запрос передан логисту\nПричина: {reason}",
            [[{"text": "↩️ К маршруту", "callback_data": "open:route"}]],
        )
        return

    if data == "log:routes":
        show_log_routes(chat_id)
        return

    if data == "log:drivers":
        show_log_drivers(chat_id)
        return

    if data == "log:documents":
        show_log_documents(chat_id)
        return

    if data.startswith("logdocs:"):
        show_driver_documents(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("driverroutepick:"):
        show_routes_for_driver(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("assignexisting:"):
        _, driver_id, route_id = data.split(":", 2)
        if driver_id in DRIVERS and route_id in ROUTES:
            assign_route(route_id, driver_id)
            show_log_driver_detail(chat_id, driver_id)
        return

    if data.startswith("driverroute_manual:"):
        driver_id = data.split(":", 1)[1]
        if driver_id in DRIVERS:
            start_add_route(chat_id, target_driver_id=driver_id)
        return

    if data.startswith("logdriver:"):
        show_log_driver_detail(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("logroute:"):
        show_route_assignment(chat_id, data.split(":", 1)[1])
        return

    if data.startswith("assign:"):
        _, route_id, driver_id = data.split(":", 2)
        if route_id in ROUTES and driver_id in DRIVERS:
            assign_route(route_id, driver_id)
            show_log_routes(chat_id)
        return

    if data == "log:add_driver":
        start_add_driver(chat_id)
        return

    if data == "log:add_route":
        start_add_route(chat_id)
        return

    if data == "routewizard:add_point":
        wizard = session(chat_id).get("wizard")
        if wizard and wizard.get("type") == "add_route":
            wizard["step"] = "point"
            number = len(wizard["data"]["points"]) + 1
            set_workspace(
                chat_id,
                f"Точка {number}. Пришлите:\n"
                "Адрес | Контакт | Окно | Комментарий",
                [],
            )
        return

    if data == "routewizard:save":
        wizard = session(chat_id).get("wizard")
        if wizard and wizard.get("type") == "add_route":
            points = wizard["data"].get("points", [])
            if not points:
                set_workspace(chat_id, "Добавьте хотя бы одну точку.", [])
                return

            next_number = len(ROUTES) + 1
            route_id = f"r{next_number}"
            while route_id in ROUTES:
                next_number += 1
                route_id = f"r{next_number}"

            ROUTES[route_id] = {
                "name": wizard["data"]["name"],
                "points": points,
                "assigned_driver_id": None,
            }

            target_driver_id = wizard["data"].get("target_driver_id")
            session(chat_id)["wizard"] = None

            if target_driver_id and target_driver_id in DRIVERS:
                assign_route(route_id, target_driver_id)
                show_log_driver_detail(chat_id, target_driver_id)
            else:
                show_log_routes(chat_id)
        return

    if data == "routewizard:cancel":
        session(chat_id)["wizard"] = None
        show_log_routes(chat_id)
        return

    if data.startswith("logact:"):
        _, action, driver_id = data.split(":", 2)
        if driver_id not in DRIVERS:
            return

        st = driver_state(driver_id)
        route_id, route = route_for_driver(driver_id)

        if action == "wait":
            st["active_help"] = None
            notify_driver(driver_id, "👤 Решение логиста: продолжайте ждать.")
            log_event(driver_id, "logistician_decision", "wait")
            show_log_help(chat_id)
            return

        if action in ("next", "cancel"):
            decision = "ехать дальше" if action == "next" else "точка отменена"
            st["active_help"] = None
            if route:
                st["current_point"] += 1
            st["arrival_ts"] = None
            st["stage"] = "between_points"
            notify_driver(driver_id, f"👤 Решение логиста: {decision}.")
            log_event(driver_id, "logistician_decision", action)
            show_log_help(chat_id)
            return


# ---------------- TEXT / PHOTO / LOCATION ----------------

def normalize(text):
    return re.sub(r"\s+", " ", (text or "").replace("\ufe0f", "")).strip().lower()


def menu_action(text):
    t = normalize(text)
    if t == "маршруты":
        return "routes"
    if t == "маршрут" or ("маршрут" in t and "маршруты" not in t):
        return "route"
    if "водител" in t:
        return "drivers"
    if "документ" in t:
        return "documents"
    if "помощ" in t:
        return "help"
    if "проблем" in t:
        return "problem"
    if "сегодня" in t:
        return "today"
    if "отч" in t:
        return "report"
    return None


def handle_text(chat_id, message_id, text):
    global RUNTIME_LOGISTICIAN_CHAT_ID

    s = session(chat_id)

    if text in ("/start", "/старт"):
        start_screen(chat_id)
        return

    if s.get("wizard"):
        handle_wizard_text(chat_id, text)
        return

    driver_id, st = selected_driver(chat_id)
    if s["role"] == "driver" and driver_id and st["awaiting_other_help"]:
        st["awaiting_other_help"] = False
        reason = text.strip()[:500] or "Другая ситуация"
        notify_help(driver_id, reason)
        set_workspace(
            chat_id,
            f"📞 Запрос передан логисту\nПричина: {reason}",
            [[{"text": "↩️ К маршруту", "callback_data": "open:route"}]],
        )
        return

    action = menu_action(text)

    if s["role"] == "driver":
        if action == "route":
            route_screen(chat_id)
            return
        if action == "documents":
            documents_screen(chat_id)
            return
        if action == "help":
            help_screen(chat_id)
            return

    if s["role"] == "logistician":
        RUNTIME_LOGISTICIAN_CHAT_ID = chat_id

        if action == "today":
            show_log_today(chat_id)
            return
        if action == "routes":
            show_log_routes(chat_id)
            return
        if action == "drivers":
            show_log_drivers(chat_id)
            return
        if action == "documents":
            show_log_documents(chat_id)
            return
        if action in ("help", "problem"):
            show_log_help(chat_id)
            return
        if action == "report":
            show_log_report(chat_id)
            return

    set_workspace(chat_id, "Нажмите /start и выберите роль.", [])


def handle_photo(chat_id, message):
    driver_id, st = selected_driver(chat_id)
    if not driver_id:
        set_workspace(chat_id, "Сначала нажмите /start и выберите водителя.", [])
        return

    if st["awaiting_delivery_photo"]:
        handle_delivery_photo(chat_id)
        return

    if session(chat_id)["role"] == "driver" and st["document_mode"]:
        handle_document_photo(chat_id, message)
        return

    set_workspace(
        chat_id,
        "Если это документ, сначала откройте раздел «Документы».",
        [],
    )


def check_wait_timers():
    current = now_ts()

    for driver_id in list(DRIVERS.keys()):
        st = driver_state(driver_id)

        if st["stage"] != "arrived":
            continue
        if not st["arrival_ts"] or st["wait_notified"]:
            continue

        elapsed = current - st["arrival_ts"]
        if elapsed >= WAIT_THRESHOLD_SECONDS:
            st["wait_notified"] = True
            minutes = max(1, int(elapsed / 60))
            notify_help(driver_id, "Простой на точке", minutes)
            notify_driver(
                driver_id,
                f"⏱️ Ожидание превысило "
                f"{int(WAIT_THRESHOLD_SECONDS / 60)} минут. "
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

    handle_text(chat_id, message_id, message.get("text", ""))


# ---------------- MAIN ----------------

def main():
    api("deleteWebhook", {"drop_pending_updates": True})

    # Initialize driver states so logistician can see all 3 drivers immediately.
    for driver_id in DRIVERS:
        driver_state(driver_id)

    print("FENIX dispatch v4 started", flush=True)

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
