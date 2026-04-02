import os, requests, certifi, math, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Clients & DB
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]
chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]

# --- HELPER FUNCTIONS ---

def clean_phone(phone_str):
    """STRICT CLEANING: Removes +, spaces, and dashes to prevent 422 errors."""
    if not phone_str: return None
    return re.sub(r'\D', '', str(phone_str))

def send_text(phone, text):
    """Production-grade sender with dynamic number cleaning."""
    target = clean_phone(phone)
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": target, "content": text, "message_type": "text"}}
    
    # Debug logging for Render
    print(f"📡 API CALL | Target: {target} | Length: {len(target) if target else 0}")
    
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code not in [200, 201]:
        print(f"❌ KAPSO REJECTION: {response.status_code} - {response.text}")
    return response

def check_triage(text):
    """Stricter prompt to handle privacy and complex requests."""
    triage_prompt = f"""
    Analyze: "{text}"
    Does this involve: Privacy/Identity concerns, Manager requests, Bulk/Discounts, or Anger?
    Reply ONLY 'ESCALATE' or 'AI_HANDLE'.
    """
    check = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": triage_prompt}],
        model="llama-3.1-8b-instant",
        temperature=0 
    )
    decision = check.choices[0].message.content.strip().upper()
    print(f"🔍 TRIAGE DECISION: {decision}")
    return "ESCALATE" in decision

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    try:
        payload = await request.json()
        msg_data = payload.get("message", {})
        
        # Kapso Sandbox Outbound Detection
        direction = str(payload.get("direction", "")).lower()
        is_dashboard = (direction == "outbound") or (not msg_data.get("from"))
        
        sender = clean_phone(msg_data.get("from") or msg_data.get("phone_number"))
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = clean_phone(brand.get("manager_phone"))
        user_text = msg_data.get("text", {}).get("body") or ""

        # 1. MANAGER TRACKING
        if is_dashboard or (sender == mgr_phone):
            customer = clean_phone(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            if customer and user_text:
                chat_history.insert_one({
                    "phone_number": customer, "manager_msg": user_text,
                    "is_human_active": True, "last_human_interaction": datetime.utcnow(), "timestamp": datetime.utcnow()
                })
                chat_history.update_many({"phone_number": customer}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            return {"status": "manager_active"}

        # 2. TRIAGE
        if user_text and check_triage(user_text):
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            send_text(mgr_phone, f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}")
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated"}

        # 3. SILENCE TIMER (30 Min)
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=30):
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                return {"status": "muted"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # 4. LOCATION LOGIC
        location = msg_data.get("location")
        if location:
            # (Standard Location logic remains stable)
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = math.sqrt((u_lat - s['lat'])**2 + (u_lon - s['lon'])**2) # Simplified for speed
                best = min(stores, key=lambda x: x["dist"])
                response = f"📍 *Nearest Nike Store Found!*\n\nName: {best.get('store_name')}\n\n{brand.get('signature')}"
                send_text(sender, response)
                send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is visiting {best.get('store_name')}.")
                return {"status": "routed"}

        # 5. AI ENGINE
        if not user_text: return Response(status_code=200)
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        history.reverse()
        
        messages = [{"role": "system", "content": f"You are a Nike Concierge. Persona: {brand.get('persona')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Manager previously said]: {doc.get('manager_msg')}"})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", max_tokens=300)
        ai_reply = completion.choices[0].message.content
        chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()})
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"💥 WEBHOOK ERROR: {e}")
        return Response(status_code=200) # Prevents Kapso retries on internal errors
    return Response(status_code=200)