import os
import json

root = '.'
output_file = 'openagi_query.jsonl'
task_types = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]

with open(output_file, 'w') as fout:
    for task_type in task_types:
        task_dir = os.path.join(root, task_type)
        for dag_id in os.listdir(task_dir):
            dag_path = os.path.join(task_dir, dag_id)
            inputs_dir = os.path.join(dag_path, 'inputs')
            if not os.path.isdir(inputs_dir):
                continue
            files = []
            for f in sorted(os.listdir(inputs_dir)):
                f_path = os.path.join(inputs_dir, f)
                if f in ['images'] and task_type in ['image_captioning_complex', 'multimodal_vqa_complex']:
                    images_dir = f_path
                    if os.path.isdir(images_dir):
                        images = sorted(os.listdir(images_dir))
                        files.extend([f"images/{img}" for img in images])
                elif os.path.isfile(f_path):
                    files.append(f)
            entry = {
                "dag_id": dag_id,
                "dag_source": "openagi",
                "dag_type": task_type,
                "dag_supplementary_files": files
            }
            fout.write(json.dumps(entry, ensure_ascii=False) + '\n')