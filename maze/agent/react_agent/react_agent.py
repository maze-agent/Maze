"""ReAct Agent implementation."""
import json

class ReActAgent:
    """
    ReAct (Reasoning and Acting) Agent implementation.
    This agent uses a reasoning-acting cycle with memory and tools.
    """
    
    def __init__(
        self, 
        model,   
        memory,  
        toolkit,  
        max_iterations: int = 10
    ):
        """
        Initialize the ReAct agent.
        
        Args:
            model: Language model to use for reasoning
            memory: Memory component to store conversation history
            toolkit: Toolkit containing available tools
            max_iterations: Maximum number of reasoning-action cycles
        """
        self.model = model
        self.memory = memory
        self.toolkit = toolkit
        self.max_iterations = max_iterations
    
    async def run(self, prompt: str) -> str:
        """
        Run the ReAct agent with the given prompt.
        
        Args:
            prompt: Initial prompt for the agent
            
        Returns:
            Final response from the agent
        """
        # Add the initial user prompt to memory
        self.memory.add_message("user", prompt)
        
        iteration = 0        
        while iteration < self.max_iterations:            
            iteration += 1
            
            # Get all messages from memory to provide context
            messages = self.memory.get_messages()
             
            # Prepare tools for the model
            tools_schema = await self.toolkit.get_all_schemas()
            tools = list(tools_schema.values())

            # Get model response with tools
            response = await self.model.chat_completion(
                messages=messages,
                tools=tools if tools else None
            )
            # Extract the response content
            choice = response["choices"][0]
            message = choice["message"]

            # Check if the model wants to use a tool
            if len(message["tool_calls"]) > 0:
                # Process tool calls
                for tool_call in message["tool_calls"]:
                    tool_name = tool_call["function"]["name"]
                    print("Call tool:", tool_name)
                    arguments = json.loads(tool_call["function"]["arguments"])
                    # Execute the tool
                    tool_func = await self.toolkit.get_tool(tool_name)
                    if tool_func:
                        try:
                            tool_result = await tool_func(**arguments)
                            # Add tool call and result to memory
                            self.memory.add_message(
                                "assistant", 
                                f"Tool call: {tool_name}({arguments})",
                                {"tool_call": True, "tool_name": tool_name}
                            )
                            self.memory.add_message(
                                "system", 
                                str(tool_result),
                                {"tool_result": True, "tool_name": tool_name}
                            )
                        except Exception as e:
                            error_msg = f"Error executing tool {tool_name}: {str(e)}"
                            self.memory.add_message("tool", error_msg, {"tool_error": True})
                            print("Tool error:", error_msg)
                    else:
                        error_msg = f"Tool {tool_name} not found"
                        self.memory.add_message("tool", error_msg, {"tool_error": True})
            else:
                # If no tool calls, add the final response to memory and return
                final_response = message.get("content", "")
                if final_response:
                    self.memory.add_message("assistant", final_response)
                    return final_response
        
        # If we've reached max iterations without a final response
        fallback_response = "I've reached the maximum number of iterations without reaching a conclusion."
        self.memory.add_message("assistant", fallback_response)
        return fallback_response