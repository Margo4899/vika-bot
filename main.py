import os
import re
import json
import random
import requests
import threading
import psycopg2
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

# Переменные окружения из Render
VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '')

AI_MODELS = [
    'openai/gpt-oss-120b',
    'openai/gpt-oss-20b',
    'qwen/qwen3.8-27b',
    'qwen/qwen3.6-27b'
]

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
    r'дур', r'туп', r 'идиот', r'клоун', r'твар', r'гнид', r'урод', r'мраз',
    r'придур', r'кончен', r'чушпан', r'бес', r'сволоч', r'шлюх', r'бред',
    r'ахрин', r'охрин', r'афиг', r'офиг', r'ахуе', r'охуе',
    r'заткн', r'завал', r'закрой', r'пошел', r'пошла', r'соси', r'отвал',
    r'свал', r'исчез', r'съеб', r'оффн', r'пизд'
]
BAD_WORDS_PATTERN = re.compile(r'|'.join(BAD_WORDS), re.IGNORECASE)

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
processed_msg_ids = set()
user_names_cache = {}

def get_db_connection():
    """Подключение к Supabase PostgreSQL с выводом ошибок"""
    if not DATABASE_URL:
        print("❌ [БД] ОШИБКА: Переменная DATABASE_URL пустая или не найдена в Render!")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"❌ [БД] Ошибка подключения к PostgreSQL: {e}")
        return None

def add_toxicity_point(peer_id, user_id):
    """Атомарное добавление +1 балла с детальным логированием"""
    print(f"🔄 [БД] Попытка добавить балл для peer_id={peer_id}, user_id={user_id}...")
    conn = get_db_connection()
    if not conn:
        print("❌ [БД] Отмена операции: нет соединения с базами данных.")
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO toxicity_stats (peer_id, user_id, score)
                VALUES (%s, %s, 1)
                ON CONFLICT (peer_id, user_id)
                DO UPDATE SET score = toxicity_stats.score + 1
                RETURNING score;
            """, (peer_id, user_id))
            
            result = cur.fetchone()
            if result:
                new_score = result[0]
                conn.commit()
                print(f"✅ [БД] Успешно! Новый счёт пользователя {user_id}: {new_score}")
                return new_score
            else:
                print("⚠️ [БД] Запрос выполнился, но RETURNING score вернул None")
                return 0
    except Exception as e:
        print(f"❌ [БД] Исключение при выполнении SQL-запроса: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()

def get_top_users(peer_id):
    """Получение Топ-10 пользователей с логами"""
    print(f"🔄 [БД] Запрос ТОПа для peer_id={peer_id}...")
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, score FROM toxicity_stats
                WHERE peer_id = %s
                ORDER BY score DESC
                LIMIT 10;
            """, (peer_id,))
            rows = cur.fetchall()
            print(f"✅ [БД] Найдено строк в ТОПе: {len(rows)}")
            return rows
    except Exception as e:
        print(f"❌ [БД] Ошибка при получении ТОПа: {e}")
        return []
    finally:
        conn.close()

def send_message(peer_id, text):
    if vk:
        try:
            vk.method('messages.send', {
                'peer_id': peer_id,
                'message': text,
                'random_id': 0
            })
            print(f"✉️ [ВК] Сообщение успешно отправлено в peer_id={peer_id}")
        except Exception as e:
            print(f"❌ [ВК] Ошибка отправки сообщения через VK API: {e}")

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
            print(f"⚠️ [ВК] Не удалось получить имя пользователя {user_id}: {e}")
    return "Участник"

def analyze_and_generate_response(text):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        print("⚠️ [ИИ] AI_API_KEY отсутствует, пропуск анализа ИИ")
        return False, "участников чата", ""

    system_instruction = (
        "Тебя зовут Хамулька. Ты — дерзкая девчонка-пацанка в беседе ВК, отвечающая в стиле гопницы с легким дворовым сленгом, сарказмом и дерзостью.\n\n"
        "Твоя задача — жестко фиксировать ЛЮБУЮ токсичность, даже самую легкую!\n\n"
        "КРИТЕРИИ:\n"
        "1. Засчитывай как ТОКСИК любую грубость, обзывательства (даже легкие: 'хам', 'выпендрежник', 'душнила', 'клоун'), сарказм, подколы, претензии или наезды.\n"
        "2. Отвечай НЕ_ТОКСИК ТОЛЬКО если фраза полностью добрая, вежливая, нейтральная (например: 'Привет', 'Доброе утро', 'Спасибо', 'Как дела?') или просто ласковое/обычное имя без намека на наезд.\n\n"
        "ФОРМАТ ОТВЕТА ПРИ ТОКСИЧНОСТИ:\n"
        "ТОКСИК | [Кого задели: Вику / Ксюшу / Риту / участников чата] | [Короткий смешной комментарий от Хамульки в стиле гопницы с сарказмом]"
    )

    user_prompt = f"Проанализируй сообщение из чата: '{text}'"
    headers = {"Authorization": f"Bearer {clean_key}", "Content-Type": "application/json"}

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
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=8)
            result = response.json()
            
            if response.status_code == 200 and 'choices' in result and len(result['choices']) > 0:
                full_content = result['choices'][0]['message']['content'].strip()
                full_content = re.sub(r'<think>.*?</think>', '', full_content, flags=re.DOTALL).strip()

                if "НЕ_ТОКСИК" in full_content or "НЕ ТОКСИК" in full_content:
                    return False, "участников чата", ""
                
                parts = full_content.split("|")
                if len(parts) >= 3:
                    return True, parts[1].strip(), parts[2].strip()
                elif len(parts) == 2:
                    return True, "участников чата", parts[1].strip()
                else:
                    return True, "участников чата", random.choice(FALLBACK_COMMENTS)
        except Exception as e:
            print(f"⚠️ [ИИ] Ошибка модели {model_name}: {e}")
            continue

    return False, "участников чата", ""

def process_message_async(text, peer_id, from_id):
    # Команда рейтинга
    if '!топ' in text.lower() or '!рейтинг' in text.lower():
        announce_text = "🎁 Кто первый наберет 100 баллов токсичности, того ждет признание от Хамульки и секретный приз!\n\n"
        top_users = get_top_users(peer_id)
        if not top_users:
            send_message(peer_id, announce_text + "📊 Пока никто не токсичил в беседе!")
        else:
            top_text = announce_text + "🏆 ТОП самых острых на язык в беседе:\n\n"
            for i, (u_id, count) in enumerate(top_users, 1):
                user_name = get_user_name(u_id)
                top_text += f"{i}. [id{u_id}|{user_name}] — {count} замеченных наездов\n"
            send_message(peer_id, top_text)
        return

    # Проверка на ключевые слова
    if NAMES_PATTERN.search(text) or BAD_WORDS_PATTERN.search(text):
        print(f"🔍 [Анализ] Найдено совпадение по ключевым словам в тексте: '{text}'")
        is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
        
        if is_toxic:
            user_count = add_toxicity_point(peer_id, from_id)
            user_name = get_user_name(from_id)
            
            reply = (
                f"{ai_comment}\n\n"
                f"🛡️ Фиксация: [id{from_id}|{user_name}], зафиксирован наезд на {target_name}!\n"
                f"📈 Ваша статистика в банке токсичности: {user_count}"
            )
            send_message(peer_id, reply)

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
        
        msg_id = message.get('id') or message.get('conversation_message_id')
        text = message.get('text', '')
        peer_id = message.get('peer_id')
        from_id = message.get('from_id')
        
        if msg_id:
            if msg_id in processed_msg_ids:
                return 'ok'
            processed_msg_ids.add(msg_id)
            if len(processed_msg_ids) > 1000:
                processed_msg_ids.clear()

        if peer_id and from_id:
            threading.Thread(target=process_message_async, args=(text, peer_id, from_id)).start()
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
