import os, requests, certifi, math, re, sys
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# Clients
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

# Collections
chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] 

def send_text(phone, text):
    clean_target = re.sub(r'\D', '', str(phone))
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": clean_target, "content": text, "message_type": "text"}}
    return requests.post(url, headers=headers, json=payload)

def check_triage(text):
    print(f"--- STARTING TRIAGE FOR: {text} ---", flush=True)
    triage_prompt = f"Analyze: '{text}'. Reply 'ESCALATE' if user wants a manager, human, discount, is angry, or suspicious. Otherwise reply 'AI_HANDLE'. Reply ONLY the word."
    try:
        check = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": triage_prompt}], 
            model="llama-3.1-70b-versatile", 
            temperature=0,
            max_tokens=10
        )
        decision = check.choices[0].message.content.strip().upper()
        print(f"🔍 DECISION MADE: {decision}", flush=True)
        return "ESCALATE" in decision
    except Exception as e:
        print(f"❌ TRIAGE ERROR: {e}", flush=True)
        return False

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    # 0. IDEMPOTENCY
    if msg_id and processed_msg_ids.find_one({"msg_id": msg_id}): return Response(status_code=200)
    if msg_id: processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        # Load Brand Config
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        if not brand:
            print("❌ DATABASE ERROR: No brand found with ID NIKE_IND", flush=True)
            return Response(status_code=200)

        sender = re.sub(r'\D', '', msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        mgr_phone = re.sub(r'\D', '', brand.get("manager_phone", ""))
        brand_phone = re.sub(r'\D', '', brand.get('brand_phone', ""))
        
        is_dashboard = (str(payload.get("direction")).lower() == "outbound") or (sender == brand_phone)
        
        print(f"📩 Incoming from: {sender} | Is Manager/Brand: {is_dashboard}", flush=True)

        # 1. MANAGER TRACKING
        if is_dashboard or (sender == mgr_phone):
            customer = re.sub(r'\D', '', msg_data.get("to") or msg_data.get("recipient_id") or sender)
            chat_history.update_many({"phone_number": customer}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            if user_text:
                chat_history.insert_one({"phone_number": customer, "manager_msg": user_text, "timestamp": datetime.utcnow()})
            print(f"🔇 Manager talking to {customer}. AI Muted.", flush=True)
            return {"status": "manager_logged"}

        # 2. TRIAGE GATE (THE HARD STOP)
        if user_text and check_triage(user_text):
            print(f"🚨 ESCALATING {sender} TO MANAGER", flush=True)
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            send_text(mgr_phone, f"🚨 *MANAGER ALERT*: {sender} needs you: {user_text}")
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated"}

        # 3. 5-MIN MUTE CHECK
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_hit = user_state.get("last_human_interaction")
            if last_hit and (datetime.utcnow() - last_hit) < timedelta(minutes=5):
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                if user_text:
                    chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "timestamp": datetime.utcnow()})
                print(f"🤐 AI is still muted for {sender}", flush=True)
                return {"status": "muted"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # 4. LOCATION (Omitted for brevity, keep your original logic here)

        # 5. AI ENGINE
        print(f"🤖 AI Handling message for {sender}", flush=True)
        # ... (Rest of your AI history and Groq logic)
        # Ensure your Groq call is inside this block
        
        completion = groq_client.chat.completions.create(
            messages=[{"role": "system", "content": f"You are Nike Concierge. Persona: {brand.get('persona')}"}, {"role": "user", "content": user_text}],
            model="llama-3.1-8b-instant"
        )
        reply = completion.choices[0].message.content
        chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "ai_reply": reply, "timestamp": datetime.utcnow()})
        send_text(sender, reply)

    except Exception as e:
        print(f"💥 CRITICAL WEBHOOK ERROR: {e}", flush=True)
    
    return Response(status_code=200)