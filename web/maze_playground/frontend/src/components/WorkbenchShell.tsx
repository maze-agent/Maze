import { ReactNode, useState } from 'react';
import { Button, Tooltip } from 'antd';
import { DoubleLeftOutlined, DoubleRightOutlined } from '@ant-design/icons';
import TaskInspector from '@/components/workbench/TaskInspector';

interface WorkbenchShellProps {
  topBar: ReactNode;
  leftSidebar: ReactNode;
  canvas: ReactNode;
  runsInspector: ReactNode;
  clusterDrawer: ReactNode;
}

export default function WorkbenchShell({
  topBar,
  leftSidebar,
  canvas,
  runsInspector,
  clusterDrawer,
}: WorkbenchShellProps) {
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);

  return (
    <div className="workbench-shell">
      <div className="workbench-topbar" data-workbench-region="TopBar">
        {topBar}
      </div>

      <div className="workbench-body" data-inspector-collapsed={inspectorCollapsed}>
        <aside className="workbench-left-sidebar" data-workbench-region="LeftSidebar">
          {leftSidebar}
        </aside>

        <main className="workbench-main">
          <section className="workbench-canvas" data-workbench-region="WorkflowCanvas">
            {canvas}
          </section>
        </main>

        <aside
          className="workbench-task-inspector"
          data-workbench-region="TaskInspector"
          data-inspector-collapsed={inspectorCollapsed}
        >
          <Tooltip title={inspectorCollapsed ? 'Open inspector' : 'Close inspector'} placement="left">
            <Button
              className="workbench-inspector-collapse-button"
              size="small"
              icon={inspectorCollapsed ? <DoubleLeftOutlined /> : <DoubleRightOutlined />}
              aria-label={inspectorCollapsed ? 'Open inspector' : 'Close inspector'}
              onClick={() => setInspectorCollapsed((value) => !value)}
            />
          </Tooltip>
          {!inspectorCollapsed && <TaskInspector />}
        </aside>
      </div>

      {runsInspector}
      {clusterDrawer}
    </div>
  );
}
