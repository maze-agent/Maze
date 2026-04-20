import sentence_transformers
from typing import List
from agentos.rag.data import BaseData
from chromadb import Documents, Embeddings


class EmbeddingModel:
    def __init__(
        self,
        model_name:str,
        cache_dir:str=None,
        **kwargs
    ):
        self.model_name=model_name
      
        self.embedding_model= sentence_transformers.SentenceTransformer(  
            model_name, cache_folder=cache_dir,**kwargs
        )
    
    def __call__(self, input: Documents) -> Embeddings:
        return  [self.embedding_model.encode(text) for text in input]

    def encode(
        self,
        data:List[BaseData]
    ):
        content = [d.get_content() for d in data]
        return self.embedding_model.encode(content)
        
 