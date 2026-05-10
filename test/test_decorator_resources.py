"""
测试 @task 装饰器的资源规范化功能
"""
import pytest
from maze import task
from maze.client.maze.decorator import TaskOutputInferenceError, get_task_metadata


# 测试1: 不指定 resources - 应该使用默认值
@task
def task_default_resources(input1=None):
    """不指定resources，使用默认值"""
    return {"output1": input1}


# 测试2: 只指定部分资源 - 应该补全其他
@task(resources={"cpu": 2})
def task_partial_cpu_only(input2=None):
    """只指定cpu"""
    return {"output2": input2}


# 测试3: 只指定 cpu_mem
@task(resources={"cpu_mem": 1024})
def task_partial_cpu_mem_only(input3=None):
    """只指定cpu_mem"""
    return {"output3": input3}


# 测试4: cpu 小于 1 - 应该自动设为 1
@task(resources={"cpu": 0, "gpu": 0.5})
def task_cpu_zero(input4=None):
    """cpu设为0，应该自动修正为1"""
    return {"output4": input4}


# 测试5: cpu 为负数 - 应该自动设为 1
@task(resources={"cpu": -1})
def task_cpu_negative(input5=None):
    """cpu为负数，应该自动修正为1"""
    return {"output5": input5}


# 测试6: 指定 gpu_mem 但没有 gpu - gpu 应该自动设为 1
@task(resources={"gpu_mem": 2048})
def task_gpu_mem_only(input6=None):
    """只指定gpu_mem，gpu应该自动设为1"""
    return {"output6": input6}


# 测试7: gpu_mem > 0 但 gpu = 0 - gpu 应该自动设为 1
@task(resources={"gpu": 0, "gpu_mem": 4096})
def task_gpu_zero_with_mem(input7=None):
    """gpu=0但gpu_mem>0，gpu应该自动设为1"""
    return {"output7": input7}


# 测试8: 完整指定所有资源
@task(resources={"cpu": 4, "cpu_mem": 8192, "gpu": 2, "gpu_mem": 16384})
def task_full_resources(input8=None):
    """完整指定所有资源"""
    return {"output8": input8}


# 测试9: 混合指定
@task(resources={"cpu": 3, "gpu_mem": 1024})
def task_mixed_resources(input9=None):
    """混合指定cpu和gpu_mem"""
    return {"output9": input9}


def test_default_resources():
    """测试1: 默认资源配置"""
    metadata = get_task_metadata(task_default_resources)
    expected = {"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
    
    print("\n测试1 - 默认资源配置:")
    print(f"  Expected: {expected}")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources == expected, f"Expected {expected}, got {metadata.resources}"
    print("  ✓ 通过")


def test_partial_cpu_only():
    """测试2: 只指定cpu"""
    metadata = get_task_metadata(task_partial_cpu_only)
    expected = {"cpu": 2, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
    
    print("\n测试2 - 只指定cpu=2:")
    print(f"  Expected: {expected}")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources == expected, f"Expected {expected}, got {metadata.resources}"
    print("  ✓ 通过")


def test_partial_cpu_mem_only():
    """测试3: 只指定cpu_mem"""
    metadata = get_task_metadata(task_partial_cpu_mem_only)
    expected = {"cpu": 1, "cpu_mem": 1024, "gpu": 0, "gpu_mem": 0}
    
    print("\n测试3 - 只指定cpu_mem=1024:")
    print(f"  Expected: {expected}")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources == expected, f"Expected {expected}, got {metadata.resources}"
    print("  ✓ 通过")


def test_cpu_minimum():
    """测试4: CPU最小值为1"""
    metadata = get_task_metadata(task_cpu_zero)
    
    print("\n测试4 - CPU最小值（指定cpu=0）:")
    print(f"  Expected: cpu=1 (自动修正), gpu=0.5")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources["cpu"] == 1, f"CPU should be at least 1, got {metadata.resources['cpu']}"
    assert metadata.resources["gpu"] == 0.5, f"GPU should be 0.5, got {metadata.resources['gpu']}"
    print("  ✓ 通过")


def test_cpu_negative():
    """测试5: CPU负数自动修正"""
    metadata = get_task_metadata(task_cpu_negative)
    
    print("\n测试5 - CPU负数（指定cpu=-1）:")
    print(f"  Expected: cpu=1 (自动修正)")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources["cpu"] == 1, f"CPU should be at least 1, got {metadata.resources['cpu']}"
    print("  ✓ 通过")


def test_gpu_auto_set():
    """测试6: GPU自动设置"""
    metadata = get_task_metadata(task_gpu_mem_only)
    
    print("\n测试6 - GPU自动设置（只指定gpu_mem=2048）:")
    print(f"  Expected: gpu=1 (自动设置), gpu_mem=2048")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources["gpu"] == 1, f"GPU should be auto-set to 1, got {metadata.resources['gpu']}"
    assert metadata.resources["gpu_mem"] == 2048, f"GPU_MEM should be 2048, got {metadata.resources['gpu_mem']}"
    print("  ✓ 通过")


def test_gpu_zero_with_mem():
    """测试7: GPU=0但有gpu_mem时自动设置"""
    metadata = get_task_metadata(task_gpu_zero_with_mem)
    
    print("\n测试7 - GPU自动修正（gpu=0但gpu_mem=4096）:")
    print(f"  Expected: gpu=1 (自动修正), gpu_mem=4096")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources["gpu"] == 1, f"GPU should be auto-corrected to 1, got {metadata.resources['gpu']}"
    assert metadata.resources["gpu_mem"] == 4096, f"GPU_MEM should be 4096, got {metadata.resources['gpu_mem']}"
    print("  ✓ 通过")


def test_full_resources():
    """测试8: 完整资源配置"""
    metadata = get_task_metadata(task_full_resources)
    expected = {"cpu": 4, "cpu_mem": 8192, "gpu": 2, "gpu_mem": 16384}
    
    print("\n测试8 - 完整资源配置:")
    print(f"  Expected: {expected}")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources == expected, f"Expected {expected}, got {metadata.resources}"
    print("  ✓ 通过")


def test_mixed_resources():
    """测试9: 混合资源配置"""
    metadata = get_task_metadata(task_mixed_resources)
    
    print("\n测试9 - 混合资源（cpu=3, gpu_mem=1024）:")
    print(f"  Expected: cpu=3, cpu_mem=0, gpu=1 (自动设置), gpu_mem=1024")
    print(f"  Actual:   {metadata.resources}")
    
    assert metadata.resources["cpu"] == 3, f"CPU should be 3, got {metadata.resources['cpu']}"
    assert metadata.resources["cpu_mem"] == 0, f"CPU_MEM should be 0, got {metadata.resources['cpu_mem']}"
    assert metadata.resources["gpu"] == 1, f"GPU should be auto-set to 1, got {metadata.resources['gpu']}"
    assert metadata.resources["gpu_mem"] == 1024, f"GPU_MEM should be 1024, got {metadata.resources['gpu_mem']}"
    print("  ✓ 通过")


def test_task_without_parentheses_infers_inputs_outputs_and_types():
    """@task without parentheses should infer normal-function metadata."""
    metadata = get_task_metadata(task_default_resources)

    assert metadata.inputs == ["input1"]
    assert metadata.outputs == ["output1"]
    assert metadata.data_types["input1"] == "str"
    assert metadata.data_types["output1"] == "any"


def test_multi_input_task_infers_signature_and_return_dict_keys():
    @task(resources={"cpu": 2})
    def combine_text(left: str, right: int = 1):
        return {"combined": f"{left}:{right}", "count": right}

    metadata = get_task_metadata(combine_text)

    assert metadata.inputs == ["left", "right"]
    assert metadata.outputs == ["combined", "count"]
    assert metadata.data_types["left"] == "str"
    assert metadata.data_types["right"] == "int"
    assert metadata.resources == {"cpu": 2, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}


def test_serialized_callable_uses_keyword_arguments_and_defaults():
    @task
    def greet(name: str, suffix: str = "!"):
        return {"message": f"hello {name}{suffix}"}

    metadata = get_task_metadata(greet)
    import base64
    import cloudpickle

    runner_callable = cloudpickle.loads(base64.b64decode(metadata.code_ser))

    assert runner_callable({"name": "Maze"}) == {"message": "hello Maze!"}
    assert runner_callable({"name": "Maze", "suffix": "?"}) == {"message": "hello Maze?"}


def test_task_return_must_be_dict_literal_for_output_inference():
    with pytest.raises(TaskOutputInferenceError, match="must return a dict literal"):
        @task
        def bad_return(value: str):
            return value


def test_old_inputs_outputs_decorator_options_are_rejected():
    with pytest.raises(TypeError, match="no longer accepts"):
        @task(inputs=["value"], outputs=["result"])
        def old_style(value):
            return {"result": value}


if __name__ == "__main__":
    print("=" * 60)
    print("开始测试 @task 装饰器的资源规范化功能")
    print("=" * 60)
    
    test_default_resources()
    test_partial_cpu_only()
    test_partial_cpu_mem_only()
    test_cpu_minimum()
    test_cpu_negative()
    test_gpu_auto_set()
    test_gpu_zero_with_mem()
    test_full_resources()
    test_mixed_resources()
    
    print("\n" + "=" * 60)
    print("🎉 所有测试通过！资源规范化功能正常工作。")
    print("=" * 60)
