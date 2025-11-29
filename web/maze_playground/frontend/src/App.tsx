import { ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import Toolbar from './components/Toolbar';
import BuiltinTasksSidebar from './components/BuiltinTasksSidebar';
import WorkflowCanvas from './components/WorkflowCanvas';
import NodePanel from './components/NodePanel';
import ResultsModal from './components/ResultsModal';

function App() {
  return (
    <ConfigProvider locale={enUS}>
      <div style={{ width: '100vw', height: '100vh', display: 'flex', flexDirection: 'column' }}>
        <Toolbar />
        
        <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          <BuiltinTasksSidebar />
          
          <div style={{ flex: 1, position: 'relative' }}>
            <WorkflowCanvas />
          </div>
          
          <NodePanel />
        </div>
        
        <ResultsModal />
      </div>
    </ConfigProvider>
  );
}

export default App;
