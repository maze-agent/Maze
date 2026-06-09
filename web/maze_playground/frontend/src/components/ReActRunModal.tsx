import { useEffect, useState } from 'react';
import { Alert, Button, Input, InputNumber, Modal, Select, Segmented, Space, Typography, message } from 'antd';
import { ThunderboltOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings } from '@/utils/llmSettings';
import { computeReactRunTimeout, defaultReactTaskTimeout, normalizeReactTaskTimeout } from '@/utils/reactRuntime';
import type { WorkspaceSkillMeta } from '@/types/workflow';
import type { LlmSettings } from '@/utils/llmSettings';

const { Text } = Typography;
const { TextArea } = Input;

type ReActMode = 'local' | 'online';
type ExecBackend = 'workspace_sandbox' | 'docker';

interface ReActRunModalProps {
  open: boolean;
  onClose: () => void;
  onCompleted?: (runId: string) => void;
}

export default function ReActRunModal({ open, onClose, onCompleted }: ReActRunModalProps) {
  const { workspaceId, workspaceDir } = useWorkflowStore();
  const [mode, setMode] = useState<ReActMode>('local');
  const [prompt, setPrompt] = useState('Use the calculator to compute 18 * 7, then give the final answer.');
  const [maxSteps, setMaxSteps] = useState(4);
  const [maxTokens, setMaxTokens] = useState(2048);
  const [taskTimeout, setTaskTimeout] = useState<number>(defaultReactTaskTimeout('local'));
  const [llmSettings, setLlmSettings] = useState<LlmSettings>(DEFAULT_LLM_SETTINGS);
  const [availableSkills, setAvailableSkills] = useState<WorkspaceSkillMeta[]>([]);
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [execBackend, setExecBackend] = useState<ExecBackend>('workspace_sandbox');
  const [dockerAvailable, setDockerAvailable] = useState(false);
  const [dockerReason, setDockerReason] = useState('');
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (open) {
      setLlmSettings(loadLlmSettings());
    }
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;

    let canceled = false;
    setSkillsLoading(true);
    api.getWorkspaceSkills(workspaceDir || undefined)
      .then((result) => {
        if (canceled) return;
        const skills = result.skills || [];
        const skillNames = new Set(skills.map((skill) => skill.name));
        setAvailableSkills(skills);
        setSelectedSkills((current) => current.filter((name) => skillNames.has(name)));
      })
      .catch((error) => {
        if (canceled) return;
        console.error('Failed to load workspace skills:', error);
        setAvailableSkills([]);
        setSelectedSkills([]);
        message.warning(error.response?.data?.error || 'Failed to load workspace skills');
      })
      .finally(() => {
        if (!canceled) {
          setSkillsLoading(false);
        }
      });

    return () => {
      canceled = true;
    };
  }, [open, workspaceDir]);

  useEffect(() => {
    if (!open) return undefined;

    let canceled = false;
    api.getClusterResources()
      .then((result) => {
        if (canceled) return;
        const nodes = result.cluster?.nodes || [];
        const dockerNode = nodes.find((node) => node.alive && node.capabilities?.docker_sandbox);
        setDockerAvailable(Boolean(dockerNode));
        const reason = nodes
          .map((node) => node.capabilities?.docker_reason)
          .find((value) => typeof value === 'string' && value.length > 0);
        setDockerReason(reason || '');
        if (!dockerNode) {
          setExecBackend('workspace_sandbox');
        }
      })
      .catch(() => {
        if (canceled) return;
        setDockerAvailable(false);
        setDockerReason('Cluster capability unavailable');
        setExecBackend('workspace_sandbox');
      });

    return () => {
      canceled = true;
    };
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
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        maxSteps,
        maxTokens,
        timeoutSeconds: computeReactRunTimeout(maxSteps, taskTimeout),
        taskTimeout,
        skills: selectedSkills,
        execBackend,
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
      width={720}
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
          onChange={(value) => {
            const nextMode = value as ReActMode;
            setMode(nextMode);
            setTaskTimeout(defaultReactTaskTimeout(nextMode));
          }}
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
          <Text strong>Skills</Text>
          <Select
            mode="multiple"
            allowClear
            loading={skillsLoading}
            value={selectedSkills}
            onChange={setSelectedSkills}
            optionLabelProp="value"
            placeholder="No skills selected"
            notFoundContent={skillsLoading ? 'Loading...' : 'No workspace skills'}
            options={availableSkills.map((skill) => ({
              label: skill.description ? `${skill.name} - ${skill.description}` : skill.name,
              value: skill.name,
            }))}
            style={{ width: '100%', marginTop: 6 }}
          />
        </div>

        <div>
          <Text strong>Sandbox</Text>
          <Select
            value={execBackend}
            onChange={(value) => setExecBackend(value as ExecBackend)}
            options={[
              { label: 'Workspace Sandbox', value: 'workspace_sandbox' },
              {
                label: dockerAvailable ? 'Docker Sandbox' : `Docker Sandbox${dockerReason ? ` - ${dockerReason}` : ''}`,
                value: 'docker',
                disabled: !dockerAvailable,
              },
            ]}
            style={{ width: '100%', display: 'block', marginTop: 6 }}
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

        <div>
          <Text strong>Task Timeout</Text>
          <InputNumber
            min={10}
            max={600}
            step={10}
            addonAfter="s"
            value={taskTimeout}
            onChange={(value) => setTaskTimeout(normalizeReactTaskTimeout(value, mode))}
            style={{ display: 'block', width: 180, marginTop: 6 }}
          />
        </div>

        {mode === 'online' && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <div>
              <Text strong>Max Tokens</Text>
              <InputNumber
                min={256}
                max={8192}
                step={256}
                value={maxTokens}
                onChange={(value) => setMaxTokens(Number(value || 2048))}
                style={{ display: 'block', width: 160, marginTop: 6 }}
              />
            </div>

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
