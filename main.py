import os
import re
import requests
from flask import Flask, request
import vk_api

from phrases import NAMES_PATTERN

app = Flask(__name__)

VK_TOKEN = os.environ.get('VK_TOKEN', '')
CONFIRMATION_CODE = os.environ.get('CONFIRMATION_CODE', '')
AI_API_KEY = os.environ.get('AI_API_KEY', '')

AI_URL = 'https://api.groq.com/openai/v1/chat/completions'
AI_MODEL = 'llama-3.3-70b-versatile'

vk = vk_api.VkApi(token=VK_TOKEN) if VK_TOKEN else None
stats = {}

def send_message(peer_id, text):
    if vk:
        try:
            vk.method('messages.send', {
                'peer_id': peer_id,
                'message': text,
                'random_id': 0
            })
            print(f"ОТПРАВЛЕНО В ВК [{peer_id}]: {text}")
        except Exception as e:
            print("ОШИБКА ВК API:", e)

def analyze_and_generate_response(text):
    prompt = (
        f"Проанализируй сообщение из чата: '{text}'.\n\n"
        "Твоя задача:\n"
        "1. Определи, адресован ли тут подкол, хамство, сарказм, ирония или наезд на девушку по имени Вика, Ксюша или Рита (или их формы/опечатки).\n"
        "2. Если наезда/хамства НЕТ или имя не относится к этой троице, ответь строго одним словом: НОРМА.\n"
        "3. Если наезд/хамство ЕСТЬ, ответь строго в таком формате без лишних слов:\n"
        "ТОКСИК | [Имя девушки в винительном падеже (Вику / Ксюшу / Риту)] | [Короткий, смешной и ироничный комментарий бота]\n\n"
        "Пример ответа при хамстве на Вику:\n"
        "ТОКСИК | Вику | 🚨 Обнаружена попытка задеть Вику! Наш виртуальный щит уже активирован."
    )
    
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7
    }
    
    try:
        response = requests.post(AI_URL, json=payload, headers=headers, timeout=7)
        result = response.json()
        print("GROQ RAW RESPONSE:", result)
        
        if 'choices' in result and len(result['choices']) > 0:
            answer = result['choices'][0]['message']['content'].strip()
            print("AI ANSWER:", answer)
            
            if "ТОКСИК" in answer.upper():
                parts = answer.split("|")
                if len(parts) >= 3:
                    target_name = parts[1].strip()
                    ai_comment = parts[2].strip()
                    return True, target_name, ai_comment
                return True, "девушку", "🚨 Фиксирую подкол! Счётчик токсичности пополнен."
        return False, None, None
    except Exception as e:
        print("GROQ API ERROR:", e)
        return False, None, None

# Добавили GET, чтобы Render не считал сервис мёртвым!
@app.route('/', methods=['GET', 'POST'])
def bot():
    if request.method == 'GET':
        return 'Bot is running alive!', 200

    data = request.get_json()
    print("ВХОДЯЩИЕ ДАННЫЕ ОТ ВК:", data)

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
        
        if not peer_id:
            return 'ok'

        if peer_id not in stats:
            stats[peer_id] = {}

        if '!топ' in text.lower() or '!рейтинг' in text.lower():
            if not stats[peer_id]:
                send_message(peer_id, "📊 Пока никто не подкалывал девчонок!")
            else:
                sorted_users = sorted(stats[peer_id].items(), key=lambda x: x[1], reverse=True)
                top_text = "🏆 **ТОП самых острых на язык в беседе:**\n\n"
                for i, (u_id, count) in enumerate(sorted_users[:10], 1):
                    top_text += f"{i}. [id{u_id}|Участник] — {count} замеченных наездов\n"
                send_message(peer_id, top_text)
            return 'ok'

        if NAMES_PATTERN.search(text):
            print(f"Имя распознано в сообщении: '{text}'")
            is_toxic, target_name, ai_comment = analyze_and_generate_response(text)
            
            if is_toxic:
                stats[peer_id][from_id] = stats[peer_id].get(from_id, 0) + 1
                user_count = stats[peer_id][from_id]
                
                reply = (
                    f"{ai_comment}\n\n"
                    f"🛡️ **Защита:** [id{from_id}|Участник], зафиксирован наезд на **{target_name}**!\n"
                    f"📈 Ваша статистика в банке токсичности: **{user_count}**"
                )
                send_message(peer_id, reply)
            else:
                print("ИИ счёл сообщение безопасным.")
            
        return 'ok'

    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
