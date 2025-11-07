import { Card, Typography, Tag, Space, Button } from 'antd';
import { FileImageOutlined, FileTextOutlined, FilePdfOutlined, FileOutlined, DownloadOutlined } from '@ant-design/icons';

const { Text, Link } = Typography;

interface ResultDisplayProps {
  data: any;
}

// 检测是否是文件路径
function isFilePath(value: string): boolean {
  if (typeof value !== 'string') return false;
  
  // 检测常见文件扩展名
  const fileExtensions = [
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg',  // 图片
    '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv',           // 视频
    '.mp3', '.wav', '.ogg', '.flac', '.aac',                  // 音频
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', // 文档
    '.txt', '.md', '.csv', '.json', '.xml',                   // 文本
    '.zip', '.rar', '.7z', '.tar', '.gz',                     // 压缩包
  ];
  
  const lowerValue = value.toLowerCase();
  return fileExtensions.some(ext => lowerValue.endsWith(ext)) || 
         lowerValue.includes('temp\\') || 
         lowerValue.includes('temp/') ||
         lowerValue.includes('\\') || 
         lowerValue.includes('/');
}

// 获取文件图标
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

// 获取文件名
function getFileName(filePath: string): string {
  const parts = filePath.replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || filePath;
}

// 检测是否是图片文件
function isImageFile(filePath: string): boolean {
  const lowerPath = filePath.toLowerCase();
  return /\.(jpg|jpeg|png|gif|bmp|webp|svg)$/.test(lowerPath);
}

// 渲染单个值
function renderValue(value: any, key: string): React.ReactNode {
  // null 或 undefined
  if (value === null || value === undefined) {
    return <Tag color="default">null</Tag>;
  }
  
  // 布尔值
  if (typeof value === 'boolean') {
    return <Tag color={value ? 'success' : 'error'}>{String(value)}</Tag>;
  }
  
  // 数字
  if (typeof value === 'number') {
    return <Tag color="blue">{value}</Tag>;
  }
  
  // 字符串
  if (typeof value === 'string') {
    // 文件路径
    if (isFilePath(value)) {
      return (
        <Card size="small" style={{ marginTop: '8px', background: '#f0f7ff', border: '1px solid #91d5ff' }}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space>
              {getFileIcon(value)}
              <Text strong>{getFileName(value)}</Text>
            </Space>
            
            {/* 如果是图片，显示缩略图 */}
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
                    // 图片加载失败时隐藏
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
                  // 本地文件路径直接提示
                  alert(`文件已保存在本地:\n${value}\n\n可以直接在文件浏览器中打开`);
                }}
              >
                打开文件位置
              </Button>
              {isImageFile(value) && (
                <Button 
                  size="small" 
                  onClick={() => {
                    // 尝试在新窗口打开
                    window.open(`file:///${value.replace(/\\/g, '/')}`, '_blank');
                  }}
                >
                  在新窗口打开
                </Button>
              )}
            </Space>
          </Space>
        </Card>
      );
    }
    
    // 普通字符串
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
  
  // 数组
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
  
  // 对象
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
  
  // 其他类型
  return <Text>{String(value)}</Text>;
}

export default function ResultDisplay({ data }: ResultDisplayProps) {
  if (!data) {
    return (
      <div style={{ textAlign: 'center', padding: '40px', color: '#999' }}>
        暂无结果
      </div>
    );
  }
  
  // 如果是简单类型，直接显示
  if (typeof data !== 'object' || data === null) {
    return (
      <div style={{ padding: '16px' }}>
        {renderValue(data, 'result')}
      </div>
    );
  }
  
  // 对象或数组
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

