from typing import Dict,Any,List

class BaseData():
    content = ""
    metadata = {}

    def __init__(
        self,
        content:str,
        metadata:Dict[str,Any]
    ):
        self.content=content
        self.metadata=metadata

    def get_content(self):
        return self.content    

    def set_content(self,content:str):
        self.content=content

    def get_metadata(self):
        return self.metadata    

    def add_metadata(
        self,
        key:str,
        value,
    ):
        self.metadata[key] = value
   
class PdfData(BaseData):
    def __init__(
        self,
        content:str,
        number_of_pages
    ):
        self.content=content
        self.metadata["number_of_pages"]=number_of_pages

class TextData(BaseData):
    def __init__(
        self,
        content:str,
        encoding:str = "utf-8"
    ):
        self.content=content
        self.metadata["encoding"]=encoding
         
class JsonData(BaseData):
    def __init__(
        self,
        content:str,
        encoding:str = "utf-8"
    ):
        self.content=content
        self.metadata["encoding"]=encoding

class CsvData(BaseData):
    def __init__(
        self,
        content:str,
        encoding:str = "utf-8"
    ):
        self.content=content
        self.metadata["encoding"]=encoding

def merge_content(
    data:List[BaseData]
):
    return "\n\n".join(d.get_content() for d in data)
    