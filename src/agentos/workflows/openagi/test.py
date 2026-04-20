import argparse
from agentos.utils.query_loader import OpenAGILoader

#

args = argparse.Namespace(
    proj_path="/home/hustlbw/AgentOS",
    data_path="data",
    dag_path="dags"
)

#  dag_type  dag_id
dag_type = "document_qa"
dag_id = "3b6953e7-4acb-4251-8469-3a59b367e4c8"
dag_source = "openagi"
run_id = "test_run"
supplementary_files = []  # file

loader = OpenAGILoader(
    args=args,
    dag_id=dag_id,
    run_id=run_id,
    dag_type=dag_type,
    dag_source=dag_source,
    supplementary_files=supplementary_files
)

print("", loader.question)
print("answer", loader.answer)
print("file", loader.get_supplementary_files())