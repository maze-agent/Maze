import { Card, Typography, Tag, Space, Button } from 'antd';
import { FileImageOutlined, FileTextOutlined, FilePdfOutlined, FileOutlined, DownloadOutlined } from '@ant-design/icons';

const { Text, Link } = Typography;

interface ResultDisplayProps {
  data: any;
}

function isFilePath(value: string): boolean {
  if (typeof value !== 'string') return false;
  
  const fileExtensions = [
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg',
    '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv',
    '.mp3', '.wav', '.ogg', '.flac', '.aac',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.md', '.csv', '.json', '.xml',
    '.zip', '.rar', '.7z', '.tar', '.gz',
  ];
  
  const lowerValue = value.toLowerCase();
  return fileExtensions.some(ext => lowerValue.endsWith(ext)) || 
         lowerValue.includes('temp\\') || 
         lowerValue.includes('temp/') ||
         lowerValue.includes('\\') || 
         lowerValue.includes('/');
}

function getFileIcon(filePath: string) {
  const lowerPath = filePath.toLowerCase();
  
  if (lowerPath.match(/\.(jpg|jpeg|png|gif|bmp|webp|svg)$/)) {
    return <FileImageOutlined style={{ color: '#52c41a', fontSize: '18px' }} />;
  }
  if (lowerPath.match(/\.(pdf)$/)) {
    return <FilePdfOutlined style={{ color: '#ff4d4f', fontSize: '18px' }} />;
  }
  if (lowerPath.match(/\.(txt|md|csv|json|xml)$/)) {
    return <FileTextOutlined style={{ color: '#1890ff', fontSize: '18px' }} />;
  }
  return <FileOutlined style={{ color: '#8c8c8c', fontSize: '18px' }} />;
}

function getFileName(filePath: string): string {
  const parts = filePath.replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || filePath;
}

function isImageFile(filePath: string): boolean {
  const lowerPath = filePath.toLowerCase();
  return /\.(jpg|jpeg|png|gif|bmp|webp|svg)$/.test(lowerPath);
}

function renderValue(value: any, key: string): React.ReactNode {
  if (value === null || value === undefined) {
    return <Tag color="default">null</Tag>;
  }
  
  if (typeof value === 'boolean') {
    return <Tag color={value ? 'success' : 'error'}>{String(value)}</Tag>;
  }
  
  if (typeof value === 'number') {
    return <Tag color="blue">{value}</Tag>;
  }
  
  if (typeof value === 'string') {
    if (isFilePath(value)) {
      return (
        <Card size="small" style={{ marginTop: '8px', background: '#f0f7ff', border: '1px solid #91d5ff' }}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space>
              {getFileIcon(value)}
              <Text strong>{getFileName(value)}</Text>
            </Space>
            
            {isImageFile(value) && (
              <div style={{ 
                marginTop: '8px',
                padding: '8px',
                background: 'white',
                borderRadius: '4px',
                textAlign: 'center'
              }}>
                <img 
                  src={`file:///${value.replace(/\\/g, '/')}`}
                  alt={getFileName(value)}
                  style={{ 
                    maxWidth: '100%', 
                    maxHeight: '200px',
                    objectFit: 'contain',
                    borderRadius: '4px'
                  }}
                  onError={(e) => {
                    (e.target as HTMLImageElement).style.display = 'none';
                  }}
                />
              </div>
            )}
            
            <Text type="secondary" style={{ fontSize: '12px', wordBreak: 'break-all' }}>
              📁 {value}
            </Text>
            
            <Space>
              <Button 
                type="primary" 
                size="small" 
                icon={<DownloadOutlined />}
                onClick={() => {
                  alert(`File saved locally:\n${value}\n\nYou can open it in file explorer`);
                }}
              >
                Open File Location
              </Button>
              {isImageFile(value) && (
                <Button 
                  size="small" 
                  onClick={() => {
                    window.open(`file:///${value.replace(/\\/g, '/')}`, '_blank');
                  }}
                >
                  Open in New Window
                </Button>
              )}
            </Space>
          </Space>
        </Card>
      );
    }
    
    if (value.length > 100) {
      return (
        <div style={{ 
          background: '#f5f5f5', 
          padding: '8px', 
          borderRadius: '4px',
          maxHeight: '200px',
          overflow: 'auto',
          marginTop: '8px'
        }}>
          <Text style={{ whiteSpace: 'pre-wrap', fontSize: '12px' }}>{value}</Text>
        </div>
      );
    }
    return <Text>{value}</Text>;
  }
  
  if (Array.isArray(value)) {
    return (
      <div style={{ marginTop: '8px' }}>
        <Tag color="purple">Array [{value.length}]</Tag>
        <div style={{ marginLeft: '16px', marginTop: '8px' }}>
          {value.map((item, index) => (
            <div key={index} style={{ marginBottom: '8px' }}>
              <Text type="secondary">[{index}]:</Text> {renderValue(item, `${key}[${index}]`)}
            </div>
          ))}
        </div>
      </div>
    );
  }
  
  if (typeof value === 'object') {
    return (
      <div style={{ marginTop: '8px' }}>
        <Tag color="cyan">Object</Tag>
        <div style={{ marginLeft: '16px', marginTop: '8px' }}>
          {Object.entries(value).map(([k, v]) => (
            <div key={k} style={{ marginBottom: '12px' }}>
              <Text strong>{k}:</Text> {renderValue(v, `${key}.${k}`)}
            </div>
          ))}
        </div>
      </div>
    );
  }
  
  return <Text>{String(value)}</Text>;
}

export default function ResultDisplay({ data }: ResultDisplayProps) {
  if (!data) {
    return (
      <div style={{ textAlign: 'center', padding: '40px', color: '#999' }}>
        No results
      </div>
    );
  }
  
  if (typeof data !== 'object' || data === null) {
    return (
      <div style={{ padding: '16px' }}>
        {renderValue(data, 'result')}
      </div>
    );
  }
  
  return (
    <div style={{ padding: '16px' }}>
      {Object.entries(data).map(([key, value]) => (
        <div key={key} style={{ marginBottom: '20px' }}>
          <div style={{ 
            background: '#fafafa', 
            padding: '8px 12px', 
            borderRadius: '4px',
            marginBottom: '8px',
            borderLeft: '3px solid #1890ff'
          }}>
            <Text strong style={{ fontSize: '14px' }}>{key}</Text>
            <Tag color="geekblue" style={{ marginLeft: '8px', fontSize: '11px' }}>
              {Array.isArray(value) ? 'Array' : typeof value}
            </Tag>
          </div>
          <div style={{ marginLeft: '12px' }}>
            {renderValue(value, key)}
          </div>
        </div>
      ))}
    </div>
  );
}
