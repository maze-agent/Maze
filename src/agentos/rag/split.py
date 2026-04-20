from agentos.rag.data import BaseData
from typing import List
 
 
class CharacterSplit:
    def __init__(
        self,
        chunk_size:int,
        chunk_overlap:int=0
    ):
        self.chunk_size=chunk_size
        self.chunk_overlap=chunk_overlap
    
    def split(
        self,
        data:BaseData
    )->List[BaseData]:
        
        meta_data=data.get_metadata()
        content = data.get_content()
        chunk_res = []

        i = 0
        while i < len(content):
            if i > 0:
                start = max(i - self.chunk_overlap, 0)
            else:
                start = i
            end = min(start + self.chunk_size, len(content))   

           
            chunk_res.append(BaseData(content=content[start:end],metadata=meta_data))
            
            i = end
            
        return chunk_res


class RowSplit:
    def __init__(
        self,
        chunk_row_size:int,
        chunk_overlap:int=0
    ):
        self.chunk_row_size=chunk_row_size
        self.chunk_overlap=chunk_overlap
        assert(chunk_overlap<chunk_row_size)
    
    def split(
        self,
        data:BaseData
    )->List[BaseData]:
        
        meta_data=data.get_metadata()
        content = data.get_content().split('\n')
        chunk_res = []

        begin = 0
        while begin < len(content):
            end = min(begin + self.chunk_row_size, len(content))   
            chunk_res.append(BaseData(content='\n'.join(content[begin:end]),metadata=meta_data))
            begin=begin+self.chunk_row_size-self.chunk_overlap
        
        return chunk_res

