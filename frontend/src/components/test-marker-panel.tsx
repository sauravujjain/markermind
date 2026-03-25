'use client'

import { useState, useRef, useEffect, useCallback } from 'react'
import { api, Pattern, TestMarkerResponse, SavedTestMarkerResult } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Loader2, FlaskConical, Info, ChevronDown, ChevronUp, Settings2, Plus, Play, Trash2, X, Database, Cloud, Download } from 'lucide-react'

interface TestMarkerPanelProps {
  pattern: Pattern
  fabricWidthInches: number
  orderId?: string
}

/** Snapshot of nesting params captured at queue/nest time */
interface NestParams {
  timeLimit: number
  quadtreeDepth: number
  earlyTermination: boolean
  pieceBuffer: number
  edgeBuffer: number
  orientation: 'free' | 'nap_one_way'
  explorationTime: number | null
  compressionTime: number | null
  useCloud: boolean
  seedScreening: boolean
}

interface QueuedRatio {
  id: string
  sizeBundles: Record<string, number>
  ratioStr: string
  totalBundles: number
  params: NestParams
}

interface MarkerResult {
  id: string
  dbId?: string  // persisted DB id
  ratioStr: string
  sizeBundles: Record<string, number>
  totalBundles: number
  params: NestParams
  result?: TestMarkerResponse
  error?: string
  status: 'pending' | 'running' | 'done' | 'error'
  startedAt?: number
}

let nextId = 0

/** Build a short tag string showing key params */
function paramTag(p: NestParams): string {
  const parts: string[] = []
  parts.push(`qt${p.quadtreeDepth}`)
  if (p.explorationTime != null && p.compressionTime != null) {
    const totalT = p.explorationTime + p.compressionTime
    const ePct = totalT > 0 ? Math.round(p.explorationTime / totalT * 100) : 80
    parts.push(`${ePct}/${100 - ePct}`)
  } else {
    parts.push(`${p.timeLimit}s`)
  }
  if (!p.earlyTermination) parts.push('no-es')
  if (p.pieceBuffer > 0) parts.push(`pb${p.pieceBuffer}`)
  if (p.edgeBuffer > 0) parts.push(`eb${p.edgeBuffer}`)
  if (p.orientation === 'nap_one_way') parts.push('nap')
  if (p.useCloud) parts.push('cloud')
  if (p.seedScreening) parts.push('seed-screen')
  return parts.join(' ')
}

/** Build a unique key combining ratio + params for duplicate detection */
function entryKey(ratioStr: string, params: NestParams): string {
  const timeKey = params.explorationTime != null && params.compressionTime != null
    ? `exp${params.explorationTime}/cmp${params.compressionTime}`
    : `${params.timeLimit}s`
  return `${ratioStr}|qt${params.quadtreeDepth}|${timeKey}|es${params.earlyTermination}|pb${params.pieceBuffer}|eb${params.edgeBuffer}|${params.orientation}|cloud${params.useCloud}|ss${params.seedScreening}`
}

/** Convert a SavedTestMarkerResult to a MarkerResult for display */
function savedToMarkerResult(s: SavedTestMarkerResult): MarkerResult {
  return {
    id: `db-${s.id}`,
    dbId: s.id,
    ratioStr: s.ratio_str,
    sizeBundles: s.size_bundles,
    totalBundles: s.bundle_count,
    params: {
      timeLimit: s.time_limit_s,
      quadtreeDepth: s.quadtree_depth,
      earlyTermination: s.early_termination,
      pieceBuffer: s.piece_buffer_mm,
      edgeBuffer: s.edge_buffer_mm,
      orientation: s.orientation as 'free' | 'nap_one_way',
      explorationTime: s.exploration_time_s,
      compressionTime: s.compression_time_s,
      useCloud: s.use_cloud || false,
      seedScreening: s.seed_screening || false,
    },
    result: {
      id: s.id,
      efficiency: s.efficiency,
      length_mm: s.length_mm,
      length_yards: s.length_yards,
      fabric_width_mm: s.fabric_width_mm,
      piece_count: s.piece_count,
      bundle_count: s.bundle_count,
      ratio_str: s.ratio_str,
      computation_time_ms: s.computation_time_ms,
      svg_preview: null,  // not loaded in list
      exploration_time_s: s.exploration_time_s,
      compression_time_s: s.compression_time_s,
      use_cloud: s.use_cloud || false,
    },
    status: 'done',
  }
}

// Live elapsed timer for running items
function ElapsedTimer({ startedAt }: { startedAt: number }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    const interval = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => clearInterval(interval)
  }, [startedAt])
  const mins = Math.floor(elapsed / 60)
  const secs = elapsed % 60
  return <span>{mins > 0 ? `${mins}m ${secs}s` : `${secs}s`}</span>
}

export function TestMarkerPanel({ pattern, fabricWidthInches, orderId }: TestMarkerPanelProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [fabricWidth, setFabricWidth] = useState(fabricWidthInches)
  const [sizeBundles, setSizeBundles] = useState<Record<string, number>>({})
  const [showAdvanced, setShowAdvanced] = useState(false)

  // Queue and results
  const [queue, setQueue] = useState<QueuedRatio[]>([])
  const [results, setResults] = useState<MarkerResult[]>([])
  const [isRunning, setIsRunning] = useState(false)
  const [runProgress, setRunProgress] = useState({ current: 0, total: 0 })
  const [expandedResult, setExpandedResult] = useState<string | null>(null)
  const [expandedSvg, setExpandedSvg] = useState<string | null>(null)  // lazy-loaded SVG
  const [error, setError] = useState<string | null>(null)
  const cancelRef = useRef(false)
  const [loadedFromDb, setLoadedFromDb] = useState(false)

  // Material selection
  const availableMaterials = pattern.available_materials || []
  const defaultMaterial = availableMaterials.includes('SHELL') ? 'SHELL' : (availableMaterials[0] || '')
  const [material, setMaterial] = useState(defaultMaterial)

  // Nesting parameters
  const [timeLimit, setTimeLimit] = useState(300)
  const [pieceBuffer, setPieceBuffer] = useState(0.0)
  const [edgeBuffer, setEdgeBuffer] = useState(0.0)
  const [orientation, setOrientation] = useState<'free' | 'nap_one_way'>('free')
  const [quadtreeDepth, setQuadtreeDepth] = useState(3)
  const [earlyTermination, setEarlyTermination] = useState(false)
  const [seedScreening, setSeedScreening] = useState(false)

  // Cloud nesting toggle
  const [useCloud, setUseCloud] = useState(false)

  // Explore/compress time split
  const [timeSplitMode, setTimeSplitMode] = useState<'auto' | 'custom'>('auto')
  const [explorePct, setExplorePct] = useState(80)

  const availableSizes = pattern.available_sizes

  // Load saved results from DB on mount
  const loadSavedResults = useCallback(async () => {
    try {
      const saved = await api.getTestMarkers(pattern.id, orderId)
      if (saved.length > 0) {
        setResults(prev => {
          // Merge: keep any in-progress/pending local results, add DB results that aren't already present
          const localIds = new Set(prev.filter(r => r.dbId).map(r => r.dbId))
          const newFromDb = saved.filter(s => !localIds.has(s.id)).map(savedToMarkerResult)
          return [...prev, ...newFromDb]
        })
      }
    } catch {
      // Silently fail — DB results are optional
    }
    setLoadedFromDb(true)
  }, [pattern.id, orderId])

  useEffect(() => {
    if (isOpen && !loadedFromDb) {
      loadSavedResults()
    }
  }, [isOpen, loadedFromDb, loadSavedResults])

  /** Snapshot current params */
  const currentParams = (): NestParams => ({
    timeLimit, quadtreeDepth, earlyTermination,
    pieceBuffer, edgeBuffer, orientation,
    explorationTime: timeSplitMode === 'custom' ? Math.round(timeLimit * explorePct / 100) : null,
    compressionTime: timeSplitMode === 'custom' ? Math.round(timeLimit * (100 - explorePct) / 100) : null,
    useCloud,
    seedScreening,
  })

  const setBundleCount = (size: string, count: number) => {
    if (count < 0) count = 0
    if (count > 12) count = 12
    setSizeBundles(prev => {
      const next = { ...prev }
      if (count === 0) {
        delete next[size]
      } else {
        next[size] = count
      }
      return next
    })
  }

  const totalBundles = Object.values(sizeBundles).reduce((sum, v) => sum + v, 0)
  const selectedSizeCount = Object.keys(sizeBundles).length
  const canAdd = selectedSizeCount > 0 && totalBundles >= 1 && totalBundles <= 20

  const buildRatioStr = (bundles: Record<string, number>) =>
    availableSizes.map(s => bundles[s] || 0).join('-')

  const addToQueue = () => {
    if (!canAdd) return
    const ratioStr = buildRatioStr(sizeBundles)
    const params = currentParams()
    const key = entryKey(ratioStr, params)
    // Check for duplicate (same ratio + same params)
    const isDup = queue.some(q => entryKey(q.ratioStr, q.params) === key)
      || results.some(r => entryKey(r.ratioStr, r.params) === key)
    if (isDup) {
      setError(`Ratio ${ratioStr} with same params already queued or completed`)
      return
    }
    setQueue(prev => [...prev, {
      id: `q${++nextId}`,
      sizeBundles: { ...sizeBundles },
      ratioStr,
      totalBundles,
      params,
    }])
    // Don't clear sizeBundles — user may want to queue same ratio with different params
    setError(null)
  }

  const removeFromQueue = (id: string) => {
    setQueue(prev => prev.filter(q => q.id !== id))
  }

  const clearResults = () => {
    setResults([])
    setExpandedResult(null)
    setExpandedSvg(null)
  }

  const deleteResult = async (r: MarkerResult) => {
    if (r.dbId) {
      try {
        await api.deleteTestMarker(r.dbId)
      } catch {
        // Continue with local removal even if API fails
      }
    }
    setResults(prev => prev.filter(x => x.id !== r.id))
    if (expandedResult === r.id) {
      setExpandedResult(null)
      setExpandedSvg(null)
    }
  }

  const nestWithParams = async (bundles: Record<string, number>, params: NestParams) => {
    return api.testMarker({
      pattern_id: pattern.id,
      fabric_width_inches: fabricWidth,
      size_bundles: bundles,
      material: material || undefined,
      time_limit: params.timeLimit,
      piece_buffer_mm: params.pieceBuffer,
      edge_buffer_mm: params.edgeBuffer,
      orientation: params.orientation,
      quadtree_depth: params.quadtreeDepth,
      early_termination: params.earlyTermination,
      exploration_time_s: params.explorationTime,
      compression_time_s: params.compressionTime,
      order_id: orderId || null,
      use_cloud: params.useCloud,
      seed_screening: params.seedScreening,
    })
  }

  // Nest a single ratio immediately (bypasses queue)
  const handleNestNow = async () => {
    if (!canAdd || isRunning) return
    const ratioStr = buildRatioStr(sizeBundles)
    const params = currentParams()
    const id = `r${++nextId}`
    const entry: MarkerResult = {
      id, ratioStr, sizeBundles: { ...sizeBundles }, totalBundles, params,
      status: 'running', startedAt: Date.now(),
    }
    setResults(prev => [entry, ...prev])
    setIsRunning(true)
    setError(null)
    try {
      const res = await nestWithParams(sizeBundles, params)
      setResults(prev => prev.map(r => r.id === id
        ? { ...r, result: res, dbId: res.id || undefined, status: 'done' } : r))
      setExpandedResult(id)
      if (res.svg_preview) setExpandedSvg(res.svg_preview)
    } catch (e) {
      setResults(prev => prev.map(r => r.id === id
        ? { ...r, error: e instanceof Error ? e.message : 'Failed', status: 'error' } : r))
    } finally {
      setIsRunning(false)
      setSizeBundles({})
    }
  }

  // Nest all queued ratios — cloud in parallel, local sequentially
  const handleNestAll = async () => {
    if (queue.length === 0 || isRunning) return
    setIsRunning(true)
    cancelRef.current = false
    const items = [...queue]
    setQueue([])

    // Partition into cloud (parallel) and local (sequential) items
    const cloudItems = items.filter(q => q.params.useCloud)
    const localItems = items.filter(q => !q.params.useCloud)
    const total = items.length
    let completed = 0
    setRunProgress({ current: 0, total })

    // Add all as pending results
    const newResults: MarkerResult[] = items.map(q => ({
      id: q.id, ratioStr: q.ratioStr, sizeBundles: q.sizeBundles,
      totalBundles: q.totalBundles, params: q.params, status: 'pending' as const,
    }))
    setResults(prev => [...newResults, ...prev])

    // Phase 1: Fire all cloud items concurrently (independent Modal workers)
    if (cloudItems.length > 0) {
      // Mark all cloud items as running simultaneously
      setResults(prev => prev.map(r => {
        if (cloudItems.some(c => c.id === r.id)) {
          return { ...r, status: 'running' as const, startedAt: Date.now() }
        }
        return r
      }))

      const cloudPromises = cloudItems.map(async (item) => {
        try {
          const res = await nestWithParams(item.sizeBundles, item.params)
          completed++
          setRunProgress({ current: completed, total })
          setResults(prev => prev.map(r => r.id === item.id
            ? { ...r, result: res, dbId: res.id || undefined, status: 'done' as const } : r))
        } catch (e) {
          completed++
          setRunProgress({ current: completed, total })
          setResults(prev => prev.map(r => r.id === item.id
            ? { ...r, error: e instanceof Error ? e.message : 'Failed', status: 'error' as const } : r))
        }
      })
      await Promise.all(cloudPromises)
    }

    // Phase 2: Run local items sequentially (shared CPU)
    for (const item of localItems) {
      if (cancelRef.current) break
      setResults(prev => prev.map(r => r.id === item.id
        ? { ...r, status: 'running' as const, startedAt: Date.now() } : r))

      try {
        const res = await nestWithParams(item.sizeBundles, item.params)
        setResults(prev => prev.map(r => r.id === item.id
          ? { ...r, result: res, dbId: res.id || undefined, status: 'done' as const } : r))
      } catch (e) {
        setResults(prev => prev.map(r => r.id === item.id
          ? { ...r, error: e instanceof Error ? e.message : 'Failed', status: 'error' as const } : r))
      }
      completed++
      setRunProgress({ current: completed, total })
    }

    setIsRunning(false)
    setRunProgress({ current: 0, total: 0 })
  }

  const handleCancel = () => {
    cancelRef.current = true
  }

  // Lazy load SVG when expanding a result
  const handleExpandResult = async (r: MarkerResult) => {
    const isExpanded = expandedResult === r.id
    if (isExpanded) {
      setExpandedResult(null)
      setExpandedSvg(null)
      return
    }
    setExpandedResult(r.id)
    // If we already have SVG from the nest response, use it
    if (r.result?.svg_preview) {
      setExpandedSvg(r.result.svg_preview)
      return
    }
    // Otherwise load from DB
    if (r.dbId) {
      setExpandedSvg(null)  // show loading
      try {
        const full = await api.getTestMarker(r.dbId)
        if (full.svg_preview) {
          setExpandedSvg(full.svg_preview)
        }
      } catch {
        setExpandedSvg(null)
      }
    }
  }

  const efficiencyColor = (eff: number) => {
    if (eff >= 0.80) return 'text-green-700'
    if (eff >= 0.70) return 'text-amber-700'
    return 'text-red-700'
  }

  const efficiencyBg = (eff: number) => {
    if (eff >= 0.80) return 'bg-green-50'
    if (eff >= 0.70) return 'bg-amber-50'
    return 'bg-red-50'
  }

  // Sort completed results by efficiency (best first)
  const sortedResults = [...results].sort((a, b) => {
    if (a.status === 'done' && b.status === 'done') {
      return (b.result?.efficiency || 0) - (a.result?.efficiency || 0)
    }
    if (a.status === 'done') return -1
    if (b.status === 'done') return 1
    return 0
  })

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="w-full flex items-center gap-2 px-4 py-3 rounded-lg border border-dashed border-blue-300 bg-blue-50/50 hover:bg-blue-100/50 transition-colors text-sm text-blue-700"
      >
        <FlaskConical className="h-4 w-4" />
        <span className="font-medium">Quick Test Marker</span>
        <span className="text-blue-500 ml-1">— batch test marker ratios with CPU vector nest</span>
      </button>
    )
  }

  return (
    <Card className="border-blue-200 bg-blue-50/30">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="h-8 w-8 rounded-lg bg-gradient-to-br from-blue-500 to-blue-600 flex items-center justify-center">
              <FlaskConical className="h-4 w-4 text-white" />
            </div>
            <div>
              <CardTitle className="text-base">Quick Test Marker</CardTitle>
              <CardDescription className="text-xs">
                CPU vector nest (Spyrrow) — results auto-saved to DB
              </CardDescription>
            </div>
          </div>
          <button
            onClick={() => setIsOpen(false)}
            className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded"
          >
            Close
          </button>
        </div>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {/* Row 1: Fabric width + Material */}
          <div className="flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <Label className="text-xs whitespace-nowrap">Width</Label>
              <div className="flex items-center gap-1">
                <Input
                  type="number"
                  value={fabricWidth}
                  onChange={e => { const v = e.target.value; setFabricWidth(v === '' ? '' as any : Number(v)) }}
                  className="w-20 h-8 text-sm"
                  min={20} max={120}
                />
                <span className="text-xs text-muted-foreground">in</span>
              </div>
            </div>
            {availableMaterials.length > 1 && (
              <div className="flex items-center gap-2">
                <Label className="text-xs whitespace-nowrap">Material</Label>
                <div className="flex flex-wrap gap-1">
                  {availableMaterials.map(mat => (
                    <button
                      key={mat}
                      onClick={() => setMaterial(mat)}
                      className={`px-2 py-0.5 rounded text-xs font-medium border transition-colors ${
                        material === mat
                          ? 'bg-blue-600 text-white border-blue-600'
                          : 'bg-white text-foreground border-border hover:border-blue-400'
                      }`}
                    >
                      {mat}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Nesting Parameters — above ratio picker */}
          <button
            onClick={() => setShowAdvanced(!showAdvanced)}
            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <Settings2 className="h-3.5 w-3.5" />
            <span>Nesting Parameters</span>
            <span className="ml-1 text-[10px] font-mono text-blue-500">{paramTag(currentParams())}</span>
            {showAdvanced ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>

          {showAdvanced && (
            <div className="space-y-3 pl-1 border-l-2 border-blue-200 ml-1 py-1">
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Max Time</Label>
                <div className="flex items-center gap-1">
                  <Input
                    type="number" value={timeLimit}
                    onChange={e => { const v = e.target.value; setTimeLimit(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm" min={10} max={600} step={10}
                  />
                  <span className="text-xs text-muted-foreground">sec</span>
                </div>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input type="checkbox" checked={earlyTermination} onChange={e => setEarlyTermination(e.target.checked)} className="rounded" />
                  Early stop
                </label>
              </div>

              {/* Time Split: Auto vs Custom */}
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Time Split</Label>
                <div className="flex gap-1">
                  <button onClick={() => setTimeSplitMode('auto')}
                    className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                      timeSplitMode === 'auto' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}>Auto</button>
                  <button onClick={() => setTimeSplitMode('custom')}
                    className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                      timeSplitMode === 'custom' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}>Custom</button>
                </div>
                {timeSplitMode === 'auto' && (
                  <span className="text-[10px] text-muted-foreground">solver decides (80/20 default)</span>
                )}
              </div>
              {timeSplitMode === 'custom' && (
                <div className="flex items-center gap-3 ml-[7.5rem]">
                  <div className="flex items-center gap-1">
                    <span className="text-xs text-muted-foreground">Explore</span>
                    <Input type="number" value={explorePct}
                      onChange={e => { const v = e.target.value; setExplorePct(v === '' ? '' as any : Math.max(1, Math.min(99, Number(v)))) }}
                      className="w-14 h-7 text-sm" min={1} max={99} step={5}
                    />
                    <span className="text-xs text-muted-foreground">%</span>
                  </div>
                  <span className="text-[10px] text-muted-foreground font-mono">
                    → {Math.round(timeLimit * explorePct / 100)}s explore / {Math.round(timeLimit * (100 - explorePct) / 100)}s compress
                  </span>
                </div>
              )}

              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Quadtree Depth</Label>
                <div className="flex gap-1">
                  {[2, 3, 4, 5, 6].map(d => (
                    <button key={d} onClick={() => setQuadtreeDepth(d)}
                      className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                        quadtreeDepth === d ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                      }`}
                    >{d}</button>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Piece Buffer</Label>
                <div className="flex items-center gap-1">
                  <Input type="number" value={pieceBuffer}
                    onChange={e => { const v = e.target.value; setPieceBuffer(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm" min={0} max={10} step={0.5}
                  /><span className="text-xs text-muted-foreground">mm</span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Edge Buffer</Label>
                <div className="flex items-center gap-1">
                  <Input type="number" value={edgeBuffer}
                    onChange={e => { const v = e.target.value; setEdgeBuffer(v === '' ? '' as any : Number(v)) }}
                    className="w-16 h-7 text-sm" min={0} max={20} step={0.5}
                  /><span className="text-xs text-muted-foreground">mm</span>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Orientation</Label>
                <div className="flex gap-1">
                  <button onClick={() => setOrientation('free')}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                      orientation === 'free' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}>Free (0/180)</button>
                  <button onClick={() => setOrientation('nap_one_way')}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                      orientation === 'nap_one_way' ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}>Nap One-Way (0 only)</button>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Cloud Nesting</Label>
                <div className="flex gap-1">
                  <button onClick={() => setUseCloud(false)}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
                      !useCloud ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}>Local CPU</button>
                  <button onClick={() => setUseCloud(true)}
                    className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors flex items-center gap-1 ${
                      useCloud ? 'bg-blue-600 text-white border-blue-600' : 'bg-white text-foreground border-border hover:border-blue-400'
                    }`}><Cloud className="h-3 w-3" />Surface CPU</button>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <Label className="text-xs whitespace-nowrap w-28">Seed Selection</Label>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                  <input type="checkbox" checked={seedScreening}
                    onChange={e => setSeedScreening(e.target.checked)} className="rounded" />
                  Screen 6 seeds (adds ~60s)
                </label>
              </div>
            </div>
          )}

          {/* Compact ratio picker — horizontal */}
          <div>
            <Label className="text-xs mb-2 block">Marker Ratio — click sizes and set bundle counts</Label>
            {availableSizes.length === 0 ? (
              <div className="text-xs text-muted-foreground italic py-2">No sizes available</div>
            ) : (
              <div className="flex flex-wrap gap-1.5 items-center">
                {availableSizes.map(size => {
                  const count = sizeBundles[size] || 0
                  const isSelected = count > 0
                  return (
                    <div key={size} className={`flex items-center gap-0.5 rounded-lg border px-1.5 py-1 transition-colors ${
                      isSelected ? 'bg-blue-100 border-blue-300' : 'bg-white border-border'
                    }`}>
                      <button
                        onClick={() => setBundleCount(size, count > 0 ? 0 : 1)}
                        className={`px-1.5 py-0.5 rounded text-xs font-medium transition-colors ${
                          isSelected ? 'text-blue-800' : 'text-muted-foreground hover:text-foreground'
                        }`}
                      >
                        {size}
                      </button>
                      {isSelected && (
                        <div className="flex items-center gap-0.5">
                          <button
                            onClick={() => setBundleCount(size, count - 1)}
                            className="h-5 w-5 rounded text-xs font-medium hover:bg-blue-200 flex items-center justify-center"
                          >-</button>
                          <span className="w-4 text-center text-xs font-bold text-blue-700">{count}</span>
                          <button
                            onClick={() => setBundleCount(size, count + 1)}
                            disabled={totalBundles >= 20}
                            className="h-5 w-5 rounded text-xs font-medium hover:bg-blue-200 flex items-center justify-center disabled:opacity-30"
                          >+</button>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
            {selectedSizeCount > 0 && (
              <p className="text-xs text-muted-foreground mt-1.5">
                Ratio: <span className="font-mono font-medium">{buildRatioStr(sizeBundles)}</span>
                {' '}({totalBundles} bundle{totalBundles !== 1 ? 's' : ''})
                {totalBundles > 20 && <span className="text-red-600 ml-1">(max 20)</span>}
              </p>
            )}
          </div>

          {/* Action buttons: Add to Queue + Nest Now */}
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={addToQueue}
              disabled={!canAdd || isRunning}
              className="flex-1"
            >
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Add to Queue
            </Button>
            <Button
              size="sm"
              onClick={handleNestNow}
              disabled={!canAdd || isRunning}
              className="flex-1"
            >
              {isRunning ? (
                <><Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />Nesting...</>
              ) : (
                <><FlaskConical className="mr-1.5 h-3.5 w-3.5" />Nest Now</>
              )}
            </Button>
          </div>

          {/* Error */}
          {error && (
            <div className="p-2 rounded-lg bg-red-50 border border-red-200 text-xs text-red-700 flex items-center justify-between">
              <span>{error}</span>
              <button onClick={() => setError(null)} className="ml-2"><X className="h-3 w-3" /></button>
            </div>
          )}

          {/* Queue */}
          {queue.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-muted-foreground">{queue.length} queued</span>
                <Button
                  size="sm"
                  onClick={handleNestAll}
                  disabled={isRunning}
                >
                  <Play className="mr-1.5 h-3.5 w-3.5" />
                  Nest All ({queue.length})
                </Button>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {queue.map(q => (
                  <div key={q.id} className="flex items-center gap-1 px-2 py-1 rounded-md bg-blue-100 border border-blue-200 text-xs">
                    <span className="font-mono">{q.ratioStr}</span>
                    <span className="text-blue-500">({q.totalBundles}b)</span>
                    <span className="text-[10px] font-mono text-blue-600/70 bg-blue-50 px-1 rounded">{paramTag(q.params)}</span>
                    <button
                      onClick={() => removeFromQueue(q.id)}
                      className="ml-0.5 text-blue-400 hover:text-red-500"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Running progress */}
          {isRunning && runProgress.total > 0 && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">Nesting {runProgress.current}/{runProgress.total}...</span>
                <Button variant="outline" size="sm" onClick={handleCancel} className="h-6 text-xs text-red-600 border-red-200">
                  Cancel
                </Button>
              </div>
              <div className="w-full bg-blue-100 rounded-full h-1.5">
                <div
                  className="bg-blue-500 h-1.5 rounded-full transition-all duration-500"
                  style={{ width: `${Math.max((runProgress.current / runProgress.total) * 100, 3)}%` }}
                />
              </div>
            </div>
          )}

          {/* Results Table */}
          {results.length > 0 && (
            <div className="space-y-2 pt-2 border-t border-blue-200">
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold flex items-center gap-1.5">
                  Results ({results.filter(r => r.status === 'done').length} completed)
                  {results.some(r => r.dbId) && (
                    <Database className="h-3 w-3 text-blue-500" />
                  )}
                </span>
                <button onClick={clearResults} className="text-xs text-muted-foreground hover:text-red-600 flex items-center gap-1">
                  <Trash2 className="h-3 w-3" /> Clear
                </button>
              </div>

              <div className="border rounded-lg overflow-hidden text-xs">
                {/* Header */}
                <div className="flex items-center bg-muted/40 text-muted-foreground font-medium">
                  <span className="text-left px-2 py-1.5 w-[110px] shrink-0">Ratio</span>
                  <span className="text-center px-1 py-1.5 w-8 shrink-0">B</span>
                  <span className="text-left px-1 py-1.5 flex-1 min-w-0">Params</span>
                  <span className="text-center px-1 py-1.5 w-12 shrink-0">Util%</span>
                  <span className="text-center px-1 py-1.5 w-14 shrink-0">Yards</span>
                  <span className="text-center px-1 py-1.5 w-14 shrink-0">W&Prime;</span>
                  <span className="text-center px-1 py-1.5 w-8 shrink-0">Pcs</span>
                  <span className="text-center px-1 py-1.5 w-14 shrink-0">Time</span>
                  <span className="w-6 shrink-0"></span>
                </div>
                {/* Rows */}
                {sortedResults.map((r, idx) => {
                  const isExpanded = expandedResult === r.id
                  const isDone = r.status === 'done' && r.result
                  const isBest = idx === 0 && isDone
                  return (
                    <div key={r.id}>
                      <div className={`flex items-center border-t transition-colors ${
                        isExpanded ? 'bg-blue-50/60' : ''} ${isBest && isDone ? efficiencyBg(r.result!.efficiency) : ''}`}>
                        <button
                          onClick={() => isDone && handleExpandResult(r)}
                          className={`flex items-center flex-1 min-w-0 ${
                            isDone ? 'cursor-pointer hover:bg-blue-50/50' : 'cursor-default'
                          }`}
                          disabled={!isDone}
                        >
                          <span className="text-left px-2 py-1.5 font-mono w-[110px] shrink-0 truncate">
                            {r.ratioStr}
                          </span>
                          <span className="text-center px-1 py-1.5 w-8 shrink-0">{r.totalBundles}</span>
                          <span className="text-left px-1 py-1.5 flex-1 min-w-0 truncate">
                            <span className="text-[10px] font-mono text-muted-foreground bg-muted/40 px-1 rounded">{paramTag(r.params)}</span>
                          </span>
                          {r.status === 'done' && r.result ? (
                            <>
                              <span className={`text-center px-1 py-1.5 w-12 shrink-0 font-bold ${efficiencyColor(r.result.efficiency)}`}>
                                {(r.result.efficiency * 100).toFixed(1)}
                              </span>
                              <span className="text-center px-1 py-1.5 w-14 shrink-0">{r.result.length_yards.toFixed(3)}</span>
                              <span className="text-center px-1 py-1.5 w-14 shrink-0 text-muted-foreground">{(r.result.fabric_width_mm / 25.4).toFixed(3)}</span>
                              <span className="text-center px-1 py-1.5 w-8 shrink-0">{r.result.piece_count}</span>
                              <span className="text-center px-1 py-1.5 w-14 shrink-0 text-muted-foreground">
                                {(r.result.computation_time_ms / 1000).toFixed(1)}s
                              </span>
                            </>
                          ) : r.status === 'running' ? (
                            <span className="text-center px-1 py-1.5 flex-1 text-blue-600 flex items-center justify-center gap-1.5">
                              <Loader2 className="h-3 w-3 animate-spin" />
                              Nesting... {r.startedAt && <ElapsedTimer startedAt={r.startedAt} />}
                            </span>
                          ) : r.status === 'error' ? (
                            <span className="text-center px-1 py-1.5 flex-1 text-red-600 truncate">{r.error}</span>
                          ) : (
                            <span className="text-center px-1 py-1.5 flex-1 text-muted-foreground">Queued</span>
                          )}
                        </button>
                        {isDone && (
                          <button
                            onClick={(e) => { e.stopPropagation(); deleteResult(r) }}
                            className="w-6 shrink-0 flex items-center justify-center text-muted-foreground hover:text-red-500 transition-colors"
                            title="Delete result"
                          >
                            <X className="h-3 w-3" />
                          </button>
                        )}
                      </div>
                      {/* Expanded SVG preview */}
                      {isExpanded && isDone && (
                        <div className="px-2 pb-2 bg-blue-50/30 border-t border-blue-100">
                          {expandedSvg ? (
                            <>
                              <div
                                className="rounded-lg border bg-white p-2 overflow-x-auto mt-1"
                                dangerouslySetInnerHTML={{ __html: expandedSvg }}
                              />
                              {r.dbId && (
                                <div className="flex justify-end mt-1">
                                  <button
                                    onClick={async (e) => {
                                      e.stopPropagation()
                                      try {
                                        const blob = await api.downloadTestMarkerDxf(r.dbId!)
                                        const url = URL.createObjectURL(blob)
                                        const a = document.createElement('a')
                                        a.href = url
                                        a.download = `marker_${r.ratioStr}_${((r.result?.efficiency ?? 0) * 100).toFixed(0)}pct.dxf`
                                        a.click()
                                        URL.revokeObjectURL(url)
                                      } catch (err: any) {
                                        console.error('DXF download failed:', err)
                                      }
                                    }}
                                    className="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800 transition-colors px-2 py-1 rounded hover:bg-blue-50"
                                    title="Download marker as DXF"
                                  >
                                    <Download className="h-3 w-3" />
                                    DXF
                                  </button>
                                </div>
                              )}
                            </>
                          ) : (
                            <div className="flex items-center justify-center py-4 text-xs text-muted-foreground gap-1.5">
                              <Loader2 className="h-3 w-3 animate-spin" />
                              Loading preview...
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>

              <div className="flex items-start gap-1.5 text-xs text-muted-foreground bg-muted/20 rounded-md p-2">
                <Info className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                <span>Click a row to expand/collapse the marker preview. Results sorted by utilization (best first). Results are auto-saved and persist across sessions.</span>
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
