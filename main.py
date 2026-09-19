import os
import re
import random
from flask import Flask, request
import vk_api
import requests

# Инициализация Flask приложения
app = Flask(__name__)

# Токены из переменных окружения
VK_TOKEN = os.environ.get("VK_TOKEN")
CONFIRMATION_TOKEN = os.environ.get("CONFIRMATION_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Авторизация в ВК API
vk_session = vk_api.VkApi(token=VK_TOKEN)
vk = vk_session.get_api()

# Актуальная модель Groq API
AI_MODEL = "openai/gpt-oss-120b"


def ask_groq_safety(user_text):
    """Отправляет запрос в Groq API для проверки на оскорбление."""
    if not GROQ_API_KEY:
        print("ОШИБКА: GROQ_API_KEY не установлен!")
        return False, None

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    prompt = (
        f"Проанализируй текст: '{user_text}'. "
        "Определи, является ли это оскорблением, негативом или неуважением в адрес Вики/Викульки/бота. "
        "Ответь строго в формате JSON без дополнительного текста и без markdown-разметки: "
        '{"is_insult": true/false, "reply": "ироничный и колкий ответ боту в 1 предложении"}.'
    )

    data = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=5)
        res_json = response.json()

        if "choices" in res_json and len(res_json["choices"]) > 0:
            content = res_json["choices"][0]["message"]["content"].strip()
            print(f"ОТВЕТ ИИ: {content}")

            # Ищем флаг is_insult
            is_insult = '"is_insult": true' in content.lower()

            # Извлекаем текст ответа
            reply_match = re.search(r'"reply"\s*:\s*"([^"]+)"', content)
            reply = (
                reply_match.group(1)
                if reply_match
                else "Сам такой, между прочим!"
            )

            return is_insult, reply
        else:
            print(f"Ошибка Groq API: {res_json}")
            return False, None
    except Exception as e:
        print(f"Исключение при вызове Groq: {e}")
        return False, None


@app.route("/", methods=["POST"])
def callback():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "not ok"

    event_type = data.get("type")

    # Подтверждение сервера VK
    if event_type == "confirmation":
        return CONFIRMATION_TOKEN

    # Новое сообщение
    if event_type == "message_new":
        message = data.get("object", {}).get("message", {})
        text = message.get("text", "")
        peer_id = message.get("peer_id")

        print(f"ВХОДЯЩЕЕ СООБЩЕНИЕ: {text}")

        # Регулярка для проверки упоминания бота
        if re.search(r"\b(вика|викулька|вике|вику)\b", text, re.IGNORECASE):
            print(f"Имя распознано в сообщении: '{text}'")

            is_insult, ai_reply = ask_groq_safety(text)

            if is_insult:
                print("ИИ распознал оскорбление! Отправляем ответ.")
                vk.messages.send(
                    peer_id=peer_id,
                    message=ai_reply,
                    random_id=random.randint(1, 1000000),
                )
            else:
                print("ИИ счёл сообщение безопасным.")

        return "ok"

    return "ok"


@app.route("/", methods=["GET"])
def index():
    return "Bot is running!"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
