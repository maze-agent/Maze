import { useState, useEffect } from 'react';
import { Drawer, Form, Input, Select, Button, Typography, Popconfirm, Space, Divider, Tag, Alert } from 'antd';
import { DeleteOutlined, CheckOutlined, CodeOutlined, EditOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import CustomTaskEditor from './CustomTaskEditor';

const { Title, Text } = Typography;

export default function NodePanel() {
  const { selectedNode, selectNode, updateNode, deleteNode, nodes } = useWorkflowStore();
  const [editorOpen, setEditorOpen] = useState(false);

  useEffect(() => {
    if (selectedNode) {
      const currentNode = nodes.find(n => n.id === selectedNode.id);
    }
  }, [selectedNode, nodes]);

  if (!selectedNode) {
    return null;
  }

  const currentNode = nodes.find(n => n.id === selectedNode.id) || selectedNode;

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
  const isConfigured = currentNode.data.configured;

  return (
    <>
      <Drawer
        title={
          <Space>
            <span>{currentNode.data.label}</span>
            {isCustomTask && <Tag color="purple">custom</Tag>}
          </Space>
        }
        placement="right"
        onClose={handleClose}
        open={!!selectedNode}
        width={450}
      >
        <Form layout="vertical">
          {isCustomTask && !isConfigured && (
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

          {isCustomTask && isConfigured && (
            <Button
              type="dashed"
              icon={<EditOutlined />}
              block
              onClick={() => setEditorOpen(true)}
              style={{ marginBottom: '16px' }}
            >
              Edit Task Code
            </Button>
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

      {isCustomTask && (
        <CustomTaskEditor
          node={currentNode}
          open={editorOpen}
          onClose={handleEditorClose}
        />
      )}
    </>
  );
}
