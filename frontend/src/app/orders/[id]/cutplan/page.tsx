'use client'

import { useEffect, useState, useRef } from 'react'
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
    strategies: ['max_efficiency', 'balanced', 'min_markers'] as string[],
    selectedColor: 'all' as string,
  })
  const [isGeneratingCutplan, setIsGeneratingCutplan] = useState(false)
  const [cutplanOptStatus, setCutplanOptStatus] = useState<{
    status: string; progress: number; message: string; strategies_total: number; strategies_done: number
  } | null>(null)

  // Per-cutplan refinement state
  const [refinementConfigs, setRefinementConfigs] = useState<Record<string, RefinementConfig>>({})
  const [refiningCutplans, setRefiningCutplans] = useState<Record<string, boolean>>({})
  const [refinementStatuses, setRefinementStatuses] = useState<Record<string, RefinementStatus>>({})

  // Per-cutplan expanded marker tracking for refined layouts
  const [expandedMarkers, setExpandedMarkers] = useState<Record<string, Set<number>>>({})

  const cutplanPollingRef = useRef<NodeJS.Timeout | null>(null)
  const refinementPollingRefs = useRef<Record<string, NodeJS.Timeout>>({})
  const lastStrategiesDone = useRef(0)

  const getRefinementConfig = (cpId: string): RefinementConfig =>
    refinementConfigs[cpId] || { piece_buffer_mm: 2.0, edge_buffer_mm: 5.0, time_limit_s: 20.0, rotation_mode: 'free' }

  const setRefinementConfig = (cpId: string, config: RefinementConfig) =>
    setRefinementConfigs(prev => ({ ...prev, [cpId]: config }))

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
    const eligiblePlans = cutplans.filter(c => c.status === 'approved' || c.status === 'refining' || c.status === 'refined')
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
  }, [orderId, cutplans.length])

  // Auto-show config panel if nesting results exist but no cutplans
  useEffect(() => {
    if (hasNestingResults && cutplans.length === 0 && !isGeneratingCutplan) {
      setShowCutplanConfig(true)
    }
  }, [hasNestingResults, cutplans.length, isGeneratingCutplan])

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
        if (status.strategies_done > lastStrategiesDone.current) {
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
    setCutplanOptStatus({ status: 'running', progress: 0, message: 'Starting...', strategies_total: cutplanConfig.strategies.length, strategies_done: 0 })
    try {
      await api.optimizeCutplan({
        order_id: orderId,
        generate_options: cutplanConfig.strategies,
        penalty: 5.0,
        fabric_cost_per_yard: cutplanConfig.fabricCostPerYard,
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
                    <button
                      onClick={() => setCutplanConfig({ ...cutplanConfig, selectedColor: 'all' })}
                      className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                        cutplanConfig.selectedColor === 'all'
                          ? 'bg-primary text-primary-foreground border-primary'
                          : 'bg-muted/30 hover:bg-muted border-border'
                      }`}
                    >
                      <div className="font-medium">All Colors</div>
                      <div className={`text-xs ${cutplanConfig.selectedColor === 'all' ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                        Aggregate demand across all colors
                      </div>
                    </button>
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
                    { id: 'max_efficiency', label: 'Max Efficiency', desc: 'Minimize fabric waste' },
                    { id: 'balanced', label: 'Balanced', desc: 'Efficiency + marker count' },
                    { id: 'min_markers', label: 'Min Markers', desc: 'Simplest cutting plan' },
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
                <CardTitle>Cutplan Optimization Running</CardTitle>
                <CardDescription>{cutplanOptStatus.message}</CardDescription>
              </div>
              <div className="text-right text-sm">
                <span className="font-medium">{cutplanOptStatus.strategies_done}/{cutplanOptStatus.strategies_total}</span>
                <span className="text-muted-foreground"> strategies</span>
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
      {cutplans.length > 0 && (
        <Card data-cutplan-options>
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle>Cutplan Options</CardTitle>
                <CardDescription>Compare and select the best cutplan for production</CardDescription>
              </div>
              {cutplans.some(c => c.status === 'refined') && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={async () => {
                    try {
                      const blob = await api.downloadOrderExcel(orderId)
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
              )}
            </div>
          </CardHeader>
          <CardContent className="space-y-6">
            {cutplans.map((plan, planIdx) => {
              const effPercent = (plan.efficiency || 0) * 100
              return (
                <div key={plan.id} className={`border rounded-lg overflow-hidden ${plan.status === 'approved' ? 'border-green-500 bg-green-50/50' : ''}`}>
                  {/* Plan Header */}
                  <div className="bg-muted/50 px-4 py-3 flex items-center justify-between">
                    <div>
                      <h3 className="font-semibold">{plan.name || `Option ${planIdx + 1}`}</h3>
                      <div className="text-xs text-muted-foreground flex gap-3 mt-1">
                        <span className={`font-medium ${effPercent >= 80 ? 'text-green-600' : effPercent >= 75 ? 'text-amber-600' : 'text-red-600'}`}>
                          {effPercent.toFixed(2)}% Efficiency
                        </span>
                        <span>{plan.unique_markers} markers</span>
                        <span>{plan.total_plies} plies</span>
                        <span>{plan.total_cuts} cuts</span>
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <div className="text-lg font-bold">${plan.total_cost?.toFixed(2)}</div>
                        <div className="text-xs text-muted-foreground">Total Cost</div>
                      </div>
                      {plan.status === 'refined' ? (
                        <Button variant="outline" disabled className="bg-green-100">
                          <CheckCircle2 className="mr-2 h-4 w-4 text-green-600" />
                          Refined
                        </Button>
                      ) : plan.status === 'refining' ? (
                        <Button variant="outline" disabled className="bg-indigo-100">
                          <Loader2 className="mr-2 h-4 w-4 animate-spin text-indigo-600" />
                          Refining...
                        </Button>
                      ) : plan.status === 'approved' ? (
                        <Button variant="outline" disabled className="bg-green-100">
                          <CheckCircle2 className="mr-2 h-4 w-4 text-green-600" />
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
                  {plan.markers && plan.markers.length > 0 && (
                    <div className="overflow-x-auto">
                      <table className="w-full text-sm">
                        <thead className="bg-muted/30">
                          <tr className="border-b">
                            <th className="text-left py-2 px-3 font-medium">Marker</th>
                            {sizes.map(size => (
                              <th key={size} className="text-center py-2 px-2 font-medium min-w-[40px]">{size}</th>
                            ))}
                            <th className="text-center py-2 px-3 font-medium">Bundles</th>
                            <th className="text-center py-2 px-3 font-medium">Width</th>
                            <th className="text-center py-2 px-3 font-medium">Plies</th>
                            <th className="text-center py-2 px-3 font-medium">Cuts</th>
                          </tr>
                        </thead>
                        <tbody>
                          {plan.markers.map((marker, idx) => {
                            const ratioValues = marker.ratio_str.split('-').map(v => parseInt(v) || 0)
                            const bundles = ratioValues.reduce((a, b) => a + b, 0)
                            const gpuWidth = nestingJobs.find(j => j.status === 'completed')?.fabric_width_inches
                            return (
                              <tr key={marker.id || idx} className="border-b border-border/50 hover:bg-muted/20">
                                <td className="py-2 px-3 font-medium">{marker.marker_label || `M${idx + 1}`}</td>
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
                                  {gpuWidth ? `${gpuWidth}"` : '-'}
                                </td>
                                <td className="text-center py-2 px-3 tabular-nums font-medium">{marker.total_plies}</td>
                                <td className="text-center py-2 px-3 tabular-nums">{marker.cuts}</td>
                              </tr>
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
                            <td className="text-center py-2 px-3 tabular-nums">{plan.total_plies}</td>
                            <td className="text-center py-2 px-3 tabular-nums">{plan.total_cuts}</td>
                          </tr>
                        </tfoot>
                      </table>
                    </div>
                  )}

                  {/* Cost Breakdown */}
                  <div className="px-4 py-3 bg-muted/20 border-t">
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
                  </div>

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
                          <span className="text-sm font-semibold text-indigo-800">CPU Refinement</span>
                        </div>

                        {/* Config panel */}
                        {!isRefiningThis && !hasLayouts && (
                          <div className="space-y-3 p-3 bg-white rounded-lg border">
                            <div className="grid grid-cols-4 gap-3">
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
                              <div>
                                <label className="text-xs font-medium text-muted-foreground block mb-1">Time/Marker (sec)</label>
                                <input
                                  type="number"
                                  value={config.time_limit_s}
                                  onChange={(e) => { const v = e.target.value; setRefinementConfig(cpId, { ...config, time_limit_s: v === '' ? '' as any : parseFloat(v) }) }}
                                  className="w-full px-2 py-1.5 border rounded text-sm"
                                  min={5} max={120} step={5}
                                />
                              </div>
                              <div>
                                <label className="text-xs font-medium text-muted-foreground block mb-1">Orientation</label>
                                <select
                                  value={config.rotation_mode}
                                  onChange={(e) => setRefinementConfig(cpId, { ...config, rotation_mode: e.target.value })}
                                  className="w-full px-2 py-1.5 border rounded text-sm"
                                >
                                  <option value="free">Free (0/180)</option>
                                  <option value="nap_safe">Nap-Safe (0 only)</option>
                                </select>
                              </div>
                            </div>
                            <div className="flex items-center justify-between">
                              <p className="text-xs text-muted-foreground">
                                {plan.markers.length} markers will be refined ({plan.markers.length * config.time_limit_s}s max)
                              </p>
                              <Button size="sm" onClick={() => handleStartRefinement(cpId)}>
                                <Play className="mr-2 h-4 w-4" />
                                Start CPU Refine
                              </Button>
                            </div>
                          </div>
                        )}

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

                        {/* Completed layouts with collapsible marker dropdowns */}
                        {hasLayouts && (() => {
                          const layouts = refStatus!.layouts
                          const expanded = expandedMarkers[cpId] || new Set<number>()
                          const allExpanded = expanded.size === layouts.length
                          const fabricWidth = nestingJobs.find(j => j.status === 'completed')?.fabric_width_inches

                          const toggleMarker = (idx: number) => {
                            setExpandedMarkers(prev => {
                              const current = new Set(prev[cpId] || [])
                              if (current.has(idx)) current.delete(idx)
                              else current.add(idx)
                              return { ...prev, [cpId]: current }
                            })
                          }

                          const toggleAll = () => {
                            setExpandedMarkers(prev => {
                              if (allExpanded) {
                                return { ...prev, [cpId]: new Set<number>() }
                              } else {
                                return { ...prev, [cpId]: new Set(layouts.map((_, i) => i)) }
                              }
                            })
                          }

                          // Build "Size:Qty" ratio string
                          const formatRatio = (ratioStr: string) => {
                            const ratioValues = ratioStr.split('-').map(v => parseInt(v) || 0)
                            return sizes
                              .map((size, i) => ratioValues[i] > 0 ? `${size}:${ratioValues[i]}` : null)
                              .filter(Boolean)
                              .join(', ')
                          }

                          return (
                            <div className="space-y-1">
                              {/* Show All / Hide All toggle */}
                              <div className="flex items-center justify-between mb-2">
                                <span className="text-xs font-medium text-muted-foreground">
                                  {layouts.length} refined markers
                                </span>
                                <Button
                                  variant="ghost"
                                  size="sm"
                                  className="h-7 text-xs gap-1.5"
                                  onClick={toggleAll}
                                >
                                  {allExpanded
                                    ? <><EyeOff className="h-3.5 w-3.5" /> Hide All</>
                                    : <><Eye className="h-3.5 w-3.5" /> Show All</>
                                  }
                                </Button>
                              </div>

                              {/* Marker rows */}
                              {layouts.map((layout, idx) => {
                                const isOpen = expanded.has(idx)
                                const ratioValues = layout.ratio_str.split('-').map(v => parseInt(v) || 0)
                                const bundles = ratioValues.reduce((a: number, b: number) => a + b, 0)
                                const utilPct = layout.utilization * 100

                                return (
                                  <div key={layout.id} className="border rounded-lg overflow-hidden bg-white">
                                    {/* Compact summary row — always visible */}
                                    <button
                                      onClick={() => toggleMarker(idx)}
                                      className="w-full px-4 py-2.5 flex items-center gap-3 hover:bg-muted/30 transition-colors text-left"
                                    >
                                      {isOpen
                                        ? <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
                                        : <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
                                      }
                                      <span className="font-semibold text-sm w-8 shrink-0">{layout.marker_label || `M${idx + 1}`}</span>
                                      {fabricWidth && (
                                        <span className="text-xs text-muted-foreground shrink-0">{fabricWidth}&quot;W</span>
                                      )}
                                      <span className="text-xs text-muted-foreground flex-1 truncate font-mono">
                                        {formatRatio(layout.ratio_str)}
                                      </span>
                                      <span className="text-xs bg-primary/10 text-primary px-1.5 py-0.5 rounded shrink-0">
                                        {bundles}bndl
                                      </span>
                                      <span className={`text-xs font-medium shrink-0 w-14 text-right ${utilPct >= 80 ? 'text-green-600' : utilPct >= 75 ? 'text-amber-600' : 'text-red-600'}`}>
                                        {utilPct.toFixed(1)}%
                                      </span>
                                      <span className="text-xs text-muted-foreground shrink-0 w-16 text-right">
                                        {layout.length_yards.toFixed(2)}yd
                                      </span>
                                      <span className="text-xs text-muted-foreground shrink-0 w-12 text-right">
                                        {layout.computation_time_s.toFixed(1)}s
                                      </span>
                                    </button>

                                    {/* Expanded: SVG preview */}
                                    {isOpen && (
                                      <div className="border-t p-3 overflow-x-auto bg-muted/10">
                                        <div
                                          className="min-w-[400px]"
                                          dangerouslySetInnerHTML={{ __html: layout.svg_preview }}
                                        />
                                      </div>
                                    )}
                                  </div>
                                )
                              })}

                              {/* Download / Re-run buttons */}
                              {!isRefiningThis && refStatus!.status === 'completed' && (
                                <div className="flex items-center gap-3 pt-2">
                                  <Button size="sm" onClick={() => handleDownloadMarkers(cpId)}>
                                    <Download className="mr-2 h-4 w-4" />
                                    Download DXF
                                  </Button>
                                  <Button variant="outline" size="sm" onClick={() => {
                                    setRefinementStatuses(prev => {
                                      const next = { ...prev }
                                      delete next[cpId]
                                      return next
                                    })
                                  }}>
                                    Re-run with Different Settings
                                  </Button>
                                </div>
                              )}
                            </div>
                          )
                        })()}
                      </div>
                    )
                  })()}
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}
    </>
  )
}
