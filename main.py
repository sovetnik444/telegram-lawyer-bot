import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from groq import Groq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

client = Groq(api_key=GROQ_API_KEY)
user_histories = {}

def start(update: Update, context: CallbackContext):
    keyboard = [[InlineKeyboardButton("👨‍⚖ Связаться с юристом", url="https://t.me/sovetnik_moscow")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(
        "Здравствуйте! Я виртуальный помощник-юрист. Задайте ваш вопрос:",
        reply_markup=reply_markup
    )

def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    user_input = update.message.text

    if user_id not in user_histories:
        user_histories[user_id] = []
    user_histories[user_id].append({"role": "user", "content": user_input})
    user_histories[user_id] = user_histories[user_id][-3:]

    system_prompt = {
        "role": "system",
        "content": "Ты профессиональный юрист. Отвечай ясно, юридически грамотно, на русском языке, не упоминая что ты ИИ."
    }

    messages = [system_prompt] + user_histories[user_id]

    try:
        response = client.chat.completions.create(
            model="llama3-8b-8192",
            messages=messages
        )
        reply = response.choices[0].message.content
        update.message.reply_text(reply)
    except Exception as e:
        update.message.reply_text("⚠️ Ошибка. Попробуйте позже.")

def main():
    updater = Updater(TELEGRAM_BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()