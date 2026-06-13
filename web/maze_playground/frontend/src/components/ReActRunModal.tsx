import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Input, InputNumber, List, Modal, Select, Segmented, Space, Tag, Tooltip, Typography, message } from 'antd';
import { CopyOutlined, DownloadOutlined, ImportOutlined, ThunderboltOutlined } from '@ant-design/icons';
import { api } from '@/api/client';
import type { McpProfileSummary, McpServerConfig } from '@/api/client';
import { useWorkflowStore } from '@/stores/workflowStore';
import { DEFAULT_LLM_SETTINGS, SILICONFLOW_MODELS, loadLlmSettings } from '@/utils/llmSettings';
import { computeReactRunTimeout, defaultReactTaskTimeout, normalizeReactTaskTimeout } from '@/utils/reactRuntime';
import type { WorkspaceSkillMeta } from '@/types/workflow';
import type { LlmSettings } from '@/utils/llmSettings';

const { Text } = Typography;
const { TextArea } = Input;

type ReActMode = 'local' | 'online';
type ExecBackend = 'workspace_sandbox' | 'docker';
const DEFAULT_MCP_JSON = `[
  {
    "name": "local",
    "transport": "stdio",
    "command": "python",
    "args": ["path/to/server.py"]
  }
]`;
const HTTP_MCP_JSON = `[
  {
    "name": "remote",
    "transport": "streamable_http",
    "url": "https://example.com/mcp",
    "headers": {
      "Authorization": "Bearer TOKEN"
    },
    "timeout": 30
  }
]`;
const MCP_TRANSPORTS = new Set(['stdio', 'streamable_http', 'sse']);

function formatProfileTime(value?: string | null): string {
  if (!value) return 'never';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function suggestedCopyName(name?: string): string {
  const base = String(name || 'mcp-profile').trim() || 'mcp-profile';
  return `${base}-copy`;
}

function downloadJsonFile(fileName: string, payload: unknown) {
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

interface McpValidation {
  servers: McpServerConfig[];
  error?: string;
  details: string[];
}

function validateMcpServersJson(source: string): McpValidation {
  let parsed: any;
  try {
    parsed = JSON.parse(source || '[]');
  } catch (error: any) {
    return { servers: [], error: `JSON parse error: ${error.message}`, details: [] };
  }

  if (!Array.isArray(parsed)) {
    return { servers: [], error: 'MCP config must be a JSON array', details: [] };
  }
  if (parsed.length > 8) {
    return { servers: [], error: 'MCP config supports at most 8 servers per run', details: [] };
  }

  const details: string[] = [];
  const servers: McpServerConfig[] = [];
  for (let index = 0; index < parsed.length; index += 1) {
    const server = parsed[index];
    const label = `server #${index + 1}`;
    if (!server || typeof server !== 'object' || Array.isArray(server)) {
      return { servers: [], error: `${label} must be an object`, details };
    }

    const transport = String(server.transport || 'stdio').trim();
    if (!MCP_TRANSPORTS.has(transport)) {
      return { servers: [], error: `${label} has unsupported transport: ${transport}`, details };
    }
    if (server.args !== undefined && !Array.isArray(server.args)) {
      return { servers: [], error: `${label} args must be an array`, details };
    }
    if (server.env !== undefined && (!server.env || typeof server.env !== 'object' || Array.isArray(server.env))) {
      return { servers: [], error: `${label} env must be an object`, details };
    }
    if (server.headers !== undefined && (!server.headers || typeof server.headers !== 'object' || Array.isArray(server.headers))) {
      return { servers: [], error: `${label} headers must be an object`, details };
    }
    if (server.timeout !== undefined) {
      const timeout = Number(server.timeout);
      if (!Number.isFinite(timeout) || timeout < 1 || timeout > 300) {
        return { servers: [], error: `${label} timeout must be a number from 1 to 300`, details };
      }
    }
    if (transport === 'stdio' && !String(server.command || '').trim()) {
      return { servers: [], error: `${label} stdio transport requires command`, details };
    }
    if ((transport === 'streamable_http' || transport === 'sse') && !String(server.url || '').trim()) {
      return { servers: [], error: `${label} ${transport} transport requires url`, details };
    }

    const name = String(server.name || `mcp-${index + 1}`);
    servers.push(server as McpServerConfig);
    details.push(`${name}: ${transport}${server.env ? ', env hidden' : ''}${server.headers ? ', headers hidden' : ''}`);
  }

  return { servers, details };
}

function hasRedactedPlaceholder(source: string): boolean {
  return /"<hidden>"/.test(source);
}

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
  const [mcpEnabled, setMcpEnabled] = useState(false);
  const [mcpServersJson, setMcpServersJson] = useState(DEFAULT_MCP_JSON);
  const [mcpProfiles, setMcpProfiles] = useState<McpProfileSummary[]>([]);
  const [selectedMcpProfileName, setSelectedMcpProfileName] = useState<string>();
  const [mcpProfilesLoading, setMcpProfilesLoading] = useState(false);
  const [mcpProfileSaving, setMcpProfileSaving] = useState(false);
  const [mcpProfileDeleting, setMcpProfileDeleting] = useState(false);
  const [mcpProfileCopying, setMcpProfileCopying] = useState(false);
  const [mcpProfileExporting, setMcpProfileExporting] = useState(false);
  const [mcpProfileImporting, setMcpProfileImporting] = useState(false);
  const [mcpProfileNameInput, setMcpProfileNameInput] = useState('local-tools');
  const [mcpProfileDescriptionInput, setMcpProfileDescriptionInput] = useState('');
  const [mcpProfileImportJson, setMcpProfileImportJson] = useState('');
  const [mcpTesting, setMcpTesting] = useState(false);
  const [mcpDiscovery, setMcpDiscovery] = useState<{
    servers: Array<Record<string, any>>;
    tools: Array<Record<string, any>>;
    toolCount: number;
    profileName?: string;
  } | null>(null);
  const [mcpDiscoveryError, setMcpDiscoveryError] = useState('');
  const [running, setRunning] = useState(false);
  const mcpValidation = useMemo(
    () => (mcpEnabled ? validateMcpServersJson(mcpServersJson) : { servers: [], details: [] }),
    [mcpEnabled, mcpServersJson],
  );
  const selectedMcpProfile = useMemo(
    () => mcpProfiles.find((profile) => profile.name === selectedMcpProfileName),
    [mcpProfiles, selectedMcpProfileName],
  );

  const loadMcpProfiles = async () => {
    setMcpProfilesLoading(true);
    try {
      const result = await api.listMcpProfiles({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
      });
      setMcpProfiles(result.profiles || []);
    } catch (error: any) {
      console.error('Failed to load MCP profiles:', error);
      setMcpProfiles([]);
      message.warning(error.response?.data?.error || 'Failed to load MCP profiles');
    } finally {
      setMcpProfilesLoading(false);
    }
  };

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

  useEffect(() => {
    if (!open) return;
    loadMcpProfiles();
  }, [open, workspaceId, workspaceDir]);

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

    let mcpServers: McpServerConfig[] = [];
    let mcpProfileName: string | undefined;
    if (mcpEnabled) {
      if (selectedMcpProfileName) {
        mcpProfileName = selectedMcpProfileName;
      } else if (hasRedactedPlaceholder(mcpServersJson)) {
        message.warning('Replace <hidden> values or select the profile before running');
        return;
      } else if (mcpValidation.error) {
        message.warning(mcpValidation.error);
        return;
      } else {
        mcpServers = mcpValidation.servers;
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
	        permissionAskTimeoutSeconds: 120,
	        mcpServers: mcpProfileName ? undefined : mcpServers,
        mcpProfileName,
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

  const testMcpDiscovery = async () => {
    if (!mcpEnabled) {
      message.warning('Enable MCP JSON first');
      return;
    }
    if (!selectedMcpProfileName && hasRedactedPlaceholder(mcpServersJson)) {
      message.warning('Replace <hidden> values or select the profile before testing');
      return;
    }
    if (!selectedMcpProfileName && mcpValidation.error) {
      message.warning(mcpValidation.error);
      return;
    }

    setMcpTesting(true);
    setMcpDiscovery(null);
    setMcpDiscoveryError('');
    try {
      const result = await api.discoverMcpTools({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        mcpProfileName: selectedMcpProfileName,
        mcpServers: selectedMcpProfileName ? undefined : mcpValidation.servers,
      });
      setMcpDiscovery({
        servers: result.servers || [],
        tools: result.tools || [],
        toolCount: result.toolCount ?? (result.tools || []).length,
        profileName: result.mcpProfileName || selectedMcpProfileName,
      });
      if (selectedMcpProfileName) {
        await loadMcpProfiles();
      }
      message.success(`Discovered ${result.toolCount ?? (result.tools || []).length} MCP tool(s)`);
    } catch (error: any) {
      const messageText = error.response?.data?.error || error.message || 'MCP discovery failed';
      setMcpDiscoveryError(messageText);
      message.error(messageText);
    } finally {
      setMcpTesting(false);
    }
  };

  const selectMcpProfile = (profileName?: string) => {
    setSelectedMcpProfileName(profileName);
    setMcpDiscovery(null);
    setMcpDiscoveryError('');
    if (!profileName) return;
    const profile = mcpProfiles.find((item) => item.name === profileName);
    if (profile) {
      setMcpEnabled(true);
      setMcpProfileNameInput(profile.name);
      setMcpProfileDescriptionInput(profile.description || '');
      setMcpServersJson(JSON.stringify(profile.redactedMcpServers || [], null, 2));
    }
  };

  const updateMcpJson = (value: string) => {
    setMcpServersJson(value);
    setMcpDiscovery(null);
    setMcpDiscoveryError('');
    if (selectedMcpProfileName) {
      setSelectedMcpProfileName(undefined);
    }
  };

  const applyMcpTemplate = (source: string) => {
    setMcpEnabled(true);
    setSelectedMcpProfileName(undefined);
    setMcpProfileDescriptionInput('');
    setMcpDiscovery(null);
    setMcpDiscoveryError('');
    setMcpServersJson(source);
  };

  const saveMcpProfile = async () => {
    if (selectedMcpProfileName) {
      message.warning('Edit the JSON before saving, so hidden values are not written back to the profile');
      return;
    }
    if (hasRedactedPlaceholder(mcpServersJson)) {
      message.warning('Replace <hidden> values before saving this profile');
      return;
    }
    const profileName = mcpProfileNameInput.trim();
    if (!profileName) {
      message.warning('Profile name is required');
      return;
    }
    if (mcpValidation.error) {
      message.warning(mcpValidation.error);
      return;
    }

    setMcpProfileSaving(true);
    try {
      const result = await api.saveMcpProfile({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        name: profileName,
        description: mcpProfileDescriptionInput,
        mcpServers: mcpValidation.servers,
      });
      await loadMcpProfiles();
      setSelectedMcpProfileName(result.profile.name);
      setMcpProfileNameInput(result.profile.name);
      setMcpProfileDescriptionInput(result.profile.description || '');
      setMcpServersJson(JSON.stringify(result.profile.redactedMcpServers || [], null, 2));
      message.success(`Saved MCP profile: ${result.profile.name}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to save MCP profile');
    } finally {
      setMcpProfileSaving(false);
    }
  };

  const deleteMcpProfile = async () => {
    if (!selectedMcpProfileName) {
      message.warning('Select a profile first');
      return;
    }
    setMcpProfileDeleting(true);
    try {
      await api.deleteMcpProfile(selectedMcpProfileName, {
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
      });
      const deletedName = selectedMcpProfileName;
      setSelectedMcpProfileName(undefined);
      setMcpProfileDescriptionInput('');
      await loadMcpProfiles();
      message.success(`Deleted MCP profile: ${deletedName}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to delete MCP profile');
    } finally {
      setMcpProfileDeleting(false);
    }
  };

  const copyMcpProfile = async () => {
    if (!selectedMcpProfileName) {
      message.warning('Select a profile first');
      return;
    }
    const targetName = mcpProfileNameInput.trim() && mcpProfileNameInput.trim() !== selectedMcpProfileName
      ? mcpProfileNameInput.trim()
      : suggestedCopyName(selectedMcpProfileName);
    setMcpProfileCopying(true);
    try {
      const result = await api.copyMcpProfile(selectedMcpProfileName, {
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        name: targetName,
        description: mcpProfileDescriptionInput || `${selectedMcpProfileName} copy`,
      });
      await loadMcpProfiles();
      setSelectedMcpProfileName(result.profile.name);
      setMcpProfileNameInput(result.profile.name);
      setMcpProfileDescriptionInput(result.profile.description || '');
      setMcpServersJson(JSON.stringify(result.profile.redactedMcpServers || [], null, 2));
      message.success(`Copied MCP profile: ${result.profile.name}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to copy MCP profile');
    } finally {
      setMcpProfileCopying(false);
    }
  };

  const exportMcpProfile = async () => {
    if (!selectedMcpProfileName) {
      message.warning('Select a profile first');
      return;
    }
    setMcpProfileExporting(true);
    try {
      const result = await api.exportMcpProfile(selectedMcpProfileName, {
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
      });
      downloadJsonFile(`${selectedMcpProfileName}.mcp-profile.redacted.json`, result.export);
      message.success(`Exported redacted MCP profile: ${selectedMcpProfileName}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to export MCP profile');
    } finally {
      setMcpProfileExporting(false);
    }
  };

  const importMcpProfile = async () => {
    const source = mcpProfileImportJson.trim();
    if (!source) {
      message.warning('Paste a profile JSON bundle before importing');
      return;
    }
    let parsed: any;
    try {
      parsed = JSON.parse(source);
    } catch (error: any) {
      message.warning(`Import JSON parse error: ${error.message}`);
      return;
    }
    const targetName = mcpProfileNameInput.trim() || parsed.name || parsed.profile?.name;
    if (!targetName) {
      message.warning('Profile name is required');
      return;
    }
    setMcpProfileImporting(true);
    try {
      const result = await api.importMcpProfile({
        workspaceId: workspaceId || undefined,
        workspaceDir: workspaceDir || undefined,
        name: targetName,
        description: mcpProfileDescriptionInput || parsed.description || parsed.profile?.description,
        export: parsed,
      });
      await loadMcpProfiles();
      setSelectedMcpProfileName(result.profile.name);
      setMcpProfileNameInput(result.profile.name);
      setMcpProfileDescriptionInput(result.profile.description || '');
      setMcpServersJson(JSON.stringify(result.profile.redactedMcpServers || [], null, 2));
      setMcpProfileImportJson('');
      message.success(`Imported MCP profile: ${result.profile.name}`);
    } catch (error: any) {
      message.error(error.response?.data?.error || 'Failed to import MCP profile');
    } finally {
      setMcpProfileImporting(false);
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
          <Space align="center" style={{ marginBottom: 6 }}>
            <Text strong>MCP Servers</Text>
            <Segmented
              size="small"
              value={mcpEnabled ? 'enabled' : 'disabled'}
              onChange={(value) => {
                const enabled = value === 'enabled';
                setMcpEnabled(enabled);
                if (!enabled) {
                  setSelectedMcpProfileName(undefined);
                  setMcpDiscovery(null);
                  setMcpDiscoveryError('');
                }
              }}
              options={[
                { label: 'Off', value: 'disabled' },
                { label: 'JSON', value: 'enabled' },
              ]}
            />
            {mcpEnabled && (
              <>
                <Button size="small" onClick={() => applyMcpTemplate(DEFAULT_MCP_JSON)}>
                  Stdio
                </Button>
                <Button size="small" onClick={() => applyMcpTemplate(HTTP_MCP_JSON)}>
                  HTTP
                </Button>
                <Button size="small" loading={mcpTesting} onClick={testMcpDiscovery}>
                  Test MCP
                </Button>
              </>
            )}
          </Space>
          {mcpEnabled && (
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
	              <Space.Compact style={{ width: '100%' }}>
	                <Select
	                  allowClear
	                  loading={mcpProfilesLoading}
	                  value={selectedMcpProfileName}
	                  onChange={selectMcpProfile}
	                  placeholder="Select workspace MCP profile"
	                  options={mcpProfiles.map((profile) => ({
	                    label: `${profile.name} (${profile.serverCount} server${profile.serverCount === 1 ? '' : 's'}, ${profile.toolCount || 0} tool${profile.toolCount === 1 ? '' : 's'})`,
	                    value: profile.name,
	                  }))}
	                  style={{ width: '48%' }}
	                />
	                <Input
	                  value={mcpProfileNameInput}
	                  onChange={(event) => setMcpProfileNameInput(event.target.value)}
	                  placeholder="local-tools"
	                  style={{ width: '52%' }}
	                />
	              </Space.Compact>
	              <Space.Compact style={{ width: '100%' }}>
	                <Input
	                  value={mcpProfileDescriptionInput}
	                  onChange={(event) => setMcpProfileDescriptionInput(event.target.value)}
	                  placeholder="Profile description"
	                  style={{ width: '50%' }}
	                />
	                <Tooltip title="Save JSON as profile">
	                  <Button loading={mcpProfileSaving} onClick={saveMcpProfile}>
	                    Save
	                  </Button>
	                </Tooltip>
	                <Tooltip title="Duplicate selected profile">
	                  <Button
	                    icon={<CopyOutlined />}
	                    disabled={!selectedMcpProfileName}
	                    loading={mcpProfileCopying}
	                    onClick={copyMcpProfile}
	                  />
	                </Tooltip>
	                <Tooltip title="Export selected profile without secrets">
	                  <Button
	                    icon={<DownloadOutlined />}
	                    disabled={!selectedMcpProfileName}
	                    loading={mcpProfileExporting}
	                    onClick={exportMcpProfile}
	                  />
	                </Tooltip>
	                <Tooltip title="Import pasted profile JSON">
	                  <Button icon={<ImportOutlined />} loading={mcpProfileImporting} onClick={importMcpProfile} />
	                </Tooltip>
	                <Button danger disabled={!selectedMcpProfileName} loading={mcpProfileDeleting} onClick={deleteMcpProfile}>
	                  Delete
	                </Button>
	              </Space.Compact>
	              <TextArea
	                value={mcpProfileImportJson}
	                onChange={(event) => setMcpProfileImportJson(event.target.value)}
	                rows={2}
	                spellCheck={false}
	                placeholder="Paste redacted/non-secret profile export JSON here, then click import"
	                style={{ fontFamily: 'monospace' }}
	              />
	              <Alert
	                type="info"
	                showIcon
	                message={selectedMcpProfileName ? `Using workspace profile: ${selectedMcpProfileName}` : 'AgentMCPServerConfig[]'}
	                description={
	                  selectedMcpProfileName
	                    ? `Redacted preview. Last test: ${selectedMcpProfile?.lastTest?.status || 'never'} at ${formatProfileTime(selectedMcpProfile?.lastTest?.testedAt)}. Test MCP and Run ReAct load the full profile server-side.`
	                    : 'stdio requires command. streamable_http and sse require url. env and headers are passed to runtime; use ${ENV_NAME} to keep secrets in backend environment variables.'
	                }
	              />
              <TextArea
                value={mcpServersJson}
                onChange={(event) => updateMcpJson(event.target.value)}
                rows={8}
                spellCheck={false}
                status={!selectedMcpProfileName && mcpValidation.error ? 'error' : undefined}
                style={{ fontFamily: 'monospace' }}
              />
              {!selectedMcpProfileName && mcpValidation.error ? (
                <Alert type="error" showIcon message={mcpValidation.error} />
              ) : (
	                <Space wrap>
	                  <Tag color="magenta">
	                    {selectedMcpProfile ? selectedMcpProfile.serverCount : mcpValidation.servers.length} MCP server(s)
	                  </Tag>
	                  {selectedMcpProfile && (
	                    <Tag color={selectedMcpProfile.lastTest?.status === 'ok' ? 'green' : selectedMcpProfile.lastTest?.status === 'failed' ? 'red' : 'default'}>
	                      last test {selectedMcpProfile.lastTest?.status || 'never'}
	                    </Tag>
	                  )}
	                  {selectedMcpProfile?.lastTest?.toolCount !== undefined && selectedMcpProfile?.lastTest?.toolCount !== null && (
	                    <Tag color="blue">{selectedMcpProfile.lastTest.toolCount} tested tool(s)</Tag>
	                  )}
	                  {selectedMcpProfile?.usesEnvRefs && (
	                    <Tag color="gold">{selectedMcpProfile.envRefCount || 0} env ref(s)</Tag>
	                  )}
	                  {(selectedMcpProfile ? selectedMcpProfile.servers.map((server) => {
	                    const label = `${server.name || 'mcp'}: ${server.transport || 'stdio'}`;
	                    return `${label}${server.has_env ? ', env hidden' : ''}${server.has_headers ? ', headers hidden' : ''}`;
	                  }) : mcpValidation.details).map((detail) => (
	                    <Tag key={detail} color="blue">{detail}</Tag>
	                  ))}
	                </Space>
	              )}
	              {selectedMcpProfile?.lastTest?.status === 'failed' && selectedMcpProfile.lastTest.error && (
	                <Alert
	                  type="error"
	                  showIcon
	                  message="Last MCP profile test failed"
	                  description={selectedMcpProfile.lastTest.error}
	                />
	              )}
              {mcpDiscoveryError && (
                <Alert type="error" showIcon message="MCP discovery failed" description={mcpDiscoveryError} />
              )}
              {mcpDiscovery && (
                <div>
                  <Space wrap style={{ marginBottom: 6 }}>
                    <Tag color="green">{mcpDiscovery.profileName ? 'profile available' : 'discovery ok'}</Tag>
                    {mcpDiscovery.profileName && <Tag color="purple">{mcpDiscovery.profileName}</Tag>}
                    <Tag color="magenta">{mcpDiscovery.servers.length} server(s)</Tag>
                    <Tag color="blue">{mcpDiscovery.toolCount} tool(s)</Tag>
                  </Space>
                  <List
                    size="small"
                    bordered
                    dataSource={mcpDiscovery.tools}
                    locale={{ emptyText: 'No MCP tools discovered' }}
                    renderItem={(tool: any) => (
                      <List.Item>
                        <Space direction="vertical" size={4} style={{ width: '100%' }}>
                          <Space wrap>
                            <Tag color="magenta">{tool.server || 'mcp'}</Tag>
                            <Tag color="blue">{tool.agent_tool || tool.tool || 'tool'}</Tag>
                            {(tool.required_inputs || []).map((input: string) => (
                              <Tag key={`${tool.agent_tool || tool.tool}-${input}`}>{input}</Tag>
                            ))}
                          </Space>
                          {tool.description && <Text type="secondary">{tool.description}</Text>}
                        </Space>
                      </List.Item>
                    )}
                  />
                </div>
              )}
            </Space>
          )}
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
