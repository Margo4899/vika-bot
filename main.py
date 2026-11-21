import os
import re
import json
import random
import requests
import threading
import time
import traceback
import psycopg2
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN
from toxic_words import check_toxicity

app = Flask(__name__)

# чтобы логи сразу появлялись в Render
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# --- 1. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')          # Groq (запасная модель)
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')  # Google Gemini (основная модель)
GEMINI_MODELS = ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.8-flash']
VK_API_VERSION = '5.199'

# Награды
TOP1_AWARD = 'top1_medal'      # медаль хамства лидеру топа (вручается один раз на беседу)
LEGEND_AWARD = 'legend_500'    # секретный приз за 500 баллов
LEGEND_SCORE = 500
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS toxicity_awards (
                    peer_id BIGINT NOT NULL,
                    award TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    PRIMARY KEY (peer_id, award)
                );
            """)
            conn.commit()
            print("✅ [БД-ИНИЦИАЛИЗА] Таблицы toxicity_stats и toxicity_awards проверены/созданы.")
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


def get_awards(peer_id):
    """{award: user_id} для беседы."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT award, user_id FROM toxicity_awards WHERE peer_id = %s;", (peer_id,))
            return {a: u for a, u in cur.fetchall()}
    except Exception as e:
        print(f"❌ [БД НАГРАДЫ]: {e}")
        return {}
    finally:
        conn.close()

def claim_award(peer_id, award, user_id):
    """Записывает награду. True, если записали сейчас (раньше ее в этой беседе не было)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO toxicity_awards (peer_id, award, user_id) VALUES (%s, %s, %s)
                ON CONFLICT (peer_id, award) DO NOTHING;
            """, (peer_id, award, user_id))
            conn.commit()
            return cur.rowcount == 1
    except Exception as e:
        print(f"❌ [БД НАГРАДЫ]: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

def get_all_peers():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT peer_id FROM toxicity_stats;")
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        print(f"❌ [БД]: {e}")
        return []
    finally:
        conn.close()


# --- 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (VK И ИИ) ---

def send_message(peer_id, text, attachment=None):
    if vk:
        try:
            params = {
                'peer_id': peer_id,
                'message': text,
                'random_id': random.randint(1, 2**31 - 1)
            }
            if attachment:
                params['attachment'] = attachment
            vk.method('messages.send', params)
            print(f"✉️ [ВК] Сообщение успешно отправлено в peer_id={peer_id}")
        except Exception as e:
            print(f"❌ [ВК] Ошибка отправки VK API: {e}")

screen_names_cache = {}

def get_user_name(user_id):
    """Имя человека (Имя Фамилия) или название сообщества, если это бот."""
    if user_id in user_names_cache:
        return user_names_cache[user_id]
    name = None
    if vk:
        try:
            if user_id < 0:
                res = vk.method('groups.getById', {'group_ids': -user_id})
                groups = res.get('groups', []) if isinstance(res, dict) else res
                if groups:
                    name = groups[0].get('name')
            else:
                res = vk.method('users.get', {'user_ids': user_id, 'fields': 'screen_name'})
                if res:
                    name = f"{res[0].get('first_name', '')} {res[0].get('last_name', '')}".strip()
                    screen_names_cache[user_id] = res[0].get('screen_name') or f"id{user_id}"
        except Exception as e:
            print(f"⚠️ [ВК] Ошибка получения имени {user_id}: {e}")
    name = name or ("Бот" if user_id < 0 else "Участник")
    user_names_cache[user_id] = name
    return name

def mention(user_id, text=None):
    """Синяя ссылка на человека. У ботов ссылки не работают, поэтому просто имя."""
    text = text or get_user_name(user_id)
    if user_id < 0:
        return text
    return f"[id{user_id}|{text}]"

def at_mention(user_id):
    """Отметка через @ник, чтобы человек увидел уведомление. У ботов просто название."""
    if user_id < 0:
        return get_user_name(user_id)
    get_user_name(user_id)
    return f"[id{user_id}|@{screen_names_cache.get(user_id, f'id{user_id}')}]"

def upload_photo(peer_id, path):
    """Загружает картинку для отправки в беседу. Возвращает строку attachment или None."""
    if not vk or not os.path.exists(path):
        return None
    try:
        server = vk.method('photos.getMessagesUploadServer', {'peer_id': peer_id})
        with open(path, 'rb') as f:
            up = requests.post(server['upload_url'], files={'photo': f}, timeout=60).json()
        saved = vk.method('photos.saveMessagesPhoto', {'photo': up['photo'], 'server': up['server'], 'hash': up['hash']})[0]
        return f"photo{saved['owner_id']}_{saved['id']}"
    except Exception as e:
        print(f"⚠️ [ВК] Не удалось загрузить картинку {path}: {e}")
        return None

SYSTEM_INSTRUCTION = (
    "Ты — Хамулька, дерзкая девчонка-гопница из дворового чата аватарии. "
    "Ты следишь за наездами в беседе: мат, обзывательства, грубые подколы, посылания.\n"
    "Дружеские шутки без грубости, приветствия, спокойные споры и просто упоминание имени — это НЕ наезд.\n\n"
    "Если это наезд, ответь строго в формате:\n"
    "ТОКСИК | [имя того, на кого наехали, или 'участников чата'] | [твой короткий дерзкий смешной комментарий]\n"
    "Комментарий: одна-две фразы, каждый раз по-новому, с юмором, по-дворовому, без мата, "
    "можно сленг и эмодзи в начале. Не повторяй шаблоны вроде 'фиксирую наезд'.\n"
    "Если это не наезд, ответь одним словом: НЕ_ТОКСИК"
)

def parse_ai(full_content):
    full_content = re.sub(r'<think>.*?</think>', '', full_content or '', flags=re.DOTALL).strip()
    if not full_content:
        return None
    if "НЕ_ТОКСИК" in full_content or "НЕ ТОКСИК" in full_content:
        return False, "участников чата", ""
    parts = [p.strip() for p in full_content.split("|")]
    if len(parts) >= 3 and parts[2]:
        return True, parts[1] or "участников чата", parts[2]
    if len(parts) == 2 and parts[1]:
        return True, "участников чата", parts[1]
    return None

def ask_gemini(text):
    for model_name in GEMINI_MODELS:
        try:
            print(f"🤖 [GEMINI] Запрос к модели {model_name}...")
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={
                    "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
                    "contents": [{"role": "user", "parts": [{"text": f"Фраза из чата: '{text}'"}]}],
                    "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1024},
                },
                timeout=25,
            )
            data = r.json()
            if r.status_code != 200:
                print(f"⚠️ [GEMINI] {model_name}: HTTP {r.status_code}: {data.get('error', {}).get('message')}")
                continue
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            print(f"🤖 [GEMINI Ответ ({model_name})]: {content}")
            parsed = parse_ai(content)
            if parsed:
                return parsed
        except Exception as e:
            print(f"⚠️ [GEMINI] Ошибка {model_name}: {e}")
    return None

def ask_groq(text):
    clean_key = AI_API_KEY.strip() if AI_API_KEY else ""
    if not clean_key:
        return None
    headers = {"Authorization": f"Bearer {clean_key}", "Content-Type": "application/json"}
    for model_name in AI_MODELS:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": f"Фраза из чата: '{text}'"}
            ],
            "temperature": 0.8,
            "max_tokens": 1024,
        }
        if model_name.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        try:
            print(f"🤖 [GROQ] Запрос к модели {model_name}...")
            response = requests.post(AI_URL, json=payload, headers=headers, timeout=25)
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                print(f"🤖 [GROQ Ответ ({model_name})]: {content}")
                parsed = parse_ai(content)
                if parsed:
                    return parsed
            else:
                print(f"⚠️ [GROQ] {model_name}: HTTP {response.status_code}")
        except Exception as e:
            print(f"⚠️ [GROQ] Ошибка {model_name}: {e}")
    return None

def analyze_and_generate_response(text):
    """(токсик?, на кого, комментарий). Сначала Gemini, потом Groq, потом заготовки."""
    result = (ask_gemini(text) if GEMINI_API_KEY else None) or ask_groq(text)
    if result:
        return result
    print("⚠️ [ИИ] Нейросети не ответили, переходим на заготовку")
    return None

# --- 4. ОБРАБОТКА СООБЩЕНИЙ ---

TOP_ANNOUNCE = ("🎁 Кто первым в беседе наберет 500 баллов токсичности, того ждет СЕКРЕТНЫЙ приз от Хамульки, "
                "еще грандиознее медали хамства!\n\n")

def format_top(peer_id, top_users):
    awards = get_awards(peer_id)
    medal_holder = awards.get(TOP1_AWARD)
    legend_holder = awards.get(LEGEND_AWARD)
    announce = TOP_ANNOUNCE if not legend_holder else "💎 Легенда хамства в этой беседе уже есть, но топ живет дальше: кто сместит лидера?\n\n"
    text = announce + "🏆 ТОП самых острых на язык в беседе:\n\n"
    for i, (u_id, count) in enumerate(top_users, 1):
        badges = ("💎 " if u_id == legend_holder else "") + ("👑 " if u_id == medal_holder else "")
        text += f"{i}. {badges}{mention(u_id)} — {count} замеченных наездов\n"
    if medal_holder:
        text += "\n👑 — обладатель медали хамства"
    if legend_holder:
        text += "\n💎 — легенда хамства"
    return text

def award_top1(peer_id):
    """Вручает медаль хамства лидеру топа беседы (один раз на беседу)."""
    top = get_top_users(peer_id)
    if not top:
        return
    leader, score = top[0]
    if not claim_award(peer_id, TOP1_AWARD, leader):
        return
    print(f"🏅 [НАГРАДА] Медаль хамства в беседе {peer_id}: {leader} ({score} баллов)")
    photo = upload_photo(peer_id, os.path.join(ASSETS, "medal_top1.png"))
    send_message(peer_id,
        f"🏅 ВНИМАНИЕ, БЕСЕДА! Хамулька вручает МЕДАЛЬ ХАМСТВА!\n\n"
        f"{at_mention(leader)}, это тебе! {score} замеченных наездов, первое место в топе, "
        f"ни у кого язык не острее. Носи с гордостью 🌶️\n\n"
        f"👑 Встречайте обладателя медали хамства! Теперь в топе рядом с этим именем сияет корона.\n\n"
        f"🎁 А дальше гонка за СЕКРЕТНЫМ призом: кто первым наберет {LEGEND_SCORE} баллов, "
        f"получит награду еще грандиознее. Пишите !топ, чтобы следить за гонкой.",
        attachment=photo)

def check_legend(peer_id, user_id, score):
    """Секретный приз первому, кто наберет 500 баллов в беседе."""
    if score < LEGEND_SCORE or not claim_award(peer_id, LEGEND_AWARD, user_id):
        return
    print(f"💎 [НАГРАДА] Легенда хамства в беседе {peer_id}: {user_id} ({score})")
    photo = upload_photo(peer_id, os.path.join(ASSETS, "legend_500.png"))
    send_message(peer_id,
        f"💎💎💎 СЕКРЕТНЫЙ ПРИЗ РАСКРЫТ! 💎💎💎\n\n"
        f"{at_mention(user_id)} первым в беседе набрал(а) {score} баллов и становится ЛЕГЕНДОЙ ХАМСТВА!\n\n"
        f"👑 Отныне в топе рядом с именем сияет 💎, и это навсегда.\n"
        f"🌶️ Хамулька снимает кепку и официально признает: острее языка в этом дворе нет.",
        attachment=photo)

_own_group_id = {"id": None}

def own_group_id():
    """id сообщества самой Хамульки, чтобы не считать ее собственные сообщения."""
    if _own_group_id["id"] is None and vk:
        try:
            res = vk.method('groups.getById', {})
            groups = res.get('groups', []) if isinstance(res, dict) else res
            _own_group_id["id"] = groups[0]['id'] if groups else 0
        except Exception as e:
            print(f"⚠️ [ВК] Не удалось узнать свой id: {e}")
            _own_group_id["id"] = 0
    return _own_group_id["id"]

def process_message_async(text, peer_id, from_id):
    print(f"\n📩 [НОВОЕ СООБЩЕНИЕ] peer_id={peer_id}, from_id={from_id}, текст: '{text}'")
    if from_id < 0 and -from_id == own_group_id():
        return

    # Команда ТОПа / рейтинга
    if '!топ' in text.lower() or '!рейтинг' in text.lower():
        top_users = get_top_users(peer_id)
        if not top_users:
            send_message(peer_id, TOP_ANNOUNCE + "📊 Пока никто не токсичил в беседе!")
        else:
            send_message(peer_id, format_top(peer_id, top_users))
        return

    has_name = bool(NAMES_PATTERN.search(text))
    level = check_toxicity(text)
    if not has_name and not level:
        return
    print(f"🔍 [Анализ] имя={has_name}, грубость={level or 'нет'}")

    result = analyze_and_generate_response(text)
    if result:
        is_toxic, target_name, ai_comment = result
    else:
        is_toxic, target_name, ai_comment = (level == "strong"), "участников чата", random.choice(FALLBACK_COMMENTS)

    # мат и явные оскорбления засчитываются всегда, даже если нейросеть решила иначе
    if level == "strong" and not is_toxic:
        print("🔥 [Анализ] Явная грубость: засчитываем, хотя нейросеть сказала 'не токсик'")
        is_toxic = True
        ai_comment = ai_comment or random.choice(FALLBACK_COMMENTS)

    print(f"📊 [Итог анализа] is_toxic={is_toxic}, target_name='{target_name}'")
    if not is_toxic:
        return

    user_count = add_toxicity_point(peer_id, from_id)
    reply = (
        f"{ai_comment}\n\n"
        f"🛡️ Фиксация: {mention(from_id)}, зафиксирован наезд на {target_name}!\n"
        f"📈 Ваша статистика в банке токсичности: {user_count}"
    )
    send_message(peer_id, reply)
    check_legend(peer_id, from_id, user_count)


def award_current_leaders():
    time.sleep(15)
    for pid in get_all_peers():
        try:
            award_top1(pid)
        except Exception as e:
            print(f"❌ [НАГРАДА] Ошибка вручения в {pid}: {e}")

threading.Thread(target=award_current_leaders, daemon=True).start()


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
        
        msg_id = (message.get('peer_id'), message.get('conversation_message_id'), message.get('id'))
        text = message.get('text', '')
        peer_id = message.get('peer_id')
        from_id = message.get('from_id')
        
        if msg_id[1] or msg_id[2]:
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
