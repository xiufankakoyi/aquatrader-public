import type { ChainGraphData, ChainInfo, ChainNode, NodeDetail } from './industryChain';

export const FALLBACK_CHAIN: ChainInfo = {
  chain_id: 'optical_communication',
  name: '光通信',
  aliases: ['光通讯', 'Optical Communication'],
  description: '缺少后端结构数据时使用的本地产业链结构示例，不包含真实股票映射。',
  node_count: 20,
  source: 'fallback',
  is_fallback: true,
};

const layers = [
  { id: 'upstream', name: '上游材料', order: 0 },
  { id: 'component', name: '中游器件', order: 1 },
  { id: 'product', name: '下游产品', order: 2 },
  { id: 'application', name: '应用场景', order: 3 },
];

function node(
  id: string,
  name: string,
  type: string,
  layer: string,
  order: number,
  description: string,
  keywords: string[] = [],
  aliases: string[] = [],
  demoHighlight = false
): ChainNode {
  const layerName = layers.find((item) => item.id === layer)?.name || layer;
  const demoScore = demoHighlight ? (id === 'optical_module' ? 72 : 66) : 0;
  return {
    id,
    name,
    type,
    layer,
    layer_name: layerName,
    order,
    aliases,
    keywords,
    description,
    x: 0,
    y: 0,
    fixed: true,
    importance: demoHighlight ? 9 : 6,
    hot_score: demoScore,
    hot_score_source: demoHighlight ? '示例热度' : '',
    market_strength: '暂无',
    stock_count: 0,
    candidate_count: 0,
    verified_stock_count: 0,
    candidate_stock_count: 0,
    limit_up_count: 0,
    avg_return_1d: 0,
    avg_pct_chg: 0,
    avg_return_5d: 0,
    total_amount: 0,
    main_net_inflow: 0,
    updated_at: '',
    provider_summary: '',
    symbolSize: [144, 50],
    itemStyle: {},
    label: { show: true },
    is_fallback: true,
    demo_highlight: demoHighlight,
  };
}

export const FALLBACK_GRAPH: ChainGraphData = {
  layers,
  nodes: [
    node('optical_communication', '光通信', 'theme', 'upstream', 0, '光通信产业链主题节点，用于组织上游材料、中游器件、下游产品和应用场景。', ['光通信', '光通讯']),
    node('indium_phosphide', '磷化铟', 'material', 'upstream', 1, '高速光通信器件常用的 III-V 族半导体材料。', ['磷化铟', 'InP', 'III-V'], ['InP', 'Indium Phosphide'], true),
    node('silicon_photonics', '硅光', 'technology', 'component', 0, '利用硅基工艺实现光子器件集成的技术路线。', ['硅光', '硅光子', 'Silicon Photonics']),
    node('pcb', 'PCB', 'component', 'component', 1, '高速光模块和通信设备中的印制电路板环节。', ['PCB', '高速 PCB', '印制电路板']),
    node('thermal', '散热', 'component', 'component', 2, '高速光模块和设备运行所需的热管理环节。', ['散热', '热管理']),
    node('optical_chip', '光芯片', 'component', 'component', 3, '光模块中的核心芯片环节，包括发射、接收、调制等方向。', ['光芯片', '激光器芯片', '探测器芯片'], [], true),
    node('eml', 'EML', 'component', 'component', 4, '电吸收调制激光器，常用于高速率光模块。', ['EML', '电吸收调制激光器']),
    node('dfb', 'DFB', 'component', 'component', 5, '分布反馈激光器，属于光源器件方向。', ['DFB', '分布反馈激光器']),
    node('vcsel', 'VCSEL', 'component', 'component', 6, '垂直腔面发射激光器，常用于短距光互连。', ['VCSEL', '垂直腔面发射']),
    node('dsp', 'DSP', 'component', 'component', 7, '数字信号处理芯片，用于高速信号调制和补偿。', ['DSP', '数字信号处理', 'PAM4']),
    node('tia_cdr', 'TIA/CDR', 'component', 'component', 8, '跨阻放大器与时钟恢复芯片，属于光接收端关键器件。', ['TIA', 'CDR', '跨阻放大器']),
    node('optical_device', '光器件', 'component', 'component', 9, '光模块中的有源和无源器件组合。', ['光器件', '光有源器件', '光无源器件']),
    node('optical_module', '光模块', 'product', 'product', 0, '实现光电信号转换的产品环节，可用于数据中心和通信网络。', ['光模块', 'CPO', '800G', '1.6T', '光收发'], ['Optical Module'], true),
    node('optical_fiber_cable', '光纤光缆', 'product', 'product', 1, '承载光信号传输的基础介质。', ['光纤光缆', '光纤', '光缆']),
    node('communication_equipment', '通信设备', 'product', 'product', 2, '光通信网络中的传输、交换和接入设备。', ['通信设备', '网络设备']),
    node('switch', '交换机', 'product', 'product', 3, '数据中心和通信网络中的数据交换设备。', ['交换机', '以太网交换机', '数据中心交换机']),
    node('ai_datacenter', 'AI 数据中心', 'application', 'application', 0, 'AI 训练和推理所需的大规模数据中心场景。', ['AI 数据中心', '智算中心', '算力']),
    node('cloud_computing', '云计算', 'application', 'application', 1, '基于数据中心基础设施的云服务场景。', ['云计算', '云服务', 'Cloud']),
    node('carrier_network', '运营商网络', 'application', 'application', 2, '电信运营商网络建设和扩容场景。', ['运营商网络', '电信网络', '5G', '骨干网']),
    node('overseas_capex', '海外云厂商资本开支', 'application', 'application', 3, '海外云厂商资本开支变化对光通信需求的观察场景。', ['海外 Capex', '资本开支', '云厂商开支']),
  ],
  edges: [
    { source: 'optical_communication', target: 'indium_phosphide', relation: 'theme', label: { formatter: '主题' }, lineStyle: {}, description: '主题到上游材料' },
    { source: 'indium_phosphide', target: 'optical_chip', relation: 'material', label: { formatter: '材料' }, lineStyle: {}, description: '材料支撑光芯片环节' },
    { source: 'silicon_photonics', target: 'optical_chip', relation: 'technology', label: { formatter: '技术' }, lineStyle: {}, description: '硅光属于光芯片技术路线' },
    { source: 'optical_chip', target: 'eml', relation: 'contains', label: { formatter: '包含' }, lineStyle: {}, description: '光芯片细分器件' },
    { source: 'optical_chip', target: 'dfb', relation: 'contains', label: { formatter: '包含' }, lineStyle: {}, description: '光芯片细分器件' },
    { source: 'optical_chip', target: 'vcsel', relation: 'contains', label: { formatter: '包含' }, lineStyle: {}, description: '光芯片细分器件' },
    { source: 'eml', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'EML 支撑高速光模块' },
    { source: 'dfb', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'DFB 支撑部分光模块路线' },
    { source: 'vcsel', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'VCSEL 支撑短距光模块' },
    { source: 'dsp', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'DSP 支撑高速信号处理' },
    { source: 'tia_cdr', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'TIA/CDR 支撑接收端链路' },
    { source: 'pcb', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: 'PCB 支撑电连接和高速信号' },
    { source: 'thermal', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: '散热支撑高速模块稳定运行' },
    { source: 'optical_device', target: 'optical_module', relation: 'supports', label: { formatter: '支撑' }, lineStyle: {}, description: '光器件支撑光模块集成' },
    { source: 'optical_module', target: 'switch', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '光模块用于高速交换设备' },
    { source: 'optical_module', target: 'communication_equipment', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '光模块用于通信设备' },
    { source: 'optical_fiber_cable', target: 'carrier_network', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '光纤光缆用于运营商网络' },
    { source: 'communication_equipment', target: 'carrier_network', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '通信设备用于运营商网络' },
    { source: 'switch', target: 'ai_datacenter', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '交换机用于 AI 数据中心' },
    { source: 'switch', target: 'cloud_computing', relation: 'used_by', label: { formatter: '用于' }, lineStyle: {}, description: '交换机用于云计算基础设施' },
    { source: 'optical_module', target: 'ai_datacenter', relation: 'demand', label: { formatter: '需求' }, lineStyle: {}, description: 'AI 数据中心形成光模块需求场景' },
    { source: 'optical_module', target: 'cloud_computing', relation: 'demand', label: { formatter: '需求' }, lineStyle: {}, description: '云计算形成光模块需求场景' },
    { source: 'overseas_capex', target: 'ai_datacenter', relation: 'observes', label: { formatter: '观察' }, lineStyle: {}, description: '资本开支用于观察 AI 数据中心建设节奏' },
    { source: 'overseas_capex', target: 'cloud_computing', relation: 'observes', label: { formatter: '观察' }, lineStyle: {}, description: '资本开支用于观察云计算基础设施投入' },
  ],
  summary: {
    chain_id: 'optical_communication',
    chain_name: '光通信',
    hot_score: 0,
    market_strength: '暂无',
    top_node: '',
    top_node_name: '',
    limit_up_count: 0,
    turnover_change: 0,
    node_count: 20,
    stock_count: 0,
  },
  source: 'fallback',
  is_fallback: true,
};

export function createFallbackNodeDetail(graph: ChainGraphData, selected: ChainNode): NodeDetail {
  const upstreamIds = graph.edges.filter((edge) => edge.target === selected.id).map((edge) => edge.source);
  const downstreamIds = graph.edges.filter((edge) => edge.source === selected.id).map((edge) => edge.target);
  const getNodeBrief = (id: string) => {
    const item = graph.nodes.find((candidate) => candidate.id === id);
    return item ? { id: item.id, name: item.name, type: item.type } : null;
  };

  return {
    node: {
      id: selected.id,
      name: selected.name,
      type: selected.type,
      layer: selected.layer,
      layer_name: selected.layer_name,
      order: selected.order,
      aliases: selected.aliases || [],
      keywords: selected.keywords || [],
      description: selected.description || '暂无本地描述',
      importance: selected.importance || 0,
    },
    upstream: upstreamIds.map(getNodeBrief).filter(Boolean) as { id: string; name: string; type: string }[],
    downstream: downstreamIds.map(getNodeBrief).filter(Boolean) as { id: string; name: string; type: string }[],
    metrics: {
      hot_score: selected.hot_score || 0,
      hot_score_source: selected.hot_score_source || '',
      market_strength: selected.market_strength || '暂无',
      stock_count: 0,
      verified_stock_count: 0,
      candidate_stock_count: 0,
    },
    stock_count: 0,
    stocks: [],
  };
}
