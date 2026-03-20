import math
import json
from datetime import datetime

def calculate_distance(lat1, lon1, lat2, lon2):
    # Haversine formula
    R = 6371 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def get_best_store(user_lat, user_lon):
    with open('data/stores.json') as f:
        stores = json.load(f)
    
    current_time = datetime.now().strftime("%H:%M")
    available_stores = []

    for store in stores:
        # Check if open and has capacity
        if (store['open_time'] <= current_time <= store['close_time']) and \
           (store['active_chats'] < store['max_capacity']):
            
            dist = calculate_distance(user_lat, user_lon, store['lat'], store['long'])
            store['dist'] = dist
            available_stores.append(store)

    if not available_stores:
        return None
    
    # Return the nearest one
    return min(available_stores, key=lambda x: x['dist'])
