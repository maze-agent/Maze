import csv
from pathlib import Path
from pypdf import PdfReader
from agentos.rag.data import JsonData,TextData,PdfData,CsvData
   
   

def text_load(file_path,**kwargs):
    encoding = kwargs.get('encoding')
    with open(file_path, 'r' , encoding=encoding) as file:
        content = file.read()
        
    return TextData(content,encoding)

def json_load(file_path,**kwargs):
    encoding = kwargs.get('encoding')
    with open(file_path, 'r' , encoding=encoding) as file:
        content = file.read()
        
    return JsonData(content,encoding)

def pdf_load(file_path,**kwargs):  
    reader = PdfReader(file_path)
    number_of_pages = len(reader.pages)
    #page = reader.pages[0]
    #text = page.extract_text()
    
    page_str=""
    for i in range(number_of_pages):
        page_str = page_str+reader.pages[i].extract_text()
         

    return PdfData(page_str,number_of_pages)

def csv_load(file_path,**kwargs):
    encoding = kwargs.get('encoding')
    
   
    content=""
    with open(file_path, 'r', encoding=encoding) as f:
        reader = csv.reader(f)     
        for row in reader:
            content += ','.join(row)
            content += '\n'
     
    return CsvData(content,encoding)

load_fun_dict={
    ".txt":text_load,
    ".json":json_load,
    ".pdf":pdf_load,
    ".csv":csv_load
}
    

class DataLoader:
    def __init__(
        self,
        file_path:str,
        encoding:str = "utf-8", #for [text_load,json_load]
    ):
        self.file_path=file_path
        self.encoding=encoding
    
    def load_data(
        self
    ):
        file_suffix = Path(self.file_path).suffix
        data=load_fun_dict[file_suffix](
            file_path=self.file_path,
            encoding=self.encoding
        )
        return data  
