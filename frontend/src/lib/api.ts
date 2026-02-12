const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api'

class ApiClient {
  private token: string | null = null

  setToken(token: string | null) {
    this.token = token
    if (token) {
      localStorage.setItem('token', token)
    } else {
      localStorage.removeItem('token')
    }
  }

  getToken(): string | null {
    if (typeof window === 'undefined') return null
    if (!this.token) {
      this.token = localStorage.getItem('token')
    }
    return this.token
  }

  private async request<T>(
    endpoint: string,
    options: RequestInit = {}
  ): Promise<T> {
    const token = this.getToken()
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(options.headers as Record<string, string> || {}),
    }

    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    }

    const response = await fetch(`${API_URL}${endpoint}`, {
      ...options,
      headers,
    })

    if (!response.ok) {
      if (response.status === 401) {
        this.setToken(null)
        window.location.href = '/login'
      }
      const error = await response.json().catch(() => ({}))
      throw new Error(error.detail || 'Request failed')
    }

    if (response.status === 204) {
      return {} as T
    }

    return response.json()
  }

  // Auth
  async register(data: { email: string; password: string; name: string; customer_code?: string }) {
    return this.request<{ id: string; email: string; name: string }>('/auth/register', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async login(email: string, password: string) {
    const response = await this.request<{
      access_token: string
      expires_in: number
      user: { id: string; email: string; name: string; role: string; customer_id: string }
    }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    })
    this.setToken(response.access_token)
    return response
  }

  async logout() {
    await this.request('/auth/logout', { method: 'POST' })
    this.setToken(null)
  }

  async getMe() {
    return this.request<{ id: string; email: string; name: string; role: string; customer_id: string }>('/auth/me')
  }

  // Orders
  async getOrders(params?: { status?: string; skip?: number; limit?: number }) {
    const searchParams = new URLSearchParams()
    if (params?.status) searchParams.set('status', params.status)
    if (params?.skip) searchParams.set('skip', params.skip.toString())
    if (params?.limit) searchParams.set('limit', params.limit.toString())
    const query = searchParams.toString()
    return this.request<Order[]>(`/orders${query ? `?${query}` : ''}`)
  }

  async getOrder(id: string) {
    return this.request<Order>(`/orders/${id}`)
  }

  async createOrder(data: OrderCreate) {
    return this.request<Order>('/orders', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async updateOrder(id: string, data: Partial<OrderCreate>) {
    return this.request<Order>(`/orders/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  async deleteOrder(id: string) {
    return this.request(`/orders/${id}`, { method: 'DELETE' })
  }

  async importOrder(file: File, orderNumber?: string) {
    const formData = new FormData()
    formData.append('file', file)
    if (orderNumber) formData.append('order_number', orderNumber)

    const token = this.getToken()
    const response = await fetch(`${API_URL}/orders/import`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
    })

    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      throw new Error(error.detail || 'Import failed')
    }

    return response.json() as Promise<Order>
  }

  async importOrdersBatch(rows: OrderImportRow[]): Promise<Order[]> {
    return this.request<Order[]>('/orders/import-batch', {
      method: 'POST',
      body: JSON.stringify({ rows }),
    })
  }

  // Patterns
  async getPatterns() {
    return this.request<Pattern[]>('/patterns')
  }

  async getPattern(id: string) {
    return this.request<Pattern>(`/patterns/${id}`)
  }

  async uploadPattern(name: string, fileType: string, dxfFile: File, rulFile?: File) {
    const formData = new FormData()
    formData.append('name', name)
    formData.append('file_type', fileType)
    formData.append('dxf_file', dxfFile)
    if (rulFile) formData.append('rul_file', rulFile)

    const token = this.getToken()
    const response = await fetch(`${API_URL}/patterns/upload`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
    })

    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      throw new Error(error.detail || 'Upload failed')
    }

    return response.json() as Promise<Pattern>
  }

  async parsePattern(id: string) {
    return this.request<{ success: boolean; sizes: string[]; materials: string[]; piece_count: number }>(
      `/patterns/${id}/parse`,
      { method: 'POST' }
    )
  }

  async updateFabricMappings(patternId: string, mappings: { material_name: string; fabric_id: string }[]) {
    return this.request(`/patterns/${patternId}/fabric-mapping`, {
      method: 'POST',
      body: JSON.stringify(mappings),
    })
  }

  async getPatternPieces(patternId: string, material?: string) {
    const query = material ? `?material=${encodeURIComponent(material)}` : ''
    return this.request<PatternPiecesResponse>(`/patterns/${patternId}/pieces${query}`)
  }

  // Fabrics
  async getFabrics() {
    return this.request<Fabric[]>('/fabrics')
  }

  async createFabric(data: { name: string; code: string; width_inches: number; cost_per_yard?: number }) {
    return this.request<Fabric>('/fabrics', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async updateFabric(id: string, data: Partial<{ name: string; code: string; width_inches: number; cost_per_yard: number }>) {
    return this.request<Fabric>(`/fabrics/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  async deleteFabric(id: string) {
    return this.request(`/fabrics/${id}`, { method: 'DELETE' })
  }

  // Cost Config
  async getCostConfig() {
    return this.request<CostConfig>('/costs')
  }

  async updateCostConfig(data: Partial<CostConfig>) {
    return this.request<CostConfig>('/costs', {
      method: 'PUT',
      body: JSON.stringify(data),
    })
  }

  // Nesting
  async createNestingJob(data: NestingJobCreate) {
    return this.request<NestingJob>('/nesting/jobs', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async getNestingJob(id: string) {
    return this.request<NestingJob>(`/nesting/jobs/${id}`)
  }

  async getNestingJobs(orderId?: string) {
    const query = orderId ? `?order_id=${orderId}` : ''
    return this.request<NestingJob[]>(`/nesting/jobs${query}`)
  }

  async getNestingJobPreview(jobId: string) {
    return this.request<{
      has_preview: boolean
      ratio_str: string | null
      efficiency: number | null
      preview_base64: string | null
      timestamp: number | null
    }>(`/nesting/jobs/${jobId}/preview`)
  }

  async cancelNestingJob(jobId: string) {
    return this.request<{ message: string; status: string }>(`/nesting/jobs/${jobId}/cancel`, { method: 'POST' })
  }

  async getMarkers(patternId?: string, fabricId?: string) {
    const params = new URLSearchParams()
    if (patternId) params.set('pattern_id', patternId)
    if (fabricId) params.set('fabric_id', fabricId)
    const query = params.toString()
    return this.request<Marker[]>(`/nesting/markers${query ? `?${query}` : ''}`)
  }

  // Cutplans
  async optimizeCutplan(data: CutplanOptimizeRequest) {
    return this.request<Cutplan[]>('/cutplans/optimize', {
      method: 'POST',
      body: JSON.stringify(data),
    })
  }

  async getCutplan(id: string) {
    return this.request<Cutplan>(`/cutplans/${id}`)
  }

  async getCutplans(orderId?: string) {
    const query = orderId ? `?order_id=${orderId}` : ''
    return this.request<Cutplan[]>(`/cutplans${query}`)
  }

  async getCutplanOptimizeStatus(orderId: string) {
    return this.request<{
      status: string
      progress: number
      message: string
      strategies_total: number
      strategies_done: number
    }>(`/cutplans/optimize-status/${orderId}`)
  }

  async cancelCutplanOptimize(orderId: string) {
    return this.request<{ message: string; status: string }>(`/cutplans/optimize-cancel/${orderId}`, { method: 'POST' })
  }

  async approveCutplan(id: string) {
    return this.request<Cutplan>(`/cutplans/${id}/approve`, { method: 'POST' })
  }

  async getCostAnalysis(id: string) {
    return this.request<CostBreakdown>(`/cutplans/${id}/cost-analysis`)
  }

  // Final Nesting (CPU Refinement)
  async startRefinement(cutplanId: string, config: RefinementConfig) {
    return this.request<{ message: string; cutplan_id: string }>(`/cutplans/${cutplanId}/refine`, {
      method: 'POST',
      body: JSON.stringify(config),
    })
  }

  async getRefinementStatus(cutplanId: string) {
    return this.request<RefinementStatus>(`/cutplans/${cutplanId}/refine-status`)
  }

  async cancelRefinement(cutplanId: string) {
    return this.request<{ message: string; status: string }>(`/cutplans/${cutplanId}/refine-cancel`, { method: 'POST' })
  }

  async downloadMarkersDxf(cutplanId: string): Promise<Blob> {
    const token = this.getToken()
    const headers: Record<string, string> = {}
    if (token) headers['Authorization'] = `Bearer ${token}`

    const response = await fetch(`${API_URL}/cutplans/${cutplanId}/download-markers`, { headers })
    if (!response.ok) {
      const error = await response.json().catch(() => ({}))
      throw new Error(error.detail || 'Download failed')
    }
    return response.blob()
  }
}

export const api = new ApiClient()

// Types
export interface Order {
  id: string
  customer_id: string
  order_number: string
  style_number?: string
  style_id?: string
  pattern_id?: string
  status: string
  piece_buffer_mm: number
  edge_buffer_mm: number
  rotation_mode: string
  order_lines: OrderLine[]
  created_at: string
  updated_at: string
}

export interface OrderLine {
  id: string
  order_id: string
  fabric_code: string
  color_code: string
  extra_percent: number
  fabric_id?: string
  size_quantities: SizeQuantity[]
}

export interface SizeQuantity {
  id: string
  order_line_id: string
  size_code: string
  quantity: number
}

export interface OrderCreate {
  order_number: string
  style_id?: string
  pattern_id?: string
  piece_buffer_mm?: number
  edge_buffer_mm?: number
  rotation_mode?: string
  colors?: {
    color_code: string
    color_name?: string
    fabric_id?: string
    material_name?: string
    quantities?: { size_code: string; quantity: number }[]
  }[]
}

export interface Pattern {
  id: string
  customer_id: string
  name: string
  file_type: string
  dxf_file_path?: string
  rul_file_path?: string
  is_parsed: boolean
  available_sizes: string[]
  available_materials: string[]
  parse_metadata: Record<string, unknown>
  fabric_mappings: PatternFabricMapping[]
  created_at: string
  updated_at: string
}

export interface PatternFabricMapping {
  id: string
  pattern_id: string
  material_name: string
  fabric_id?: string
}

export interface Fabric {
  id: string
  customer_id: string
  name: string
  code: string
  width_inches: number
  cost_per_yard: number
  description?: string
  created_at: string
  updated_at: string
}

export interface CostConfig {
  id: string
  customer_id: string
  name: string
  fabric_cost_per_yard: number
  spreading_cost_per_yard: number
  spreading_cost_per_ply: number
  spreading_labor_cost_per_hour: number
  spreading_speed_m_per_min: number
  spreading_prep_buffer_pct: number
  spreading_workers_per_lay: number
  ply_end_cut_time_s: number
  cutting_cost_per_inch: number
  cutting_speed_cm_per_s: number
  cutting_labor_cost_per_hour: number
  cutting_workers_per_cut: number
  prep_cost_per_marker: number
  prep_cost_per_meter: number
  prep_perf_paper_cost_per_m: number
  prep_perf_paper_enabled: boolean
  prep_underlayer_cost_per_m: number
  prep_underlayer_enabled: boolean
  prep_top_layer_cost_per_m: number
  prep_top_layer_enabled: boolean
  max_ply_height: number
  min_plies_by_bundle: string
  created_at: string
  updated_at: string
}

export interface NestingJobCreate {
  order_id: string
  pattern_id: string
  fabric_width_inches: number
  max_bundle_count?: number
  top_n_results?: number
  full_coverage?: boolean
}

export interface NestingJob {
  id: string
  order_id: string
  pattern_id: string
  status: string
  progress: number
  progress_message?: string
  error_message?: string
  fabric_width_inches?: number
  max_bundle_count: number
  top_n_results: number
  full_coverage: boolean
  results: NestingJobResult[]
  created_at: string
  updated_at: string
}

export interface NestingJobResult {
  id: string
  nesting_job_id: string
  bundle_count: number
  rank: number
  ratio_str: string
  efficiency: number
  length_yards: number
  length_mm?: number
}

export interface Marker {
  id: string
  pattern_id: string
  fabric_id: string
  ratio_str: string
  efficiency: number
  length_yards: number
  source_type: string
}

export interface CutplanOptimizeRequest {
  order_id: string
  solver_type?: string
  penalty?: number
  generate_options?: string[]
  color_code?: string
  fabric_cost_per_yard?: number
}

export interface Cutplan {
  id: string
  order_id: string
  name?: string
  solver_type: string
  status: string
  unique_markers?: number
  total_cuts?: number
  bundle_cuts?: number
  total_plies?: number
  total_yards?: number
  efficiency?: number
  total_cost?: number
  fabric_cost?: number
  spreading_cost?: number
  cutting_cost?: number
  prep_cost?: number
  markers: CutplanMarker[]
  created_at: string
  updated_at: string
}

export interface CutplanMarker {
  id: string
  cutplan_id: string
  marker_id?: string
  ratio_str: string
  efficiency?: number
  length_yards?: number
  plies_by_color: Record<string, number>
  total_plies: number
  cuts: number
}

export interface CostBreakdown {
  total_cost: number
  fabric_cost: number
  spreading_cost: number
  cutting_cost: number
  prep_cost: number
  fabric_yards: number
  total_plies: number
  unique_markers: number
}

export interface OrderImportRow {
  order_number: string
  style_number: string
  fabric_code: string
  color_code: string
  extra_percent: number
  sizes: { [size: string]: number }
}

export interface PatternPiece {
  name: string
  material: string
  quantity: number
  has_left_right: boolean
  left_qty: number
  right_qty: number
  has_grain_line: boolean
  bbox?: {
    width: number
    height: number
    min_x: number
    max_x: number
    min_y: number
    max_y: number
  }
  vertices?: number[][]  // Simplified vertices for preview, normalized to bbox origin
}

export interface PatternPiecesResponse {
  pieces: PatternPiece[]
  total_count: number
  pieces_by_material: Record<string, PatternPiece[]>
}

export interface RefinementConfig {
  piece_buffer_mm: number
  edge_buffer_mm: number
  time_limit_s: number
  rotation_mode: string  // "free" or "nap_safe"
}

export interface MarkerLayout {
  id: string
  cutplan_marker_id: string
  ratio_str: string
  utilization: number
  strip_length_mm: number
  length_yards: number
  computation_time_s: number
  svg_preview: string
  dxf_file_path?: string
  piece_buffer_mm?: number
  edge_buffer_mm?: number
  time_limit_s?: number
  rotation_mode?: string
}

export interface RefinementStatus {
  status: string  // running, completed, failed, cancelled, idle
  progress: number
  message: string
  markers_total: number
  markers_done: number
  layouts: MarkerLayout[]
}
