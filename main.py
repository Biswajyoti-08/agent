import os
import requests
from fastapi import FastAPI, Request
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# Initialize API Clients
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")

# Connect to MongoDB Atlas
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
    # DEBUG: Print Kapso's response to see if they reject our outgoing message!
    response = requests.post(url, headers=headers, json=payload)
    print(f"Kapso Send API Response: {response.status_code} - {response.text}")

@app.post("/webhook")
async def kapso_webhook(request: Request):
    payload = await request.json()
    event_type = request.headers.get("X-Webhook-Event")

    # DEBUG: Print exactly what Kapso sends to our server
    print(f"--- NEW WEBHOOK RECEIVED ---")
    print(f"Event Type: {event_type}")
    print(f"Raw Payload: {payload}")

    # Loosened the filter to ensure we catch the message
    if event_type == "whatsapp.message.received" or "message" in payload:
        
        # Safely extract phone and text depending on Kapso's payload structure
        if "message" in payload and isinstance(payload["message"], dict):
            sender_phone = payload["message"].get("phone_number")
            user_text = payload["message"].get("content", "")
        else:
            sender_phone = payload.get("phone_number")
            user_text = payload.get("content", "")

        if not sender_phone or not user_text:
            print("Ignored: Missing phone number or text content.")
            return {"status": "ignored"}

        print(f"Processing message from {sender_phone}: {user_text}")

        try:
            # Memory: Fetch the last 5 messages from MongoDB
            history_cursor = chat_history.find({"phone_number": sender_phone}).sort("_id", -1).limit(5)
            history_docs = list(history_cursor)
            history_docs.reverse()

            messages = [
                {"role": "system", "content": "You are a helpful AI WhatsApp agent. Keep answers brief and friendly. You remember context from previous messages."}
            ]
            
            for doc in history_docs:
                messages.append({"role": "user", "content": doc["user_msg"]})
                messages.append({"role": "assistant", "content": doc["ai_reply"]})

            messages.append({"role": "user", "content": user_text})

            # Generate AI Reply
            chat_completion = groq_client.chat.completions.create(
                messages=messages,
                model="llama-3.1-8b-instant",
            )
            ai_reply = chat_completion.choices[0].message.content
            print(f"AI Reply Generated: {ai_reply}")

            # Memory: Save to database
            chat_history.insert_one({
                "phone_number": sender_phone,
                "user_msg": user_text,
                "ai_reply": ai_reply
            })

            # Send back to WhatsApp
            send_whatsapp_message(sender_phone, ai_reply)
            
        except Exception as e:
            print(f"System Error during generation or DB save: {e}")

    return {"status": "success"}

@app.get("/")
def read_root():
    return {"status": "Production Live Agent Webhook is Online!"}
