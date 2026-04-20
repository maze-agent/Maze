import uuid
import warnings
import chromadb
from agentos.rag.data import BaseData,PdfData,TextData,JsonData,CsvData,merge_content
from agentos.rag.rerank import *
from agentos.rag.embedding import *
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
 



class ChromaDB():
    
    collection_name = "agentos"
    def __init__(
        self,
        chroma_client:ClientAPI,
        collection:Collection,
        embedding_model:EmbeddingModel
    ):
        self.chroma_client=chroma_client
        self.collection=collection
        self.embedding_model=embedding_model
    

    @classmethod
    def load_document(
        cls,
        embedding_model:EmbeddingModel,
        dir:str,
    ):
        """Load Chromadb from exist dir.
 
        """
        chroma_client = chromadb.PersistentClient(path=dir) 
        collection = chroma_client.get_collection(name=cls.collection_name, embedding_function=embedding_model)
           
        return cls(
            chroma_client,
            collection,
            embedding_model,
        )
    


    @classmethod
    def create_document(
        cls,
        embedding_model:EmbeddingModel,
        if_persist:bool=False,
        dir:str=None
    ):
        """Create a Chromadb.

        Args:   
           embedding_model: The embedding model when create the Chromadb.
           if_persist: Whether persist to disk.
           dir: The dir to persist.
           collection_name: The collection name when create the Chromadb(We stipulate one ChromaDB can only one collection).
        
        Return:
            A Chromadb instance.
        """
        if if_persist:
            warnings.warn("You have to make sure there is not a ChromaDB document in the {dir} before call this function,otherwise an unknown error may occur.")
            if not dir:
                raise("please input the dir you want to persist the document")
            
            chroma_client = chromadb.PersistentClient(path=dir)
            collection = chroma_client.create_collection(name=cls.collection_name, embedding_function=embedding_model)
        else:
            chroma_client = chromadb.Client()
            collection = chroma_client.create_collection(name=cls.collection_name, embedding_function=embedding_model)


        return cls(
            chroma_client,
            collection,
            embedding_model,
        )
 
     
    
    def add_data(
        self,
        data:List[BaseData],
    ):
        documents=[d.get_content() for d in data]
        metadatas=[d.get_metadata() for d in data]

        ids = [str(uuid.uuid4()) for _ in documents]
        self.collection.add(
            documents=documents,
            metadatas=metadatas,
            ids=ids
        )
    

    def query_data(
        self,
        query_text:str,
        query_num:int=10,
        rerank:bool=False,
        reranker=None
    )->List[BaseData]:
        query_data=self.collection.query(query_texts=query_text,n_results=query_num)
        
        results=[]
        for i in range(len(query_data['metadatas'][0])):
            content=query_data['documents'][0][i]
            metadata=query_data['metadatas'][0][i]
            results.append(BaseData(content,metadata))

        if rerank:
            if not reranker:
                raise("Please input the reranker.")
            
            passages=[]
            for i in range(len(results)):
                passages.append(results[i].get_content())
            reranker_res=reranker.rerank(query_text,passages)

            
            for i in range(len(reranker_res)):
                id=reranker_res[i]['corpus_id']
                content=reranker_res[i]['text']
                results[id].set_content(content)
            
        return results