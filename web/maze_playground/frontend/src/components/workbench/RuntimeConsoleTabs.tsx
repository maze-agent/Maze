import type { RuntimeConsoleTab } from './types';

const tabLabels: Record<RuntimeConsoleTab, string> = {
  timeline: 'Timeline',
  events: 'Events',
  logs: 'Logs',
  artifacts: 'Artifacts',
};

const tabOrder: RuntimeConsoleTab[] = ['timeline', 'events', 'logs', 'artifacts'];

type RuntimeConsoleTabsProps = {
  activeTab: RuntimeConsoleTab;
  counts: Record<RuntimeConsoleTab, number>;
  onTabChange: (tab: RuntimeConsoleTab) => void;
};

export default function RuntimeConsoleTabs({ activeTab, counts, onTabChange }: RuntimeConsoleTabsProps) {
  return (
    <nav className="runtime-console-tabs" aria-label="Runtime console tabs">
      {tabOrder.map((tab) => (
        <button
          key={tab}
          type="button"
          className={activeTab === tab ? 'is-active' : ''}
          onClick={() => onTabChange(tab)}
        >
          <span>{tabLabels[tab]}</span>
          <strong>{counts[tab]}</strong>
        </button>
      ))}
    </nav>
  );
}
