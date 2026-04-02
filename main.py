import os, requests, certifi, math
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response
from pymongo import MongoClient
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
kapso_api_key = os.environ.get("KAPSO_API_KEY")
mongo_client = MongoClient(os.environ.get("MONGO_URI"), tlsCAFile=certifi.where())
db = mongo_client["EnterpriseAgent"]

chat_history = db["ChatHistory"]
brands_col = db["Brands"]
stores_col = db["Stores"]
processed_msg_ids = db["ProcessedMessages"] 

# --- HELPER FUNCTIONS ---

def get_distance(lat1, lon1, lat2, lon2):
    R = 6371 
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def send_text(phone, text):
    """X-Ray Sending: Logs Kapso failures to Render for debugging."""
    url = "https://app.kapso.ai/api/v1/whatsapp_messages"
    headers = {"X-API-Key": kapso_api_key, "Content-Type": "application/json"}
    payload = {"message": {"phone_number": phone, "content": text, "message_type": "text"}}
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"❌ KAPSO ERROR: {response.text}")
        return response
    except Exception as e:
        print(f"❌ NETWORK ERROR: {e}")
        return None

def check_triage(text):
    """LLM Router: Uses strict criteria to detect if a human manager is needed."""
    triage_prompt = f"""
    Analyze this Nike Athlete message: "{text}"
    Determine if this needs a HUMAN MANAGER.
    Criteria:
    1. Anger or extreme frustration.
    2. Explicit human request (e.g., "let me talk to a person").
    3. Complex issues: Bulk orders, manufacturing defects, or refund disputes.
    
    Reply ONLY with 'ESCALATE' or 'AI_HANDLE'.
    """
    try:
        check = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": triage_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0
        )
        result = check.choices[0].message.content.strip().upper()
        print(f"🔍 TRIAGE DECISION: {result}")
        return "ESCALATE" in result
    except Exception as e:
        print(f"❌ TRIAGE CRASH: {e}")
        return False

@app.post("/webhook")
async def enterprise_webhook(request: Request):
    payload = await request.json()
    msg_data = payload.get("message", {})
    msg_id = msg_data.get("id") or msg_data.get("message_id")
    
    # --- 0. ANTI-SPAM ---
    if msg_id and processed_msg_ids.find_one({"msg_id": msg_id}):
        return Response(status_code=200)
    if msg_id:
        processed_msg_ids.insert_one({"msg_id": msg_id, "created_at": datetime.utcnow()})

    try:
        # Detect direction to handle Kapso Web Sandbox replies
        direction = msg_data.get("direction")
        is_dashboard_reply = (direction == "outbound")
        
        sender = str(msg_data.get("from") or msg_data.get("phone_number"))
        user_text = msg_data.get("text", {}).get("body") or ""
        brand = brands_col.find_one({"brand_id": "NIKE_IND"})
        mgr_phone = brand.get("manager_phone")

        print(f"\n--- INCOMING: {sender} | TEXT: {user_text} ---")

        # --- 1. MANAGER TRACKING (Handles WhatsApp & Kapso Sandbox) ---
        if sender == mgr_phone or is_dashboard_reply:
            print("👨‍💼 MANAGER DETECTED: Silencing AI & Resetting Timer.")
            # Identify customer phone from payload 'to' field if outbound
            customer_phone = str(msg_data.get("to") or msg_data.get("recipient_id") or sender)
            
            if user_text:
                chat_history.insert_one({
                    "phone_number": customer_phone,
                    "manager_msg": user_text,
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(),
                    "timestamp": datetime.utcnow()
                })
                # Lock status and reset clock
                chat_history.update_many(
                    {"phone_number": customer_phone}, 
                    {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
                )
            return {"status": "manager_active_ai_silenced"}

        # --- 2. INTELLIGENT TRIAGE (Escalation Wall) ---
        if user_text and check_triage(user_text):
            print("⚠️ ESCALATING: Detection of complex intent.")
            chat_history.update_many(
                {"phone_number": sender}, 
                {"$set": {"is_human_active": True, "last_human_interaction": datetime.utcnow()}}
            )
            alert = f"⚠️ *URGENT ESCALATION*: Athlete {sender} has a complex issue: {user_text}"
            send_text(mgr_phone, alert)
            send_text(sender, "I've detected this requires expert assistance. I'm bringing in a Nike Store Manager to help you right away! 👟")
            return {"status": "escalated_to_human"}

        # --- 3. CHECK AI SILENCE STATUS (Sliding Window TTL) ---
        user_state = chat_history.find_one({"phone_number": sender}, sort=[("_id", -1)])
        if user_state and user_state.get("is_human_active"):
            last_interaction = user_state.get("last_human_interaction")
            # 5-Minute Sliding Window (Reset by manager/user activity)
            if last_interaction and (datetime.utcnow() - last_interaction) < timedelta(minutes=5):
                print("🔇 AI MUTED: Human handling is active. Logging user text.")
                # Log user text while muted to maintain transcript
                chat_history.insert_one({
                    "phone_number": sender, 
                    "user_msg": user_text, 
                    "is_human_active": True,
                    "last_human_interaction": datetime.utcnow(), # Reset timer on user reply
                    "timestamp": datetime.utcnow()
                })
                chat_history.update_many({"phone_number": sender}, {"$set": {"last_interaction": datetime.utcnow()}})
                return {"status": "ai_muted_active_handover"}
            else:
                print("🔊 TIMEOUT: AI regaining control.")
                chat_history.update_many({"phone_number": sender}, {"$set": {"is_human_active": False}})

        # --- 4. LOCATION LOGIC ---
        location = msg_data.get("location")
        if location:
            print("📍 LOCATION PIN: Calculating nearest store.")
            u_lat, u_lon = location.get("latitude"), location.get("longitude")
            stores = list(stores_col.find({"brand_id": "NIKE_IND"}))
            if stores:
                for s in stores: s["dist"] = get_distance(u_lat, u_lon, s.get("lat"), s.get("lon"))
                best = min(stores, key=lambda x: x["dist"])
                
                chat_history.insert_one({
                    "phone_number": sender, "user_msg": "[Shared Location Pin]", 
                    "ai_reply": f"SYSTEM: Found {best.get('store_name')}", "goal_reached": True, "timestamp": datetime.utcnow()
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

        # --- 5. NATURAL LANGUAGE AI ENGINE ---
        if not user_text: return {"status": "ignore"}
        
        print("🤖 AI GENERATING RESPONSE...")
        history = list(chat_history.find({"phone_number": sender}).sort("_id", -1).limit(5))
        goal_reached = any(h.get("goal_reached") for h in history)
        history.reverse()
        
        instruction = (
            "You are a professional Nike Concierge. Be concise (max 2 paragraphs). "
            "Avoid robotic scripts and never suggest wholesale/emails. "
        )
        if goal_reached:
            instruction += "Athlete found a store. Be helpful with gear, but DON'T ask for location."
        else:
            instruction += "If they want a store, guide them to click (📎 > Location) to share their pin."

        messages = [{"role": "system", "content": f"{instruction} Persona: {brand.get('persona')} Signature: {brand.get('signature')}"}]
        for doc in history:
            if doc.get("user_msg"): messages.append({"role": "user", "content": doc.get("user_msg")})
            if doc.get("ai_reply"): messages.append({"role": "assistant", "content": doc.get("ai_reply")})
            if doc.get("manager_msg"): messages.append({"role": "assistant", "content": f"[Manager previously said]: {doc.get('manager_msg')}"})
        messages.append({"role": "user", "content": user_text})

        completion = groq_client.chat.completions.create(
            messages=messages, 
            model="llama-3.1-8b-instant",
            max_tokens=300
        )
        ai_reply = completion.choices[0].message.content

        chat_history.insert_one({
            "phone_number": sender, "user_msg": user_text, "ai_reply": ai_reply, "timestamp": datetime.utcnow()
        })
        send_text(sender, ai_reply)

    except Exception as e:
        print(f"💥 WEBHOOK FATAL: {e}")
        return Response(status_code=500)
    return Response(status_code=200)

@app.get("/")
def home(): return {"status": "Retail AI OS Online - Production Active"}