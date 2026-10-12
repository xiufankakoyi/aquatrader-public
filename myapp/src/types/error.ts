/**
 * 回测错误分类体系
 * 用于精准定位错误发生阶段和原因
 */

export enum ErrorStage {
  FRONTEND_SEND = 'frontend_send',
  BACKEND_RECEIVE = 'backend_receive',
  SYSTEM_INTERACTION = 'system_interaction',
  BACKTEST_EXECUTE = 'backtest_execute',
  RESULT_RETURN = 'result_return',
  UNKNOWN = 'unknown'
}

export enum ErrorCategory {
  NETWORK = 'network',
  VALIDATION = 'validation',
  DATA = 'data',
  COMPUTATION = 'computation',
  STORAGE = 'storage',
  TIMEOUT = 'timeout',
  PERMISSION = 'permission',
  CONFIGURATION = 'configuration',
  INTERNAL = 'internal',
  UNKNOWN = 'unknown'
}

export enum ErrorSeverity {
  LOW = 'low',
  MEDIUM = 'medium',
  HIGH = 'high',
  CRITICAL = 'critical'
}

export interface ErrorSolution {
  title: string;
  description: string;
  steps: string[];
  autoFix?: () => void;
}

export interface BacktestErrorContext {
  strategyName?: string;
  startDate?: string;
  endDate?: string;
  benchmarkCode?: string;
  params?: Record<string, any>;
  timestamp: number;
  sessionId?: string;
  requestPayload?: any;
  responsePayload?: any;
  stackTrace?: string;
}

export interface BacktestError {
  id: string;
  code: string;
  stage: ErrorStage;
  category: ErrorCategory;
  severity: ErrorSeverity;
  title: string;
  message: string;
  detail?: string;
  possibleCauses: string[];
  solutions: ErrorSolution[];
  context: BacktestErrorContext;
  rawError?: any;
  isRecoverable: boolean;
  retryCount: number;
  maxRetries: number;
}

export const ERROR_CODES = {
  // 前端发送阶段 (1xxx)
  FRONTEND_NETWORK_OFFLINE: 'E1001',
  FRONTEND_REQUEST_TIMEOUT: 'E1002',
  FRONTEND_INVALID_PARAMS: 'E1003',
  FRONTEND_SOCKET_DISCONNECTED: 'E1004',
  FRONTEND_SERIALIZATION_ERROR: 'E1005',
  
  // 后端接收阶段 (2xxx)
  BACKEND_NOT_RESPONDING: 'E2001',
  BACKEND_VALIDATION_FAILED: 'E2002',
  BACKEND_RATE_LIMITED: 'E2003',
  BACKEND_AUTH_FAILED: 'E2004',
  BACKEND_INVALID_REQUEST: 'E2005',
  
  // 系统交互阶段 (3xxx)
  SYSTEM_DB_CONNECTION_FAILED: 'E3001',
  SYSTEM_REDIS_CONNECTION_FAILED: 'E3002',
  SYSTEM_STRATEGY_NOT_FOUND: 'E3003',
  SYSTEM_DATA_QUERY_FAILED: 'E3004',
  SYSTEM_INITIALIZATION_FAILED: 'E3005',
  
  // 回测执行阶段 (4xxx)
  BACKTEST_NO_DATA: 'E4001',
  BACKTEST_DATA_INCOMPLETE: 'E4002',
  BACKTEST_CALCULATION_ERROR: 'E4003',
  BACKTEST_MEMORY_OVERFLOW: 'E4004',
  BACKTEST_ENGINE_ERROR: 'E4005',
  BACKTEST_STRATEGY_ERROR: 'E4006',
  BACKTEST_SIGNAL_ERROR: 'E4007',
  BACKTEST_TRADE_ERROR: 'E4008',
  
  // 结果回传阶段 (5xxx)
  RESULT_STREAM_INTERRUPTED: 'E5001',
  RESULT_DESERIALIZATION_ERROR: 'E5002',
  RESULT_INCOMPLETE: 'E5003',
  RESULT_SOCKET_ERROR: 'E5004',
  
  // 未知错误 (9xxx)
  UNKNOWN_ERROR: 'E9999'
} as const;

export type ErrorCode = typeof ERROR_CODES[keyof typeof ERROR_CODES];

export const ERROR_DEFINITIONS: Record<ErrorCode, Partial<BacktestError>> = {
  [ERROR_CODES.FRONTEND_NETWORK_OFFLINE]: {
    stage: ErrorStage.FRONTEND_SEND,
    category: ErrorCategory.NETWORK,
    severity: ErrorSeverity.HIGH,
    title: '网络连接已断开',
    message: '无法发送回测请求，请检查网络连接',
    possibleCauses: [
      '网络连接已断开',
      '防火墙阻止了连接',
      'DNS 解析失败'
    ],
    solutions: [{
      title: '检查网络连接',
      description: '确保您的设备已连接到互联网',
      steps: [
        '检查网络电缆或 WiFi 连接',
        '尝试访问其他网站确认网络正常',
        '检查防火墙设置是否阻止了应用'
      ]
    }]
  },
  
  [ERROR_CODES.FRONTEND_REQUEST_TIMEOUT]: {
    stage: ErrorStage.FRONTEND_SEND,
    category: ErrorCategory.TIMEOUT,
    severity: ErrorSeverity.MEDIUM,
    title: '请求发送超时',
    message: '回测请求发送超时，服务器可能繁忙',
    possibleCauses: [
      '服务器负载过高',
      '网络延迟过大',
      '请求数据过大'
    ],
    solutions: [{
      title: '重试请求',
      description: '稍后重试发送请求',
      steps: [
        '等待几秒后重试',
        '检查服务器状态',
        '如果问题持续，联系技术支持'
      ]
    }]
  },
  
  [ERROR_CODES.FRONTEND_INVALID_PARAMS]: {
    stage: ErrorStage.FRONTEND_SEND,
    category: ErrorCategory.VALIDATION,
    severity: ErrorSeverity.LOW,
    title: '请求参数无效',
    message: '回测参数验证失败，请检查输入',
    possibleCauses: [
      '日期格式不正确',
      '策略名称不存在',
      '参数值超出范围'
    ],
    solutions: [{
      title: '检查参数设置',
      description: '验证所有输入参数是否正确',
      steps: [
        '确认日期范围有效（开始日期早于结束日期）',
        '确认策略名称正确',
        '检查参数值是否在允许范围内'
      ]
    }]
  },
  
  [ERROR_CODES.FRONTEND_SOCKET_DISCONNECTED]: {
    stage: ErrorStage.FRONTEND_SEND,
    category: ErrorCategory.NETWORK,
    severity: ErrorSeverity.HIGH,
    title: 'Socket 连接已断开',
    message: '与服务器的实时连接已断开',
    possibleCauses: [
      '服务器重启',
      '网络中断',
      '长时间无活动导致超时'
    ],
    solutions: [{
      title: '重新建立连接',
      description: '刷新页面或重新启动回测',
      steps: [
        '刷新浏览器页面',
        '清除浏览器缓存后重试',
        '检查服务器是否正常运行'
      ]
    }]
  },
  
  [ERROR_CODES.FRONTEND_SERIALIZATION_ERROR]: {
    stage: ErrorStage.FRONTEND_SEND,
    category: ErrorCategory.INTERNAL,
    severity: ErrorSeverity.MEDIUM,
    title: '数据序列化错误',
    message: '请求数据无法正确序列化',
    possibleCauses: [
      '参数包含无法序列化的对象',
      '循环引用',
      '特殊字符编码问题'
    ],
    solutions: [{
      title: '简化参数',
      description: '检查并简化复杂参数',
      steps: [
        '移除不必要的复杂参数',
        '确保所有参数都是基本类型',
        '检查是否有特殊字符'
      ]
    }]
  },
  
  [ERROR_CODES.BACKEND_NOT_RESPONDING]: {
    stage: ErrorStage.BACKEND_RECEIVE,
    category: ErrorCategory.NETWORK,
    severity: ErrorSeverity.CRITICAL,
    title: '后端服务无响应',
    message: '无法连接到后端服务器',
    possibleCauses: [
      '后端服务未启动',
      '端口被占用',
      '服务崩溃'
    ],
    solutions: [{
      title: '启动后端服务',
      description: '确保后端服务正在运行',
      steps: [
        '运行 start_no_docker.bat 启动服务',
        '检查端口 5000 是否被占用',
        '查看后端日志排查问题'
      ]
    }]
  },
  
  [ERROR_CODES.BACKEND_VALIDATION_FAILED]: {
    stage: ErrorStage.BACKEND_RECEIVE,
    category: ErrorCategory.VALIDATION,
    severity: ErrorSeverity.LOW,
    title: '后端参数验证失败',
    message: '服务器拒绝了请求参数',
    possibleCauses: [
      '参数类型不匹配',
      '必填参数缺失',
      '参数值不合法'
    ],
    solutions: [{
      title: '修正参数',
      description: '根据错误提示修正参数',
      steps: [
        '查看详细错误信息',
        '对照参数文档检查',
        '使用默认参数重试'
      ]
    }]
  },
  
  [ERROR_CODES.BACKEND_RATE_LIMITED]: {
    stage: ErrorStage.BACKEND_RECEIVE,
    category: ErrorCategory.PERMISSION,
    severity: ErrorSeverity.MEDIUM,
    title: '请求频率超限',
    message: '请求过于频繁，请稍后再试',
    possibleCauses: [
      '短时间内发送大量请求',
      '并发回测任务过多'
    ],
    solutions: [{
      title: '等待后重试',
      description: '降低请求频率',
      steps: [
        '等待几分钟后重试',
        '减少并发回测数量',
        '联系管理员提升配额'
      ]
    }]
  },
  
  [ERROR_CODES.BACKEND_AUTH_FAILED]: {
    stage: ErrorStage.BACKEND_RECEIVE,
    category: ErrorCategory.PERMISSION,
    severity: ErrorSeverity.HIGH,
    title: '认证失败',
    message: '无权限执行此操作',
    possibleCauses: [
      '未登录或会话过期',
      '权限不足'
    ],
    solutions: [{
      title: '重新认证',
      description: '检查登录状态',
      steps: [
        '刷新页面重新登录',
        '检查用户权限',
        '联系管理员获取权限'
      ]
    }]
  },
  
  [ERROR_CODES.BACKEND_INVALID_REQUEST]: {
    stage: ErrorStage.BACKEND_RECEIVE,
    category: ErrorCategory.VALIDATION,
    severity: ErrorSeverity.MEDIUM,
    title: '无效请求',
    message: '服务器无法处理此请求',
    possibleCauses: [
      '请求格式错误',
      '缺少必要字段',
      '协议版本不匹配'
    ],
    solutions: [{
      title: '检查请求格式',
      description: '确保请求符合 API 规范',
      steps: [
        '刷新页面重试',
        '清除浏览器缓存',
        '检查是否有版本更新'
      ]
    }]
  },
  
  [ERROR_CODES.SYSTEM_DB_CONNECTION_FAILED]: {
    stage: ErrorStage.SYSTEM_INTERACTION,
    category: ErrorCategory.STORAGE,
    severity: ErrorSeverity.CRITICAL,
    title: '数据库连接失败',
    message: '无法连接到数据存储系统',
    possibleCauses: [
      'DuckDB/QuestDB 未正确初始化',
      '数据库文件损坏',
      '磁盘空间不足'
    ],
    solutions: [{
      title: '检查数据库',
      description: '验证数据库配置和状态',
      steps: [
        '确认 data/parquet_data 目录存在且有数据',
        '检查 DB_BACKEND 环境变量设置',
        '查看后端日志中的数据库错误'
      ]
    }]
  },
  
  [ERROR_CODES.SYSTEM_REDIS_CONNECTION_FAILED]: {
    stage: ErrorStage.SYSTEM_INTERACTION,
    category: ErrorCategory.STORAGE,
    severity: ErrorSeverity.HIGH,
    title: 'Redis 连接失败',
    message: '无法连接到缓存服务',
    possibleCauses: [
      'Redis 服务未启动',
      'Redis 配置错误',
      '端口被占用'
    ],
    solutions: [{
      title: '启动 Redis',
      description: '确保 Redis 服务正在运行',
      steps: [
        '运行 Redis 服务器',
        '检查端口 6379 是否可用',
        '验证 Redis 连接配置'
      ]
    }]
  },
  
  [ERROR_CODES.SYSTEM_STRATEGY_NOT_FOUND]: {
    stage: ErrorStage.SYSTEM_INTERACTION,
    category: ErrorCategory.CONFIGURATION,
    severity: ErrorSeverity.MEDIUM,
    title: '策略未找到',
    message: '指定的策略不存在或未加载',
    possibleCauses: [
      '策略名称拼写错误',
      '策略文件未正确放置',
      '策略加载失败'
    ],
    solutions: [{
      title: '检查策略配置',
      description: '验证策略是否存在',
      steps: [
        '检查策略名称是否正确',
        '确认策略文件在 core/strategies 目录',
        '查看后端日志确认策略加载状态'
      ]
    }]
  },
  
  [ERROR_CODES.SYSTEM_DATA_QUERY_FAILED]: {
    stage: ErrorStage.SYSTEM_INTERACTION,
    category: ErrorCategory.DATA,
    severity: ErrorSeverity.HIGH,
    title: '数据查询失败',
    message: '无法从数据库获取所需数据',
    possibleCauses: [
      '查询日期范围无数据',
      'SQL 查询语法错误',
      '数据库表不存在'
    ],
    solutions: [{
      title: '检查数据范围',
      description: '验证数据库中有对应日期的数据',
      steps: [
        '确认日期范围在数据覆盖范围内',
        '检查数据库表结构',
        '运行数据导入脚本'
      ]
    }]
  },
  
  [ERROR_CODES.SYSTEM_INITIALIZATION_FAILED]: {
    stage: ErrorStage.SYSTEM_INTERACTION,
    category: ErrorCategory.INTERNAL,
    severity: ErrorSeverity.CRITICAL,
    title: '系统初始化失败',
    message: '回测系统初始化过程中发生错误',
    possibleCauses: [
      '配置文件缺失',
      '依赖服务未启动',
      '资源不足'
    ],
    solutions: [{
      title: '检查系统配置',
      description: '验证所有配置和依赖',
      steps: [
        '检查 .env 配置文件',
        '确保所有依赖服务已启动',
        '查看详细错误日志'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_NO_DATA]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.DATA,
    severity: ErrorSeverity.HIGH,
    title: '无可用数据',
    message: '指定日期范围内没有股票数据',
    possibleCauses: [
      '日期范围超出数据覆盖范围',
      '数据库为空',
      '数据导入失败'
    ],
    solutions: [{
      title: '调整日期范围',
      description: '选择有数据的日期范围',
      steps: [
        '检查数据库中可用的日期范围',
        '调整开始和结束日期',
        '确认数据已正确导入'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_DATA_INCOMPLETE]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.DATA,
    severity: ErrorSeverity.MEDIUM,
    title: '数据不完整',
    message: '部分日期或股票数据缺失',
    possibleCauses: [
      '数据源中断',
      '部分交易日数据缺失',
      '股票退市或暂停交易'
    ],
    solutions: [{
      title: '检查数据完整性',
      description: '验证数据是否完整',
      steps: [
        '查看缺失的具体日期',
        '检查数据源状态',
        '考虑使用其他日期范围'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_CALCULATION_ERROR]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.COMPUTATION,
    severity: ErrorSeverity.HIGH,
    title: '计算错误',
    message: '回测计算过程中发生错误',
    possibleCauses: [
      '指标计算溢出',
      '除零错误',
      '数据类型不匹配'
    ],
    solutions: [{
      title: '检查策略参数',
      description: '调整策略参数避免计算错误',
      steps: [
        '检查策略参数是否合理',
        '尝试使用默认参数',
        '查看详细错误日志定位问题'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_MEMORY_OVERFLOW]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.INTERNAL,
    severity: ErrorSeverity.CRITICAL,
    title: '内存溢出',
    message: '回测过程占用内存过大',
    possibleCauses: [
      '日期范围过长',
      '股票池过大',
      '内存泄漏'
    ],
    solutions: [{
      title: '缩小回测范围',
      description: '减少数据量',
      steps: [
        '缩短日期范围',
        '减少股票池大小',
        '重启服务释放内存'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_ENGINE_ERROR]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.INTERNAL,
    severity: ErrorSeverity.HIGH,
    title: '回测引擎错误',
    message: '回测引擎内部发生错误',
    possibleCauses: [
      '引擎初始化失败',
      '内部状态异常',
      '未处理的异常'
    ],
    solutions: [{
      title: '重启服务',
      description: '重置回测引擎状态',
      steps: [
        '停止当前回测',
        '重启后端服务',
        '重新开始回测'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_STRATEGY_ERROR]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.COMPUTATION,
    severity: ErrorSeverity.MEDIUM,
    title: '策略执行错误',
    message: '策略计算过程中发生错误',
    possibleCauses: [
      '策略代码有 bug',
      '指标计算参数错误',
      '数据格式不匹配'
    ],
    solutions: [{
      title: '检查策略代码',
      description: '验证策略逻辑',
      steps: [
        '查看策略代码是否有错误',
        '检查指标计算参数',
        '使用简单策略测试'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_SIGNAL_ERROR]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.COMPUTATION,
    severity: ErrorSeverity.MEDIUM,
    title: '信号生成错误',
    message: '策略信号生成过程中发生错误',
    possibleCauses: [
      '信号生成逻辑错误',
      '指标数据缺失',
      '参数配置不当'
    ],
    solutions: [{
      title: '调整信号参数',
      description: '检查信号生成配置',
      steps: [
        '检查信号生成参数',
        '验证指标数据完整性',
        '简化信号条件测试'
      ]
    }]
  },
  
  [ERROR_CODES.BACKTEST_TRADE_ERROR]: {
    stage: ErrorStage.BACKTEST_EXECUTE,
    category: ErrorCategory.COMPUTATION,
    severity: ErrorSeverity.MEDIUM,
    title: '交易执行错误',
    message: '模拟交易执行过程中发生错误',
    possibleCauses: [
      '价格数据异常',
      '仓位计算错误',
      '资金不足'
    ],
    solutions: [{
      title: '检查交易逻辑',
      description: '验证交易执行条件',
      steps: [
        '检查价格数据是否正常',
        '验证仓位管理逻辑',
        '调整初始资金'
      ]
    }]
  },
  
  [ERROR_CODES.RESULT_STREAM_INTERRUPTED]: {
    stage: ErrorStage.RESULT_RETURN,
    category: ErrorCategory.NETWORK,
    severity: ErrorSeverity.MEDIUM,
    title: '数据流中断',
    message: '回测结果传输中断',
    possibleCauses: [
      '网络连接不稳定',
      '服务器超时',
      '客户端断开'
    ],
    solutions: [{
      title: '重新运行回测',
      description: '重新开始回测获取完整结果',
      steps: [
        '检查网络连接',
        '重新运行回测',
        '查看是否有部分结果已保存'
      ]
    }]
  },
  
  [ERROR_CODES.RESULT_DESERIALIZATION_ERROR]: {
    stage: ErrorStage.RESULT_RETURN,
    category: ErrorCategory.INTERNAL,
    severity: ErrorSeverity.MEDIUM,
    title: '数据解析错误',
    message: '无法解析服务器返回的数据',
    possibleCauses: [
      '数据格式不兼容',
      '数据损坏',
      '版本不匹配'
    ],
    solutions: [{
      title: '刷新页面',
      description: '重新加载数据',
      steps: [
        '刷新浏览器页面',
        '清除缓存后重试',
        '检查是否有版本更新'
      ]
    }]
  },
  
  [ERROR_CODES.RESULT_INCOMPLETE]: {
    stage: ErrorStage.RESULT_RETURN,
    category: ErrorCategory.DATA,
    severity: ErrorSeverity.MEDIUM,
    title: '结果不完整',
    message: '回测结果数据不完整',
    possibleCauses: [
      '回测被中断',
      '部分数据丢失',
      '存储失败'
    ],
    solutions: [{
      title: '重新运行',
      description: '重新执行完整回测',
      steps: [
        '重新运行回测',
        '检查是否有错误日志',
        '确认回测完成状态'
      ]
    }]
  },
  
  [ERROR_CODES.RESULT_SOCKET_ERROR]: {
    stage: ErrorStage.RESULT_RETURN,
    category: ErrorCategory.NETWORK,
    severity: ErrorSeverity.HIGH,
    title: 'Socket 传输错误',
    message: '实时数据传输过程中发生错误',
    possibleCauses: [
      'Socket 连接断开',
      '数据包过大',
      '传输超时'
    ],
    solutions: [{
      title: '检查连接',
      description: '验证 Socket 连接状态',
      steps: [
        '刷新页面重新连接',
        '检查网络稳定性',
        '查看控制台错误详情'
      ]
    }]
  },
  
  [ERROR_CODES.UNKNOWN_ERROR]: {
    stage: ErrorStage.UNKNOWN,
    category: ErrorCategory.UNKNOWN,
    severity: ErrorSeverity.HIGH,
    title: '未知错误',
    message: '发生了未预期的错误',
    possibleCauses: [
      '系统内部错误',
      '未捕获的异常',
      '外部依赖问题'
    ],
    solutions: [{
      title: '联系技术支持',
      description: '提供错误详情以获取帮助',
      steps: [
        '复制完整错误信息',
        '截图错误界面',
        '联系技术支持或提交 Issue'
      ]
    }]
  }
}

export const STAGE_LABELS: Record<ErrorStage, string> = {
  [ErrorStage.FRONTEND_SEND]: '前端发送',
  [ErrorStage.BACKEND_RECEIVE]: '后端接收',
  [ErrorStage.SYSTEM_INTERACTION]: '系统交互',
  [ErrorStage.BACKTEST_EXECUTE]: '回测执行',
  [ErrorStage.RESULT_RETURN]: '结果回传',
  [ErrorStage.UNKNOWN]: '未知阶段'
}

export const CATEGORY_LABELS: Record<ErrorCategory, string> = {
  [ErrorCategory.NETWORK]: '网络错误',
  [ErrorCategory.VALIDATION]: '参数验证',
  [ErrorCategory.DATA]: '数据错误',
  [ErrorCategory.COMPUTATION]: '计算错误',
  [ErrorCategory.STORAGE]: '存储错误',
  [ErrorCategory.TIMEOUT]: '超时',
  [ErrorCategory.PERMISSION]: '权限错误',
  [ErrorCategory.CONFIGURATION]: '配置错误',
  [ErrorCategory.INTERNAL]: '内部错误',
  [ErrorCategory.UNKNOWN]: '未知类型'
}

export const SEVERITY_LABELS: Record<ErrorSeverity, { label: string; color: string; icon: string }> = {
  [ErrorSeverity.LOW]: { label: '低', color: 'text-blue-500', icon: 'ℹ️' },
  [ErrorSeverity.MEDIUM]: { label: '中', color: 'text-yellow-500', icon: '⚠️' },
  [ErrorSeverity.HIGH]: { label: '高', color: 'text-orange-500', icon: '🔶' },
  [ErrorSeverity.CRITICAL]: { label: '严重', color: 'text-red-500', icon: '🚨' }
}
