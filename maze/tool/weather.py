from maze import task
import requests


@task
def weather(location: str = "", api_key: str = ""):
    
    if not location or not api_key:
        return {"result": None, "error": "Missing required parameters: location or api_key"}
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/weather"
        params = {
            "q": location,
            "appid": api_key,
            "units": "metric" 
        }
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        weather_info = {
            "location": data["name"],
            "temperature": data["main"]["temp"],
            "feels_like": data["main"]["feels_like"],
            "humidity": data["main"]["humidity"],
            "description": data["weather"][0]["description"],
            "wind_speed": data["wind"]["speed"]
        }
        
        return {"result": weather_info}
    except Exception as e:
        return {"result": None, "error": str(e)}