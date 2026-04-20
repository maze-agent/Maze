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
    questions_file = os.path.join(input_dir, 'questions.txt')
    context_file = os.path.join(input_dir, 'context.txt')
    question_file = os.path.join(input_dir, 'question.txt')
    answers_file = os.path.join(output_dir, 'answers.txt')
    if not os.path.exists(questions_file) or not os.path.exists(answers_file) or not os.path.exists(context_file):
        continue

    #

    with open(questions_file, 'r', encoding='utf-8') as f:
        questions = [q for q in f.readlines() if q.strip()]
    with open(answers_file, 'r', encoding='utf-8') as f:
        answers = [a for a in f.readlines()]
    with open(context_file, 'r', encoding='utf-8') as f:
        contexts = [c for c in f.readlines()]

    # 20
    chunk_size = 20
    question_chunks = [questions[i:i+chunk_size] for i in range(0, len(questions), chunk_size)]
    answer_chunks = [answers[i:i+chunk_size] for i in range(0, len(answers), chunk_size)]
    context_chunks = [contexts[i:i+chunk_size] for i in range(0, len(contexts), chunk_size)]

    #

    for q_chunk, a_chunk, c_chunk in zip(question_chunks, answer_chunks, context_chunks):
        new_task_dir = str(uuid.uuid4())
        new_input_dir = os.path.join(base_dir, new_task_dir, 'inputs')
        new_output_dir = os.path.join(base_dir, new_task_dir, 'outputs')
        os.makedirs(new_input_dir, exist_ok=True)
        os.makedirs(new_output_dir, exist_ok=True)
        #  question.txt
        shutil.copy(question_file, new_input_dir)
        #  questions.txt
        with open(os.path.join(new_input_dir, 'questions.txt'), 'w', encoding='utf-8') as f:
            f.writelines(q_chunk)
        #  context.txt
        with open(os.path.join(new_input_dir, 'context.txt'), 'w', encoding='utf-8') as f:
            f.writelines(c_chunk)
        #  answers.txt
        with open(os.path.join(new_output_dir, 'answers.txt'), 'w', encoding='utf-8') as f:
            f.writelines(a_chunk)

    #

    shutil.rmtree(task_path)