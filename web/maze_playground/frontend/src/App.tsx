import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import NodePanel from './components/NodePanel';
import ResultsModal from './components/ResultsModal';

function App() {
  return (
    <ConfigProvider locale={zhCN}>
      <div style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
        {/* 顶部工具栏 */}
        <Toolbar />
        
        {/* 主内容区 */}
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          {/* 左侧任务列表 */}
          <BuiltinTasksSidebar />
          
          {/* 中间工作流画布 */}
          <div style={{ flex: 1, position: 'relative' }}>
            <WorkflowCanvas />
          </div>
          
          {/* 右侧属性面板 */}
          <NodePanel />
        </div>
        
        {/* 结果模态框 */}
        <ResultsModal />
      </div>
    </ConfigProvider>
  );
}

export default App;

