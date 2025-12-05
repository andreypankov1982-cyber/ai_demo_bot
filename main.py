import os
from flask import Flask, request
import requests

TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}/"

app = Flask(__name__)

def send_message(chat_id, text):
    requests.post(TELEGRAM_API + "sendMessage", json={
        "chat_id": chat_id,
        "text": text
    })

@app.route("/", methods=["POST", "GET"])
def webhook():
    if request.method == "POST":
        data = request.get_json()
        if "message" in data:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"].get("text", "")

            # Ответ бота
            send_message(chat_id, f"Я работаю на Railway! Вы написали: {text}")

        return {"ok": True}

    return "Bot is running!"

if __name__ == "__main__":
    app.run(port=3000)
