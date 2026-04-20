#: tbench 

import json
from typing import Dict, Any
from agentos.utils.tbench_tools.retail.get_product_details import GetProductDetails

def _extract_json_from_llm_output(llm_output: str) -> str:
    """LLMemitJSONstring"""
    llm_output = llm_output.strip()
    json_block_start = llm_output.find("```json")
    if json_block_start != -1:
        start = json_block_start + len("```json")
        end = llm_output.find("```", start)
        if end != -1:
            return llm_output[start:end].strip()
    start = llm_output.find('{')
    end = llm_output.rfind('}')
    if start != -1 and end != -1 and start < end:
        return llm_output[start:end+1].strip()
    start = llm_output.find('[')
    end = llm_output.rfind(']')
    if start != -1 and end != -1 and start < end:
        return llm_output[start:end+1].strip()
    return ""

def _find_item_details_in_order(order_details: Dict[str, Any], item_spec: Dict[str, Any]) -> Dict[str, Any]:
    """In order"""
    if not item_spec or not item_spec.get("name"): return None
    item_name_to_find = item_spec["name"].lower()
    attributes_to_find = {k.lower(): str(v).lower() for k, v in item_spec.get("attributes", {}).items()}
    for item in order_details.get("items", []):
        if item["name"].lower() == item_name_to_find:
            item_options = {k.lower(): str(v).lower() for k, v in item.get("options", {}).items()}
            if all(item_options.get(k) == v for k, v in attributes_to_find.items()):
                return item
    return None

def _find_new_product_variant_id(backend_data: Dict[str, Any], product_id: str, original_item_options: Dict[str, Any], new_item_spec: Dict[str, Any]) -> str:
    """ID"""
    product_details_str = GetProductDetails.invoke(backend_data, product_id)
    if "Error" in product_details_str: return None
    product_info = json.loads(product_details_str)
    target_options = original_item_options.copy()
    if new_item_spec and new_item_spec.get("attributes"):
        target_options.update(new_item_spec.get("attributes"))
    
    normalized_target = {k.lower(): str(v).lower() for k, v in target_options.items()}
    
    for variant in product_info.get("variants", {}).values():
        normalized_variant = {k.lower(): str(v).lower() for k, v in variant.get("options", {}).items()}
        if normalized_target == normalized_variant:
            return variant.get("item_id")
    return None 