'use client'

import React, { useEffect, useState, useRef } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { api, RefinementConfig, RefinementStatus, MarkerLayout } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useToast } from '@/hooks/use-toast'
import { useOrderContext } from '../order-context'
import {
  Play,
  CheckCircle2,
  Loader2,
  Layers,
  XCircle,
  Download,
  Settings,
  ChevronDown,
  ChevronRight,
  Eye,
  EyeOff,
  History,
  Trash2,
} from 'lucide-react'

export default function CutplanPage() {
  const router = useRouter()
  const { toast } = useToast()
  const {
    order,
    orderId,
    patterns,
    fabrics,
    nestingJobs,
    cutplans,
    hasNestingResults,
    orderSizes,
    loadData,
  } = useOrderContext()

  // Local state
  const [showCutplanConfig, setShowCutplanConfig] = useState(false)
  const [cutplanConfig, setCutplanConfig] = useState({
    maxPlyHeight: 100,
    fabricCostPerYard: 3.0,
    strategies: ['max_efficiency', 'balanced', 'min_bundle_cuts', 'endbit_optimized'] as string[],
    selectedColor: '' as string,  // '' = auto-detect on mount
    minPliesByBundle: '6:50,5:40,4:30,3:10,2:1,1:1',
  })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [isGeneratingCutplan, setIsGeneratingCutplan] = useState(false)
  const [cutplanOptStatus, setCutplanOptStatus] = useState<{
    status: string; progress: number; message: string; strategies_total: number; strategies_done: number; phase?: string
  } | null>(null)

  // Per-cutplan refinement state
  const [refinementConfigs, setRefinementConfigs] = useState<Record<string, RefinementConfig>>({})
  const [refiningCutplans, setRefiningCutplans] = useState<Record<string, boolean>>({})
  const [refinementStatuses, setRefinementStatuses] = useState<Record<string, RefinementStatus>>({})

  // Excel export option
  const [includeMarkerImages, setIncludeMarkerImages] = useState(false)

  // History toggle
  const [showHistory, setShowHistory] = useState(false)

  // Split active vs superseded cutplans
  const activeCutplans = cutplans.filter(c => c.status !== 'superseded')
  const supersededCutplans = cutplans.filter(c => c.status === 'superseded')

  // (removed: expandedMarkers — refined layouts now shown in unified marker table)

  // Per-cutplan expanded marker preview tracking (pre-refinement, GPU SVGs)
  const [previewMarkers, setPreviewMarkers] = useState<Record<string, Set<number>>>({})

  // Per-cutplan advanced refinement toggle
  const [showRefAdvanced, setShowRefAdvanced] = useState<Record<string, boolean>>({})

  const cutplanPollingRef = useRef<NodeJS.Timeout | null>(null)
  const refinementPollingRefs = useRef<Record<string, NodeJS.Timeout>>({})
  const lastStrategiesDone = useRef(0)

  const getRefinementConfig = (cpId: string): RefinementConfig =>
    refinementConfigs[cpId] || { piece_buffer_mm: 0.0, edge_buffer_mm: 0.0, time_limit_s: 120, rotation_mode: 'free', quadtree_depth: 3, early_termination: false, exploration_time_s: null, compression_time_s: null, seed_screening: false, use_cloud: true }

  const setRefinementConfig = (cpId: string, config: RefinementConfig) =>
    setRefinementConfigs(prev => ({ ...prev, [cpId]: config }))

  // Load cost config defaults on mount (max_ply_height, fabric cost, etc.)
  useEffect(() => {
    api.getCostConfig().then((config: any) => {
      setCutplanConfig(prev => ({
        ...prev,
        maxPlyHeight: config.max_ply_height || prev.maxPlyHeight,
        fabricCostPerYard: config.fabric_cost_per_yard ?? prev.fabricCostPerYard,
        minPliesByBundle: config.min_plies_by_bundle || prev.minPliesByBundle,
      }))
    }).catch(() => {})
  }, [])

  // Auto-default to single color if order has only one color
  useEffect(() => {
    if (!order || cutplanConfig.selectedColor !== '') return
    const colors = Array.from(new Set(order.order_lines.map(l => l.color_code)))
    setCutplanConfig(prev => ({
      ...prev,
      selectedColor: colors.length === 1 ? colors[0] : colors[0],  // default to first (single) color
    }))
  }, [order])

  // Check if cutplan optimization is already running on load
  useEffect(() => {
    if (!orderId) return
    api.getCutplanOptimizeStatus(orderId).then(status => {
      if (status.status === 'running') {
        setCutplanOptStatus(status)
        setIsGeneratingCutplan(true)
        startCutplanPolling()
      }
    }).catch(() => {})
  }, [orderId])

  // Check if refinement is already running on load
  useEffect(() => {
    if (!orderId) return
    const eligiblePlans = activeCutplans.filter(c => c.status === 'approved' || c.status === 'refining' || c.status === 'refined')
    for (const plan of eligiblePlans) {
      if (plan.status === 'refining' || plan.status === 'refined') {
        api.getRefinementStatus(plan.id).then(status => {
          setRefinementStatuses(prev => ({ ...prev, [plan.id]: status }))
          if (status.status === 'running') {
            setRefiningCutplans(prev => ({ ...prev, [plan.id]: true }))
            startRefinementPolling(plan.id)
          }
        }).catch(() => {})
      }
    }
  }, [orderId, activeCutplans.length])

  // Auto-show config panel if nesting results exist but no cutplans
  useEffect(() => {
    if (hasNestingResults && activeCutplans.length === 0 && !isGeneratingCutplan) {
      setShowCutplanConfig(true)
    }
  }, [hasNestingResults, activeCutplans.length, isGeneratingCutplan])

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (cutplanPollingRef.current) clearInterval(cutplanPollingRef.current)
      Object.values(refinementPollingRefs.current).forEach(ref => clearInterval(ref))
      refinementPollingRefs.current = {}
    }
  }, [])

  const startCutplanPolling = () => {
    if (cutplanPollingRef.current) clearInterval(cutplanPollingRef.current)
    lastStrategiesDone.current = 0
    cutplanPollingRef.current = setInterval(async () => {
      try {
        const status = await api.getCutplanOptimizeStatus(orderId)
        setCutplanOptStatus(status)

        // Reload cutplans when a new strategy completes (incremental display)
        // Also reload during cost estimation phase to pick up per-cutplan updates
        if (status.strategies_done > lastStrategiesDone.current || status.phase === 'cpu_nesting') {
          lastStrategiesDone.current = status.strategies_done
          loadData()
        }

        if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
          if (cutplanPollingRef.current) clearInterval(cutplanPollingRef.current)
          setIsGeneratingCutplan(false)
          loadData()
          if (status.status === 'completed') {
            toast({ title: 'Cutplan optimization complete', description: status.message })
          } else if (status.status === 'failed') {
            toast({ title: 'Cutplan optimization failed', description: status.message, variant: 'destructive' })
          }
        }
      } catch (e) {
        console.error('Cutplan polling error:', e)
      }
    }, 2000)
  }

  const handleOptimizeCutplan = async () => {
    setIsGeneratingCutplan(true)
    const totalStrategies = cutplanConfig.strategies.length
    setCutplanOptStatus({ status: 'running', progress: 0, message: 'Starting...', strategies_total: totalStrategies, strategies_done: 0 })
    try {
      await api.optimizeCutplan({
        order_id: orderId,
        generate_options: cutplanConfig.strategies,
        penalty: 5.0,
        fabric_cost_per_yard: cutplanConfig.fabricCostPerYard,
        max_ply_height: cutplanConfig.maxPlyHeight,
        min_plies_by_bundle: cutplanConfig.minPliesByBundle,
        cost_metric: 'length',
        ...(cutplanConfig.selectedColor !== 'all' ? { color_code: cutplanConfig.selectedColor } : {}),
      })
      setShowCutplanConfig(false)
      startCutplanPolling()
    } catch (error) {
      toast({
        title: 'Failed to start cutplan optimization',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
      setIsGeneratingCutplan(false)
      setCutplanOptStatus(null)
    }
  }

  const handleCancelCutplan = async () => {
    try {
      await api.cancelCutplanOptimize(orderId)
      toast({ title: 'Cancellation requested', description: 'The solver will stop after the current strategy' })
    } catch (error) {
      toast({ title: 'Failed to cancel', description: error instanceof Error ? error.message : 'Please try again', variant: 'destructive' })
    }
  }

  const handleStartRefinement = async (cutplanId: string) => {
    setRefiningCutplans(prev => ({ ...prev, [cutplanId]: true }))
    setRefinementStatuses(prev => {
      const next = { ...prev }
      delete next[cutplanId]
      return next
    })
    try {
      await api.startRefinement(cutplanId, getRefinementConfig(cutplanId))
      toast({ title: 'Final nesting started' })
      startRefinementPolling(cutplanId)
    } catch (error) {
      toast({
        title: 'Failed to start final nesting',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
      setRefiningCutplans(prev => ({ ...prev, [cutplanId]: false }))
    }
  }

  const startRefinementPolling = (cutplanId: string) => {
    if (refinementPollingRefs.current[cutplanId]) clearInterval(refinementPollingRefs.current[cutplanId])
    refinementPollingRefs.current[cutplanId] = setInterval(async () => {
      try {
        const status = await api.getRefinementStatus(cutplanId)
        setRefinementStatuses(prev => ({ ...prev, [cutplanId]: status }))

        if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
          if (refinementPollingRefs.current[cutplanId]) clearInterval(refinementPollingRefs.current[cutplanId])
          delete refinementPollingRefs.current[cutplanId]
          setRefiningCutplans(prev => ({ ...prev, [cutplanId]: false }))
          loadData()
          if (status.status === 'completed') {
            toast({ title: 'Final nesting complete', description: status.message })
          } else if (status.status === 'failed') {
            toast({ title: 'Final nesting failed', description: status.message, variant: 'destructive' })
          }
        }
      } catch (e) {
        console.error('Refinement polling error:', e)
      }
    }, 2000)
  }

  const handleCancelRefinement = async (cutplanId: string) => {
    try {
      await api.cancelRefinement(cutplanId)
      toast({ title: 'Cancellation requested' })
    } catch (error) {
      toast({ title: 'Failed to cancel', variant: 'destructive' })
    }
  }

  const handleDownloadMarkers = async (cutplanId: string) => {
    try {
      const blob = await api.downloadMarkersDxf(cutplanId)
      const url = window.URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'markers.zip'
      document.body.appendChild(a)
      a.click()
      window.URL.revokeObjectURL(url)
      document.body.removeChild(a)
    } catch (error) {
      toast({
        title: 'Download failed',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    }
  }

  if (!order) return null

  // Sizes in Excel column order (from context, backed by sort_order in DB)
  const sizes = orderSizes

  // Empty state — no nesting results
  if (!hasNestingResults) {
    return (
      <Card>
        <CardContent className="py-12 text-center">
          <Settings className="h-12 w-12 mx-auto text-muted-foreground/30 mb-4" />
          <h3 className="font-semibold text-lg mb-2">No Nesting Results Yet</h3>
          <p className="text-muted-foreground mb-4">
            Run GPU nesting first to generate markers, then come back here to create cutplan options.
          </p>
          <Button onClick={() => router.push(`/orders/${orderId}/configure`)}>
            <Play className="mr-2 h-4 w-4" />
            Go to Configure & Nest
          </Button>
        </CardContent>
      </Card>
    )
  }

  return (
    <>
      {/* Nesting Results Summary Banner */}
      <Card className="border-green-200 bg-green-50/30">
        <CardContent className="py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <div className="h-9 w-9 rounded-lg bg-gradient-to-br from-green-500 to-green-600 flex items-center justify-center">
                <CheckCircle2 className="h-4 w-4 text-white" />
              </div>
              <div>
                <div className="font-semibold text-sm">Nesting Complete</div>
                <div className="text-xs text-muted-foreground flex gap-3 mt-0.5">
                  {(() => {
                    const completedJobs = nestingJobs.filter(j => j.status === 'completed')
                    const totalMarkers = completedJobs.reduce((sum, j) => sum + (j.results?.length || 0), 0)
                    const bestEff = Math.max(...completedJobs.flatMap(j => (j.results || []).map(r => r.efficiency)))
                    const bundleCounts = Array.from(new Set(completedJobs.flatMap(j => (j.results || []).map(r => r.bundle_count)))).sort((a, b) => a - b)
                    return (
                      <>
                        <span>{totalMarkers} markers</span>
                        <span>{bundleCounts.join(',')}-bundle ratios</span>
                        <span className="text-green-600 font-medium">Best: {(bestEff * 100).toFixed(1)}%</span>
                      </>
                    )
                  })()}
                </div>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => router.push(`/orders/${orderId}/configure`)}>
                Re-run
              </Button>
              <Button size="sm" onClick={() => setShowCutplanConfig(true)}>
                Generate Cutplan Options
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Cutplan Configuration */}
      {showCutplanConfig && (
        <Card id="cutplan-config-section" className="border-primary/30">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle>Generate Cutplan Options</CardTitle>
                <CardDescription>
                  Configure parameters for cutplan optimization
                </CardDescription>
              </div>
              <Button variant="ghost" size="sm" onClick={() => setShowCutplanConfig(false)}>
                Cancel
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-6">
              {/* Color Selection */}
              {order.order_lines.length > 0 && (
                <div>
                  <label className="text-sm font-medium mb-2 block">Color</label>
                  <div className="flex flex-wrap gap-2">
                    {Array.from(new Set(order.order_lines.map(l => l.color_code))).length > 1 && (
                    <button
                      onClick={() => setCutplanConfig({ ...cutplanConfig, selectedColor: 'all' })}
                      className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                        cutplanConfig.selectedColor === 'all'
                          ? 'bg-primary text-primary-foreground border-primary'
                          : 'bg-muted/30 hover:bg-muted border-border'
                      }`}
                    >
                      <div className="font-medium">Multicolor (Rainbow)</div>
                      <div className={`text-xs ${cutplanConfig.selectedColor === 'all' ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                        Aggregate demand across all colors
                      </div>
                    </button>
                    )}
                    {Array.from(new Set(order.order_lines.map(l => l.color_code))).map((color) => {
                      const colorLines = order.order_lines.filter(l => l.color_code === color)
                      const firstLine = colorLines[0]
                      const perFabricQty = firstLine.size_quantities.reduce((s, sq) => s + sq.quantity, 0)
                      const fabricCount = Array.from(new Set(colorLines.map(l => l.fabric_code))).length
                      return (
                        <button
                          key={color}
                          onClick={() => setCutplanConfig({ ...cutplanConfig, selectedColor: color })}
                          className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                            cutplanConfig.selectedColor === color
                              ? 'bg-primary text-primary-foreground border-primary'
                              : 'bg-muted/30 hover:bg-muted border-border'
                          }`}
                        >
                          <div className="font-medium">{color}</div>
                          <div className={`text-xs ${cutplanConfig.selectedColor === color ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                            {perFabricQty.toLocaleString()} garments · {fabricCount} fabric{fabricCount > 1 ? 's' : ''}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                  <p className="text-xs text-muted-foreground mt-2">
                    Select a specific color to optimize, or &quot;All Colors&quot; to aggregate demand
                  </p>
                </div>
              )}

              {/* Input Parameters */}
              <div className="grid gap-4 sm:grid-cols-2">
                <div>
                  <label className="text-sm font-medium mb-2 block">Max Ply Height</label>
                  <input
                    type="number"
                    value={cutplanConfig.maxPlyHeight}
                    onChange={(e) => {
                      const v = e.target.value
                      setCutplanConfig({ ...cutplanConfig, maxPlyHeight: v === '' ? '' as any : parseInt(v) })
                    }}
                    className="w-full px-3 py-2 border rounded-md text-sm"
                    min={1}
                    max={200}
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Maximum layers per cutting operation (default: 100)
                  </p>
                </div>
                <div>
                  <label className="text-sm font-medium mb-2 block">Fabric Cost ($/yard, default: $3)</label>
                  <input
                    type="number"
                    value={cutplanConfig.fabricCostPerYard}
                    onChange={(e) => {
                      const v = e.target.value
                      setCutplanConfig({ ...cutplanConfig, fabricCostPerYard: v === '' ? '' as any : parseFloat(v) })
                    }}
                    className="w-full px-3 py-2 border rounded-md text-sm"
                    min={0}
                    step={0.5}
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Cost per yard for fabric cost calculation
                  </p>
                </div>
              </div>

              {/* Strategy Selection */}
              <div>
                <label className="text-sm font-medium mb-2 block">Optimization Strategies</label>
                <div className="flex flex-wrap gap-2">
                  {[
                    { id: 'max_efficiency', label: 'Max Efficiency', desc: 'Best fabric utilization' },
                    { id: 'balanced', label: 'Balanced', desc: 'Efficiency vs marker count trade-off' },
                    { id: 'min_bundle_cuts', label: 'Min Cutting Work', desc: 'Least cutting operations' },
                    { id: 'endbit_optimized', label: 'EndBit Optimized', desc: 'Long markers + end-bit recovery' },
                  ].map((strategy) => {
                    const isSelected = cutplanConfig.strategies.includes(strategy.id)
                    return (
                      <button
                        key={strategy.id}
                        onClick={() => {
                          if (isSelected) {
                            setCutplanConfig({ ...cutplanConfig, strategies: cutplanConfig.strategies.filter(s => s !== strategy.id) })
                          } else {
                            setCutplanConfig({ ...cutplanConfig, strategies: [...cutplanConfig.strategies, strategy.id] })
                          }
                        }}
                        className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                          isSelected
                            ? 'bg-primary text-primary-foreground border-primary'
                            : 'bg-muted/30 hover:bg-muted border-border'
                        }`}
                      >
                        <div className="font-medium">{strategy.label}</div>
                        <div className={`text-xs ${isSelected ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                          {strategy.desc}
                        </div>
                      </button>
                    )
                  })}
                </div>
                <p className="text-xs text-muted-foreground mt-2">
                  Select one or more strategies to generate cutplan options
                </p>
              </div>

              {/* Advanced Settings */}
              <div>
                <button
                  onClick={() => setShowAdvanced(!showAdvanced)}
                  className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1"
                >
                  <span className={`transition-transform ${showAdvanced ? 'rotate-90' : ''}`}>▶</span>
                  Advanced Settings
                </button>
                {showAdvanced && (
                  <div className="mt-3 p-3 rounded-lg border bg-muted/20 space-y-3">
                    <div>
                      <label className="text-xs font-medium mb-1 block">Min Plies by Bundle Count</label>
                      <p className="text-[10px] text-muted-foreground mb-2">
                        Minimum plies required per marker based on its bundle count. Prevents wasteful small-ply large markers.
                      </p>
                      <div className="grid grid-cols-6 gap-2">
                        {(() => {
                          const parsed: Record<string, string> = {}
                          cutplanConfig.minPliesByBundle.split(',').forEach(part => {
                            const [bc, mp] = part.trim().split(':')
                            if (bc && mp) parsed[bc] = mp
                          })
                          return ['6', '5', '4', '3', '2', '1'].map(bc => (
                            <div key={bc} className="text-center">
                              <div className="text-[10px] text-muted-foreground mb-0.5">{bc}-bndl</div>
                              <input
                                type="number"
                                value={parsed[bc] || '1'}
                                onChange={(e) => {
                                  const newParsed = { ...parsed, [bc]: e.target.value || '1' }
                                  const newStr = ['6', '5', '4', '3', '2', '1']
                                    .map(b => `${b}:${newParsed[b] || '1'}`)
                                    .join(',')
                                  setCutplanConfig({ ...cutplanConfig, minPliesByBundle: newStr })
                                }}
                                className="w-full px-1.5 py-1 border rounded text-xs text-center"
                                min={1}
                                max={200}
                              />
                            </div>
                          ))
                        })()}
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Generate Button */}
              <div className="flex justify-end gap-3">
                <Button variant="outline" onClick={() => setShowCutplanConfig(false)}>
                  Cancel
                </Button>
                <Button
                  onClick={handleOptimizeCutplan}
                  disabled={isGeneratingCutplan || cutplanConfig.strategies.length === 0}
                >
                  {isGeneratingCutplan ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Generating...
                    </>
                  ) : (
                    `Generate ${cutplanConfig.strategies.length} Option${cutplanConfig.strategies.length > 1 ? 's' : ''}`
                  )}
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Cutplan Optimization Progress */}
      {isGeneratingCutplan && cutplanOptStatus && (
        <Card className="border-purple-200 bg-purple-50/30">
          <CardHeader>
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-purple-500 to-purple-600 flex items-center justify-center">
                <Loader2 className="h-5 w-5 text-white animate-spin" />
              </div>
              <div className="flex-1">
                <CardTitle>{cutplanOptStatus.phase === 'cpu_nesting' ? 'Preparing Cost Estimates' : 'Cutplan Optimization Running'}</CardTitle>
                <CardDescription>{cutplanOptStatus.message}</CardDescription>
              </div>
              <div className="text-right text-sm">
                {cutplanOptStatus.phase === 'cpu_nesting' ? (
                  <span className="text-muted-foreground">Preparing marker previews</span>
                ) : (
                  <>
                    <span className="font-medium">{cutplanOptStatus.strategies_done}/{cutplanOptStatus.strategies_total}</span>
                    <span className="text-muted-foreground"> strategies</span>
                  </>
                )}
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              <div className="w-full bg-purple-100 rounded-full h-2">
                <div
                  className="bg-purple-500 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${Math.max(cutplanOptStatus.progress, 2)}%` }}
                />
              </div>
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{cutplanOptStatus.progress}%</span>
                <span>Each strategy has a 2-minute time limit</span>
              </div>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleCancelCutplan}
              >
                <XCircle className="mr-2 h-4 w-4" />
                Stop Optimization
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Cutplan Options */}
      {activeCutplans.length > 0 && (
        <Card data-cutplan-options>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle>Cutplan Options</CardTitle>
                <CardDescription>Compare and select the best cutplan for production</CardDescription>
              </div>
              {activeCutplans.some(c => c.status === 'refined') && (
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={includeMarkerImages}
                      onChange={(e) => setIncludeMarkerImages(e.target.checked)}
                      className="rounded border-gray-300"
                    />
                    Include marker images
                  </label>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={async () => {
                      try {
                        const blob = await api.downloadOrderExcel(orderId, includeMarkerImages)
                        const url = URL.createObjectURL(blob)
                        const a = document.createElement('a')
                        a.href = url
                        a.download = `order_${order.order_number}_cutplan.xlsx`
                        a.click()
                        URL.revokeObjectURL(url)
                      } catch (e) {
                        toast({ title: 'Export failed', description: e instanceof Error ? e.message : 'Please try again', variant: 'destructive' })
                      }
                    }}
                  >
                    <Download className="h-4 w-4 mr-1.5" />
                    Export All (Excel)
                  </Button>
                </div>
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-6">
            {activeCutplans.map((plan, planIdx) => {
              const effPercent = (plan.efficiency || 0) * 100
              const isCpuNesting = isGeneratingCutplan && cutplanOptStatus?.phase === 'cpu_nesting'
              const totalBundleCuts = plan.markers?.reduce((sum: number, m: { ratio_str: string; cuts: number }) => {
                const bundles = m.ratio_str.split('-').reduce((a: number, b: string) => a + (parseInt(b) || 0), 0)
                return sum + bundles * m.cuts
              }, 0) || 0
              return (
                <div key={plan.id} className={`border rounded-lg overflow-hidden ${plan.status === 'approved' ? 'border-amber-400 ring-1 ring-amber-200 bg-amber-50/40' : ''} ${plan.status === 'refined' ? 'border-amber-500 ring-2 ring-amber-300/60 bg-gradient-to-br from-amber-50/60 to-yellow-50/40' : ''}`}>
                  {/* Plan Header */}
                  <div className={`px-4 py-3 flex items-center justify-between ${plan.status === 'refined' ? 'bg-gradient-to-r from-amber-100/70 to-yellow-50/50' : plan.status === 'approved' ? 'bg-amber-50/60' : 'bg-muted/50'}`}>
                    <div>
                      <h3 className="font-semibold">{plan.name || `Option ${planIdx + 1}`}</h3>
                      <div className="text-xs text-muted-foreground flex gap-3 mt-1">
                        {isCpuNesting ? (
                          <span className="flex items-center gap-1 text-purple-600 font-medium">
                            <Loader2 className="h-3 w-3 animate-spin" />
                            Preparing...
                          </span>
                        ) : (
                          <span className={`font-medium ${effPercent >= 80 ? 'text-green-600' : effPercent >= 75 ? 'text-amber-600' : 'text-red-600'}`}>
                            {effPercent.toFixed(2)}% Efficiency
                          </span>
                        )}
                        <span>{plan.unique_markers} markers</span>
                        <span>{plan.total_plies} plies</span>
                        <span>{plan.total_cuts} cuts</span>
                        <span>{totalBundleCuts} bundle-cuts</span>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      {isCpuNesting ? (
                        <div className="text-right">
                          <div className="text-sm text-purple-600 flex items-center gap-1.5">
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            Preparing costs...
                          </div>
                        </div>
                      ) : (
                        <div className="text-right">
                          <div className="text-lg font-bold">${plan.total_cost?.toFixed(2)}</div>
                          <div className="text-xs text-muted-foreground">Total Cost</div>
                        </div>
                      )}
                      {plan.status === 'refined' ? (
                        <Button variant="outline" disabled className="bg-amber-100 border-amber-400 text-amber-800">
                          <CheckCircle2 className="mr-2 h-4 w-4 text-amber-600" />
                          Refined
                        </Button>
                      ) : plan.status === 'refining' ? (
                        <Button variant="outline" disabled className="bg-indigo-100">
                          <Loader2 className="mr-2 h-4 w-4 animate-spin text-indigo-600" />
                          Refining...
                        </Button>
                      ) : plan.status === 'approved' ? (
                        <Button variant="outline" disabled className="bg-amber-50 border-amber-300 text-amber-700">
                          <CheckCircle2 className="mr-2 h-4 w-4 text-amber-500" />
                          Approved
                        </Button>
                      ) : (
                        <Button
                          onClick={async () => {
                            await api.approveCutplan(plan.id)
                            loadData()
                          }}
                        >
                          Approve
                        </Button>
                      )}
                    </div>
                  </div>

                  {/* Marker Table */}
                  {plan.markers && plan.markers.length > 0 && (() => {
                    const cpId = plan.id
                    const previews = previewMarkers[cpId] || new Set<number>()
                    const allPreviewed = previews.size === plan.markers.length && plan.markers.length > 0
                    const completedJob = nestingJobs.find(j => j.status === 'completed')
                    const hasSvgs = plan.markers.some((m: any) => m.svg_preview)

                    // Build refined overlay map from in-progress refinement status
                    const refStatus = refinementStatuses[cpId]
                    const isRefiningThis = refiningCutplans[cpId] || false
                    const refinedMap = new Map<string, MarkerLayout>()
                    if (refStatus?.layouts) {
                      for (const l of refStatus.layouts) {
                        refinedMap.set(l.cutplan_marker_id, l)
                      }
                    }
                    const allMarkersRefined = plan.status === 'refined' || (refinedMap.size === plan.markers.length && refinedMap.size > 0)

                    const togglePreview = (idx: number) => {
                      setPreviewMarkers(prev => {
                        const current = new Set(prev[cpId] || [])
                        if (current.has(idx)) current.delete(idx)
                        else current.add(idx)
                        return { ...prev, [cpId]: current }
                      })
                    }

                    const toggleAllPreviews = () => {
                      setPreviewMarkers(prev => {
                        if (allPreviewed) return { ...prev, [cpId]: new Set<number>() }
                        return { ...prev, [cpId]: new Set(plan.markers.map((_: any, i: number) => i)) }
                      })
                    }

                    return (
                    <div className="overflow-x-auto">
                      {(hasSvgs || allMarkersRefined) && (
                        <div className="flex items-center justify-between px-3 py-1.5 border-b bg-muted/10">
                          <div className="flex items-center gap-2">
                            {allMarkersRefined && !isRefiningThis && refStatus?.status === 'completed' && (
                              <>
                                <Button size="sm" className="h-7 text-xs gap-1.5" onClick={() => handleDownloadMarkers(cpId)}>
                                  <Download className="h-3.5 w-3.5" />
                                  Download DXF
                                </Button>
                                <Button variant="outline" size="sm" className="h-7 text-xs gap-1.5" onClick={async () => {
                                  try {
                                    const blob = await api.downloadCutplanExcel(cpId, includeMarkerImages)
                                    const url = URL.createObjectURL(blob)
                                    const a = document.createElement('a')
                                    a.href = url
                                    a.download = `cutplan_${plan.name?.replace(/\s+/g, '_') || cpId}.xlsx`
                                    a.click()
                                    URL.revokeObjectURL(url)
                                  } catch (e) {
                                    toast({ title: 'Export failed', description: e instanceof Error ? e.message : 'Please try again', variant: 'destructive' })
                                  }
                                }}>
                                  <Download className="h-3.5 w-3.5" />
                                  Export Excel
                                </Button>
                                <label className="flex items-center gap-1 text-[10px] text-muted-foreground cursor-pointer select-none">
                                  <input
                                    type="checkbox"
                                    checked={includeMarkerImages}
                                    onChange={(e) => setIncludeMarkerImages(e.target.checked)}
                                    className="rounded border-gray-300 h-3 w-3"
                                  />
                                  With markers
                                </label>
                                <Button variant="outline" size="sm" className="h-7 text-xs gap-1.5" onClick={() => {
                                  setRefinementStatuses(prev => {
                                    const next = { ...prev }
                                    delete next[cpId]
                                    return next
                                  })
                                }}>
                                  Re-run Refinement
                                </Button>
                              </>
                            )}
                          </div>
                          {hasSvgs && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1.5"
                              onClick={toggleAllPreviews}
                            >
                              {allPreviewed
                                ? <><EyeOff className="h-3.5 w-3.5" /> Hide All Markers</>
                                : <><Eye className="h-3.5 w-3.5" /> Show All Markers</>
                              }
                            </Button>
                          )}
                        </div>
                      )}
                      <table className="w-full text-sm">
                        <thead className={`bg-muted/30 ${allMarkersRefined ? 'border-l-4 border-l-amber-400' : ''}`}>
                          <tr className="border-b">
                            <th className="text-left py-2 px-3 font-medium">Marker</th>
                            {sizes.map(size => (
                              <th key={size} className="text-center py-2 px-2 font-medium min-w-[40px]">{size}</th>
                            ))}
                            <th className="text-center py-2 px-3 font-medium">Bundles</th>
                            <th className="text-center py-2 px-3 font-medium">Width</th>
                            <th className="text-center py-2 px-3 font-medium">Length</th>
                            <th className="text-center py-2 px-3 font-medium">Eff%</th>
                            <th className="text-center py-2 px-3 font-medium">Plies</th>
                            <th className="text-center py-2 px-3 font-medium">Cuts</th>
                          </tr>
                        </thead>
                        <tbody>
                          {[...plan.markers].sort((a, b) => (b.length_yards || 0) - (a.length_yards || 0)).map((marker, idx) => {
                            const ratioValues = marker.ratio_str.split('-').map(v => parseInt(v) || 0)
                            const bundles = ratioValues.reduce((a, b) => a + b, 0)
                            const markerResult = completedJob?.results?.find(r => r.ratio_str === marker.ratio_str)
                            const markerWidth = markerResult?.fabric_width_inches || completedJob?.fabric_width_inches

                            // Refined overlay: use in-progress layout if available, else fall back to marker data
                            const refinedLayout = refinedMap.get(marker.id)
                            const isRefined = plan.status === 'refined' || !!refinedLayout || !!marker.computation_time_s
                            const displayEff = refinedLayout ? refinedLayout.utilization : marker.efficiency
                            const displayLength = refinedLayout ? refinedLayout.length_yards : marker.length_yards
                            const displaySvg = refinedLayout ? refinedLayout.svg_preview : marker.svg_preview
                            const displayTime = refinedLayout ? refinedLayout.computation_time_s : marker.computation_time_s

                            const svgPreview = displaySvg
                            const isPreviewOpen = previews.has(idx)
                            return (
                              <React.Fragment key={marker.id || idx}>
                              <tr
                                className={`border-b border-border/50 hover:bg-muted/20 ${svgPreview ? 'cursor-pointer' : ''} ${isPreviewOpen ? 'bg-muted/30' : ''} ${isRefined && !isPreviewOpen ? 'bg-amber-50/50 border-l-4 border-l-amber-400' : ''}`}
                                onClick={() => svgPreview && togglePreview(idx)}
                              >
                                <td className="py-2 px-3 font-medium flex items-center gap-1.5">
                                  {svgPreview && (
                                    isPreviewOpen
                                      ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                                      : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                                  )}
                                  {marker.marker_label || `M${idx + 1}`}
                                </td>
                                {sizes.map((size, sizeIdx) => (
                                  <td key={size} className="text-center py-2 px-2 tabular-nums">
                                    {ratioValues[sizeIdx] > 0 ? ratioValues[sizeIdx] : <span className="text-muted-foreground/30">-</span>}
                                  </td>
                                ))}
                                <td className="text-center py-2 px-3">
                                  <span className="bg-primary/10 text-primary px-2 py-0.5 rounded text-xs font-medium">
                                    {bundles}
                                  </span>
                                </td>
                                <td className="text-center py-2 px-3 text-xs text-muted-foreground">
                                  {markerWidth ? `${markerWidth}"` : '-'}
                                </td>
                                <td className="text-center py-2 px-3 tabular-nums text-xs">
                                  {isCpuNesting
                                    ? <span className="text-purple-500 text-[10px]">...</span>
                                    : displayLength ? `${displayLength.toFixed(2)} yd` : <span className="text-muted-foreground/40">-</span>}
                                </td>
                                <td className="text-center py-2 px-3 tabular-nums text-xs">
                                  {isCpuNesting
                                    ? <span className="text-purple-500 text-[10px]">...</span>
                                    : displayEff ? `${(displayEff * 100).toFixed(1)}%` : <span className="text-muted-foreground/40">-</span>}
                                </td>
                                <td className="text-center py-2 px-3 tabular-nums font-medium">{marker.total_plies}</td>
                                <td className="text-center py-2 px-3 tabular-nums">{marker.cuts}</td>
                              </tr>
                              {isPreviewOpen && svgPreview && (
                                <tr className="border-b border-border/50 bg-muted/10">
                                  <td colSpan={sizes.length + 7} className="p-3">
                                    <div
                                      className="w-full [&>svg]:w-full [&>svg]:h-auto [&>svg]:max-h-[180px] rounded border bg-white p-2"
                                      dangerouslySetInnerHTML={{ __html: svgPreview }}
                                    />
                                    {displayTime != null && (
                                      <div className="text-[10px] text-muted-foreground mt-1 text-right">
                                        Refined in {displayTime.toFixed(1)}s
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              )}
                              </React.Fragment>
                            )
                          })}
                        </tbody>
                        <tfoot className="bg-muted/50 font-medium">
                          <tr>
                            <td className="py-2 px-3">Total</td>
                            {sizes.map(size => {
                              const sizeIdx = sizes.indexOf(size)
                              const total = plan.markers.reduce((sum, m) => {
                                const ratio = parseInt(m.ratio_str.split('-')[sizeIdx]) || 0
                                return sum + ratio * m.total_plies
                              }, 0)
                              return (
                                <td key={size} className="text-center py-2 px-2 tabular-nums">{total > 0 ? total : '-'}</td>
                              )
                            })}
                            <td className="text-center py-2 px-3">-</td>
                            <td className="text-center py-2 px-3">-</td>
                            <td className="text-center py-2 px-3 tabular-nums text-xs">
                              {isCpuNesting
                                ? <span className="text-purple-500 text-[10px]">...</span>
                                : plan.total_yards ? `${plan.total_yards.toFixed(1)} yd` : '-'}
                            </td>
                            <td className="text-center py-2 px-3 tabular-nums text-xs">
                              {isCpuNesting
                                ? <span className="text-purple-500 text-[10px]">...</span>
                                : plan.efficiency ? `${(plan.efficiency * 100).toFixed(1)}%` : '-'}
                            </td>
                            <td className="text-center py-2 px-3 tabular-nums">{plan.total_plies}</td>
                            <td className="text-center py-2 px-3 tabular-nums">{plan.total_cuts}</td>
                          </tr>
                        </tfoot>
                      </table>
                    </div>
                    )
                  })()}

                  {/* Cost Breakdown */}
                  <div className="px-4 py-3 bg-muted/20 border-t">
                    {isCpuNesting ? (
                      <div className="flex items-center gap-2 py-2 text-sm text-purple-600">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        Preparing cost calculations...
                      </div>
                    ) : (
                    <>
                    <div className="flex items-center justify-between mb-3">
                      <div className="text-sm font-medium">Cost Breakdown</div>
                      <div className="text-xs text-muted-foreground">
                        {plan.total_yards?.toFixed(1)} yards total
                      </div>
                    </div>
                    <div className="grid grid-cols-5 gap-3 text-sm">
                      <div className="bg-background rounded-lg p-2 border">
                        <div className="text-muted-foreground text-xs">Fabric</div>
                        <div className="font-semibold">${plan.fabric_cost?.toFixed(2) || '0.00'}</div>
                      </div>
                      <div className="bg-background rounded-lg p-2 border">
                        <div className="text-muted-foreground text-xs">Spreading</div>
                        <div className="font-semibold">${plan.spreading_cost?.toFixed(2) || '0.00'}</div>
                      </div>
                      <div className="bg-background rounded-lg p-2 border">
                        <div className="text-muted-foreground text-xs">Cutting</div>
                        <div className="font-semibold">${plan.cutting_cost?.toFixed(2) || '0.00'}</div>
                      </div>
                      <div className="bg-background rounded-lg p-2 border">
                        <div className="text-muted-foreground text-xs">Prep</div>
                        <div className="font-semibold">${plan.prep_cost?.toFixed(2) || '0.00'}</div>
                      </div>
                      <div className="bg-primary/5 rounded-lg p-2 border border-primary/20">
                        <div className="text-muted-foreground text-xs">Total</div>
                        <div className="font-bold text-primary">${plan.total_cost?.toFixed(2) || '0.00'}</div>
                      </div>
                    </div>
                    {/* Floor waste estimate + Total incl. waste */}
                    {(plan as any).solver_config?.mc_waste_pct != null && (() => {
                      const sc = (plan as any).solver_config
                      const wasteYards = sc.mc_waste_yards || 0
                      const fabricCost = sc.fabric_cost_per_yard || 0
                      const totalInclWaste = (plan.total_cost || 0) + wasteYards * fabricCost
                      return (
                        <div className="mt-3 flex items-center gap-3 flex-wrap">
                          <div className="bg-emerald-50 rounded-lg px-3 py-1.5 border border-emerald-200 inline-flex items-center gap-2">
                            <span className="text-emerald-700 text-xs">Est. Floor Waste</span>
                            <span className="font-semibold text-emerald-800">{sc.mc_waste_pct.toFixed(1)}%</span>
                            {wasteYards > 0 && <span className="text-emerald-600 text-xs">({wasteYards.toFixed(1)} yd)</span>}
                          </div>
                          {wasteYards > 0 && fabricCost > 0 && (
                            <div className="bg-orange-50 rounded-lg px-3 py-1.5 border border-orange-200 inline-flex items-center gap-2">
                              <span className="text-orange-700 text-xs">Total Cost (Incl. Est. Waste)</span>
                              <span className="font-bold text-orange-800">${totalInclWaste.toFixed(2)}</span>
                            </div>
                          )}
                        </div>
                      )
                    })()}
                    </>
                    )}
                  </div>

                  {/* Approve + Refine prompt */}
                  {plan.status === 'ready' && !isCpuNesting && (
                    <div className="px-4 py-2.5 bg-indigo-50/50 border-t border-indigo-200 flex items-center justify-between">
                      <div className="flex items-center gap-2 text-xs text-indigo-700">
                        <Layers className="h-3.5 w-3.5" />
                        Approve this cutplan to unlock Marker Refinement for optimized layouts
                      </div>
                      <Button
                        size="sm"
                        className="h-7 text-xs"
                        onClick={async () => {
                          await api.approveCutplan(plan.id)
                          loadData()
                        }}
                      >
                        <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" />
                        Approve
                      </Button>
                    </div>
                  )}

                  {/* Inline Refinement Section */}
                  {(plan.status === 'approved' || plan.status === 'refining' || plan.status === 'refined') && (() => {
                    const cpId = plan.id
                    const isRefiningThis = refiningCutplans[cpId] || false
                    const refStatus = refinementStatuses[cpId] || null
                    const config = getRefinementConfig(cpId)
                    const hasLayouts = refStatus && refStatus.layouts && refStatus.layouts.length > 0

                    return (
                      <div className="px-4 py-3 bg-indigo-50/50 border-t border-indigo-200">
                        <div className="flex items-center gap-2 mb-3">
                          <Layers className="h-4 w-4 text-indigo-600" />
                          <span className="text-sm font-semibold text-indigo-800">Marker Refinement</span>
                        </div>

                        {/* Config panel */}
                        {!isRefiningThis && !hasLayouts && (() => {
                          const isRefAdvanced = showRefAdvanced[cpId] || false
                          const toggleRefAdvanced = () => setShowRefAdvanced(prev => ({ ...prev, [cpId]: !prev[cpId] }))
                          const totalTime = config.time_limit_s || 120

                          return (
                          <div className="space-y-3 p-3 bg-white rounded-lg border">
                            {/* Main settings: Time, Orientation, Piece Buffer */}
                            <div className="grid grid-cols-3 gap-3">
                              <div>
                                <label className="text-xs font-medium text-muted-foreground block mb-1">Max Time/Marker (sec)</label>
                                <input
                                  type="number"
                                  value={config.time_limit_s}
                                  onChange={(e) => {
                                    const v = e.target.value
                                    setRefinementConfig(cpId, { ...config, time_limit_s: v === '' ? '' as any : parseFloat(v) })
                                  }}
                                  className="w-full px-2 py-1.5 border rounded text-sm"
                                  min={10} max={600} step={10}
                                />
                              </div>
                              <div>
                                <label className="text-xs font-medium text-muted-foreground block mb-1">Orientation</label>
                                <div className="flex gap-1 mt-0.5">
                                  <button onClick={() => setRefinementConfig(cpId, { ...config, rotation_mode: 'free' })}
                                    className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                                      config.rotation_mode === 'free' ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-foreground border-border hover:border-indigo-400'
                                    }`}>Free (0/180)</button>
                                  <button onClick={() => setRefinementConfig(cpId, { ...config, rotation_mode: 'nap_one_way' })}
                                    className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                                      config.rotation_mode === 'nap_one_way' ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-foreground border-border hover:border-indigo-400'
                                    }`}>Nap (0 only)</button>
                                </div>
                              </div>
                              <div>
                                <label className="text-xs font-medium text-muted-foreground block mb-1">Piece Buffer (mm)</label>
                                <input
                                  type="number"
                                  value={config.piece_buffer_mm}
                                  onChange={(e) => { const v = e.target.value; setRefinementConfig(cpId, { ...config, piece_buffer_mm: v === '' ? '' as any : parseFloat(v) }) }}
                                  className="w-full px-2 py-1.5 border rounded text-sm"
                                  min={0} max={10} step={0.5}
                                />
                              </div>
                            </div>

                            {/* Advanced toggle */}
                            <button
                              onClick={toggleRefAdvanced}
                              className="text-[10px] text-muted-foreground hover:text-foreground flex items-center gap-1"
                            >
                              <span className={`transition-transform ${isRefAdvanced ? 'rotate-90' : ''}`}>▶</span>
                              Advanced
                            </button>
                            {isRefAdvanced && (
                              <div className="space-y-3 p-2.5 rounded border bg-muted/10">
                                <div className="grid grid-cols-3 gap-3">
                                  <div>
                                    <label className="text-xs font-medium text-muted-foreground block mb-1">Quadtree Depth</label>
                                    <div className="flex gap-1">
                                      {[2, 3, 4, 5, 6, 7, 8].map(d => (
                                        <button key={d} onClick={() => setRefinementConfig(cpId, { ...config, quadtree_depth: d })}
                                          className={`px-2 py-1 rounded text-xs font-medium border transition-colors ${
                                            config.quadtree_depth === d ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-foreground border-border hover:border-indigo-400'
                                          }`}
                                        >{d}</button>
                                      ))}
                                    </div>
                                  </div>
                                  <div>
                                    <label className="text-xs font-medium text-muted-foreground block mb-1">Edge Buffer (mm)</label>
                                    <input
                                      type="number"
                                      value={config.edge_buffer_mm}
                                      onChange={(e) => { const v = e.target.value; setRefinementConfig(cpId, { ...config, edge_buffer_mm: v === '' ? '' as any : parseFloat(v) }) }}
                                      className="w-full px-2 py-1.5 border rounded text-sm"
                                      min={0} max={20} step={0.5}
                                    />
                                  </div>
                                  <div className="flex items-center pt-4">
                                    <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground cursor-pointer">
                                      <input type="checkbox" checked={config.early_termination}
                                        onChange={(e) => setRefinementConfig(cpId, { ...config, early_termination: e.target.checked })}
                                        className="rounded" />
                                      Early stop
                                    </label>
                                  </div>
                                </div>
                                <div className="grid grid-cols-2 gap-3">
                                  <div className="flex items-center gap-2">
                                    <label className="text-xs font-medium text-muted-foreground shrink-0">Seed Selection</label>
                                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                                      <input type="checkbox" checked={config.seed_screening || false}
                                        onChange={(e) => setRefinementConfig(cpId, { ...config, seed_screening: e.target.checked })}
                                        className="rounded" />
                                      Screen 6 seeds (+60s/marker)
                                    </label>
                                  </div>
                                  <div className="flex items-center gap-2">
                                    <label className="text-xs font-medium text-muted-foreground shrink-0">Compute</label>
                                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                                      <input type="checkbox" checked={config.use_cloud || false}
                                        onChange={(e) => setRefinementConfig(cpId, { ...config, use_cloud: e.target.checked })}
                                        className="rounded" />
                                      Surface / Cloud
                                    </label>
                                  </div>
                                </div>
                              </div>
                            )}

                            {/* Summary + Start button */}
                            <div className="flex items-center justify-between pt-1 border-t border-border/50">
                              <p className="text-xs text-muted-foreground">
                                {plan.markers.length} markers &times; {totalTime}s = {plan.markers.length * totalTime}s max
                                {config.seed_screening ? ' (+seed screening)' : ''}
                              </p>
                              <Button size="sm" onClick={() => handleStartRefinement(cpId)}>
                                <Play className="mr-2 h-4 w-4" />
                                Start Refinement
                              </Button>
                            </div>
                          </div>
                          )
                        })()}

                        {/* Progress bar */}
                        {isRefiningThis && refStatus && (
                          <div className="space-y-2">
                            <div className="flex items-center justify-between">
                              <div className="text-sm font-medium">{refStatus.message}</div>
                              <span className="text-sm font-medium">{refStatus.markers_done}/{refStatus.markers_total}</span>
                            </div>
                            <div className="w-full bg-indigo-100 rounded-full h-2">
                              <div
                                className="bg-indigo-500 h-2 rounded-full transition-all duration-500"
                                style={{ width: `${Math.max(refStatus.progress, 2)}%` }}
                              />
                            </div>
                            <Button
                              variant="outline"
                              size="sm"
                              className="text-red-600 border-red-200 hover:bg-red-50"
                              onClick={() => handleCancelRefinement(cpId)}
                            >
                              <XCircle className="mr-2 h-4 w-4" />
                              Stop
                            </Button>
                          </div>
                        )}

                        {/* Refinement complete summary */}
                        {!isRefiningThis && hasLayouts && refStatus?.status === 'completed' && (
                          <div className="text-xs text-green-600 font-medium flex items-center gap-1.5">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            {refStatus.message}
                          </div>
                        )}
                      </div>
                    )
                  })()}
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}

      {/* Cutplan History (superseded) */}
      {supersededCutplans.length > 0 && (
        <Card className="border-muted">
          <CardHeader className="py-3 cursor-pointer" onClick={() => setShowHistory(!showHistory)}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {showHistory ? <ChevronDown className="h-4 w-4 text-muted-foreground" /> : <ChevronRight className="h-4 w-4 text-muted-foreground" />}
                <History className="h-4 w-4 text-muted-foreground" />
                <span className="text-sm font-medium text-muted-foreground">
                  History ({supersededCutplans.length} superseded)
                </span>
              </div>
            </div>
          </CardHeader>
          {showHistory && (
            <CardContent className="pt-0 space-y-4">
              {(() => {
                // Group by generation_batch_id
                const batches = new Map<string, typeof supersededCutplans>()
                for (const cp of supersededCutplans) {
                  const key = cp.generation_batch_id || 'legacy'
                  if (!batches.has(key)) batches.set(key, [])
                  batches.get(key)!.push(cp)
                }
                // Sort batches by newest first
                const sortedBatches = Array.from(batches.entries()).sort((a, b) => {
                  const aDate = a[1][0]?.created_at || ''
                  const bDate = b[1][0]?.created_at || ''
                  return bDate.localeCompare(aDate)
                })
                return sortedBatches.map(([batchId, batchPlans]) => {
                  const batchDate = batchPlans[0]?.created_at
                    ? new Date(batchPlans[0].created_at).toLocaleString()
                    : 'Unknown'
                  return (
                    <div key={batchId} className="border rounded-lg overflow-hidden opacity-60">
                      <div className="px-3 py-2 bg-muted/30 border-b flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-medium text-muted-foreground">
                            {batchDate}
                          </span>
                          <span className="text-[10px] bg-muted px-1.5 py-0.5 rounded font-medium text-muted-foreground">
                            Superseded
                          </span>
                        </div>
                        <span className="text-[10px] text-muted-foreground">
                          {batchPlans.length} option{batchPlans.length !== 1 ? 's' : ''}
                        </span>
                      </div>
                      <table className="w-full text-xs">
                        <thead className="bg-muted/20">
                          <tr className="border-b">
                            <th className="text-left py-1.5 px-3 font-medium">Name</th>
                            <th className="text-center py-1.5 px-2 font-medium">Eff%</th>
                            <th className="text-center py-1.5 px-2 font-medium">Markers</th>
                            <th className="text-center py-1.5 px-2 font-medium">Cost</th>
                            <th className="text-center py-1.5 px-2 font-medium">Was</th>
                            <th className="text-center py-1.5 px-2 font-medium w-8"></th>
                          </tr>
                        </thead>
                        <tbody>
                          {batchPlans.map(cp => {
                            const prevStatus = (cp.solver_config as Record<string, unknown>)?.pre_superseded_status as string || 'ready'
                            return (
                              <tr key={cp.id} className="border-b border-border/30 hover:bg-muted/10">
                                <td className="py-1.5 px-3">{cp.name || 'Untitled'}</td>
                                <td className="text-center py-1.5 px-2 tabular-nums">
                                  {cp.efficiency ? `${(cp.efficiency * 100).toFixed(1)}%` : '-'}
                                </td>
                                <td className="text-center py-1.5 px-2 tabular-nums">{cp.unique_markers || '-'}</td>
                                <td className="text-center py-1.5 px-2 tabular-nums">
                                  {cp.total_cost ? `$${cp.total_cost.toFixed(2)}` : '-'}
                                </td>
                                <td className="text-center py-1.5 px-2">
                                  <span className="text-[10px] bg-muted/60 px-1.5 py-0.5 rounded">{prevStatus}</span>
                                </td>
                                <td className="text-center py-1.5 px-2">
                                  <button
                                    className="text-muted-foreground hover:text-red-500 transition-colors"
                                    title="Delete superseded cutplan"
                                    onClick={async (e) => {
                                      e.stopPropagation()
                                      try {
                                        await api.deleteCutplan(cp.id)
                                        loadData()
                                      } catch (err) {
                                        toast({ title: 'Delete failed', description: err instanceof Error ? err.message : 'Unknown error', variant: 'destructive' })
                                      }
                                    }}
                                  >
                                    <Trash2 className="h-3 w-3" />
                                  </button>
                                </td>
                              </tr>
                            )
                          })}
                        </tbody>
                      </table>
                    </div>
                  )
                })
              })()}
            </CardContent>
          )}
        </Card>
      )}
    </>
  )
}
