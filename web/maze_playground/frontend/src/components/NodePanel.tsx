import { KeyboardEvent, useEffect, useState } from 'react';
import { Drawer, Form, Input, Select, Button, Typography, Popconfirm, Space, Divider, Tag, Alert, InputNumber } from 'antd';
import { DeleteOutlined, CheckOutlined, CodeOutlined, EditOutlined, PartitionOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import CustomTaskEditor from './CustomTaskEditor';

const { Title } = Typography;

export default function NodePanel() {
  const { selectedNode, selectNode, updateNode, deleteNode, nodes } = useWorkflowStore();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingLabel, setEditingLabel] = useState(false);
  const [labelDraft, setLabelDraft] = useState('');

  const currentNode = selectedNode ? (nodes.find(n => n.id === selectedNode.id) || selectedNode) : null;

  useEffect(() => {
    if (!editingLabel) {
      setLabelDraft(currentNode?.data.label || '');
    }
  }, [currentNode?.id, currentNode?.data.label, editingLabel]);

  if (!currentNode) {
    return null;
  }

  const handleEditorClose = () => {
    setEditorOpen(false);
  };

  const handleClose = () => {
    selectNode(null);
  };

  const getAvailableTasks = () => {
    return nodes.filter(n => n.id !== currentNode.id);
  };

  const getTaskOutputs = (taskId: string) => {
    const task = nodes.find(n => n.id === taskId);
    return task?.data.outputs || [];
  };

  const isCustomTask = currentNode.data.category === 'custom';
  const isWorkspaceTask = currentNode.data.category === 'workspace';
  const isAgentNode = currentNode.data.category === 'agent';
  const isEditableTask = isCustomTask || isWorkspaceTask;
  const isConfigured = currentNode.data.configured;

  const commitLabel = () => {
    const nextLabel = labelDraft.trim() || currentNode.data.label;
    setEditingLabel(false);
    setLabelDraft(nextLabel);

    if (nextLabel === currentNode.data.label) {
      return;
    }

    const updatedNode = {
      ...currentNode,
      data: {
        ...currentNode.data,
        label: nextLabel,
      },
    };

    updateNode(currentNode.id, { label: nextLabel });
    selectNode(updatedNode);
  };

  const cancelLabelEdit = () => {
    setLabelDraft(currentNode.data.label);
    setEditingLabel(false);
  };

  const handleLabelKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.currentTarget.blur();
    }
    if (event.key === 'Escape') {
      cancelLabelEdit();
    }
  };

  return (
    <>
      <Drawer
        title={
          <Space>
            {editingLabel ? (
              <Input
                autoFocus
                size="small"
                value={labelDraft}
                onChange={(event) => setLabelDraft(event.target.value)}
                onBlur={commitLabel}
                onKeyDown={handleLabelKeyDown}
                style={{ width: '240px' }}
              />
            ) : (
              <span
                onClick={() => setEditingLabel(true)}
                title="Click to rename task"
                style={{ cursor: 'text' }}
              >
                {currentNode.data.label}
              </span>
            )}
            {isCustomTask && <Tag color="purple">custom</Tag>}
            {isWorkspaceTask && <Tag color="purple">workspace</Tag>}
            {isAgentNode && <Tag color="purple">agent</Tag>}
          </Space>
        }
        placement="right"
        onClose={handleClose}
        open={!!selectedNode}
        width={450}
      >
        <Form layout="vertical">
          {isEditableTask && !isConfigured && (
            <Alert
              message="Not Configured"
              description="Please write and parse task code first before configuring input/output parameters."
              type="warning"
              showIcon
              style={{ marginBottom: '16px' }}
              action={
                <Button 
                  size="small" 
                  type="primary"
                  icon={<CodeOutlined />}
                  onClick={() => setEditorOpen(true)}
                >
                  Write Code
                </Button>
              }
            />
          )}

          {isEditableTask && isConfigured && (
            <Button
              type="dashed"
              icon={<EditOutlined />}
              block
              onClick={() => setEditorOpen(true)}
              style={{ marginBottom: '16px' }}
            >
              {isWorkspaceTask ? 'Edit Workspace Task' : 'Edit Task Code'}
            </Button>
          )}

          {isAgentNode && (
            <>
              <Alert
                message="ReAct Workflow"
                description="This canvas node starts a DynamicRun-backed ReAct workflow. Runtime steps are visualized on the main canvas and in Runs."
                type="info"
                showIcon
                icon={<PartitionOutlined />}
                style={{ marginBottom: '16px' }}
              />

              <Title level={5}>Agent Settings</Title>
              <Form.Item label="Mode">
                <Select
                  value={currentNode.data.reactMode || 'local'}
                  onChange={(reactMode) => updateNode(currentNode.id, { reactMode })}
                >
                  <Select.Option value="local">Local Demo</Select.Option>
                  <Select.Option value="online">Online LLM</Select.Option>
                </Select>
              </Form.Item>
              <Form.Item label="Prompt">
                <Input.TextArea
                  rows={4}
                  value={currentNode.data.prompt || ''}
                  onChange={(event) => updateNode(currentNode.id, { prompt: event.target.value })}
                />
              </Form.Item>
              <Form.Item label="Max Steps">
                <InputNumber
                  min={3}
                  max={20}
                  value={currentNode.data.maxSteps || 4}
                  onChange={(value) => updateNode(currentNode.id, { maxSteps: Number(value || 4) })}
                  style={{ width: 160 }}
                />
              </Form.Item>
              {currentNode.data.reactMode === 'online' && (
                <Form.Item label="Max Tokens">
                  <InputNumber
                    min={256}
                    max={8192}
                    step={256}
                    value={currentNode.data.maxTokens || 2048}
                    onChange={(value) => updateNode(currentNode.id, { maxTokens: Number(value || 2048) })}
                    style={{ width: 160 }}
                  />
                </Form.Item>
              )}
            </>
          )}

          {isConfigured && (
            <>
              <Title level={5}>Input Parameters</Title>
              
              {currentNode.data.inputs.length === 0 ? (
                <div style={{ padding: '12px', background: '#fafafa', borderRadius: '4px', marginBottom: '16px', color: '#999' }}>
                  This task has no input parameters
                </div>
              ) : (
                currentNode.data.inputs.map((input, idx) => (
                  <div key={idx} style={{ marginBottom: '16px', padding: '12px', background: '#fafafa', borderRadius: '4px' }}>
                    <Form.Item label={`${input.name} (${input.dataType})`}>
                      <Select
                        value={input.source}
                        onChange={(source) => {
                          const newInputs = [...currentNode.data.inputs];
                          newInputs[idx].source = source;
                          updateNode(currentNode.id, { inputs: newInputs });
                        }}
                      >
                        <Select.Option value="user">User Input</Select.Option>
                        <Select.Option value="task">From Task</Select.Option>
                      </Select>
                    </Form.Item>

                    {input.source === 'user' && (
                      <Form.Item>
                        <Input
                          placeholder="Enter value"
                          value={input.value}
                          onChange={(e) => {
                            const newInputs = [...currentNode.data.inputs];
                            newInputs[idx].value = e.target.value;
                            updateNode(currentNode.id, { inputs: newInputs });
                          }}
                        />
                      </Form.Item>
                    )}

                    {input.source === 'task' && (
                      <>
                        <Form.Item label="Select Task">
                          <Select
                            value={input.taskSource?.taskId}
                            onChange={(taskId) => {
                              const newInputs = [...currentNode.data.inputs];
                              newInputs[idx].taskSource = { taskId, outputKey: '' };
                              updateNode(currentNode.id, { inputs: newInputs });
                            }}
                          >
                            {getAvailableTasks().map(task => (
                              <Select.Option key={task.id} value={task.id}>
                                {task.data.label}
                              </Select.Option>
                            ))}
                          </Select>
                        </Form.Item>

                        {input.taskSource?.taskId && (
                          <Form.Item label="Select Output">
                            <Select
                              value={input.taskSource?.outputKey}
                              onChange={(outputKey) => {
                                const newInputs = [...currentNode.data.inputs];
                                if (newInputs[idx].taskSource) {
                                  newInputs[idx].taskSource!.outputKey = outputKey;
                                }
                                updateNode(currentNode.id, { inputs: newInputs });
                              }}
                            >
                              {getTaskOutputs(input.taskSource.taskId).map(output => (
                                <Select.Option key={output.name} value={output.name}>
                                  {output.name} ({output.dataType})
                                </Select.Option>
                              ))}
                            </Select>
                          </Form.Item>
                        )}
                      </>
                    )}
                  </div>
                ))
              )}

              <Title level={5}>Output Parameters</Title>
              {currentNode.data.outputs.length === 0 ? (
                <div style={{ padding: '12px', background: '#fafafa', borderRadius: '4px', marginBottom: '16px', color: '#999' }}>
                  This task has no output parameters
                </div>
              ) : (
                currentNode.data.outputs.map((output, idx) => (
                  <div key={idx} style={{ padding: '8px', background: '#f0f0f0', marginBottom: '8px', borderRadius: '4px' }}>
                    {output.name} ({output.dataType})
                  </div>
                ))
              )}
            </>
          )}

          <Divider />
          
          <Space direction="vertical" style={{ width: '100%' }}>
            {isConfigured && (
              <Button 
                type="primary" 
                icon={<CheckOutlined />}
                block 
                onClick={handleClose}
              >
                Done
              </Button>
            )}
            
            <Popconfirm
              title="Delete Node"
              description="Are you sure you want to delete this node? Related connections will also be removed."
              onConfirm={() => {
                deleteNode(currentNode.id);
                selectNode(null);
              }}
              okText="Delete"
              cancelText="Cancel"
              okButtonProps={{ danger: true }}
            >
              <Button 
                danger 
                icon={<DeleteOutlined />}
                block
              >
                Delete Node
              </Button>
            </Popconfirm>
          </Space>
        </Form>
      </Drawer>

      {isEditableTask && (
        <CustomTaskEditor
          node={currentNode}
          open={editorOpen}
          onClose={handleEditorClose}
        />
      )}
    </>
  );
}
