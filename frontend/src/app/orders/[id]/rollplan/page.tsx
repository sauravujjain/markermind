'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { useOrderContext } from '../order-context'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  api,
  Cutplan,
  RollPlan,
  RollPlanStatus,
  CutDocket,
  MonteCarloResult,
  GAResult,
  TuneStatus,
  WasteAssessment,
  RollPreviewResponse,
} from '@/lib/api'
import { useToast } from '@/hooks/use-toast'
import {
  Loader2,
  Play,
  ChevronDown,
  ChevronRight,
  AlertCircle,
  CheckCircle2,
  XCircle,
  Trash2,
  RotateCcw,
  Upload,
  Wrench,
  Download,
} from 'lucide-react'

export default function RollPlanPage() {
  const { orderId, cutplans, loadData } = useOrderContext()
  const { toast } = useToast()

  // Eligible cutplans: approved or refined
  const eligibleCutplans = cutplans.filter(
    c => c.status === 'approved' || c.status === 'refined' || c.status === 'refining'
  )

  // State
  const [selectedCutplanId, setSelectedCutplanId] = useState<string>('')
  const [existingRollPlans, setExistingRollPlans] = useState<RollPlan[]>([])

  // Shared config
  const [rollSource, setRollSource] = useState<'generated' | 'upload'>('generated')
  const [generatedAvg, setGeneratedAvg] = useState(100)
  const [generatedDelta, setGeneratedDelta] = useState(20)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadPreview, setUploadPreview] = useState<RollPreviewResponse | null>(null)
  const [isParsingPreview, setIsParsingPreview] = useState(false)

  // Evaluate panel state
  const [wasteThreshold, setWasteThreshold] = useState(2.0)
  const [evalRollPlan, setEvalRollPlan] = useState<RollPlan | null>(null)
  const [isEvalRunning, setIsEvalRunning] = useState(false)
  const [evalProgress, setEvalProgress] = useState(0)
  const [evalProgressMessage, setEvalProgressMessage] = useState('')
  const evalPollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [evalDockets, setEvalDockets] = useState<CutDocket[]>([])
  const [evalExpandedDocket, setEvalExpandedDocket] = useState<number | null>(null)

  // Optimize panel state
  const [optRollPlan, setOptRollPlan] = useState<RollPlan | null>(null)
  const [isOptRunning, setIsOptRunning] = useState(false)
  const [optProgress, setOptProgress] = useState(0)
  const [optProgressMessage, setOptProgressMessage] = useState('')
  const optPollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [optDockets, setOptDockets] = useState<CutDocket[]>([])
  const [optExpandedDocket, setOptExpandedDocket] = useState<number | null>(null)

  // UI toggles
  const [showAllReports, setShowAllReports] = useState(false)
  const [showGeneratedConfig, setShowGeneratedConfig] = useState(false)

  // Downloads / misc
  const [isDownloadingExcel, setIsDownloadingExcel] = useState(false)
  const [isDownloadingCutplan, setIsDownloadingCutplan] = useState(false)
  const [isDownloadingEvalExcel, setIsDownloadingEvalExcel] = useState(false)

  // Shortfall confirmation state
  const [shortfallPlanId, setShortfallPlanId] = useState<string | null>(null)
  const [shortfallMessage, setShortfallMessage] = useState('')
  const [shortfallPanel, setShortfallPanel] = useState<'eval' | 'opt'>('eval')

  // Tuning state
  const [isTuning, setIsTuning] = useState(false)
  const [tunedCutplanId, setTunedCutplanId] = useState<string | null>(null)
  const tunePollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Backward compat: rollPlan points to opt panel (for shared handlers)
  const rollPlan = optRollPlan || evalRollPlan

  // Auto-select first eligible cutplan
  useEffect(() => {
    if (eligibleCutplans.length > 0 && !selectedCutplanId) {
      setSelectedCutplanId(eligibleCutplans[0].id)
    }
  }, [eligibleCutplans, selectedCutplanId])

  // Load existing roll plans when cutplan changes
  useEffect(() => {
    if (!selectedCutplanId) return
    api.listRollPlans(selectedCutplanId).then(plans => {
      setExistingRollPlans(plans)
      // Load most recent completed plans into the appropriate panels
      const completedPlans = plans.filter(p => p.status === 'completed')
      for (const plan of completedPlans) {
        if (plan.ga) {
          setOptRollPlan(prev => prev || plan)
        }
        if (plan.monte_carlo && !plan.ga) {
          setEvalRollPlan(prev => prev || plan)
        }
      }
    }).catch(() => {})
  }, [selectedCutplanId])

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (evalPollingRef.current) clearInterval(evalPollingRef.current)
      if (optPollingRef.current) clearInterval(optPollingRef.current)
      if (tunePollingRef.current) clearInterval(tunePollingRef.current)
    }
  }, [])

  // Load eval dockets when eval rollplan completes
  useEffect(() => {
    if (!evalRollPlan || evalRollPlan.status !== 'completed') return
    if (evalRollPlan.monte_carlo) {
      api.getRollPlanDockets(evalRollPlan.id, 'mc').then(setEvalDockets).catch(() => setEvalDockets([]))
    }
  }, [evalRollPlan?.id, evalRollPlan?.status])

  // Load opt dockets when opt rollplan completes
  useEffect(() => {
    if (!optRollPlan || optRollPlan.status !== 'completed') return
    if (optRollPlan.ga) {
      api.getRollPlanDockets(optRollPlan.id, 'ga').then(setOptDockets).catch(() => setOptDockets([]))
    }
  }, [optRollPlan?.id, optRollPlan?.status])

  const startPolling = useCallback((planId: string, panel: 'eval' | 'opt') => {
    const setRunning = panel === 'eval' ? setIsEvalRunning : setIsOptRunning
    const setProgressFn = panel === 'eval' ? setEvalProgress : setOptProgress
    const setMessage = panel === 'eval' ? setEvalProgressMessage : setOptProgressMessage
    const pollingRef = panel === 'eval' ? evalPollingRef : optPollingRef
    const setPlan = panel === 'eval' ? setEvalRollPlan : setOptRollPlan

    setRunning(true)
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try {
        const status: RollPlanStatus = await api.getRollPlanStatus(planId)
        setProgressFn(status.progress)
        setMessage(status.message)

        if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled' || status.status === 'needs_confirmation') {
          if (pollingRef.current) clearInterval(pollingRef.current)
          pollingRef.current = null
          setRunning(false)

          if (status.status === 'needs_confirmation') {
            // Real rolls shortfall — ask user to confirm
            setShortfallPlanId(planId)
            setShortfallMessage(status.message || 'Uploaded rolls do not cover the cutplan requirement.')
            setShortfallPanel(panel)
          } else if (status.status === 'completed') {
            const fullPlan = await api.getRollPlan(planId)
            setPlan(fullPlan)
            // Refresh saved reports list
            if (selectedCutplanId) {
              api.listRollPlans(selectedCutplanId).then(setExistingRollPlans).catch(() => {})
            }
            toast({ title: `${panel === 'eval' ? 'Evaluation' : 'Optimization'} complete`, description: 'Results are ready.' })
          } else if (status.status === 'failed') {
            toast({ title: `${panel === 'eval' ? 'Evaluation' : 'Optimization'} failed`, description: status.message, variant: 'destructive' })
          } else {
            toast({ title: 'Simulation cancelled' })
          }
        }
      } catch {
        if (pollingRef.current) clearInterval(pollingRef.current)
        pollingRef.current = null
        setRunning(false)
      }
    }, 2000)
  }, [toast, selectedCutplanId])

  const handleRunPanel = async (panel: 'eval' | 'opt', confirmShortfall?: boolean, existingPlanId?: string) => {
    if (!selectedCutplanId) return
    const setRunning = panel === 'eval' ? setIsEvalRunning : setIsOptRunning
    const setPlan = panel === 'eval' ? setEvalRollPlan : setOptRollPlan
    const setProgressFn = panel === 'eval' ? setEvalProgress : setOptProgress
    const setMessage = panel === 'eval' ? setEvalProgressMessage : setOptProgressMessage

    setRunning(true)
    try {
      let planId = existingPlanId
      if (!planId) {
        const result = await api.createRollPlan({
          cutplan_id: selectedCutplanId,
          mode: panel === 'eval' ? 'monte_carlo' : 'both',
          num_simulations: 100,
          pseudo_roll_avg_yards: rollSource === 'generated' ? generatedAvg : undefined,
          pseudo_roll_delta_yards: rollSource === 'generated' ? generatedDelta : undefined,
          waste_threshold_pct: wasteThreshold,
          ga_pop_size: 30,
          ga_generations: 50,
        })
        planId = result.id

        // Upload rolls if needed
        if (rollSource === 'upload' && uploadFile) {
          await api.uploadRolls(planId, uploadFile)
        }
      }

      // Start simulation (with confirm_shortfall for real rolls, auto-confirm for generated)
      const autoConfirm = rollSource === 'generated' ? true : (confirmShortfall || false)
      await api.startRollSimulation(planId, {
        ga_pop_size: 30,
        ga_generations: 50,
        confirm_shortfall: autoConfirm,
      })

      setPlan({ id: planId, status: 'running' } as RollPlan)
      setProgressFn(0)
      setMessage('Starting simulation...')
      startPolling(planId, panel)
    } catch (e) {
      toast({ title: 'Failed to start simulation', description: e instanceof Error ? e.message : 'Please try again', variant: 'destructive' })
      setRunning(false)
    }
  }

  const handleCancel = async (panel: 'eval' | 'opt') => {
    const plan = panel === 'eval' ? evalRollPlan : optRollPlan
    const pollingRef = panel === 'eval' ? evalPollingRef : optPollingRef
    const setRunning = panel === 'eval' ? setIsEvalRunning : setIsOptRunning
    if (!plan) return
    try {
      await api.cancelRollSimulation(plan.id)
      if (pollingRef.current) clearInterval(pollingRef.current)
      pollingRef.current = null
      setRunning(false)
      toast({ title: 'Simulation cancelled' })
    } catch (e) {
      toast({ title: 'Cancel failed', description: e instanceof Error ? e.message : '', variant: 'destructive' })
    }
  }

  const handleDelete = async () => {
    const planToDelete = optRollPlan || evalRollPlan
    if (!planToDelete) return
    try {
      await api.deleteRollPlan(planToDelete.id)
      if (optRollPlan?.id === planToDelete.id) { setOptRollPlan(null); setOptDockets([]) }
      if (evalRollPlan?.id === planToDelete.id) { setEvalRollPlan(null); setEvalDockets([]) }
      toast({ title: 'Roll plan deleted' })
    } catch (e) {
      toast({ title: 'Delete failed', description: e instanceof Error ? e.message : '', variant: 'destructive' })
    }
  }

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploadFile(file)
    setUploadPreview(null)
    setIsParsingPreview(true)
    try {
      const preview = await api.parseRollsPreview(file, selectedCutplanId || undefined)
      setUploadPreview(preview)
    } catch (err) {
      toast({ title: 'Failed to parse file', description: err instanceof Error ? err.message : 'Invalid file', variant: 'destructive' })
    } finally {
      setIsParsingPreview(false)
    }
  }

  const handleDownloadTemplate = async () => {
    try {
      await api.downloadRollTemplate()
    } catch {
      toast({ title: 'Download failed', variant: 'destructive' })
    }
  }

  const handleDownloadRollPlanExcel = async () => {
    if (!rollPlan) return
    setIsDownloadingExcel(true)
    try {
      await api.downloadRollPlanExcel(rollPlan.id, 'ga')
      toast({ title: 'Roll plan downloaded' })
    } catch (e: unknown) {
      toast({ title: e instanceof Error ? e.message : 'Download failed', variant: 'destructive' })
    } finally {
      setIsDownloadingExcel(false)
    }
  }

  const handleDownloadEvalExcel = async () => {
    if (!evalRollPlan) return
    setIsDownloadingEvalExcel(true)
    try {
      await api.downloadRollPlanExcel(evalRollPlan.id, 'mc')
      toast({ title: 'Roll plan report downloaded' })
    } catch (e: unknown) {
      toast({ title: e instanceof Error ? e.message : 'Download failed', variant: 'destructive' })
    } finally {
      setIsDownloadingEvalExcel(false)
    }
  }

  const handleDownloadCutplanExcel = async () => {
    setIsDownloadingCutplan(true)
    try {
      const blob = await api.downloadOrderExcel(orderId)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `cutplan_report.xlsx`
      document.body.appendChild(a)
      a.click()
      URL.revokeObjectURL(url)
      document.body.removeChild(a)
      toast({ title: 'Cutplan report downloaded' })
    } catch (e: unknown) {
      toast({ title: e instanceof Error ? e.message : 'Download failed', variant: 'destructive' })
    } finally {
      setIsDownloadingCutplan(false)
    }
  }

  const handleTuneCutplan = async () => {
    if (!rollPlan) return
    setIsTuning(true)
    setTunedCutplanId(null)
    try {
      await api.tuneCutplan(rollPlan.id, {
        avg_roll_length_yards: rollPlan.pseudo_roll_avg_yards || generatedAvg,
        roll_penalty_weight: 2.0,
      })

      // Poll tune status
      if (tunePollingRef.current) clearInterval(tunePollingRef.current)
      tunePollingRef.current = setInterval(async () => {
        try {
          const status: TuneStatus = await api.getTuneStatus(rollPlan.id)
          if (status.status === 'completed') {
            if (tunePollingRef.current) clearInterval(tunePollingRef.current)
            tunePollingRef.current = null
            setIsTuning(false)
            setTunedCutplanId(status.new_cutplan_id || null)
            toast({ title: 'Cutplan tuned', description: 'A roll-optimized cutplan has been created.' })
            loadData()  // Refresh cutplans list
          } else if (status.status === 'failed') {
            if (tunePollingRef.current) clearInterval(tunePollingRef.current)
            tunePollingRef.current = null
            setIsTuning(false)
            toast({ title: 'Tuning failed', description: status.message, variant: 'destructive' })
          }
        } catch {
          if (tunePollingRef.current) clearInterval(tunePollingRef.current)
          tunePollingRef.current = null
          setIsTuning(false)
        }
      }, 2000)
    } catch (e) {
      setIsTuning(false)
      toast({ title: 'Tuning failed', description: e instanceof Error ? e.message : 'Please try again', variant: 'destructive' })
    }
  }

  const selectedCutplan = eligibleCutplans.find(c => c.id === selectedCutplanId)

  // Empty state: no eligible cutplans
  if (eligibleCutplans.length === 0) {
    return (
      <Card className="border-amber-200 bg-amber-50/30">
        <CardContent className="py-8 text-center">
          <AlertCircle className="h-10 w-10 text-amber-500 mx-auto mb-3" />
          <h3 className="text-lg font-semibold mb-1">No Approved Cutplan</h3>
          <p className="text-sm text-muted-foreground">
            Approve a cutplan and run CPU refinement before creating a roll plan.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      {/* Section A: Cutplan Selector */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-lg">Roll Plan</CardTitle>
          <CardDescription>Optimize roll-to-marker assignment and estimate waste</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium">Cutplan:</label>
              <Select value={selectedCutplanId} onValueChange={(v) => { setSelectedCutplanId(v); setEvalRollPlan(null); setOptRollPlan(null); setEvalDockets([]); setOptDockets([]) }}>
                <SelectTrigger className="w-[260px]">
                  <SelectValue placeholder="Select cutplan" />
                </SelectTrigger>
                <SelectContent>
                  {eligibleCutplans.map(cp => (
                    <SelectItem key={cp.id} value={cp.id}>
                      {cp.name || `Cutplan`} ({cp.status})
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {selectedCutplan && (
              <div className="flex gap-4 text-sm text-muted-foreground">
                <span>{selectedCutplan.unique_markers} markers</span>
                <span>{selectedCutplan.total_plies} plies</span>
                <span>{selectedCutplan.total_yards?.toFixed(1)} yd</span>
                <span>{((selectedCutplan.efficiency || 0) * 100).toFixed(1)}% eff</span>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Roll Configuration */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">Roll Configuration</CardTitle>
          <CardDescription>Upload your actual roll inventory or create simulated rolls for cutplan refinement</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {/* Upload Rolls — primary, always visible */}
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-sm font-medium">Upload Roll Inventory</label>
                <button
                  onClick={handleDownloadTemplate}
                  className="flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  <Download className="h-3 w-3" />
                  Download sample template
                </button>
              </div>
              <input
                type="file"
                accept=".xlsx,.xls"
                onChange={(e) => { handleFileUpload(e); setRollSource('upload') }}
                className="w-full px-3 py-2 border rounded-md text-sm file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1 file:text-sm file:text-primary-foreground"
              />
              <p className="text-xs text-muted-foreground mt-1">
                Required columns: <span className="font-mono">Roll Number</span>, <span className="font-mono">Roll Length</span>.
                Optional: <span className="font-mono">Unit</span> (default yd), <span className="font-mono">Roll Width</span>, <span className="font-mono">Shade Group</span>.
              </p>
              {isParsingPreview && (
                <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" /> Parsing file...
                </div>
              )}
              {uploadPreview && (
                <div className="mt-2 space-y-2">
                  <div className="p-2 bg-muted/30 rounded text-xs space-y-1">
                    <div className="font-medium">
                      {uploadPreview.rolls_count} rolls — {uploadPreview.total_length_yards.toFixed(1)} yd total
                    </div>
                    <div className="text-muted-foreground">
                      Avg {uploadPreview.avg_length_yards.toFixed(1)} yd,
                      Median {uploadPreview.median_length_yards.toFixed(1)} yd,
                      Min {uploadPreview.min_length_yards.toFixed(1)} yd,
                      Max {uploadPreview.max_length_yards.toFixed(1)} yd
                    </div>
                  </div>
                  {uploadPreview.preview_rows.length > 0 && (
                    <table className="w-full text-xs border-collapse">
                      <thead>
                        <tr className="border-b text-left text-muted-foreground">
                          <th className="py-1 pr-3">Roll #</th>
                          <th className="py-1 pr-3 text-right">Length (yd)</th>
                        </tr>
                      </thead>
                      <tbody>
                        {uploadPreview.preview_rows.map((r, i) => (
                          <tr key={i} className="border-b border-border/30">
                            <td className="py-1 pr-3 font-mono">{r.roll_number}</td>
                            <td className="py-1 pr-3 text-right">{r.length_yards.toFixed(1)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {uploadPreview.rolls_count > 10 && (
                    <p className="text-xs text-muted-foreground">...and {uploadPreview.rolls_count - 10} more</p>
                  )}
                  {uploadPreview.fabric_required_yards != null && (
                    uploadPreview.total_length_yards < uploadPreview.fabric_required_yards ? (
                      <div className="p-2 rounded text-xs bg-amber-50 border border-amber-200 text-amber-800">
                        Uploaded: {uploadPreview.total_length_yards.toFixed(1)} yd — Required: {uploadPreview.fabric_required_yards.toFixed(1)} yd.
                        Generated rolls will be added to cover the shortfall.
                      </div>
                    ) : (
                      <div className="p-2 rounded text-xs bg-green-50 border border-green-200 text-green-700">
                        <CheckCircle2 className="inline h-3 w-3 mr-1" />
                        Uploaded rolls cover cutplan requirement ({uploadPreview.fabric_required_yards.toFixed(1)} yd)
                      </div>
                    )
                  )}
                </div>
              )}
            </div>

            {/* Simulate with Generated Rolls — collapsible */}
            <div className="border rounded-lg">
              <button
                onClick={() => { setShowGeneratedConfig(!showGeneratedConfig); if (!showGeneratedConfig) setRollSource('generated') }}
                className="w-full flex items-center gap-2 p-3 text-sm font-medium hover:bg-muted/30 transition-colors rounded-lg"
              >
                {showGeneratedConfig ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                Simulate with Generated Rolls
                {rollSource === 'generated' && <span className="text-xs text-primary font-normal ml-auto">Active</span>}
              </button>
              {showGeneratedConfig && (
                <div className="px-3 pb-3 space-y-3">
                  <div className="p-2.5 bg-blue-50 border border-blue-100 rounded text-xs text-blue-700">
                    Use generated rolls to simulate floor waste scenarios when you don&apos;t have real roll inventory. This helps evaluate how different roll length distributions affect end-bit waste.
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <label className="text-xs font-medium mb-1 block">Avg Length (yd)</label>
                      <input
                        type="number"
                        value={generatedAvg}
                        onChange={e => setGeneratedAvg(parseFloat(e.target.value) || 100)}
                        min={10}
                        className="w-full px-2 py-1.5 border rounded-md text-sm"
                      />
                      <p className="text-xs text-muted-foreground mt-0.5">Average length of generated rolls. Typical factory rolls: 80-120 yards.</p>
                    </div>
                    <div>
                      <label className="text-xs font-medium mb-1 block">Delta +/- (yd)</label>
                      <input
                        type="number"
                        value={generatedDelta}
                        onChange={e => setGeneratedDelta(parseFloat(e.target.value) || 20)}
                        min={0}
                        className="w-full px-2 py-1.5 border rounded-md text-sm"
                      />
                      <p className="text-xs text-muted-foreground mt-0.5">Variation range. Rolls will range from ({generatedAvg} - {generatedDelta}) to ({generatedAvg} + {generatedDelta}) = {generatedAvg - generatedDelta}–{generatedAvg + generatedDelta} yd</p>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Pre-flight Warnings */}
      {rollPlan && rollPlan.preflight_warnings && rollPlan.preflight_warnings.length > 0 && (
        <Card className="border-amber-200 bg-amber-50/30">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-amber-500 mt-0.5 flex-shrink-0" />
              <div className="space-y-1">
                <div className="font-medium text-sm text-amber-700">Pre-flight Warnings</div>
                {rollPlan.preflight_warnings.map((w: { message: string }, i: number) => (
                  <p key={i} className="text-sm text-amber-600">{w.message}</p>
                ))}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Shortfall Confirmation Dialog */}
      {shortfallPlanId && (
        <Card className="border-amber-300 bg-amber-50/50">
          <CardContent className="py-4">
            <div className="flex items-start gap-3">
              <AlertCircle className="h-5 w-5 text-amber-500 mt-0.5 flex-shrink-0" />
              <div className="space-y-3 flex-1">
                <div>
                  <div className="font-medium text-sm text-amber-800">Roll Shortfall Detected</div>
                  <p className="text-sm text-amber-700 mt-1">{shortfallMessage}</p>
                </div>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    className="border-amber-300 text-amber-700 hover:bg-amber-100"
                    onClick={() => {
                      // Re-run with confirm_shortfall=true using existing plan
                      handleRunPanel(shortfallPanel, true, shortfallPlanId)
                      setShortfallPlanId(null)
                      setShortfallMessage('')
                    }}
                  >
                    Confirm — Add Generated Rolls
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-muted-foreground"
                    onClick={() => {
                      setShortfallPlanId(null)
                      setShortfallMessage('')
                    }}
                  >
                    Cancel
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Generated rolls will be added to cover the shortfall. Consider uploading more rolls or tuning the cutplan to reduce fabric requirement.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Two-Column Split: Evaluate (left) | Optimize (right) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left Panel: Evaluate Cutplan */}
        <Card className="border-l-4 border-l-teal-400 border-teal-200/60 bg-gradient-to-br from-teal-50/60 to-cyan-50/20 shadow-sm">
          <CardHeader className="pb-3">
            <div className="flex items-start gap-3">
              {/* Shield + pulse — cutplan health check */}
              <svg viewBox="0 0 40 40" className="w-10 h-10 flex-shrink-0 mt-0.5" fill="none" xmlns="http://www.w3.org/2000/svg">
                <path d="M20 4 L33 10 L33 22 C33 30 26 36 20 38 C14 36 7 30 7 22 L7 10 Z" fill="#ccfbf1" stroke="#14b8a6" strokeWidth="1.5" />
                <polyline points="11,22 16,22 18,16 22,28 24,22 29,22" stroke="#0d9488" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" />
              </svg>
              <div>
                <CardTitle className="text-base">Evaluate Cutplan</CardTitle>
                <CardDescription>How robust is your cutplan against end-bit waste with your actual rolls?</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {/* Eval-specific settings */}
              <div>
                <label className="text-xs font-medium mb-1 block">Waste Threshold (%)</label>
                <input
                  type="number"
                  value={wasteThreshold}
                  onChange={e => setWasteThreshold(Math.max(0.1, parseFloat(e.target.value) || 1.0))}
                  min={0.1}
                  step={0.1}
                  className="w-full px-2 py-1.5 border rounded-md text-sm"
                />
                <p className="text-xs text-muted-foreground mt-0.5">Flag if waste exceeds this % of total fabric.</p>
              </div>

              <Button
                onClick={() => handleRunPanel('eval')}
                disabled={isEvalRunning || (rollSource === 'upload' && !uploadFile)}
                className="w-full"
                variant="outline"
              >
                {isEvalRunning ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                {isEvalRunning ? 'Evaluating...' : 'Evaluate Cutplan'}
              </Button>

              {/* Eval Progress */}
              {isEvalRunning && (
                <div className="space-y-2">
                  <div className="h-2 bg-blue-200 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-blue-500 rounded-full transition-all duration-500"
                      style={{ width: `${evalProgress}%` }}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">{evalProgressMessage || 'Initializing...'}</span>
                    <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={() => handleCancel('eval')}>
                      <XCircle className="h-3 w-3 mr-1" />
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {/* Eval Results */}
              {evalRollPlan && evalRollPlan.status === 'completed' && evalRollPlan.monte_carlo && (
                <div className="space-y-3 pt-2 border-t">
                  <div className="flex items-center gap-2 text-sm font-medium text-green-700">
                    <CheckCircle2 className="h-4 w-4" />
                    Evaluation Complete
                  </div>
                  {evalRollPlan.roll_adjustment_message && (
                    <div className="p-2 bg-blue-50 border border-blue-200 rounded text-xs text-blue-700">
                      {evalRollPlan.roll_adjustment_message}
                    </div>
                  )}
                  {(() => {
                    const tf = evalRollPlan.monte_carlo!.total_fabric_required || 1
                    const t1Yd = evalRollPlan.monte_carlo!.unusable_waste.avg
                    const t2Yd = evalRollPlan.monte_carlo!.endbit_waste.avg
                    const overallYd = t1Yd + t2Yd
                    const totalFabric = tf + overallYd
                    const t1Pct = (t1Yd / tf) * 100
                    const t2Pct = (t2Yd / tf) * 100
                    const overallPct = t1Pct + t2Pct
                    return (
                      <div className="space-y-2">
                        <div className="flex gap-2 items-center">
                          <div className="flex-1 rounded-lg border p-2 bg-blue-50 border-blue-200">
                            <div className="text-[10px] font-medium text-blue-600">Total Fabric Needed</div>
                            <div className="text-lg font-bold text-blue-700 tabular-nums">{totalFabric.toFixed(1)} yd</div>
                            <div className="text-[10px] text-blue-500">{tf.toFixed(1)} cutplan + {overallYd.toFixed(1)} waste</div>
                          </div>
                        </div>
                        <div className="flex gap-2">
                          <div className="flex-1 rounded-lg border p-3 bg-red-50 border-red-200">
                            <div className="text-[10px] font-medium text-red-600">Est. Floor Waste</div>
                            <div className="text-2xl font-bold text-red-700 tabular-nums">{overallPct.toFixed(1)}%</div>
                            <div className="text-xs text-red-600/70">{overallYd.toFixed(1)} yd</div>
                          </div>
                          <div className="w-24 rounded-lg border p-2 bg-gray-50 border-gray-200">
                            <div className="text-[10px] font-medium text-gray-600">Unusable</div>
                            <div className="text-sm font-bold text-gray-700 tabular-nums">{t1Pct.toFixed(1)}%</div>
                            <div className="text-[10px] text-gray-500">{t1Yd.toFixed(1)} yd</div>
                          </div>
                          <div className="w-24 rounded-lg border p-2 bg-amber-50 border-amber-200">
                            <div className="text-[10px] font-medium text-amber-600">End-bits</div>
                            <div className="text-sm font-bold text-amber-700 tabular-nums">{t2Pct.toFixed(1)}%</div>
                            <div className="text-[10px] text-amber-500">{t2Yd.toFixed(1)} yd</div>
                          </div>
                        </div>
                      </div>
                    )
                  })()}

                  {/* Waste assessment */}
                  {evalRollPlan.waste_assessment && (
                    <div className={`p-2.5 rounded text-xs ${evalRollPlan.waste_assessment.exceeds_threshold ? 'bg-amber-50 border border-amber-200 text-amber-700' : 'bg-green-50 border border-green-200 text-green-700'}`}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="font-semibold">
                          {evalRollPlan.waste_assessment.exceeds_threshold ? 'Waste exceeds limit' : 'Waste within limit'}
                        </span>
                        <span className="font-mono font-bold">
                          {evalRollPlan.waste_assessment.waste_pct.toFixed(2)}%
                          <span className="text-[10px] font-normal ml-1">/ {evalRollPlan.waste_assessment.threshold_pct}% limit</span>
                        </span>
                      </div>
                      <div className="flex items-center justify-between">
                        <div className="text-[11px] opacity-80">
                          {evalRollPlan.waste_assessment.recommendation}
                        </div>
                        {evalRollPlan.waste_assessment.exceeds_threshold && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={handleTuneCutplan}
                            disabled={isTuning}
                            className="h-6 text-xs border-amber-300 text-amber-700 hover:bg-amber-100 ml-2 shrink-0"
                          >
                            {isTuning ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Wrench className="h-3 w-3 mr-1" />}
                            {isTuning ? 'Tuning...' : 'Tune'}
                          </Button>
                        )}
                      </div>
                      {tunedCutplanId && (
                        <div className="mt-2 p-2 bg-green-50 border border-green-200 rounded space-y-1">
                          <div className="flex items-center gap-1.5 text-green-700 font-medium">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            Roll-tuned cutplan created
                          </div>
                          <a href={`/orders/${orderId}/cutplan`}
                             className="text-xs text-primary hover:underline font-medium">
                            View tuned cutplan →
                          </a>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Eval Dockets */}
                  {evalDockets.length > 0 && (
                    <div>
                      <div className="text-xs font-medium mb-1 flex items-center justify-between">
                        <span>Cut Dockets ({evalDockets.length})</span>
                        <div className="flex items-center gap-2">
                          <span className="text-muted-foreground">{evalRollPlan.monte_carlo.num_simulations} sims</span>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 text-xs"
                            onClick={handleDownloadEvalExcel}
                            disabled={isDownloadingEvalExcel}
                          >
                            {isDownloadingEvalExcel ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Download className="h-3 w-3 mr-1" />}
                            Roll Plan
                          </Button>
                        </div>
                      </div>
                      <div className="overflow-x-auto max-h-64 overflow-y-auto">
                        <table className="w-full text-xs">
                          <thead className="bg-muted/30 sticky top-0">
                            <tr className="border-b">
                              <th className="text-left py-1 px-2 font-medium w-6"></th>
                              <th className="text-left py-1 px-2 font-medium">Cut</th>
                              <th className="text-left py-1 px-2 font-medium">Marker</th>
                              <th className="text-right py-1 px-2 font-medium">Plies</th>
                              <th className="text-right py-1 px-2 font-medium">End Bits</th>
                            </tr>
                          </thead>
                          <tbody>
                            {evalDockets.map(d => (
                              <tr key={d.cut_number} className="border-b border-border/30 hover:bg-muted/20 cursor-pointer" onClick={() => setEvalExpandedDocket(evalExpandedDocket === d.cut_number ? null : d.cut_number)}>
                                <td className="py-1 px-2">{evalExpandedDocket === d.cut_number ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}</td>
                                <td className="py-1 px-2">{d.cut_number}</td>
                                <td className="py-1 px-2">{d.marker_label}</td>
                                <td className="py-1 px-2 text-right tabular-nums">{d.plies}</td>
                                <td className="py-1 px-2 text-right tabular-nums">{d.total_end_bit_yards.toFixed(2)}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  )}

                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-xs"
                    onClick={() => { setEvalRollPlan(null); setEvalDockets([]) }}
                  >
                    <RotateCcw className="h-3 w-3 mr-1" />
                    Re-evaluate
                  </Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Right Panel: Optimize Roll Plan */}
        <Card className="border-l-4 border-l-amber-400 border-amber-200/60 bg-gradient-to-br from-amber-50/50 to-orange-50/20 shadow-sm">
          <CardHeader className="pb-3">
            <div className="flex items-start gap-3">
              {/* Target/crosshair — precision optimization */}
              <svg viewBox="0 0 40 40" className="w-10 h-10 flex-shrink-0 mt-0.5" fill="none" xmlns="http://www.w3.org/2000/svg">
                <circle cx="20" cy="20" r="15" stroke="#d97706" strokeWidth="1.5" fill="#fef3c7" />
                <circle cx="20" cy="20" r="9" stroke="#b45309" strokeWidth="1.3" fill="#fde68a" />
                <circle cx="20" cy="20" r="3.5" fill="#d97706" />
                <line x1="20" y1="2" x2="20" y2="10" stroke="#92400e" strokeWidth="1.3" strokeLinecap="round" />
                <line x1="20" y1="30" x2="20" y2="38" stroke="#92400e" strokeWidth="1.3" strokeLinecap="round" />
                <line x1="2" y1="20" x2="10" y2="20" stroke="#92400e" strokeWidth="1.3" strokeLinecap="round" />
                <line x1="30" y1="20" x2="38" y2="20" stroke="#92400e" strokeWidth="1.3" strokeLinecap="round" />
              </svg>
              <div>
                <CardTitle className="text-base">Optimize Roll Plan</CardTitle>
                <CardDescription>Intelligently assign rolls to markers for minimum waste. Produces cutting dockets — requires floor discipline for best results.</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">

              <Button
                onClick={() => handleRunPanel('opt')}
                disabled={isOptRunning || (rollSource === 'upload' && !uploadFile)}
                className="w-full"
              >
                {isOptRunning ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                {isOptRunning ? 'Optimizing...' : 'Optimize Roll Plan'}
              </Button>

              {/* Opt Progress */}
              {isOptRunning && (
                <div className="space-y-2">
                  <div className="h-2 bg-amber-200 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-amber-500 rounded-full transition-all duration-500"
                      style={{ width: `${optProgress}%` }}
                    />
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">{optProgressMessage || 'Initializing...'}</span>
                    <Button variant="ghost" size="sm" className="h-6 text-xs" onClick={() => handleCancel('opt')}>
                      <XCircle className="h-3 w-3 mr-1" />
                      Cancel
                    </Button>
                  </div>
                </div>
              )}

              {/* Opt Results */}
              {optRollPlan && optRollPlan.status === 'completed' && optRollPlan.ga && (
                <div className="space-y-3 pt-2 border-t">
                  <div className="flex items-center gap-2 text-sm font-medium text-green-700">
                    <CheckCircle2 className="h-4 w-4" />
                    Optimization Complete
                  </div>
                  {optRollPlan.roll_adjustment_message && (
                    <div className="p-2 bg-blue-50 border border-blue-200 rounded text-xs text-blue-700">
                      {optRollPlan.roll_adjustment_message}
                    </div>
                  )}
                  {(() => {
                    const tf = optRollPlan.monte_carlo?.total_fabric_required
                      || optRollPlan.waste_assessment?.total_fabric_yards || 1
                    const t1Yd = optRollPlan.ga!.waste.unusable_yards
                    const t2Yd = optRollPlan.ga!.waste.endbit_yards
                    const overallYd = t1Yd + t2Yd
                    const t1Pct = (t1Yd / tf) * 100
                    const t2Pct = (t2Yd / tf) * 100
                    const overallPct = t1Pct + t2Pct
                    return (
                      <div className="flex gap-2">
                        <div className="flex-1 rounded-lg border p-3 bg-red-50 border-red-200">
                          <div className="text-[10px] font-medium text-red-600">Est. Floor Waste</div>
                          <div className="text-2xl font-bold text-red-700 tabular-nums">{overallPct.toFixed(1)}%</div>
                          <div className="text-xs text-red-600/70">{overallYd.toFixed(1)} yd</div>
                        </div>
                        <div className="w-24 rounded-lg border p-2 bg-gray-50 border-gray-200">
                          <div className="text-[10px] font-medium text-gray-600">Unusable</div>
                          <div className="text-sm font-bold text-gray-700 tabular-nums">{t1Pct.toFixed(1)}%</div>
                          <div className="text-[10px] text-gray-500">{t1Yd.toFixed(1)} yd</div>
                        </div>
                        <div className="w-24 rounded-lg border p-2 bg-amber-50 border-amber-200">
                          <div className="text-[10px] font-medium text-amber-600">End-bits</div>
                          <div className="text-sm font-bold text-amber-700 tabular-nums">{t2Pct.toFixed(1)}%</div>
                          <div className="text-[10px] text-amber-500">{t2Yd.toFixed(1)} yd</div>
                        </div>
                      </div>
                    )
                  })()}

                  {/* Waste assessment */}
                  {optRollPlan.waste_assessment && (
                    <div className={`p-2 rounded text-xs flex items-center justify-between ${optRollPlan.waste_assessment.exceeds_threshold ? 'bg-amber-50 text-amber-700' : 'bg-green-50 text-green-700'}`}>
                      <div>
                        <span className="font-medium">Waste: {optRollPlan.waste_assessment.waste_pct.toFixed(1)}%</span>
                        {' — '}{optRollPlan.waste_assessment.recommendation}
                      </div>
                      {optRollPlan.waste_assessment.exceeds_threshold && (
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={handleTuneCutplan}
                          disabled={isTuning}
                          className="h-6 text-xs border-amber-300 text-amber-700 hover:bg-amber-100"
                        >
                          {isTuning ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Wrench className="h-3 w-3 mr-1" />}
                          {isTuning ? 'Tuning...' : 'Tune'}
                        </Button>
                      )}
                    </div>
                  )}
                  {tunedCutplanId && (
                    <div className="p-3 bg-green-50 border border-green-200 rounded space-y-2">
                      <div className="flex items-center gap-2 text-sm font-medium text-green-700">
                        <CheckCircle2 className="h-4 w-4" />
                        Roll-tuned cutplan created
                      </div>
                      <div className="flex flex-col gap-1.5">
                        <a href={`/orders/${orderId}/cutplan`}
                           className="text-xs text-primary hover:underline font-medium">
                          View tuned cutplan →
                        </a>
                        <p className="text-xs text-muted-foreground">
                          Or use <span className="font-medium">Optimize Roll Plan</span> for intelligent
                          roll-marker matching — requires floor execution discipline.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* MC vs GA Comparison (if both available) */}
                  {optRollPlan.monte_carlo && optRollPlan.ga && (
                    <div>
                      <div className="text-xs font-medium mb-1">Simulation vs Optimized</div>
                      <table className="w-full text-xs">
                        <thead className="bg-muted/30">
                          <tr className="border-b">
                            <th className="text-left py-1 px-2 font-medium">Type</th>
                            <th className="text-right py-1 px-2 font-medium">Avg Sim</th>
                            <th className="text-right py-1 px-2 font-medium">Optimized</th>
                            <th className="text-right py-1 px-2 font-medium">Diff</th>
                          </tr>
                        </thead>
                        <tbody>
                          <ComparisonRow label="Unusable" mcAvg={optRollPlan.monte_carlo.unusable_waste.avg} gaValue={optRollPlan.ga.waste.unusable_yards} />
                          <ComparisonRow label="End-bits" mcAvg={optRollPlan.monte_carlo.endbit_waste.avg} gaValue={optRollPlan.ga.waste.endbit_yards} highlight />
                          <ComparisonRow label="Returnable" mcAvg={optRollPlan.monte_carlo.returnable_waste.avg} gaValue={optRollPlan.ga.waste.returnable_yards} />
                          <ComparisonRow label="Real Waste" mcAvg={optRollPlan.monte_carlo.real_waste.avg} gaValue={optRollPlan.ga.waste.real_waste_yards} />
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Opt Dockets */}
                  {optDockets.length > 0 && (
                    <div>
                      <div className="text-xs font-medium mb-1 flex items-center justify-between">
                        <span>Cut Dockets ({optDockets.length})</span>
                        <div className="flex gap-1">
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 text-xs"
                            onClick={handleDownloadCutplanExcel}
                            disabled={isDownloadingCutplan}
                          >
                            {isDownloadingCutplan ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Download className="h-3 w-3 mr-1" />}
                            Cutplan
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-6 text-xs"
                            onClick={handleDownloadRollPlanExcel}
                            disabled={isDownloadingExcel}
                          >
                            {isDownloadingExcel ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Download className="h-3 w-3 mr-1" />}
                            Roll Plan
                          </Button>
                        </div>
                      </div>
                      <div className="overflow-x-auto max-h-64 overflow-y-auto">
                        <table className="w-full text-xs">
                          <thead className="bg-muted/30 sticky top-0">
                            <tr className="border-b">
                              <th className="text-left py-1 px-2 font-medium w-6"></th>
                              <th className="text-left py-1 px-2 font-medium">Cut</th>
                              <th className="text-left py-1 px-2 font-medium">Marker</th>
                              <th className="text-right py-1 px-2 font-medium">Plies</th>
                              <th className="text-right py-1 px-2 font-medium">Rolls</th>
                              <th className="text-right py-1 px-2 font-medium">End Bits</th>
                            </tr>
                          </thead>
                          <tbody>
                            {optDockets.map(d => (
                              <DocketRow
                                key={d.cut_number}
                                docket={d}
                                isExpanded={optExpandedDocket === d.cut_number}
                                onToggle={() => setOptExpandedDocket(optExpandedDocket === d.cut_number ? null : d.cut_number)}
                              />
                            ))}
                          </tbody>
                          <tfoot className="bg-muted/50 font-medium">
                            <tr>
                              <td className="py-1 px-2" colSpan={3}>Total</td>
                              <td className="py-1 px-2 text-right tabular-nums">{optDockets.reduce((s, d) => s + d.plies, 0)}</td>
                              <td className="py-1 px-2 text-right tabular-nums">{optDockets.reduce((s, d) => s + d.assigned_rolls.length, 0)}</td>
                              <td className="py-1 px-2 text-right tabular-nums">{optDockets.reduce((s, d) => s + d.total_end_bit_yards, 0).toFixed(2)}</td>
                            </tr>
                          </tfoot>
                        </table>
                      </div>
                    </div>
                  )}

                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-xs"
                    onClick={() => { setOptRollPlan(null); setOptDockets([]) }}
                  >
                    <RotateCcw className="h-3 w-3 mr-1" />
                    Re-optimize
                  </Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Saved Reports — at the bottom, collapsible */}
      {existingRollPlans.filter(p => p.status === 'completed').length > 0 && (() => {
        const completedPlans = existingRollPlans.filter(p => p.status === 'completed')
        const visiblePlans = showAllReports ? completedPlans : completedPlans.slice(0, 3)
        return (
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">Saved Reports</CardTitle>
              <CardDescription>Completed roll plans for this cutplan</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {visiblePlans.map(plan => {
                  const wasteYards = (plan as Record<string, unknown>).ga_real_waste_yards as number | undefined
                    ?? (plan as Record<string, unknown>).mc_real_waste_avg as number | undefined
                  const totalFabric = (plan as Record<string, unknown>).total_fabric_required as number | undefined
                  const wastePct = totalFabric && wasteYards != null
                    ? ((wasteYards / totalFabric) * 100).toFixed(1)
                    : null
                  const isActive = rollPlan?.id === plan.id
                  return (
                    <div
                      key={plan.id}
                      className={`flex items-center justify-between p-3 rounded-lg border transition-all cursor-pointer ${
                        isActive
                          ? 'border-primary bg-primary/5'
                          : 'border-border hover:border-primary/40 hover:bg-muted/30'
                      }`}
                      onClick={() => {
                        if (plan.ga) setOptRollPlan(plan)
                        else setEvalRollPlan(plan)
                      }}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <div className={`h-2 w-2 rounded-full flex-shrink-0 ${isActive ? 'bg-primary' : 'bg-green-500'}`} />
                        <div className="min-w-0">
                          <div className="text-sm font-medium truncate">
                            {plan.color_code || 'All Colors'}
                            {plan.input_type && <span className="ml-2 text-xs text-muted-foreground">({plan.input_type})</span>}
                          </div>
                          <div className="text-xs text-muted-foreground">
                            {new Date(plan.created_at).toLocaleDateString()} {new Date(plan.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                            {wastePct && <span className="ml-2">Waste: {wastePct}%</span>}
                            {wasteYards != null && <span className="ml-1">({wasteYards.toFixed(1)} yd)</span>}
                          </div>
                        </div>
                      </div>
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 text-xs"
                          onClick={async (e) => {
                            e.stopPropagation()
                            try {
                              await api.downloadRollPlanExcel(plan.id, 'ga')
                              toast({ title: 'Roll plan downloaded' })
                            } catch { toast({ title: 'Download failed', variant: 'destructive' }) }
                          }}
                        >
                          <Download className="h-3 w-3 mr-1" />
                          Roll Plan
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 text-xs"
                          onClick={async (e) => {
                            e.stopPropagation()
                            try {
                              const blob = await api.downloadOrderExcel(orderId)
                              const url = URL.createObjectURL(blob)
                              const a = document.createElement('a')
                              a.href = url
                              a.download = `cutplan_report.xlsx`
                              document.body.appendChild(a)
                              a.click()
                              URL.revokeObjectURL(url)
                              document.body.removeChild(a)
                            } catch { toast({ title: 'Download failed', variant: 'destructive' }) }
                          }}
                        >
                          <Download className="h-3 w-3 mr-1" />
                          Cutplan
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 w-7 p-0 text-destructive hover:text-destructive"
                          onClick={async (e) => {
                            e.stopPropagation()
                            try {
                              await api.deleteRollPlan(plan.id)
                              setExistingRollPlans(prev => prev.filter(p => p.id !== plan.id))
                              if (evalRollPlan?.id === plan.id) { setEvalRollPlan(null); setEvalDockets([]) }
                              if (optRollPlan?.id === plan.id) { setOptRollPlan(null); setOptDockets([]) }
                              toast({ title: 'Roll plan deleted' })
                            } catch { toast({ title: 'Delete failed', variant: 'destructive' }) }
                          }}
                        >
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </div>
                  )
                })}
                {completedPlans.length > 3 && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="w-full text-xs"
                    onClick={() => setShowAllReports(!showAllReports)}
                  >
                    {showAllReports ? 'Show less' : `Show all (${completedPlans.length})`}
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        )
      })()}

      {/* Error state (either panel) */}
      {((evalRollPlan && evalRollPlan.status === 'failed') || (optRollPlan && optRollPlan.status === 'failed')) && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-6 text-center">
            <XCircle className="h-10 w-10 text-destructive mx-auto mb-3" />
            <h3 className="text-lg font-semibold mb-1">Simulation Failed</h3>
            <p className="text-sm text-muted-foreground mb-4">
              {(evalRollPlan?.status === 'failed' ? evalRollPlan.error_message : optRollPlan?.error_message) || 'An unknown error occurred'}
            </p>
            <Button variant="outline" onClick={() => { setEvalRollPlan(null); setOptRollPlan(null) }}>
              <RotateCcw className="mr-2 h-4 w-4" />
              Try Again
            </Button>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

// --- Sub-components ---


function ComparisonRow({
  label,
  mcAvg,
  gaValue,
  highlight,
}: {
  label: string
  mcAvg: number
  gaValue: number
  highlight?: boolean
}) {
  const improvement = mcAvg > 0 ? ((mcAvg - gaValue) / mcAvg) * 100 : 0
  const improved = gaValue < mcAvg

  return (
    <tr className={`border-b border-border/50 ${highlight ? 'bg-amber-50/50' : ''}`}>
      <td className={`py-2 px-3 ${highlight ? 'font-medium' : ''}`}>{label}</td>
      <td className="py-2 px-3 text-right tabular-nums">{mcAvg.toFixed(2)}</td>
      <td className="py-2 px-3 text-right tabular-nums">{gaValue.toFixed(2)}</td>
      <td className={`py-2 px-3 text-right tabular-nums ${improved ? 'text-green-600' : 'text-red-600'}`}>
        {improved ? '-' : '+'}{Math.abs(improvement).toFixed(1)}%
      </td>
    </tr>
  )
}

function DocketRow({
  docket,
  isExpanded,
  onToggle,
}: {
  docket: CutDocket
  isExpanded: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr className="border-b border-border/50 hover:bg-muted/20 cursor-pointer" onClick={onToggle}>
        <td className="py-2 px-3">
          {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </td>
        <td className="py-2 px-3 font-medium">{docket.cut_number}</td>
        <td className="py-2 px-3">{docket.marker_label}</td>
        <td className="py-2 px-3 font-mono text-xs">{docket.ratio_str}</td>
        <td className="py-2 px-3 text-right tabular-nums">{docket.marker_length_yards.toFixed(2)}</td>
        <td className="py-2 px-3 text-right tabular-nums">
          {(docket.plies_planned ?? docket.plies) < docket.plies ? (
            <span className="text-amber-600" title={`Shortfall: ${docket.plies - (docket.plies_planned ?? docket.plies)} plies`}>
              {docket.plies_planned ?? docket.plies}/{docket.plies}
            </span>
          ) : (
            docket.plies
          )}
        </td>
        <td className="py-2 px-3 text-right tabular-nums">{docket.assigned_rolls.length}</td>
        <td className="py-2 px-3 text-right tabular-nums">
          {docket.assigned_rolls.reduce((s, r) => s + r.fabric_used_yards, 0).toFixed(1)}
        </td>
        <td className="py-2 px-3 text-right tabular-nums">{docket.total_end_bit_yards.toFixed(2)}</td>
      </tr>
      {isExpanded && (
        <tr className="bg-muted/10">
          <td colSpan={9} className="p-0">
            <div className="px-6 py-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th className="text-left py-1 px-2">Roll ID</th>
                    <th className="text-right py-1 px-2">Roll Length (yd)</th>
                    <th className="text-right py-1 px-2">Plies from Roll</th>
                    <th className="text-right py-1 px-2">Fabric Used (yd)</th>
                    <th className="text-right py-1 px-2">End Bit (yd)</th>
                    <th className="text-center py-1 px-2">Type</th>
                  </tr>
                </thead>
                <tbody>
                  {docket.assigned_rolls.map((roll, i) => (
                    <tr key={i} className="border-b border-border/30">
                      <td className="py-1 px-2 font-mono">{roll.roll_id}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{roll.roll_length_yards.toFixed(1)}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{roll.plies_from_roll}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{roll.fabric_used_yards.toFixed(1)}</td>
                      <td className="py-1 px-2 text-right tabular-nums">{roll.end_bit_yards.toFixed(2)}</td>
                      <td className="py-1 px-2 text-center">
                        {roll.is_pseudo ? (
                          <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">Generated</span>
                        ) : (
                          <span className="text-xs bg-blue-100 text-blue-600 px-1.5 py-0.5 rounded">Actual</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
