import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from groq import Groq

# 🔐 Получение ключей из переменных окружения
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Инициализация клиента Groq
client = Groq(api_key=GROQ_API_KEY)
logging.basicConfig(level=logging.INFO)

# Простая история сообщений на 3 последних
user_histories = {}

# /start команда
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("👨‍⚖ Связаться с юристом", url="https://t.me/sovetnik_moscow")]]
    await update.message.reply_text(
        "Здравствуйте! Я виртуальный помощник-юрист. Задайте ваш вопрос:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Обработка текстовых сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    user_input = update.message.text

    # Сохраняем историю пользователя
    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "content": user_input})
    user_histories[user_id] = user_histories[user_id][-3:]

    # Системная инструкция (инструкция для ИИ)
    system_prompt = {
        "role": "system",
        "content": "Ты профессиональный юрист. Отвечай ясно, юридически грамотно, на русском языке, как будто ты адвокат-консультант. Не пиши 'как ИИ', говори от первого лица. Будь вежлив и деловит."
    }

    messages = [system_prompt] + user_histories[user_id]

    try:
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=messages
        )
        reply = response.choices[0].message.content
        await update.message.reply_text(reply)
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

# Основной запуск
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == '__main__':
    main()