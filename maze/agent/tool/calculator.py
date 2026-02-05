"""Calculator tool implementation using function instead of class."""
import re
from typing import Union


async def calculator(expression: str) -> Union[float, str]:
    """
    Calculate mathematical expressions.
    
    Args:
        expression: Mathematical expression to evaluate
        
    Returns:
        Result of the calculation or error message
    """
    # Sanitize the input to prevent code injection
    sanitized_expr = re.sub(r'[^\d+\-*/().\s]', '', expression)
    
    try:
        # Evaluate the expression safely
        result = eval(sanitized_expr, {"__builtins__": {}}, {})
        return float(result)
    except Exception as e:
        return f"Error evaluating expression: {str(e)}"