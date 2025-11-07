import { useState, useEffect } from 'react';
import { Drawer, Form, Input, Select, Button, Typography, Popconfirm, Space, Divider, Tag, Alert } from 'antd';
import { DeleteOutlined, CheckOutlined, CodeOutlined, EditOutlined } from '@ant-design/icons';
import { useWorkflowStore } from '@/stores/workflowStore';
import CustomTaskEditor from './CustomTaskEditor';

const { Title, Text } = Typography;

export default function NodePanel() {
  const { selectedNode, selectNode, updateNode, deleteNode, nodes } = useWorkflowStore();
  const [editorOpen, setEditorOpen] = useState(false);

  // 调试：监控 currentNode 的变化（Hooks 必须在所有条件判断之前）
  useEffect(() => {
    if (selectedNode) {
      const currentNode = nodes.find(n => n.id === selectedNode.id);
      if (currentNode && currentNode.data.category === 'custom') {
        console.log('🎯 NodePanel - currentNode 更新');
        console.log('   节点ID:', currentNode.id);
        console.log('   标签:', currentNode.data.label);
        console.log('   代码长度:', currentNode.data.customCode?.length || 0);
        console.log('   已配置:', currentNode.data.configured);
      }
    }
  }, [selectedNode, nodes]);

  // 早期返回必须在所有 Hooks 之后
  if (!selectedNode) {
    return null;
  }

  // 始终从 nodes 数组中获取最新的节点数据
  const currentNode = nodes.find(n => n.id === selectedNode.id) || selectedNode;

  // 当编辑器关闭时的回调
  const handleEditorClose = () => {
    setEditorOpen(false);
    console.log('🔄 编辑器关闭');
    // 不需要在这里同步，因为 CustomTaskEditor 已经更新了 selectedNode
  };

  const handleClose = () => {
    selectNode(null);
  };

  const getAvailableTasks = () => {
    // 简化：返回所有其他节点
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
            {isCustomTask && <Tag color="purple">自定义</Tag>}
          </Space>
        }
        placement="right"
        onClose={handleClose}
        open={!!selectedNode}
        width={450}
      >
        <Form layout="vertical">
          {/* 自定义任务需要先配置代码 */}
          {isCustomTask && !isConfigured && (
            <Alert
              message="未配置"
              description="请先编写任务代码并解析，才能配置输入输出参数。"
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
                  编写代码
                </Button>
              }
            />
          )}

          {/* 自定义任务已配置时显示编辑按钮 */}
          {isCustomTask && isConfigured && (
            <Button
              type="dashed"
              icon={<EditOutlined />}
              block
              onClick={() => setEditorOpen(true)}
              style={{ marginBottom: '16px' }}
            >
              编辑任务代码
            </Button>
          )}

          {/* 只有已配置的任务才显示输入输出配置 */}
          {isConfigured && (
            <>
              <Title level={5}>输入参数</Title>
              
              {currentNode.data.inputs.length === 0 ? (
                <div style={{ padding: '12px', background: '#fafafa', borderRadius: '4px', marginBottom: '16px', color: '#999' }}>
                  此任务没有输入参数
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
                        <Select.Option value="user">用户输入</Select.Option>
                        <Select.Option value="task">来自任务</Select.Option>
                      </Select>
                    </Form.Item>

                    {input.source === 'user' && (
                      <Form.Item>
                        <Input
                          placeholder="输入值"
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
                        <Form.Item label="选择任务">
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
                          <Form.Item label="选择输出">
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

              <Title level={5}>输出参数</Title>
              {currentNode.data.outputs.length === 0 ? (
                <div style={{ padding: '12px', background: '#fafafa', borderRadius: '4px', marginBottom: '16px', color: '#999' }}>
                  此任务没有输出参数
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
                完成配置
              </Button>
            )}
            
            <Popconfirm
              title="删除节点"
              description="确定要删除这个节点吗？相关的连接也会被删除。"
              onConfirm={() => {
                deleteNode(currentNode.id);
                selectNode(null);
              }}
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
            >
              <Button 
                danger 
                icon={<DeleteOutlined />}
                block
              >
                删除节点
              </Button>
            </Popconfirm>
          </Space>
        </Form>
      </Drawer>

      {/* 自定义任务代码编辑器 */}
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

