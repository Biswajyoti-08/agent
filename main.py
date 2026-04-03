import os, requests, certifi, math, re
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# 1. SCALABLE INFRASTRUCTURE SETUP
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] 

# --- HELPER: GLOBAL SENDER ---
def send_text(phone, text):
    clean_target = re.sub(r'\D', '', str(phone))
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": clean_target, "content": text, "message_type": "text"}}
    return requests.post(url, headers=headers, json=payload)

# --- HELPER: STRICT BINARY TRIAGE ---
def check_triage(text):
    """STRICT GATEKEEPER: Binary output only to prevent hallucinations."""
    triage_prompt = f"""
    OBJECTIVE: You are a Gatekeeper. Classify this message: "{text}"
    
    Reply 'ESCALATE' if the user:
    - Wants a "person", "human", "manager", "boss", or "live chat".
    - Mentions "scam", "how did you get my number", "fake", or "crazy".
    - Wants a "call", "transfer", or "connection".
    - Is angry or says "stop".
    - Mentions "discount", "price", or "negotiate".
    
    Otherwise, reply 'AI_HANDLE'.
    OUTPUT ONLY THE WORD: 'ESCALATE' OR 'AI_HANDLE'.
    """
    try:
        check = groq_client.chat.completions.create(
            messages=[
                {"role": "system", "content": "You are a binary classifier. Output only ESCALATE or AI_HANDLE."},
                {"role": "user", "content": triage_prompt}
            ], 
            model="llama-3.1-70b-versatile", 
            temperature=0,
            max_tokens=5
        )
        result = check.choices[0].message.content.strip().upper()
        print(f"🔍 TRIAGE DECISION: {result}")
        return "ESCALATE" in result
    except: return False

# --- CORE WEBHOOK ENGINE ---
@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    if msg_id and processed_msg_ids.find_one({"msg_id": msg_id}): return Response(status_code=200)
    if msg_id: processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        sender = re.sub(r'\D', '', msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        mgr_phone = re.sub(r'\D', '', brand.get("manager_phone"))
        brand_phone = re.sub(r'\D', '', brand.get('brand_phone'))
        
        is_dashboard = (str(payload.get("direction")).lower() == "outbound") or (sender == brand_phone)

        # 1. MANAGER INTERVENTION (Mute & Log)
        if is_dashboard or (sender == mgr_phone):
            customer = re.sub(r'\D', '', msg_data.get("to") or msg_data.get("recipient_id") or sender)
            chat_history.update_many({"phone_number": customer}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            if user_text:
                chat_history.insert_one({"phone_number": customer, "manager_msg": user_text, "timestamp": datetime.utcnow()})
            return {"status": "manager_logged"}

        # 2. TRIAGE GATE (The "Hard-Stop")
        if user_text and check_triage(user_text):
            chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}})
            send_text(mgr_phone, f"🚨 *MANAGER ALERT*: {sender} said: {user_text}")
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            # CRITICAL: Return here to stop the AI from generating a fake 'transfer' response.
            return {"status": "escalated"}

        # 3. 5-MINUTE SILENCE TIMER (Sliding Window)
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_hit = user_state.get("last_human_interaction")
            if last_hit and (datetime.utcnow() - last_hit) < timedelta(minutes=5):
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_human_interaction": datetime.utcnow()}})
                if user_text:
                    chat_history.insert_one({"phone_number": sender, "user_msg": user_text, "timestamp": datetime.utcnow()})
                return {"status": "muted_by_human"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # 4. LOCATION LEAD GENERATION
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = math.sqrt((u_lat - s['lat'])**2 + (u_lon - s['lon'])**2)
                best = min(stores, key=lambda x: x["dist"])
                chat_history.insert_one({"phone_number": sender, "user_msg": "[Location Pin]", "ai_reply": f"SYSTEM: Found {best.get('store_name')}", "timestamp": datetime.utcnow()})
                send_text(sender, f"📍 *Nearest Nike Hub Found!*\n\nName: {best.get('store_name')}\n\n{brand.get('signature')}")
                send_text(mgr_phone, f"🚨 *NEW HUB VISIT*: {sender} is visiting {best.get('store_name')}.")
                return {"status": "routed"}

        # 5. AI ENGINE (Clean Response)
        if not user_text: return Response(status_code=200)
        
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        history.reverse()
        
        system_prompt = f"""
        You are a Nike Concierge. Persona: {brand.get('persona')}
        CONSTRAINTS:
        - You are an AI assistant. NEVER pretend to be a manager or human.
        - You CANNOT transfer to live chat or calls.
        - NEVER say "*Your chat has been connected*". 
        - If a user asks for a person, say nothing or acknowledge you are AI.
        """
        
        messages = [{"role": "system", "content": system_prompt}]
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
        print(f"💥 PROD ERROR: {e}")
        return Response(status_code=200)
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Nike Retail OS v3.2 - Hard-Stop Patch Active"}