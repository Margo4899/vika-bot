import os
import re
import json
import requests
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

# Переменные окружения
VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE') or os.environ.get('CONFIRMATION_TOKEN', '')
AI_API_KEY = os.environ.get('AI_API_KEY') or os.environ.get('GROQ_API_KEY', '')

# Переменные для постоянного хранения через JSONBin
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID', '')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY', '')

AI_MODELS = [
    'llama-3.3-70b-versatile',
    'llama3-70b-8192',
    'mixtral-8x7b-32768'
]
AI_URL = 'https://api.groq.com/openai/v1/chat/completions'

# Отрывки и корни слов
BAD_WORDS = [
    # Хамство, наглость, дерзость
    r'хам', r'нахал', r'нагл', r'дерз', r'выпендр', r'понт',
    
    # Оскорбления, дурость, тупость
    r'дур', r'туп', r'идиот', r'клоун', r'твар', r'гнид', r'урод', r'мраз',
    r'придур', r'кончен', r'чушпан', r'бес', r'сволоч', r'шлюх', r'бред',
    
    # Наезды, эмоции, удивления/агрессия
    r'ахрин', r'охрин', r'афиг', r'офиг', r'ахуе', r'охуе',
    
    # Посылы и приказы
    r'заткн', r'завал', r'закрой', r'пошел', r'пошла', r'соси', r'отвал',
    r'свал', r'исчез', r'съеб', r'оффн', r'пизд'
]
BAD_WORDS_PATTERN = re.compile(r'|'.join(BAD_WORDS), re.IGNORECASE)

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None

def load_stats():
    """Загружает статистику из облака JSONBin"""
    if not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        print("ВНИМАНИЕ: Ключи JSONBin не настроены. Используется локальная память.")
        return {}
    
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
    headers = {"X-Master-Key": JSONBIN_API_KEY}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            record = res.json().get('record', {})
            return {int(k): {int(uk): uv for uk, uv in v.items()} for k, v in record.items()}
    except Exception as e:
        print("Ошибка загрузки статистики из облака:", e)
    return {}

def save_stats(stats_data):
    """Сохраняет статистику в облако JSONBin"""
    if not JSONBIN_BIN_ID or not JSONBIN_API_KEY:
        return
    
    url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
    headers = {
        "Content-Type": "application/json",
        "X-Master-Key": JSONBIN_API_KEY
    }
    try:
        requests.put(url, json=stats_data, headers=headers, timeout=5)
        print("Статистика успешно сохранена в облаке!")
    except Exception as e:
        print("Ошибка сохранения статистики в облако:", e)

stats = load_stats()
user_names_cache = {}

def send_message(peer_id, text):
    if vk:
        try:
            vk.method('messages.send', {
                'peer_id': peer_id,
                'message': text,
                'random_id': 0
            })
        except Exception as e:
            print("ОШИБКА ВК API:", e)

def get_user_name(user_id):
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
        print("ОШИБКА: НЕТ AI_API_KEY!")
        return True, "участников чата", "🚨 Фиксирую подкол! Счётчик токсичности пополнен."

    prompt = (
        f"Проанализируй сообщение из чата: '{text}'.\n\n"
        "Сгенерируй короткий, ироничный и смешной комментарий бота о зафиксированном хамстве или наезде.\n"
        "Ответь строго в формате:\n"
        "ТОКСИК | [Кого задели: Вику / Ксюшу / Риту / участников чата] | [Короткий шуточный комментарий бота]\n\n"
        "Пример:\n"
        "ТОКСИК | участников чата | 🚨 Всплеск токсичности зафиксирован! Включаю режим миротворца."
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
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=5)
            result = response.json()
            
            if 'choices' in result and len(result['choices']) > 0:
                full_content = result['choices'][0]['message']['content'].strip()
                print(f"ОТВЕТ ИИ ({model_name}): {full_content}")
                
                parts = full_content.split("|")
                if len(parts) >= 3:
                    target_name = parts[1].strip()
                    ai_comment = parts[2].strip()
                    return True, target_name, ai_comment
                elif len(parts) == 2:
                    return True, "участников чата", parts[1].strip()
        except Exception as e:
            print(f"Ошибка запроса к ИИ ({model_name}):", e)
            continue

    # Запасной вариант, если ИИ недоступен
    return True, "участников чата", "🚨 Фиксирую подкол! Счётчик токсичности пополнен."

@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    if not data:
        return 'ok'

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
                send_message(peer_id, "📊 Пока никто не токсичил в беседе!")
            else:
                sorted_users = sorted(stats[peer_id].items(), key=lambda x: x[1], reverse=True)
                top_text = "🏆 ТОП самых острых на язык в беседе:\n\n"
                for i, (u_id, count) in enumerate(sorted_users[:10], 1):
                    user_name = get_user_name(u_id)
                    top_text += f"{i}. [id{u_id}|{user_name}] — {count} замеченных наездов\n"
                send_message(peer_id, top_text)
            return 'ok'

        # Проверка триггерных слов
        if NAMES_PATTERN.search(text) or BAD_WORDS_PATTERN.search(text):
            print(f"Найден триггер в сообщении: '{text}'")
            is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
            
            if is_toxic:
                stats[peer_id][from_id] = stats[peer_id].get(from_id, 0) + 1
                save_stats(stats)
                
                user_count = stats[peer_id][from_id]
                user_name = get_user_name(from_id)
                
                reply = (
                    f"{ai_comment}\n\n"
                    f"🛡️ Фиксация: [id{from_id}|{user_name}], зафиксирован наезд на {target_name}!\n"
                    f"📈 Ваша статистика в банке токсичности: {user_count}"
                )
                send_message(peer_id, reply)
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
