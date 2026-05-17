export interface LlmSettings {
  baseUrl: string;
  apiKey: string;
  model: string;
}

export const SILICONFLOW_MODELS = [
  'zai-org/GLM-4.6',
  'deepseek-ai/DeepSeek-V3',
  'deepseek-ai/DeepSeek-V3.2',
  'deepseek-ai/DeepSeek-R1',
  'deepseek-ai/DeepSeek-R1-Distill-Qwen-32B',
  'Qwen/Qwen3-32B',
  'Qwen/Qwen3-8B',
];

export const DEFAULT_LLM_SETTINGS: LlmSettings = {
  baseUrl: 'https://api.siliconflow.cn/v1',
  apiKey: '',
  model: 'zai-org/GLM-4.6',
};

const STORAGE_KEY = 'maze.playground.llmSettings.v1';

export function loadLlmSettings(): LlmSettings {
  if (typeof window === 'undefined') {
    return DEFAULT_LLM_SETTINGS;
  }

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return DEFAULT_LLM_SETTINGS;
    }
    const parsed = JSON.parse(raw);
    return {
      baseUrl: String(parsed.baseUrl || DEFAULT_LLM_SETTINGS.baseUrl),
      apiKey: String(parsed.apiKey || ''),
      model: String(parsed.model || DEFAULT_LLM_SETTINGS.model),
    };
  } catch {
    return DEFAULT_LLM_SETTINGS;
  }
}

export function saveLlmSettings(settings: LlmSettings) {
  if (typeof window === 'undefined') {
    return;
  }

  window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
    baseUrl: settings.baseUrl.trim(),
    apiKey: settings.apiKey,
    model: settings.model.trim(),
  }));
}
