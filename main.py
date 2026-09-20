import os
import re
import json
import random
import requests
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

# Переменные окружения из Render
VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID', '')
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY', '')

# Твой список моделей
AI_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

# Эндпоинт Groq API
AI_URL = 'https://api.groq.com/openai/v1/chat/completions'

FALLBACK_COMMENTS = [
    "🚨 Э, слышь! Фиксирую подкол! Счётчик токсичности пополнен.",
    "⚠️ Ого, какая дерзость! Фиксируем этот наезд в базу.",
    "🛡️ Ты на кого батон крошишь? Система всё зафиксировала!",
    "🌶️ Было остро, но Хамулька всё запишет!",
    "👀 Внимание! Замечен несанкционированный наезд.",
    "📉 Градус дружелюбия стремительно упал!"
]

BAD_WORDS = [
    r'хам', r'нахал', r'нагл', r'дерз', r'выпендр', r'понт',
    r'дур', r'туп', r'идиот', r'клоун', r'твар', r'гнид', r'урод', r'мраз',
    r'придур', r'кончен', r'чушпан', r'бес', r'сволоч', r'шлюх', r'бред',
    r'ахрин', r'охрин', r'афиг', r'офиг', r'ахуе', r'охуе',
    r'заткн', r'завал', r'закрой', r'пошел', r'пошла', r'соси', r'отвал',
    r'свал', r'исчез', r'съеб', r'оффн', r'пизд'
]
BAD_WORDS_PATTERN = re.compile(r'|'.join(BAD_WORDS), re.IGNORECASE)

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None

def load_stats():
    """Загружает статистику из облака JSONBin"""
    bin_id = JSONBIN_BIN_ID.strip().split('/')[-1] if JSONBIN_BIN_ID else ""
    api_key = JSONBIN_API_KEY.strip() if JSONBIN_API_KEY else ""
    
    if not bin_id or not api_key:
        print("ВНИМАНИЕ: JSONBin не настроен.")
        return {}
    
    url = f"https://api.jsonbin.io/v3/b/{bin_id}/latest"
    headers = {"X-Master-Key": api_key}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            record = data.get('record', {})
            loaded = {}
            for k, v in record.items():
                if isinstance(v, dict):
                    loaded[int(k)] = {int(uk): int(uv) for uk, uv in v.items()}
            print("Статистика успешно загружена из облака!")
            return loaded
        else:
            print(f"Ошибка загрузки JSONBin [{res.status_code}]: {res.text}")
    except Exception as e:
        print("Ошибка загрузки статистики:", e)
    return {}

def save_stats(stats_data):
    """Сохраняет статистику в облако JSONBin"""
    bin_id = JSONBIN_BIN_ID.strip().split('/')[-1] if JSONBIN_BIN_ID else ""
    api_key = JSONBIN_API_KEY.strip() if JSONBIN_API_KEY else ""
    
    if not bin_id or not api_key:
        return
    
    url = f"https://api.jsonbin.io/v3/b/{bin_id}"
    headers = {
        "Content-Type": "application/json",
        "X-Master-Key": api_key
    }
    try:
        serializable_stats = {str(k): {str(uk): uv for uk, uv in v.items()} for k, v in stats_data.items()}
        res = requests.put(url, json=serializable_stats, headers=headers, timeout=5)
        if res.status_code == 200:
            print("Статистика успешно сохранена в облаке!")
        else:
            print(f"Ошибка сохранения JSONBin [{res.status_code}]: {res.text}")
    except Exception as e:
        print("Ошибка сохранения статистики:", e)

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
            print("Ошибка получения имени:", e)
    return "Участник"

def analyze_and_generate_response(text):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        print("ОШИБКА: AI_API_KEY пустой!")
        return False, "участников чата", ""

    system_instruction = (
        "Тебя зовут Хамулька. Ты — дерзкая девчонка-пацанка в беседе ВК, отвечающая в стиле гопницы с легким дворовым сленгом, сарказмом и дерзостью.\n\n"
        "Твоя задача — жестко фиксировать ЛЮБУЮ токсичность, даже самую легкую!\n\n"
        "КРИТЕРИИ:\n"
        "1. Засчитывай как ТОКСИК любая грубость, обзывательства (даже легкие: 'хам', 'выпендрежник', 'душнила', 'клоун'), сарказм, подколы, претензии или наезды.\n"
        "2. Отвечай НЕ_ТОКСИК ТОЛЬКО если фраза полностью добрая, вежливая, нейтральная (например: 'Привет', 'Доброе утро', 'Спасибо', 'Как дела?') или просто ласковое/обычное имя без намека на наезд.\n\n"
        "ФОРМАТ ОТВЕТА ПРИ ТОКСИЧНОСТИ:\n"
        "ТОКСИК | [Кого задели: Вику / Ксюшу / Риту / участников чата] | [Короткий смешной комментарий от Хамульки в стиле гопницы с сарказмом]"
    )

    user_prompt = f"Проанализируй сообщение из чата: '{text}'"
    
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json"
    }

    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.7
        }
        try:
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=7)
            result = response.json()
            
            if response.status_code == 200 and 'choices' in result and len(result['choices']) > 0:
                full_content = result['choices'][0]['message']['content'].strip()
                full_content = re.sub(r'<think>.*?</think>', '', full_content, flags=re.DOTALL).strip()
                
                print(f"ОТВЕТ ИИ ({model_name}): {full_content}")

                if "НЕ_ТОКСИК" in full_content or "НЕ ТОКСИК" in full_content:
                    return False, "участников чата", ""
                
                parts = full_content.split("|")
                if len(parts) >= 3:
                    return True, parts[1].strip(), parts[2].strip()
                elif len(parts) == 2:
                    return True, "участников чата", parts[1].strip()
                else:
                    return True, "участников чата", random.choice(FALLBACK_COMMENTS)
            else:
                print(f"Сбой ИИ ({model_name}) [{response.status_code}]: {result}")
        except Exception as e:
            print(f"Ошибка запроса к ИИ ({model_name}):", e)
            continue

    return False, "участников чата", ""

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

        # Команда просмотра рейтинга
        if '!топ' in text.lower() or '!рейтинг' in text.lower():
            announce_text = "🎁 Кто первый наберет 100 баллов токсичности, того ждет признание от Хамульки и секретный приз!\n\n"
            if not stats[peer_id]:
                send_message(peer_id, announce_text + "📊 Пока никто не токсичил в беседе!")
            else:
                sorted_users = sorted(stats[peer_id].items(), key=lambda x: x[1], reverse=True)
                top_text = announce_text + "🏆 ТОП самых острых на язык в беседе:\n\n"
                for i, (u_id, count) in enumerate(sorted_users[:10], 1):
                    user_name = get_user_name(u_id)
                    top_text += f"{i}. [id{u_id}|{user_name}] — {count} замеченных наездов\n"
                send_message(peer_id, top_text)
            return 'ok'

        # Проверяем ключевые слова
        if NAMES_PATTERN.search(text) or BAD_WORDS_PATTERN.search(text):
            is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
            
            # Начисляем баллы ТОЛЬКО если ИИ подтвердил токсичность
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
