import { useState, useEffect } from 'react';
import { Modal, Button, message, Alert, Spin, Space } from 'antd';
import { CodeOutlined, CheckOutlined, ReloadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { WorkflowNode } from '@/types/workflow';

interface CustomTaskEditorProps {
  node: WorkflowNode;
  open: boolean;
  onClose: () => void;
}

export default function CustomTaskEditor({ node, open, onClose }: CustomTaskEditorProps) {
  const { updateNode, selectNode, nodes } = useWorkflowStore();
  const [code, setCode] = useState('');
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);

  // 当编辑器打开时，从最新的节点数据加载代码
  useEffect(() => {
    if (open) {
      const currentNode = nodes.find(n => n.id === node.id);
      const currentCode = currentNode?.data.customCode || '';
      
      console.log('🔍 编辑器打开');
      console.log('   节点ID:', node.id);
      console.log('   当前节点:', currentNode ? '找到' : '未找到');
      console.log('   代码长度:', currentCode.length);
      console.log('   代码预览:', currentCode.substring(0, 80) + (currentCode.length > 80 ? '...' : ''));
      
      setCode(currentCode);
      setParseError(null);
    }
  }, [open, node.id, nodes]);

  const defaultCode = `from maze.core.client.decorator import task

@task(
    inputs={"text": str},
    outputs={"result": str}
)
def my_custom_task(text: str) -> dict:
    """
    自定义任务示例
    
    Args:
        text: 输入文本
        
    Returns:
        包含 result 的字典
    """
    # 在这里编写你的任务逻辑
    result = f"处理结果: {text}"
    
    return {"result": result}
`;

  const handleParse = async () => {
    if (!code.trim()) {
      message.warning('请输入代码');
      return;
    }

    setParsing(true);
    setParseError(null);

    try {
      const parsed = await api.parseCustomFunction(code);
      
      console.log('✅ 解析成功，函数名:', parsed.name, '代码长度:', code.length);
      
      // 更新节点配置
      const updatedData = {
        customCode: code,
        label: parsed.name || '自定义任务',
        nodeType: parsed.nodeType,
        inputs: parsed.inputs.map(inp => ({
          name: inp.name,
          dataType: inp.dataType,
          source: 'user' as const,
          value: ''
        })),
        outputs: parsed.outputs,
        resources: parsed.resources,
        configured: true,
      };
      
      // 构建更新后的节点对象
      const updatedNode = {
        ...node,
        data: {
          ...node.data,
          ...updatedData
        }
      };
      
      console.log('📝 更新节点到 store');
      console.log('   节点ID:', node.id);
      console.log('   代码长度:', code.length);
      console.log('   任务名:', parsed.name);
      
      // 先更新 selectedNode（立即生效）
      selectNode(updatedNode);
      
      // 再更新 store 中的 nodes 数组（稍后生效）
      updateNode(node.id, updatedData);
      
      console.log('✅ 节点更新完成');
      console.log('   updatedNode.data.customCode 长度:', updatedNode.data.customCode?.length);

      message.success(`解析成功！任务名称: ${parsed.name}`);
      
      // 关闭编辑器
      onClose();
    } catch (error: any) {
      console.error('❌ 解析自定义函数失败:', error);
      const errorMsg = error.response?.data?.error || error.message || '解析失败';
      setParseError(errorMsg);
      message.error('解析失败');
    } finally {
      setParsing(false);
    }
  };

  const handleReset = () => {
    setCode(defaultCode);
    setParseError(null);
  };

  return (
    <Modal
      title={
        <Space>
          <CodeOutlined />
          <span>编辑自定义任务代码</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      width={800}
      footer={[
        <Button key="reset" icon={<ReloadOutlined />} onClick={handleReset}>
          重置为示例
        </Button>,
        <Button key="cancel" onClick={onClose}>
          取消
        </Button>,
        <Button 
          key="parse" 
          type="primary" 
          icon={<CheckOutlined />}
          onClick={handleParse}
          loading={parsing}
        >
          解析并配置
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        <Alert
          message="提示"
          description={
            <div>
              <p>请使用 <code>@task</code> 或 <code>@tool</code> 装饰器编写您的函数。</p>
              <ul style={{ marginBottom: 0 }}>
                <li>使用 <code>inputs</code> 参数定义输入参数</li>
                <li>使用 <code>outputs</code> 参数定义输出参数</li>
                <li>函数需要返回一个字典，key 对应 outputs 中定义的名称</li>
              </ul>
            </div>
          }
          type="info"
          showIcon
        />

        {parseError && (
          <Alert
            message="解析错误"
            description={<pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{parseError}</pre>}
            type="error"
            showIcon
            closable
            onClose={() => setParseError(null)}
          />
        )}

        <div style={{ position: 'relative' }}>
          <textarea
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="在这里输入您的任务代码..."
            style={{
              width: '100%',
              height: '450px',
              fontFamily: 'Consolas, Monaco, "Courier New", monospace',
              fontSize: '13px',
              padding: '12px',
              border: '1px solid #d9d9d9',
              borderRadius: '4px',
              resize: 'vertical',
              backgroundColor: '#fafafa',
            }}
          />
          {parsing && (
            <div 
              style={{ 
                position: 'absolute',
                top: 0,
                left: 0,
                right: 0,
                bottom: 0,
                background: 'rgba(255, 255, 255, 0.8)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                borderRadius: '4px'
              }}
            >
              <Spin size="large" tip="正在解析代码..." />
            </div>
          )}
        </div>
      </Space>
    </Modal>
  );
}

