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
  const [rollPlan, setRollPlan] = useState<RollPlan | null>(null)
  const [existingRollPlans, setExistingRollPlans] = useState<RollPlan[]>([])
  const [isCreating, setIsCreating] = useState(false)
  const [showConfig, setShowConfig] = useState(true)

  // Config
  const [mode, setMode] = useState<string>('both')
  const [numSimulations, setNumSimulations] = useState(100)
  const [minReuseLength, setMinReuseLength] = useState(0.5)
  const [rollSource, setRollSource] = useState<'pseudo' | 'upload'>('pseudo')
  const [pseudoAvg, setPseudoAvg] = useState(100)
  const [pseudoDelta, setPseudoDelta] = useState(20)
  const [gaPopSize, setGaPopSize] = useState(30)
  const [gaGenerations, setGaGenerations] = useState(50)
  const [showGATuning, setShowGATuning] = useState(false)
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadSummary, setUploadSummary] = useState<{ count: number; totalYards: number; avgYards: number; minYards: number; maxYards: number } | null>(null)

  // Progress
  const [isRunning, setIsRunning] = useState(false)
  const [progress, setProgress] = useState(0)
  const [progressMessage, setProgressMessage] = useState('')
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Results
  const [docketSource, setDocketSource] = useState<'mc' | 'ga'>('ga')
  const [dockets, setDockets] = useState<CutDocket[]>([])
  const [expandedDocket, setExpandedDocket] = useState<number | null>(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)

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
      // If there's a completed one, load it
      const completed = plans.find(p => p.status === 'completed')
      const running = plans.find(p => p.status === 'running')
      if (completed) {
        setRollPlan(completed)
        setShowConfig(false)
      } else if (running) {
        setRollPlan(running)
        setShowConfig(false)
        startPolling(running.id)
      }
    }).catch(() => {})
  }, [selectedCutplanId])

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current)
    }
  }, [])

  // Load dockets when rollplan completes or source changes
  useEffect(() => {
    if (!rollPlan || rollPlan.status !== 'completed') return
    const source = docketSource
    // Check if the requested source is available
    if (source === 'ga' && !rollPlan.ga) {
      if (rollPlan.monte_carlo) setDocketSource('mc')
      return
    }
    if (source === 'mc' && !rollPlan.monte_carlo) {
      if (rollPlan.ga) setDocketSource('ga')
      return
    }
    api.getRollPlanDockets(rollPlan.id, source).then(setDockets).catch(() => setDockets([]))
  }, [rollPlan?.id, rollPlan?.status, docketSource])

  const startPolling = useCallback((planId: string) => {
    setIsRunning(true)
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try {
        const status: RollPlanStatus = await api.getRollPlanStatus(planId)
        setProgress(status.progress)
        setProgressMessage(status.message)

        if (status.status === 'completed' || status.status === 'failed' || status.status === 'cancelled') {
          if (pollingRef.current) clearInterval(pollingRef.current)
          pollingRef.current = null
          setIsRunning(false)

          if (status.status === 'completed') {
            const fullPlan = await api.getRollPlan(planId)
            setRollPlan(fullPlan)
            setShowConfig(false)
            toast({ title: 'Simulation complete', description: 'Roll plan results are ready.' })
          } else if (status.status === 'failed') {
            toast({ title: 'Simulation failed', description: status.message, variant: 'destructive' })
          } else {
            toast({ title: 'Simulation cancelled' })
          }
        }
      } catch {
        if (pollingRef.current) clearInterval(pollingRef.current)
        pollingRef.current = null
        setIsRunning(false)
      }
    }, 2000)
  }, [toast])

  const handleRunSimulation = async () => {
    if (!selectedCutplanId) return
    setIsCreating(true)
    try {
      // Create the roll plan
      const result = await api.createRollPlan({
        cutplan_id: selectedCutplanId,
        mode,
        num_simulations: numSimulations,
        min_reuse_length_yards: minReuseLength,
        pseudo_roll_avg_yards: rollSource === 'pseudo' ? pseudoAvg : undefined,
        pseudo_roll_delta_yards: rollSource === 'pseudo' ? pseudoDelta : undefined,
        ga_pop_size: gaPopSize,
        ga_generations: gaGenerations,
      })

      const planId = result.id

      // Upload rolls if needed
      if (rollSource === 'upload' && uploadFile) {
        await api.uploadRolls(planId, uploadFile)
      }

      // Start simulation
      await api.startRollSimulation(planId, { ga_pop_size: gaPopSize, ga_generations: gaGenerations })

      setRollPlan({ id: planId, status: 'running' } as RollPlan)
      setProgress(0)
      setProgressMessage('Starting simulation...')
      startPolling(planId)
      setShowConfig(false)
    } catch (e) {
      toast({ title: 'Failed to start simulation', description: e instanceof Error ? e.message : 'Please try again', variant: 'destructive' })
    } finally {
      setIsCreating(false)
    }
  }

  const handleCancel = async () => {
    if (!rollPlan) return
    try {
      await api.cancelRollSimulation(rollPlan.id)
      if (pollingRef.current) clearInterval(pollingRef.current)
      pollingRef.current = null
      setIsRunning(false)
      toast({ title: 'Simulation cancelled' })
    } catch (e) {
      toast({ title: 'Cancel failed', description: e instanceof Error ? e.message : '', variant: 'destructive' })
    }
  }

  const handleDelete = async () => {
    if (!rollPlan) return
    try {
      await api.deleteRollPlan(rollPlan.id)
      setRollPlan(null)
      setDockets([])
      setShowConfig(true)
      setShowDeleteConfirm(false)
      toast({ title: 'Roll plan deleted' })
    } catch (e) {
      toast({ title: 'Delete failed', description: e instanceof Error ? e.message : '', variant: 'destructive' })
    }
  }

  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      setUploadFile(file)
      setUploadSummary(null)
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
          <CardDescription>Simulate fabric roll allocation to minimize waste</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <label className="text-sm font-medium">Cutplan:</label>
              <Select value={selectedCutplanId} onValueChange={(v) => { setSelectedCutplanId(v); setRollPlan(null); setDockets([]); setShowConfig(true) }}>
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

      {/* Section B: Simulation Config */}
      {showConfig && !isRunning && (
        <Card className="border-primary/30">
          <CardHeader>
            <div className="flex items-center justify-between">
              <div>
                <CardTitle className="text-base">Roll Plan Settings</CardTitle>
                <CardDescription>Assign fabric rolls to markers and estimate waste</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-5">
              {/* Explainer */}
              <div className="text-sm text-muted-foreground bg-muted/30 rounded-lg p-3 space-y-1">
                <p><strong>Simulate Cutting Floor</strong> — runs many random roll orderings to measure how much waste this cutplan produces on average. Tells you if the cutplan is good.</p>
                <p><strong>Optimize Roll Plan</strong> — finds the best order to use your rolls, minimizing wasted end-bits. Produces cut dockets for the cutting room.</p>
                <p><strong>Simulate + Optimize</strong> — does both: first evaluates the cutplan, then finds the best roll assignment.</p>
              </div>

              {/* Mode */}
              <div>
                <label className="text-sm font-medium mb-2 block">What do you want to do?</label>
                <div className="flex gap-2">
                  {[
                    { value: 'monte_carlo', label: 'Simulate Cutting Floor', desc: 'Run N random scenarios to measure waste' },
                    { value: 'ga', label: 'Optimize Roll Plan', desc: 'Find the best roll-to-marker assignment' },
                    { value: 'both', label: 'Simulate + Optimize', desc: 'Evaluate waste, then find best plan' },
                  ].map(opt => (
                    <button
                      key={opt.value}
                      onClick={() => setMode(opt.value)}
                      className={`px-3 py-2 rounded-lg border text-sm transition-all flex-1 ${
                        mode === opt.value
                          ? 'bg-primary text-primary-foreground border-primary'
                          : 'bg-muted/30 hover:bg-muted border-border'
                      }`}
                    >
                      <div className="font-medium">{opt.label}</div>
                      <div className={`text-xs ${mode === opt.value ? 'text-primary-foreground/80' : 'text-muted-foreground'}`}>
                        {opt.desc}
                      </div>
                    </button>
                  ))}
                </div>
              </div>

              {/* MC Runs + Min Reuse Length */}
              <div className="grid grid-cols-2 gap-4">
                {(mode === 'monte_carlo' || mode === 'both') && (
                  <div>
                    <label className="text-sm font-medium mb-1 block">Simulation Runs</label>
                    <input
                      type="number"
                      value={numSimulations}
                      onChange={e => setNumSimulations(parseInt(e.target.value) || 100)}
                      min={10}
                      max={1000}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                    <p className="text-xs text-muted-foreground mt-1">How many random cutting-floor scenarios to test</p>
                  </div>
                )}
                <div>
                  <label className="text-sm font-medium mb-1 block">Min Reuse Length (yd)</label>
                  <input
                    type="number"
                    value={minReuseLength}
                    onChange={e => setMinReuseLength(parseFloat(e.target.value) || 0.5)}
                    min={0}
                    step={0.1}
                    className="w-full px-3 py-2 border rounded-md text-sm"
                  />
                  <p className="text-xs text-muted-foreground mt-1">End-bits shorter than this are scrapped</p>
                </div>
              </div>

              {/* Roll Source */}
              <div>
                <label className="text-sm font-medium mb-2 block">Roll Source</label>
                <div className="flex gap-2">
                  <button
                    onClick={() => setRollSource('pseudo')}
                    className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                      rollSource === 'pseudo'
                        ? 'bg-primary text-primary-foreground border-primary'
                        : 'bg-muted/30 hover:bg-muted border-border'
                    }`}
                  >
                    Pseudo Rolls (Default)
                  </button>
                  <button
                    onClick={() => setRollSource('upload')}
                    className={`px-3 py-2 rounded-lg border text-sm transition-all ${
                      rollSource === 'upload'
                        ? 'bg-primary text-primary-foreground border-primary'
                        : 'bg-muted/30 hover:bg-muted border-border'
                    }`}
                  >
                    <Upload className="inline h-3.5 w-3.5 mr-1" />
                    Upload Excel
                  </button>
                </div>
              </div>

              {/* Pseudo-roll config */}
              {rollSource === 'pseudo' && (
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="text-sm font-medium mb-1 block">Avg Roll Length (yd)</label>
                    <input
                      type="number"
                      value={pseudoAvg}
                      onChange={e => setPseudoAvg(parseFloat(e.target.value) || 100)}
                      min={10}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                  </div>
                  <div>
                    <label className="text-sm font-medium mb-1 block">Delta +/- (yd)</label>
                    <input
                      type="number"
                      value={pseudoDelta}
                      onChange={e => setPseudoDelta(parseFloat(e.target.value) || 20)}
                      min={0}
                      className="w-full px-3 py-2 border rounded-md text-sm"
                    />
                    <p className="text-xs text-muted-foreground mt-1">Range: {pseudoAvg - pseudoDelta}–{pseudoAvg + pseudoDelta} yd</p>
                  </div>
                </div>
              )}

              {/* Upload file */}
              {rollSource === 'upload' && (
                <div>
                  <label className="text-sm font-medium mb-1 block">Roll Inventory Excel</label>
                  <input
                    type="file"
                    accept=".xlsx,.xls"
                    onChange={handleFileUpload}
                    className="w-full px-3 py-2 border rounded-md text-sm file:mr-3 file:rounded-md file:border-0 file:bg-primary file:px-3 file:py-1 file:text-sm file:text-primary-foreground"
                  />
                  <p className="text-xs text-muted-foreground mt-1">
                    Required columns: <span className="font-mono">Roll Number</span>, <span className="font-mono">Roll Length</span>.
                    Optional: <span className="font-mono">Unit</span> (default yd), <span className="font-mono">Roll Width</span>, <span className="font-mono">Shade Group</span>.
                  </p>
                  {uploadFile && (
                    <p className="text-xs text-muted-foreground mt-1">Selected: {uploadFile.name}</p>
                  )}
                  {uploadSummary && (
                    <div className="mt-2 p-2 bg-muted/30 rounded text-xs">
                      {uploadSummary.count} rolls, {uploadSummary.totalYards.toFixed(1)} yd total,
                      avg {uploadSummary.avgYards.toFixed(1)} yd
                      (min {uploadSummary.minYards.toFixed(1)}, max {uploadSummary.maxYards.toFixed(1)})
                    </div>
                  )}
                </div>
              )}

              {/* GA Tuning (collapsible) */}
              {(mode === 'ga' || mode === 'both') && (
                <div>
                  <button
                    onClick={() => setShowGATuning(!showGATuning)}
                    className="flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
                  >
                    {showGATuning ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                    Optimizer Tuning
                  </button>
                  {showGATuning && (
                    <div className="grid grid-cols-2 gap-4 mt-2 pl-5">
                      <div>
                        <label className="text-sm font-medium mb-1 block">Population Size</label>
                        <input
                          type="number"
                          value={gaPopSize}
                          onChange={e => setGaPopSize(parseInt(e.target.value) || 30)}
                          min={10}
                          max={200}
                          className="w-full px-3 py-2 border rounded-md text-sm"
                        />
                      </div>
                      <div>
                        <label className="text-sm font-medium mb-1 block">Generations</label>
                        <input
                          type="number"
                          value={gaGenerations}
                          onChange={e => setGaGenerations(parseInt(e.target.value) || 50)}
                          min={10}
                          max={500}
                          className="w-full px-3 py-2 border rounded-md text-sm"
                        />
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Run button */}
              <Button onClick={handleRunSimulation} disabled={isCreating || (rollSource === 'upload' && !uploadFile)} className="w-full">
                {isCreating ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Play className="mr-2 h-4 w-4" />}
                {isCreating ? 'Creating...' : 'Run Simulation'}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Section C: Progress Card */}
      {isRunning && rollPlan && (
        <Card className="border-purple-200 bg-purple-50/30">
          <CardHeader>
            <div className="flex items-center gap-3">
              <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-purple-500 to-purple-600 flex items-center justify-center">
                <Loader2 className="h-5 w-5 text-white animate-spin" />
              </div>
              <div className="flex-1">
                <CardTitle className="text-base">Roll Simulation Running</CardTitle>
                <CardDescription>{progressMessage || 'Initializing...'}</CardDescription>
              </div>
              <div className="text-right text-sm font-medium tabular-nums">
                {progress}%
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {/* Progress bar */}
              <div className="h-2 bg-purple-200 rounded-full overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-purple-500 to-purple-600 rounded-full transition-all duration-500"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <div className="flex justify-end">
                <Button variant="outline" size="sm" onClick={handleCancel}>
                  <XCircle className="mr-2 h-4 w-4" />
                  Cancel
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Section D: Results */}
      {rollPlan && rollPlan.status === 'completed' && (
        <div className="space-y-6">
          {/* Results header with plan info + new simulation button */}
          <Card className="border-green-200 bg-green-50/30">
            <CardContent className="py-4">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <CheckCircle2 className="h-5 w-5 text-green-600" />
                  <div>
                    <div className="font-medium text-sm">Simulation Complete</div>
                    <div className="text-xs text-muted-foreground">
                      Mode: {rollPlan.mode === 'both' ? 'Simulate + Optimize' : rollPlan.mode === 'monte_carlo' ? 'Simulate' : 'Optimize'}
                      {rollPlan.monte_carlo && ` | ${rollPlan.monte_carlo.num_simulations} simulation runs`}
                      {rollPlan.ga?.generations_run && ` | ${rollPlan.ga.generations_run} optimizer iterations`}
                      {' | '}Rolls: {rollPlan.rolls_count} ({rollPlan.real_rolls_count} real, {rollPlan.pseudo_rolls_count} pseudo)
                    </div>
                  </div>
                </div>
                <Button variant="outline" size="sm" onClick={() => { setShowConfig(true); setRollPlan(null); setDockets([]) }}>
                  <RotateCcw className="mr-2 h-4 w-4" />
                  New Simulation
                </Button>
              </div>
            </CardContent>
          </Card>

          {/* D1: Waste Summary Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {rollPlan.monte_carlo ? (
              <>
                <WasteCard
                  label="Type 1: Unusable"
                  description="Remnants too short for any piece"
                  stats={rollPlan.monte_carlo.unusable_waste}
                  color="gray"
                />
                <WasteCard
                  label="Type 2: End-bits"
                  description="Could have been used (optimization target)"
                  stats={rollPlan.monte_carlo.endbit_waste}
                  color="amber"
                  highlight
                />
                <WasteCard
                  label="Type 3: Returnable"
                  description="Long enough to return to warehouse"
                  stats={rollPlan.monte_carlo.returnable_waste}
                  color="green"
                />
                <WasteCard
                  label="Real Waste (T1+T2)"
                  description="Total unavoidable waste"
                  stats={rollPlan.monte_carlo.real_waste}
                  color="red"
                />
              </>
            ) : rollPlan.ga ? (
              <>
                <SimpleWasteCard label="Type 1: Unusable" value={rollPlan.ga.waste.unusable_yards} color="gray" />
                <SimpleWasteCard label="Type 2: End-bits" value={rollPlan.ga.waste.endbit_yards} color="amber" highlight />
                <SimpleWasteCard label="Type 3: Returnable" value={rollPlan.ga.waste.returnable_yards} color="green" />
                <SimpleWasteCard label="Real Waste" value={rollPlan.ga.waste.real_waste_yards} color="red" />
              </>
            ) : null}
          </div>

          {/* D2: MC vs GA Comparison */}
          {rollPlan.monte_carlo && rollPlan.ga && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">Simulation vs Optimized</CardTitle>
              </CardHeader>
              <CardContent>
                <table className="w-full text-sm">
                  <thead className="bg-muted/30">
                    <tr className="border-b">
                      <th className="text-left py-2 px-3 font-medium">Waste Type</th>
                      <th className="text-right py-2 px-3 font-medium">Avg Simulation (yd)</th>
                      <th className="text-right py-2 px-3 font-medium">Optimized Plan (yd)</th>
                      <th className="text-right py-2 px-3 font-medium">Improvement</th>
                    </tr>
                  </thead>
                  <tbody>
                    <ComparisonRow
                      label="Unusable"
                      mcAvg={rollPlan.monte_carlo.unusable_waste.avg}
                      gaValue={rollPlan.ga.waste.unusable_yards}
                    />
                    <ComparisonRow
                      label="End-bits"
                      mcAvg={rollPlan.monte_carlo.endbit_waste.avg}
                      gaValue={rollPlan.ga.waste.endbit_yards}
                      highlight
                    />
                    <ComparisonRow
                      label="Returnable"
                      mcAvg={rollPlan.monte_carlo.returnable_waste.avg}
                      gaValue={rollPlan.ga.waste.returnable_yards}
                    />
                    <ComparisonRow
                      label="Real Waste"
                      mcAvg={rollPlan.monte_carlo.real_waste.avg}
                      gaValue={rollPlan.ga.waste.real_waste_yards}
                    />
                  </tbody>
                </table>
              </CardContent>
            </Card>
          )}

          {/* D3: Cut Dockets Table */}
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base">Cut Dockets</CardTitle>
                <div className="flex gap-1">
                  {rollPlan.monte_carlo && (
                    <button
                      onClick={() => setDocketSource('mc')}
                      className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                        docketSource === 'mc'
                          ? 'bg-primary text-primary-foreground'
                          : 'bg-muted/30 hover:bg-muted text-muted-foreground'
                      }`}
                    >
                      Simulation Best
                    </button>
                  )}
                  {rollPlan.ga && (
                    <button
                      onClick={() => setDocketSource('ga')}
                      className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                        docketSource === 'ga'
                          ? 'bg-primary text-primary-foreground'
                          : 'bg-muted/30 hover:bg-muted text-muted-foreground'
                      }`}
                    >
                      Optimized Plan
                    </button>
                  )}
                </div>
              </div>
            </CardHeader>
            <CardContent>
              {dockets.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">No dockets available</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/30">
                      <tr className="border-b">
                        <th className="text-left py-2 px-3 font-medium w-8"></th>
                        <th className="text-left py-2 px-3 font-medium">Cut #</th>
                        <th className="text-left py-2 px-3 font-medium">Marker</th>
                        <th className="text-left py-2 px-3 font-medium">Ratio</th>
                        <th className="text-right py-2 px-3 font-medium">Length (yd)</th>
                        <th className="text-right py-2 px-3 font-medium">Plies</th>
                        <th className="text-right py-2 px-3 font-medium">Rolls</th>
                        <th className="text-right py-2 px-3 font-medium">End Bits (yd)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {dockets.map((d) => (
                        <DocketRow
                          key={d.cut_number}
                          docket={d}
                          isExpanded={expandedDocket === d.cut_number}
                          onToggle={() => setExpandedDocket(expandedDocket === d.cut_number ? null : d.cut_number)}
                        />
                      ))}
                    </tbody>
                    <tfoot className="bg-muted/50 font-medium">
                      <tr>
                        <td className="py-2 px-3" colSpan={4}>Total</td>
                        <td className="py-2 px-3 text-right tabular-nums">
                          {dockets.reduce((s, d) => s + d.total_fabric_yards, 0).toFixed(1)}
                        </td>
                        <td className="py-2 px-3 text-right tabular-nums">
                          {dockets.reduce((s, d) => s + d.plies, 0)}
                        </td>
                        <td className="py-2 px-3 text-right tabular-nums">
                          {dockets.reduce((s, d) => s + d.assigned_rolls.length, 0)}
                        </td>
                        <td className="py-2 px-3 text-right tabular-nums">
                          {dockets.reduce((s, d) => s + d.total_end_bit_yards, 0).toFixed(2)}
                        </td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>

          {/* D4: Actions */}
          <div className="flex items-center gap-3 justify-end">
            <Button variant="outline" size="sm" className="text-destructive hover:bg-destructive/10" onClick={() => setShowDeleteConfirm(true)}>
              <Trash2 className="mr-2 h-4 w-4" />
              Delete Roll Plan
            </Button>
          </div>

          {/* Delete confirmation */}
          {showDeleteConfirm && (
            <Card className="border-destructive/50">
              <CardContent className="py-4">
                <div className="flex items-center justify-between">
                  <p className="text-sm">Are you sure you want to delete this roll plan?</p>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" onClick={() => setShowDeleteConfirm(false)}>Cancel</Button>
                    <Button variant="destructive" size="sm" onClick={handleDelete}>Delete</Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* Error state */}
      {rollPlan && rollPlan.status === 'failed' && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="py-6 text-center">
            <XCircle className="h-10 w-10 text-destructive mx-auto mb-3" />
            <h3 className="text-lg font-semibold mb-1">Simulation Failed</h3>
            <p className="text-sm text-muted-foreground mb-4">
              {rollPlan.error_message || 'An unknown error occurred'}
            </p>
            <Button variant="outline" onClick={() => { setShowConfig(true); setRollPlan(null) }}>
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

function WasteCard({
  label,
  description,
  stats,
  color,
  highlight,
}: {
  label: string
  description: string
  stats: { avg: number; std: number; p95: number }
  color: string
  highlight?: boolean
}) {
  const colorMap: Record<string, string> = {
    gray: 'bg-gray-50 border-gray-200',
    amber: 'bg-amber-50 border-amber-200',
    green: 'bg-green-50 border-green-200',
    red: 'bg-red-50 border-red-200',
  }
  const textColorMap: Record<string, string> = {
    gray: 'text-gray-700',
    amber: 'text-amber-700',
    green: 'text-green-700',
    red: 'text-red-700',
  }

  return (
    <div className={`rounded-lg border p-3 ${colorMap[color] || ''} ${highlight ? 'ring-2 ring-amber-400' : ''}`}>
      <div className={`text-xs font-medium ${textColorMap[color] || ''}`}>{label}</div>
      <div className={`text-xl font-bold tabular-nums mt-1 ${textColorMap[color] || ''}`}>
        {stats.avg.toFixed(1)} yd
      </div>
      <div className="text-xs text-muted-foreground mt-1">
        p95: {stats.p95.toFixed(1)} yd
      </div>
      <div className="text-xs text-muted-foreground">{description}</div>
    </div>
  )
}

function SimpleWasteCard({
  label,
  value,
  color,
  highlight,
}: {
  label: string
  value: number
  color: string
  highlight?: boolean
}) {
  const colorMap: Record<string, string> = {
    gray: 'bg-gray-50 border-gray-200',
    amber: 'bg-amber-50 border-amber-200',
    green: 'bg-green-50 border-green-200',
    red: 'bg-red-50 border-red-200',
  }
  const textColorMap: Record<string, string> = {
    gray: 'text-gray-700',
    amber: 'text-amber-700',
    green: 'text-green-700',
    red: 'text-red-700',
  }

  return (
    <div className={`rounded-lg border p-3 ${colorMap[color] || ''} ${highlight ? 'ring-2 ring-amber-400' : ''}`}>
      <div className={`text-xs font-medium ${textColorMap[color] || ''}`}>{label}</div>
      <div className={`text-xl font-bold tabular-nums mt-1 ${textColorMap[color] || ''}`}>
        {value.toFixed(1)} yd
      </div>
    </div>
  )
}

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
        <td className="py-2 px-3 text-right tabular-nums">{docket.plies}</td>
        <td className="py-2 px-3 text-right tabular-nums">{docket.assigned_rolls.length}</td>
        <td className="py-2 px-3 text-right tabular-nums">{docket.total_end_bit_yards.toFixed(2)}</td>
      </tr>
      {isExpanded && (
        <tr className="bg-muted/10">
          <td colSpan={8} className="p-0">
            <div className="px-6 py-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b text-muted-foreground">
                    <th className="text-left py-1 px-2">Roll ID</th>
                    <th className="text-right py-1 px-2">Roll Length (yd)</th>
                    <th className="text-right py-1 px-2">Plies from Roll</th>
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
                      <td className="py-1 px-2 text-right tabular-nums">{roll.end_bit_yards.toFixed(2)}</td>
                      <td className="py-1 px-2 text-center">
                        {roll.is_pseudo ? (
                          <span className="text-xs bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">Pseudo</span>
                        ) : (
                          <span className="text-xs bg-blue-100 text-blue-600 px-1.5 py-0.5 rounded">Real</span>
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
