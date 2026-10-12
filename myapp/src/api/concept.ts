import service from './index';

export interface ConceptInfo {
  concept_id: string;
  concept_name: string;
  aliases: string[];
  parent_concepts: string[];
  industry_chain: string[];
  keywords: string[];
  description: string;
}

export interface ConceptStock {
  symbol: string;
  stock_name: string;
  concept_id: string;
  chain_position: string;
  relevance_score: number;
  purity_score: number;
  evidence_type: string;
  evidence_text: string;
  evidence_source: string;
  updated_at: string;
  notes: string;
  is_sample: boolean;
  evidence_score: number;
  market_confirm_score: number;
  concept_score: number;
}

interface ApiResponse<T> {
  success: boolean;
  data: T;
  message?: string;
  error?: string;
  research_boundary?: string;
}

export async function listConcepts(): Promise<ConceptInfo[]> {
  const response = await service.get<ApiResponse<ConceptInfo[]>>('/api/concepts');
  return response.data.data;
}

export async function searchConcepts(keyword: string): Promise<ConceptInfo[]> {
  const response = await service.post<ApiResponse<ConceptInfo[]>>('/api/concepts/search', { keyword });
  return response.data.data;
}

export async function getConceptStocks(conceptId: string): Promise<{ rows: ConceptStock[]; message?: string }> {
  const response = await service.get<ApiResponse<ConceptStock[]>>(`/api/concepts/${encodeURIComponent(conceptId)}/stocks`);
  return { rows: response.data.data, message: response.data.message };
}
