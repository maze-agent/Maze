import os
import shutil
import uuid

base_dir = './'
for task_dir in os.listdir(base_dir):
    task_path = os.path.join(base_dir, task_dir)
    #  uuid 
    if not os.path.isdir(task_path) or '-' not in task_dir or task_dir == '__pycache__':
        continue
    input_dir = os.path.join(task_path, 'inputs')
    question_file = os.path.join(input_dir, 'question.txt')
    text_file = os.path.join(input_dir, 'text.txt')
    if not os.path.exists(question_file) or not os.path.exists(text_file):
        continue

    #  text.txt 
    with open(text_file, 'r', encoding='utf-8') as f:
        text_lines = [line for line in f.readlines()]

    chunk_size = 20
    text_chunks = [text_lines[i:i+chunk_size] for i in range(0, len(text_lines), chunk_size)]

    for idx, chunk in enumerate(text_chunks):
        new_task_dir = str(uuid.uuid4())
        new_input_dir = os.path.join(base_dir, new_task_dir, 'inputs')
        os.makedirs(new_input_dir, exist_ok=True)
        #  question.txt
        shutil.copy(question_file, new_input_dir)
        #  text.txt
        with open(os.path.join(new_input_dir, 'text.txt'), 'w', encoding='utf-8') as f:
            f.writelines(chunk)

    #

    shutil.rmtree(task_path)