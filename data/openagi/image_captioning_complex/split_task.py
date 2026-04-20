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
    output_dir = os.path.join(task_path, 'outputs')
    images_dir = os.path.join(input_dir, 'images')
    question_file = os.path.join(input_dir, 'question.txt')
    labels_file = os.path.join(output_dir, 'labels.txt')
    if not os.path.exists(images_dir) or not os.path.exists(labels_file):
        continue

    #

    image_files = sorted([f for f in os.listdir(images_dir) if f.endswith('.jpg')])
    with open(labels_file, 'r', encoding='utf-8') as f:
        labels = [l for l in f.readlines()]

    chunk_size = 20
    image_chunks = [image_files[i:i+chunk_size] for i in range(0, len(image_files), chunk_size)]
    label_chunks = [labels[i:i+chunk_size] for i in range(0, len(labels), chunk_size)]

    for img_chunk, lbl_chunk in zip(image_chunks, label_chunks):
        new_task_dir = str(uuid.uuid4())
        new_input_dir = os.path.join(base_dir, new_task_dir, 'inputs')
        new_images_dir = os.path.join(new_input_dir, 'images')
        new_output_dir = os.path.join(base_dir, new_task_dir, 'outputs')
        os.makedirs(new_images_dir, exist_ok=True)
        os.makedirs(new_output_dir, exist_ok=True)
        #  question.txt
        shutil.copy(question_file, new_input_dir)
        #

        for img in img_chunk:
            shutil.copy(os.path.join(images_dir, img), new_images_dir)
        #  labels.txt
        with open(os.path.join(new_output_dir, 'labels.txt'), 'w', encoding='utf-8') as f:
            f.writelines(lbl_chunk)

    #

    shutil.rmtree(task_path)