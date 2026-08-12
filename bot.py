import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"
SEEN_UPDATES = set()


def api(method, payload=None):
    payload = payload or {}
    data = urllib.parse.urlencode({
        k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        for k, v in payload.items()
    }).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"ok": False, "error_code": e.code, "description": body}


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return api("sendMessage", payload)


def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    else:
        payload["reply_markup"] = {"inline_keyboard": []}
    return api("editMessageText", payload)


def answer_callback(callback_id, text=None):
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    api("answerCallbackQuery", payload)


def role_keyboard():
    return [
        [{"text": "🚚 Водитель", "callback_data": "role:driver"}],
        [{"text": "🧭 Логист", "callback_data": "role:logistician"}],
        [{"text": "👤 Клиент", "callback_data": "role:client"}],
    ]


def driver_menu():
    return [
        [{"text": "🚚 Статус моей перевозки", "callback_data": "driver:status"}],
        [{"text": "📍 Сообщить этап", "callback_data": "driver:stage"}],
        [{"text": "⚠️ Сообщить проблему", "callback_data": "driver:problem"}],
        [{"text": "📄 Документы", "callback_data": "driver:docs"}],
        [{"text": "❓ Вопрос логисту", "callback_data": "driver:question"}],
        [{"text": "↩️ Сменить роль", "callback_data": "start"}],
    ]


def stage_menu():
    return [
        [{"text": "🚚 Выехал на погрузку", "callback_data": "stage:to_loading"}],
        [{"text": "📍 Прибыл на погрузку", "callback_data": "stage:arrived_loading"}],
        [{"text": "📦 Начали погрузку", "callback_data": "stage:loading"}],
        [{"text": "✅ Погрузка завершена", "callback_data": "stage:loaded"}],
        [{"text": "🚛 Выехал на выгрузку", "callback_data": "stage:to_unloading"}],
        [{"text": "📍 Прибыл на выгрузку", "callback_data": "stage:arrived_unloading"}],
        [{"text": "📤 Начали выгрузку", "callback_data": "stage:unloading"}],
        [{"text": "✅ Выгрузка завершена", "callback_data": "stage:unloaded"}],
        [{"text": "↩️ Назад", "callback_data": "role:driver"}],
    ]


def problem_menu():
    return [
        [{"text": "⏱ Долго жду на погрузке", "callback_data": "problem:loading_wait"}],
        [{"text": "⏱ Долго жду на выгрузке", "callback_data": "problem:unloading_wait"}],
        [{"text": "🚫 Не грузят", "callback_data": "problem:loading_refused"}],
        [{"text": "🚫 Не принимают", "callback_data": "problem:unloading_refused"}],
        [{"text": "🔧 Поломка машины", "callback_data": "problem:breakdown"}],
        [{"text": "📄 Нет документа", "callback_data": "problem:document"}],
        [{"text": "✏️ Другая проблема", "callback_data": "problem:other"}],
        [{"text": "↩️ Назад", "callback_data": "role:driver"}],
    ]


def logistician_menu():
    return [
        [{"text": "🔴 Что требует решения?", "callback_data": "log:exceptions"}],
        [{"text": "🚚 Статус перевозки", "callback_data": "log:status"}],
        [{"text": "🧾 История событий", "callback_data": "log:history"}],
        [{"text": "↩️ Сменить роль", "callback_data": "start"}],
    ]


def client_menu():
    return [
        [{"text": "📍 Где машина / какой статус?", "callback_data": "client:status"}],
        [{"text": "⏱ Когда прибудет?", "callback_data": "client:eta"}],
        [{"text": "📄 Документы", "callback_data": "client:docs"}],
        [{"text": "❓ Задать вопрос", "callback_data": "client:question"}],
        [{"text": "↩️ Сменить роль", "callback_data": "start"}],
    ]


def edit_once(q, text, keyboard=None):
    """Use Telegram's own message state as a duplicate guard.
    If two workers handle the same click, only the first edit changes the message;
    the second gets 'message is not modified' and does not create a duplicate reply.
    """
    chat_id = q["message"]["chat"]["id"]
    message_id = q["message"]["message_id"]
    result = edit(chat_id, message_id, text, keyboard)
    return bool(result.get("ok"))


def handle_callback(q):
    data = q["data"]
    answer_callback(q["id"])

    if data == "noop":
        return

    if data == "start":
        edit_once(q, "Fenix Logistics — TEST BOT\n\nКто вы в этом сценарии?", role_keyboard())
        return

    if data == "role:driver":
        edit_once(q, "Роль: Водитель\n\nЧто вы хотите сделать?", driver_menu())
        return

    if data == "role:logistician":
        edit_once(q, "Роль: Логист\n\nЧто вы хотите проверить?", logistician_menu())
        return

    if data == "role:client":
        edit_once(q, "Роль: Клиент\n\nЧто вы хотите узнать?", client_menu())
        return

    if data == "driver:stage":
        edit_once(q, "Сообщить этап перевозки\n\nЧто произошло?", stage_menu())
        return

    if data == "driver:problem":
        edit_once(q, "Сообщить проблему\n\nЧто произошло?", problem_menu())
        return

    if data == "driver:status":
        edit_once(
            q,
            "DEMO-001\nСтатус: машина назначена.\n\nЭто тестовые данные.",
            driver_menu(),
        )
        return

    if data == "driver:docs":
        edit_once(
            q,
            "📄 Отправьте фото или файл документа.\n\n"
            "В этой тестовой версии файл пока не распознаётся автоматически.",
            driver_menu(),
        )
        return

    if data == "driver:question":
        edit_once(q, "❓ Напишите вопрос логисту обычным сообщением.", driver_menu())
        return

    if data.startswith("stage:"):
        labels = {
            "stage:to_loading": "Выехал на погрузку",
            "stage:arrived_loading": "Прибыл на погрузку",
            "stage:loading": "Начали погрузку",
            "stage:loaded": "Погрузка завершена",
            "stage:to_unloading": "Выехал на выгрузку",
            "stage:arrived_unloading": "Прибыл на выгрузку",
            "stage:unloading": "Начали выгрузку",
            "stage:unloaded": "Выгрузка завершена",
        }
        edit_once(
            q,
            f"✅ Этап зафиксирован: {labels.get(data, data)}\n\n"
            "Пока без реального изменения перевозки — тестируем UX.",
            driver_menu(),
        )
        return

    if data.startswith("problem:"):
        labels = {
            "problem:loading_wait": "Долго жду на погрузке",
            "problem:unloading_wait": "Долго жду на выгрузке",
            "problem:loading_refused": "Не грузят",
            "problem:unloading_refused": "Не принимают",
            "problem:breakdown": "Поломка машины",
            "problem:document": "Нет документа",
            "problem:other": "Другая проблема",
        }
        edit_once(
            q,
            f"⚠️ Проблема зарегистрирована: {labels.get(data, data)}\n\n"
            "В следующей версии здесь появится эскалация логисту.",
            driver_menu(),
        )
        return

    if data == "log:exceptions":
        edit_once(q, "🔴 DEMO: открытых проблем пока нет.", logistician_menu())
        return

    if data == "log:status":
        edit_once(q, "DEMO-001\nСтатус: DRIVER_ASSIGNED", logistician_menu())
        return

    if data == "log:history":
        edit_once(q, "🧾 DEMO: история событий пока пуста.", logistician_menu())
        return

    if data == "client:status":
        edit_once(q, "DEMO-001\nПодтверждённый статус: машина назначена.", client_menu())
        return

    if data == "client:eta":
        edit_once(q, "ETA пока не рассчитан — GPS не подключён.", client_menu())
        return

    if data == "client:docs":
        edit_once(q, "Документы пока недоступны в тестовой версии.", client_menu())
        return

    if data == "client:question":
        edit_once(q, "Напишите вопрос обычным сообщением.", client_menu())
        return


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    if text == "/start":
        send(chat_id, "Fenix Logistics — TEST BOT\n\nКто вы в этом сценарии?", role_keyboard())
    else:
        send(
            chat_id,
            f"Получил сообщение:\n«{text or '[не текст]'}»\n\n"
            "Пока тестируем меню. Нажмите /start, чтобы начать заново.",
        )


def main():
    offset = None
    print("Fenix test bot v3 started")
    while True:
        try:
            payload = {"timeout": 30}
            if offset is not None:
                payload["offset"] = offset
            result = api("getUpdates", payload)

            for update in result.get("result", []):
                update_id = update["update_id"]

                # In-process duplicate guard.
                if update_id in SEEN_UPDATES:
                    continue
                SEEN_UPDATES.add(update_id)
                if len(SEEN_UPDATES) > 1000:
                    SEEN_UPDATES.clear()
                    SEEN_UPDATES.add(update_id)

                offset = update_id + 1

                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])

        except Exception as e:
            print("Polling error:", repr(e))
            time.sleep(3)


if __name__ == "__main__":
    main()
