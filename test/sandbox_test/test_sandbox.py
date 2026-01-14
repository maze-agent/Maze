import pytest
import asyncio
from maze.sandbox.client import CodeSandboxClient
 


@pytest.mark.asyncio
async def test_sandbox_concurrent_execution():
    """Test concurrent execution in multiple sandboxes."""
    codesandbox1 = CodeSandboxClient(url="http://localhost:8000")
    codesandbox2 = CodeSandboxClient(url="http://localhost:8000")
    
    try:
        # Run simple code concurrently
        result1, result2 = await asyncio.gather(
            codesandbox1.run_code("print('hello world from sandbox1')"),
            codesandbox2.run_code("print('hello world from sandbox2')")
        )
        
        assert 'stdout' in result1
        assert 'stdout' in result2
        assert 'hello world from sandbox1' in result1['stdout']
        assert 'hello world from sandbox2' in result2['stdout']
        
    
    finally:
        await codesandbox1.close()
        await codesandbox2.close()
 