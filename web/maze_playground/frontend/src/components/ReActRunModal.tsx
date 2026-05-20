import { useEffect, useState } from 'react';
import { Alert, Button, Input, InputNumber, Modal, Select, Segmented, Space, Typography, message } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings } from '@/utils/llmSettings';
import type { LlmSettings } from '@/utils/llmSettings';

const { Text } = Typography;
const { TextArea } = Input;

type ReActMode = 'local' | 'online';

interface ReActRunModalProps {
  open: boolean;
  onClose: () => void;
  onCompleted?: (runId: string) => void;
}

export default function ReActRunModal({ open, onClose, onCompleted }: ReActRunModalProps) {
  const { workspaceDir } = useWorkflowStore();
  const [mode, setMode] = useState<ReActMode>('local');
  const [prompt, setPrompt] = useState('Use the calculator to compute 18 * 7, then give the final answer.');
  const [maxSteps, setMaxSteps] = useState(4);
  const [llmSettings, setLlmSettings] = useState<LlmSettings>(DEFAULT_LLM_SETTINGS);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (open) {
      setLlmSettings(loadLlmSettings());
    }
  }, [open]);

  const runReactWorkflow = async () => {
    const trimmedPrompt = prompt.trim();
    if (!trimmedPrompt) {
      message.warning('Prompt is required');
      return;
    }

    if (mode === 'online') {
      if (!llmSettings.baseUrl.trim()) {
        message.warning('Base URL is required');
        return;
      }
      if (!llmSettings.model.trim()) {
        message.warning('Model is required');
        return;
      }
      if (!llmSettings.apiKey.trim()) {
        message.warning('API key is required for online ReAct runs');
        return;
      }
    }

    setRunning(true);
    try {
      const result = await api.startReactRun({
        mode,
        prompt: trimmedPrompt,
        workspaceDir: workspaceDir || undefined,
        maxSteps,
        timeoutSeconds: mode === 'online' ? 180 : 90,
        taskTimeout: mode === 'online' ? 120 : 60,
        llm: mode === 'online'
          ? {
              ...llmSettings,
              baseUrl: llmSettings.baseUrl.trim(),
              model: llmSettings.model.trim(),
            }
          : undefined,
      });

      message.success(`ReAct run started: ${result.runId.slice(0, 8)}...`);
      onCompleted?.(result.runId);
      onClose();
    } catch (error: any) {
      console.error('Failed to run ReAct workflow:', error);
      const runId = error.response?.data?.runId;
      if (runId) {
        onCompleted?.(runId);
      }
      message.error(error.response?.data?.error || 'Failed to run ReAct workflow');
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      title={
        <Space>
          <ThunderboltOutlined />
          Run ReAct Workflow
        </Space>
      }
      open={open}
      onCancel={onClose}
      width={640}
      footer={[
        <Button key="cancel" onClick={onClose} disabled={running}>
          Cancel
        </Button>,
        <Button key="run" type="primary" icon={<ThunderboltOutlined />} loading={running} onClick={runReactWorkflow}>
          Run ReAct
        </Button>,
      ]}
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Segmented
          value={mode}
          onChange={(value) => setMode(value as ReActMode)}
          options={[
            { label: 'Local Demo', value: 'local' },
            { label: 'Online LLM', value: 'online' },
          ]}
        />

        <Alert
          type="info"
          showIcon
          message={mode === 'local' ? 'Local demo mode' : 'Online LLM mode'}
          description={
            mode === 'local'
              ? 'Runs a deterministic ReAct loop that demonstrates repair, tool execution, and final answer tracing without an API key.'
              : 'Runs an OpenAI-compatible ReAct decision task. The API key is sent to the local Playground backend for this run and is not written into Maze task metadata.'
          }
        />

        <div>
          <Text strong>Prompt</Text>
          <TextArea
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            rows={4}
            style={{ marginTop: 6 }}
          />
        </div>

        <div>
          <Text strong>Max Steps</Text>
          <InputNumber
            min={3}
            max={20}
            value={maxSteps}
            onChange={(value) => setMaxSteps(Number(value || 4))}
            style={{ display: 'block', width: 160, marginTop: 6 }}
          />
        </div>

        {mode === 'online' && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <div>
              <Text strong>Base URL</Text>
              <Input
                value={llmSettings.baseUrl}
                onChange={(event) => setLlmSettings((current) => ({ ...current, baseUrl: event.target.value }))}
                placeholder="https://api.siliconflow.cn/v1"
                style={{ marginTop: 6 }}
              />
            </div>

            <div>
              <Text strong>API Key</Text>
              <Input.Password
                value={llmSettings.apiKey}
                onChange={(event) => setLlmSettings((current) => ({ ...current, apiKey: event.target.value }))}
                placeholder="sk-..."
                style={{ marginTop: 6 }}
              />
            </div>

            <div>
              <Text strong>Model</Text>
              <Select
                showSearch
                value={llmSettings.model}
                onChange={(model) => setLlmSettings((current) => ({ ...current, model }))}
                options={SILICONFLOW_MODELS.map((model) => ({ label: model, value: model }))}
                style={{ width: '100%', marginTop: 6 }}
              />
            </div>
          </Space>
        )}
      </Space>
    </Modal>
  );
}
