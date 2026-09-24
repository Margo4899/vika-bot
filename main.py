import os
import re
import json
import random
import requests
import threading
import traceback
import psycopg2
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
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
    r'дур', r'туп', r'идиот', r'клоун', r'твар', r'гнид', r'урод', r'мраз',
    r'придур', r'кончен', r'чушпан', r'бес', r'сволоч', r'шлюх', r'бред',
    r'ахрин', r'охрин', r'афиг', r'офиг', r'ахуе', r'охуе',
    r'заткн', r'завал', r'закрой', r'пошел', r'пошла', r'соси', r'отвал',
    r'свал', r'исчез', r'съеб', r'оффн', r'пизд'
]
BAD_WORDS_PATTERN = re.compile(r'|'.join(BAD_WORDS), re.IGNORECASE)

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
processed_msg_ids = set()
user_names_cache = {}

# --- Маскирование пароля для безопасного логирования URL ---
def mask_db_url(url):
    if not url:
        return "НЕ ЗАДАН"
    return re.sub(r':([^@]+)@', ':****@', url)

print(f"🔧 [СТАРТ] DATABASE_URL: {mask_db_url(DATABASE_URL)}")


# --- 2. РАБОТА С БАЗОЙ ДАННЫХ (С ПОЛНОЙ ОТЛАДКОЙ) ---

def get_db_connection():
    """Подключение к PostgreSQL с детальным логированием ошибок"""
    if not DATABASE_URL:
        print("❌ [БД] КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL пуста в Render!")
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"❌ [БД] Ошибка подключения к PostgreSQL:")
        print(f"   Тип ошибки: {type(e).__name__}")
        print(f"   Детали: {e}")
        return None

def init_db_structure():
    """Проверка и автоматическое создание/исправление таблицы в БД при старте"""
    conn = get_db_connection()
    if not conn:
        print("⚠️ [БД-ИНИЦИАЛИЗА] Не удалось подключиться к БД для проверки таблицы.")
        return
    try:
        with conn.cursor() as cur:
            # Создаём таблицу, если её нет
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_stats (
                    peer_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    score INT DEFAULT 0,
                    PRIMARY KEY (peer_id, user_id)
                );
            """)
            conn.commit()
            print("✅ [БД-ИНИЦИАЛИЗА] Таблица toxicity_stats успешно проверена/создана с PRIMARY KEY(peer_id, user_id).")
    except Exception as e:
        print(f"❌ [БД-ИНИЦИАЛИЗА] Ошибка проверки структуры таблицы: {e}")
        traceback.print_exc()
        conn.rollback()
    finally:
        conn.close()

# Запускаем проверку структуры БД при загрузке скрипта
init_db_structure()

def add_toxicity_point(peer_id, user_id):
    """Гарантированное добавление +1 балла с полным логированием каждого шага"""
    print(f"\n==================== [БД ОБНОВЛЕНИЕ] ====================")
    print(f"👉 Запрос на плюс балл: peer_id={peer_id} (тип: {type(peer_id)}), user_id={user_id} (тип: {type(user_id)})")
    
    conn = get_db_connection()
    if not conn:
        print("❌ [БД] Отмена операции: нет активного соединения с базой данных!")
        print("=========================================================\n")
        return 0
    
    try:
        with conn.cursor() as cur:
            print("⏳ [БД] Выполняем UPSERT запрос (INSERT ... ON CONFLICT)...")
            
            sql_upsert = """
                INSERT INTO toxicity_stats (peer_id, user_id, score)
                VALUES (%s, %s, 1)
                ON CONFLICT (peer_id, user_id)
                DO UPDATE SET score = toxicity_stats.score + 1;
            """
            cur.execute(sql_upsert, (peer_id, user_id))
            print(f"   Успешно выполнено cur.execute()! Затронуто строк: {cur.rowcount}")

            print("⏳ [БД] Выполняем commit()...")
            conn.commit()
            print("   Успешно зафиксировано (committed)!")

            print("⏳ [БД] Запрашиваем текущее количество баллов...")
            sql_select = """
                SELECT score FROM toxicity_stats
                WHERE peer_id = %s AND user_id = %s;
            """
            cur.execute(sql_select, (peer_id, user_id))
            row = cur.fetchone()
            
            if row:
                current_score = row[0]
                print(f"🎉 [БД УСПЕХ] Актуальный счет в базе для {user_id}: {current_score}")
                print("=========================================================\n")
                return current_score
            else:
                print("⚠️ [БД ПРЕДУПРЕЖДЕНИЕ] Запись не найдена сразу после INSERT!")
                print("=========================================================\n")
                return 0

    except Exception as e:
        print(f"❌ [БД ОШИБКА ВЫПОЛНЕНИЯ]: {type(e).__name__} — {e}")
        print("📜 Трассировка ошибки (Traceback):")
        traceback.print_exc()
        conn.rollback()
        print("=========================================================\n")
        return 0
    finally:
        conn.close()

def get_top_users(peer_id):
    """Получение ТОП-10 с логированием"""
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
            print(f"✅ [БД] Найдено записей в ТОПе: {len(rows)}")
            return rows
    except Exception as e:
        print(f"❌ [БД ОШИБКА ТОПа]: {e}")
        traceback.print_exc()
        return []
    finally:
        conn.close()


# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (VK И ИИ) ---

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
            print(f"❌ [ВК] Ошибка отправки VK API: {e}")

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
            print(f"⚠️ [ВК] Ошибка получения имени: {e}")
    return "Участник"

def analyze_and_generate_response(text):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        print("⚠️ [ИИ] Ключ AI_API_KEY не задан в Render, работаем в режиме фолбэка.")
        return True, "участников чата", random.choice(FALLBACK_COMMENTS)

    system_instruction = (
        "Ты — Хамулька, дерзкая девчонка-гопница из дворового чата. "
        "Твоя задача — строго фиксировать ЛЮБЫЕ наезды, мат, обозвания и подколы.\n"
        "Слова типа 'дура', 'тупой', 'клоун', 'хам', 'гнида' — это 100% ТОКСИК!\n\n"
        "ОТВЕЧАЙ СТРОГО В ФОРМАТЕ:\n"
        "ТОКСИК | [Имя кого задели или 'участников чата'] | [Короткий дерзкий ответ от Хамульки]\n\n"
        "Если фраза абсолютно добрая ('привет', 'спасибо'), отвечай: НЕ_ТОКСИК"
    )

    user_prompt = f"Проанализируй фразу из чата: '{text}'"
    headers = {"Authorization": f"Bearer {clean_key}", "Content-Type": "application/json"}

    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": 0.5
        }
        try:
            print(f"🤖 [ИИ] Запрос к модели {model_name}...")
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=6)
            if response.status_code == 200:
                result = response.json()
                if 'choices' in result and len(result['choices']) > 0:
                    full_content = result['choices'][0]['message']['content'].strip()
                    full_content = re.sub(r'<think>.*?</think>', '', full_content, flags=re.DOTALL).strip()
                    
                    print(f"🤖 [ИИ Ответ ({model_name})]: {full_content}")

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
                print(f"⚠️ [ИИ] Модель {model_name} вернула HTTP status {response.status_code}: {response.text}")
        except Exception as e:
            print(f"⚠️ [ИИ] Ошибка обращения к модели {model_name}: {e}")
            continue

    print("⚠️ [ИИ] Все модели ИИ не ответили, переходим на фолбэк-комментарий")
    return True, "участников чата", random.choice(FALLBACK_COMMENTS)


# --- 4. ОБРАБОТКА СООБЩЕНИЙ ---

def process_message_async(text, peer_id, from_id):
    print(f"\n📩 [НОВОЕ СООБЩЕНИЕ] peer_id={peer_id}, from_id={from_id}, текст: '{text}'")

    # Команда ТОПа / рейтинга
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

    # Проверка ключевых слов и матов
    has_name = bool(NAMES_PATTERN.search(text))
    has_bad_word = bool(BAD_WORDS_PATTERN.search(text))

    if has_name or has_bad_word:
        print(f"🔍 [Анализ] Найдено совпадение (имя={has_name}, плохие слова={has_bad_word})")
        
        is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
        
        # Если есть явный мат/оскорбление — считаем токсиком гарантированно
        if has_bad_word:
            print("🔥 [Анализ] Обнаружено явное оскорбление! Принудительно устанавливаем is_toxic = True")
            is_toxic = True
            if not ai_comment:
                ai_comment = random.choice(FALLBACK_COMMENTS)

        print(f"📊 [Итог анализа] is_toxic={is_toxic}, target_name='{target_name}'")

        if is_toxic:
            user_count = add_toxicity_point(peer_id, from_id)
            user_name = get_user_name(from_id)
            
            reply = (
                f"{ai_comment}\n\n"
                f"🛡️ Фиксация: [id{from_id}|{user_name}], зафиксирован наезд на {target_name}!\n"
                f"📈 Ваша статистика в банке токсичности: {user_count}"
            )
            send_message(peer_id, reply)


# --- 5. FLASK СЕРВЕР И WEBHOOK ---

@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json(force=True, silent=True)
    if not data:
        return 'ok'

    event_type = data.get('type')

    if event_type == 'confirmation':
        print("✅ [ВК Webhook] Подтверждение адреса (confirmation)")
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
