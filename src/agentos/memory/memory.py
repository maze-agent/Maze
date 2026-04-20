from agentos.memory.message import Message


class TemporaryMemory:
   
    def __init__(
        self
    ):    
        self.memory=[]

    def add_memory(
        self,
        msg:Message
    ): 
        self.memory.append({"role":msg.role,"content":msg.content})

    def clear(
        self
    ):
        self.memory=[]
    
    
            
        
        
         
    
        