import { useState, useEffect, useRef } from 'react';
import { Modal, Button, message, Alert, Spin, Space, Typography } from 'antd';
import { CodeOutlined, CheckOutlined, ReloadOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import { api } from '@/api/client';
import type { WorkflowNode } from '@/types/workflow';

const { Text } = Typography;

interface CustomTaskEditorProps {
  node: WorkflowNode;
  open: boolean;
  onClose: () => void;
}

export default function CustomTaskEditor({ node, open, onClose }: CustomTaskEditorProps) {
  const { updateNode, selectNode, workspaceDir, setWorkspaceDir, setWorkspaceTasks } = useWorkflowStore();
  const [code, setCode] = useState('');
  const [parsing, setParsing] = useState(false);
  const [parseError, setParseError] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<'idle' | 'unsaved' | 'saving' | 'saved' | 'failed'>('idle');
  const lastSavedCodeRef = useRef('');

  const isWorkspaceTask = node.data.category === 'workspace';
  const targetWorkspaceDir = node.data.workspaceDir || workspaceDir;
  const targetTaskPath = node.data.taskPath;

  useEffect(() => {
    if (open) {
      const currentCode = node.data.customCode || '';
      setCode(currentCode);
      setParseError(null);
      lastSavedCodeRef.current = currentCode;
      setSaveState(isWorkspaceTask ? 'saved' : 'idle');
    }
  }, [open, node.id, node.data.customCode, isWorkspaceTask]);

  useEffect(() => {
    if (!open || !isWorkspaceTask || !targetWorkspaceDir || !targetTaskPath) {
      return;
    }

    if (code === lastSavedCodeRef.current) {
      return;
    }

    setSaveState('unsaved');
    const timer = window.setTimeout(async () => {
      try {
        setSaveState('saving');
        const saved = await api.saveWorkspaceTask({
          workspaceDir: targetWorkspaceDir,
          relativePath: targetTaskPath,
          code,
          parse: false,
        });

        lastSavedCodeRef.current = code;
        updateNode(node.id, {
          customCode: code,
          workspaceDir: saved.workspaceDir,
          taskPath: saved.relativePath,
        });
        setSaveState('saved');
      } catch (error) {
        console.error('Failed to autosave workspace task:', error);
        setSaveState('failed');
      }
    }, 800);

    return () => window.clearTimeout(timer);
  }, [code, open, isWorkspaceTask, targetWorkspaceDir, targetTaskPath, node.id, updateNode]);

  const defaultCode = `from maze import task

@task(
    inputs=["text"],
    outputs=["result"],
    resources={"cpu": 1, "cpu_mem": 0, "gpu": 0, "gpu_mem": 0}
)
def my_custom_task(params):
    """
    Custom task example
    
    Args:
        params: Dictionary containing input parameters
        
    Returns:
        Dictionary with output values
    """
    # Get input parameters from params
    text = params.get("text")
    
    # Write your task logic here
    result = f"Processed: {text}"
    
    # Return dictionary with keys matching outputs
    return {"result": result}
`;

  const handleParse = async () => {
    if (!code.trim()) {
      message.warning('Please enter code');
      return;
    }

    setParsing(true);
    setParseError(null);

    try {
      const parsedResult = isWorkspaceTask && targetWorkspaceDir && targetTaskPath
        ? await api.saveWorkspaceTask({
            workspaceDir: targetWorkspaceDir,
            relativePath: targetTaskPath,
            code,
            parse: true,
          })
        : await api.parseCustomFunction(code);

      const parsed = parsedResult.task || parsedResult;
      
      const updatedData = {
        category: isWorkspaceTask ? 'workspace' as const : node.data.category,
        customCode: code,
        label: parsed.displayName || parsed.name || 'Custom Task',
        nodeType: 'task' as const,
        workspaceDir: parsed.workspaceDir || targetWorkspaceDir,
        taskPath: parsed.relativePath || targetTaskPath,
        functionName: parsed.functionName || parsed.name,
        inputs: parsed.inputs.map((inp: { name: string; dataType: string }) => ({
          name: inp.name,
          dataType: inp.dataType,
          source: 'user' as const,
          value: ''
        })),
        outputs: parsed.outputs,
        resources: parsed.resources,
        configured: true,
      };
      
      const updatedNode = {
        ...node,
        data: {
          ...node.data,
          ...updatedData
        }
      };
      
      selectNode(updatedNode);
      updateNode(node.id, updatedData);

      if (isWorkspaceTask && (parsed.workspaceDir || targetWorkspaceDir)) {
        const refreshed = await api.getWorkspaceTasks(parsed.workspaceDir || targetWorkspaceDir);
        setWorkspaceDir(refreshed.workspaceDir);
        setWorkspaceTasks(refreshed.tasks || []);
        lastSavedCodeRef.current = code;
        setSaveState('saved');
      }

      message.success(isWorkspaceTask ? `Saved and parsed: ${parsed.name}` : `Parse successful! Task name: ${parsed.name}`);
      onClose();
    } catch (error: any) {
      console.error('Failed to parse custom function:', error);
      const errorMsg = error.response?.data?.error || error.message || 'Parse failed';
      setParseError(errorMsg);
      message.error('Parse failed');
    } finally {
      setParsing(false);
    }
  };

  const handleReset = () => {
    setCode(defaultCode);
    setParseError(null);
  };

  const renderSaveState = () => {
    if (!isWorkspaceTask) {
      return null;
    }

    const labelMap = {
      idle: '',
      unsaved: 'Unsaved changes',
      saving: 'Saving...',
      saved: 'Saved',
      failed: 'Autosave failed',
    };

    const colorMap = {
      idle: undefined,
      unsaved: 'secondary' as const,
      saving: 'secondary' as const,
      saved: 'success' as const,
      failed: 'danger' as const,
    };

    return (
      <Text type={colorMap[saveState]} style={{ fontSize: '12px' }}>
        {labelMap[saveState]}
      </Text>
    );
  };

  return (
    <Modal
      title={
        <Space>
          <CodeOutlined />
          <span>{isWorkspaceTask ? 'Edit Workspace Task' : 'Edit Custom Task Code'}</span>
        </Space>
      }
      open={open}
      onCancel={onClose}
      width={800}
      footer={[
        <Button key="reset" icon={<ReloadOutlined />} onClick={handleReset}>
          Reset to Example
        </Button>,
        <Button key="cancel" onClick={onClose}>
          Cancel
        </Button>,
        <Button 
          key="parse" 
          type="primary" 
          icon={<CheckOutlined />}
          onClick={handleParse}
          loading={parsing}
        >
          {isWorkspaceTask ? 'Parse & Save' : 'Parse & Configure'}
        </Button>,
      ]}
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {isWorkspaceTask && (
          <Alert
            message={targetTaskPath || 'Workspace task'}
            description={
              <Space direction="vertical" size={2}>
                <Text type="secondary">{targetWorkspaceDir}</Text>
                {renderSaveState()}
              </Space>
            }
            type="info"
            showIcon
          />
        )}

        <Alert
          message="Guidelines"
          description={
            <div>
              <p>Use the <code>@task</code> decorator (<code>from maze import task</code>):</p>
              <ul style={{ marginBottom: 0 }}>
                <li><code>inputs</code>: List of input parameter names, e.g. <code>["text", "count"]</code></li>
                <li><code>outputs</code>: List of output parameter names, e.g. <code>["result"]</code></li>
                <li><code>resources</code> (optional): Resource config, e.g. <code>{`{"cpu": 1, "gpu": 0}`}</code></li>
                <li>Function signature: <code>def my_task(params):</code>, use <code>params.get("name")</code> to get inputs</li>
                <li>Return: Dictionary with keys matching outputs</li>
              </ul>
            </div>
          }
          type="info"
          showIcon
        />

        {parseError && (
          <Alert
            message="Parse Error"
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
            placeholder="Enter your task code here..."
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
              <Spin size="large" />
            </div>
          )}
        </div>
      </Space>
    </Modal>
  );
}
