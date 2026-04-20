import os
import ray
import argparse
from typing import Dict, List
from collections import Counter

@ray.remote
class DAGContext:
    """
    DAGContext class to store and manage DAG-related data.
    """

    def __init__(self, dag_id: str, node_id: str, node_ip: str)-> None:
        """
        Initialize DAGContext with DAG ID, preferred node ID, and preferred node IP.

        :param dag_id: Unique identifier for the DAG
        :param node_id: Preferred node ID for task execution
        :param node_ip: Preferred node IP address for task execution
        """
        self.data= {}
        self.data["dag_id"]= ray.put(dag_id)
        self.dag_id= dag_id
        self.preferred_node_ip= node_ip
        self.preferred_node_id= node_id

    def put(self, key: str, value: object)-> None:
        """
        Store a value in the DAG context using a key.

        :param key: The key for the data
        :param value: The value to store in the context
        """
        self.data[key]= ray.put(value)

    def get(self, key: str)-> object:
        """
        Retrieve a value from the DAG context using a key.

        :param key: The key for the data
        :return: The value associated with the key
        :raises KeyError: If the key is not found in the context
        """
        if key not in self.data:
            raise KeyError(f"Key '{key}' not found.")
        return ray.get(self.data[key])

    def get_preferred_id(self)-> str:
        """
        Get the preferred node ID for task execution.

        :return: Preferred node ID
        """
        return self.preferred_node_id

class DAGContextManager:
    """
    Manager for maintaining the mapping of DAG ID to DAGContext actor.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        """
        Initialize the DAGContextManager with dictionaries for DAG to context and context to ID mapping.
        """
        self.args= args
        self.run2ctx: Dict[str, ray.actor.ActorHandle]= {}
        self.ctx2id: Dict[ray.actor.ActorHandle, str]= {}
        self.node_load_counter: Dict[str, int] = Counter()

    def get_context(self, run_id: str)-> ray.actor.ActorHandle:
        """
        Retrieve the DAGContext actor associated with the given DAG ID.

        :param run_id: The DAG ID
        :return: The DAGContext actor for the specified DAG ID, or None if not found
        """
        return self.run2ctx.get(run_id, None)

    def get_id(self, ctx: ray.actor.ActorHandle)-> str:
        """
        Retrieve the node ID associated with the given DAGContext actor.

        :param ctx: The DAGContext actor
        :return: The node ID associated with the given DAGContext
        """
        return self.ctx2id[ctx]

    def create_context(self, node_id: str, node_ip: str, task_info: dict)-> ray.actor.ActorHandle:
        """
        Create and return a new DAGContext actor instance bound to the specified compute node.

        :param run_id: The DAG ID
        :param node_id: The node ID where the context will be executed
        :param node_ip: The node IP address where the context will be executed
        :return: The newly created DAGContext actor
        """
        run_id, arrival_time, func_name= task_info.get('run_id'), task_info.get('arrival_time'), task_info.get('func_name')
        dag_ctx= DAGContext.remote(run_id, node_id, node_ip)  # Instantiate remote DAGContext
        self.run2ctx[run_id]= dag_ctx  # Store DAGContext in the manager
        self.ctx2id[dag_ctx]= node_id
        self.node_load_counter[node_id] += 1
        print(f"  -> Context created on node '{node_id}'. Current node loads: {dict(self.node_load_counter)}")
        dag_ctx.put.remote(f"{func_name}_request_api_url", task_info.get(f"{func_name}_request_api_url", None))
        dag_ctx.put.remote("model_folder", os.path.join(self.args.proj_path, self.args.model_folder))
        dag_ctx.put.remote("use_online_model", self.args.use_online_model)
        dag_ctx.put.remote("api_url", self.args.api_url)
        dag_ctx.put.remote("api_key", self.args.api_key)
        dag_ctx.put.remote("top_p", self.args.top_p)
        dag_ctx.put.remote("max_tokens", self.args.max_tokens)
        dag_ctx.put.remote("temperature", self.args.temperature)
        dag_ctx.put.remote("repetition_penalty", self.args.repetition_penalty)
        dag_ctx.put.remote("arrival_time", arrival_time)
        dag_ctx.put.remote("vlm_batch_size", self.args.vlm_batch_size)
        dag_ctx.put.remote("text_batch_size", self.args.text_batch_size)
        return dag_ctx

    def release_context(self, run_id: str)-> bool:
        """
        Release and remove the DAGContext actor associated with the given DAG ID.

        :param dag_id: The DAG ID
        :return: True if context was found and released, False otherwise        
        """
        ctx= self.run2ctx.pop(run_id, None)
        if ctx:
            node_id = self.ctx2id.pop(ctx, None)
            if node_id and node_id in self.node_load_counter:
                self.node_load_counter[node_id] -= 1
                if self.node_load_counter[node_id] == 0:
                    del self.node_load_counter[node_id]
                print(f"  -> Context released from node '{node_id}'. Current node loads: {dict(self.node_load_counter)}")
            ray.kill(ctx)
            return True
        return False

    def get_least_loaded_node(self, available_nodes: List[str]):
        """
        Finds the node with the minimum number of active DAGContexts among a list of available nodes.
        """
        if not available_nodes:
            return None
            
        # Find the node with the minimum count, defaulting to 0 if a node has no contexts yet
        least_loaded_node = min(available_nodes, key=lambda node_id: self.node_load_counter.get(node_id, 0))
        return least_loaded_node

    def get_all_contexts(self)-> Dict[str, ray.actor.ActorHandle]:
        """
        Get all currently active DAGContexts.

        :return: Dictionary mapping dag_id to DAGContext actor
        """
        return self.run2ctx