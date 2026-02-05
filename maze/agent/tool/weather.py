"""Weather tool implementation using function instead of class."""
import random
from typing import Dict, Any


async def get_current_weather(location: str) -> Dict[str, Any]:
    """
    Get current weather for a given location.
    
    Args:
        location: Location to get weather for
        
    Returns:
        Weather information dictionary
    """
    # Simulate getting weather data
    weather_conditions = ["sunny", "cloudy", "rainy", "snowy", "windy"]
    temperature = random.randint(-10, 40)
    condition = random.choice(weather_conditions)
    
    return {
        "location": location,
        "temperature": f"{temperature}°C",
        "condition": condition,
        "humidity": f"{random.randint(30, 90)}%",
        "description": f"The current weather in {location} is {condition} with a temperature of {temperature}°C."
    }