import os
import shutil

base_dir = './'
for task_dir in os.listdir(base_dir):
    task_path = os.path.join(base_dir, task_dir)
    input_dir = os.path.join(task_path, 'inputs')
    if not os.path.isdir(input_dir):
        continue
    question_file = os.path.join(input_dir, 'question.txt')
    questions_file = os.path.join(input_dir, 'questions.txt')
    if os.path.exists(question_file) and os.path.exists(questions_file):
        print(f"Deleting {task_path} ...")
        shutil.rmtree(task_path)