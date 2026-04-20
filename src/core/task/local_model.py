from typing import List
from modelscope import AutoModelForCausalLM, AutoTokenizer

def modelscope_query(model:str,messages:List):
    
    
    local_model = AutoModelForCausalLM.from_pretrained(
        model,
        torch_dtype="auto", 
        low_cpu_mem_usage=True,
    ).to("cuda")
    tokenizer = AutoTokenizer.from_pretrained(model)
    
    text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True
    )
    model_inputs = tokenizer([text], return_tensors="pt").to("cuda")
    generated_ids = model.generate(
        **model_inputs,
        max_new_tokens=1024
    )
    generated_ids = [
        output_ids[len(input_ids):] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]

    response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
    return response