import type { RuntimeArtifact } from './types';

type ArtifactsTableProps = {
  artifacts: RuntimeArtifact[];
  onSelectTask?: (taskId: string) => void;
};

export default function ArtifactsTable({ artifacts, onSelectTask }: ArtifactsTableProps) {
  if (artifacts.length === 0) {
    return <div className="runtime-empty-state">No artifacts match the current filters.</div>;
  }

  return (
    <div className="runtime-table-shell" role="table" aria-label="Runtime artifacts">
      <div className="runtime-table-header runtime-artifacts-grid" role="row">
        <span>Name</span>
        <span>Task</span>
        <span>Type</span>
        <span>Size</span>
        <span>Created</span>
      </div>
      {artifacts.map((artifact) => (
        <button
          key={artifact.id}
          type="button"
          className="runtime-table-row runtime-artifacts-grid"
          title={artifact.uri || artifact.name}
          onClick={() => onSelectTask?.(artifact.taskId)}
        >
          <strong>{artifact.name}</strong>
          <span>{artifact.taskName}</span>
          <span className="runtime-artifact-type">{artifact.type}</span>
          <span>{artifact.size || '-'}</span>
          <span>{artifact.createdAt}</span>
        </button>
      ))}
    </div>
  );
}
