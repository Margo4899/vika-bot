import os
import re
import requests
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

# Получение переменных окружения
VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE') or os.environ.get('CONFIRMATION_TOKEN', '')
AI_API_KEY = os.environ.get('AI_API_KEY') or os.environ.get('GROQ_API_KEY', '')

# Актуальные модели Groq
AI_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]
AI_URL = 'https://api.groq.com/openai/v1/chat/completions'

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
stats = {}
user_names_cache = {}  # Кэш для имён, чтобы не спамить запросами к ВК

def send_message(peer_id, text):
    if vk:
        try:
            vk.method('messages.send', {
                'peer_id': peer_id,
                'message': text,
                'random_id': 0
            })
            print(f"УСПЕШНО ОТПРАВЛЕНО В ВК [{peer_id}]: {text}")
        except Exception as e:
            print("ОШИБКА ВК API:", e)

def get_user_name(user_id):
    """Получает имя и фамилию пользователя по его ID"""
    if user_id in user_names_cache:
        return user_names_cache[user_id]
    
    if vk:
        try:
            res = vk.method('users.get', {'user_ids': user_id})
            if res and len(res) > 0:
                first_name = res[0].get('first_name', '')
                last_name = res[0].get('last_name', '')
                full_name = f"{first_name} {last_name}".strip()
                if full_name:
                    user_names_cache[user_id] = full_name
                    return full_name
        except Exception as e:
            print(f"Ошибка получения имени для id{user_id}:", e)
            
    return "Участник"

def analyze_and_generate_response(text):
    if not AI_API_KEY:
        print("ОШИБКА: Ключ API не найден в переменных окружения!")
        return False, None, None

    prompt = (
        f"Проанализируй сообщение из чата: '{text}'.\n\n"
        "Твоя задача:\n"
        "1. Определи, адресован ли тут подкол, хамство, сарказм, ирония или наезд на девушку по имени Вика, Ксюша или Рита (или их формы/опечатки).\n"
        "2. Если наезда/хамства НЕТ или имя не относится к этой троице, ответь строго одним словом: НОРМА.\n"
        "3. Если наезд/хамство ЕСТЬ, ответь строго в таком формате без лишних слов и рассуждений:\n"
        "ТОКСИК | [Имя девушки в винительном падеже (Вику / Ксюшу / Риту)] | [Короткий, смешной и ироничный комментарий бота]\n\n"
        "Пример ответа при хамстве на Вику:\n"
        "ТОКСИК | Вику | 🚨 Обнаружена попытка задеть Вику! Наш виртуальный щит уже активирован."
    )
    
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }

    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7
        }
        try:
            print(f"Пробуем модель: {model_name}...")
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=5)
            result = response.json()
            
            if 'error' in result:
                print(f"Модель {model_name} вернула ошибку: {result['error'].get('message')}")
                continue

            print(f"УСПЕХ НА МОДЕЛИ [{model_name}]:", result)
            if 'choices' in result and len(result['choices']) > 0:
                full_content = result['choices'][0]['message']['content'].strip()
                
                if "ТОКСИК" in full_content.upper():
                    toxic_index = full_content.upper().find("ТОКСИК")
                    clean_answer = full_content[toxic_index:].strip()
                    
                    parts = clean_answer.split("|")
                    if len(parts) >= 3:
                        target_name = parts[1].strip()
                        ai_comment = parts[2].strip()
                        return True, target_name, ai_comment
                    return True, "девушку", "🚨 Фиксирую подкол! Счётчик токсичности пополнен."
                return False, None, None
        except Exception as e:
            print(f"Исключение при вызове {model_name}:", e)
            continue

    print("ОШИБКА: Ни одна из моделей не ответила.")
    return False, None, None

@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    if not data:
        return 'ok'

    print("ВХОДЯЩИЕ ДАННЫЕ ОТ ВК:", data)

    event_type = data.get('type')

    if event_type == 'confirmation':
        return CONFIRMATION_CODE
    
    if event_type == 'message_new':
        obj = data.get('object', {})
        message = obj.get('message', obj)
        
        text = message.get('text', '')
        peer_id = message.get('peer_id')
        from_id = message.get('from_id')
        
        if not peer_id or not from_id:
            return 'ok'

        if peer_id not in stats:
            stats[peer_id] = {}

        if '!топ' in text.lower() or '!рейтинг' in text.lower():
            if not stats[peer_id]:
                send_message(peer_id, "📊 Пока никто не подкалывал девчонок!")
            else:
                sorted_users = sorted(stats[peer_id].items(), key=lambda x: x[1], reverse=True)
                top_text = "🏆 ТОП самых острых на язык в беседе:\n\n"
                for i, (u_id, count) in enumerate(sorted_users[:10], 1):
                    user_name = get_user_name(u_id)
                    top_text += f"{i}. [id{u_id}|{user_name}] — {count} замеченных наездов\n"
                send_message(peer_id, top_text)
            return 'ok'

        if NAMES_PATTERN.search(text):
            print(f"Имя распознано в сообщении: '{text}'")
            is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
            
            if is_toxic:
                stats[peer_id][from_id] = stats[peer_id].get(from_id, 0) + 1
                user_count = stats[peer_id][from_id]
                
                user_name = get_user_name(from_id)
                
                reply = (
                    f"{ai_comment}\n\n"
                    f"🛡️ Защита: [id{from_id}|{user_name}], зафиксирован наезд на {target_name}!\n"
                    f"📈 Ваша статистика в банке токсичности: {user_count}"
                )
                send_message(peer_id, reply)
            else:
                print("ИИ счёл сообщение безопасным или произошла ошибка.")
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
