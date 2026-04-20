import networkx as nx
from typing import Dict, Tuple
#

TASK_TYPE_DEFAULT_EXEC_TIMES = {'io': 2.0, 'cpu': 5.0, 'gpu': 30.0, 'default': 1.0}

def compute_ranku(dag: nx.DiGraph, exec_time_db: Dict[Tuple[str, str], float], dag_id: str) -> Dict[str, float]:
    """
    useDFS()DAGtaskranku
    ranku(i) = w(i) + max_{j in succ(i)}(ranku(j))
    w(i)taskisucc(i)taskitask
    taskranku
    Args:
        dag (nx.DiGraph): NetworkX
        exec_time_db (Dict): task
                                 {(dag_id, task_name): exec_time}
        dag_id (str): DAGID

    Returns:
        Dict[str, float]: taskranku
    """
    ranku_cache = {}
    def dfs(task_name: str) -> float:
        #

        if task_name in ranku_cache:
            return ranku_cache[task_name]
        #

        exec_time = exec_time_db.get((dag_id, task_name))
        if exec_time is None:
            task_type = dag.nodes[task_name].get('type')
            exec_time = TASK_TYPE_DEFAULT_EXEC_TIMES.get(task_type, TASK_TYPE_DEFAULT_EXEC_TIMES['default'])

        #

        successors = list(dag.successors(task_name))
        # ranku
        if not successors:
            ranku_cache[task_name] = exec_time
            return exec_time
        # ranku + ranku
        max_succ_ranku = max(dfs(s_task) for s_task in successors)
        ranku_cache[task_name] = exec_time + max_succ_ranku
        return ranku_cache[task_name]
    # ranku
    for node in dag.nodes:
        if node not in ranku_cache:
            dfs(node)
    return ranku_cache


def compute_rankd(dag: nx.DiGraph, exec_time_db: Dict[Tuple[str, str], float], dag_id: str) -> Dict[str, float]:
    """
    useDFS()DAGtaskrankd
    rankd(i) = max_{j in pred(i)}(rankd(j) + w(j))
    w(j)taskjpred(i)taskitask
    taskrankd0

    Args:
        dag (nx.DiGraph): NetworkX
        exec_time_db (Dict): task
                                 {(dag_id, task_name): exec_time}
        dag_id (str): DAGID

    Returns:
        Dict[str, float]: taskrankd
    """
    rankd_cache = {}
    def dfs(task_name: str) -> float:
        #

        if task_name in rankd_cache:
            return rankd_cache[task_name]
        #

        predecessors = list(dag.predecessors(task_name))
        # rankd0
        if not predecessors:
            rankd_cache[task_name] = 0
            return 0
        exec_time = exec_time_db.get((dag_id, task_name))
        if exec_time is None:
            task_type = dag.nodes[task_name].get('type')
            exec_time = TASK_TYPE_DEFAULT_EXEC_TIMES.get(task_type, TASK_TYPE_DEFAULT_EXEC_TIMES['default'])
        # rankd "rankd(j) + w(j)" 
        max_pred_rankd = max(
            dfs(p_task)+ exec_time
            for p_task in predecessors
        )
        rankd_cache[task_name] = max_pred_rankd
        return rankd_cache[task_name]
    # rankd
    for node in dag.nodes:
        if node not in rankd_cache:
            dfs(node)
    return rankd_cache