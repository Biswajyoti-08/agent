import requests

# URL of your FastAPI server
URL = "http://127.0.0.1:8000/webhook" 

# Mocking the WhatsApp Location Event
payload = {
    "message": {
        "from": "918888888888", 
        "location": {
            "latitude": 12.9250,   # Near Jayanagar
            "longitude": 77.5890
        }
    }
}

headers = {
    "X-Webhook-Event": "whatsapp.message.received",
    "Content-Type": "application/json"
}

response = requests.post(URL, json=payload, headers=headers)

print(f"Status Code: {response.status_code}")
print(f"Response Body: {response.json()}")
