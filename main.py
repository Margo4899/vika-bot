import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor
from vkbottle.bot import Bot, Message

# 1. Токен ВК и ссылка на БД из переменных окружения
VK_TOKEN = os.getenv("VK_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=VK_TOKEN)

# 2. Вспомогательные функции для работы с базой данных
def get_db_connection():
    """Создает подключение к базе данных Supabase."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def add_points(peer_id: int, user_id: int, points: int):
    """Добавляет или уменьшает баллы пользователя."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    query = """
    INSERT INTO toxicity_stats (peer_id, user_id, score)
    VALUES (%s, %s, %s)
    ON CONFLICT (peer_id, user_id)
    DO UPDATE SET score = toxicity_stats.score + EXCLUDED.score;
    """
    cur.execute(query, (peer_id, user_id, points))
    conn.commit()
    
    # Получаем обновленное количество баллов
    cur.execute(
        "SELECT score FROM toxicity_stats WHERE peer_id = %s AND user_id = %s;",
        (peer_id, user_id)
    )
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res["score"] if res else 0

def get_top_users(peer_id: int, limit: int = 10):
    """Возвращает топ пользователей текущей беседы."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    query = """
    SELECT user_id, score 
    FROM toxicity_stats 
    WHERE peer_id = %s 
    ORDER BY score DESC 
    LIMIT %s;
    """
    cur.execute(query, (peer_id, limit))
    rows = cur.fetchall()
    
    cur.close()
    conn.close()
    return rows

# 3. Обработчик команды "!топ"
@bot.on.message(text="!топ")
async def top_handler(message: Message):
    rows = get_top_users(message.peer_id)
    
    if not rows:
        await message.answer("Статистика в этой беседе пока пуста!")
        return
    
    text = "🏆 Топ токсичности беседы:\n\n"
    for idx, row in enumerate(rows, 1):
        user_id = row["user_id"]
        score = row["score"]
        
        # Получаем имя пользователя через VK API
        user_info = await bot.api.users.get(user_ids=user_id)
        if user_info:
            name = f"{user_info[0].first_name} {user_info[0].last_name}"
        else:
            name = f"ID {user_id}"
            
        text += f"{idx}. {name} — {score} б.\n"
        
    await message.answer(text)

# 4. Обработчик ключевых слов для начисления баллов
@bot.on.message()
async def check_toxicity(message: Message):
    if not message.text:
        return
        
    text = message.text.lower()
    
    # Пример логики подсчета (настройте слова под себя)
    plus_words = ["токсик", "клоун", "душнила", "кринж"]
    minus_words = ["спасибо", "молодец", "красава", "спас"]
    
    points_to_add = 0
    for word in plus_words:
        if word in text:
            points_to_add += 1
            
    for word in minus_words:
        if word in text:
            points_to_add -= 1
            
    if points_to_add != 0:
        new_score = add_points(message.peer_id, message.from_id, points_to_add)
        # Можно раскомментировать, если хотите отправлять сообщение при начислении:
        # await message.answer(f"Баллы обновлены! Текущий счет: {new_score}")

if __name__ == "__main__":
    bot.run_forever()
