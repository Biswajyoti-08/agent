import requests
import time

# URL of your live FastAPI server on Render
URL = "https://agent-bjjq.onrender.com/webhook"

# Mocking the WhatsApp Location Event from an "Athlete"
payload = {
    "message": {
        "from": "918888888888",  # The mock customer's phone number
        "location": {
            "latitude": 12.9250,   # Coordinates near the Jayanagar store
            "longitude": 77.5890
        }
    }
}

headers = {
    "X-Webhook-Event": "whatsapp.message.received",
    "Content-Type": "application/json"
}

print("🚀 Initiating Final PoC Test: Nike OnGround.ai")
print(f"📡 Pinging Server: {URL}")
print("📍 Sending mock location data...")

try:
    # Adding a 15-second timeout. If Render is asleep, it might take a moment to wake up.
    start_time = time.time()
    response = requests.post(URL, json=payload, headers=headers, timeout=15)
    elapsed_time = round(time.time() - start_time, 2)

    print(f"\n✅ Request Complete in {elapsed_time}s")
    print(f"🟢 Status Code: {response.status_code}")
    
    if response.status_code == 200:
        print(f"📩 Response Body: {response.json()}")
        print("\n🎉 SUCCESS! The routing logic fired.")
        print("📱 Check your WhatsApp (the number you set as Manager) to see the alert!")
    else:
        print(f"⚠️ Server returned an unexpected status: {response.text}")

except requests.exceptions.Timeout:
    print("\n⏳ Error: Request timed out. Render's free tier is likely waking up from a 'cold start'.")
    print("💡 Fix: Wait 15 seconds and run this script one more time!")
except requests.exceptions.ConnectionError:
    print("\n❌ Error: Connection Refused. Double-check that your Render service is 'Live' and the URL is exact.")
except Exception as e:
    print(f"\n❌ An unexpected error occurred: {e}")
