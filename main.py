import os, requests, certifi, math
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq

app = FastAPI()

# 1. Clients & DB Setup (Environment Variables Only)
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

# Collections
chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] # Idempotency layer

# --- HELPER FUNCTIONS ---

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_text(phone, text):
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    return requests.post(url, headers=headers, json=payload)

def check_triage(text):
    """STRICT ROUTER: Zero temperature for binary decision making."""
    triage_prompt = f"""
    Analyze this message from a Nike Athlete: "{text}"
    Does this involve: BULK ORDERS, DISCOUNTS, REFUNDS, or ANGER?
    Reply ONLY 'ESCALATE' or 'AI_HANDLE'.
    """
    check = groq_client.chat.completions.create(
        messages=[{"role": "user", "content": triage_prompt}],
        model="llama-3.1-8b-instant",
        temperature=0 # Strict enforcement
    )
    return "ESCALATE" in check.choices[0].message.content.upper()

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    # --- 0. ANTI-SPAM (Idempotency Check) ---
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    if processed_msg_ids.find_one({"msg_id": msg_id}):
        return Response(status_code=200) # Ignore duplicate retries from Kapso
    processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        sender = str(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = brand.get("manager_phone")

        # --- 1. MANAGER TRACKING ---
        if sender == mgr_phone:
            customer_phone = msg_data.get("to") or msg_data.get("recipient_id")
            if customer_phone:
                chat_history.insert_one({
                    "phone_number": str(customer_phone),
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
            return {"status": "manager_logged"}

        # --- 2. STEALTH TRIAGE (The Escalation Wall) ---
        if user_text and check_triage(user_text):
            chat_history.update_many(
                {"phone_number": sender}, 
                {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
            )
            # Alert Manager
            alert = f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}"
            send_text(mgr_phone, alert)
            
            # Send Holding Message to Athlete
            send_text(sender, "That's a great question. Let me have our Store Manager grab those details for you. One moment! 👟")
            
            # CRITICAL: Stop the AI from generating a hallucinated response!
            return {"status": "escalated_and_muted"}

        # --- 3. CHECK AI SILENCE STATUS (5-Minute Window) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=5):
                return {"status": "ai_muted_human_is_handling"}
            else:
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
        location = msg_data.get("location")
        if location:
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": "[Shared Location Pin]", 
                    "ai_reply": f"SYSTEM_NOTE: Found {best.get('store_name')}. Goal achieved.",
                    "goal_reached": True,
                    "timestamp": datetime.utcnow()
                })

                response = (
                    f"📍 *Nearest {brand.get('brand_name')} Store Found!*\n\n"
                    f"Name: {best.get('store_name')}\n"
                    f"Distance: {round(best['dist'], 1)} km\n"
                    f"Get Directions: {best.get('maps_url')}\n\n"
                    f"I'm here if you need anything else. {brand.get('signature', 'Just Do It.')}"
                )
                send_text(sender, response)
                if mgr_phone:
                    send_text(mgr_phone, f"🚨 *NEW LEAD*: {sender} is heading to {best.get('store_name')}.")
                return {"status": "routed"}

        # --- 5. PROFESSIONAL AI CONCIERGE ---
        if not user_text: return {"status": "ignore"}
        
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse() # Chronological order
        
        # Strict Persona Guardrails
        instruction = (
            "You are a high end Nike Concierge. BE CONCISE. "
            "Never offer discounts, negotiate, or use slang. "
        )
        if goal_reached:
            instruction += "The Athlete already found a store. Answer product questions directly."
        else:
            instruction += "Your goal is to guide them to click (📎 > Location) to share their pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')}"}]
        
        # Build Context (Including Manager's Interventions!)
        for doc in history:
            if doc.get("user_msg"): 
                messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): 
                messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): 
                # This ensures the AI knows exactly what the human manager said
                messages.append({"role": "assistant", "content": f"[Human Manager intervened]: {doc.get('manager_msg')}"})
                
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant",
            max_tokens=150 # Force conciseness
        )
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, 
            "user_msg": user_text, 
            "ai_reply": ai_reply, 
            "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
        return Response(status_code=500)
        
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Nike Retail OS Online - All Guardrails Active"}