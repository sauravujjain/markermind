'use client'

import { useEffect, useState, useMemo } from 'react'
import Link from 'next/link'
import { api } from '@/lib/api'
import { DashboardLayout } from '@/components/dashboard-layout'
import { AuthGuard } from '@/components/auth-guard'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useToast } from '@/hooks/use-toast'
import {
  ArrowLeft,
  Save,
  Pencil,
  X,
  DollarSign,
  Gauge,
  Clock,
  Users,
  Scissors,
  Layers,
  FileText,
  SlidersHorizontal,
  ArrowRightLeft,
  Ruler,
  CheckSquare,
  Square,
} from 'lucide-react'

const METERS_PER_YARD = 0.9144

function calcSpreadingCostPerMeter(
  laborCostPerHour: number,
  speedMPerMin: number,
  prepBufferPct: number,
  workersPerLay: number,
): number {
  if (speedMPerMin <= 0) return 0
  const base = (laborCostPerHour * workersPerLay) / (60 * speedMPerMin)
  return base * (1 + prepBufferPct / 100)
}

function calcSpreadingCostPerPly(
  laborCostPerHour: number,
  plyEndCutTimeS: number,
  prepBufferPct: number,
  workersPerLay: number,
): number {
  const base = (laborCostPerHour * workersPerLay / 3600) * plyEndCutTimeS
  return base * (1 + prepBufferPct / 100)
}

function calcCuttingCostPerCm(
  laborCostPerHour: number,
  cuttingSpeedCmPerS: number,
  workersPerCut: number,
): number {
  if (cuttingSpeedCmPerS <= 0) return 0
  return (laborCostPerHour * workersPerCut / 3600) / cuttingSpeedCmPerS
}

export default function CostSettingsPage() {
  const { toast } = useToast()
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [spreadingEditOpen, setSpreadingEditOpen] = useState(false)
  const [cuttingEditOpen, setCuttingEditOpen] = useState(false)

  // Spreading input params
  const [laborCostPerHour, setLaborCostPerHour] = useState('1')
  const [spreadingSpeed, setSpreadingSpeed] = useState('20')
  const [prepBuffer, setPrepBuffer] = useState('20')
  const [workersPerLay, setWorkersPerLay] = useState('2')
  const [plyEndCutTime, setPlyEndCutTime] = useState('20')

  // Cutting input params
  const [cuttingSpeed, setCuttingSpeed] = useState('10')
  const [cuttingLaborCost, setCuttingLaborCost] = useState('1')
  const [cuttingWorkers, setCuttingWorkers] = useState('1')

  // Prep cost params
  const [perfPaperCost, setPerfPaperCost] = useState('0.1')
  const [perfPaperEnabled, setPerfPaperEnabled] = useState(true)
  const [underlayerCost, setUnderlayerCost] = useState('0.1')
  const [underlayerEnabled, setUnderlayerEnabled] = useState(true)
  const [topLayerCost, setTopLayerCost] = useState('0.05')
  const [topLayerEnabled, setTopLayerEnabled] = useState(true)

  // Other cost fields
  const [fabricCostPerMeter, setFabricCostPerMeter] = useState('')
  const [maxPlyHeight, setMaxPlyHeight] = useState('')
  const [minPliesByBundle, setMinPliesByBundle] = useState('')

  // Calculated spreading costs (live)
  const spreadingCostPerMeter = useMemo(
    () => calcSpreadingCostPerMeter(
      parseFloat(laborCostPerHour) || 0,
      parseFloat(spreadingSpeed) || 0,
      parseFloat(prepBuffer) || 0,
      parseInt(workersPerLay) || 0,
    ),
    [laborCostPerHour, spreadingSpeed, prepBuffer, workersPerLay],
  )

  const spreadingCostPerPly = useMemo(
    () => calcSpreadingCostPerPly(
      parseFloat(laborCostPerHour) || 0,
      parseFloat(plyEndCutTime) || 0,
      parseFloat(prepBuffer) || 0,
      parseInt(workersPerLay) || 0,
    ),
    [laborCostPerHour, plyEndCutTime, prepBuffer, workersPerLay],
  )

  // Calculated cutting cost (live)
  const cuttingCostPerCm = useMemo(
    () => calcCuttingCostPerCm(
      parseFloat(cuttingLaborCost) || 0,
      parseFloat(cuttingSpeed) || 0,
      parseInt(cuttingWorkers) || 0,
    ),
    [cuttingLaborCost, cuttingSpeed, cuttingWorkers],
  )

  // Calculated prep cost per meter (live)
  const prepCostPerMeter = useMemo(() => {
    let total = 0
    if (perfPaperEnabled) total += parseFloat(perfPaperCost) || 0
    if (underlayerEnabled) total += parseFloat(underlayerCost) || 0
    if (topLayerEnabled) total += parseFloat(topLayerCost) || 0
    return total
  }, [perfPaperCost, perfPaperEnabled, underlayerCost, underlayerEnabled, topLayerCost, topLayerEnabled])

  useEffect(() => {
    loadCostConfig()
  }, [])

  const loadCostConfig = async () => {
    try {
      const config = await api.getCostConfig()
      // Spreading inputs
      setLaborCostPerHour(config.spreading_labor_cost_per_hour.toString())
      setSpreadingSpeed(config.spreading_speed_m_per_min.toString())
      setPrepBuffer(config.spreading_prep_buffer_pct.toString())
      setWorkersPerLay(config.spreading_workers_per_lay.toString())
      setPlyEndCutTime(config.ply_end_cut_time_s.toString())

      // Cutting inputs
      setCuttingSpeed(config.cutting_speed_cm_per_s.toString())
      setCuttingLaborCost(config.cutting_labor_cost_per_hour.toString())
      setCuttingWorkers(config.cutting_workers_per_cut.toString())

      // Prep inputs
      setPerfPaperCost(config.prep_perf_paper_cost_per_m.toString())
      setPerfPaperEnabled(config.prep_perf_paper_enabled)
      setUnderlayerCost(config.prep_underlayer_cost_per_m.toString())
      setUnderlayerEnabled(config.prep_underlayer_enabled)
      setTopLayerCost(config.prep_top_layer_cost_per_m.toString())
      setTopLayerEnabled(config.prep_top_layer_enabled)

      // Convert stored per-yard to per-meter for display
      const fabricPerMeter = config.fabric_cost_per_yard / METERS_PER_YARD
      setFabricCostPerMeter(fabricPerMeter.toFixed(4))

      setMaxPlyHeight(config.max_ply_height.toString())
      setMinPliesByBundle(config.min_plies_by_bundle)
    } catch (error) {
      toast({
        title: 'Failed to load cost settings',
        variant: 'destructive',
      })
    } finally {
      setIsLoading(false)
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsSaving(true)
    try {
      const fabricPerYard = parseFloat(fabricCostPerMeter) * METERS_PER_YARD
      const spreadingPerYard = spreadingCostPerMeter * METERS_PER_YARD
      const cuttingPerInch = cuttingCostPerCm * 2.54

      await api.updateCostConfig({
        fabric_cost_per_yard: fabricPerYard,
        spreading_cost_per_yard: spreadingPerYard,
        spreading_cost_per_ply: spreadingCostPerPly,
        spreading_labor_cost_per_hour: parseFloat(laborCostPerHour),
        spreading_speed_m_per_min: parseFloat(spreadingSpeed),
        spreading_prep_buffer_pct: parseFloat(prepBuffer),
        spreading_workers_per_lay: parseInt(workersPerLay, 10),
        ply_end_cut_time_s: parseFloat(plyEndCutTime),
        cutting_cost_per_inch: cuttingPerInch,
        cutting_speed_cm_per_s: parseFloat(cuttingSpeed),
        cutting_labor_cost_per_hour: parseFloat(cuttingLaborCost),
        cutting_workers_per_cut: parseInt(cuttingWorkers, 10),
        prep_cost_per_meter: prepCostPerMeter,
        prep_cost_per_marker: prepCostPerMeter, // keep legacy field in sync
        prep_perf_paper_cost_per_m: parseFloat(perfPaperCost),
        prep_perf_paper_enabled: perfPaperEnabled,
        prep_underlayer_cost_per_m: parseFloat(underlayerCost),
        prep_underlayer_enabled: underlayerEnabled,
        prep_top_layer_cost_per_m: parseFloat(topLayerCost),
        prep_top_layer_enabled: topLayerEnabled,
        max_ply_height: parseInt(maxPlyHeight, 10),
        min_plies_by_bundle: minPliesByBundle,
      })
      toast({ title: 'Settings saved' })
    } catch (error) {
      toast({
        title: 'Failed to save settings',
        description: error instanceof Error ? error.message : 'Please try again',
        variant: 'destructive',
      })
    } finally {
      setIsSaving(false)
    }
  }

  return (
    <AuthGuard>
    <DashboardLayout>
      <div className="space-y-6">
        <div className="flex items-center space-x-4">
          <Link href="/settings">
            <Button variant="ghost" size="icon">
              <ArrowLeft className="h-4 w-4" />
            </Button>
          </Link>
          <div>
            <h1 className="text-3xl font-bold tracking-tight">Cost Settings</h1>
            <p className="text-muted-foreground">
              Configure cost parameters for cutplan optimization
            </p>
          </div>
        </div>

        {isLoading ? (
          <div className="flex justify-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-6">
            {/* ── Fabric Cost ── */}
            <Card>
              <CardHeader>
                <div className="flex items-center space-x-3">
                  <div className="w-11 h-11 bg-blue-100 rounded-xl flex items-center justify-center">
                    <DollarSign className="h-6 w-6 text-blue-600" />
                  </div>
                  <div>
                    <CardTitle className="text-lg">Fabric Cost</CardTitle>
                    <CardDescription>Base material cost per unit length</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="fabricCostPerMeter" className="flex items-center gap-1.5">
                      <Ruler className="h-4 w-4 text-blue-500" />
                      Cost per Meter (USD)
                    </Label>
                    <Input
                      id="fabricCostPerMeter"
                      type="number"
                      step="0.0001"
                      value={fabricCostPerMeter}
                      onChange={(e) => setFabricCostPerMeter(e.target.value)}
                      required
                    />
                  </div>
                  <div className="flex items-end pb-2">
                    <p className="text-sm text-muted-foreground flex items-center gap-1.5">
                      <ArrowRightLeft className="h-3.5 w-3.5" />
                      ${(parseFloat(fabricCostPerMeter || '0') * METERS_PER_YARD).toFixed(4)} / yard
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* ── Spreading Costs ── */}
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <div className="w-11 h-11 bg-green-100 rounded-xl flex items-center justify-center">
                      <Layers className="h-6 w-6 text-green-600" />
                    </div>
                    <div>
                      <CardTitle className="text-lg">Spreading Costs</CardTitle>
                      <CardDescription>Calculated from labour, speed, and ply parameters</CardDescription>
                    </div>
                  </div>
                  <Button
                    type="button"
                    variant={spreadingEditOpen ? 'secondary' : 'outline'}
                    size="sm"
                    onClick={() => setSpreadingEditOpen(!spreadingEditOpen)}
                  >
                    {spreadingEditOpen
                      ? <><X className="mr-1.5 h-3.5 w-3.5" /> Close</>
                      : <><Pencil className="mr-1.5 h-3.5 w-3.5" /> Edit Parameters</>
                    }
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="rounded-lg border bg-muted/40 p-4 space-y-1">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Spreading Cost / Meter</p>
                    <p className="text-2xl font-semibold">${spreadingCostPerMeter.toFixed(6)}</p>
                    <p className="text-xs text-muted-foreground flex items-center gap-1">
                      <ArrowRightLeft className="h-3 w-3" />
                      ${(spreadingCostPerMeter * METERS_PER_YARD).toFixed(6)} / yard
                    </p>
                  </div>
                  <div className="rounded-lg border bg-muted/40 p-4 space-y-1">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Spreading Cost / Ply</p>
                    <p className="text-2xl font-semibold">${spreadingCostPerPly.toFixed(6)}</p>
                  </div>
                </div>

                {spreadingEditOpen && (
                  <div className="rounded-lg border border-dashed p-5 space-y-4 bg-muted/20">
                    <p className="text-sm font-medium">Input Parameters</p>
                    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                      <div className="space-y-2">
                        <Label htmlFor="laborCost" className="flex items-center gap-1.5">
                          <DollarSign className="h-4 w-4 text-green-500" />
                          Labour Cost (USD/hr)
                        </Label>
                        <Input id="laborCost" type="number" step="0.01" value={laborCostPerHour} onChange={(e) => setLaborCostPerHour(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="spreadingSpeed" className="flex items-center gap-1.5">
                          <Gauge className="h-4 w-4 text-green-500" />
                          Spreading Speed (m/min)
                        </Label>
                        <Input id="spreadingSpeed" type="number" step="0.1" value={spreadingSpeed} onChange={(e) => setSpreadingSpeed(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="prepBuffer" className="flex items-center gap-1.5">
                          <Clock className="h-4 w-4 text-green-500" />
                          Prep/Wait Buffer (%)
                        </Label>
                        <Input id="prepBuffer" type="number" step="1" value={prepBuffer} onChange={(e) => setPrepBuffer(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="workers" className="flex items-center gap-1.5">
                          <Users className="h-4 w-4 text-green-500" />
                          Workers per Lay
                        </Label>
                        <Input id="workers" type="number" step="1" min="1" value={workersPerLay} onChange={(e) => setWorkersPerLay(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="plyEndCut" className="flex items-center gap-1.5">
                          <Scissors className="h-4 w-4 text-green-500" />
                          Ply End Cut Time (s)
                        </Label>
                        <Input id="plyEndCut" type="number" step="1" value={plyEndCutTime} onChange={(e) => setPlyEndCutTime(e.target.value)} required />
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground pt-1">
                      Cost/meter = (labour &times; workers) &divide; (60 &times; speed) &times; (1 + buffer%).{' '}
                      Cost/ply = (labour &times; workers &divide; 3600) &times; ply-cut-time &times; (1 + buffer%).
                    </p>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* ── Cutting Cost ── */}
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-3">
                    <div className="w-11 h-11 bg-orange-100 rounded-xl flex items-center justify-center">
                      <Scissors className="h-6 w-6 text-orange-600" />
                    </div>
                    <div>
                      <CardTitle className="text-lg">Cutting Cost</CardTitle>
                      <CardDescription>Calculated from cutting speed, labour, and workers</CardDescription>
                    </div>
                  </div>
                  <Button
                    type="button"
                    variant={cuttingEditOpen ? 'secondary' : 'outline'}
                    size="sm"
                    onClick={() => setCuttingEditOpen(!cuttingEditOpen)}
                  >
                    {cuttingEditOpen
                      ? <><X className="mr-1.5 h-3.5 w-3.5" /> Close</>
                      : <><Pencil className="mr-1.5 h-3.5 w-3.5" /> Edit Parameters</>
                    }
                  </Button>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="rounded-lg border bg-muted/40 p-4 space-y-1">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Cutting Cost / cm</p>
                    <p className="text-2xl font-semibold">${cuttingCostPerCm.toFixed(6)}</p>
                    <p className="text-xs text-muted-foreground flex items-center gap-1">
                      <ArrowRightLeft className="h-3 w-3" />
                      ${(cuttingCostPerCm * 2.54).toFixed(6)} / inch
                    </p>
                  </div>
                </div>

                {cuttingEditOpen && (
                  <div className="rounded-lg border border-dashed p-5 space-y-4 bg-muted/20">
                    <p className="text-sm font-medium">Input Parameters</p>
                    <div className="grid gap-4 md:grid-cols-3">
                      <div className="space-y-2">
                        <Label htmlFor="cuttingSpeed" className="flex items-center gap-1.5">
                          <Gauge className="h-4 w-4 text-orange-500" />
                          Cutting Speed (cm/s)
                        </Label>
                        <Input id="cuttingSpeed" type="number" step="0.1" value={cuttingSpeed} onChange={(e) => setCuttingSpeed(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="cuttingLaborCost" className="flex items-center gap-1.5">
                          <DollarSign className="h-4 w-4 text-orange-500" />
                          Labour Cost (USD/hr)
                        </Label>
                        <Input id="cuttingLaborCost" type="number" step="0.01" value={cuttingLaborCost} onChange={(e) => setCuttingLaborCost(e.target.value)} required />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="cuttingWorkers" className="flex items-center gap-1.5">
                          <Users className="h-4 w-4 text-orange-500" />
                          Workers per Cut
                        </Label>
                        <Input id="cuttingWorkers" type="number" step="1" min="1" value={cuttingWorkers} onChange={(e) => setCuttingWorkers(e.target.value)} required />
                      </div>
                    </div>
                    <p className="text-xs text-muted-foreground pt-1">
                      Cost/cm = (labour &times; workers &divide; 3600) &divide; cutting speed
                    </p>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* ── Preparatory Cost ── */}
            <Card>
              <CardHeader>
                <div className="flex items-center space-x-3">
                  <div className="w-11 h-11 bg-amber-100 rounded-xl flex items-center justify-center">
                    <FileText className="h-6 w-6 text-amber-600" />
                  </div>
                  <div>
                    <CardTitle className="text-lg">Preparatory Cost</CardTitle>
                    <CardDescription>Consumable paper costs per meter of marker (Auto CNC / Cutter)</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="space-y-4">
                {/* Total */}
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="rounded-lg border bg-muted/40 p-4 space-y-1">
                    <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Total Prep Cost / Meter</p>
                    <p className="text-2xl font-semibold">${prepCostPerMeter.toFixed(4)}</p>
                    <p className="text-xs text-muted-foreground flex items-center gap-1">
                      <ArrowRightLeft className="h-3 w-3" />
                      ${(prepCostPerMeter * METERS_PER_YARD).toFixed(4)} / yard
                    </p>
                  </div>
                </div>

                {/* Line items with checkboxes */}
                <div className="space-y-3">
                  {/* Perforated Paper */}
                  <div
                    className={`flex items-center gap-4 rounded-lg border p-4 transition-colors cursor-pointer ${perfPaperEnabled ? 'bg-amber-50/50 border-amber-200' : 'bg-muted/20 opacity-60'}`}
                    onClick={() => setPerfPaperEnabled(!perfPaperEnabled)}
                  >
                    <button type="button" className="flex-shrink-0 text-amber-600" onClick={(e) => { e.stopPropagation(); setPerfPaperEnabled(!perfPaperEnabled) }}>
                      {perfPaperEnabled ? <CheckSquare className="h-5 w-5" /> : <Square className="h-5 w-5 text-muted-foreground" />}
                    </button>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium">Plotting Paper for Marker Print</p>
                    </div>
                    <div className="flex-shrink-0 w-36" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center gap-1">
                        <span className="text-sm text-muted-foreground">$</span>
                        <Input
                          type="number"
                          step="0.01"
                          value={perfPaperCost}
                          onChange={(e) => setPerfPaperCost(e.target.value)}
                          disabled={!perfPaperEnabled}
                          className="h-8 text-sm"
                        />
                        <span className="text-xs text-muted-foreground whitespace-nowrap">/m</span>
                      </div>
                    </div>
                  </div>

                  {/* Underlayer Paper */}
                  <div
                    className={`flex items-center gap-4 rounded-lg border p-4 transition-colors cursor-pointer ${underlayerEnabled ? 'bg-amber-50/50 border-amber-200' : 'bg-muted/20 opacity-60'}`}
                    onClick={() => setUnderlayerEnabled(!underlayerEnabled)}
                  >
                    <button type="button" className="flex-shrink-0 text-amber-600" onClick={(e) => { e.stopPropagation(); setUnderlayerEnabled(!underlayerEnabled) }}>
                      {underlayerEnabled ? <CheckSquare className="h-5 w-5" /> : <Square className="h-5 w-5 text-muted-foreground" />}
                    </button>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium">Perforated Underlayer Paper (Auto Cutter)</p>
                    </div>
                    <div className="flex-shrink-0 w-36" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center gap-1">
                        <span className="text-sm text-muted-foreground">$</span>
                        <Input
                          type="number"
                          step="0.01"
                          value={underlayerCost}
                          onChange={(e) => setUnderlayerCost(e.target.value)}
                          disabled={!underlayerEnabled}
                          className="h-8 text-sm"
                        />
                        <span className="text-xs text-muted-foreground whitespace-nowrap">/m</span>
                      </div>
                    </div>
                  </div>

                  {/* Top Layer */}
                  <div
                    className={`flex items-center gap-4 rounded-lg border p-4 transition-colors cursor-pointer ${topLayerEnabled ? 'bg-amber-50/50 border-amber-200' : 'bg-muted/20 opacity-60'}`}
                    onClick={() => setTopLayerEnabled(!topLayerEnabled)}
                  >
                    <button type="button" className="flex-shrink-0 text-amber-600" onClick={(e) => { e.stopPropagation(); setTopLayerEnabled(!topLayerEnabled) }}>
                      {topLayerEnabled ? <CheckSquare className="h-5 w-5" /> : <Square className="h-5 w-5 text-muted-foreground" />}
                    </button>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium">Top Layer Paper (Auto Cutter)</p>
                    </div>
                    <div className="flex-shrink-0 w-36" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center gap-1">
                        <span className="text-sm text-muted-foreground">$</span>
                        <Input
                          type="number"
                          step="0.01"
                          value={topLayerCost}
                          onChange={(e) => setTopLayerCost(e.target.value)}
                          disabled={!topLayerEnabled}
                          className="h-8 text-sm"
                        />
                        <span className="text-xs text-muted-foreground whitespace-nowrap">/m</span>
                      </div>
                    </div>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* ── Constraints ── */}
            <Card>
              <CardHeader>
                <div className="flex items-center space-x-3">
                  <div className="w-11 h-11 bg-purple-100 rounded-xl flex items-center justify-center">
                    <SlidersHorizontal className="h-6 w-6 text-purple-600" />
                  </div>
                  <div>
                    <CardTitle className="text-lg">Constraints</CardTitle>
                    <CardDescription>Ply height limits and minimum ply rules by bundle count</CardDescription>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="maxPlyHeight" className="flex items-center gap-1.5">
                      <Layers className="h-4 w-4 text-purple-500" />
                      Max Ply Height
                    </Label>
                    <Input id="maxPlyHeight" type="number" step="1" value={maxPlyHeight} onChange={(e) => setMaxPlyHeight(e.target.value)} required />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="minPliesByBundle" className="flex items-center gap-1.5">
                      <SlidersHorizontal className="h-4 w-4 text-purple-500" />
                      Min Plies by Bundle
                    </Label>
                    <Input
                      id="minPliesByBundle"
                      value={minPliesByBundle}
                      onChange={(e) => setMinPliesByBundle(e.target.value)}
                      placeholder="6:50,5:40,4:30,3:10,2:1,1:1"
                      required
                    />
                    <p className="text-xs text-muted-foreground">
                      Format: bundles:min_plies, comma-separated (e.g., 6:50,5:40,4:30,3:10,2:1,1:1)
                    </p>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Button type="submit" disabled={isSaving} size="lg">
              <Save className="mr-2 h-4 w-4" />
              {isSaving ? 'Saving...' : 'Save Settings'}
            </Button>
          </form>
        )}
      </div>
    </DashboardLayout>
    </AuthGuard>
  )
}
