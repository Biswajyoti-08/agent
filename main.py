import os
import requests
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Initialize API Clients
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")

# 2. Connect to MongoDB Atlas
mongo_uri = os.environ.get("MONGO_URI")
mongo_client = MongoClient(mongo_uri)
db = mongo_client["WhatsAppAgent"]
chat_history = db["ChatHistory"]

def send_whatsapp_message(phone_number: str, text: str):
    """Sends a message back to the user via Kapso's API."""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {
        "X-API-Key": kapso_api_key,
        "Content-Type": "application/json"
    }
    payload = {
        "message": {
            "phone_number": phone_number,
            "content": text,
            "message_type": "text"
        }
    }
    requests.post(url, headers=headers, json=payload)

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    event_type = request.headers.get("X-Webhook-Event")

    # Kapso sends many events (delivered, read, etc). We only want new incoming messages!
    if event_type == "whatsapp.message.received":
        message_data = payload.get("message", {})
        sender_phone = message_data.get("phone_number")
        user_text = message_data.get("content", "")

        # Ignore if it is an image or empty message for now
        if not sender_phone or not user_text:
            return {"status": "ignored"}

        # 3. Memory: Fetch the last 5 messages from MongoDB
        history_cursor = chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5)
        history_docs = list(history_cursor)
        history_docs.reverse() # Put in chronological order

        # 4. Build the prompt with history
        messages = [
            {"role": "system", "content": "You are a helpful AI WhatsApp agent. Keep answers brief and friendly. You remember context from previous messages."}
        ]
        
        for doc in history_docs:
            messages.append({"role": "user", "content": doc["user_msg"]})
            messages.append({"role": "assistant", "content": doc["ai_reply"]})

        # Add the brand new message
        messages.append({"role": "user", "content": user_text})

        # 5. Generate AI Reply
        try:
            chat_completion = groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.1-8b-instant",
            )
            ai_reply = chat_completion.choices[0].message.content
        except Exception as e:
            print(f"Groq Error: {e}")
            ai_reply = "Oops! My AI brain hit a slight glitch."

        # 6. Memory: Save this exact conversation to the database
        chat_history.insert_one({
            "phone_number": sender_phone,
            "user_msg": user_text,
            "ai_reply": ai_reply
        })

        # 7. Send the reply back to the user
        send_whatsapp_message(sender_phone, ai_reply)

    # Always return 200 OK so Kapso knows we received the webhook
    return {"status": "success"}

@app.get("/")
def read_root():
    return {"status": "Production Live Agent Webhook is Online!"}
